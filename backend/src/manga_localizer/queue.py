from __future__ import annotations

import asyncio
import io
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.config import Settings
from manga_localizer.database import ImageAsset, Job, JobItem, JobStatus, TextRegion
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
from manga_localizer.providers.ocr import OCRRegion
from manga_localizer.providers.registry import ProviderRegistry
from manga_localizer.security import (
    atomic_write_bytes,
    normalize_remote_endpoints,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.exporting import (
    choose_export_root,
    ensure_project_bundle,
    export_image,
    validate_image_export_readiness,
    write_json_export_summary,
)
from manga_localizer.services.images import (
    clear_stage_reviews,
    image_path,
    invalidate_image_pipeline,
    reset_image_review,
)
from manga_localizer.services.inpaint_candidates import (
    prepare_page_inpaint_candidates,
    write_page_inpaint_candidates,
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
            item_ids = [
                item.id
                for item in job.items
                if item.status not in {JobStatus.COMPLETED.value, JobStatus.CANCELLED.value}
            ]
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
                if image is not None and not isinstance(error, StaleJobResult):
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
    ) -> Job:
        if kind not in {
            "preprocess",
            "detect",
            "ocr",
            "translate",
            "render",
            "export",
            "inpaint",
            "typeset",
        }:
            raise ProjectError(f"Unsupported job kind: {kind}")
        if len(set(image_ids)) != len(image_ids) or len(set(region_ids)) != len(region_ids):
            raise ProjectError("Job targets must not contain duplicate ids")
        safe_options = normalize_remote_endpoints(without_secrets(options))
        safe_options.pop("regionIds", None)
        safe_options.pop("region_ids", None)
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
            job = Job(
                project_id=project.id,
                kind=kind,
                status=JobStatus.QUEUED.value,
                options=safe_options,
                total=len(targets),
            )
            session.add(job)
            session.flush()
            for position, (image_id, region_id) in enumerate(targets):
                session.add(
                    JobItem(
                        job_id=job.id,
                        image_id=image_id,
                        region_id=region_id,
                        position=position,
                    )
                )
            session.flush()
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

    def pause(self, store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ProjectError("Job was not found")
            if job.status not in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                raise JobConflict(f"Cannot pause a {job.status} job")
            job.status = JobStatus.PAUSED.value
        store.write_snapshot()
        return self.get_job(store, job_id)

    def resume(self, store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ProjectError("Job was not found")
            if job.status != JobStatus.PAUSED.value:
                raise JobConflict(f"Cannot resume a {job.status} job")
            job.status = JobStatus.QUEUED.value
        store.write_snapshot()
        return self.get_job(store, job_id)

    def cancel(self, store: ProjectStore, job_id: str) -> Job:
        with store.session() as session:
            job = session.scalar(
                select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
            )
            if job is None:
                raise ProjectError("Job was not found")
            if job.status in {
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            }:
                raise JobConflict(f"Cannot cancel a {job.status} job")
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
            if job.status not in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                raise JobConflict(f"Cannot retry a {job.status} job")
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
            kind = job.kind
            options = dict(job.options)
            image_id = item.image_id
            region_id = item.region_id
        if image_id is None:
            raise ProjectError("Job item has no image")
        if kind == "preprocess":
            return self._process_preprocess(store, image_id, options)
        if kind == "detect":
            return self._process_detect(store, image_id, options)
        if kind == "ocr":
            return self._process_ocr(store, image_id, region_id, options)
        if kind == "translate":
            return self._process_translation(store, image_id, region_id, options)
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
            source, scale_x, scale_y, input_variant = self._processing_source(store, image)
            image_width = image.width
            image_height = image.height
            expected_image_revision = image.revision
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
                None,
                "text detection",
            )
            existing = list(
                session.scalars(select(TextRegion).where(TextRegion.image_id == image_id)).all()
            )
            for region in list(existing):
                if not self._is_stale_auto_detection(region, image.width, image.height):
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

    def _process_ocr(
        self,
        store: ProjectStore,
        image_id: str,
        region_id: str | None,
        options: dict[str, Any],
    ) -> dict[str, Any]:
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
            typesetting_provider_name = str(
                (
                    options.get("provider")
                    if kind == "typeset"
                    else options.get("typesetterProvider")
                )
                or "pillow"
            )
            inpaint_is_current = image.status.get("inpaint") == "done"
            recorded_repair_policy = image.status.get("inpaintingRepairPolicy") or image.status.get(
                "repairPolicy"
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
            source = image_path(store, image)
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
        active_regions = [region_payload(region) for region in regions if not region.ignored]

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
        region_inpainting_providers: dict[str, str] = {}
        effective_inpainting_provider_name = str(
            recorded_inpainting_provider or page_inpainting_provider_name
        )
        rebuilt_inpaint = False
        overlay_ids: list[str] = []
        did_partial_typeset = False
        if (
            kind != "typeset"
            or not inpaint_is_current
            or recorded_repair_policy != repair_policy
            or not inpaint_path.exists()
        ):
            rebuilt_inpaint = True
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
                region_inpainting_providers[str(region["id"])] = inpainting_provider_name
                padding = int(
                    options.get(
                        "padding",
                        repair.get(
                            "maskPadding",
                            repair.get("padding", DEFAULT_REPAIR_SETTINGS["maskPadding"]),
                        ),
                    )
                )
                dilation = int(
                    options.get(
                        "dilation",
                        repair.get("dilation", DEFAULT_REPAIR_SETTINGS["dilation"]),
                    )
                )
                feather = int(
                    options.get(
                        "feather",
                        repair.get("feather", DEFAULT_REPAIR_SETTINGS["feather"]),
                    )
                )
                default_mask_mode = str(DEFAULT_REPAIR_SETTINGS["maskMode"])
                mask_mode = str(options.get("maskMode", repair.get("maskMode", default_mask_mode)))
                mask_region = {
                    "x": region["x"],
                    "y": region["y"],
                    "width": region["width"],
                    "height": region["height"],
                    "rotation": region.get("rotation", 0),
                    "padding": padding,
                    "maskMode": mask_mode,
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
                )
                if not np.any(region_mask):
                    continue
                mask = np.maximum(mask, region_mask)
                if inpainting_provider_name in {"lama", "lama-onnx"}:
                    generated = inpainting_provider.inpaint(
                        cleaned,
                        region_mask,
                        context_padding=int(
                            options.get("contextPadding", repair.get("contextPadding", 64))
                        ),
                        # The persisted region mask already contains the final
                        # feather weights. Blurring again inside LaMa would make
                        # its composite boundary diverge from that saved mask.
                        feather=0,
                    )
                else:
                    generated = inpainting_provider.inpaint(
                        cleaned,
                        region_mask,
                        radius=float(
                            options.get(
                                "radius",
                                repair.get("radius", DEFAULT_REPAIR_SETTINGS["radius"]),
                            )
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
                radius=float(DEFAULT_REPAIR_SETTINGS["radius"]),
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
                page_ids = {str(region["id"]) for region in active_regions}
                matching = [region_id for region_id in requested_ids if region_id in page_ids]
                if matching and typeset_path.is_file() and inpaint_path.is_file():
                    overlay_ids = expand_typeset_region_ids(typesetting_data, matching)
                    overlay_set = set(overlay_ids)
                    overlay_ids.extend(
                        region_id for region_id in matching if region_id not in overlay_set
                    )
                    overlay_set = set(overlay_ids)
                    punch_regions = [
                        region for region in active_regions if str(region["id"]) in overlay_set
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
            result = typeset_image(typeset_source, renderable_typesetting_data)
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
            if mask_bytes is not None:
                atomic_write_bytes(mask_path, mask_bytes)
            if inpainted_bytes is not None:
                atomic_write_bytes(inpaint_path, inpainted_bytes)
            if pending_candidate_files and selected_inpaint_candidate is not None:
                write_page_inpaint_candidates(
                    store,
                    relative,
                    selected_id=selected_inpaint_candidate,
                    encoded_files=pending_candidate_files,
                    manifest_candidates=pending_candidate_manifest,
                )
            if typeset_bytes is not None:
                atomic_write_bytes(typeset_path, typeset_bytes)
            if inpainted_bytes is not None or typeset_bytes is not None:
                reset_image_review(current)
            invalidate_image_pipeline(
                store,
                current,
                {"typeset", "export"} if kind == "inpaint" else {"export"},
            )
            status = dict(current.status)
            status["inpaint"] = "done"
            if mask_bytes is not None:
                status["inpaintingProvider"] = effective_inpainting_provider_name
                status["inpaintingRepairPolicy"] = repair_policy
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
