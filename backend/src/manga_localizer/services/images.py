from __future__ import annotations

import hashlib
import io
import os
import shutil
from collections.abc import Iterable
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
from manga_localizer.services.projects import ProjectError, ProjectStore, add_revision

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF"}


class InvalidImage(ProjectError):
    pass


_PROVIDER_STATUS_KEYS = {
    "detection": "detectorProvider",
    "ocr": "ocrProvider",
    "translation": "translatorProvider",
    "inpaint": "inpaintingProvider",
    "typeset": "typesettingProvider",
}

_ERROR_STAGE_KEYS = {
    "detection": {"detect"},
    "ocr": {"ocr"},
    "translation": {"translate"},
    "inpaint": {"render", "inpaint"},
    "typeset": {"render", "typeset"},
    "export": {"export"},
}


def invalidate_image_pipeline(
    store: ProjectStore,
    image: ImageAsset,
    stages: set[str],
) -> None:
    """Mark derived state stale and remove local artifacts that could be reused accidentally."""
    allowed = {"detection", "ocr", "translation", "inpaint", "typeset", "export"}
    if not stages <= allowed:
        raise ValueError("Unknown pipeline stage invalidation")
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


def copy_file_without_overwrite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
