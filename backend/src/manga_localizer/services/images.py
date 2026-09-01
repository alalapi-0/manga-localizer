from __future__ import annotations

import hashlib
import io
import json
import math
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
from manga_localizer.imaging.preprocessing import suggest_preprocess_profile
from manga_localizer.security import (
    UnsafePathError,
    atomic_write_bytes,
    portable_path_key,
    resolve_within,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.page_lineage import (
    PageLineageConflict,
    record_stage_review_event,
    require_image_mutation_lineage,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)
from manga_localizer.services.trust import is_region_trusted

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF"}
AI_INPAINT_PROVIDERS = frozenset({"lama", "lama-onnx"})
CLASSICAL_FALLBACK_REASON = "ai-visible-artifacts"
CLASSICAL_FALLBACK_KIND = "classical-page-fallback"
INPAINT_ORIGIN_KINDS = frozenset(
    {"direct-ai", "ai-derived", "classical", "deterministic-postprocess", "mixed", "no-op"}
)


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


class StagePrerequisiteConflict(ProjectError):
    def __init__(
        self,
        message: str,
        *,
        resource: str,
        stage: str,
        reason: str,
        mismatches: list[str] | None = None,
    ):
        super().__init__(message)
        self.resource = resource
        self.stage = stage
        self.required_state = "accepted"
        self.reason = reason
        self.mismatches = list(mismatches or [])


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


def normalize_inpaint_provenance(value: object) -> dict[str, object] | None:
    """Return the canonical internal clean-plate provenance record."""
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        return None
    if value.get("version") != 1:
        return None
    artifact_checksum = value.get("artifactChecksum")
    mask_checksum = value.get("maskChecksum")
    candidate_id = value.get("candidateId")
    origin_kind = value.get("originKind")
    generation_id = value.get("generationId")
    candidate_manifest_digest = value.get("candidateManifestDigest")
    provider_ids = value.get("providerIds")
    if (
        not isinstance(artifact_checksum, str)
        or not _SHA256_RE.fullmatch(artifact_checksum)
        or not isinstance(mask_checksum, str)
        or not _SHA256_RE.fullmatch(mask_checksum)
        or not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 80
        or origin_kind not in INPAINT_ORIGIN_KINDS
        or not isinstance(generation_id, str)
        or not generation_id
        or len(generation_id) > 128
        or not isinstance(candidate_manifest_digest, str)
        or not _SHA256_RE.fullmatch(candidate_manifest_digest)
        or not isinstance(provider_ids, list)
        or any(
            not isinstance(provider_id, str) or not provider_id or len(provider_id) > 80
            for provider_id in provider_ids
        )
    ):
        return None
    normalized_provider_ids = sorted(set(provider_ids))
    if origin_kind in {"direct-ai", "ai-derived"} and not normalized_provider_ids:
        return None
    if origin_kind == "no-op" and normalized_provider_ids:
        return None
    return {
        "version": 1,
        "artifactChecksum": artifact_checksum,
        "maskChecksum": mask_checksum,
        "candidateId": candidate_id,
        "originKind": origin_kind,
        "providerIds": normalized_provider_ids,
        "generationId": generation_id,
        "candidateManifestDigest": candidate_manifest_digest,
    }


def make_inpaint_provenance(
    *,
    artifact_checksum: str,
    mask_checksum: str,
    candidate_id: str,
    origin_kind: str,
    provider_ids: Iterable[str],
    generation_id: str,
    candidate_manifest_digest: str,
) -> dict[str, object]:
    normalized = normalize_inpaint_provenance(
        {
            "version": 1,
            "artifactChecksum": artifact_checksum,
            "maskChecksum": mask_checksum,
            "candidateId": candidate_id,
            "originKind": origin_kind,
            "providerIds": list(provider_ids),
            "generationId": generation_id,
            "candidateManifestDigest": candidate_manifest_digest,
        }
    )
    if normalized is None:
        raise ProjectError("Inpainting provenance is invalid")
    return normalized


def inpaint_provenance_digest(value: object) -> str | None:
    normalized = normalize_inpaint_provenance(value)
    if normalized is None:
        return None
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_inpaint_provenance(
    store: ProjectStore,
    image: ImageAsset,
    checksums: dict[str, str],
) -> tuple[dict[str, object], str] | None:
    normalized = normalize_inpaint_provenance(image.inpaint_provenance)
    if normalized is None:
        return None
    if normalized["artifactChecksum"] != checksums.get("artifactChecksum") or normalized[
        "maskChecksum"
    ] != checksums.get("maskChecksum"):
        return None
    if not _inpaint_provenance_matches_candidate_evidence(store, image, normalized):
        return None
    digest = inpaint_provenance_digest(normalized)
    if digest is None:
        return None
    return normalized, digest


def _strict_ai_candidate_records(candidates: object) -> list[dict[str, object]]:
    if not isinstance(candidates, list):
        return []
    eligible: list[dict[str, object]] = []
    for record in candidates:
        if not isinstance(record, dict) or record.get("originKind") not in {
            "direct-ai",
            "ai-derived",
        }:
            continue
        providers = record.get("providerIds")
        if (
            not isinstance(providers, list)
            or not providers
            or any(provider not in AI_INPAINT_PROVIDERS for provider in providers)
        ):
            continue
        eligible.append(
            {
                "id": record.get("id"),
                "artifactChecksum": record.get("artifactChecksum"),
                "originKind": record.get("originKind"),
                "providerIds": list(providers),
            }
        )
    return sorted(eligible, key=lambda item: str(item["id"]))


def _ai_candidate_reviews_digest(value: dict[str, object]) -> str:
    payload = dict(value)
    payload.pop("evidenceDigest", None)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_ai_candidate_reviews(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "generationId",
        "candidateManifestDigest",
        "reviews",
        "evidenceDigest",
    }:
        return None
    if value.get("version") != 1:
        return None
    generation_id = value.get("generationId")
    manifest_digest = value.get("candidateManifestDigest")
    evidence_digest = value.get("evidenceDigest")
    reviews = value.get("reviews")
    if (
        not isinstance(generation_id, str)
        or not generation_id
        or not isinstance(manifest_digest, str)
        or not _SHA256_RE.fullmatch(manifest_digest)
        or not isinstance(evidence_digest, str)
        or not _SHA256_RE.fullmatch(evidence_digest)
        or not isinstance(reviews, list)
    ):
        return None
    normalized_reviews: list[dict[str, object]] = []
    for review in reviews:
        if not isinstance(review, dict) or set(review) != {
            "id",
            "artifactChecksum",
            "originKind",
            "providerIds",
            "state",
            "reviewedAt",
        }:
            return None
        providers = review.get("providerIds")
        reviewed_at = review.get("reviewedAt")
        if (
            not isinstance(review.get("id"), str)
            or not review["id"]
            or not isinstance(review.get("artifactChecksum"), str)
            or not _SHA256_RE.fullmatch(str(review["artifactChecksum"]))
            or review.get("originKind") not in {"direct-ai", "ai-derived"}
            or review.get("state") != "rejected"
            or not isinstance(providers, list)
            or not providers
            or providers != sorted(set(providers))
            or any(provider not in AI_INPAINT_PROVIDERS for provider in providers)
            or not isinstance(reviewed_at, str)
        ):
            return None
        try:
            parsed = datetime.fromisoformat(reviewed_at)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        normalized_reviews.append(dict(review))
    if normalized_reviews != sorted(normalized_reviews, key=lambda item: str(item["id"])):
        return None
    if len({str(review["id"]) for review in normalized_reviews}) != len(normalized_reviews):
        return None
    normalized = dict(value)
    normalized["reviews"] = normalized_reviews
    if _ai_candidate_reviews_digest(normalized) != evidence_digest:
        return None
    return normalized


def _current_ai_candidate_reviews_metadata(
    store: ProjectStore,
    image: ImageAsset,
) -> tuple[dict[str, object], list[dict[str, object]]] | None:
    """Validate review metadata without reading candidate image bytes."""
    authority = _normalize_ai_candidate_reviews(image.inpaint_ai_candidate_reviews)
    provenance = normalize_inpaint_provenance(image.inpaint_provenance)
    if authority is None or provenance is None or (image.status or {}).get("inpaint") != "done":
        return None
    from manga_localizer.services.inpaint_candidates import (
        _read_internal_candidate_manifest,
        inpaint_candidate_manifest_digest,
    )

    try:
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        manifest = _read_internal_candidate_manifest(store, relative)
        manifest_digest = inpaint_candidate_manifest_digest(
            generation_id=manifest["generationId"],
            mask_checksum=manifest["maskChecksum"],
            candidates=manifest["candidates"],
            internal_bases=manifest.get("internalBases", []),
        )
    except (OSError, ProjectError, TypeError, ValueError, UnsafePathError):
        return None
    strict_candidates = _strict_ai_candidate_records(manifest.get("candidates"))
    by_id = {str(candidate["id"]): candidate for candidate in strict_candidates}
    selected_records = [
        record
        for record in manifest["candidates"]
        if isinstance(record, dict) and record.get("id") == manifest.get("selectedId")
    ]
    reviews = authority["reviews"]
    if (
        authority["generationId"] != manifest.get("generationId")
        or authority["generationId"] != provenance["generationId"]
        or authority["candidateManifestDigest"] != manifest_digest
        or authority["candidateManifestDigest"] != provenance["candidateManifestDigest"]
        or manifest.get("maskChecksum") != provenance["maskChecksum"]
        or manifest.get("selectedId") != provenance["candidateId"]
        or len(selected_records) != 1
        or selected_records[0].get("artifactChecksum") != provenance["artifactChecksum"]
        or selected_records[0].get("originKind") != provenance["originKind"]
        or selected_records[0].get("providerIds") != provenance["providerIds"]
        or not isinstance(reviews, list)
    ):
        return None
    for review in reviews:
        if not isinstance(review, dict):
            return None
        candidate = by_id.get(str(review.get("id")))
        if candidate is None or any(
            review.get(key) != candidate.get(key)
            for key in ("id", "artifactChecksum", "originKind", "providerIds")
        ):
            return None
    return authority, strict_candidates


def public_inpaint_ai_rejected_candidate_ids(
    store: ProjectStore,
    image: ImageAsset,
) -> list[str]:
    current = _current_ai_candidate_reviews_metadata(store, image)
    if current is None:
        return []
    authority, _strict_candidates = current
    reviews = authority["reviews"]
    assert isinstance(reviews, list)
    return [str(review["id"]) for review in reviews if isinstance(review, dict)]


def _normalize_classical_approval(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    required = {
        "version",
        "state",
        "kind",
        "reason",
        "originKind",
        "candidateId",
        "rejectedAiCandidates",
        "generationId",
        "candidateManifestDigest",
        "artifactChecksum",
        "maskChecksum",
        "provenanceDigest",
        "reviewResultRevision",
        "approvedAt",
        "approvalDigest",
    }
    if set(value) != required or (
        value.get("version") != 1
        or value.get("state") != "approved"
        or value.get("kind") != CLASSICAL_FALLBACK_KIND
        or value.get("reason") != CLASSICAL_FALLBACK_REASON
        or value.get("originKind") != "classical"
    ):
        return None
    strings = (
        "candidateId",
        "generationId",
        "approvedAt",
    )
    digests = (
        "candidateManifestDigest",
        "artifactChecksum",
        "maskChecksum",
        "provenanceDigest",
        "approvalDigest",
    )
    if any(not isinstance(value.get(key), str) or not value[key] for key in strings):
        return None
    if any(
        not isinstance(value.get(key), str) or not _SHA256_RE.fullmatch(str(value[key]))
        for key in digests
    ):
        return None
    try:
        approved_at = datetime.fromisoformat(str(value["approvedAt"]))
    except ValueError:
        return None
    if approved_at.tzinfo is None:
        return None
    result_revision = value.get("reviewResultRevision")
    rejected = value.get("rejectedAiCandidates")
    if (
        not isinstance(result_revision, int)
        or isinstance(result_revision, bool)
        or result_revision < 0
        or not isinstance(rejected, list)
        or not rejected
    ):
        return None
    normalized_rejected: list[dict[str, object]] = []
    for record in rejected:
        if not isinstance(record, dict) or set(record) != {
            "id",
            "artifactChecksum",
            "originKind",
            "providerIds",
            "state",
            "reviewedAt",
        }:
            return None
        providers = record.get("providerIds")
        if (
            not isinstance(record.get("id"), str)
            or not record["id"]
            or not isinstance(record.get("artifactChecksum"), str)
            or not _SHA256_RE.fullmatch(str(record["artifactChecksum"]))
            or record.get("originKind") not in {"direct-ai", "ai-derived"}
            or record.get("state") != "rejected"
            or not isinstance(providers, list)
            or not providers
            or providers != sorted(set(providers))
            or any(provider not in AI_INPAINT_PROVIDERS for provider in providers)
            or not isinstance(record.get("reviewedAt"), str)
        ):
            return None
        normalized_rejected.append(dict(record))
    if normalized_rejected != sorted(normalized_rejected, key=lambda item: str(item["id"])):
        return None
    if len({str(record["id"]) for record in normalized_rejected}) != len(normalized_rejected):
        return None
    normalized = dict(value)
    normalized["rejectedAiCandidates"] = normalized_rejected
    digest_payload = dict(normalized)
    recorded_digest = str(digest_payload.pop("approvalDigest"))
    actual_digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if recorded_digest != actual_digest:
        return None
    return normalized


def _current_classical_fallback_approval(
    store: ProjectStore,
    image: ImageAsset,
    *,
    checksums: dict[str, str],
    review: dict[str, str | int],
    provenance: dict[str, object],
    provenance_digest: str,
) -> dict[str, object] | None:
    approval = _normalize_classical_approval(image.inpaint_classical_approval)
    if approval is None or provenance.get("originKind") != "classical":
        return None
    from manga_localizer.services.inpaint_candidates import (
        _read_internal_candidate_manifest,
        validate_inpaint_candidate_evidence,
    )

    try:
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        manifest = _read_internal_candidate_manifest(store, relative)
        manifest_digest = validate_inpaint_candidate_evidence(store, relative, manifest)
    except (OSError, ProjectError, TypeError, ValueError, UnsafePathError):
        return None
    rejected = _strict_ai_candidate_records(manifest.get("candidates"))
    authority = _current_ai_candidate_reviews_metadata(store, image)
    authority_reviews = authority[0]["reviews"] if authority is not None else None
    anchored_rejected = [
        {
            **candidate,
            "state": "rejected",
            "reviewedAt": review["reviewedAt"],
        }
        for candidate in rejected
        for review in (authority_reviews if isinstance(authority_reviews, list) else [])
        if isinstance(review, dict) and review.get("id") == candidate.get("id")
    ]
    anchors_match = (
        bool(rejected)
        and manifest.get("selectedId") == provenance.get("candidateId")
        and approval["candidateId"] == provenance.get("candidateId")
        and approval["generationId"] == provenance.get("generationId")
        and approval["candidateManifestDigest"] == manifest_digest
        and approval["candidateManifestDigest"] == provenance.get("candidateManifestDigest")
        and approval["artifactChecksum"] == checksums.get("artifactChecksum")
        and approval["artifactChecksum"] == provenance.get("artifactChecksum")
        and approval["maskChecksum"] == checksums.get("maskChecksum")
        and approval["maskChecksum"] == provenance.get("maskChecksum")
        and approval["provenanceDigest"] == provenance_digest
        and review.get("provenanceDigest") == provenance_digest
        and approval["reviewResultRevision"] == review.get("resultRevision")
        and len(anchored_rejected) == len(rejected)
        and approval["rejectedAiCandidates"] == anchored_rejected
    )
    return approval if anchors_match else None


def public_inpaint_fallback(
    store: ProjectStore,
    image: ImageAsset,
) -> dict[str, object]:
    approval = _normalize_classical_approval(image.inpaint_classical_approval)
    if approval is None:
        return {"state": "pending", "rejectedAiCandidateIds": []}
    review = stage_reviews(image).get("inpaint")
    if review is None or review.get("state") != "accepted":
        return {"state": "pending", "rejectedAiCandidateIds": []}
    try:
        checksums = stage_artifact_checksums(store, image, "inpaint")
    except (ProjectError, UnsafePathError):
        return {"state": "pending", "rejectedAiCandidateIds": []}
    current = current_inpaint_provenance(store, image, checksums)
    if current is None:
        return {"state": "pending", "rejectedAiCandidateIds": []}
    provenance, provenance_digest = current
    if (
        _current_classical_fallback_approval(
            store,
            image,
            checksums=checksums,
            review=review,
            provenance=provenance,
            provenance_digest=provenance_digest,
        )
        is None
    ):
        return {"state": "pending", "rejectedAiCandidateIds": []}
    return {
        "state": "approved",
        "kind": CLASSICAL_FALLBACK_KIND,
        "reason": CLASSICAL_FALLBACK_REASON,
        "originKind": "classical",
        "candidateId": approval["candidateId"],
        "rejectedAiCandidateIds": [record["id"] for record in approval["rejectedAiCandidates"]],
    }


def _inpaint_provenance_matches_candidate_evidence(
    store: ProjectStore,
    image: ImageAsset,
    provenance: dict[str, object],
) -> bool:
    """Bind mutable database provenance to the immutable candidate-set evidence."""
    from manga_localizer.services.inpaint_candidates import (
        _read_internal_candidate_manifest,
        inpaint_candidate_manifest_digest,
        validate_inpaint_candidate_evidence,
    )

    generation_id = provenance["generationId"]
    mask_checksum = provenance["maskChecksum"]
    candidate_id = provenance["candidateId"]
    origin_kind = provenance["originKind"]
    provider_ids = provenance["providerIds"]
    manifest_digest = provenance["candidateManifestDigest"]
    if origin_kind == "no-op":
        try:
            empty_manifest_digest = inpaint_candidate_manifest_digest(
                generation_id=str(generation_id),
                mask_checksum=str(mask_checksum),
                candidates=[],
            )
        except ProjectError:
            return False
        return (
            candidate_id == "primary"
            and provider_ids == []
            and manifest_digest == empty_manifest_digest
        )

    try:
        relative = safe_relative_path(image.relative_path).with_suffix(".png")
        manifest = _read_internal_candidate_manifest(store, relative)
        candidates = manifest["candidates"]
        anchored_digest = validate_inpaint_candidate_evidence(store, relative, manifest)
    except (OSError, ProjectError, TypeError, ValueError, UnsafePathError):
        return False
    if (
        manifest["generationId"] != generation_id
        or manifest["maskChecksum"] != mask_checksum
        or manifest.get("selectedId") != candidate_id
        or anchored_digest != manifest_digest
    ):
        return False
    matching_records = [
        record
        for record in candidates
        if isinstance(record, dict) and record.get("id") == candidate_id
    ]
    if len(matching_records) != 1:
        return False
    selected_record = matching_records[0]
    record_provider_ids = selected_record.get("providerIds")
    if (
        not isinstance(record_provider_ids, list)
        or any(
            not isinstance(provider_id, str) or not provider_id or len(provider_id) > 80
            for provider_id in record_provider_ids
        )
        or record_provider_ids != sorted(set(record_provider_ids))
    ):
        return False
    return (
        selected_record.get("artifactChecksum") == provenance["artifactChecksum"]
        and selected_record.get("originKind") == origin_kind
        and record_provider_ids == provider_ids
    )


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


def _generated_mask_path(store: ProjectStore, image: ImageAsset) -> Path:
    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    return resolve_write_target(
        store.root,
        Path("generated") / "masks" / relative,
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


def _generated_image_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            return opened.size
    except (OSError, ValueError) as error:
        raise ProjectError(
            "Generated visual-stage artifact could not be decoded; rerun the stage"
        ) from error


def _validate_generated_grid(image: ImageAsset, size: tuple[int, int]) -> None:
    width, height = size
    if image.width <= 0 or image.height <= 0:
        raise ProjectError("Source image has an invalid canonical grid")
    scale_x = width / image.width
    scale_y = height / image.height
    rounded = round(scale_x)
    if (
        not math.isclose(scale_x, scale_y, rel_tol=1e-6, abs_tol=1e-6)
        or not math.isclose(scale_x, rounded, rel_tol=1e-6, abs_tol=1e-6)
        or not 1 <= rounded <= 4
    ):
        raise ProjectError(
            "Generated visual-stage artifact does not use a supported canonical scale"
        )


def stage_artifact_checksums(
    store: ProjectStore,
    image: ImageAsset,
    stage: str,
) -> dict[str, str]:
    artifact_path = _generated_stage_path(store, image, stage)
    artifact_size = _generated_image_size(artifact_path)
    _validate_generated_grid(image, artifact_size)
    checksums = {"artifactChecksum": _sha256_file(artifact_path)}
    if stage == "inpaint":
        mask_path = _generated_mask_path(store, image)
        mask_size = _generated_image_size(mask_path)
        _validate_generated_grid(image, mask_size)
        if mask_size != artifact_size:
            raise ProjectError("Inpainted artifact and mask use different pixel grids")
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
        provenance_digest = record.get("provenanceDigest")
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
            if isinstance(provenance_digest, str) and _SHA256_RE.fullmatch(provenance_digest):
                normalized[stage]["provenanceDigest"] = provenance_digest
    return normalized


def require_current_accepted_stage_review(
    store: ProjectStore,
    image: ImageAsset,
    stage: str,
    *,
    require_ai: bool = False,
) -> dict[str, str]:
    """Require an accepted visual review that still matches the current files."""
    resource = f"image:{image.id}"
    if stage not in VISUAL_REVIEW_STAGES:
        raise ProjectError("Visual stage prerequisite is not supported")
    if (image.status or {}).get(stage) != "done":
        raise StagePrerequisiteConflict(
            f"{stage} must be completed and accepted before this operation",
            resource=resource,
            stage=stage,
            reason="stage-not-done",
        )
    review = stage_reviews(image).get(stage)
    if review is None:
        raise StagePrerequisiteConflict(
            f"{stage} must be accepted before this operation",
            resource=resource,
            stage=stage,
            reason="review-required",
        )
    if review.get("state") != "accepted":
        raise StagePrerequisiteConflict(
            f"{stage} was rejected and must be rebuilt and accepted",
            resource=resource,
            stage=stage,
            reason="review-rejected",
        )
    try:
        checksums = stage_artifact_checksums(store, image, stage)
    except ProjectError as error:
        raise StagePrerequisiteConflict(
            f"The accepted {stage} output is unavailable and must be rebuilt",
            resource=resource,
            stage=stage,
            reason="artifact-unavailable",
        ) from error
    mismatches = [key for key, checksum in checksums.items() if review.get(key) != checksum]
    if mismatches:
        raise StagePrerequisiteConflict(
            f"The accepted {stage} output changed and must be reviewed again",
            resource=resource,
            stage=stage,
            reason="checksum-mismatch",
            mismatches=mismatches,
        )
    if require_ai and stage == "inpaint":
        mask_path = _generated_mask_path(store, image)
        try:
            with Image.open(mask_path) as opened:
                has_repair_support = opened.convert("L").getbbox() is not None
        except (OSError, UnidentifiedImageError, ValueError) as error:
            raise StagePrerequisiteConflict(
                "The accepted inpaint mask is unavailable and must be rebuilt",
                resource=resource,
                stage=stage,
                reason="artifact-unavailable",
            ) from error
        if has_repair_support:
            current_provenance = current_inpaint_provenance(store, image, checksums)
            if current_provenance is None:
                raise StagePrerequisiteConflict(
                    "An accepted AI inpaint is required before this operation",
                    resource=resource,
                    stage=stage,
                    reason="ai-inpaint-required",
                )
            provenance, provenance_digest = current_provenance
            provider_ids = provenance["providerIds"]
            ai_generated = (
                provenance["originKind"] in {"direct-ai", "ai-derived"}
                and isinstance(provider_ids, list)
                and bool(provider_ids)
                and all(provider_id in AI_INPAINT_PROVIDERS for provider_id in provider_ids)
                and review.get("provenanceDigest") == provenance_digest
            )
            classical_approved = (
                not ai_generated
                and _current_classical_fallback_approval(
                    store,
                    image,
                    checksums=checksums,
                    review=review,
                    provenance=provenance,
                    provenance_digest=provenance_digest,
                )
                is not None
            )
            if not ai_generated and not classical_approved:
                raise StagePrerequisiteConflict(
                    "An accepted AI inpaint or approved classical fallback is required "
                    "before this operation",
                    resource=resource,
                    stage=stage,
                    reason="ai-inpaint-required",
                )
    return checksums


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
    unready_count = sum(
        not (region.confirmed and is_region_trusted(region)) for region in non_ignored_regions
    )
    if unready_count:
        raise ProjectError(
            "Cannot mark image as reviewed until every non-ignored region is confirmed "
            f"and trusted ({unready_count} not ready)"
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
    if stages & {"preprocess", "detection", "ocr", "inpaint"}:
        image.inpaint_classical_approval = None
        image.inpaint_ai_candidate_reviews = None
    clear_stage_reviews(image, stages)
    status = dict(image.status)
    for stage in stages:
        status[stage] = "pending"
        provider_key = _PROVIDER_STATUS_KEYS.get(stage)
        if provider_key:
            status[provider_key] = ""
    if "inpaint" in stages:
        image.inpaint_provenance = None
        status.pop("inpaintCandidate", None)
        status.pop("inpaintCandidates", None)
        status.pop("renderInputVariant", None)
        status.pop("renderScale", None)
        status.pop("renderedSize", None)
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
        from manga_localizer.services.inpaint_candidates import delete_inpaint_candidate_files

        delete_inpaint_candidate_files(store, relative)
    # Keep the last typeset plate when only typesetting is stale so a region-scoped
    # rerun can overlay selected boxes. Inpaint invalidation still removes it.
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
                with Image.open(io.BytesIO(data)) as opened:
                    suggestion = suggest_preprocess_profile(opened)
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
                    status={
                        "preprocess": "pending",
                        "detection": "pending",
                        "ocr": "pending",
                        "translation": "pending",
                        "inpaint": "pending",
                        "typeset": "pending",
                        "export": "pending",
                        "reviewState": "pending",
                        "reviewedAt": "",
                        "preprocessingProvider": "",
                        "detectorProvider": "",
                        "ocrProvider": "",
                        "translatorProvider": "",
                        "inpaintingProvider": "",
                        "typesettingProvider": "",
                        "preprocessSuggestion": suggestion,
                    },
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
        require_image_mutation_lineage(store, session, image, None)
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
    lineage: dict[str, object] | None = None,
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
            lineage_binding = require_image_mutation_lineage(
                store,
                session,
                image,
                lineage,
            )
            if lineage_binding is not None and stage == "inpaint":
                raise PageLineageConflict(
                    "Strict G8 clean plates must use the dedicated clean-plate review gate",
                    resource=f"image:{image.id}",
                    reason="g8-legacy-stage-review-blocked",
                )
            if lineage_binding is not None and stage == "typeset":
                raise PageLineageConflict(
                    "Strict G10 typesets must use the dedicated candidate review gate",
                    resource=f"image:{image.id}",
                    reason="g10-legacy-stage-review-blocked",
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
                if stage == "inpaint":
                    provenance = current_inpaint_provenance(store, image, checksums)
                    if (
                        state == "accepted"
                        and image.inpaint_provenance is not None
                        and (provenance is None)
                    ):
                        raise ProjectError(
                            "Inpainting provenance changed; rerun inpainting before accepting"
                        )
                    if provenance is not None:
                        checksums["provenanceDigest"] = provenance[1]
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
            previous_review = reviews.get(stage)
            # Every new inpaint review has a new resultRevision. An existing
            # fallback is anchored to the earlier review and must be re-approved,
            # even when the operator accepts the same pixels again.
            revoke_classical = stage == "inpaint" and image.inpaint_classical_approval is not None
            if revoke_classical:
                image.inpaint_classical_approval = None
                status = dict(image.status or {})
                status["typeset"] = "pending"
                status["export"] = "pending"
                image.status = status
                reviews.pop("typeset", None)
            if state == "pending":
                reviews.pop(stage, None)
                after = None
                artifact_changed = False
                reviewed_at = datetime.now(UTC)
            else:
                integrity_keys = {"artifactChecksum", "maskChecksum", "provenanceDigest"}
                artifact_changed = previous_review is not None and any(
                    previous_review.get(key) != checksums.get(key) for key in integrity_keys
                )
                reviewed_at = datetime.now(UTC)
                after = {
                    "state": state,
                    "reviewedAt": reviewed_at.isoformat(),
                    "resultRevision": image.revision,
                    **checksums,
                }
                reviews[stage] = after
            preprocess_requires_redraw = (
                stage == "preprocess"
                and state == "accepted"
                and (
                    previous_review is None
                    or previous_review.get("state") != "accepted"
                    or artifact_changed
                )
            )
            clear_dependents = state != "accepted" or artifact_changed or preprocess_requires_redraw
            cleared_dependents = dependent_reviews if clear_dependents else {}
            if clear_dependents:
                for dependent in cleared_dependents:
                    reviews.pop(dependent, None)
            if preprocess_requires_redraw:
                invalidate_image_pipeline(
                    store,
                    image,
                    {"inpaint", "typeset", "export"},
                )
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
            revision = add_revision(
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
            session.flush()
            record_stage_review_event(
                session,
                binding=lineage_binding,
                stage=stage,
                state=state,
                checksums=checksums,
                revision_id=revision.id,
                reviewed_at=reviewed_at,
            )
        store.write_snapshot()
    return image


def set_inpaint_classical_fallback(
    store: ProjectStore,
    image_id: str,
    *,
    state: str,
    reason: str | None,
    expected_revision: int,
) -> ImageAsset:
    """Approve or explicitly revoke one honest page-level classical fallback."""
    if state not in {"approved", "pending"}:
        raise ProjectError("Classical fallback state must be approved or pending")
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
            project = store.project(session)
            strict = project.settings.get("requireAIInpaintBeforeDownstream", False)
            if type(strict) is not bool:
                raise ProjectError("requireAIInpaintBeforeDownstream must be a boolean")
            before = public_inpaint_fallback(store, image)
            if state == "pending":
                if reason is not None:
                    raise ProjectError(
                        "Pending classical fallback cannot include approval evidence"
                    )
                if image.inpaint_classical_approval is None:
                    return image
                image.inpaint_classical_approval = None
                invalidate_image_pipeline(store, image, {"typeset", "export"})
            else:
                if not strict:
                    raise ProjectError("Classical fallback is available only in strict projects")
                if reason != CLASSICAL_FALLBACK_REASON:
                    raise ProjectError("Classical fallback reason must be ai-visible-artifacts")
                checksums = require_current_accepted_stage_review(store, image, "inpaint")
                mask_path = _generated_mask_path(store, image)
                try:
                    with Image.open(mask_path) as opened:
                        has_repair_support = opened.convert("L").getbbox() is not None
                except (OSError, UnidentifiedImageError, ValueError) as error:
                    raise ProjectError(
                        "Inpainting mask is unavailable; rerun inpainting"
                    ) from error
                if not has_repair_support:
                    raise ProjectError("Classical fallback requires a non-empty repair mask")
                current = current_inpaint_provenance(store, image, checksums)
                if current is None:
                    raise ProjectError("Inpainting evidence changed; rerun inpainting")
                provenance, provenance_digest = current
                if provenance["originKind"] != "classical":
                    raise ProjectError("Classical fallback requires a selected classical candidate")
                from manga_localizer.services.inpaint_candidates import (
                    _read_internal_candidate_manifest,
                    validate_inpaint_candidate_evidence,
                )

                relative = safe_relative_path(image.relative_path).with_suffix(".png")
                manifest = _read_internal_candidate_manifest(store, relative)
                manifest_digest = validate_inpaint_candidate_evidence(store, relative, manifest)
                if (
                    manifest.get("selectedId") != provenance["candidateId"]
                    or manifest.get("generationId") != provenance["generationId"]
                    or manifest_digest != provenance["candidateManifestDigest"]
                ):
                    raise ProjectError("Inpainting evidence changed; rerun inpainting")
                rejected = _strict_ai_candidate_records(manifest.get("candidates"))
                if not rejected:
                    raise ProjectError(
                        "Classical fallback requires at least one rejected AI candidate"
                    )
                authority = _current_ai_candidate_reviews_metadata(store, image)
                authority_reviews = authority[0]["reviews"] if authority is not None else None
                rejected_with_reviews = [
                    {
                        **candidate,
                        "state": "rejected",
                        "reviewedAt": review["reviewedAt"],
                    }
                    for candidate in rejected
                    for review in (authority_reviews if isinstance(authority_reviews, list) else [])
                    if isinstance(review, dict) and review.get("id") == candidate.get("id")
                ]
                if len(rejected_with_reviews) != len(rejected):
                    raise ProjectError("Every current AI candidate must be reviewed as rejected")
                review = stage_reviews(image).get("inpaint")
                if review is None or review.get("provenanceDigest") != provenance_digest:
                    raise ProjectError("The current inpaint review is not anchored to provenance")
                approval = {
                    "version": 1,
                    "state": "approved",
                    "kind": CLASSICAL_FALLBACK_KIND,
                    "reason": CLASSICAL_FALLBACK_REASON,
                    "originKind": "classical",
                    "candidateId": provenance["candidateId"],
                    "rejectedAiCandidates": rejected_with_reviews,
                    "generationId": provenance["generationId"],
                    "candidateManifestDigest": manifest_digest,
                    "artifactChecksum": checksums["artifactChecksum"],
                    "maskChecksum": checksums["maskChecksum"],
                    "provenanceDigest": provenance_digest,
                    "reviewResultRevision": review["resultRevision"],
                    "approvedAt": datetime.now(UTC).isoformat(),
                }
                approval["approvalDigest"] = hashlib.sha256(
                    json.dumps(
                        approval,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                existing = _normalize_classical_approval(image.inpaint_classical_approval)
                if existing is not None:
                    comparable = dict(existing)
                    comparable.pop("approvedAt", None)
                    comparable.pop("approvalDigest", None)
                    requested = dict(approval)
                    requested.pop("approvedAt", None)
                    requested.pop("approvalDigest", None)
                    if comparable == requested:
                        return image
                image.inpaint_classical_approval = approval
                status = dict(image.status or {})
                status["export"] = "pending"
                image.status = status
            image.revision += 1
            session.flush()
            add_revision(
                session,
                project,
                entity_type="image",
                entity_id=image.id,
                operation="inpaint-classical-fallback",
                before=before,
                after=public_inpaint_fallback(store, image),
            )
        store.write_snapshot()
    return image


def set_inpaint_ai_candidate_review(
    store: ProjectStore,
    image_id: str,
    *,
    state: str,
    expected_revision: int,
) -> ImageAsset:
    """Record or revoke the review of the current selected strict-AI candidate."""
    if state not in {"rejected", "pending"}:
        raise ProjectError("AI candidate review state must be rejected or pending")
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
            if (image.status or {}).get("inpaint") != "done":
                raise ProjectError("Cannot review an AI candidate until inpainting is done")
            checksums = stage_artifact_checksums(store, image, "inpaint")
            mask_path = _generated_mask_path(store, image)
            try:
                with Image.open(mask_path) as opened:
                    has_repair_support = opened.convert("L").getbbox() is not None
            except (OSError, UnidentifiedImageError, ValueError) as error:
                raise ProjectError("Inpainting mask is unavailable; rerun inpainting") from error
            if not has_repair_support:
                raise ProjectError("AI candidate review requires a non-empty repair mask")
            current = current_inpaint_provenance(store, image, checksums)
            if current is None:
                raise ProjectError("Inpainting evidence changed; rerun inpainting")
            provenance, _provenance_digest = current
            providers = provenance["providerIds"]
            if (
                provenance["originKind"] not in {"direct-ai", "ai-derived"}
                or not isinstance(providers, list)
                or not providers
                or any(provider not in AI_INPAINT_PROVIDERS for provider in providers)
            ):
                raise ProjectError("Only the current selected AI candidate can be reviewed")
            from manga_localizer.services.inpaint_candidates import (
                _read_internal_candidate_manifest,
                validate_inpaint_candidate_evidence,
            )

            relative = safe_relative_path(image.relative_path).with_suffix(".png")
            manifest = _read_internal_candidate_manifest(store, relative)
            manifest_digest = validate_inpaint_candidate_evidence(store, relative, manifest)
            strict_candidates = _strict_ai_candidate_records(manifest.get("candidates"))
            selected = next(
                (
                    candidate
                    for candidate in strict_candidates
                    if candidate["id"] == provenance["candidateId"]
                ),
                None,
            )
            if (
                selected is None
                or manifest.get("selectedId") != selected["id"]
                or manifest.get("generationId") != provenance["generationId"]
                or manifest_digest != provenance["candidateManifestDigest"]
            ):
                raise ProjectError("Inpainting evidence changed; rerun inpainting")
            existing = _current_ai_candidate_reviews_metadata(store, image)
            reviews = (
                [dict(review) for review in existing[0]["reviews"]]
                if existing is not None and isinstance(existing[0]["reviews"], list)
                else []
            )
            before = [str(review["id"]) for review in reviews]
            reviews = [review for review in reviews if review.get("id") != selected["id"]]
            if state == "rejected":
                reviews.append(
                    {
                        **selected,
                        "state": "rejected",
                        "reviewedAt": datetime.now(UTC).isoformat(),
                    }
                )
            reviews.sort(key=lambda review: str(review["id"]))
            after = [str(review["id"]) for review in reviews]
            if before == after and not (
                state == "pending" and image.inpaint_classical_approval is not None
            ):
                return image
            authority: dict[str, object] | None = None
            if reviews:
                authority = {
                    "version": 1,
                    "generationId": provenance["generationId"],
                    "candidateManifestDigest": manifest_digest,
                    "reviews": reviews,
                }
                authority["evidenceDigest"] = _ai_candidate_reviews_digest(authority)
            project = store.project(session)
            image.inpaint_ai_candidate_reviews = authority
            if state == "pending" and image.inpaint_classical_approval is not None:
                image.inpaint_classical_approval = None
                invalidate_image_pipeline(store, image, {"typeset", "export"})
            image.revision += 1
            session.flush()
            add_revision(
                session,
                project,
                entity_type="image",
                entity_id=image.id,
                operation="inpaint-ai-candidate-review",
                before={"rejectedCandidateIds": before},
                after={"rejectedCandidateIds": after},
            )
        store.write_snapshot()
    return image


def copy_file_without_overwrite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
