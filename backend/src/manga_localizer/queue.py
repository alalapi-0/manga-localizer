from __future__ import annotations

import asyncio
import io
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.config import Settings
from manga_localizer.database import ImageAsset, Job, JobItem, JobStatus, TextRegion
from manga_localizer.imaging import typeset_image
from manga_localizer.logging_utils import without_secrets
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
)
from manga_localizer.services.images import image_path, invalidate_image_pipeline
from manga_localizer.services.projects import (
    ProjectError,
    ProjectRegistry,
    ProjectStore,
    add_revision,
    region_payload,
    safe_error,
)

logger = logging.getLogger(__name__)


class JobConflict(ProjectError):
    pass


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
                    f"Project bundle finalization failed: {safe_error(error)}",
                )
                store.write_snapshot()

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
            export_root = choose_export_root(store, job_options.get("outputPath"), job_id)
            ensure_project_bundle(store, export_root, finalized_job_id=job_id)
            with store.session() as session:
                finished_job = session.scalar(
                    select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
                )
                if finished_job is None:
                    raise ProjectError("Export job disappeared during bundle finalization")
                finished_job.options = {
                    **dict(finished_job.options),
                    "bundleFinalized": True,
                }
                PersistentJobQueue._recompute_in_session(finished_job)
            store.write_snapshot()

    async def _execute_item(self, store: ProjectStore, job_id: str, item_id: str) -> None:
        await asyncio.to_thread(self._execute_item_sync, store, job_id, item_id)

    def _execute_item_sync(self, store: ProjectStore, job_id: str, item_id: str) -> None:
        with store.session() as session:
            job = session.get(Job, job_id)
            kind = job.kind if job is not None else None

        def process_and_finish() -> None:
            try:
                output = self._process_item(store, job_id, item_id)
            except Exception as error:
                self._finish_item(store, job_id, item_id, error=error)
            else:
                self._finish_item(store, job_id, item_id, output=output)
            store.write_snapshot()

        if kind == "export":
            # Keep the metadata read, filesystem copy, and export status transition indivisible
            # relative to editor mutations. Export jobs are serialized by _job_concurrency.
            with store.lock:
                process_and_finish()
            return
        process_and_finish()

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
                item.status = JobStatus.FAILED.value
                item.error = safe_error(error).replace(str(store.root), "<project>")
                image = session.get(ImageAsset, item.image_id) if item.image_id else None
                if image is not None:
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
        if kind not in {"detect", "ocr", "translate", "render", "export", "inpaint", "typeset"}:
            raise ProjectError(f"Unsupported job kind: {kind}")
        if len(set(image_ids)) != len(image_ids) or len(set(region_ids)) != len(region_ids):
            raise ProjectError("Job targets must not contain duplicate ids")
        safe_options = normalize_remote_endpoints(without_secrets(options))
        if kind == "export":
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
            reset = 0
            for item in job.items:
                if item.status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
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
        if kind == "detect":
            return self._process_detect(store, image_id, options)
        if kind == "ocr":
            return self._process_ocr(store, image_id, region_id, options)
        if kind == "translate":
            return self._process_translation(store, image_id, region_id, options)
        if kind in {"render", "inpaint", "typeset"}:
            return self._process_render(store, image_id, options, kind)
        if kind == "export":
            root = choose_export_root(store, options.get("outputPath"), job_id)
            return export_image(
                store,
                image_id,
                export_root=root,
                export_format=str(options.get("format", "both")),
                conflict=str(options.get("conflict", "rename")),
                preserve_tree=bool(options.get("preserveTree", True)),
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
            raise ProjectError(f"Image changed during {operation}; retry the job")
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
            provider_name = str(
                options.get("provider")
                or options.get("detectorProvider")
                or project.settings.get("detectorProvider")
                or "tesseract"
            )
            source = image_path(store, image)
            expected_image_revision = image.revision
        if provider_name != "tesseract":
            raise ProjectError(f"Unsupported detection provider: {provider_name}")
        direction = str(options.get("direction", self.settings.ocr_default_direction))
        language = self._ocr_language(direction, options.get("language"))
        detections = self.providers.ocr.detect_text_regions(
            source,
            direction=direction,
            language=language,
        )
        created_ids: list[str] = []
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
            for region in existing:
                if region.repair.get("detectorGenerated") and not region.confirmed:
                    before = region_payload(region)
                    session.delete(region)
                    add_revision(
                        session,
                        project,
                        entity_type="region",
                        entity_id=region.id,
                        operation="detect-replace",
                        before=before,
                        after=None,
                    )
            for order, detection in enumerate(detections):
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
                    reading_order=order,
                    repair={
                        "detectorGenerated": True,
                        "detectedTextCandidate": detection.text,
                    },
                    revision=1,
                )
                session.add(region)
                session.flush()
                created_ids.append(region.id)
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
            status = dict(image.status)
            status["detection"] = "done"
            status["detectorProvider"] = provider_name
            image.status = status
            image.processing_errors = [
                error for error in (image.processing_errors or []) if error.get("stage") != "detect"
            ]
            image.revision += 1
        return {
            "provider": provider_name,
            "regionIds": created_ids,
            "count": len(created_ids),
            "candidates": [detection.to_dict() for detection in detections],
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
            provider_name = str(
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
            if region_id:
                region = session.get(TextRegion, region_id)
                if region is None or region.image_id != image_id:
                    raise ProjectError("OCR region was not found")
                has_targets = True
        if provider_name != "tesseract":
            raise ProjectError(f"Unsupported OCR provider: {provider_name}")
        if not has_targets:
            self._process_detect(store, image_id, options)
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("OCR image was not found")
            source = image_path(store, image)
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
            target_snapshots = [
                {
                    "id": target.id,
                    "direction": target.direction
                    if target.direction != "auto"
                    else str(options.get("direction", "auto")),
                    "box": {
                        "x": target.x,
                        "y": target.y,
                        "width": target.width,
                        "height": target.height,
                    },
                }
                for target in targets
            ]
        results = [
            (
                str(target["id"]),
                self.providers.ocr.recognize_region(
                    source,
                    target["box"],
                    direction=str(target["direction"]),
                    language=self._ocr_language(
                        str(target["direction"]),
                        options.get("language"),
                    ),
                ),
            )
            for target in target_snapshots
        ]
        with store.session() as session:
            image = self._assert_image_unchanged(
                session,
                image_id,
                expected_image_revision,
                expected_region_versions,
                "OCR",
            )
            project = store.project(session)
            for target_id, result in results:
                current = session.get(TextRegion, target_id)
                assert current is not None
                before = region_payload(current)
                current.source_text = result.text
                current.confidence = result.confidence
                current.direction = result.direction
                current.ocr_provider = provider_name
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
                {"translation", "typeset", "export"},
            )
            status = dict(image.status)
            status["ocr"] = "done"
            status["ocrProvider"] = provider_name
            image.status = status
            image.processing_errors = [
                error for error in (image.processing_errors or []) if error.get("stage") != "ocr"
            ]
            image.revision += 1
        recognized = [result.to_dict() for _target_id, result in results]
        return {"provider": provider_name, "regions": recognized, "count": len(recognized)}

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
        targets = [region for region in page_regions if not region.ignored]
        if region_id:
            targets = [region for region in targets if region.id == region_id]
            if not targets:
                raise ProjectError("Translation region was not found or is ignored")
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
                    if not neighbor.ignored and neighbor.source_text:
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
            for target_id, value in translated:
                current = session.get(TextRegion, target_id)
                assert current is not None
                before = region_payload(current)
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
        return {
            "provider": provider_name,
            "regions": [
                {"regionId": target_id, "translation": value} for target_id, value in translated
            ],
            "count": len(translated),
        }

    @staticmethod
    def _png_bytes(image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _process_render(
        self,
        store: ProjectStore,
        image_id: str,
        options: dict[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Render image was not found")
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
            inpaint_is_current = image.status.get("inpaint") == "done"
        region_data = [region_payload(region) for region in regions if not region.ignored]
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
        typeset_source: Path | Image.Image = inpaint_path
        if kind != "typeset" or not inpaint_is_current or not inpaint_path.exists():
            mask = self.providers.inpainting.create_mask(
                source,
                [],
                padding=0,
                dilation=0,
                feather=0,
            )
            cleaned: Path | Image.Image = source
            for region in region_data:
                repair = region.get("repair") if isinstance(region.get("repair"), dict) else {}
                padding = int(options.get("padding", repair.get("maskPadding", 3)))
                dilation = int(options.get("dilation", repair.get("dilation", 1)))
                feather = int(options.get("feather", repair.get("feather", 0)))
                region_mask = self.providers.inpainting.create_mask(
                    source,
                    [{**region, "padding": padding}],
                    padding=padding,
                    dilation=dilation,
                    feather=feather,
                )
                mask = np.maximum(mask, region_mask)
                cleaned = self.providers.inpainting.inpaint(
                    cleaned,
                    region_mask,
                    radius=float(options.get("radius", repair.get("radius", 3))),
                    method=str(options.get("method", repair.get("method", "telea"))),
                    fill_color=str(options.get("fillColor", repair.get("fillColor", "#ffffff"))),
                )
            if not region_data:
                with Image.open(source) as opened:
                    cleaned = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
            mask_bytes = self._png_bytes(Image.fromarray(mask))
            inpainted_bytes = self._png_bytes(cleaned)
            typeset_source = cleaned
        if kind != "inpaint":
            result = typeset_image(typeset_source, region_data)
            typeset_bytes = self._png_bytes(result.image)
            layouts = result.layouts
        else:
            layouts = []
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
            if typeset_bytes is not None:
                atomic_write_bytes(typeset_path, typeset_bytes)
            invalidate_image_pipeline(
                store,
                current,
                {"typeset", "export"} if kind == "inpaint" else {"export"},
            )
            status = dict(current.status)
            status["inpaint"] = "done"
            status["inpaintingProvider"] = "opencv"
            if kind != "inpaint":
                status["typeset"] = "done"
                status["typesettingProvider"] = "pillow"
            current.status = status
            cleared_stages = {"render", "inpaint", "typeset"}
            current.processing_errors = [
                error
                for error in (current.processing_errors or [])
                if error.get("stage") not in cleared_stages
            ]
            current.revision += 1
        return {
            "inpaintedArtifact": inpaint_relative.as_posix(),
            "inpaintedUrl": f"/api/images/{image_id}/content?variant=erased",
            "maskArtifact": mask_relative.as_posix(),
            "maskUrl": f"/api/images/{image_id}/generated/mask",
            "typesetArtifact": typeset_relative.as_posix() if kind != "inpaint" else None,
            "typesetUrl": (
                f"/api/images/{image_id}/content?variant=typeset" if kind != "inpaint" else None
            ),
            "layouts": layouts,
            "overflowCount": sum(bool(layout["overflow"]) for layout in layouts),
        }
