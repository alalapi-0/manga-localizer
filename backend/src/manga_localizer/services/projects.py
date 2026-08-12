from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload, sessionmaker

from manga_localizer.config import Settings
from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    JobStatus,
    Project,
    Revision,
    TextRegion,
    create_project_engine,
)
from manga_localizer.logging_utils import redact, without_secrets
from manga_localizer.security import (
    UnsafePathError,
    UnsafeRemoteEndpointError,
    atomic_write_bytes,
    normalize_remote_endpoints,
    resolve_write_target,
)


class ProjectError(RuntimeError):
    pass


class ProjectNotFound(ProjectError):
    pass


class RevisionConflict(ProjectError):
    def __init__(
        self,
        message: str,
        *,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
        resource: str | None = None,
    ):
        super().__init__(message)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.resource = resource


DEFAULT_PROJECT_SETTINGS: dict[str, Any] = {
    "sourceLanguage": "ja",
    "targetLanguage": "zh-CN",
    "targetScript": "simplified",
    "preprocessorProvider": "opencv-pillow",
    "preprocessing": {
        "profile": "ocr-friendly",
        "enableUpscale": True,
        "upscaleFactor": 2,
        "enableDenoise": True,
        "enableSharpen": True,
        "enableContrastEnhance": True,
        "enableEdgeOptimize": False,
        "enableBinarize": False,
        "threshold": 180,
    },
    "detectorProvider": "tesseract",
    "ocrProvider": "tesseract",
    "translatorProvider": "manual",
    "inpainterProvider": "opencv",
    "typesetterProvider": "pillow",
    "glossary": {},
    "characterNames": {},
    "export": {
        "format": "both",
        "imageVariant": "typeset",
        "conflict": "rename",
        "preserveTree": True,
        "translatedDirectory": "translated",
        "cleanDirectory": "clean",
        "originalTextDirectory": "original-text",
        "translatedTextDirectory": "translated-text",
        "maskDirectory": "masks",
    },
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-.")
    return slug[:80] or "project"


def _safe_settings(
    settings: dict[str, Any],
    *,
    drop_invalid_remote_endpoints: bool = False,
) -> dict[str, Any]:
    sanitized = without_secrets(settings)
    try:
        sanitized = normalize_remote_endpoints(
            sanitized,
            drop_invalid=drop_invalid_remote_endpoints,
        )
    except UnsafeRemoteEndpointError as error:
        raise ProjectError(str(error)) from None
    return sanitized if isinstance(sanitized, dict) else {}


def settings_with_defaults(
    settings: dict[str, Any] | None,
    *,
    base: dict[str, Any] | None = None,
    drop_invalid_remote_endpoints: bool = False,
) -> dict[str, Any]:
    def merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = value
        return result

    merged = merge(
        DEFAULT_PROJECT_SETTINGS,
        _safe_settings(
            base or {},
            drop_invalid_remote_endpoints=drop_invalid_remote_endpoints,
        ),
    )
    return merge(
        merged,
        _safe_settings(
            settings or {},
            drop_invalid_remote_endpoints=drop_invalid_remote_endpoints,
        ),
    )


class ProjectStore:
    def __init__(self, root: Path, engine: Engine):
        self.root = root.resolve()
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.lock = threading.RLock()

    @property
    def database_path(self) -> Path:
        return resolve_write_target(self.root, "project/project.sqlite3")

    @property
    def manifest_path(self) -> Path:
        return resolve_write_target(self.root, "project/project.json")

    @property
    def source_root(self) -> Path:
        return resolve_write_target(self.root, "source")

    @property
    def generated_root(self) -> Path:
        return resolve_write_target(self.root, "generated")

    @property
    def export_root(self) -> Path:
        return resolve_write_target(self.root, "exports")

    @property
    def cache_root(self) -> Path:
        return resolve_write_target(self.root, "project/cache")

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.lock, self.sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def project(self, session: Session | None = None) -> Project:
        if session is not None:
            project = session.scalar(select(Project).limit(1))
            if project is None:
                raise ProjectNotFound("Portable project database has no project record")
            return project
        with self.session() as own_session:
            project = own_session.scalar(select(Project).limit(1))
            if project is None:
                raise ProjectNotFound("Portable project database has no project record")
            return project

    def write_snapshot(self) -> None:
        with self.session() as session:
            project = session.scalar(
                select(Project)
                .options(
                    selectinload(Project.images).selectinload(ImageAsset.regions),
                    selectinload(Project.jobs).selectinload(Job.items),
                )
                .limit(1)
            )
            if project is None:
                raise ProjectNotFound("Portable project database has no project record")
            payload = {
                "formatVersion": 1,
                "project": {
                    "id": project.id,
                    "name": project.name,
                    "schemaVersion": project.schema_version,
                    "revision": project.revision,
                    "settings": _safe_settings(project.settings),
                    "createdAt": project.created_at.isoformat(),
                    "updatedAt": project.updated_at.isoformat(),
                },
                "images": [
                    {
                        "id": image.id,
                        "name": image.name,
                        "relativePath": image.relative_path,
                        "sourceKind": image.source_kind,
                        "width": image.width,
                        "height": image.height,
                        "mediaType": image.media_type,
                        "checksum": image.checksum,
                        "status": {
                            key: image.status.get(key, "pending")
                            for key in (
                                "preprocess",
                                "detection",
                                "ocr",
                                "translation",
                                "inpaint",
                                "typeset",
                                "export",
                            )
                        }
                        | {
                            "reviewState": image.status.get("reviewState", "pending"),
                            "reviewedAt": image.status.get("reviewedAt") or "",
                        },
                        "providers": {
                            "preprocessing": image.status.get("preprocessingProvider") or None,
                            "detector": image.status.get("detectorProvider") or None,
                            "ocr": image.status.get("ocrProvider") or None,
                            "translator": image.status.get("translatorProvider") or None,
                            "inpainting": image.status.get("inpaintingProvider") or None,
                            "typesetting": image.status.get("typesettingProvider") or None,
                        },
                        "processingErrors": image.processing_errors,
                        "revision": image.revision,
                        "regions": [region_payload(region) for region in image.regions],
                    }
                    for image in project.images
                ],
                "jobs": [
                    {
                        "id": job.id,
                        "kind": job.kind,
                        "status": job.status,
                        "progress": job.progress,
                        "total": job.total,
                        "completed": job.completed,
                        "error": redact(job.error) if job.error else None,
                        "items": [
                            {
                                "id": item.id,
                                "imageId": item.image_id,
                                "regionId": item.region_id,
                                "position": item.position,
                                "status": item.status,
                                "progress": item.progress,
                                "error": redact(item.error) if item.error else None,
                            }
                            for item in job.items
                        ],
                    }
                    for job in project.jobs
                ],
            }
        atomic_write_bytes(
            self.manifest_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def recover_jobs(self) -> int:
        with self.session() as session:
            running_item_rows = list(
                session.execute(
                    select(JobItem, Job.status)
                    .join(Job, Job.id == JobItem.job_id)
                    .where(JobItem.status == JobStatus.RUNNING.value)
                ).all()
            )
            item_job_ids = {item.job_id for item, _job_status in running_item_rows}
            running_job_ids = set(
                session.scalars(select(Job.id).where(Job.status == JobStatus.RUNNING.value)).all()
            )
            incomplete_bundle_jobs = list(
                session.scalars(
                    select(Job).where(
                        Job.kind == "export",
                        Job.status == JobStatus.COMPLETED.value,
                    )
                ).all()
            )
            incomplete_bundle_job_ids = {
                job.id
                for job in incomplete_bundle_jobs
                if job.options.get("bundleFinalized") is not True
            }
            now = datetime.now(UTC)
            for item, parent_status in running_item_rows:
                item.error = None
                item.progress = 0.0
                if parent_status == JobStatus.CANCELLED.value:
                    item.status = JobStatus.CANCELLED.value
                    item.finished_at = now
                else:
                    item.status = JobStatus.QUEUED.value
                    item.started_at = None
                    item.finished_at = None
            session.execute(
                update(Job)
                .where(Job.status == JobStatus.RUNNING.value)
                .values(status=JobStatus.QUEUED.value, error=None)
            )
            if incomplete_bundle_job_ids:
                session.execute(
                    update(Job)
                    .where(Job.id.in_(incomplete_bundle_job_ids))
                    .values(status=JobStatus.QUEUED.value, error=None)
                )
            recovered = len(item_job_ids | running_job_ids | incomplete_bundle_job_ids)
        if recovered:
            self.write_snapshot()
        return recovered


def region_payload(region: TextRegion) -> dict[str, Any]:
    return {
        "id": region.id,
        "imageId": region.image_id,
        "x": region.x,
        "y": region.y,
        "width": region.width,
        "height": region.height,
        "rotation": region.rotation,
        "sourceText": region.source_text,
        "translationText": region.translation_text,
        "type": region.region_type,
        "direction": region.direction,
        "order": region.reading_order,
        "confidence": region.confidence,
        "ignored": region.ignored,
        "confirmed": region.confirmed,
        "style": region.style,
        "repair": region.repair,
        "ocrProvider": region.ocr_provider,
        "translationProvider": region.translation_provider,
        "revision": region.revision,
    }


def add_revision(
    session: Session,
    project: Project,
    *,
    entity_type: str,
    entity_id: str,
    operation: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    project.revision += 1
    project.updated_at = datetime.now(UTC)
    session.add(
        Revision(
            project_id=project.id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            before=before,
            after=after,
            project_revision=project.revision,
        )
    )


class ProjectRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, ProjectStore] = {}
        self._lock = threading.RLock()

    def _catalog_entries(self) -> list[dict[str, str]]:
        try:
            raw = json.loads(self.settings.catalog_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return raw if isinstance(raw, list) else []

    def _save_catalog(self) -> None:
        entries = [
            {
                "projectId": project_id,
                "manifestPath": str(store.manifest_path),
                "openedAt": datetime.now(UTC).isoformat(),
            }
            for project_id, store in sorted(self._stores.items())
        ]
        atomic_write_bytes(
            self.settings.catalog_path,
            json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_catalog(self) -> None:
        for entry in self._catalog_entries():
            manifest = entry.get("manifestPath")
            if not manifest:
                continue
            try:
                self.open(Path(manifest), remember=False)
            except (ProjectError, OSError, ValueError, json.JSONDecodeError):
                continue
        self._save_catalog()

    def create(
        self,
        name: str,
        output_path: Path | None,
        settings: dict[str, Any] | None = None,
    ) -> tuple[ProjectStore, Project]:
        validated_settings = settings_with_defaults(settings)
        if output_path is None:
            root = self.settings.data_dir / "projects" / _slug(name)
            counter = 2
            while (root / "project" / "project.sqlite3").exists():
                root = self.settings.data_dir / "projects" / f"{_slug(name)}-{counter}"
                counter += 1
        else:
            root = output_path.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        reserved_directories = tuple(
            root / name for name in ("project", "source", "generated", "exports")
        )
        for directory in reserved_directories:
            if directory.is_symlink():
                raise UnsafePathError("Project reserved directories must not be symlinks")
            if directory.exists() and not directory.is_dir():
                raise ProjectError("A project reserved path is not a directory")
            if directory.exists() and any(directory.iterdir()):
                raise ProjectError("Project reserved directories must be empty for a new project")
        database_path = root / "project" / "project.sqlite3"
        manifest_path = root / "project" / "project.json"
        if database_path.exists() or manifest_path.exists():
            raise ProjectError("A project already exists at the selected output path")
        for directory in reserved_directories:
            directory.mkdir(parents=True, exist_ok=True)
        engine = create_project_engine(database_path)
        store = ProjectStore(root, engine)
        with store.session() as session:
            project = Project(
                name=name,
                root_path=str(root),
                settings=validated_settings,
                schema_version=1,
            )
            session.add(project)
            session.flush()
        with self._lock:
            self._stores[project.id] = store
            self._save_catalog()
        store.write_snapshot()
        return store, project

    def open(self, manifest_path: Path, *, remember: bool = True) -> tuple[ProjectStore, Project]:
        manifest_entry = manifest_path.expanduser()
        if manifest_entry.is_symlink() or manifest_entry.parent.is_symlink():
            raise UnsafePathError("Project manifest and project directory must not be symlinks")
        manifest_path = manifest_entry.resolve(strict=True)
        if manifest_path.name != "project.json" or manifest_path.parent.name != "project":
            raise ProjectError("Expected a portable project/project.json manifest")
        payload = json.loads(manifest_path.read_text("utf-8"))
        project_id = payload.get("project", {}).get("id")
        if not isinstance(project_id, str):
            raise ProjectError("Project manifest is missing a valid project id")
        root = manifest_path.parent.parent.resolve()
        database_entry = root / "project" / "project.sqlite3"
        if database_entry.is_symlink():
            raise UnsafePathError("Portable project database must not be a symlink")
        database_path = resolve_write_target(root, "project/project.sqlite3")
        if not database_path.is_file():
            raise ProjectError("Portable project database is missing")
        store = ProjectStore(root, create_project_engine(database_path))
        project = store.project()
        if project.id != project_id:
            raise ProjectError("Manifest and database project ids do not match")
        with store.session() as session:
            current = store.project(session)
            current.settings = settings_with_defaults(
                None,
                base=current.settings,
                drop_invalid_remote_endpoints=True,
            )
            for revision in session.scalars(select(Revision)).all():
                revision.before = redact(
                    normalize_remote_endpoints(without_secrets(revision.before), drop_invalid=True)
                )
                revision.after = redact(
                    normalize_remote_endpoints(without_secrets(revision.after), drop_invalid=True)
                )
            for image in session.scalars(select(ImageAsset)).all():
                image.processing_errors = redact(image.processing_errors)
            for job in session.scalars(select(Job).options(selectinload(Job.items))).all():
                job.options = normalize_remote_endpoints(
                    without_secrets(job.options),
                    drop_invalid=True,
                )
                job.error = redact(job.error) if job.error else None
                for item in job.items:
                    item.output = without_secrets(item.output)
                    item.error = redact(item.error) if item.error else None
        store.write_snapshot()
        project = store.project()
        with self._lock:
            self._stores[project.id] = store
            if remember:
                self._save_catalog()
        return store, project

    def list(self) -> list[Project]:
        projects: list[Project] = []
        for store in self.stores():
            try:
                projects.append(store.project())
            except ProjectNotFound:
                continue
        return sorted(projects, key=lambda item: item.updated_at, reverse=True)

    def stores(self) -> list[ProjectStore]:
        with self._lock:
            return list(self._stores.values())

    def get(self, project_id: str) -> ProjectStore:
        with self._lock:
            store = self._stores.get(project_id)
        if store is None:
            raise ProjectNotFound(f"Project {project_id} is not open")
        return store

    def find_image(self, image_id: str) -> tuple[ProjectStore, ImageAsset]:
        for store in self.stores():
            with store.session() as session:
                image = session.get(ImageAsset, image_id)
                if image is not None:
                    return store, image
        raise ProjectNotFound(f"Image {image_id} was not found in an open project")

    def find_region(self, region_id: str) -> tuple[ProjectStore, TextRegion]:
        for store in self.stores():
            with store.session() as session:
                region = session.get(TextRegion, region_id)
                if region is not None:
                    return store, region
        raise ProjectNotFound(f"Region {region_id} was not found in an open project")

    def find_job(self, job_id: str) -> tuple[ProjectStore, Job]:
        for store in self.stores():
            with store.session() as session:
                job = session.scalar(
                    select(Job).options(selectinload(Job.items)).where(Job.id == job_id)
                )
                if job is not None:
                    return store, job
        raise ProjectNotFound(f"Job {job_id} was not found in an open project")


def public_root(root: Path) -> str:
    # The local UI needs this for trusted-path workflows. It never enters snapshots.
    return str(root)


def safe_error(error: Exception) -> str:
    return str(redact(str(error)))[:1000]
