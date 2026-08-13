from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.config import Settings
from manga_localizer.database import ImageAsset, ImportBoundary
from manga_localizer.security import (
    UnsafePathError,
    atomic_write_bytes,
    portable_path_key,
    resolve_within,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF"}


class InvalidImage(ProjectError):
    pass


class StageReviewObservationConflict(ProjectError):
    def __init__(
        self,
        message: str,
        *,
        resource: str,
        stage: str,
        mismatches: list[str],
    ):
        super().__init__(message)
        self.resource = resource
        self.stage = stage
        self.mismatches = mismatches


_PROVIDER_STATUS_KEYS = {
    "preprocess": "preprocessingProvider",
    "detection": "detectorProvider",
    "ocr": "ocrProvider",
    "translation": "translatorProvider",
    "inpaint": "inpaintingProvider",
    "typeset": "typesettingProvider",
}

_ERROR_STAGE_KEYS = {
    "preprocess": {"preprocess"},
    "detection": {"detect"},
    "ocr": {"ocr"},
    "translation": {"translate"},
    "inpaint": {"render", "inpaint"},
    "typeset": {"render", "typeset"},
    "export": {"export"},
}

REVIEW_STATES = {"pending", "reviewed", "no-text-reviewed"}
VISUAL_REVIEW_STAGES = {"preprocess", "inpaint", "typeset"}
VISUAL_REVIEW_STATES = {"accepted", "rejected"}
VISUAL_REVIEW_DEPENDENTS = {
    "preprocess": {"inpaint", "typeset"},
    "inpaint": {"typeset"},
    "typeset": set(),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _generated_stage_path(
    store: ProjectStore,
    image: ImageAsset,
    stage: str,
) -> Path:
    directory = {
        "preprocess": "preprocessed",
        "inpaint": "inpainted",
        "typeset": "typeset",
    }[stage]
    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    return resolve_write_target(
        store.root,
        Path("generated") / directory / relative,
        protected_roots=(store.source_root,),
    )


def _sha256_file(path: Path) -> str:
    try:
        if not path.is_file():
            raise ProjectError("Generated visual-stage artifact is missing; rerun the stage")
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as error:
        raise ProjectError(
            "Generated visual-stage artifact could not be read; rerun the stage"
        ) from error


def stage_artifact_checksums(
    store: ProjectStore,
    image: ImageAsset,
    stage: str,
) -> dict[str, str]:
    checksums = {"artifactChecksum": _sha256_file(_generated_stage_path(store, image, stage))}
    if stage == "inpaint":
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        mask_path = resolve_write_target(
            store.root,
            Path("generated") / "masks" / relative,
            protected_roots=(store.source_root,),
        )
        checksums["maskChecksum"] = _sha256_file(mask_path)
    return checksums


def stage_reviews(image: ImageAsset) -> dict[str, dict[str, str | int]]:
    """Normalize durable visual reviews while treating absent/legacy data as pending."""
    raw = (image.status or {}).get("stageReviews")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, str | int]] = {}
    for stage in VISUAL_REVIEW_STAGES:
        record = raw.get(stage)
        if not isinstance(record, dict) or record.get("state") not in VISUAL_REVIEW_STATES:
            continue
        reviewed_at = record.get("reviewedAt")
        result_revision = record.get("resultRevision")
        artifact_checksum = record.get("artifactChecksum")
        mask_checksum = record.get("maskChecksum")
        if (
            not isinstance(reviewed_at, str)
            or not reviewed_at
            or not isinstance(result_revision, int)
            or isinstance(result_revision, bool)
            or result_revision < 0
            or not isinstance(artifact_checksum, str)
            or not _SHA256_RE.fullmatch(artifact_checksum)
            or (
                stage == "inpaint"
                and (not isinstance(mask_checksum, str) or not _SHA256_RE.fullmatch(mask_checksum))
            )
        ):
            continue
        normalized[stage] = {
            "state": str(record["state"]),
            "reviewedAt": reviewed_at,
            "resultRevision": result_revision,
            "artifactChecksum": artifact_checksum,
        }
        if stage == "inpaint":
            normalized[stage]["maskChecksum"] = str(mask_checksum)
    return normalized


def clear_stage_reviews(image: ImageAsset, stages: set[str]) -> bool:
    reviews = stage_reviews(image)
    changed = False
    for stage in VISUAL_REVIEW_STAGES & stages:
        changed = reviews.pop(stage, None) is not None or changed
    status = dict(image.status or {})
    if reviews:
        status["stageReviews"] = reviews
    else:
        status.pop("stageReviews", None)
    image.status = status
    return changed


def reset_image_review(image: ImageAsset) -> bool:
    """Reset explicit page review without changing the owning entity revision."""
    status = dict(image.status or {})
    changed = status.get("reviewState", "pending") != "pending" or bool(status.get("reviewedAt"))
    status["reviewState"] = "pending"
    status["reviewedAt"] = ""
    image.status = status
    return changed


def _validate_image_review_state(image: ImageAsset, review_state: str) -> None:
    non_ignored_regions = [region for region in image.regions if not region.ignored]
    if review_state == "no-text-reviewed" and non_ignored_regions:
        raise ProjectError("Cannot mark image as no-text-reviewed while non-ignored regions remain")
    if review_state != "reviewed":
        return
    if not non_ignored_regions:
        raise ProjectError("Cannot mark image as reviewed without at least one non-ignored region")
    unconfirmed_count = sum(not region.confirmed for region in non_ignored_regions)
    if unconfirmed_count:
        raise ProjectError(
            "Cannot mark image as reviewed until every non-ignored region is confirmed "
            f"({unconfirmed_count} unconfirmed)"
        )


def invalidate_image_pipeline(
    store: ProjectStore,
    image: ImageAsset,
    stages: set[str],
) -> None:
    """Mark derived state stale and remove local artifacts that could be reused accidentally."""
    allowed = {
        "preprocess",
        "detection",
        "ocr",
        "translation",
        "inpaint",
        "typeset",
        "export",
    }
    if not stages <= allowed:
        raise ValueError("Unknown pipeline stage invalidation")
    if stages & {"detection", "ocr", "translation", "inpaint", "typeset"}:
        reset_image_review(image)
    clear_stage_reviews(image, stages)
    status = dict(image.status)
    for stage in stages:
        status[stage] = "pending"
        provider_key = _PROVIDER_STATUS_KEYS.get(stage)
        if provider_key:
            status[provider_key] = ""
    image.status = status
    invalid_error_stages = set().union(*(_ERROR_STAGE_KEYS[stage] for stage in stages))
    image.processing_errors = [
        error
        for error in (image.processing_errors or [])
        if error.get("stage") not in invalid_error_stages
    ]

    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    artifact_directories: set[str] = set()
    if "preprocess" in stages:
        artifact_directories.add("preprocessed")
    if "inpaint" in stages:
        artifact_directories.update(("inpainted", "masks", "typeset"))
    elif "typeset" in stages:
        artifact_directories.add("typeset")
    for directory in artifact_directories:
        relative_artifact = Path("generated") / directory / relative
        target = resolve_write_target(
            store.root,
            relative_artifact,
            protected_roots=(store.source_root,),
        )
        target.unlink(missing_ok=True)


def validate_image_bytes(data: bytes, settings: Settings) -> None:
    if len(data) > settings.max_upload_bytes:
        raise InvalidImage(f"Image exceeds the {settings.max_upload_bytes} byte upload limit")
    _inspect_image(data)


def _inspect_image(data: bytes) -> tuple[int, int, str, str]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            if image_format not in SUPPORTED_FORMATS:
                raise InvalidImage(f"Unsupported image format: {image_format or 'unknown'}")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise InvalidImage("Image dimensions must be positive")
            media_type = Image.MIME.get(image_format, "application/octet-stream")
            canonical_suffix = {
                "JPEG": ".jpg",
                "PNG": ".png",
                "WEBP": ".webp",
                "TIFF": ".tiff",
                "BMP": ".bmp",
                "GIF": ".gif",
            }[image_format]
            return width, height, media_type, canonical_suffix
    except (UnidentifiedImageError, OSError, ValueError) as error:
        if isinstance(error, InvalidImage):
            raise
        raise InvalidImage("The uploaded file is not a readable supported image") from error


def _unique_relative(store: ProjectStore, requested: Path) -> Path:
    def derived_key(path: str | Path) -> str:
        candidate = Path(path)
        return portable_path_key(candidate.with_suffix(".png"))

    with store.session() as session:
        existing_keys = {
            derived_key(path) for path in session.scalars(select(ImageAsset.relative_path)).all()
        }
    candidate = requested
    counter = 2
    while (
        derived_key(candidate) in existing_keys
        or resolve_within(store.source_root, candidate.as_posix()).exists()
    ):
        candidate = candidate.with_name(f"{requested.stem}-{counter}{requested.suffix}")
        counter += 1
    return candidate


def ingest_bytes(
    store: ProjectStore,
    settings: Settings,
    *,
    data: bytes,
    relative_path: str,
    source_kind: str = "browser-upload",
    input_path: str | None = None,
) -> ImageAsset:
    validate_image_bytes(data, settings)
    width, height, media_type, suffix = _inspect_image(data)
    requested = safe_relative_path(relative_path)
    if requested.suffix.lower() not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".tif",
        ".tiff",
        ".bmp",
        ".gif",
    }:
        requested = requested.with_suffix(suffix)
    checksum = hashlib.sha256(data).hexdigest()
    with store.lock:
        actual_relative = _unique_relative(store, requested)
        destination = resolve_write_target(store.source_root, actual_relative)
        atomic_write_bytes(destination, data)
        try:
            with store.session() as session:
                project = store.project(session)
                image = ImageAsset(
                    project_id=project.id,
                    name=actual_relative.name,
                    relative_path=actual_relative.as_posix(),
                    source_path=(Path("source") / actual_relative).as_posix(),
                    source_kind=source_kind,
                    input_path=input_path,
                    width=width,
                    height=height,
                    media_type=media_type,
                    checksum=checksum,
                )
                session.add(image)
                session.flush()
                add_revision(
                    session,
                    project,
                    entity_type="image",
                    entity_id=image.id,
                    operation="create",
                    before=None,
                    after={"relativePath": image.relative_path, "checksum": checksum},
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        store.write_snapshot()
    return image


def _iter_local_images(paths: Iterable[Path]) -> list[tuple[Path, Path]]:
    requested = list(paths)
    include_selected_root = len(requested) > 1
    candidates: list[tuple[Path, Path]] = []
    for raw in requested:
        path = raw.expanduser().resolve(strict=True)
        if raw.expanduser().is_symlink():
            raise UnsafePathError(f"Symlink imports are not allowed: {raw}")
        if path.is_file():
            candidates.append((path, Path(path.name)))
            continue
        if not path.is_dir():
            raise InvalidImage(f"Local import path is neither a file nor a directory: {path}")
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                continue
            if child.is_file():
                relative = child.relative_to(path)
                if include_selected_root:
                    relative = Path(path.name) / relative
                candidates.append((child, relative))
    return candidates


def import_local(
    store: ProjectStore,
    settings: Settings,
    paths: Iterable[Path],
) -> tuple[list[ImageAsset], list[dict[str, str]]]:
    requested_paths = list(paths)
    roots: list[Path] = []
    boundaries: list[tuple[Path, str]] = []
    for raw in requested_paths:
        expanded = raw.expanduser()
        if expanded.is_symlink():
            raise UnsafePathError(f"Symlink imports are not allowed: {raw}")
        resolved = expanded.resolve(strict=True)
        roots.append(resolved if resolved.is_dir() else resolved.parent)
        boundaries.append((resolved, "directory" if resolved.is_dir() else "file"))
    try:
        input_root: Path | None = Path(os.path.commonpath([str(path) for path in roots]))
    except ValueError:
        # Windows has no common path for selections spanning multiple drives. Exact persisted
        # ImportBoundary rows remain authoritative for write protection.
        input_root = None
    with store.session() as session:
        project = store.project(session)
        project.input_root = str(input_root) if input_root is not None else None
        existing = {
            (boundary.path, boundary.kind)
            for boundary in session.scalars(
                select(ImportBoundary).where(ImportBoundary.project_id == project.id)
            ).all()
        }
        for boundary, kind in boundaries:
            identity = (str(boundary), kind)
            if identity not in existing:
                session.add(ImportBoundary(project_id=project.id, path=str(boundary), kind=kind))
                existing.add(identity)
    imported: list[ImageAsset] = []
    failures: list[dict[str, str]] = []
    for source, relative in _iter_local_images(requested_paths):
        try:
            if source.stat().st_size > settings.max_upload_bytes:
                raise InvalidImage("File exceeds upload limit")
            imported.append(
                ingest_bytes(
                    store,
                    settings,
                    data=source.read_bytes(),
                    relative_path=relative.as_posix(),
                    source_kind="trusted-local-import",
                    input_path=str(source),
                )
            )
        except (InvalidImage, UnsafePathError, OSError) as error:
            failures.append({"path": str(source), "error": str(error)})
    return imported, failures


def image_path(store: ProjectStore, image: ImageAsset) -> Path:
    relative = safe_relative_path(image.source_path)
    if not relative.parts or relative.parts[0] != "source":
        raise UnsafePathError("Image source path is outside immutable source storage")
    source = resolve_write_target(store.root, relative)
    if not source.is_file():
        raise InvalidImage("Immutable source image is missing")
    return source


def thumbnail_path(store: ProjectStore, image: ImageAsset, size: int) -> Path:
    target = resolve_write_target(
        store.root,
        Path("project") / "cache" / "thumbnails" / f"{image.id}.jpg",
        protected_roots=(store.source_root,),
    )
    source = image_path(store, image)
    if target.exists() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as picture:
        picture.seek(0)
        thumbnail = picture.convert("RGB")
        thumbnail.thumbnail((size, size), Image.Resampling.LANCZOS)
        encoded = io.BytesIO()
        thumbnail.save(encoded, format="JPEG", quality=84, optimize=True)
        atomic_write_bytes(target, encoded.getvalue())
    return target


def list_images(store: ProjectStore) -> list[ImageAsset]:
    with store.session() as session:
        return list(
            session.scalars(
                select(ImageAsset)
                .options(selectinload(ImageAsset.regions))
                .order_by(ImageAsset.relative_path, ImageAsset.created_at)
            ).all()
        )


def review_image(
    store: ProjectStore,
    image_id: str,
    *,
    review_state: str,
    expected_revision: int,
) -> ImageAsset:
    if review_state not in REVIEW_STATES:
        raise ProjectError("Unknown image review state")
    with store.session() as session:
        image = session.scalar(
            select(ImageAsset)
            .options(selectinload(ImageAsset.regions))
            .where(ImageAsset.id == image_id)
        )
        if image is None:
            raise ProjectError("Image was not found in this project")
        if image.revision != expected_revision:
            raise RevisionConflict(
                f"Image revision is {image.revision}, expected {expected_revision}",
                expected_revision=expected_revision,
                actual_revision=image.revision,
                resource=f"image:{image.id}",
            )
        _validate_image_review_state(image, review_state)
        project = store.project(session)
        before = {
            "reviewState": image.status.get("reviewState", "pending"),
            "reviewedAt": image.status.get("reviewedAt") or "",
        }
        status = dict(image.status or {})
        status["reviewState"] = review_state
        status["reviewedAt"] = "" if review_state == "pending" else datetime.now(UTC).isoformat()
        status["export"] = "pending"
        image.status = status
        image.processing_errors = [
            error for error in (image.processing_errors or []) if error.get("stage") != "export"
        ]
        image.revision += 1
        session.flush()
        add_revision(
            session,
            project,
            entity_type="image",
            entity_id=image.id,
            operation="review",
            before=before,
            after={
                "reviewState": status["reviewState"],
                "reviewedAt": status["reviewedAt"],
            },
        )
    store.write_snapshot()
    return image


def review_image_stage(
    store: ProjectStore,
    image_id: str,
    *,
    stage: str,
    state: str,
    expected_revision: int,
    observed_artifact_checksum: str | None = None,
    observed_mask_checksum: str | None = None,
) -> ImageAsset:
    if stage not in VISUAL_REVIEW_STAGES:
        raise ProjectError("Visual review stage must be preprocess, inpaint, or typeset")
    if state not in {"pending", *VISUAL_REVIEW_STATES}:
        raise ProjectError("Visual review state must be pending, accepted, or rejected")
    with store.lock:
        with store.session() as session:
            image = session.scalar(
                select(ImageAsset)
                .options(selectinload(ImageAsset.regions))
                .where(ImageAsset.id == image_id)
            )
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    f"Image revision is {image.revision}, expected {expected_revision}",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            if state != "pending" and image.status.get(stage) != "done":
                raise ProjectError(f"Cannot review {stage} output until that stage is done")
            if state == "pending":
                if observed_artifact_checksum is not None or observed_mask_checksum is not None:
                    raise ProjectError("Pending visual reviews cannot include observed checksums")
                checksums: dict[str, str] = {}
            else:
                if observed_artifact_checksum is None:
                    raise ProjectError(
                        "Accepted and rejected reviews require an observed artifact checksum"
                    )
                if stage == "inpaint" and observed_mask_checksum is None:
                    raise ProjectError("Inpaint reviews require an observed mask checksum")
                if stage != "inpaint" and observed_mask_checksum is not None:
                    raise ProjectError(f"{stage} reviews cannot include an observed mask checksum")
                checksums = stage_artifact_checksums(store, image, stage)
                observed = {"artifactChecksum": observed_artifact_checksum}
                if stage == "inpaint":
                    observed["maskChecksum"] = observed_mask_checksum
                mismatches = [key for key, value in observed.items() if value != checksums.get(key)]
                if mismatches:
                    raise StageReviewObservationConflict(
                        "The reviewed visual no longer matches the current stage output",
                        resource=f"image:{image.id}",
                        stage=stage,
                        mismatches=mismatches,
                    )
            project = store.project(session)
            reviews = stage_reviews(image)
            dependent_reviews = {
                dependent: reviews[dependent]
                for dependent in VISUAL_REVIEW_DEPENDENTS[stage]
                if dependent in reviews
            }
            before = {
                "stage": stage,
                "review": reviews.get(stage),
            }
            if state == "pending":
                reviews.pop(stage, None)
                after = None
                artifact_changed = False
            else:
                previous_review = reviews.get(stage)
                artifact_changed = previous_review is None or any(
                    previous_review.get(key) != value for key, value in checksums.items()
                )
                after = {
                    "state": state,
                    "reviewedAt": datetime.now(UTC).isoformat(),
                    "resultRevision": image.revision,
                    **checksums,
                }
                reviews[stage] = after
            clear_dependents = state != "accepted" or artifact_changed
            cleared_dependents = dependent_reviews if clear_dependents else {}
            if clear_dependents:
                for dependent in cleared_dependents:
                    reviews.pop(dependent, None)
            before["clearedDependents"] = cleared_dependents
            status = dict(image.status or {})
            if reviews:
                status["stageReviews"] = reviews
            else:
                status.pop("stageReviews", None)
            status["export"] = "pending"
            image.status = status
            image.processing_errors = [
                error for error in (image.processing_errors or []) if error.get("stage") != "export"
            ]
            image.revision += 1
            session.flush()
            add_revision(
                session,
                project,
                entity_type="image",
                entity_id=image.id,
                operation="stage-review",
                before=before,
                after={
                    "stage": stage,
                    "review": after,
                    "clearedDependents": sorted(cleared_dependents),
                },
            )
        store.write_snapshot()
    return image


def copy_file_without_overwrite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
