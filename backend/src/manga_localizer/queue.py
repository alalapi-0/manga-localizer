from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.config import Settings
from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    JobStatus,
    PageGeneration,
    PageLineageEvent,
    RegionOCRAttempt,
    TextRegion,
)
from manga_localizer.imaging import (
    DEFAULT_REPAIR_SETTINGS,
    expand_typeset_region_ids,
    overflow_region_ids,
    restore_clean_region_boxes,
    typeset_image,
)
from manga_localizer.logging_utils import without_secrets
from manga_localizer.providers.detection import (
    consolidate_text_regions,
    detection_min_side_for_image,
    detection_region_is_usable,
)
from manga_localizer.providers.inpainting_lama import (
    COMPONENT_CONTEXT_PADDING,
    COMPONENT_INFERENCE_PADDING,
    DEFAULT_INFERENCE_PADDING,
    MODEL_SIZE,
    TILE_OVERLAP,
)
from manga_localizer.providers.ocr import OCRRegion
from manga_localizer.providers.registry import ProviderRegistry
from manga_localizer.security import (
    atomic_write_bytes,
    normalize_remote_endpoints,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.clean_plates import publish_clean_plate_candidate
from manga_localizer.services.exporting import (
    choose_export_root,
    ensure_project_bundle,
    export_image,
    require_strict_generated_export_inpaint,
    validate_image_export_readiness,
    write_json_export_summary,
)
from manga_localizer.services.images import (
    StagePrerequisiteConflict,
    clear_stage_reviews,
    image_path,
    invalidate_image_pipeline,
    make_inpaint_provenance,
    require_current_accepted_stage_review,
    reset_image_review,
)
from manga_localizer.services.inpaint_candidates import (
    inpaint_candidate_manifest_digest,
    prepare_page_inpaint_candidates,
    write_page_inpaint_candidates,
)
from manga_localizer.services.masks import publish_mask_artifact
from manga_localizer.services.page_lineage import (
    JobMutationBinding,
    PageLineageConflict,
    g4_region_state_checksum,
    g6_ocr_state_checksum,
    job_mutation_binding,
    normalize_job_lineage,
    record_detect_regions_produced,
    record_job_artifact_produced,
    record_job_enqueued_events,
    record_job_item_finished,
    record_ocr_attempts_produced,
    require_current_background_classifications,
    require_current_text_present_quality_plate,
    require_job_lineage_for_execution,
    require_supported_lineage_job_kind,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectRegistry,
    ProjectStore,
    add_revision,
    region_payload,
    safe_error,
)
from manga_localizer.services.trust import (
    TRUST_POLICY_VERSION,
    invalidate_trust,
    is_region_trusted,
    persist_legacy_recognition,
    recognition_payload,
    recognition_uses_input_variant,
    region_trust,
    with_detection_evidence,
    with_human_ignore,
    with_ocr_evidence,
)

logger = logging.getLogger(__name__)


class JobConflict(ProjectError):
    pass


class StaleJobResult(ProjectError):
    """A computed result that lost its optimistic-concurrency race."""


class PersistentJobQueue:
    def __init__(
        self,
        registry: ProjectRegistry,
        providers: ProviderRegistry,
        settings: Settings,
    ):
        self.registry = registry
        self.providers = providers
        self.settings = settings
        self._runner: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @staticmethod
    def _requires_ai_inpaint(project_settings: dict[str, Any]) -> bool:
        value = project_settings.get("requireAIInpaintBeforeDownstream", False)
        if type(value) is not bool:
            raise ProjectError("requireAIInpaintBeforeDownstream must be a boolean")
        return value

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    async def start(self) -> None:
        if self.running:
            return
        for store in self.registry.stores():
            store.recover_jobs()
        self._stopping.clear()
        self._runner = asyncio.create_task(self._loop(), name="manga-localizer-job-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._runner is None:
            return
        self._runner.cancel()
        try:
            await self._runner
        except asyncio.CancelledError:
            pass
        self._runner = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            claimed = self._claim_next()
            if claimed is None:
                await asyncio.sleep(self.settings.worker_poll_seconds)
                continue
            store, job_id = claimed
            try:
                await self._execute(store, job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected persistent queue failure for job %s", job_id)
                self._fail_job(store, job_id, "Unexpected worker failure")

    def _claim_next(self) -> tuple[ProjectStore, str] | None:
        for store in self.registry.stores():
            with store.session() as session:
                job = session.scalar(
                    select(Job)
                    .where(Job.status == JobStatus.QUEUED.value)
                    .order_by(Job.created_at)
                    .limit(1)
                )
                if job is None:
                    continue
                job.status = JobStatus.RUNNING.value
                job.error = None
                return store, job.id
        return None

    async def _execute(self, store: ProjectStore, job_id: str) -> None:
        with store.session() as session:
            job = session.scalar(
                select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
            )
            if job is None:
                return
            job_kind = job.kind
            job_options = dict(job.options)
            concurrency = self._job_concurrency(job_kind, job_options)
            item_ids = [item.id for item in job.items if item.status == JobStatus.QUEUED.value]
        for offset in range(0, len(item_ids), concurrency):
            batch = item_ids[offset : offset + concurrency]
            started: list[str] = []
            for item_id in batch:
                if not self._begin_item(store, job_id, item_id):
                    break
                started.append(item_id)
            await asyncio.gather(
                *(self._execute_item(store, job_id, item_id) for item_id in started)
            )
            if len(started) != len(batch):
                return
        self._recompute(store, job_id)
        store.write_snapshot()
        if job_kind == "export":
            try:
                await asyncio.to_thread(
                    self._finalize_export_bundle,
                    store,
                    job_id,
                    job_options,
                )
            except Exception as error:
                self._fail_job(
                    store,
                    job_id,
                    f"Export finalization failed: {safe_error(error)}",
                )
                store.write_snapshot()

    @staticmethod
    def _completed_export_item_is_current(session, item: JobItem) -> bool:
        if item.image_id is None:
            return False
        image = session.get(ImageAsset, item.image_id)
        exported_revision = (item.output or {}).get("imageRevision")
        return bool(
            image is not None
            and isinstance(exported_revision, int)
            and not isinstance(exported_revision, bool)
            # A successful export advances the image exactly once while marking
            # its export status done. Any later mutation makes this item stale.
            and image.revision == exported_revision + 1
        )

    @staticmethod
    def _finalize_export_bundle(
        store: ProjectStore,
        job_id: str,
        job_options: dict[str, Any],
    ) -> None:
        with store.lock:
            with store.session() as session:
                finished_job = session.scalar(
                    select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
                )
                if (
                    finished_job is None
                    or finished_job.status
                    not in {JobStatus.RUNNING.value, JobStatus.COMPLETED.value}
                    or any(item.status != JobStatus.COMPLETED.value for item in finished_job.items)
                ):
                    return
                if finished_job.options.get("bundleFinalized") is True:
                    return
                require_job_lineage_for_execution(store, session, finished_job)
                stale_items = [
                    item
                    for item in finished_job.items
                    if not PersistentJobQueue._completed_export_item_is_current(session, item)
                ]
                if stale_items:
                    raise ProjectError(
                        "One or more pages changed after their export item completed; "
                        "retry with overwrite or start a new export job"
                    )
            export_format = str(job_options.get("format", "both"))
            image_variant = str(job_options.get("imageVariant", "typeset"))
            if export_format in {"images", "both"}:
                for item in finished_job.items:
                    if item.image_id is None:
                        raise ProjectError("Export job item has no image")
                    if require_strict_generated_export_inpaint(
                        store,
                        item.image_id,
                        export_format=export_format,
                    ):
                        validate_image_export_readiness(
                            store,
                            item.image_id,
                            export_format=export_format,
                            image_variant=image_variant,
                        )
            include_assets = export_format == "both"
            export_root = choose_export_root(
                store,
                job_options.get("outputPath"),
                job_id,
                include_assets=include_assets,
            )
            summary: dict[str, str | None] | None = None
            if export_format == "both":
                ensure_project_bundle(
                    store,
                    export_root,
                    finalized_job_id=job_id,
                )
            elif export_format == "json":
                summary = write_json_export_summary(store, export_root, job_id)
            with store.session() as session:
                finished_job = session.scalar(
                    select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
                )
                if finished_job is None:
                    raise ProjectError("Export job disappeared during bundle finalization")
                finalized_options = {
                    **dict(finished_job.options),
                    "bundleFinalized": True,
                }
                if summary is not None:
                    finalized_options["summaryArtifact"] = summary["artifact"]
                    finalized_options["summaryConflict"] = summary["conflict"]
                finished_job.options = finalized_options
                PersistentJobQueue._recompute_in_session(finished_job)
            store.write_snapshot()

    async def _execute_item(self, store: ProjectStore, job_id: str, item_id: str) -> None:
        await asyncio.to_thread(self._execute_item_sync, store, job_id, item_id)

    def _execute_item_sync(self, store: ProjectStore, job_id: str, item_id: str) -> None:
        with store.session() as session:
            job = session.get(Job, job_id)
            kind = job.kind if job is not None else None
            item = session.get(JobItem, item_id)
            image = (
                session.get(ImageAsset, item.image_id)
                if item is not None and item.image_id is not None
                else None
            )
            expected_image_revision = image.revision if image is not None else None

        def process_and_finish(failure_revision: int | None) -> None:
            try:
                output = self._process_item(store, job_id, item_id)
            except Exception as error:
                self._finish_item(
                    store,
                    job_id,
                    item_id,
                    error=error,
                    expected_image_revision=failure_revision,
                )
            else:
                self._finish_item(store, job_id, item_id, output=output)
            store.write_snapshot()

        if kind == "export":
            # Keep the metadata read, filesystem copy, and export status transition indivisible
            # relative to editor mutations. Export jobs are serialized by _job_concurrency.
            with store.lock:
                # An edit may commit between the initial lookup and this critical section.
                # Refresh the failure guard so an error against the newer state is not
                # misclassified as a stale provider failure.
                with store.session() as session:
                    current_item = session.get(JobItem, item_id)
                    current_image = (
                        session.get(ImageAsset, current_item.image_id)
                        if current_item is not None and current_item.image_id is not None
                        else None
                    )
                    export_failure_revision = (
                        current_image.revision if current_image is not None else None
                    )
                process_and_finish(export_failure_revision)
            return
        process_and_finish(expected_image_revision)

    @staticmethod
    def _typeset_region_ids(options: dict[str, Any]) -> list[str]:
        raw = options.get("regionIds")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ProjectError("regionIds must be a list of region ids")
        ids: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item or item in seen:
                continue
            seen.add(item)
            ids.append(item)
        return ids

    @staticmethod
    def _require_translation_acceptance(
        image_id: str,
        regions: list[TextRegion],
    ) -> None:
        active = [region for region in regions if not region.ignored]
        mismatches: list[str] = []
        if any(not is_region_trusted(region) for region in active):
            mismatches.append("untrusted-region")
        if any(not region.confirmed for region in active):
            mismatches.append("unconfirmed-translation")
        if any(not (region.translation_text or "").strip() for region in active):
            mismatches.append("missing-translation")
        if mismatches:
            raise StagePrerequisiteConflict(
                "Every typeset region must have a trusted, confirmed, non-empty translation",
                resource=f"image:{image_id}",
                stage="translation",
                reason="translation-review-required",
                mismatches=mismatches,
            )

    @staticmethod
    def _job_concurrency(kind: str, options: dict[str, Any]) -> int:
        raw = options.get("concurrency", 1)
        if isinstance(raw, bool):
            raise ProjectError("Job concurrency must be an integer from 1 to 8")
        try:
            concurrency = int(raw)
        except (TypeError, ValueError) as error:
            raise ProjectError("Job concurrency must be an integer from 1 to 8") from error
        if (
            concurrency < 1
            or concurrency > 8
            or str(raw).strip()
            not in {
                str(concurrency),
                f"{concurrency}.0",
            }
        ):
            raise ProjectError("Job concurrency must be an integer from 1 to 8")
        # Output conflict resolution is intentionally serialized so two pages with the same
        # flattened name cannot race between the existence check and exclusive write.
        return 1 if kind == "export" else concurrency

    def _begin_item(self, store: ProjectStore, job_id: str, item_id: str) -> bool:
        with store.session() as session:
            job = session.get(Job, job_id)
            item = session.get(JobItem, item_id)
            if job is None or item is None:
                return False
            if job.status in {JobStatus.PAUSED.value, JobStatus.CANCELLED.value}:
                return False
            if job.status != JobStatus.RUNNING.value:
                return False
            item.status = JobStatus.RUNNING.value
            published_typeset = bool(
                job.kind == "typeset"
                and item.started_at is not None
                and session.scalar(
                    select(PageLineageEvent.id).where(
                        PageLineageEvent.job_id == job.id,
                        PageLineageEvent.job_item_id == item.id,
                        PageLineageEvent.operation == "typeset-candidate-produced",
                    )
                )
            )
            if not published_typeset:
                item.started_at = datetime.now(UTC)
            item.error = None
            return True

    def _finish_item(
        self,
        store: ProjectStore,
        job_id: str,
        item_id: str,
        *,
        output: dict[str, Any] | None = None,
        error: Exception | None = None,
        expected_image_revision: int | None = None,
    ) -> None:
        with store.session() as session:
            job = session.get(Job, job_id)
            item = session.get(JobItem, item_id)
            if job is None or item is None or item.status == JobStatus.CANCELLED.value:
                return
            item.finished_at = datetime.now(UTC)
            item.progress = 1.0
            if error is None:
                item.status = JobStatus.COMPLETED.value
                item.output = without_secrets(output or {})
                item.error = None
                if job.kind == "export" and item.image_id:
                    image = session.get(ImageAsset, item.image_id)
                    if image is not None:
                        status = dict(image.status)
                        status["export"] = "done"
                        image.status = status
                        image.processing_errors = [
                            recorded
                            for recorded in (image.processing_errors or [])
                            if recorded.get("stage") != "export"
                        ]
                        image.revision += 1
            else:
                image = session.get(ImageAsset, item.image_id) if item.image_id else None
                if (
                    image is not None
                    and expected_image_revision is not None
                    and image.revision != expected_image_revision
                    and not isinstance(error, StaleJobResult)
                ):
                    error = StaleJobResult(
                        f"Image changed while {job.kind} was running; "
                        "the stale failure was discarded"
                    )
                item.status = JobStatus.FAILED.value
                item.error = safe_error(error).replace(str(store.root), "<project>")
                if (
                    image is not None
                    and not isinstance(error, (StaleJobResult, PageLineageConflict))
                    and not (job.kind == "inpaint" and job.lineage_context is not None)
                ):
                    processing_errors = list(image.processing_errors or [])
                    processing_errors.append(
                        {
                            "stage": job.kind,
                            "error": item.error,
                            "createdAt": datetime.now(UTC).isoformat(),
                        }
                    )
                    image.processing_errors = processing_errors[-50:]
                    stage = {
                        "preprocess": "preprocess",
                        "detect": "detection",
                        "ocr": "ocr",
                        "translate": "translation",
                        "render": "typeset",
                        "inpaint": "inpaint",
                        "typeset": "typeset",
                        "export": "export",
                    }.get(job.kind)
                    if stage:
                        status = dict(image.status)
                        status[stage] = "failed"
                        image.status = status
                    image.revision += 1
            record_job_item_finished(
                store,
                session,
                job=job,
                item=item,
                output=output,
                error=error,
            )
            self._recompute_in_session(job)

    @staticmethod
    def _recompute_in_session(job: Job) -> None:
        items = job.items
        job.total = len(items)
        job.completed = sum(item.status == JobStatus.COMPLETED.value for item in items)
        finished = sum(
            item.status
            in {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
            for item in items
        )
        job.progress = finished / len(items) if items else 1.0
        if items and finished < len(items):
            return
        if job.status == JobStatus.CANCELLED.value:
            job.error = None
            return
        failures = [item for item in items if item.status == JobStatus.FAILED.value]
        cancellations = [item for item in items if item.status == JobStatus.CANCELLED.value]
        if failures:
            job.status = JobStatus.FAILED.value
            job.error = f"{len(failures)} of {len(items)} job items failed; inspect item errors"
        elif cancellations:
            job.status = JobStatus.CANCELLED.value
            job.error = None
        elif job.kind == "export" and job.options.get("bundleFinalized") is not True:
            job.status = JobStatus.RUNNING.value
            job.error = None
        else:
            job.status = JobStatus.COMPLETED.value
            job.error = None

    def _recompute(self, store: ProjectStore, job_id: str) -> None:
        with store.session() as session:
            job = session.scalar(
                select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
            )
            if job is not None:
                self._recompute_in_session(job)

    def _fail_job(self, store: ProjectStore, job_id: str, message: str) -> None:
        with store.session() as session:
            job = session.get(Job, job_id)
            if job is not None and job.status != JobStatus.CANCELLED.value:
                job.status = JobStatus.FAILED.value
                job.error = message

    def create_job(
        self,
        store: ProjectStore,
        *,
        kind: str,
        image_ids: list[str],
        region_ids: list[str],
        options: dict[str, Any],
        lineage: dict[str, Any] | None = None,
    ) -> Job:
        if kind not in {
            "preprocess",
            "detect",
            "ocr",
            "mask",
            "translate",
            "render",
            "export",
            "inpaint",
            "typeset",
        }:
            raise ProjectError(f"Unsupported job kind: {kind}")
        if len(set(image_ids)) != len(image_ids) or len(set(region_ids)) != len(region_ids):
            raise ProjectError("Job targets must not contain duplicate ids")
        if (
            lineage is not None
            and kind in {"ocr", "mask", "inpaint", "translate", "typeset"}
            and (region_ids or not image_ids)
        ):
            raise PageLineageConflict(
                "Strict OCR/mask/inpaint/translate/typeset jobs require whole-page image targets",
                resource="job-lineage",
                reason={
                    "ocr": "g6-whole-page-required",
                    "mask": "g7-whole-page-required",
                    "inpaint": "g8-whole-page-required",
                    "translate": "g9-whole-page-required",
                    "typeset": "g10-whole-page-required",
                }[kind],
            )
        safe_options = normalize_remote_endpoints(without_secrets(options))
        safe_options.pop("regionIds", None)
        safe_options.pop("region_ids", None)
        if lineage is not None and kind == "inpaint":
            safe_options["ownerMaskStrategy"] = "connected-contract-union-v1"
        if region_ids and kind == "typeset":
            safe_options["regionIds"] = list(region_ids)
        if kind == "export":
            export_format = safe_options.get("format", "both")
            if not isinstance(export_format, str) or export_format not in {
                "images",
                "json",
                "both",
            }:
                raise ProjectError("Export format must be images, json, or both")
            safe_options["format"] = export_format
            image_variant = safe_options.get("imageVariant", "typeset")
            if not isinstance(image_variant, str) or image_variant not in {
                "typeset",
                "inpainted",
                "both",
            }:
                raise ProjectError("Export imageVariant must be typeset, inpainted, or both")
            safe_options["imageVariant"] = image_variant
            conflict = safe_options.get("conflict", "rename")
            if not isinstance(conflict, str) or conflict not in {
                "rename",
                "overwrite",
                "skip",
            }:
                raise ProjectError("Export conflict must be rename, overwrite, or skip")
            safe_options["conflict"] = conflict
            preserve_tree = safe_options.get("preserveTree", True)
            if type(preserve_tree) is not bool:
                raise ProjectError("Export preserveTree must be a boolean")
            safe_options["preserveTree"] = preserve_tree
            output_path = safe_options.get("outputPath")
            if output_path is not None:
                if not isinstance(output_path, str) or not output_path.strip():
                    raise ProjectError("Export outputPath must be a non-empty path string")
                output_entry = Path(output_path).expanduser()
                if output_entry.is_symlink():
                    raise ProjectError("Export outputPath must not be a symlink")
                if not output_entry.is_absolute():
                    output_entry = store.root / output_entry
                safe_options["outputPath"] = str(output_entry.resolve())
            safe_options["bundleFinalized"] = False
        concurrency = self._job_concurrency(kind, safe_options)
        if region_ids and kind in {"ocr", "translate"}:
            concurrency = 1
        safe_options["concurrency"] = concurrency
        with store.session() as session:
            project = store.project(session)
            require_ai_inpaint = self._requires_ai_inpaint(dict(project.settings))
            available_images = {
                image.id: image
                for image in session.scalars(
                    select(ImageAsset).where(ImageAsset.project_id == project.id)
                ).all()
            }
            if image_ids:
                missing_images = set(image_ids) - set(available_images)
                if missing_images:
                    raise ProjectError("One or more job image ids do not belong to this project")
            else:
                image_ids = sorted(
                    available_images, key=lambda key: available_images[key].relative_path
                )
            available_regions = {
                region.id: region
                for region in session.scalars(
                    select(TextRegion).join(ImageAsset).where(ImageAsset.project_id == project.id)
                ).all()
            }
            if region_ids and set(region_ids) - set(available_regions):
                raise ProjectError("One or more job region ids do not belong to this project")
            targets: list[tuple[str | None, str | None]]
            if region_ids and kind in {"ocr", "translate"}:
                targets = [
                    (available_regions[region_id].image_id, region_id) for region_id in region_ids
                ]
            else:
                targets = [(image_id, None) for image_id in image_ids]
            if not targets:
                raise ProjectError("A job must contain at least one image or region")
            normalized_lineage, active_generations = normalize_job_lineage(
                store,
                session,
                project_id=project.id,
                target_image_ids={
                    target_image_id for target_image_id, _region_id in targets if target_image_id
                },
                lineage=lineage,
            )
            if normalized_lineage is not None:
                if kind == "render":
                    raise PageLineageConflict(
                        "Strict G10 render remains blocked; use the typeset gate",
                        resource="job-kind:render",
                        reason="g10-render-blocked",
                    )
                require_supported_lineage_job_kind(kind)
            if kind == "translate" and normalized_lineage is not None:
                from manga_localizer.services.translations import (
                    resolve_translation_job_options,
                )

                safe_options = resolve_translation_job_options(
                    dict(project.settings), safe_options, self.providers.translation
                )
            if kind == "typeset" and normalized_lineage is not None:
                from manga_localizer.services.typesets import resolve_typeset_job_options

                # ``fontToken`` is a public installed-font capability id, not a
                # credential. The generic secret scrubber intentionally removes
                # token-shaped keys, so validate the strict G10 whitelist against
                # the original request before persisting its frozen style draft.
                safe_options = resolve_typeset_job_options(dict(options))
            if kind == "export" and safe_options["format"] in {"images", "both"}:
                for target_image_id in sorted({target[0] for target in targets if target[0]}):
                    require_strict_generated_export_inpaint(
                        store,
                        target_image_id,
                        export_format=str(safe_options["format"]),
                    )
            if kind in {"translate", "typeset", "render"}:
                for target_image_id in sorted({target[0] for target in targets if target[0]}):
                    target_image = available_images[target_image_id]
                    if kind == "translate" and normalized_lineage is None:
                        require_current_accepted_stage_review(
                            store,
                            target_image,
                            "inpaint",
                            require_ai=require_ai_inpaint,
                        )
                    elif kind == "typeset" and normalized_lineage is not None:
                        continue
                    elif kind != "translate":
                        self._accepted_inpaint_render_source(
                            store,
                            target_image,
                            require_ai=require_ai_inpaint,
                        )
                        page_regions = [
                            region
                            for region in available_regions.values()
                            if region.image_id == target_image_id
                        ]
                        self._require_translation_acceptance(
                            target_image_id,
                            page_regions,
                        )
            job = Job(
                project_id=project.id,
                kind=kind,
                status=JobStatus.QUEUED.value,
                options=safe_options,
                lineage_context=normalized_lineage,
                total=len(targets),
            )
            session.add(job)
            session.flush()
            items: list[JobItem] = []
            for position, (image_id, region_id) in enumerate(targets):
                item = JobItem(
                    job_id=job.id,
                    image_id=image_id,
                    region_id=region_id,
                    position=position,
                )
                session.add(item)
                items.append(item)
            session.flush()
            record_job_enqueued_events(
                store,
                session,
                job=job,
                items=items,
                generations=active_generations,
                project_settings=dict(project.settings),
            )
            job_id = job.id
        store.write_snapshot()
        return self.get_job(store, job_id)

    @staticmethod
    def get_job(store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.scalar(
                select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
            )
            if job is None:
                raise ProjectError("Job was not found")
            return job

    @staticmethod
    def list_jobs(store: ProjectStore) -> list[Job]:
        with store.session() as session:
            return list(
                session.scalars(
                    select(Job).options(selectinload(Job.items)).order_by(Job.created_at.desc())
                ).all()
            )

    @staticmethod
    def _require_supported_job_action(job: Job) -> None:
        if job.lineage_context is not None:
            raise PageLineageConflict(
                "Lineage-bound job actions require new actor evidence",
                resource=f"job:{job.id}",
                reason="lineage-action-not-supported",
            )

    def pause(self, store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ProjectError("Job was not found")
            self._require_supported_job_action(job)
            if job.status not in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                raise JobConflict(f"Cannot pause a {job.status} job")
            job.status = JobStatus.PAUSED.value
        store.write_snapshot()
        return self.get_job(store, job_id)

    def resume(self, store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.scalar(
                select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
            )
            if job is None:
                raise ProjectError("Job was not found")
            self._require_supported_job_action(job)
            if job.status != JobStatus.PAUSED.value:
                raise JobConflict(f"Cannot resume a {job.status} job")
            require_job_lineage_for_execution(store, session, job)
            job.status = JobStatus.QUEUED.value
        store.write_snapshot()
        return self.get_job(store, job_id)

    def cancel(self, store: ProjectStore, job_id: str) -> Job:
        # Serialize cancellation with immutable G7 publication. Once a mask row/event
        # exists, completion is the only safe terminal transition for that item.
        with store.lock:
            with store.session() as session:
                job = session.scalar(
                    select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
                )
                if job is None:
                    raise ProjectError("Job was not found")
                self._require_supported_job_action(job)
                if job.status in {
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                }:
                    raise JobConflict(f"Cannot cancel a {job.status} job")
                if job.kind in {"mask", "inpaint", "typeset"} and session.scalar(
                    select(PageLineageEvent.id)
                    .where(
                        PageLineageEvent.job_id == job.id,
                        PageLineageEvent.operation.in_(
                            (
                                "mask-artifact-produced",
                                "clean-plate-candidate-produced",
                                "typeset-candidate-produced",
                            )
                        ),
                    )
                    .limit(1)
                ):
                    raise JobConflict(
                        f"Cannot cancel a {job.kind} job after immutable artifact publication"
                    )
                job.status = JobStatus.CANCELLED.value
                for item in job.items:
                    if item.status == JobStatus.QUEUED.value:
                        item.status = JobStatus.CANCELLED.value
                        item.finished_at = datetime.now(UTC)
                self._recompute_in_session(job)
        store.write_snapshot()
        return self.get_job(store, job_id)

    def retry(self, store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.scalar(
                select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
            )
            if job is None:
                raise ProjectError("Job was not found")
            self._require_supported_job_action(job)
            if job.status not in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                raise JobConflict(f"Cannot retry a {job.status} job")
            require_job_lineage_for_execution(store, session, job)
            stale_completed: list[JobItem] = []
            if job.kind == "export":
                stale_completed = [
                    item
                    for item in job.items
                    if item.status == JobStatus.COMPLETED.value
                    and not self._completed_export_item_is_current(session, item)
                ]
                if stale_completed and job.options.get("conflict", "rename") != "overwrite":
                    raise JobConflict(
                        "A completed export page changed after it was written; "
                        "start a new export job or use overwrite so stale files can be replaced"
                    )
            reset = 0
            for item in job.items:
                if item in stale_completed or item.status in {
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                }:
                    item.status = JobStatus.QUEUED.value
                    item.progress = 0.0
                    item.error = None
                    item.output = {}
                    item.started_at = None
                    item.finished_at = None
                    reset += 1
            if not reset:
                if job.kind != "export" or job.options.get("bundleFinalized") is True:
                    raise JobConflict("Job has no failed or cancelled items to retry")
            job.status = JobStatus.QUEUED.value
            job.error = None
            job.completed = sum(item.status == JobStatus.COMPLETED.value for item in job.items)
            job.progress = job.completed / job.total if job.total else 0.0
        store.write_snapshot()
        return self.get_job(store, job_id)

    def _process_item(self, store: ProjectStore, job_id: str, item_id: str) -> dict[str, Any]:
        with store.session() as session:
            job = session.get(Job, job_id)
            item = session.get(JobItem, item_id)
            if job is None or item is None:
                raise ProjectError("Job item disappeared")
            active_generations = require_job_lineage_for_execution(store, session, job)
            mutation_binding = job_mutation_binding(job, item, active_generations)
            kind = job.kind
            options = dict(job.options)
            image_id = item.image_id
            region_id = item.region_id
        if image_id is None:
            raise ProjectError("Job item has no image")
        if kind == "preprocess":
            return self._process_preprocess(
                store,
                image_id,
                options,
                lineage_binding=mutation_binding,
            )
        if kind == "detect":
            return self._process_detect(
                store,
                image_id,
                options,
                lineage_binding=mutation_binding,
            )
        if kind == "ocr":
            return self._process_ocr(
                store,
                image_id,
                region_id,
                options,
                lineage_binding=mutation_binding,
            )
        if kind == "mask":
            if mutation_binding is None:
                raise PageLineageConflict(
                    "Strict G7 mask requires lineage",
                    resource=f"image:{image_id}",
                    reason="active-generation-missing",
                )
            with store.session() as session:
                current_job = session.get(Job, job_id)
                current_item = session.get(JobItem, item_id)
                if current_job is None or current_item is None:
                    raise ProjectError("Mask job item disappeared")
                return publish_mask_artifact(
                    store, job=current_job, item=current_item, binding=mutation_binding
                )
        if kind == "inpaint" and mutation_binding is not None:
            with store.session() as session:
                current_job = session.get(Job, job_id)
                current_item = session.get(JobItem, item_id)
                if current_job is None or current_item is None:
                    raise ProjectError("Clean-plate job item disappeared")
                return publish_clean_plate_candidate(
                    store,
                    job=current_job,
                    item=current_item,
                    binding=mutation_binding,
                    inpainter=self.providers.inpainter,
                )
        if kind == "translate":
            if mutation_binding is not None:
                from manga_localizer.services.translations import publish_translation_candidates

                with store.session() as session:
                    current_job = session.get(Job, job_id)
                    current_item = session.get(JobItem, item_id)
                    if current_job is None or current_item is None:
                        raise ProjectError("Translation job item disappeared")
                return publish_translation_candidates(
                    store,
                    job=current_job,
                    item=current_item,
                    binding=mutation_binding,
                    translator_factory=self.providers.translation,
                )
            return self._process_translation(store, image_id, region_id, options)
        if kind == "typeset" and mutation_binding is not None:
            from manga_localizer.services.typesets import publish_typeset_candidate

            with store.session() as session:
                current_job = session.get(Job, job_id)
                current_item = session.get(JobItem, item_id)
                if current_job is None or current_item is None:
                    raise ProjectError("Typeset job item disappeared")
            return publish_typeset_candidate(
                store,
                job=current_job,
                item=current_item,
                binding=mutation_binding,
            )
        if kind in {"render", "inpaint", "typeset"}:
            return self._process_render(store, image_id, options, kind)
        if kind == "export":
            export_format = str(options.get("format", "both"))
            image_variant = str(options.get("imageVariant", "typeset"))
            validate_image_export_readiness(
                store,
                image_id,
                export_format=export_format,
                image_variant=image_variant,
            )
            root = choose_export_root(
                store,
                options.get("outputPath"),
                job_id,
                include_assets=export_format == "both",
            )
            return export_image(
                store,
                image_id,
                export_root=root,
                export_format=export_format,
                conflict=str(options.get("conflict", "rename")),
                preserve_tree=bool(options.get("preserveTree", True)),
                image_variant=image_variant,
            )
        raise ProjectError(f"Unsupported job kind: {kind}")

    def _ocr_language(self, direction: str, requested: str | None) -> str | None:
        if requested:
            return requested
        if direction == "auto":
            return None
        candidate = "jpn_vert" if direction == "vertical" else "jpn"
        configured = self.settings.ocr_language_list
        return candidate if candidate in configured else configured[0]

    @staticmethod
    def _preprocess_options(
        options: dict[str, Any],
        project_settings: dict[str, Any],
    ) -> dict[str, Any]:
        aliases = {
            "profile": "profile",
            "enableUpscale": "enable_upscale",
            "enable_upscale": "enable_upscale",
            "upscaleFactor": "upscale_factor",
            "upscale_factor": "upscale_factor",
            "enableDenoise": "enable_denoise",
            "enable_denoise": "enable_denoise",
            "enableSharpen": "enable_sharpen",
            "enable_sharpen": "enable_sharpen",
            "enableContrastEnhance": "enable_contrast_enhance",
            "enable_contrast_enhance": "enable_contrast_enhance",
            "enableEdgeOptimize": "enable_edge_optimize",
            "enable_edge_optimize": "enable_edge_optimize",
            "enableBinarize": "enable_binarize",
            "enable_binarize": "enable_binarize",
            "threshold": "threshold",
        }
        merged: dict[str, Any] = {}
        for candidate in (
            project_settings.get("preprocessing"),
            options.get("preprocessing"),
            options,
        ):
            if not isinstance(candidate, dict):
                continue
            # A profile at a higher-precedence layer selects a fresh preset.
            # Do not leak explicit switches from the project default into a
            # per-job/evaluator profile; switches supplied alongside this
            # profile are applied immediately below as deliberate overrides.
            if "profile" in candidate:
                merged.clear()
            for key, target in aliases.items():
                if key in candidate:
                    merged[target] = candidate[key]
        return merged

    @staticmethod
    def _preprocessed_path(store: ProjectStore, relative_path: str) -> Path:
        relative = safe_relative_path(relative_path).with_suffix(".png")
        return resolve_write_target(
            store.root,
            Path("generated") / "preprocessed" / relative,
            protected_roots=(store.source_root,),
        )

    @classmethod
    def _processing_source(
        cls,
        store: ProjectStore,
        image: ImageAsset,
    ) -> tuple[Path, float, float, str]:
        original = image_path(store, image)
        processed = cls._preprocessed_path(store, image.relative_path)
        if image.status.get("preprocess") != "done" or not processed.is_file():
            return original, 1.0, 1.0, "original"
        try:
            with Image.open(processed) as opened:
                processed_width, processed_height = opened.size
        except (OSError, ValueError):
            return original, 1.0, 1.0, "original"
        if processed_width <= 0 or processed_height <= 0:
            return original, 1.0, 1.0, "original"
        return (
            processed,
            processed_width / image.width,
            processed_height / image.height,
            "preprocessed",
        )

    @classmethod
    def _inpaint_processing_source(
        cls,
        store: ProjectStore,
        image: ImageAsset,
    ) -> tuple[Path, float, float, str]:
        """Use preprocessing for redraw only after its exact pixels were accepted."""
        original = image_path(store, image)
        if image.status.get("preprocess") != "done":
            return original, 1.0, 1.0, "original"
        try:
            require_current_accepted_stage_review(store, image, "preprocess")
        except StagePrerequisiteConflict:
            return original, 1.0, 1.0, "original"
        processed = cls._preprocessed_path(store, image.relative_path)
        try:
            with Image.open(processed) as opened:
                processed_width, processed_height = opened.size
        except (OSError, ValueError) as error:
            raise StagePrerequisiteConflict(
                "The accepted preprocess output is unavailable and must be rebuilt",
                resource=f"image:{image.id}",
                stage="preprocess",
                reason="artifact-unavailable",
            ) from error
        scale_x = processed_width / image.width
        scale_y = processed_height / image.height
        cls._render_scale(scale_x, scale_y)
        return processed, scale_x, scale_y, "preprocessed"

    @classmethod
    def _accepted_inpaint_render_source(
        cls,
        store: ProjectStore,
        image: ImageAsset,
        *,
        require_ai: bool,
    ) -> tuple[Path, float, float, str]:
        """Return the immutable source plus the accepted clean plate's render grid."""
        require_current_accepted_stage_review(
            store,
            image,
            "inpaint",
            require_ai=require_ai,
        )
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        artifact = resolve_write_target(
            store.root,
            Path("generated") / "inpainted" / relative,
            protected_roots=(store.source_root,),
        )
        try:
            with Image.open(artifact) as opened:
                rendered_width, rendered_height = opened.size
        except (OSError, ValueError) as error:
            raise StagePrerequisiteConflict(
                "The accepted inpaint output is unavailable and must be rebuilt",
                resource=f"image:{image.id}",
                stage="inpaint",
                reason="artifact-unavailable",
            ) from error
        scale_x = rendered_width / image.width
        scale_y = rendered_height / image.height
        cls._render_scale(scale_x, scale_y)
        status = image.status or {}
        recorded_variant = status.get("renderInputVariant")
        recorded_scale = status.get("renderScale")
        recorded_size = status.get("renderedSize")
        lineage_matches = (
            recorded_variant in {"original", "preprocessed"}
            and isinstance(recorded_scale, list)
            and len(recorded_scale) == 2
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in recorded_scale
            )
            and math.isclose(float(recorded_scale[0]), scale_x, rel_tol=1e-6, abs_tol=1e-6)
            and math.isclose(float(recorded_scale[1]), scale_y, rel_tol=1e-6, abs_tol=1e-6)
            and recorded_size == [rendered_width, rendered_height]
            and (recorded_variant == "preprocessed" or cls._render_scale(scale_x, scale_y) == 1)
        )
        if not lineage_matches:
            raise StagePrerequisiteConflict(
                "The accepted inpaint render lineage changed and must be rebuilt",
                resource=f"image:{image.id}",
                stage="inpaint",
                reason="checksum-mismatch",
                mismatches=["renderLineage"],
            )
        if recorded_variant == "preprocessed":
            require_current_accepted_stage_review(store, image, "preprocess")
        return image_path(store, image), scale_x, scale_y, str(recorded_variant)

    @staticmethod
    def _render_scale(scale_x: float, scale_y: float) -> int:
        """Return the supported canonical-to-render scale or fail closed."""
        if (
            not math.isfinite(scale_x)
            or not math.isfinite(scale_y)
            or not math.isclose(scale_x, scale_y, rel_tol=1e-6, abs_tol=1e-6)
        ):
            raise ProjectError("Preprocessed render input must use one uniform scale")
        rounded = round(scale_x)
        if not math.isclose(scale_x, rounded, rel_tol=1e-6, abs_tol=1e-6) or not 1 <= rounded <= 4:
            raise ProjectError("Preprocessed render input must use a 1x to 4x integer scale")
        return rounded

    @staticmethod
    def _scale_render_region(
        region: dict[str, Any],
        scale_x: float,
        scale_y: float,
    ) -> dict[str, Any]:
        """Map one canonical persisted region into a transient render-pixel snapshot."""
        scale = PersistentJobQueue._render_scale(scale_x, scale_y)
        scaled = dict(region)
        for key, axis_scale in (
            ("x", scale_x),
            ("y", scale_y),
            ("width", scale_x),
            ("height", scale_y),
        ):
            scaled[key] = float(region[key]) * axis_scale

        raw_style = region.get("style")
        if isinstance(raw_style, dict):
            style = dict(raw_style)
            for key in (
                "fontSize",
                "font_size",
                "minFontSize",
                "min_font_size",
                "maxFontSize",
                "max_font_size",
                "strokeWidth",
                "stroke_width",
                "letterSpacing",
                "letter_spacing",
                "padding",
            ):
                value = style.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    multiplied = float(value) * scale
                    style[key] = round(multiplied) if isinstance(value, int) else multiplied
            scaled["style"] = style

        raw_repair = region.get("repair")
        if isinstance(raw_repair, dict):
            repair = dict(raw_repair)
            for key in (
                "padding",
                "maskPadding",
                "dilation",
                "feather",
                "radius",
                "contextPadding",
            ):
                value = repair.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    multiplied = float(value) * scale
                    repair[key] = round(multiplied) if isinstance(value, int) else multiplied
            polygon = repair.get("maskPolygon")
            if isinstance(polygon, list):
                repair["maskPolygon"] = [
                    [float(point[0]) * scale_x, float(point[1]) * scale_y]
                    for point in polygon
                    if isinstance(point, (list, tuple)) and len(point) == 2
                ]
            edits = repair.get("maskEdits")
            if isinstance(edits, dict) and isinstance(edits.get("strokes"), list):
                scaled_strokes: list[dict[str, Any]] = []
                for raw_stroke in edits["strokes"]:
                    if not isinstance(raw_stroke, dict):
                        continue
                    stroke = dict(raw_stroke)
                    radius = stroke.get("radius")
                    if isinstance(radius, (int, float)) and not isinstance(radius, bool):
                        stroke["radius"] = float(radius) * scale
                    points = stroke.get("points")
                    if isinstance(points, list):
                        stroke["points"] = [
                            [float(point[0]) * scale_x, float(point[1]) * scale_y]
                            for point in points
                            if isinstance(point, (list, tuple)) and len(point) == 2
                        ]
                    scaled_strokes.append(stroke)
                repair["maskEdits"] = {**edits, "strokes": scaled_strokes}
            scaled["repair"] = repair
        return scaled

    @staticmethod
    def _clamp_box(
        box: dict[str, Any],
        width: int,
        height: int,
    ) -> dict[str, float]:
        left = max(0.0, min(float(width), float(box["x"])))
        top = max(0.0, min(float(height), float(box["y"])))
        right = max(left, min(float(width), float(box["x"]) + float(box["width"])))
        bottom = max(top, min(float(height), float(box["y"]) + float(box["height"])))
        if right - left < 1 or bottom - top < 1:
            raise ProjectError("Text region is outside the image after coordinate normalization")
        return {"x": left, "y": top, "width": right - left, "height": bottom - top}

    _STALE_OVERSIZED_COVERAGE = 0.15
    _STALE_OVERSIZED_MAX_CONFIDENCE = 0.5
    _STALE_PANEL_COVERAGE = 0.30

    @classmethod
    def _region_page_coverage(
        cls,
        region: TextRegion,
        image_width: int,
        image_height: int,
    ) -> float:
        page_area = float(image_width) * float(image_height)
        if page_area <= 0:
            return 0.0
        return (float(region.width) * float(region.height)) / page_area

    @staticmethod
    def _region_effective_confidence(region: TextRegion) -> float | None:
        if region.confidence is not None:
            return float(region.confidence)
        recognition = recognition_payload(region)
        ocr = recognition.get("ocr")
        if isinstance(ocr, dict) and ocr.get("confidence") is not None:
            return float(ocr["confidence"])
        detection = recognition.get("detection")
        if isinstance(detection, dict) and detection.get("confidence") is not None:
            return float(detection["confidence"])
        return None

    @classmethod
    def _is_stale_auto_detection(
        cls,
        region: TextRegion,
        image_width: int,
        image_height: int,
    ) -> bool:
        repair = region.repair if isinstance(region.repair, dict) else {}
        if repair.get("detectorGenerated") is not True:
            return False
        if region.confirmed or region.ignored:
            return False
        if region_trust(region).get("disposition") in {"trusted", "ignored"}:
            return False
        if (region.region_type or "unknown") != "unknown":
            return False
        if (region.translation_text or "").strip():
            return False
        if not (region.source_text or "").strip():
            return True
        if not detection_region_is_usable(
            int(region.width),
            int(region.height),
            min_side=detection_min_side_for_image(image_width, image_height),
        ):
            return True
        coverage = cls._region_page_coverage(region, image_width, image_height)
        confidence = cls._region_effective_confidence(region)
        if coverage >= cls._STALE_PANEL_COVERAGE and confidence is None:
            return True
        return (
            coverage >= cls._STALE_OVERSIZED_COVERAGE
            and confidence is not None
            and confidence < cls._STALE_OVERSIZED_MAX_CONFIDENCE
        )

    @staticmethod
    def _detection_overlaps_kept(detection: OCRRegion, kept: OCRRegion) -> bool:
        intersection_width = max(
            0,
            min(detection.x + detection.width, kept.x + kept.width) - max(detection.x, kept.x),
        )
        intersection_height = max(
            0,
            min(detection.y + detection.height, kept.y + kept.height) - max(detection.y, kept.y),
        )
        intersection = intersection_width * intersection_height
        if not intersection:
            return False
        smaller = min(detection.width * detection.height, kept.width * kept.height)
        union = detection.width * detection.height + kept.width * kept.height - intersection
        iou = intersection / union if union else 0.0
        containment = intersection / smaller if smaller else 0.0
        return iou >= 0.22 or containment >= 0.5

    def _process_preprocess(
        self,
        store: ProjectStore,
        image_id: str,
        options: dict[str, Any],
        *,
        lineage_binding: JobMutationBinding | None = None,
    ) -> dict[str, Any]:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Preprocessing image was not found")
            project = store.project(session)
            project_settings = dict(project.settings)
            requested_provider_name = str(
                options.get("provider")
                or options.get("preprocessorProvider")
                or project_settings.get("preprocessorProvider")
                or "opencv-pillow"
            )
            source = image_path(store, image)
            expected_image_revision = image.revision
            target = self._preprocessed_path(store, image.relative_path)
        try:
            provider = self.providers.preprocessor(requested_provider_name)
        except ValueError as error:
            raise ProjectError(str(error)) from None
        provider_name = str(getattr(provider, "name", requested_provider_name))
        preprocess_options = self._preprocess_options(options, project_settings)
        result = provider.preprocess(source, **preprocess_options)
        artifact = self._png_bytes(result.image)
        try:
            artifact_changed = not target.is_file() or target.read_bytes() != artifact
        except OSError:
            artifact_changed = True
        with store.session() as session:
            image = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                None,
                "image preprocessing",
            )
            project = store.project(session)
            provenance_changed = (
                artifact_changed or image.status.get("preprocessingProvider") != provider_name
            )
            if provenance_changed:
                for region in session.scalars(
                    select(TextRegion).where(TextRegion.image_id == image_id)
                ).all():
                    evidence = recognition_payload(region)
                    if (
                        region.ignored
                        or not recognition_uses_input_variant(evidence, "preprocessed")
                        or not (is_region_trusted(region) or region.confirmed)
                    ):
                        continue
                    before = region_payload(region)
                    region.recognition = invalidate_trust(evidence)
                    region.confirmed = False
                    region.revision += 1
                    session.flush()
                    add_revision(
                        session,
                        project,
                        entity_type="region",
                        entity_id=region.id,
                        operation="preprocess-revoke",
                        before=before,
                        after=region_payload(region),
                    )
            atomic_write_bytes(target, artifact)
            invalidate_image_pipeline(
                store,
                image,
                {"detection", "ocr", "translation", "inpaint", "typeset", "export"},
            )
            status = dict(image.status)
            status["preprocess"] = "done"
            status["preprocessingProvider"] = provider_name
            image.status = status
            clear_stage_reviews(image, {"preprocess"})
            image.processing_errors = [
                error
                for error in (image.processing_errors or [])
                if error.get("stage") != "preprocess"
            ]
            image.revision += 1
            record_job_artifact_produced(
                store,
                session,
                binding=lineage_binding,
                stage="preprocess",
                input_checksum=image.checksum,
                output_checksum=hashlib.sha256(artifact).hexdigest(),
                provider=provider_name,
                image_revision=image.revision,
            )
        return {
            "provider": provider_name,
            "profile": preprocess_options.get("profile", "off"),
            "originalSize": list(result.original_size),
            "processedSize": list(result.processed_size),
            "scale": [result.scale_x, result.scale_y],
        }

    @staticmethod
    def _confidence_buckets(values: list[float | None]) -> dict[str, int]:
        buckets = {"missing": 0, "low": 0, "medium": 0, "high": 0}
        for value in values:
            if value is None:
                buckets["missing"] += 1
            elif value < 0.5:
                buckets["low"] += 1
            elif value < 0.8:
                buckets["medium"] += 1
            else:
                buckets["high"] += 1
        return buckets

    @staticmethod
    def _trust_counts(regions: list[TextRegion]) -> tuple[dict[str, int], dict[str, int]]:
        dispositions: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for region in regions:
            trust = region_trust(region)
            disposition = str(trust["disposition"])
            reason = str(trust["reason"])
            dispositions[disposition] = dispositions.get(disposition, 0) + 1
            reasons[reason] = reasons.get(reason, 0) + 1
        return dispositions, reasons

    @staticmethod
    def _region_versions(session, image_id: str) -> dict[str, int]:
        return dict(
            session.execute(
                select(TextRegion.id, TextRegion.revision).where(TextRegion.image_id == image_id)
            ).all()
        )

    @classmethod
    def _assert_image_unchanged(
        cls,
        session,
        image_id: str,
        expected_image_revision: int,
        expected_region_versions: dict[str, int] | None,
        operation: str,
    ) -> ImageAsset:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError(f"Image disappeared during {operation}")
        region_versions_changed = (
            expected_region_versions is not None
            and cls._region_versions(session, image_id) != expected_region_versions
        )
        if image.revision != expected_image_revision or region_versions_changed:
            raise StaleJobResult(f"Image changed during {operation}; retry the job")
        return image

    def _process_detect(
        self,
        store: ProjectStore,
        image_id: str,
        options: dict[str, Any],
        *,
        lineage_binding: JobMutationBinding | None = None,
    ) -> dict[str, Any]:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Detection image was not found")
            project = store.project(session)
            requested_provider_name = str(
                options.get("provider")
                or options.get("detectorProvider")
                or project.settings.get("detectorProvider")
                or "tesseract"
            )
            quality_checksum: str | None = None
            g3_sequence: int | None = None
            if lineage_binding is not None:
                generation = session.get(
                    PageGeneration,
                    lineage_binding["generationId"],
                )
                if generation is None or generation.image_id != image.id:
                    raise PageLineageConflict(
                        "Detector job has no matching active page generation",
                        resource=f"job-item:{lineage_binding['jobItemId']}",
                        reason="generation-mismatch",
                    )
                quality, g3_event = require_current_text_present_quality_plate(
                    store,
                    session,
                    image,
                    generation,
                )
                source = quality["path"]
                try:
                    with Image.open(source) as opened:
                        processed_width, processed_height = opened.size
                except (OSError, ValueError) as error:
                    raise PageLineageConflict(
                        "Accepted detector quality plate could not be opened",
                        resource=f"image:{image.id}",
                        reason="quality-artifact-unreadable",
                    ) from error
                if processed_width <= 0 or processed_height <= 0:
                    raise PageLineageConflict(
                        "Accepted detector quality plate has invalid dimensions",
                        resource=f"image:{image.id}",
                        reason="quality-artifact-invalid",
                    )
                scale_x = processed_width / image.width
                scale_y = processed_height / image.height
                input_variant = quality["targetKind"]
                quality_checksum = quality["checksum"]
                g3_sequence = g3_event.sequence
            else:
                source, scale_x, scale_y, input_variant = self._processing_source(store, image)
            image_width = image.width
            image_height = image.height
            expected_image_revision = image.revision
            expected_region_versions = self._region_versions(session, image_id)
        try:
            detector = self.providers.detector(requested_provider_name)
        except ValueError as error:
            raise ProjectError(str(error)) from None
        provider_name = str(getattr(detector, "name", requested_provider_name))
        direction = str(options.get("direction", self.settings.ocr_default_direction))
        language = self._ocr_language(direction, options.get("language"))
        raw_detections = detector.detect_text_regions(
            source,
            direction=direction,
            language=language,
        )
        detections: list[OCRRegion] = []
        for detection in raw_detections:
            left = max(0, min(image_width, math.floor(detection.x / scale_x)))
            top = max(0, min(image_height, math.floor(detection.y / scale_y)))
            right = max(
                left,
                min(image_width, math.ceil((detection.x + detection.width) / scale_x)),
            )
            bottom = max(
                top,
                min(image_height, math.ceil((detection.y + detection.height) / scale_y)),
            )
            if not detection_region_is_usable(
                right - left,
                bottom - top,
                min_side=detection_min_side_for_image(image_width, image_height),
            ):
                continue
            polygon = (
                tuple(
                    (
                        max(0.0, min(float(image_width), point[0] / scale_x)),
                        max(0.0, min(float(image_height), point[1] / scale_y)),
                    )
                    for point in detection.polygon
                )
                if detection.polygon is not None
                else None
            )
            detections.append(
                OCRRegion(
                    x=left,
                    y=top,
                    width=right - left,
                    height=bottom - top,
                    text=detection.text,
                    confidence=detection.confidence,
                    direction=detection.direction,
                    polygon=polygon,
                )
            )
        if provider_name == "ppocr-v3+tesseract":
            detections = consolidate_text_regions(
                detections,
                (image_width, image_height),
                expand=False,
            )
        created_count = 0
        with store.session() as session:
            project = store.project(session)
            image = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                expected_region_versions,
                "text detection",
            )
            if lineage_binding is not None:
                generation = session.get(
                    PageGeneration,
                    lineage_binding["generationId"],
                )
                if generation is None or generation.image_id != image.id:
                    raise PageLineageConflict(
                        "Detector page generation changed while the provider was running",
                        resource=f"job-item:{lineage_binding['jobItemId']}",
                        reason="generation-mismatch",
                    )
                current_quality, current_g3 = require_current_text_present_quality_plate(
                    store,
                    session,
                    image,
                    generation,
                )
                if (
                    current_quality["checksum"] != quality_checksum
                    or current_g3.sequence != g3_sequence
                ):
                    raise PageLineageConflict(
                        "Detector input gate changed while the provider was running",
                        resource=f"job-item:{lineage_binding['jobItemId']}",
                        reason="detect-input-changed",
                    )
            existing = list(
                session.scalars(select(TextRegion).where(TextRegion.image_id == image_id)).all()
            )
            for region in list(existing):
                replace = self._is_stale_auto_detection(region, image.width, image.height)
                if lineage_binding is not None:
                    owned_by_item = region.detector_job_item_id == lineage_binding["jobItemId"]
                    if owned_by_item and (
                        region.content_disposition is not None
                        or region.revision != 1
                        or bool((region.source_text or "").strip())
                        or bool((region.translation_text or "").strip())
                        or region.confirmed
                        or region.ignored
                    ):
                        raise PageLineageConflict(
                            "Detector retry candidates changed after their prior publication",
                            resource=f"job-item:{lineage_binding['jobItemId']}",
                            reason="detect-retry-candidates-changed",
                        )
                    replace = owned_by_item or (
                        region.detector_job_item_id is not None
                        and region.content_disposition is None
                        and replace
                    )
                if not replace:
                    continue
                add_revision(
                    session,
                    project,
                    entity_type="region",
                    entity_id=region.id,
                    operation="detect-replace",
                    before=region_payload(region),
                    after=None,
                )
                session.delete(region)
                existing.remove(region)
            session.flush()
            kept = [
                OCRRegion(
                    x=int(region.x),
                    y=int(region.y),
                    width=int(region.width),
                    height=int(region.height),
                    text="",
                    confidence=region.confidence,
                    direction=region.direction,
                    polygon=None,
                )
                for region in existing
            ]
            detections = [
                detection
                for detection in detections
                if not any(
                    self._detection_overlaps_kept(detection, kept_region) for kept_region in kept
                )
            ]
            next_reading_order = max((region.reading_order for region in existing), default=-1) + 1
            for offset, detection in enumerate(detections):
                region = TextRegion(
                    image_id=image_id,
                    x=detection.x,
                    y=detection.y,
                    width=detection.width,
                    height=detection.height,
                    source_text="",
                    confidence=detection.confidence,
                    direction=detection.direction,
                    region_type="unknown",
                    reading_order=next_reading_order + offset,
                    detector_job_item_id=(
                        lineage_binding["jobItemId"] if lineage_binding is not None else None
                    ),
                    detector_candidate_index=(offset if lineage_binding is not None else None),
                    repair={
                        **DEFAULT_REPAIR_SETTINGS,
                        "detectorGenerated": True,
                        "detectedTextCandidate": detection.text,
                        **(
                            {"maskPolygon": detection.polygon}
                            if detection.polygon is not None
                            else {}
                        ),
                    },
                    recognition=with_detection_evidence(
                        None,
                        detection.confidence,
                        provider_name,
                        input_variant=input_variant,
                        language=language,
                    ),
                    revision=1,
                )
                session.add(region)
                session.flush()
                created_count += 1
                add_revision(
                    session,
                    project,
                    entity_type="region",
                    entity_id=region.id,
                    operation="detect-create",
                    before=None,
                    after=region_payload(region),
                )
            invalidate_image_pipeline(
                store,
                image,
                {"ocr", "translation", "inpaint", "typeset", "export"},
            )
            reset_image_review(image)
            status = dict(image.status)
            status["detection"] = "done"
            status["detectorProvider"] = provider_name
            image.status = status
            image.processing_errors = [
                error for error in (image.processing_errors or []) if error.get("stage") != "detect"
            ]
            image.revision += 1
            all_regions = list(
                session.scalars(select(TextRegion).where(TextRegion.image_id == image_id)).all()
            )
            disposition_counts, reason_counts = self._trust_counts(all_regions)
            if lineage_binding is not None:
                if quality_checksum is None:
                    raise PageLineageConflict(
                        "Detector quality checksum was not retained",
                        resource=f"job-item:{lineage_binding['jobItemId']}",
                        reason="detect-input-changed",
                    )
                record_detect_regions_produced(
                    store,
                    session,
                    binding=lineage_binding,
                    input_checksum=quality_checksum,
                    output_checksum=g4_region_state_checksum(session, image.id),
                    provider=provider_name,
                    image_revision=image.revision,
                    region_count=len(all_regions),
                )
        return {
            "provider": provider_name,
            "inputVariant": input_variant,
            "policyVersion": TRUST_POLICY_VERSION,
            "count": created_count,
            "confidenceBuckets": self._confidence_buckets(
                [detection.confidence for detection in detections]
            ),
            "dispositionCounts": disposition_counts,
            "reasonCounts": reason_counts,
        }

    @staticmethod
    def _canonical_crop_box(box: dict[str, float], width: int, height: int) -> dict[str, int]:
        rounded = {key: round(float(box[key])) for key in ("x", "y", "width", "height")}
        if (
            rounded["x"] < 0
            or rounded["y"] < 0
            or rounded["width"] < 1
            or rounded["height"] < 1
            or rounded["x"] + rounded["width"] > width
            or rounded["y"] + rounded["height"] > height
        ):
            raise ProjectError("OCR crop is outside its bound input image")
        return rounded

    @staticmethod
    def _crop_checksum(path: Path, box: dict[str, int]) -> str:
        try:
            with Image.open(path) as opened:
                source = opened.convert("RGBA")
                crop = source.crop(
                    (
                        box["x"],
                        box["y"],
                        box["x"] + box["width"],
                        box["y"] + box["height"],
                    )
                )
                header = f"RGBA:{crop.width}x{crop.height}:".encode()
                return hashlib.sha256(header + crop.tobytes()).hexdigest()
        except (OSError, ValueError) as error:
            raise ProjectError("OCR input crop could not be read") from error

    @staticmethod
    def _ocr_model_version(provider: object) -> str | None:
        # Strict provenance is provider-observed. A request body must never be
        # able to relabel the executable/model that actually produced OCR.
        for method_name in ("health_check", "get_capabilities"):
            capabilities = getattr(provider, method_name, None)
            if not callable(capabilities):
                continue
            try:
                payload = capabilities()
            except Exception:
                continue
            if isinstance(payload, dict):
                value = payload.get("version") or payload.get("modelVersion")
                if isinstance(value, str) and value and len(value) <= 128:
                    return value
        return None

    def _process_lineage_ocr(
        self,
        store: ProjectStore,
        image_id: str,
        region_id: str | None,
        options: dict[str, Any],
        *,
        lineage_binding: JobMutationBinding,
    ) -> dict[str, Any]:
        if region_id is not None:
            raise PageLineageConflict(
                "Strict G6 OCR does not allow targeted region jobs",
                resource=f"region:{region_id}",
                reason="g6-whole-page-required",
            )
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            generation = session.get(PageGeneration, lineage_binding["generationId"])
            if image is None or generation is None or generation.image_id != image.id:
                raise PageLineageConflict(
                    "Strict OCR page generation disappeared",
                    resource=f"image:{image_id}",
                    reason="generation-mismatch",
                )
            produced = session.scalar(
                select(PageLineageEvent)
                .where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.job_item_id == lineage_binding["jobItemId"],
                    PageLineageEvent.operation == "ocr-attempts-produced",
                )
                .order_by(PageLineageEvent.sequence.desc())
                .limit(1)
            )
            existing_attempts = list(
                session.scalars(
                    select(RegionOCRAttempt).where(
                        RegionOCRAttempt.job_item_id == lineage_binding["jobItemId"]
                    )
                ).all()
            )
            if produced is not None:
                if (
                    not existing_attempts
                    or produced.output_checksum
                    != g6_ocr_state_checksum(session, image.id, generation.id)
                    or (produced.evidence or {}).get("ocrAttemptCount") != len(existing_attempts)
                ):
                    raise PageLineageConflict(
                        "Recovered OCR publication does not match stored attempts",
                        resource=f"job-item:{lineage_binding['jobItemId']}",
                        reason="g6-attempt-publication-mismatch",
                    )
                return {
                    "provider": produced.provider,
                    "modelVersion": produced.model_version,
                    "parameterHash": produced.parameter_hash,
                    "inputVariant": "original+quality",
                    "count": int((produced.evidence or {}).get("eligibleRegionCount", 0)),
                    "attemptCount": len(existing_attempts),
                    "recoveredPublication": True,
                }
            if existing_attempts:
                raise PageLineageConflict(
                    "Stored OCR attempts are missing their atomic publication event",
                    resource=f"job-item:{lineage_binding['jobItemId']}",
                    reason="g6-attempt-publication-mismatch",
                )
            quality, _g3_event = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            require_current_background_classifications(store, session, image, generation)
            project = store.project(session)
            requested_provider_name = str(
                options.get("provider")
                or options.get("ocrProvider")
                or project.settings.get("ocrProvider")
                or "tesseract"
            )
            requested_language = options.get("language")
            if requested_language is not None and (
                not isinstance(requested_language, str)
                or requested_language not in {"ja", "ja-JP", "japanese", "jpn", "jpn_vert"}
            ):
                raise ProjectError(
                    "Strict G6 OCR only accepts a Japanese source-language declaration"
                )
            project_source_language = project.settings.get("sourceLanguage", "ja")
            if project_source_language not in {"ja", "ja-JP", "japanese", "jpn"}:
                raise PageLineageConflict(
                    "G6 OCR requires the project's Japanese source-language contract",
                    resource=f"image:{image.id}",
                    reason="g6-source-language-invalid",
                )
            original_source = image_path(store, image)
            quality_source = quality["path"]
            try:
                with Image.open(original_source) as opened:
                    original_width, original_height = opened.size
                with Image.open(quality_source) as opened:
                    quality_width, quality_height = opened.size
            except (OSError, ValueError) as error:
                raise ProjectError("Strict OCR input images could not be opened") from error
            if original_width != image.width or original_height != image.height:
                raise PageLineageConflict(
                    "Immutable source dimensions changed before OCR",
                    resource=f"image:{image.id}",
                    reason="source-identity-changed",
                )
            scale_x = quality_width / image.width
            scale_y = quality_height / image.height
            if (
                not math.isfinite(scale_x)
                or not math.isfinite(scale_y)
                or min(scale_x, scale_y) <= 0
            ):
                raise ProjectError("Accepted quality plate has invalid dimensions")
            targets = list(
                session.scalars(
                    select(TextRegion)
                    .where(
                        TextRegion.image_id == image.id,
                        TextRegion.region_type != "ruby",
                        TextRegion.content_disposition.in_(("translate", "redraw-art")),
                    )
                    .order_by(TextRegion.reading_order, TextRegion.id)
                ).all()
            )
            if not targets:
                raise PageLineageConflict(
                    "A page without translatable regions must use the G6 not-applicable gate",
                    resource=f"image:{image.id}",
                    reason="g6-no-translatable-regions",
                )
            expected_image_revision = image.revision
            expected_region_versions = self._region_versions(session, image.id)
            target_snapshots: list[dict[str, Any]] = []
            for target in targets:
                if target.direction not in {"horizontal", "vertical"}:
                    raise PageLineageConflict(
                        "G6 OCR requires the accepted G4 direction",
                        resource=f"region:{target.id}",
                        reason="g6-direction-invalid",
                    )
                original_float_box = self._clamp_box(
                    {
                        "x": target.x,
                        "y": target.y,
                        "width": target.width,
                        "height": target.height,
                    },
                    original_width,
                    original_height,
                )
                quality_float_box = self._clamp_box(
                    {
                        "x": original_float_box["x"] * scale_x,
                        "y": original_float_box["y"] * scale_y,
                        "width": original_float_box["width"] * scale_x,
                        "height": original_float_box["height"] * scale_y,
                    },
                    quality_width,
                    quality_height,
                )
                original_box = self._canonical_crop_box(
                    original_float_box, original_width, original_height
                )
                quality_box = self._canonical_crop_box(
                    quality_float_box, quality_width, quality_height
                )
                target_snapshots.append(
                    {
                        "id": target.id,
                        "direction": target.direction,
                        "language": "jpn_vert" if target.direction == "vertical" else "jpn",
                        "originalBox": original_box,
                        "qualityBox": quality_box,
                        "originalCropChecksum": self._crop_checksum(original_source, original_box),
                        "qualityCropChecksum": self._crop_checksum(quality_source, quality_box),
                    }
                )
        try:
            ocr_provider = self.providers.ocr_provider(requested_provider_name)
        except ValueError as error:
            raise ProjectError(str(error)) from None
        provider_name = str(getattr(ocr_provider, "name", requested_provider_name))
        model_version = self._ocr_model_version(ocr_provider)
        parameter_payload = {
            "version": 1,
            "provider": provider_name,
            "modelVersion": model_version,
            "generationParameterHash": generation.parameter_set_hash,
            "qualityChecksum": quality["checksum"],
            "regions": [
                {
                    "id": target["id"],
                    "direction": target["direction"],
                    "language": target["language"],
                }
                for target in target_snapshots
            ],
        }
        parameter_hash = hashlib.sha256(
            json.dumps(
                parameter_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        computed: list[dict[str, Any]] = []
        for target in target_snapshots:
            for variant, source, box, parent_checksum, crop_checksum in (
                (
                    "original",
                    original_source,
                    target["originalBox"],
                    generation.source_checksum,
                    target["originalCropChecksum"],
                ),
                (
                    "quality",
                    quality_source,
                    target["qualityBox"],
                    quality["checksum"],
                    target["qualityCropChecksum"],
                ),
            ):
                result = ocr_provider.recognize_region(
                    source,
                    box,
                    direction=str(target["direction"]),
                    language=target["language"],
                )
                if (
                    not isinstance(result.text, str)
                    or result.direction != target["direction"]
                    or isinstance(result.confidence, bool)
                    or (
                        result.confidence is not None
                        and (
                            not math.isfinite(float(result.confidence))
                            or float(result.confidence) < 0
                            or float(result.confidence) > 1
                        )
                    )
                ):
                    raise ProjectError("OCR provider returned invalid strict attempt evidence")
                computed.append(
                    {
                        "regionId": target["id"],
                        "inputVariant": variant,
                        "parentChecksum": parent_checksum,
                        "cropChecksum": crop_checksum,
                        "cropBox": box,
                        "language": target["language"],
                        "direction": target["direction"],
                        "text": result.text,
                        "textChecksum": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                        "confidence": (
                            float(result.confidence) if result.confidence is not None else None
                        ),
                    }
                )
        with store.session() as session:
            image = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                expected_region_versions,
                "OCR",
            )
            generation = session.get(PageGeneration, lineage_binding["generationId"])
            if generation is None:
                raise PageLineageConflict(
                    "Strict OCR generation disappeared before publication",
                    resource=f"image:{image.id}",
                    reason="generation-mismatch",
                )
            quality_now, _g3_event = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            require_current_background_classifications(store, session, image, generation)
            if quality_now["checksum"] != quality["checksum"]:
                raise StaleJobResult("Accepted quality plate changed during OCR; retry the job")
            for attempt in computed:
                source_path = (
                    original_source if attempt["inputVariant"] == "original" else quality_source
                )
                if self._crop_checksum(source_path, attempt["cropBox"]) != attempt["cropChecksum"]:
                    raise StaleJobResult("OCR crop changed before publication; retry the job")
            before_checksum = g6_ocr_state_checksum(session, image.id, generation.id)
            for attempt in computed:
                session.add(
                    RegionOCRAttempt(
                        region_id=attempt["regionId"],
                        image_id=image.id,
                        generation_id=generation.id,
                        job_id=lineage_binding["jobId"],
                        job_item_id=lineage_binding["jobItemId"],
                        input_variant=attempt["inputVariant"],
                        parent_checksum=attempt["parentChecksum"],
                        crop_checksum=attempt["cropChecksum"],
                        crop_box=attempt["cropBox"],
                        provider=provider_name,
                        model_version=model_version,
                        parameter_hash=parameter_hash,
                        language=attempt["language"],
                        direction=attempt["direction"],
                        text=attempt["text"],
                        text_checksum=attempt["textChecksum"],
                        confidence=attempt["confidence"],
                    )
                )
            invalidate_image_pipeline(store, image, {"translation", "inpaint", "typeset", "export"})
            reset_image_review(image)
            status = dict(image.status or {})
            status["ocr"] = "done"
            status["ocrProvider"] = provider_name
            image.status = status
            image.processing_errors = [
                error for error in (image.processing_errors or []) if error.get("stage") != "ocr"
            ]
            image.revision += 1
            session.flush()
            project = store.project(session)
            revision = add_revision(
                session,
                project,
                entity_type="ocr-attempt-set",
                entity_id=lineage_binding["jobItemId"],
                operation="publish",
                before={"ocrChecksum": before_checksum, "jobItemAttemptCount": 0},
                after={
                    "jobItemAttemptCount": len(computed),
                    "eligibleRegionCount": len(target_snapshots),
                },
            )
            session.flush()
            after_checksum = g6_ocr_state_checksum(session, image.id, generation.id)
            record_ocr_attempts_produced(
                store,
                session,
                binding=lineage_binding,
                input_checksum=before_checksum,
                output_checksum=after_checksum,
                provider=provider_name,
                model_version=model_version,
                parameter_hash=parameter_hash,
                image_revision=image.revision,
                region_count=len(target_snapshots),
                attempt_count=len(computed),
                revision_id=revision.id,
            )
        return {
            "provider": provider_name,
            "modelVersion": model_version,
            "parameterHash": parameter_hash,
            "inputVariant": "original+quality",
            "count": len(target_snapshots),
            "attemptCount": len(computed),
            "recoveredPublication": False,
        }

    def _process_ocr(
        self,
        store: ProjectStore,
        image_id: str,
        region_id: str | None,
        options: dict[str, Any],
        *,
        lineage_binding: JobMutationBinding | None = None,
    ) -> dict[str, Any]:
        if lineage_binding is not None:
            return self._process_lineage_ocr(
                store,
                image_id,
                region_id,
                options,
                lineage_binding=lineage_binding,
            )
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("OCR image was not found")
            project = store.project(session)
            requested_provider_name = str(
                options.get("provider")
                or options.get("ocrProvider")
                or project.settings.get("ocrProvider")
                or "tesseract"
            )
            has_targets = bool(
                session.scalar(
                    select(TextRegion.id)
                    .where(TextRegion.image_id == image_id, TextRegion.ignored.is_(False))
                    .limit(1)
                )
            )
            detection_is_current = image.status.get("detection") == "done"
            fallback_detector_name = str(
                options.get("detectorProvider")
                or image.status.get("detectorProvider")
                or project.settings.get("detectorProvider")
                or "tesseract"
            )
            if region_id:
                region = session.get(TextRegion, region_id)
                if region is None or region.image_id != image_id:
                    raise ProjectError("OCR region was not found")
                has_targets = True
        try:
            ocr_provider = self.providers.ocr_provider(requested_provider_name)
        except ValueError as error:
            raise ProjectError(str(error)) from None
        provider_name = str(getattr(ocr_provider, "name", requested_provider_name))
        if not has_targets and not detection_is_current:
            detection_options = dict(options)
            detection_options["provider"] = fallback_detector_name
            self._process_detect(store, image_id, detection_options)
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("OCR image was not found")
            original_source = image_path(store, image)
            source, scale_x, scale_y, input_variant = self._processing_source(store, image)
            expected_image_revision = image.revision
            expected_region_versions = self._region_versions(session, image_id)
            target_query = select(TextRegion).where(TextRegion.image_id == image_id)
            if region_id:
                target_query = target_query.where(TextRegion.id == region_id)
            else:
                target_query = target_query.where(TextRegion.ignored.is_(False))
            targets = list(session.scalars(target_query.order_by(TextRegion.reading_order)).all())
            if region_id and len(targets) != 1:
                raise ProjectError("OCR region changed before processing")
            target_snapshots = []
            for target in targets:
                original_box = self._clamp_box(
                    {
                        "x": target.x,
                        "y": target.y,
                        "width": target.width,
                        "height": target.height,
                    },
                    image.width,
                    image.height,
                )
                target_snapshots.append(
                    {
                        "id": target.id,
                        "direction": target.direction
                        if target.direction != "auto"
                        else str(options.get("direction", "auto")),
                        "originalBox": original_box,
                        "processedBox": self._clamp_box(
                            {
                                "x": original_box["x"] * scale_x,
                                "y": original_box["y"] * scale_y,
                                "width": original_box["width"] * scale_x,
                                "height": original_box["height"] * scale_y,
                            },
                            max(1, round(image.width * scale_x)),
                            max(1, round(image.height * scale_y)),
                        ),
                    }
                )
        results: list[tuple[str, OCRRegion, list[dict[str, Any]], int, str, str | None]] = []
        for target in target_snapshots:
            target_direction = str(target["direction"])
            language = self._ocr_language(target_direction, options.get("language"))
            primary = ocr_provider.recognize_region(
                source,
                target["processedBox"],
                direction=target_direction,
                language=language,
            )
            candidates = [(primary, input_variant)]
            primary_confidence = primary.confidence or 0.0
            if input_variant == "preprocessed" and (
                not primary.text.strip() or primary_confidence < 0.45
            ):
                fallback = ocr_provider.recognize_region(
                    original_source,
                    target["originalBox"],
                    direction=target_direction,
                    language=language,
                )
                candidates.append((fallback, "original"))
            selected, selected_variant = max(
                candidates,
                key=lambda candidate: (
                    bool(candidate[0].text.strip()),
                    len("".join(candidate[0].text.split()))
                    * (0.25 + max(0.0, min(1.0, candidate[0].confidence or 0.0))),
                    candidate[0].confidence or 0.0,
                ),
            )
            selected_index = next(
                index
                for index, candidate in enumerate(candidates)
                if candidate[0] is selected and candidate[1] == selected_variant
            )
            attempts = [
                {
                    "provider": provider_name,
                    "inputVariant": variant,
                    "confidence": candidate.confidence,
                    "direction": candidate.direction,
                    "language": language,
                }
                for candidate, variant in candidates
            ]
            results.append(
                (
                    str(target["id"]),
                    selected,
                    attempts,
                    selected_index,
                    selected_variant,
                    language,
                )
            )
        with store.session() as session:
            image = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                expected_region_versions,
                "OCR",
            )
            project = store.project(session)
            for (
                target_id,
                result,
                attempts,
                selected_index,
                selected_variant,
                effective_language,
            ) in results:
                current = session.get(TextRegion, target_id)
                assert current is not None
                before = region_payload(current)
                current.source_text = result.text
                current.confidence = result.confidence
                current.direction = result.direction
                current.confirmed = False
                current.ocr_provider = provider_name
                current.recognition = with_ocr_evidence(
                    current.recognition,
                    result.confidence,
                    provider_name,
                    attempt_count=len(attempts),
                    input_variant=selected_variant,
                    direction=result.direction,
                    attempts=attempts,
                    selected_index=selected_index,
                    language=effective_language,
                )
                if current.ignored:
                    current.recognition = with_human_ignore(current.recognition)
                persisted_ocr = current.recognition.get("ocr") or {}
                current.repair = {
                    **dict(current.repair or {}),
                    "ocrAttemptCount": persisted_ocr.get("attemptCount", len(attempts)),
                    "ocrInputVariant": selected_variant,
                }
                current.revision += 1
                session.flush()
                add_revision(
                    session,
                    project,
                    entity_type="region",
                    entity_id=current.id,
                    operation="ocr",
                    before=before,
                    after=region_payload(current),
                )
            invalidate_image_pipeline(
                store,
                image,
                {"translation", "inpaint", "typeset", "export"},
            )
            reset_image_review(image)
            status = dict(image.status)
            status["ocr"] = "done"
            status["ocrProvider"] = provider_name
            image.status = status
            image.processing_errors = [
                error for error in (image.processing_errors or []) if error.get("stage") != "ocr"
            ]
            image.revision += 1
            current_regions = list(
                session.scalars(select(TextRegion).where(TextRegion.image_id == image_id)).all()
            )
            disposition_counts, reason_counts = self._trust_counts(current_regions)
        input_variant_counts: dict[str, int] = {}
        for (
            _target_id,
            _result,
            _attempts,
            _selected_index,
            selected_variant,
            _effective_language,
        ) in results:
            input_variant_counts[selected_variant] = (
                input_variant_counts.get(selected_variant, 0) + 1
            )
        return {
            "provider": provider_name,
            "inputVariant": input_variant,
            "policyVersion": TRUST_POLICY_VERSION,
            "count": len(results),
            "attemptCount": sum(len(attempts) for _, _, attempts, _, _, _ in results),
            "selectedInputVariantCounts": input_variant_counts,
            "confidenceBuckets": self._confidence_buckets(
                [result.confidence for _, result, _, _, _, _ in results]
            ),
            "dispositionCounts": disposition_counts,
            "reasonCounts": reason_counts,
        }

    def _process_translation(
        self,
        store: ProjectStore,
        image_id: str,
        region_id: str | None,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Translation image was not found")
            project = store.project(session)
            project_settings = dict(project.settings)
            require_current_accepted_stage_review(
                store,
                image,
                "inpaint",
                require_ai=self._requires_ai_inpaint(project_settings),
            )
            expected_image_revision = image.revision
            page_regions = list(
                session.scalars(
                    select(TextRegion)
                    .where(TextRegion.image_id == image_id)
                    .order_by(TextRegion.reading_order)
                ).all()
            )
            expected_region_versions = {region.id: region.revision for region in page_regions}
        eligible_regions = [region for region in page_regions if is_region_trusted(region)]
        targets = eligible_regions
        if region_id:
            requested = next((region for region in page_regions if region.id == region_id), None)
            if requested is None:
                raise ProjectError("Translation region was not found")
            if not is_region_trusted(requested):
                raise ProjectError("Translation region requires explicit human trust confirmation")
            targets = [requested]
        provider_name = str(
            options.get("provider") or project_settings.get("translatorProvider") or "manual"
        )
        provider = self.providers.translation(provider_name, options)
        translated: list[tuple[str, str]] = []
        for target in targets:
            target_index = next(
                index for index, region in enumerate(page_regions) if region.id == target.id
            )
            context: list[str] = []
            context_radius = max(0, min(int(options.get("contextPages", 1)), 10))
            for distance in range(1, context_radius + 1):
                for index in (target_index - distance, target_index + distance):
                    if not 0 <= index < len(page_regions):
                        continue
                    neighbor = page_regions[index]
                    if is_region_trusted(neighbor) and neighbor.source_text:
                        context.append(neighbor.source_text)
            if provider_name == "manual":
                # A manual batch is a no-op; never copy Japanese into or erase reviewed Chinese.
                value = target.translation_text
            else:
                value = provider.translate_text(
                    target.source_text,
                    context,
                    glossary=options.get("glossary") or {},
                    character_names=options.get("characterNames") or {},
                    target_language=(
                        options.get("targetLanguage")
                        or project_settings.get("targetLanguage")
                        or "zh-CN"
                    ),
                )
                existing = target.translation_text or ""
                # A blank provider result must not erase a translation the operator already wrote.
                if not (value or "").strip() and existing.strip():
                    value = existing
            translated.append((target.id, value))
        with store.session() as session:
            current_image = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                expected_region_versions,
                "translation",
            )
            project = store.project(session)
            require_current_accepted_stage_review(
                store,
                current_image,
                "inpaint",
                require_ai=self._requires_ai_inpaint(dict(project.settings)),
            )
            review_changed = False
            for target_id, value in translated:
                current = session.get(TextRegion, target_id)
                assert current is not None
                before = region_payload(current)
                if current.translation_text != value:
                    review_changed = True
                    persist_legacy_recognition(current)
                    current.confirmed = False
                current.translation_text = value
                current.translation_provider = (
                    "manual" if provider_name == "manual" else provider_name
                )
                current.revision += 1
                session.flush()
                add_revision(
                    session,
                    project,
                    entity_type="region",
                    entity_id=current.id,
                    operation="translate",
                    before=before,
                    after=region_payload(current),
                )
            invalidate_image_pipeline(
                store,
                current_image,
                {"typeset", "export"},
            )
            if review_changed:
                reset_image_review(current_image)
            status = dict(current_image.status)
            status["translation"] = "done"
            status["translatorProvider"] = provider_name
            current_image.status = status
            current_image.processing_errors = [
                error
                for error in (current_image.processing_errors or [])
                if error.get("stage") != "translate"
            ]
            current_image.revision += 1
            current_regions = list(
                session.scalars(select(TextRegion).where(TextRegion.image_id == image_id)).all()
            )
            disposition_counts, reason_counts = self._trust_counts(current_regions)
        return {
            "provider": provider_name,
            "policyVersion": TRUST_POLICY_VERSION,
            "count": len(translated),
            "skippedUntrustedCount": (
                len(page_regions) - len(eligible_regions) if region_id is None else 0
            ),
            "dispositionCounts": disposition_counts,
            "reasonCounts": reason_counts,
        }

    @staticmethod
    def _png_bytes(image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _preserve_mask_outside(
        before: Path | Image.Image,
        generated: Image.Image,
        mask: np.ndarray,
    ) -> Image.Image:
        if isinstance(before, Path):
            with Image.open(before) as opened:
                before_image = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
        else:
            before_image = before.copy()
        mode = "RGBA" if "A" in before_image.getbands() else "RGB"
        before_pixels = np.asarray(before_image.convert(mode), dtype=np.uint8)
        generated_pixels = np.asarray(generated.convert(mode), dtype=np.uint8).copy()
        if before_pixels.shape != generated_pixels.shape or mask.shape != before_pixels.shape[:2]:
            raise ProjectError("Inpainting provider returned an image with incompatible dimensions")
        generated_pixels[mask == 0] = before_pixels[mask == 0]
        return Image.fromarray(generated_pixels, mode=mode)

    def _process_render(
        self,
        store: ProjectStore,
        image_id: str,
        options: dict[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        repair_policy = str(options.get("repairPolicy", "safe"))
        if repair_policy not in {"safe", "recognized", "all"}:
            raise ProjectError("repairPolicy must be safe, recognized, or all")
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Render image was not found")
            project = store.project(session)
            require_ai_inpaint = self._requires_ai_inpaint(dict(project.settings))
            if kind == "inpaint":
                source, scale_x, scale_y, render_input_variant = self._inpaint_processing_source(
                    store, image
                )
            else:
                source, scale_x, scale_y, render_input_variant = (
                    self._accepted_inpaint_render_source(
                        store,
                        image,
                        require_ai=require_ai_inpaint,
                    )
                )
            typesetting_provider_name = str(
                (
                    options.get("provider")
                    if kind == "typeset"
                    else options.get("typesetterProvider")
                )
                or "pillow"
            )
            recorded_inpainting_provider = image.status.get("inpaintingProvider")
            persisted_inpainting_provider = recorded_inpainting_provider
            if persisted_inpainting_provider not in {
                "opencv",
                "opencv-inpaint",
                "lama",
                "lama-onnx",
            }:
                persisted_inpainting_provider = None
            requested_page_provider = options.get("inpainterProvider")
            if kind in {"render", "inpaint"}:
                requested_page_provider = options.get("provider") or requested_page_provider
            page_inpainting_provider_name = str(
                requested_page_provider
                or persisted_inpainting_provider
                or project.settings.get("inpainterProvider")
                or "opencv"
            )
            regions = list(
                session.scalars(
                    select(TextRegion)
                    .where(TextRegion.image_id == image_id)
                    .order_by(TextRegion.reading_order)
                ).all()
            )
            if kind in {"typeset", "render"}:
                self._require_translation_acceptance(
                    image_id,
                    regions,
                )
            render_scale = self._render_scale(scale_x, scale_y)
            rendered_size = [round(image.width * scale_x), round(image.height * scale_y)]
            relative = safe_relative_path(image.relative_path).with_suffix(".png")
            expected_image_revision = image.revision
            expected_region_versions = {region.id: region.revision for region in regions}
            raw_overflow = image.status.get("typesetOverflowRegionIds")
            previous_overflow_ids = (
                overflow_region_ids(
                    [
                        {"regionId": item, "overflow": True}
                        for item in raw_overflow
                        if isinstance(item, str)
                    ]
                )
                if isinstance(raw_overflow, list)
                else []
            )
        if kind != "inpaint" and typesetting_provider_name != "pillow":
            raise ProjectError(f"Unknown typesetting provider: {typesetting_provider_name}")
        active_regions_canonical = [
            region_payload(region) for region in regions if not region.ignored
        ]
        active_regions = [
            self._scale_render_region(region, scale_x, scale_y)
            for region in active_regions_canonical
        ]
        canonical_regions_by_id = {str(region["id"]): region for region in active_regions_canonical}
        render_regions_by_id = {str(region["id"]): region for region in active_regions}

        def should_repair(region: dict[str, Any]) -> bool:
            if repair_policy == "all":
                return True
            source_text = str(region.get("sourceText", "")).strip()
            if repair_policy == "recognized":
                return bool(source_text)
            recognition = region.get("recognition")
            if not isinstance(recognition, dict):
                return False
            trust = recognition.get("trust")
            return (
                isinstance(trust, dict)
                and trust.get("policyVersion") == TRUST_POLICY_VERSION
                and trust.get("disposition") == "trusted"
            )

        repair_data = [region for region in active_regions if should_repair(region)]
        typesetting_data = [
            region for region in repair_data if str(region.get("translationText", "")).strip()
        ]
        typesetting_data_canonical = [
            canonical_regions_by_id[str(region["id"])] for region in typesetting_data
        ]
        inpaint_relative = Path("generated") / "inpainted" / relative
        typeset_relative = Path("generated") / "typeset" / relative
        mask_relative = Path("generated") / "masks" / relative
        inpaint_path = resolve_write_target(
            store.root,
            inpaint_relative,
            protected_roots=(store.source_root,),
        )
        typeset_path = resolve_write_target(
            store.root,
            typeset_relative,
            protected_roots=(store.source_root,),
        )
        mask_path = resolve_write_target(
            store.root,
            mask_relative,
            protected_roots=(store.source_root,),
        )
        mask_bytes: bytes | None = None
        inpainted_bytes: bytes | None = None
        typeset_bytes: bytes | None = None
        render_mask: np.ndarray | None = None
        typeset_source: Path | Image.Image = inpaint_path
        selected_inpaint_candidate: str | None = None
        public_inpaint_candidates: list[dict[str, Any]] = []
        pending_candidate_files: list[tuple[str, bytes]] = []
        pending_candidate_manifest: list[dict[str, Any]] = []
        inpaint_generation_id: str | None = None
        region_inpainting_providers: dict[str, str] = {}
        full_context_provider_name: str | None = None
        component_candidate_allowed = True
        effective_inpainting_provider_name = str(
            recorded_inpainting_provider or page_inpainting_provider_name
        )
        rebuilt_inpaint = False
        overlay_ids: list[str] = []
        did_partial_typeset = False

        def render_int_option(
            option_key: str,
            repair: dict[str, Any],
            repair_keys: tuple[str, ...],
            default: int,
        ) -> int:
            if option_key in options:
                return round(float(options[option_key]) * render_scale)
            for repair_key in repair_keys:
                if repair_key in repair:
                    return int(repair[repair_key])
            return default * render_scale

        def render_float_option(
            option_key: str,
            repair: dict[str, Any],
            repair_key: str,
            default: float,
        ) -> float:
            if option_key in options:
                return float(options[option_key]) * render_scale
            if repair_key in repair:
                return float(repair[repair_key])
            return default * render_scale

        if kind == "inpaint":
            rebuilt_inpaint = True
            inpaint_generation_id = str(uuid.uuid4())
            try:
                page_inpainting_provider = self.providers.inpainter(page_inpainting_provider_name)
            except ValueError as error:
                raise ProjectError(str(error)) from None
            page_inpainting_provider_name = str(
                getattr(page_inpainting_provider, "name", page_inpainting_provider_name)
            )
            mask = page_inpainting_provider.create_mask(
                source,
                [],
                padding=0,
                dilation=0,
                feather=0,
                render_scale=render_scale,
            )
            cleaned: Path | Image.Image = source
            repaired_region_count = 0
            for region in repair_data:
                repair = region.get("repair") if isinstance(region.get("repair"), dict) else {}
                requested_region_provider = str(
                    repair.get("inpainterProvider") or page_inpainting_provider_name
                )
                try:
                    inpainting_provider = self.providers.inpainter(requested_region_provider)
                except ValueError as error:
                    raise ProjectError(str(error)) from None
                inpainting_provider_name = str(
                    getattr(inpainting_provider, "name", requested_region_provider)
                )
                padding = render_int_option(
                    "padding",
                    repair,
                    ("maskPadding", "padding"),
                    int(DEFAULT_REPAIR_SETTINGS["maskPadding"]),
                )
                dilation = render_int_option(
                    "dilation",
                    repair,
                    ("dilation",),
                    int(DEFAULT_REPAIR_SETTINGS["dilation"]),
                )
                feather = render_int_option(
                    "feather",
                    repair,
                    ("feather",),
                    int(DEFAULT_REPAIR_SETTINGS["feather"]),
                )
                default_mask_mode = str(DEFAULT_REPAIR_SETTINGS["maskMode"])
                mask_mode = str(options.get("maskMode", repair.get("maskMode", default_mask_mode)))
                default_text_polarity = str(DEFAULT_REPAIR_SETTINGS["textPolarity"])
                text_polarity = str(
                    options.get(
                        "textPolarity",
                        repair.get("textPolarity", default_text_polarity),
                    )
                )
                mask_region = {
                    "x": region["x"],
                    "y": region["y"],
                    "width": region["width"],
                    "height": region["height"],
                    "rotation": region.get("rotation", 0),
                    "padding": padding,
                    "maskMode": mask_mode,
                    "textPolarity": text_polarity,
                }
                if repair.get("maskPolygon"):
                    mask_region["maskPolygon"] = repair["maskPolygon"]
                if repair.get("maskEdits") is not None:
                    mask_region["maskEdits"] = repair["maskEdits"]
                region_mask = inpainting_provider.create_mask(
                    source,
                    [mask_region],
                    padding=padding,
                    dilation=dilation,
                    feather=feather,
                    mask_mode=mask_mode,
                    text_polarity=text_polarity,
                    render_scale=render_scale,
                )
                if not np.any(region_mask):
                    continue
                region_inpainting_providers[str(region["id"])] = inpainting_provider_name
                if inpainting_provider_name not in {"lama", "lama-onnx"} or mask_mode != "manual":
                    component_candidate_allowed = False
                mask = np.maximum(mask, region_mask)
                if inpainting_provider_name in {"lama", "lama-onnx"}:
                    if full_context_provider_name is None:
                        full_context_provider_name = inpainting_provider_name
                    generated = inpainting_provider.inpaint(
                        cleaned,
                        region_mask,
                        context_padding=render_int_option(
                            "contextPadding",
                            repair,
                            ("contextPadding",),
                            64,
                        ),
                        inference_padding=DEFAULT_INFERENCE_PADDING * render_scale,
                        # The persisted region mask already contains the final
                        # feather weights. Blurring again inside LaMa would make
                        # its composite boundary diverge from that saved mask.
                        feather=0,
                        render_scale=render_scale,
                    )
                else:
                    generated = inpainting_provider.inpaint(
                        cleaned,
                        region_mask,
                        radius=render_float_option(
                            "radius",
                            repair,
                            "radius",
                            float(DEFAULT_REPAIR_SETTINGS["radius"]),
                        ),
                        method=str(
                            options.get(
                                "method",
                                repair.get("method", DEFAULT_REPAIR_SETTINGS["method"]),
                            )
                        ),
                        fill_color=str(
                            options.get(
                                "fillColor",
                                repair.get("fillColor", DEFAULT_REPAIR_SETTINGS["fillColor"]),
                            )
                        ),
                        render_scale=render_scale,
                    )
                cleaned = self._preserve_mask_outside(cleaned, generated, region_mask)
                repaired_region_count += 1
            routed_providers = set(region_inpainting_providers.values())
            if len(routed_providers) == 1:
                effective_inpainting_provider_name = next(iter(routed_providers))
            elif len(routed_providers) > 1:
                effective_inpainting_provider_name = "mixed"
            else:
                effective_inpainting_provider_name = page_inpainting_provider_name
            if isinstance(cleaned, Path):
                with Image.open(source) as opened:
                    cleaned = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
            with Image.open(source) as opened:
                opened.load()
                original = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
            used_only_lama = bool(region_inpainting_providers) and all(
                name in {"lama", "lama-onnx"} for name in region_inpainting_providers.values()
            )
            component_context_candidate: Image.Image | None = None
            if (
                full_context_provider_name is not None
                and used_only_lama
                and component_candidate_allowed
                and np.any(mask)
            ):
                try:
                    component_provider = self.providers.inpainter(full_context_provider_name)
                    component_runner = getattr(component_provider, "inpaint_components", None)
                    if callable(component_runner):
                        generated_component_context = component_runner(
                            original,
                            mask,
                            context_padding=COMPONENT_CONTEXT_PADDING * render_scale,
                            inference_padding=COMPONENT_INFERENCE_PADDING * render_scale,
                            feather=0,
                            render_scale=render_scale,
                        )
                        component_context_candidate = self._preserve_mask_outside(
                            original,
                            generated_component_context,
                            mask,
                        )
                except Exception:
                    # This is an optional comparison candidate. The ordinary
                    # per-region result remains valid if local component redraw
                    # is unavailable, malformed, or exceeds its conservative cap.
                    logger.warning("Optional component-context inpaint candidate was skipped")
            overview_refine_candidate: Image.Image | None = None
            overview_base_candidate: Image.Image | None = None
            overview_refine_provider_name: str | None = None
            mask_rows, mask_columns = np.nonzero(mask > 0)
            overview_refine_needed = bool(len(mask_rows)) and (
                int(mask_rows.min()) == 0
                or int(mask_columns.min()) == 0
                or int(mask_rows.max()) == mask.shape[0] - 1
                or int(mask_columns.max()) == mask.shape[1] - 1
                or int(mask_rows.max() - mask_rows.min() + 1) > MODEL_SIZE - TILE_OVERLAP
                or int(mask_columns.max() - mask_columns.min() + 1) > MODEL_SIZE - TILE_OVERLAP
            )
            if (
                full_context_provider_name is not None
                and used_only_lama
                and component_candidate_allowed
                and overview_refine_needed
            ):
                try:
                    overview_provider = self.providers.inpainter(full_context_provider_name)
                    overview_pair_runner = getattr(
                        overview_provider,
                        "inpaint_overview_candidates",
                        None,
                    )
                    if callable(overview_pair_runner):
                        generated_overview_base, generated_overview_refine = overview_pair_runner(
                            original,
                            mask,
                            context_padding=128 * render_scale,
                            inference_padding=DEFAULT_INFERENCE_PADDING * render_scale,
                            feather=0,
                            render_scale=render_scale,
                        )
                        preserved_overview_base = self._preserve_mask_outside(
                            original,
                            generated_overview_base,
                            mask,
                        )
                        preserved_overview_refine = self._preserve_mask_outside(
                            original,
                            generated_overview_refine,
                            mask,
                        )
                        overview_base_candidate = preserved_overview_base
                        overview_refine_candidate = preserved_overview_refine
                        overview_refine_provider_name = full_context_provider_name
                    else:
                        overview_runner = getattr(
                            overview_provider,
                            "inpaint_overview_refine",
                            None,
                        )
                        if callable(overview_runner):
                            generated_overview_refine = overview_runner(
                                original,
                                mask,
                                context_padding=128 * render_scale,
                                inference_padding=DEFAULT_INFERENCE_PADDING * render_scale,
                                feather=0,
                                render_scale=render_scale,
                            )
                            overview_refine_candidate = self._preserve_mask_outside(
                                original,
                                generated_overview_refine,
                                mask,
                            )
                            overview_refine_provider_name = full_context_provider_name
                except Exception:
                    # Large-cavity overview repair is an optional comparison aid.
                    # Its failure must never invalidate the ordinary provider result.
                    logger.warning("Optional overview-refine inpaint candidate was skipped")
            full_context_candidate: Image.Image | None = None
            if full_context_provider_name is not None and np.any(mask):
                full_context_provider = self.providers.inpainter(full_context_provider_name)
                try:
                    generated_full_context = full_context_provider.inpaint(
                        original,
                        mask,
                        context_padding=128 * render_scale,
                        inference_padding=DEFAULT_INFERENCE_PADDING * render_scale,
                        feather=0,
                        render_scale=render_scale,
                    )
                    full_context_candidate = self._preserve_mask_outside(
                        original,
                        generated_full_context,
                        mask,
                    )
                except Exception:
                    # The extra whole-union pass is a comparison aid. Primary
                    # per-region repair already succeeded, so an optional
                    # candidate must never fail the page or expose provider
                    # exception details in public job output.
                    logger.warning("Optional full-context inpaint candidate was skipped")
            (
                selected_inpaint_candidate,
                inpainted_bytes,
                public_inpaint_candidates,
                pending_candidate_files,
                pending_candidate_manifest,
            ) = prepare_page_inpaint_candidates(
                source=original,
                mask=mask,
                primary=cleaned,
                used_only_lama=used_only_lama,
                radius=float(DEFAULT_REPAIR_SETTINGS["radius"]) * render_scale,
                render_scale=render_scale,
                full_context=full_context_candidate,
                component_context=component_context_candidate,
                overview_base=overview_base_candidate,
                overview_refine=overview_refine_candidate,
                primary_provider_ids=sorted(routed_providers),
                full_context_provider_id=(
                    full_context_provider_name if full_context_candidate is not None else None
                ),
                component_context_provider_id=(
                    full_context_provider_name if component_context_candidate is not None else None
                ),
                overview_provider_id=(
                    overview_refine_provider_name if overview_base_candidate is not None else None
                ),
                overview_refine_provider_id=overview_refine_provider_name,
            )
            with Image.open(io.BytesIO(inpainted_bytes)) as selected_image:
                selected_image.load()
                cleaned = selected_image.copy()
            mask_bytes = self._png_bytes(Image.fromarray(mask))
            render_mask = mask
            typeset_source = cleaned
        if kind == "typeset":
            requested_ids = self._typeset_region_ids(options)
            if requested_ids and not rebuilt_inpaint:
                page_ids = {str(region["id"]) for region in active_regions_canonical}
                matching = [region_id for region_id in requested_ids if region_id in page_ids]
                if matching and typeset_path.is_file() and inpaint_path.is_file():
                    overlay_ids = expand_typeset_region_ids(
                        typesetting_data_canonical,
                        matching,
                    )
                    overlay_set = set(overlay_ids)
                    overlay_ids.extend(
                        region_id for region_id in matching if region_id not in overlay_set
                    )
                    overlay_set = set(overlay_ids)
                    punch_regions = [
                        render_regions_by_id[region_id]
                        for region_id in overlay_ids
                        if region_id in render_regions_by_id
                    ]
                    typesetting_data = [
                        region for region in typesetting_data if str(region["id"]) in overlay_set
                    ]
                    try:
                        with Image.open(typeset_path) as current_typeset:
                            current_typeset.load()
                            current = current_typeset.copy()
                        with Image.open(inpaint_path) as clean_plate:
                            clean_plate.load()
                            clean = clean_plate.copy()
                        typeset_source = restore_clean_region_boxes(
                            current,
                            clean,
                            punch_regions,
                        )
                    except (OSError, ValueError) as error:
                        raise ProjectError(
                            "Current typeset overlay could not be prepared; rerun full typesetting"
                        ) from error
                    did_partial_typeset = True
        if kind != "inpaint":
            if render_mask is None:
                if not mask_path.is_file():
                    raise ProjectError("Current inpainting mask is unavailable; rerun inpainting")
                try:
                    with Image.open(mask_path) as opened:
                        render_mask = np.asarray(opened.convert("L"), dtype=np.uint8)
                except (OSError, ValueError) as error:
                    raise ProjectError(
                        "Current inpainting mask could not be decoded; rerun inpainting"
                    ) from error

            def overlaps_actual_mask(region: dict[str, Any]) -> bool:
                height, width = render_mask.shape
                left = max(0, min(width, math.floor(float(region["x"]))))
                top = max(0, min(height, math.floor(float(region["y"]))))
                right = max(
                    left,
                    min(width, math.ceil(float(region["x"]) + float(region["width"]))),
                )
                bottom = max(
                    top,
                    min(height, math.ceil(float(region["y"]) + float(region["height"]))),
                )
                return (
                    right > left
                    and bottom > top
                    and bool(np.any(render_mask[top:bottom, left:right]))
                )

            renderable_typesetting_data = [
                region for region in typesetting_data if overlaps_actual_mask(region)
            ]
            result = typeset_image(
                typeset_source,
                renderable_typesetting_data,
                geometry_scale=render_scale,
            )
            typeset_bytes = self._png_bytes(result.image)
            layouts = result.layouts
        else:
            renderable_typesetting_data = []
            layouts = []
        overflow_ids: list[str] = []
        with store.session() as session:
            current = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                expected_region_versions,
                "rendering",
            )
            if kind == "inpaint" and render_input_variant == "preprocessed":
                require_current_accepted_stage_review(store, current, "preprocess")
            if kind != "inpaint":
                project = store.project(session)
                self._accepted_inpaint_render_source(
                    store,
                    current,
                    require_ai=self._requires_ai_inpaint(dict(project.settings)),
                )
            if mask_bytes is not None:
                atomic_write_bytes(mask_path, mask_bytes)
            if inpainted_bytes is not None:
                atomic_write_bytes(inpaint_path, inpainted_bytes)
            if pending_candidate_files and selected_inpaint_candidate is not None:
                assert inpaint_generation_id is not None
                assert mask_bytes is not None
                write_page_inpaint_candidates(
                    store,
                    relative,
                    selected_id=selected_inpaint_candidate,
                    generation_id=inpaint_generation_id,
                    mask_checksum=hashlib.sha256(mask_bytes).hexdigest(),
                    encoded_files=pending_candidate_files,
                    manifest_candidates=pending_candidate_manifest,
                )
            if typeset_bytes is not None:
                atomic_write_bytes(typeset_path, typeset_bytes)
            if inpainted_bytes is not None or typeset_bytes is not None:
                reset_image_review(current)
            if inpainted_bytes is not None:
                current.inpaint_classical_approval = None
                current.inpaint_ai_candidate_reviews = None
            invalidate_image_pipeline(
                store,
                current,
                {"typeset", "export"} if kind == "inpaint" else {"export"},
            )
            status = dict(current.status)
            status["inpaint"] = "done"
            if mask_bytes is not None:
                assert inpainted_bytes is not None
                assert inpaint_generation_id is not None
                artifact_checksum = hashlib.sha256(inpainted_bytes).hexdigest()
                mask_checksum = hashlib.sha256(mask_bytes).hexdigest()
                candidate_manifest_digest = inpaint_candidate_manifest_digest(
                    generation_id=inpaint_generation_id,
                    mask_checksum=mask_checksum,
                    candidates=pending_candidate_manifest,
                )
                if np.any(render_mask):
                    selected_record = next(
                        (
                            record
                            for record in pending_candidate_manifest
                            if record.get("id") == selected_inpaint_candidate
                        ),
                        None,
                    )
                    if selected_record is None:
                        raise ProjectError("Selected inpainting provenance is unavailable")
                    current.inpaint_provenance = make_inpaint_provenance(
                        artifact_checksum=artifact_checksum,
                        mask_checksum=mask_checksum,
                        candidate_id=str(selected_record["id"]),
                        origin_kind=str(selected_record["originKind"]),
                        provider_ids=selected_record["providerIds"],
                        generation_id=inpaint_generation_id,
                        candidate_manifest_digest=candidate_manifest_digest,
                    )
                else:
                    current.inpaint_provenance = make_inpaint_provenance(
                        artifact_checksum=artifact_checksum,
                        mask_checksum=mask_checksum,
                        candidate_id="primary",
                        origin_kind="no-op",
                        provider_ids=[],
                        generation_id=inpaint_generation_id,
                        candidate_manifest_digest=candidate_manifest_digest,
                    )
                status["inpaintingProvider"] = effective_inpainting_provider_name
                status["inpaintingRepairPolicy"] = repair_policy
                status["renderInputVariant"] = render_input_variant
                status["renderScale"] = [scale_x, scale_y]
                status["renderedSize"] = rendered_size
                status.pop("repairPolicy", None)
                if public_inpaint_candidates:
                    status["inpaintCandidate"] = selected_inpaint_candidate
                    status["inpaintCandidates"] = public_inpaint_candidates
                else:
                    status.pop("inpaintCandidate", None)
                    status.pop("inpaintCandidates", None)
            overflow_ids = overflow_region_ids(layouts)
            if did_partial_typeset:
                touched = set(overlay_ids)
                kept = [
                    region_id for region_id in previous_overflow_ids if region_id not in touched
                ]
                seen_overflow = set(kept)
                overflow_ids = kept + [
                    region_id for region_id in overflow_ids if region_id not in seen_overflow
                ]
            if kind != "inpaint":
                status["typeset"] = "done"
                status["typesettingProvider"] = typesetting_provider_name
                status["typesetOverflowCount"] = len(overflow_ids)
                status["typesetOverflowRegionIds"] = overflow_ids
            current.status = status
            clear_stage_reviews(
                current,
                ({"inpaint"} if inpainted_bytes is not None else set())
                | ({"typeset"} if typeset_bytes is not None else set()),
            )
            cleared_stages = {"render", "inpaint", "typeset"}
            current.processing_errors = [
                error
                for error in (current.processing_errors or [])
                if error.get("stage") not in cleared_stages
            ]
            current.revision += 1
        return {
            "provider": (
                effective_inpainting_provider_name
                if kind in {"render", "inpaint"}
                else typesetting_provider_name
            ),
            "inpaintingProvider": effective_inpainting_provider_name,
            "inpaintingProviders": sorted(set(region_inpainting_providers.values())),
            "typesettingProvider": (typesetting_provider_name if kind != "inpaint" else None),
            "repairPolicy": repair_policy,
            "inputVariant": render_input_variant,
            "renderedSize": rendered_size,
            "scale": [scale_x, scale_y],
            "eligibleRegionCount": len(repair_data),
            "skippedRegionCount": len(active_regions) - len(repair_data),
            "repairedRegionCount": repaired_region_count if mask_bytes is not None else None,
            "inpaintCandidate": selected_inpaint_candidate,
            "inpaintCandidateCount": (
                len(public_inpaint_candidates) if public_inpaint_candidates else None
            ),
            "typesetEligibleRegionCount": (
                len(renderable_typesetting_data) if kind != "inpaint" else None
            ),
            "typesetSkippedRegionCount": (
                len(active_regions) - len(renderable_typesetting_data)
                if kind != "inpaint"
                else None
            ),
            "overflowCount": len(overflow_ids),
            "overflowRegionIds": overflow_ids,
            "partialTypeset": did_partial_typeset,
            "overlayRegionCount": len(overlay_ids) if did_partial_typeset else 0,
            "overlayRegionIds": list(overlay_ids) if did_partial_typeset else [],
        }
