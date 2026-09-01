from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.database import ImageAsset, PageGeneration
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
from manga_localizer.services.page_lineage import (
    _immutable_image_path,
    require_image_mutation_lineage,
)
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
LAYERED_STRUCTURE_INPUT_ROOT = Path("generated") / "lineage-inputs" / "layered-structure-v1"


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


def trusted_public_candidate_evidence(
    store: ProjectStore,
    image: ImageAsset,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Project the current candidate origins only after validating private evidence."""
    from manga_localizer.services.images import normalize_inpaint_provenance

    provenance = normalize_inpaint_provenance(image.inpaint_provenance)
    if provenance is None or (image.status or {}).get("inpaint") != "done":
        return None, None, []
    try:
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        manifest = _read_internal_candidate_manifest(store, relative)
        digest = inpaint_candidate_manifest_digest(
            generation_id=manifest["generationId"],
            mask_checksum=manifest["maskChecksum"],
            candidates=manifest["candidates"],
            internal_bases=manifest.get("internalBases", []),
        )
    except (OSError, ProjectError, TypeError, ValueError):
        return None, None, []
    selected_id = manifest.get("selectedId")
    selected_records = [
        record
        for record in manifest["candidates"]
        if isinstance(record, dict) and record.get("id") == selected_id
    ]
    if (
        manifest.get("generationId") != provenance["generationId"]
        or manifest.get("maskChecksum") != provenance["maskChecksum"]
        or digest != provenance["candidateManifestDigest"]
        or selected_id != provenance["candidateId"]
        or len(selected_records) != 1
        or selected_records[0].get("artifactChecksum") != provenance["artifactChecksum"]
        or selected_records[0].get("originKind") != provenance["originKind"]
        or selected_records[0].get("providerIds") != provenance["providerIds"]
    ):
        return None, None, []
    records = public_candidate_records(manifest["candidates"])
    if any(record.get("originKind") not in VALID_CANDIDATE_ORIGINS for record in records):
        return None, None, []
    return str(manifest["generationId"]), str(selected_id), records


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


def _write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as error:
            raise ProjectError("Layered structure snapshot is unavailable") from error
        if existing != payload:
            raise ProjectError("Layered structure snapshot collision")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".layered-structure-publish-",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ProjectError("Layered structure snapshot collision") from None
        finally:
            temporary.unlink(missing_ok=True)
    except ProjectError:
        raise
    except OSError as error:
        raise ProjectError("Layered structure snapshot publication failed") from error


def snapshot_layered_structure_references(
    store: ProjectStore,
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    references: object,
    expected_grid: tuple[int, int],
) -> list[dict[str, Any]]:
    """Resolve legacy candidates once and bind immutable bytes to the current generation."""
    if not isinstance(references, list) or not 1 <= len(references) <= 16:
        raise ProjectError("layeredStructureReferences must contain 1 to 16 references")
    normalized: list[dict[str, Any]] = []
    reference_ids: set[str] = set()
    for requested in references:
        expected_keys = {
            "id",
            "imageId",
            "candidateId",
            "expectedSourceChecksum",
            "expectedArtifactChecksum",
            "expectedManifestDigest",
            "expectedMaskChecksum",
        }
        if not isinstance(requested, dict) or set(requested) != expected_keys:
            raise ProjectError("Layered structure reference has unsupported keys")
        reference_id = requested.get("id")
        if (
            not isinstance(reference_id, str)
            or not 1 <= len(reference_id) <= 64
            or not all(character.isalnum() or character in "._-" for character in reference_id)
            or reference_id in reference_ids
        ):
            raise ProjectError("Layered structure reference id is invalid or duplicated")
        reference_ids.add(reference_id)
        if any(
            not _is_sha256(requested.get(key))
            for key in (
                "expectedSourceChecksum",
                "expectedArtifactChecksum",
                "expectedManifestDigest",
                "expectedMaskChecksum",
            )
        ):
            raise ProjectError("Layered structure reference checksums are invalid")
        reference_image = session.get(ImageAsset, requested.get("imageId"))
        if (
            reference_image is None
            or reference_image.project_id != image.project_id
            or reference_image.checksum != generation.source_checksum
            or requested["expectedSourceChecksum"] != generation.source_checksum
        ):
            raise ProjectError("Layered structure reference is not from the same immutable source")
        try:
            immutable_checksum = hashlib.sha256(
                _immutable_image_path(store, reference_image).read_bytes()
            ).hexdigest()
        except OSError as error:
            raise ProjectError("Layered structure immutable source is unavailable") from error
        if immutable_checksum != generation.source_checksum:
            raise ProjectError("Layered structure immutable source changed")
        relative = safe_relative_path(reference_image.relative_path).with_suffix(".png")
        trusted_generation_id, _selected_id, trusted_records = trusted_public_candidate_evidence(
            store, reference_image
        )
        if trusted_generation_id is None or not any(
            record.get("id") == requested.get("candidateId") for record in trusted_records
        ):
            raise ProjectError("Layered structure reference is not trusted current provenance")
        manifest = _read_internal_candidate_manifest(store, relative)
        manifest_digest = validate_inpaint_candidate_evidence(store, relative, manifest)
        reference_generation = session.get(PageGeneration, manifest["generationId"])
        candidate_id = requested.get("candidateId")
        records = [
            record
            for record in manifest["candidates"]
            if isinstance(record, dict) and record.get("id") == candidate_id
        ]
        if (
            reference_generation is None
            or reference_generation.id == generation.id
            or reference_generation.project_id != image.project_id
            or reference_generation.image_id != reference_image.id
            or reference_generation.source_checksum != generation.source_checksum
            or trusted_generation_id != reference_generation.id
            or len(records) != 1
            or manifest_digest != requested["expectedManifestDigest"]
            or manifest["maskChecksum"] != requested["expectedMaskChecksum"]
            or records[0].get("artifactChecksum") != requested["expectedArtifactChecksum"]
        ):
            raise ProjectError("Layered structure reference lineage or checksum changed")
        try:
            artifact_bytes = candidate_image_path(store, relative, str(candidate_id)).read_bytes()
        except OSError as error:
            raise ProjectError("Layered structure reference artifact is unavailable") from error
        mask_path = resolve_write_target(
            store.root,
            Path("generated") / "masks" / relative,
            protected_roots=(store.source_root,),
        )
        try:
            mask_bytes = mask_path.read_bytes()
            with Image.open(io.BytesIO(artifact_bytes)) as opened:
                opened.load()
                decoded_size = opened.size
            with Image.open(io.BytesIO(mask_bytes)) as opened_mask:
                opened_mask.load()
                mask_size = opened_mask.size
        except (OSError, ValueError) as error:
            raise ProjectError("Layered structure reference artifact is unavailable") from error
        if (
            hashlib.sha256(artifact_bytes).hexdigest() != requested["expectedArtifactChecksum"]
            or hashlib.sha256(mask_bytes).hexdigest() != requested["expectedMaskChecksum"]
            or decoded_size != expected_grid
            or mask_size != expected_grid
        ):
            raise ProjectError("Layered structure reference grid or bytes changed")
        ancestry = {
            "referenceGenerationId": reference_generation.id,
            "originKind": records[0]["originKind"],
            "providerIds": records[0]["providerIds"],
            **({"lineage": records[0]["lineage"]} if "lineage" in records[0] else {}),
        }
        if records[0]["originKind"] == "classical" and records[0]["providerIds"] != ["opencv"]:
            raise ProjectError("Layered structure classical reference provenance is invalid")
        source_manifest = {
            "version": 1,
            "referenceId": reference_id,
            "referenceImageId": reference_image.id,
            "referenceCandidateId": candidate_id,
            "referenceGenerationId": reference_generation.id,
            "sourceChecksum": generation.source_checksum,
            "artifactChecksum": requested["expectedArtifactChecksum"],
            "legacyManifestDigest": manifest_digest,
            "maskChecksum": manifest["maskChecksum"],
            "width": decoded_size[0],
            "height": decoded_size[1],
            "ancestry": ancestry,
        }
        source_manifest_bytes = json.dumps(
            source_manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        source_manifest_digest = hashlib.sha256(source_manifest_bytes).hexdigest()
        snapshot_id = hashlib.sha256(
            json.dumps(
                {
                    "generationId": generation.id,
                    "sourceManifestDigest": source_manifest_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        snapshot_root = LAYERED_STRUCTURE_INPUT_ROOT / generation.id / snapshot_id
        artifact_target = resolve_write_target(
            store.root,
            snapshot_root / "artifact.png",
            protected_roots=(store.source_root,),
        )
        manifest_target = resolve_write_target(
            store.root,
            snapshot_root / "manifest.json",
            protected_roots=(store.source_root,),
        )
        _write_once(artifact_target, artifact_bytes)
        _write_once(manifest_target, source_manifest_bytes)
        normalized.append(
            {
                "referenceId": reference_id,
                "referenceImageId": reference_image.id,
                "referenceCandidateId": candidate_id,
                "snapshotId": snapshot_id,
                "artifactChecksum": requested["expectedArtifactChecksum"],
                "sourceManifestDigest": source_manifest_digest,
                "legacyManifestDigest": manifest_digest,
                "sourceChecksum": generation.source_checksum,
                "maskChecksum": manifest["maskChecksum"],
                "width": decoded_size[0],
                "height": decoded_size[1],
                "ancestry": ancestry,
            }
        )
    return sorted(normalized, key=lambda item: item["referenceId"])


def load_layered_structure_snapshots(
    store: ProjectStore,
    generation_id: str,
    snapshots: list[dict[str, Any]],
) -> dict[str, bytes]:
    loaded: dict[str, bytes] = {}
    snapshot_ids: set[str] = set()
    for snapshot in snapshots:
        expected_keys = {
            "referenceId",
            "referenceImageId",
            "referenceCandidateId",
            "snapshotId",
            "artifactChecksum",
            "sourceManifestDigest",
            "legacyManifestDigest",
            "sourceChecksum",
            "maskChecksum",
            "width",
            "height",
            "ancestry",
        }
        ancestry = snapshot.get("ancestry") if isinstance(snapshot, dict) else None
        ancestry_keys = set(ancestry) if isinstance(ancestry, dict) else set()
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != expected_keys
            or any(
                not _is_sha256(snapshot.get(key))
                for key in (
                    "snapshotId",
                    "artifactChecksum",
                    "sourceManifestDigest",
                    "legacyManifestDigest",
                    "sourceChecksum",
                    "maskChecksum",
                )
            )
            or not isinstance(snapshot.get("referenceId"), str)
            or not isinstance(snapshot.get("referenceImageId"), str)
            or not isinstance(snapshot.get("referenceCandidateId"), str)
            or type(snapshot.get("width")) is not int
            or type(snapshot.get("height")) is not int
            or snapshot["width"] <= 0
            or snapshot["height"] <= 0
            or ancestry_keys
            not in (
                {"referenceGenerationId", "originKind", "providerIds"},
                {"referenceGenerationId", "originKind", "providerIds", "lineage"},
            )
            or not isinstance(ancestry.get("referenceGenerationId"), str)
            or ancestry.get("originKind") not in VALID_CANDIDATE_ORIGINS
            or not isinstance(ancestry.get("providerIds"), list)
            or any(
                not isinstance(provider, str) or not provider or len(provider) > 80
                for provider in ancestry.get("providerIds", [])
            )
            or ancestry.get("providerIds") != sorted(set(ancestry.get("providerIds", [])))
            or snapshot.get("referenceId") in loaded
            or snapshot.get("snapshotId") in snapshot_ids
        ):
            raise ProjectError("Layered structure snapshot binding is invalid")
        expected_snapshot_id = hashlib.sha256(
            json.dumps(
                {
                    "generationId": generation_id,
                    "sourceManifestDigest": snapshot["sourceManifestDigest"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if snapshot["snapshotId"] != expected_snapshot_id:
            raise ProjectError("Layered structure snapshot content address is invalid")
        snapshot_ids.add(snapshot["snapshotId"])
        root = LAYERED_STRUCTURE_INPUT_ROOT / generation_id / snapshot["snapshotId"]
        artifact_path = resolve_write_target(
            store.root, root / "artifact.png", protected_roots=(store.source_root,)
        )
        manifest_path = resolve_write_target(
            store.root, root / "manifest.json", protected_roots=(store.source_root,)
        )
        try:
            artifact_bytes = artifact_path.read_bytes()
            manifest_bytes = manifest_path.read_bytes()
        except OSError as error:
            raise ProjectError("Layered structure snapshot is unavailable") from error
        if (
            hashlib.sha256(artifact_bytes).hexdigest() != snapshot["artifactChecksum"]
            or hashlib.sha256(manifest_bytes).hexdigest() != snapshot["sourceManifestDigest"]
        ):
            raise ProjectError("Layered structure snapshot changed")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProjectError("Layered structure snapshot manifest is invalid") from error
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {
                "version",
                "referenceId",
                "referenceImageId",
                "referenceCandidateId",
                "referenceGenerationId",
                "sourceChecksum",
                "artifactChecksum",
                "legacyManifestDigest",
                "maskChecksum",
                "width",
                "height",
                "ancestry",
            }
            or manifest.get("version") != 1
            or manifest.get("referenceId") != snapshot["referenceId"]
            or manifest.get("referenceImageId") != snapshot["referenceImageId"]
            or manifest.get("referenceCandidateId") != snapshot["referenceCandidateId"]
            or manifest.get("referenceGenerationId")
            != snapshot["ancestry"]["referenceGenerationId"]
            or manifest.get("artifactChecksum") != snapshot["artifactChecksum"]
            or manifest.get("legacyManifestDigest") != snapshot["legacyManifestDigest"]
            or manifest.get("sourceChecksum") != snapshot["sourceChecksum"]
            or manifest.get("maskChecksum") != snapshot["maskChecksum"]
            or manifest.get("width") != snapshot["width"]
            or manifest.get("height") != snapshot["height"]
            or manifest.get("ancestry") != snapshot["ancestry"]
        ):
            raise ProjectError("Layered structure snapshot manifest changed")
        try:
            with Image.open(io.BytesIO(artifact_bytes)) as opened:
                opened.load()
                decoded_size = opened.size
        except (OSError, ValueError) as error:
            raise ProjectError("Layered structure snapshot artifact is invalid") from error
        if decoded_size != (snapshot["width"], snapshot["height"]):
            raise ProjectError("Layered structure snapshot artifact grid changed")
        loaded[snapshot["referenceId"]] = artifact_bytes
    return loaded


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
            require_image_mutation_lineage(store, session, image, None)
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
            image.inpaint_classical_approval = None
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
