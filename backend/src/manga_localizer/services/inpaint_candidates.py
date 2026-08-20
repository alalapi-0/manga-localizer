from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.database import ImageAsset
from manga_localizer.imaging.lineart_inpaint import (
    CANDIDATE_IDS,
    CANDIDATE_PRIMARY,
    build_inpaint_candidates,
    choose_default_candidate,
    public_candidate_records,
)
from manga_localizer.security import atomic_write_bytes, resolve_write_target, safe_relative_path
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)

CANDIDATE_ROOT = Path("generated") / "inpaint-candidates"


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _page_key(relative: Path) -> Path:
    return relative.with_suffix("")


def candidate_manifest_path(store: ProjectStore, relative: Path) -> Path:
    return resolve_write_target(
        store.root,
        CANDIDATE_ROOT / _page_key(relative) / "manifest.json",
        protected_roots=(store.source_root,),
    )


def candidate_image_path(store: ProjectStore, relative: Path, candidate_id: str) -> Path:
    if candidate_id not in CANDIDATE_IDS:
        raise ProjectError("Unknown inpainting candidate")
    return resolve_write_target(
        store.root,
        CANDIDATE_ROOT / _page_key(relative) / f"{candidate_id}.png",
        protected_roots=(store.source_root,),
    )


def delete_inpaint_candidate_files(store: ProjectStore, relative: Path) -> None:
    directory = candidate_manifest_path(store, relative).parent
    if not directory.exists():
        return
    allowed = (store.root.expanduser().resolve() / CANDIDATE_ROOT).resolve()
    resolved = directory.resolve()
    if not resolved.is_relative_to(allowed):
        raise ProjectError("Inpainting candidate path is outside generated storage")
    if resolved.is_dir():
        shutil.rmtree(resolved)
        return
    resolved.unlink(missing_ok=True)


def prepare_page_inpaint_candidates(
    *,
    source: Image.Image,
    mask: np.ndarray,
    primary: Image.Image,
    used_only_lama: bool,
    radius: float = 3.0,
    full_context: Image.Image | None = None,
) -> tuple[str | None, bytes, list[dict[str, Any]], list[tuple[str, bytes]], list[dict[str, Any]]]:
    primary_bytes = _png_bytes(primary)
    if not np.any(mask):
        return None, primary_bytes, [], [], []
    built = build_inpaint_candidates(
        source,
        mask,
        primary,
        radius=radius,
        full_context=full_context,
    )
    if not built:
        return None, primary_bytes, [], [], []
    selected_id = choose_default_candidate(built, used_only_lama=used_only_lama)
    selected_bytes = primary_bytes
    encoded_files: list[tuple[str, bytes]] = []
    manifest_candidates: list[dict[str, Any]] = []
    for item in built:
        candidate_id = str(item["id"])
        encoded = _png_bytes(item["image"])
        encoded_files.append((candidate_id, encoded))
        if candidate_id == selected_id and candidate_id != CANDIDATE_PRIMARY:
            selected_bytes = encoded
        manifest_candidates.append(
            {
                "id": candidate_id,
                "label": item["label"],
                "changedPixelsOutsideMask": item["changedPixelsOutsideMask"],
                "meanAbsDeltaInsideMask": item["meanAbsDeltaInsideMask"],
                "chromaInsideMask": item["chromaInsideMask"],
                "anomalies": list(item["anomalies"]),
            }
        )
    return (
        selected_id,
        selected_bytes,
        public_candidate_records(manifest_candidates),
        encoded_files,
        manifest_candidates,
    )


def write_page_inpaint_candidates(
    store: ProjectStore,
    relative: Path,
    *,
    selected_id: str,
    encoded_files: list[tuple[str, bytes]],
    manifest_candidates: list[dict[str, Any]],
) -> None:
    delete_inpaint_candidate_files(store, relative)
    for candidate_id, encoded in encoded_files:
        atomic_write_bytes(candidate_image_path(store, relative, candidate_id), encoded)
    atomic_write_bytes(
        candidate_manifest_path(store, relative),
        json.dumps(
            {"selectedId": selected_id, "candidates": manifest_candidates},
            ensure_ascii=True,
            indent=2,
        ).encode("utf-8"),
    )


def public_candidates_from_status(
    status: dict[str, Any] | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    payload = status or {}
    selected = payload.get("inpaintCandidate")
    selected_id = selected if isinstance(selected, str) and selected in CANDIDATE_IDS else None
    raw_candidates = payload.get("inpaintCandidates")
    return selected_id, public_candidate_records(
        raw_candidates if isinstance(raw_candidates, list) else []
    )


def select_inpaint_candidate(
    store: ProjectStore,
    image_id: str,
    *,
    candidate_id: str,
    expected_revision: int,
) -> ImageAsset:
    from manga_localizer.services.images import (
        clear_stage_reviews,
        invalidate_image_pipeline,
        reset_image_review,
    )

    if candidate_id not in CANDIDATE_IDS:
        raise ProjectError("Unknown inpainting candidate")
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
            if image.status.get("inpaint") != "done":
                raise ProjectError("Cannot select an inpainting candidate until inpainting is done")
            current_id, records = public_candidates_from_status(image.status)
            if not records:
                raise ProjectError("No inpainting candidates are available for this page")
            if candidate_id not in {item["id"] for item in records}:
                raise ProjectError("The requested inpainting candidate is not available")
            if candidate_id == current_id:
                return image
            relative = safe_relative_path(image.relative_path).with_suffix(".png")
            source = candidate_image_path(store, relative, candidate_id)
            if not source.is_file():
                raise ProjectError(
                    "The requested inpainting candidate file is missing; rerun inpainting"
                )
            destination = resolve_write_target(
                store.root,
                Path("generated") / "inpainted" / relative,
                protected_roots=(store.source_root,),
            )
            project = store.project(session)
            before = {"inpaintCandidate": current_id}
            selected_bytes = source.read_bytes()
            atomic_write_bytes(destination, selected_bytes)
            invalidate_image_pipeline(store, image, {"typeset", "export"})
            reset_image_review(image)
            clear_stage_reviews(image, {"inpaint", "typeset"})
            status = dict(image.status or {})
            status["inpaint"] = "done"
            status["inpaintCandidate"] = candidate_id
            status["inpaintCandidates"] = records
            image.status = status
            image.revision += 1
            session.flush()
            add_revision(
                session,
                project,
                entity_type="image",
                entity_id=image.id,
                operation="inpaint-candidate",
                before=before,
                after={"inpaintCandidate": candidate_id},
            )
        store.write_snapshot()
    return image
