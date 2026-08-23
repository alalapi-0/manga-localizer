from __future__ import annotations

import hashlib
import io
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.database import ImageAsset
from manga_localizer.imaging.lineart_inpaint import (
    CANDIDATE_AI_OVERVIEW_LINEART,
    CANDIDATE_IDS,
    CANDIDATE_LAMA_COMPONENTS,
    CANDIDATE_LAMA_FULL_CONTEXT,
    CANDIDATE_LAMA_OVERVIEW_REFINE,
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
AI_PROVIDER_IDS = frozenset({"lama", "lama-onnx"})
INTERNAL_OVERVIEW_BASE_ID = "overview-base-v1"
INTERNAL_RECORD_KIND = "internal-base"
DERIVED_TRANSFORMS = frozenset({("manga-overview-lineart-cleanup", 1)})
VALID_CANDIDATE_ORIGINS = frozenset(
    {"direct-ai", "ai-derived", "classical", "deterministic-postprocess", "mixed"}
)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _page_key(relative: Path) -> Path:
    return relative.with_suffix("")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def inpaint_candidate_manifest_digest(
    *,
    generation_id: str,
    mask_checksum: str,
    candidates: list[dict[str, Any]],
    internal_bases: list[dict[str, Any]] | None = None,
) -> str:
    """Digest immutable candidate evidence; ``selectedId`` is intentionally mutable."""
    if not generation_id or not _is_sha256(mask_checksum):
        raise ProjectError("Inpainting candidate evidence is invalid")
    public_candidates, transported_bases = _partition_manifest_records(candidates)
    bases = list(internal_bases) if internal_bases is not None else transported_bases
    version = 2 if bases else 1
    encoded = json.dumps(
        {
            "version": version,
            "generationId": generation_id,
            "maskChecksum": mask_checksum,
            "candidates": public_candidates,
            **({"internalBases": bases} if bases else {}),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _partition_manifest_records(
    records: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    internal_bases: list[dict[str, Any]] = []
    for record in records:
        if isinstance(record, dict) and record.get("kind") == INTERNAL_RECORD_KIND:
            internal_bases.append(record)
        else:
            candidates.append(record)
    return candidates, internal_bases


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


def _internal_base_path(store: ProjectStore, relative: Path, base_id: str) -> Path:
    if base_id != INTERNAL_OVERVIEW_BASE_ID:
        raise ProjectError("Unknown internal inpainting derivation base")
    return resolve_write_target(
        store.root,
        CANDIDATE_ROOT / _page_key(relative) / ".internal" / f"{base_id}.png",
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
    render_scale: int = 1,
    full_context: Image.Image | None = None,
    component_context: Image.Image | None = None,
    overview_base: Image.Image | None = None,
    overview_refine: Image.Image | None = None,
    primary_provider_ids: Sequence[str] = (),
    full_context_provider_id: str | None = None,
    component_context_provider_id: str | None = None,
    overview_provider_id: str | None = None,
    overview_refine_provider_id: str | None = None,
) -> tuple[str | None, bytes, list[dict[str, Any]], list[tuple[str, bytes]], list[dict[str, Any]]]:
    primary_bytes = _png_bytes(primary)
    if not np.any(mask):
        return None, primary_bytes, [], [], []
    built = build_inpaint_candidates(
        source,
        mask,
        primary,
        radius=radius,
        render_scale=render_scale,
        full_context=full_context,
        component_context=component_context,
        overview_base=overview_base,
        overview_refine=overview_refine,
    )
    if not built:
        return None, primary_bytes, [], [], []
    selected_id = choose_default_candidate(built, used_only_lama=used_only_lama)
    selected_bytes = primary_bytes
    encoded_files: list[tuple[str, bytes]] = []
    manifest_candidates: list[dict[str, Any]] = []
    overview_base_bytes = _png_bytes(overview_base) if overview_base is not None else None
    normalized_primary_providers = sorted(set(primary_provider_ids))
    if normalized_primary_providers and all(
        provider_id in AI_PROVIDER_IDS for provider_id in normalized_primary_providers
    ):
        primary_origin = "direct-ai"
    elif normalized_primary_providers and any(
        provider_id in AI_PROVIDER_IDS for provider_id in normalized_primary_providers
    ):
        primary_origin = "mixed"
    else:
        primary_origin = "classical"
    for item in built:
        candidate_id = str(item["id"])
        encoded = _png_bytes(item["image"])
        encoded_files.append((candidate_id, encoded))
        if candidate_id == selected_id and candidate_id != CANDIDATE_PRIMARY:
            selected_bytes = encoded
        if candidate_id == CANDIDATE_PRIMARY:
            origin_kind = primary_origin
            provider_ids = normalized_primary_providers
        elif candidate_id in {
            CANDIDATE_LAMA_COMPONENTS,
            CANDIDATE_LAMA_FULL_CONTEXT,
            CANDIDATE_LAMA_OVERVIEW_REFINE,
        }:
            origin_kind = "direct-ai"
            provider_ids = [
                {
                    CANDIDATE_LAMA_COMPONENTS: component_context_provider_id,
                    CANDIDATE_LAMA_FULL_CONTEXT: full_context_provider_id,
                    CANDIDATE_LAMA_OVERVIEW_REFINE: overview_refine_provider_id,
                }[candidate_id]
            ]
        elif candidate_id == "ai-manga-clean":
            origin_kind = "deterministic-postprocess"
            provider_ids = (
                [full_context_provider_id]
                if full_context_provider_id is not None
                else normalized_primary_providers
            )
        elif candidate_id == CANDIDATE_AI_OVERVIEW_LINEART:
            origin_kind = "ai-derived"
            provider_ids = [overview_provider_id]
        else:
            origin_kind = "classical"
            provider_ids = ["opencv"]
        normalized_candidate_providers = sorted(
            {provider_id for provider_id in provider_ids if isinstance(provider_id, str)}
        )
        if origin_kind == "direct-ai" and not normalized_candidate_providers:
            raise ValueError("Direct AI candidate provenance requires a provider")
        if candidate_id == CANDIDATE_AI_OVERVIEW_LINEART and not normalized_candidate_providers:
            raise ValueError("AI overview cleanup provenance requires a provider")
        record: dict[str, Any] = {
            "id": candidate_id,
            "label": item["label"],
            "artifactChecksum": hashlib.sha256(encoded).hexdigest(),
            "originKind": origin_kind,
            "providerIds": normalized_candidate_providers,
            "changedPixelsOutsideMask": item["changedPixelsOutsideMask"],
            "meanAbsDeltaInsideMask": item["meanAbsDeltaInsideMask"],
            "chromaInsideMask": item["chromaInsideMask"],
            "anomalies": list(item["anomalies"]),
        }
        if candidate_id == CANDIDATE_AI_OVERVIEW_LINEART:
            if overview_base_bytes is None:
                raise ValueError("AI overview cleanup requires its derivation base")
            record["lineage"] = {
                "version": 1,
                "transformId": "manga-overview-lineart-cleanup",
                "transformVersion": 1,
                "baseId": INTERNAL_OVERVIEW_BASE_ID,
                "baseChecksum": hashlib.sha256(overview_base_bytes).hexdigest(),
            }
        manifest_candidates.append(record)
    if any(record.get("id") == CANDIDATE_AI_OVERVIEW_LINEART for record in manifest_candidates):
        if overview_base_bytes is None:
            raise ValueError("AI overview cleanup requires its derivation base")
        base_bytes = overview_base_bytes
        base_providers = sorted(
            {overview_provider_id} if isinstance(overview_provider_id, str) else set()
        )
        if not base_providers or any(
            provider not in AI_PROVIDER_IDS for provider in base_providers
        ):
            raise ValueError("AI overview derivation base requires an allowlisted provider")
        encoded_files.append((INTERNAL_OVERVIEW_BASE_ID, base_bytes))
        manifest_candidates.append(
            {
                "kind": INTERNAL_RECORD_KIND,
                "id": INTERNAL_OVERVIEW_BASE_ID,
                "artifactChecksum": hashlib.sha256(base_bytes).hexdigest(),
                "originKind": "direct-ai",
                "providerIds": base_providers,
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
    generation_id: str,
    mask_checksum: str,
    encoded_files: list[tuple[str, bytes]],
    manifest_candidates: list[dict[str, Any]],
) -> None:
    candidates, internal_bases = _partition_manifest_records(manifest_candidates)
    delete_inpaint_candidate_files(store, relative)
    for candidate_id, encoded in encoded_files:
        if candidate_id == INTERNAL_OVERVIEW_BASE_ID:
            atomic_write_bytes(_internal_base_path(store, relative, candidate_id), encoded)
        else:
            atomic_write_bytes(candidate_image_path(store, relative, candidate_id), encoded)
    atomic_write_bytes(
        candidate_manifest_path(store, relative),
        json.dumps(
            {
                "version": 2 if internal_bases else 1,
                "generationId": generation_id,
                "maskChecksum": mask_checksum,
                "selectedId": selected_id,
                "candidates": candidates,
                **({"internalBases": internal_bases} if internal_bases else {}),
            },
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


def _read_internal_candidate_manifest(
    store: ProjectStore,
    relative: Path,
) -> dict[str, Any]:
    path = candidate_manifest_path(store, relative)
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectError(
            "Inpainting candidate evidence is unavailable; rerun inpainting"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("version") not in {1, 2}
        or not isinstance(payload.get("generationId"), str)
        or not payload["generationId"]
        or not _is_sha256(payload.get("maskChecksum"))
        or not isinstance(payload.get("candidates"), list)
        or (payload.get("version") == 2 and not isinstance(payload.get("internalBases"), list))
    ):
        raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
    return payload


def validate_inpaint_candidate_evidence(
    store: ProjectStore,
    relative: Path,
    manifest: dict[str, Any],
) -> str:
    """Validate every immutable candidate/base artifact and its AI lineage."""
    candidates = manifest.get("candidates")
    internal_bases = manifest.get("internalBases", [])
    if not isinstance(candidates, list) or not isinstance(internal_bases, list):
        raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
    if manifest.get("version") != (2 if internal_bases else 1):
        raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
    candidate_ids: set[str] = set()
    for record in candidates:
        if not isinstance(record, dict):
            raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
        candidate_id = record.get("id")
        checksum = record.get("artifactChecksum")
        providers = record.get("providerIds")
        origin_kind = record.get("originKind")
        if (
            candidate_id not in CANDIDATE_IDS
            or candidate_id in candidate_ids
            or not _is_sha256(checksum)
            or origin_kind not in VALID_CANDIDATE_ORIGINS
            or (candidate_id == CANDIDATE_AI_OVERVIEW_LINEART) != (origin_kind == "ai-derived")
            or not isinstance(providers, list)
            or providers != sorted(set(providers))
            or any(
                not isinstance(provider, str) or not provider or len(provider) > 80
                for provider in providers
            )
        ):
            raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
        candidate_ids.add(candidate_id)
        try:
            actual_checksum = hashlib.sha256(
                candidate_image_path(store, relative, candidate_id).read_bytes()
            ).hexdigest()
        except OSError as error:
            raise ProjectError(
                "Inpainting candidate evidence is unavailable; rerun inpainting"
            ) from error
        if actual_checksum != checksum:
            raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")

    base_by_id: dict[str, dict[str, Any]] = {}
    for base in internal_bases:
        if not isinstance(base, dict):
            raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
        base_id = base.get("id")
        providers = base.get("providerIds")
        if (
            set(base) != {"kind", "id", "artifactChecksum", "originKind", "providerIds"}
            or base.get("kind") != INTERNAL_RECORD_KIND
            or base_id != INTERNAL_OVERVIEW_BASE_ID
            or base_id in base_by_id
            or base.get("originKind") != "direct-ai"
            or not _is_sha256(base.get("artifactChecksum"))
            or not isinstance(providers, list)
            or not providers
            or providers != sorted(set(providers))
            or any(provider not in AI_PROVIDER_IDS for provider in providers)
        ):
            raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
        try:
            actual_checksum = hashlib.sha256(
                _internal_base_path(store, relative, base_id).read_bytes()
            ).hexdigest()
        except OSError as error:
            raise ProjectError(
                "Inpainting candidate evidence is unavailable; rerun inpainting"
            ) from error
        if actual_checksum != base["artifactChecksum"]:
            raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")
        base_by_id[base_id] = base

    derived_count = 0
    for record in candidates:
        lineage = record.get("lineage")
        if record.get("originKind") != "ai-derived":
            if lineage is not None:
                raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
            continue
        derived_count += 1
        providers = record["providerIds"]
        if (
            record.get("id") != CANDIDATE_AI_OVERVIEW_LINEART
            or not providers
            or any(provider not in AI_PROVIDER_IDS for provider in providers)
            or not isinstance(lineage, dict)
            or set(lineage)
            != {"version", "transformId", "transformVersion", "baseId", "baseChecksum"}
            or lineage.get("version") != 1
            or (lineage.get("transformId"), lineage.get("transformVersion"))
            not in DERIVED_TRANSFORMS
            or lineage.get("baseId") not in base_by_id
            or lineage.get("baseChecksum") != base_by_id[lineage["baseId"]]["artifactChecksum"]
            or base_by_id[lineage["baseId"]]["providerIds"] != providers
        ):
            raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
    if derived_count != len(internal_bases):
        raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
    try:
        return inpaint_candidate_manifest_digest(
            generation_id=manifest["generationId"],
            mask_checksum=manifest["maskChecksum"],
            candidates=candidates,
            internal_bases=internal_bases,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting") from error


def select_inpaint_candidate(
    store: ProjectStore,
    image_id: str,
    *,
    candidate_id: str,
    expected_revision: int,
) -> ImageAsset:
    from manga_localizer.services.images import (
        clear_stage_reviews,
        current_inpaint_provenance,
        invalidate_image_pipeline,
        make_inpaint_provenance,
        normalize_inpaint_provenance,
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
            relative = safe_relative_path(image.relative_path).with_suffix(".png")
            manifest = _read_internal_candidate_manifest(store, relative)
            internal_candidates = manifest["candidates"]
            manifest_digest = validate_inpaint_candidate_evidence(store, relative, manifest)
            selected_record = next(
                (
                    item
                    for item in internal_candidates
                    if isinstance(item, dict) and item.get("id") == candidate_id
                ),
                None,
            )
            if selected_record is None:
                raise ProjectError("The requested inpainting candidate is not available")
            artifact_checksum = selected_record.get("artifactChecksum")
            origin_kind = selected_record.get("originKind")
            provider_ids = selected_record.get("providerIds")
            if (
                not _is_sha256(artifact_checksum)
                or not isinstance(origin_kind, str)
                or not isinstance(provider_ids, list)
            ):
                raise ProjectError("Inpainting candidate evidence is invalid; rerun inpainting")
            source = candidate_image_path(store, relative, candidate_id)
            if not source.is_file():
                raise ProjectError(
                    "The requested inpainting candidate file is missing; rerun inpainting"
                )
            selected_bytes = source.read_bytes()
            if hashlib.sha256(selected_bytes).hexdigest() != artifact_checksum:
                raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")
            mask_path = resolve_write_target(
                store.root,
                Path("generated") / "masks" / relative,
                protected_roots=(store.source_root,),
            )
            try:
                mask_checksum = hashlib.sha256(mask_path.read_bytes()).hexdigest()
            except OSError as error:
                raise ProjectError("Inpainting mask is unavailable; rerun inpainting") from error
            if mask_checksum != manifest["maskChecksum"]:
                raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")
            current_provenance = normalize_inpaint_provenance(image.inpaint_provenance)
            if (
                current_provenance is None
                or current_provenance["generationId"] != manifest["generationId"]
                or current_provenance["maskChecksum"] != mask_checksum
                or current_provenance["candidateManifestDigest"] != manifest_digest
            ):
                raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")
            destination = resolve_write_target(
                store.root,
                Path("generated") / "inpainted" / relative,
                protected_roots=(store.source_root,),
            )
            try:
                current_artifact_checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
            except OSError as error:
                raise ProjectError("Inpainted artifact is unavailable; rerun inpainting") from error
            if current_artifact_checksum != current_provenance["artifactChecksum"]:
                raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")
            if (
                current_inpaint_provenance(
                    store,
                    image,
                    {
                        "artifactChecksum": current_artifact_checksum,
                        "maskChecksum": mask_checksum,
                    },
                )
                is None
            ):
                raise ProjectError("Inpainting candidate evidence changed; rerun inpainting")
            provenance = make_inpaint_provenance(
                artifact_checksum=artifact_checksum,
                mask_checksum=mask_checksum,
                candidate_id=candidate_id,
                origin_kind=origin_kind,
                provider_ids=provider_ids,
                generation_id=manifest["generationId"],
                candidate_manifest_digest=manifest_digest,
            )
            project = store.project(session)
            current_id, _ = public_candidates_from_status(image.status)
            before = {"inpaintCandidate": current_id}
            atomic_write_bytes(destination, selected_bytes)
            manifest["selectedId"] = candidate_id
            atomic_write_bytes(
                candidate_manifest_path(store, relative),
                json.dumps(
                    manifest,
                    ensure_ascii=True,
                    indent=2,
                ).encode("utf-8"),
            )
            invalidate_image_pipeline(store, image, {"typeset", "export"})
            reset_image_review(image)
            clear_stage_reviews(image, {"inpaint", "typeset"})
            status = dict(image.status or {})
            status["inpaint"] = "done"
            status["inpaintCandidate"] = candidate_id
            status["inpaintCandidates"] = public_candidate_records(internal_candidates)
            image.status = status
            image.inpaint_provenance = provenance
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
