from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps
from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageCloudFullPageCandidate,
    PageCloudFullPageReview,
    PageGeneration,
    PageLineageEvent,
    Revision,
)
from manga_localizer.security import atomic_write_bytes, resolve_write_target
from manga_localizer.services.clean_plates import _g8_replay
from manga_localizer.services.masks import require_current_mask_acceptance
from manga_localizer.services.page_lineage import (
    PageLineageConflict,
    _append_event,
    require_current_background_classifications,
    require_current_text_present_quality_plate,
    require_image_mutation_lineage,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)

CLOUD_FULL_PAGE_PROFILE = "cloud-full-page-repair-v1"
STRICT_COMPOSITE_PROFILE = "strict-g8-mask-composite-v1"
NORMALIZATION_PROFILE = "canonical-whole-frame-normalization-v1"
REGISTRATION_PROFILE = "canonical-whole-frame-registration-v1"
CLOUD_FULL_PAGE_CHECKS = (
    "full-page-fidelity",
    "no-new-text",
    "no-new-objects",
    "unrelated-content-preserved",
    "target-source-text-unreadable",
    "no-white-or-gray-hole",
    "no-blur-band",
    "no-repeated-texture",
    "background-continuous",
    "structure-preserved",
)
_NAMESPACE = uuid.UUID("8814f8e7-301c-42bd-afbc-5816647149d6")
MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_NORMALIZED_BYTES = 48 * 1024 * 1024
MAX_METADATA_CHARS = 256 * 1024
MAX_RASTER_PIXELS = 32_000_000
ASPECT_LIMIT = 0.01
FIT_REJECT = "reject"
FIT_COVER_CROP = "cover-crop"
CLAIM_STATUS = "operator-attested-client-supplied-unverified"
_CLAIM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ +()-]*$")
_INVOCATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METADATA_KEYS = {
    "routeProfile",
    "invocationId",
    "provider",
    "tool",
    "modelVersion",
    "quotaClass",
    "providerParameters",
    "claimStatus",
    "promptSha256",
    "rawSha256",
    "rawMediaType",
    "normalizedSha256",
    "normalizationManifest",
    "normalizationDigest",
    "deltaManifest",
    "deltaDigest",
    "routeManifest",
    "routeChecksum",
    "ancestry",
    "expectedRevision",
    "lineage",
    "projectChecksum",
    "sourceChecksum",
    "g7Checksum",
    "legacyStateChecksum",
    "qualityChecksum",
    "backgroundChecksum",
    "maskArtifactId",
    "maskChecksum",
    "orderedInputs",
    "orderedInputDigest",
}
_LINEAGE_KEYS = {"runId", "pageGenerationId", "expectedSequence", "actor"}
_ACTOR_KEYS = {
    "actorKind",
    "actorId",
    "taskId",
    "threadId",
    "sessionId",
    "operationSource",
}
_PROVIDER_PARAMETER_KEYS = {
    "apiProfile",
    "responseMimeType",
    "inputRoles",
    "outputCount",
}
_LEGACY_ANCESTRY = {
    "originKind": "direct-ai",
    "providerClaimStatus": CLAIM_STATUS,
    "operatorAttestation": {
        "attested": True,
        "scope": "provider-tool-model-claim",
    },
}
_ANCESTRY = {
    "originKind": "deterministic-mask-composite",
    "providerRawOriginKind": "direct-ai",
    "providerClaimStatus": CLAIM_STATUS,
    "operatorAttestation": {
        "attested": True,
        "scope": "provider-tool-model-claim",
    },
    "derivation": {
        "profile": STRICT_COMPOSITE_PROFILE,
        "inputs": ["quality-plate", "provider-normalized", "accepted-g7-mask"],
    },
}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _validate_metadata_contract(metadata: dict[str, Any]) -> None:
    if set(metadata) != _METADATA_KEYS:
        raise ProjectError("Cloud ingest metadata must match the exact allowlisted contract")
    if not isinstance(metadata.get("normalizationManifest"), dict):
        raise ProjectError("Cloud normalization manifest must be an object")
    lineage = metadata.get("lineage")
    if not isinstance(lineage, dict) or set(lineage) != _LINEAGE_KEYS:
        raise ProjectError("Cloud ingest lineage metadata is invalid")
    actor = lineage.get("actor")
    if (
        not isinstance(actor, dict)
        or not {"actorKind", "operationSource"} <= set(actor) <= _ACTOR_KEYS
        or any(value is None for value in actor.values())
    ):
        raise ProjectError("Cloud ingest actor metadata is invalid")
    metadata["lineage"] = {
        "runId": lineage["runId"],
        "pageGenerationId": lineage["pageGenerationId"],
        "expectedSequence": lineage["expectedSequence"],
        "actor": {key: actor.get(key) for key in sorted(_ACTOR_KEYS)},
    }
    if metadata.get("ancestry") != _ANCESTRY:
        raise ProjectError("Cloud ancestry and provider attestation are invalid")
    if metadata.get("claimStatus") != CLAIM_STATUS:
        raise ProjectError("Cloud provider claim status is invalid")
    if metadata.get("quotaClass") not in {"included", "prepaid"}:
        raise ProjectError("Cloud quota class must be included or prepaid")
    _validate_normalization_route(
        metadata["normalizationManifest"].get("profile"), metadata.get("providerParameters")
    )
    invocation_id = metadata.get("invocationId")
    if not isinstance(invocation_id, str) or _INVOCATION_RE.fullmatch(invocation_id) is None:
        raise ProjectError("Cloud invocation identity is invalid")
    for field, maximum in (("provider", 80), ("tool", 80), ("modelVersion", 128)):
        value = metadata.get(field)
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= maximum
            or value != value.strip()
            or _CLAIM_RE.fullmatch(value) is None
        ):
            raise ProjectError(f"{field} cloud claim is invalid")
    if type(metadata.get("expectedRevision")) is not int or metadata["expectedRevision"] < 0:
        raise ProjectError("Cloud expected revision is invalid")


def _validate_provider_parameters(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _PROVIDER_PARAMETER_KEYS:
        raise ProjectError("Cloud provider parameters must match the exact allowlist")
    profile = value.get("apiProfile")
    if (
        not isinstance(profile, str)
        or not 1 <= len(profile) <= 80
        or _CLAIM_RE.fullmatch(profile) is None
    ):
        raise ProjectError("Cloud provider API profile is invalid")
    if value.get("responseMimeType") != "image/png":
        raise ProjectError("Cloud provider response media type must be image/png")
    if value.get("inputRoles") != ["quality-plate", "accepted-g7-mask"]:
        raise ProjectError("Cloud provider input roles are invalid")
    if value.get("outputCount") != 1:
        raise ProjectError("Cloud provider output count must be one")


def _validate_normalization_route(profile: object, provider_parameters: object) -> None:
    _validate_provider_parameters(provider_parameters)
    if profile == REGISTRATION_PROFILE and provider_parameters["apiProfile"] not in {
        "codex-native-subscription-v1",
        "cursor-native-subscription-v1",
    }:
        raise ProjectError("Cloud registration requires a native subscription route")


def _relative(generation_id: str, candidate_id: str, name: str) -> Path:
    return Path("generated") / "lineage-cloud-full-pages" / generation_id / candidate_id / name


def _target(store: ProjectStore, relative: Path) -> Path:
    return resolve_write_target(store.root, relative, protected_roots=(store.source_root,))


def _read_verified(path: Path, checksum: str, *, message: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PageLineageConflict(
            message, resource=str(path.name), reason="g8-cloud-artifact-missing"
        ) from error
    if _sha256(payload) != checksum:
        raise PageLineageConflict(
            message, resource=str(path.name), reason="g8-cloud-artifact-checksum-mismatch"
        )
    return payload


def _publish_once(path: Path, payload: bytes) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as error:
            raise ProjectError("Cloud full-page artifact could not be verified") from error
        if current != payload:
            raise PageLineageConflict(
                "Cloud full-page artifact path already contains different bytes",
                resource=path.name,
                reason="g8-cloud-artifact-no-clobber",
            )
        return
    atomic_write_bytes(path, payload)


def _project_checksum(store: ProjectStore, session) -> str:
    project = store.project(session)
    return _digest({"projectId": project.id, "projectRevision": project.revision})


def _candidate_revision_after(
    *,
    candidate_id: str,
    generation_id: str,
    image_id: str,
    job_id: str,
    job_item_id: str,
    raw_checksum: str,
    normalized_checksum: str,
    request_revision: int,
) -> dict[str, Any]:
    return {
        "candidateId": candidate_id,
        "generationId": generation_id,
        "imageId": image_id,
        "jobId": job_id,
        "jobItemId": job_item_id,
        "routeProfile": CLOUD_FULL_PAGE_PROFILE,
        "claimStatus": CLAIM_STATUS,
        "rawChecksum": raw_checksum,
        "normalizedChecksum": normalized_checksum,
        "requestRevision": request_revision,
    }


def _review_revision_after(
    *, review_id: str, candidate: PageCloudFullPageCandidate, state: str, reason: str, checks: list
) -> dict[str, Any]:
    return {
        "reviewId": review_id,
        "candidateId": candidate.id,
        "candidateChecksum": candidate.normalized_checksum,
        "state": state,
        "reason": reason,
        "checks": checks,
    }


def _event_actor(event: PageLineageEvent) -> dict[str, str | None]:
    return {
        "actorKind": event.actor_kind,
        "actorId": event.actor_id,
        "taskId": event.task_id,
        "threadId": event.thread_id,
        "sessionId": event.session_id,
        "operationSource": event.operation_source,
    }


def _candidate_identity(row: PageCloudFullPageCandidate) -> dict[str, Any]:
    return {
        "id": row.id,
        "generationId": row.generation_id,
        "imageId": row.image_id,
        "sequence": row.sequence,
        "routeProfile": row.route_profile,
        "invocationId": row.invocation_id,
        "jobId": row.job_id,
        "jobItemId": row.job_item_id,
        "revisionId": row.revision_id,
        "parentChecksum": row.parent_checksum,
        "legacyStateChecksum": row.legacy_state_checksum,
        "projectChecksum": row.project_checksum,
        "sourceChecksum": row.source_checksum,
        "qualityChecksum": row.quality_checksum,
        "backgroundChecksum": row.background_checksum,
        "maskArtifactId": row.mask_artifact_id,
        "maskChecksum": row.mask_checksum,
        "provider": row.provider,
        "tool": row.tool,
        "modelVersion": row.model_version,
        "claimStatus": row.ancestry.get("providerClaimStatus"),
        "promptSha256": row.prompt_sha256,
        "orderedInputs": row.ordered_input_manifest,
        "orderedInputDigest": row.ordered_input_digest,
        "rawChecksum": row.raw_checksum,
        "rawRelativePath": row.raw_relative_path,
        "rawMediaType": row.raw_media_type,
        "rawGrid": {"width": row.raw_width, "height": row.raw_height},
        "normalizedChecksum": row.normalized_checksum,
        "normalizedRelativePath": row.normalized_relative_path,
        "normalizedMediaType": row.normalized_media_type,
        "normalizedGrid": {"width": row.normalized_width, "height": row.normalized_height},
        "normalizationManifest": row.normalization_manifest,
        "normalizationDigest": row.normalization_digest,
        "deltaManifest": row.delta_manifest,
        "routeChecksum": row.route_checksum,
        "deltaDigest": row.delta_digest,
        "routeManifest": row.route_manifest,
        "parameterHash": row.parameter_hash,
        "ancestry": row.ancestry,
        "createdAt": _timestamp(row.created_at),
    }


def _review_identity(row: PageCloudFullPageReview) -> dict[str, Any]:
    return {
        "id": row.id,
        "generationId": row.generation_id,
        "imageId": row.image_id,
        "sequence": row.sequence,
        "candidateId": row.candidate_id,
        "revisionId": row.revision_id,
        "state": row.state,
        "reason": row.reason,
        "parentChecksum": row.parent_checksum,
        "candidateChecksum": row.candidate_checksum,
        "checks": row.checks,
        "reviewer": row.reviewer,
        "createdAt": _timestamp(row.created_at),
    }


def _cloud_state(
    legacy_state_checksum: str,
    candidates: list[PageCloudFullPageCandidate],
    reviews: list[PageCloudFullPageReview],
) -> str:
    if not candidates and not reviews:
        return legacy_state_checksum
    return _digest(
        {
            "profile": CLOUD_FULL_PAGE_PROFILE,
            "legacyStateChecksum": legacy_state_checksum,
            "candidates": [_candidate_identity(row) for row in candidates],
            "reviews": [_review_identity(row) for row in reviews],
        }
    )


def _cloud_rows(session, generation: PageGeneration):
    candidates = list(
        session.scalars(
            select(PageCloudFullPageCandidate)
            .where(PageCloudFullPageCandidate.generation_id == generation.id)
            .order_by(PageCloudFullPageCandidate.sequence)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(PageCloudFullPageReview)
            .where(PageCloudFullPageReview.generation_id == generation.id)
            .order_by(PageCloudFullPageReview.sequence)
        ).all()
    )
    return candidates, reviews


def _require_closed_rejected_legacy_prefix(legacy: dict[str, Any], generation_id: str) -> None:
    if legacy["terminal"] is not None:
        raise PageLineageConflict(
            "Cloud route cannot follow an accepted legacy G8 terminal",
            resource=f"page-generation:{generation_id}",
            reason="g8-cloud-replay-invalid",
        )
    candidates = legacy["candidates"]
    reviews = legacy["reviews"]
    reviewed_ids = [row.candidate_id for row in reviews]
    if (
        legacy["openItemId"] is not None
        or len(candidates) != len(reviews)
        or any(row.state != "rejected" for row in reviews)
        or any(candidate.id not in reviewed_ids for candidate in candidates)
        or len(set(reviewed_ids)) != len(reviewed_ids)
    ):
        raise PageLineageConflict(
            "Cloud route requires a direct start or a fully closed rejected legacy prefix",
            resource=f"page-generation:{generation_id}",
            reason="g8-cloud-legacy-prefix-open",
        )


def _validate_candidate_file(store: ProjectStore, row: PageCloudFullPageCandidate) -> Path:
    expected = _relative(row.generation_id, row.id, "normalized.png")
    if Path(row.normalized_relative_path) != expected:
        raise PageLineageConflict(
            "Cloud candidate path binding changed",
            resource=f"cloud-candidate:{row.id}",
            reason="g8-cloud-replay-invalid",
        )
    path = _target(store, expected)
    payload = _read_verified(
        path, row.normalized_checksum, message="Cloud candidate is unavailable"
    )
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            facts = (image.format, image.mode, image.size)
    except OSError as error:
        raise PageLineageConflict(
            "Cloud candidate is not a valid raster",
            resource=f"cloud-candidate:{row.id}",
            reason="g8-cloud-replay-invalid",
        ) from error
    if facts != ("PNG", "RGB", (row.normalized_width, row.normalized_height)):
        raise PageLineageConflict(
            "Cloud candidate raster facts changed",
            resource=f"cloud-candidate:{row.id}",
            reason="g8-cloud-replay-invalid",
        )
    return path


def _validate_candidate_evidence(
    store: ProjectStore,
    row: PageCloudFullPageCandidate,
    *,
    quality_bytes: bytes,
    mask_bytes: bytes,
    ordered_inputs: list[dict[str, Any]],
) -> None:
    raw_path = _target(store, _relative(row.generation_id, row.id, "raw.bin"))
    if Path(row.raw_relative_path) != _relative(row.generation_id, row.id, "raw.bin"):
        raise PageLineageConflict(
            "Cloud raw artifact path binding changed",
            resource=f"cloud-candidate:{row.id}",
            reason="g8-cloud-replay-invalid",
        )
    raw = _read_verified(raw_path, row.raw_checksum, message="Cloud raw artifact is unavailable")
    try:
        if not isinstance(row.normalization_manifest, dict):
            raise ProjectError("Cloud normalization manifest must be an object")
        profile = row.normalization_manifest.get("profile")
        if row.route_manifest.get("maskComposite") is not True and profile != NORMALIZATION_PROFILE:
            raise ProjectError("Legacy cloud evidence cannot use registration")
        if profile == REGISTRATION_PROFILE:
            _validate_normalization_route(profile, row.route_manifest.get("providerParameters"))
        provider_normalized, normalization, raw_grid, raw_media_type = _normalize_for_profile(
            raw,
            (row.normalized_width, row.normalized_height),
            quality=quality_bytes,
            mask=mask_bytes,
            profile=profile,
        )
    except ProjectError as error:
        raise PageLineageConflict(
            "Cloud normalization does not replay",
            resource=f"cloud-candidate:{row.id}",
            reason="g8-cloud-replay-invalid",
        ) from error
    normalized = _read_verified(
        _target(store, Path(row.normalized_relative_path)),
        row.normalized_checksum,
        message="Cloud normalized artifact is unavailable",
    )
    if row.route_manifest.get("maskComposite") is True:
        canonical, composite = _strict_mask_composite(
            quality_bytes, provider_normalized, mask_bytes
        )
        delta = _delta_manifest(quality_bytes, canonical, mask_bytes)
        quota_class = row.route_manifest.get("quotaClass")
        provider_parameters = row.route_manifest.get("providerParameters")
        try:
            _validate_provider_parameters(provider_parameters)
        except ProjectError as error:
            raise PageLineageConflict(
                "Cloud provider parameters do not replay",
                resource=f"cloud-candidate:{row.id}",
                reason="g8-cloud-replay-invalid",
            ) from error
        route = _strict_route_manifest(
            normalization,
            composite,
            delta,
            ordered_inputs,
            quota_class=quota_class,
            provider_parameters=provider_parameters,
        )
        ancestry = _ANCESTRY
        strict_outside = delta["outsideMaskChangedPixelCount"] == 0 and quota_class in {
            "included",
            "prepaid",
        }
    else:
        # Preserve replay for immutable v1 rows created before the strict G8
        # composite contract. New ingest never accepts this legacy route.
        canonical = provider_normalized
        delta = _delta_manifest(quality_bytes, canonical, mask_bytes)
        route = _legacy_route_manifest(normalization, delta, ordered_inputs)
        ancestry = _LEGACY_ANCESTRY
        strict_outside = True
    if (
        canonical != normalized
        or not strict_outside
        or row.raw_media_type != raw_media_type
        or (row.raw_width, row.raw_height) != raw_grid
        or row.ordered_input_manifest != ordered_inputs
        or row.ordered_input_digest != _digest(ordered_inputs)
        or row.normalization_manifest != normalization
        or row.normalization_digest != _digest(normalization)
        or row.delta_manifest != delta
        or row.delta_digest != _digest(delta)
        or row.route_manifest != route
        or row.route_checksum != _digest(route)
        or row.ancestry != ancestry
    ):
        raise PageLineageConflict(
            "Cloud candidate evidence does not reproduce",
            resource=f"cloud-candidate:{row.id}",
            reason="g8-cloud-replay-invalid",
        )


def cloud_full_page_replay(
    store: ProjectStore, session, image: ImageAsset, generation: PageGeneration
) -> dict[str, Any]:
    legacy = _g8_replay(store, session, image, generation)
    candidates, reviews = _cloud_rows(session, generation)
    events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G8_cloudFullPage",
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    if not candidates and not reviews:
        if events:
            raise PageLineageConflict(
                "Cloud full-page lineage events have no matching candidate evidence",
                resource=f"page-generation:{generation.id}",
                reason="g8-cloud-replay-invalid",
            )
        return {
            "legacy": legacy,
            "candidates": [],
            "reviews": [],
            "stateChecksum": legacy["stateChecksum"],
            "terminal": None,
        }
    _require_closed_rejected_legacy_prefix(legacy, generation.id)
    if any(row.sequence != index for index, row in enumerate(candidates, 1)) or any(
        row.sequence != index for index, row in enumerate(reviews, 1)
    ):
        raise PageLineageConflict(
            "Cloud full-page sequence is not contiguous",
            resource=f"page-generation:{generation.id}",
            reason="g8-cloud-replay-invalid",
        )
    expected_operations: list[str] = []
    for index in range(len(candidates)):
        expected_operations.extend(
            (
                "cloud-full-page-job-enqueued",
                "cloud-full-page-candidate-produced",
                "cloud-full-page-job-completed",
            )
        )
        if index < len(reviews):
            expected_operations.append("cloud-full-page-stage-review")
    if [event.operation for event in events] != expected_operations:
        raise PageLineageConflict(
            "Cloud full-page lineage event sequence is invalid",
            resource=f"page-generation:{generation.id}",
            reason="g8-cloud-replay-invalid",
        )
    cloud_start = events[0].sequence if events else generation.next_sequence
    all_events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(PageLineageEvent.generation_id == generation.id)
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    cloud_tail = [event for event in all_events if event.sequence >= cloud_start]
    downstream_seen = False
    for event in cloud_tail:
        if event.gate == "G8_cloudFullPage":
            if downstream_seen:
                raise PageLineageConflict(
                    "Cloud full-page events are interleaved with downstream evidence",
                    resource=f"event:{event.id}",
                    reason="g8-cloud-replay-invalid",
                )
        else:
            downstream_seen = True
    _g7, current_mask, quality, current_background, quality_bytes, mask_bytes = _input_bindings(
        store, session, image, generation
    )
    with Image.open(io.BytesIO(quality_bytes)) as quality_image:
        width, height = quality_image.size
    ordered_inputs = [
        {
            "position": 1,
            "role": "quality-plate",
            "sha256": quality["checksum"],
            "width": width,
            "height": height,
        },
        {
            "position": 2,
            "role": "accepted-g7-mask",
            "sha256": current_mask.mask_checksum,
            "width": current_mask.width,
            "height": current_mask.height,
        },
    ]
    for row in candidates:
        expected_ancestry = (
            _ANCESTRY if row.route_manifest.get("maskComposite") is True else _LEGACY_ANCESTRY
        )
        if (
            row.generation_id != generation.id
            or row.image_id != image.id
            or row.route_profile != CLOUD_FULL_PAGE_PROFILE
            or row.legacy_state_checksum != legacy["stateChecksum"]
            or row.parent_checksum != legacy["g7Checksum"]
            or row.source_checksum != image.checksum
            or row.quality_checksum != quality["checksum"]
            or row.background_checksum != current_background
            or row.mask_artifact_id != current_mask.id
            or row.mask_checksum != current_mask.mask_checksum
            or row.provider is None
            or row.tool is None
            or row.model_version is None
            or _CLAIM_RE.fullmatch(row.provider) is None
            or _CLAIM_RE.fullmatch(row.tool) is None
            or _CLAIM_RE.fullmatch(row.model_version) is None
            or row.ancestry != expected_ancestry
            or not _is_sha256(row.prompt_sha256)
            or row.normalized_media_type != "image/png"
        ):
            raise PageLineageConflict(
                "Cloud candidate is not seeded by the exact legacy G8 state",
                resource=f"cloud-candidate:{row.id}",
                reason="g8-cloud-replay-invalid",
            )
        _validate_candidate_file(store, row)
        _validate_candidate_evidence(
            store,
            row,
            quality_bytes=quality_bytes,
            mask_bytes=mask_bytes,
            ordered_inputs=ordered_inputs,
        )
    if (
        len(reviews) not in {len(candidates), len(candidates) - 1}
        or any(review.candidate_id != candidates[index].id for index, review in enumerate(reviews))
        or any(review.state == "accepted" for review in reviews[:-1])
        or (reviews and reviews[-1].state == "accepted" and len(reviews) != len(candidates))
        or (
            len(reviews) == len(candidates) - 1
            and any(review.state != "rejected" for review in reviews)
        )
    ):
        raise PageLineageConflict(
            "Cloud full-page row cardinality is invalid",
            resource=f"page-generation:{generation.id}",
            reason="g8-cloud-replay-invalid",
        )
    event_cursor = 0
    for index, candidate in enumerate(candidates):
        publication = events[event_cursor : event_cursor + 3]
        event_cursor += 3
        review = reviews[index] if index < len(reviews) else None
        review_event = None
        if review is not None:
            review_event = events[event_cursor]
            event_cursor += 1
        job = session.get(Job, candidate.job_id)
        item = session.get(JobItem, candidate.job_item_id)
        revision = session.get(Revision, candidate.revision_id)
        actor = (
            job.lineage_context.get("actor")
            if job and isinstance(job.lineage_context, dict)
            else None
        )
        enqueue_sequence = publication[0].sequence if publication else None
        expected_job_context = {
            "generationId": generation.id,
            "sourceChecksum": image.checksum,
            "expectedSequence": enqueue_sequence,
            "actor": actor,
        }
        prior_state = legacy["stateChecksum"] if index == 0 else reviews[index - 1].state_checksum
        candidate_state = _cloud_state(legacy["stateChecksum"], candidates[: index + 1], [])
        replay_metadata = {
            "routeProfile": candidate.route_profile,
            "invocationId": candidate.invocation_id,
            "provider": candidate.provider,
            "tool": candidate.tool,
            "modelVersion": candidate.model_version,
            "claimStatus": candidate.ancestry.get("providerClaimStatus"),
            "promptSha256": candidate.prompt_sha256,
            "rawSha256": candidate.raw_checksum,
            "rawMediaType": candidate.raw_media_type,
            "normalizedSha256": candidate.normalized_checksum,
            "normalizationManifest": candidate.normalization_manifest,
            "normalizationDigest": candidate.normalization_digest,
            "deltaManifest": candidate.delta_manifest,
            "deltaDigest": candidate.delta_digest,
            "routeManifest": candidate.route_manifest,
            "routeChecksum": candidate.route_checksum,
            "ancestry": candidate.ancestry,
            "expectedRevision": revision.after.get("requestRevision") if revision else None,
            "lineage": {
                "runId": generation.run_id,
                "pageGenerationId": generation.id,
                "expectedSequence": enqueue_sequence,
                "actor": actor,
            },
            "projectChecksum": candidate.project_checksum,
            "sourceChecksum": candidate.source_checksum,
            "g7Checksum": candidate.parent_checksum,
            "legacyStateChecksum": candidate.legacy_state_checksum,
            "qualityChecksum": candidate.quality_checksum,
            "backgroundChecksum": candidate.background_checksum,
            "maskArtifactId": candidate.mask_artifact_id,
            "maskChecksum": candidate.mask_checksum,
            "orderedInputs": candidate.ordered_input_manifest,
            "orderedInputDigest": candidate.ordered_input_digest,
        }
        if candidate.route_manifest.get("maskComposite") is True:
            replay_metadata["quotaClass"] = candidate.route_manifest.get("quotaClass")
            replay_metadata["providerParameters"] = candidate.route_manifest.get(
                "providerParameters"
            )
        if (
            job is None
            or item is None
            or revision is None
            or not isinstance(actor, dict)
            or len(publication) != 3
            or candidate.state_checksum != candidate_state
            or job.project_id != generation.project_id
            or job.kind != "cloud-full-page-repair"
            or job.status != "completed"
            or job.progress != 1.0
            or job.total != 1
            or job.completed != 1
            or job.options != {"routeProfile": CLOUD_FULL_PAGE_PROFILE}
            or job.lineage_context != expected_job_context
            or item.job_id != job.id
            or item.image_id != image.id
            or item.region_id is not None
            or item.position != 0
            or item.status != "completed"
            or item.progress != 1.0
            or item.output
            != {
                "candidateId": candidate.id,
                "rawChecksum": candidate.raw_checksum,
                "normalizedChecksum": candidate.normalized_checksum,
                "routeChecksum": candidate.route_checksum,
            }
            or item.started_at != candidate.created_at
            or item.finished_at != candidate.created_at
            or job.created_at != candidate.created_at
            or job.updated_at != candidate.created_at
            or revision.project_id != generation.project_id
            or revision.entity_type != "page-cloud-full-page-candidate"
            or revision.entity_id != candidate.id
            or revision.operation != "create"
            or type(revision.after.get("requestRevision")) is not int
            or revision.before != {}
            or revision.after
            != _candidate_revision_after(
                candidate_id=candidate.id,
                generation_id=generation.id,
                image_id=image.id,
                job_id=job.id,
                job_item_id=item.id,
                raw_checksum=candidate.raw_checksum,
                normalized_checksum=candidate.normalized_checksum,
                request_revision=revision.after.get("requestRevision"),
            )
            or candidate.project_checksum
            != _digest(
                {
                    "projectId": generation.project_id,
                    "projectRevision": revision.project_revision - 1,
                }
            )
            or candidate.parameter_hash
            != _digest(
                {
                    "metadata": replay_metadata,
                    "raw": candidate.raw_checksum,
                    "normalized": candidate.normalized_checksum,
                }
            )
        ):
            raise PageLineageConflict(
                "Cloud candidate job, item, or revision evidence is invalid",
                resource=f"cloud-candidate:{candidate.id}",
                reason="g8-cloud-replay-invalid",
            )
        base_event_evidence = {
            "routeProfile": CLOUD_FULL_PAGE_PROFILE,
            "claimStatus": CLAIM_STATUS,
            "candidateId": candidate.id,
            "rawChecksum": candidate.raw_checksum,
            "normalizedChecksum": candidate.normalized_checksum,
            "routeChecksum": candidate.route_checksum,
            "stateChecksum": candidate_state,
        }
        event_matrix = (
            (
                "cloud-full-page-job-enqueued",
                prior_state,
                prior_state,
                None,
                "job-enqueued",
            ),
            (
                "cloud-full-page-candidate-produced",
                prior_state,
                candidate_state,
                revision.id,
                "candidate-produced",
            ),
            (
                "cloud-full-page-job-completed",
                candidate_state,
                candidate_state,
                None,
                "review-required",
            ),
        )
        for event, (operation, input_checksum, output_checksum, revision_id, reason) in zip(
            publication, event_matrix, strict=True
        ):
            if (
                event.operation != operation
                or event.gate != "G8_cloudFullPage"
                or event.state != "pending"
                or _event_actor(event) != actor
                or event.input_checksum != input_checksum
                or event.output_checksum != output_checksum
                or event.parent_checksum != legacy["g7Checksum"]
                or event.stage != "inpaint"
                or event.provider != candidate.provider
                or event.model_version != candidate.model_version
                or event.parameter_hash != candidate.parameter_hash
                or event.job_id != job.id
                or event.job_item_id != item.id
                or event.revision_id != revision_id
                or event.decision is not None
                or event.reason != reason
                or event.git_commit is not None
                or event.evidence != {"eventType": operation, **base_event_evidence}
                or event.started_at != candidate.created_at
                or event.finished_at != candidate.created_at
            ):
                raise PageLineageConflict(
                    "Cloud publication event does not match exact candidate evidence",
                    resource=f"event:{event.id}",
                    reason="g8-cloud-replay-invalid",
                )
        if review is not None:
            review_revision = session.get(Revision, review.revision_id)
            expected_checks = [
                {"check": check, "passed": review.state == "accepted"}
                for check in CLOUD_FULL_PAGE_CHECKS
            ]
            if review.state == "rejected":
                expected_checks = review.checks
            failed_checks = [
                entry["check"] for entry in review.checks if entry.get("passed") is False
            ]
            expected_reason = (
                "cloud-full-page-repair-complete"
                if review.state == "accepted"
                else "multiple-visual-failures"
                if len(failed_checks) > 1
                else failed_checks[0]
                if failed_checks
                else None
            )
            if (
                review_event is None
                or review.generation_id != generation.id
                or review.image_id != image.id
                or review.candidate_id != candidate.id
                or review.parent_checksum != candidate_state
                or review.candidate_checksum != candidate.normalized_checksum
                or review.reviewer != _event_actor(review_event)
                or review.checks != expected_checks
                or [entry.get("check") for entry in review.checks] != list(CLOUD_FULL_PAGE_CHECKS)
                or any(set(entry) != {"check", "passed"} for entry in review.checks)
                or any(type(entry.get("passed")) is not bool for entry in review.checks)
                or review.reason != expected_reason
                or review_revision is None
                or review_revision.project_id != generation.project_id
                or review_revision.entity_type != "page-cloud-full-page-review"
                or review_revision.entity_id != review.id
                or review_revision.operation != review.state
                or review_revision.before != {}
                or review_revision.after
                != _review_revision_after(
                    review_id=review.id,
                    candidate=candidate,
                    state=review.state,
                    reason=review.reason,
                    checks=review.checks,
                )
                or review_event.operation != "cloud-full-page-stage-review"
                or review_event.gate != "G8_cloudFullPage"
                or review_event.state != review.state
                or review_event.input_checksum != candidate_state
                or review_event.output_checksum != review.state_checksum
                or review_event.parent_checksum != legacy["g7Checksum"]
                or review_event.stage != "inpaint"
                or review_event.provider != candidate.provider
                or review_event.model_version != candidate.model_version
                or review_event.parameter_hash != candidate.parameter_hash
                or review_event.job_id is not None
                or review_event.job_item_id is not None
                or review_event.revision_id != review_revision.id
                or review_event.decision != f"cloud-full-page-{review.state}"
                or review_event.reason != review.reason
                or review_event.git_commit is not None
                or review_event.evidence
                != {
                    "eventType": "cloud-full-page-stage-review",
                    "routeProfile": CLOUD_FULL_PAGE_PROFILE,
                    "claimStatus": CLAIM_STATUS,
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.normalized_checksum,
                    "g7Checksum": legacy["g7Checksum"],
                    "checks": review.checks,
                    "reviewId": review.id,
                    "stateChecksum": review.state_checksum,
                }
                or review_event.started_at != review.created_at
                or review_event.finished_at != review.created_at
            ):
                raise PageLineageConflict(
                    "Cloud review event or revision does not match exact review evidence",
                    resource=f"cloud-review:{review.id}",
                    reason="g8-cloud-replay-invalid",
                )
    if event_cursor != len(events):
        raise PageLineageConflict(
            "Cloud full-page lineage event sequence is invalid",
            resource=f"page-generation:{generation.id}",
            reason="g8-cloud-replay-invalid",
        )
    expected_state = _cloud_state(legacy["stateChecksum"], candidates, reviews)
    if reviews:
        reviewed_state = (
            expected_state
            if len(reviews) == len(candidates)
            else _cloud_state(legacy["stateChecksum"], candidates[: len(reviews)], reviews)
        )
        if reviews[-1].state_checksum != reviewed_state:
            raise PageLineageConflict(
                "Cloud full-page state checksum is invalid",
                resource=f"page-generation:{generation.id}",
                reason="g8-cloud-replay-invalid",
            )
    accepted = [review for review in reviews if review.state == "accepted"]
    return {
        "legacy": legacy,
        "candidates": candidates,
        "reviews": reviews,
        "stateChecksum": expected_state,
        "terminal": accepted[-1] if accepted else None,
    }


def _input_bindings(store: ProjectStore, session, image: ImageAsset, generation: PageGeneration):
    g7_checksum, mask = require_current_mask_acceptance(store, session, image, generation)
    if mask is None:
        raise PageLineageConflict(
            "Cloud full-page repair requires an accepted non-empty G7 mask",
            resource=f"image:{image.id}",
            reason="g8-cloud-mask-required",
        )
    quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
    background_checksum, _ = require_current_background_classifications(
        store, session, image, generation
    )
    quality_path = Path(quality["path"])
    quality_bytes = _read_verified(
        quality_path, quality["checksum"], message="Quality plate changed"
    )
    mask_path = resolve_write_target(
        store.root, mask.relative_path, protected_roots=(store.source_root,)
    )
    mask_bytes = _read_verified(mask_path, mask.mask_checksum, message="Accepted mask changed")
    return g7_checksum, mask, quality, background_checksum, quality_bytes, mask_bytes


def cloud_full_page_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = session.scalar(
            select(PageGeneration).where(
                PageGeneration.image_id == image.id,
                PageGeneration.project_id == image.project_id,
                PageGeneration.state == "active",
            )
        )
        if generation is None:
            raise PageLineageConflict(
                "Cloud route requires an active generation",
                resource=f"image:{image.id}",
                reason="active-generation-missing",
            )
        g7, mask, quality, background, quality_bytes, _mask_bytes = _input_bindings(
            store, session, image, generation
        )
        replay = cloud_full_page_replay(store, session, image, generation)
        with Image.open(io.BytesIO(quality_bytes)) as quality_image:
            width, height = quality_image.size
        ordered = [
            {
                "position": 1,
                "role": "quality-plate",
                "sha256": quality["checksum"],
                "width": width,
                "height": height,
            },
            {
                "position": 2,
                "role": "accepted-g7-mask",
                "sha256": mask.mask_checksum,
                "width": mask.width,
                "height": mask.height,
            },
        ]
        return {
            "routeProfile": CLOUD_FULL_PAGE_PROFILE,
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "runId": generation.run_id,
            "nextSequence": generation.next_sequence,
            "projectChecksum": _project_checksum(store, session),
            "sourceChecksum": image.checksum,
            "g7Checksum": g7,
            "legacyStateChecksum": replay["legacy"]["stateChecksum"],
            "stateChecksum": replay["stateChecksum"],
            "qualityChecksum": quality["checksum"],
            "backgroundChecksum": background,
            "maskArtifactId": mask.id,
            "maskChecksum": mask.mask_checksum,
            "orderedInputs": ordered,
            "orderedInputDigest": _digest(ordered),
            "targetGrid": {"width": width, "height": height},
            "candidates": [
                public_cloud_candidate(row, replay["reviews"]) for row in replay["candidates"]
            ],
            "acceptedCandidateId": next(
                (review.candidate_id for review in replay["reviews"] if review.state == "accepted"),
                None,
            ),
            "fallbackEnabled": replay["legacy"]["fallbackEnabled"],
        }


def public_cloud_candidate(
    row: PageCloudFullPageCandidate, reviews: list[PageCloudFullPageReview]
) -> dict[str, Any]:
    review = next((item for item in reviews if item.candidate_id == row.id), None)
    return {
        "candidateId": row.id,
        "sequence": row.sequence,
        "routeProfile": row.route_profile,
        "invocationId": row.invocation_id,
        "provider": row.provider,
        "tool": row.tool,
        "modelVersion": row.model_version,
        "quotaClass": row.route_manifest.get("quotaClass"),
        "providerParameters": row.route_manifest.get("providerParameters"),
        "parameterHash": row.parameter_hash,
        "promptSha256": row.prompt_sha256,
        "rawChecksum": row.raw_checksum,
        "normalizedChecksum": row.normalized_checksum,
        "width": row.normalized_width,
        "height": row.normalized_height,
        "deltaManifest": row.delta_manifest,
        "routeChecksum": row.route_checksum,
        "review": None
        if review is None
        else {
            "id": review.id,
            "state": review.state,
            "reason": review.reason,
            "checks": review.checks,
            "reviewer": review.reviewer,
            "createdAt": review.created_at,
        },
        "createdAt": row.created_at,
    }


def _aspect_error(width: int, height: int, target_grid: tuple[int, int]) -> float:
    target_ratio = target_grid[0] / target_grid[1]
    return abs((width / height) - target_ratio) / target_ratio


def _cover_crop_box(
    raw_size: tuple[int, int], target_grid: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    raw_width, raw_height = raw_size
    target_ratio = target_grid[0] / target_grid[1]
    best: tuple[float, int, tuple[int, int, int, int]] | None = None
    if raw_width / raw_height >= target_ratio:
        base = round(raw_height * target_ratio)
        for crop_width in range(max(1, base - 3), min(raw_width, base + 3) + 1):
            error = _aspect_error(crop_width, raw_height, target_grid)
            if error > ASPECT_LIMIT:
                continue
            left = (raw_width - crop_width) // 2
            candidate = (error, crop_width, (left, 0, crop_width, raw_height))
            if best is None or candidate < best:
                best = candidate
    else:
        base = round(raw_width / target_ratio)
        for crop_height in range(max(1, base - 3), min(raw_height, base + 3) + 1):
            error = _aspect_error(raw_width, crop_height, target_grid)
            if error > ASPECT_LIMIT:
                continue
            top = (raw_height - crop_height) // 2
            candidate = (error, crop_height, (0, top, raw_width, crop_height))
            if best is None or candidate < best:
                best = candidate
    return None if best is None else best[2]


def _normalize(
    raw: bytes,
    target_grid: tuple[int, int],
    *,
    fit: str = FIT_REJECT,
) -> tuple[bytes, dict[str, Any], tuple[int, int], str]:
    if fit not in {FIT_REJECT, FIT_COVER_CROP}:
        raise ProjectError("Unknown cloud normalization fit")
    crop_box: tuple[int, int, int, int] | None = None
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.width * opened.height > MAX_RASTER_PIXELS:
                raise ProjectError("Raw cloud image exceeds the raster pixel limit")
            raw_media_type = {
                "PNG": "image/png",
                "JPEG": "image/jpeg",
                "WEBP": "image/webp",
            }.get(opened.format or "")
            if raw_media_type is None:
                raise ProjectError("Raw cloud image media type is unsupported")
            orientation = int(opened.getexif().get(274, 1))
            if orientation != 1:
                raise ProjectError("Raw cloud image must already be upright")
            raw_size = opened.size
            if _aspect_error(*raw_size, target_grid) > ASPECT_LIMIT:
                if fit != FIT_COVER_CROP:
                    raise ProjectError(
                        "Raw cloud image aspect differs from the target by more than 1%"
                    )
                crop_box = _cover_crop_box(raw_size, target_grid)
                if crop_box is None:
                    raise ProjectError(
                        "Raw cloud image aspect differs from the target by more than 1%"
                    )
                fitted = (crop_box[2], crop_box[3])
                if _aspect_error(*fitted, target_grid) > ASPECT_LIMIT:
                    raise ProjectError(
                        "Raw cloud image aspect differs from the target by more than 1%"
                    )
            opened.load()
            image = ImageOps.exif_transpose(opened).convert("RGB")
            working_size = raw_size
            if crop_box is not None:
                left, top, crop_width, crop_height = crop_box
                image = image.crop((left, top, left + crop_width, top + crop_height))
                working_size = image.size
            if image.size != target_grid:
                image = image.resize(target_grid, Image.Resampling.LANCZOS)
            normalized = _png_bytes(image)
    except ProjectError:
        raise
    except (OSError, ValueError) as error:
        raise ProjectError("Raw cloud image is not a supported raster") from error
    manifest = {
        "profile": "canonical-whole-frame-normalization-v1",
        "sourceGrid": {"width": raw_size[0], "height": raw_size[1]},
        "targetGrid": {"width": target_grid[0], "height": target_grid[1]},
        "orientation": "upright",
        "colorMode": "RGB",
        "resize": "lanczos-whole-frame" if working_size != target_grid else "none",
        "crop": crop_box is not None,
        "padding": False,
        "maskComposite": False,
        "localPatch": False,
        "contentAwareTransform": False,
        "output": "canonical-png-v1",
    }
    if crop_box is not None:
        left, top, crop_width, crop_height = crop_box
        manifest["fittedGrid"] = {"width": crop_width, "height": crop_height}
        manifest["cropBox"] = {
            "x": left,
            "y": top,
            "width": crop_width,
            "height": crop_height,
        }
    return normalized, manifest, raw_size, raw_media_type


def _strict_mask_composite(
    quality: bytes, provider_normalized: bytes, mask: bytes
) -> tuple[bytes, dict[str, Any]]:
    try:
        with (
            Image.open(io.BytesIO(quality)) as source_image,
            Image.open(io.BytesIO(provider_normalized)) as provider_image,
            Image.open(io.BytesIO(mask)) as mask_image,
        ):
            source = np.asarray(source_image.convert("RGB"), dtype=np.uint8)
            provider = np.asarray(provider_image.convert("RGB"), dtype=np.uint8)
            binary_mask = np.asarray(mask_image.convert("L"), dtype=np.uint8) > 0
    except OSError as error:
        raise ProjectError("Cloud composite inputs could not be decoded") from error
    if source.shape != provider.shape or binary_mask.shape != source.shape[:2]:
        raise ProjectError("Cloud composite inputs do not share the canonical target grid")
    composite = source.copy()
    composite[binary_mask] = provider[binary_mask]
    payload = _png_bytes(Image.fromarray(composite, mode="RGB"))
    manifest = {
        "profile": STRICT_COMPOSITE_PROFILE,
        "targetGrid": {"width": int(source.shape[1]), "height": int(source.shape[0])},
        "qualitySha256": _sha256(quality),
        "providerNormalizedSha256": _sha256(provider_normalized),
        "maskSha256": _sha256(mask),
        "maskRule": "nonzero-inside-provider-zero-outside-quality",
        "outsideMaskSource": "quality-plate",
        "output": "canonical-png-v1",
    }
    return payload, manifest


def _normalize_for_profile(
    raw: bytes,
    target_grid: tuple[int, int],
    *,
    quality: bytes,
    mask: bytes,
    profile: object = NORMALIZATION_PROFILE,
) -> tuple[bytes, dict[str, Any], tuple[int, int], str]:
    if profile not in (NORMALIZATION_PROFILE, REGISTRATION_PROFILE):
        raise ProjectError("Unknown cloud normalization profile")
    if profile == NORMALIZATION_PROFILE:
        return _normalize(raw, target_grid, fit=FIT_COVER_CROP)
    if min(target_grid) <= 0 or target_grid[0] * target_grid[1] > MAX_RASTER_PIXELS:
        raise ProjectError("Cloud target exceeds the raster pixel limit")
    normalized, manifest, raw_grid, media_type = _normalize(raw, target_grid, fit=FIT_COVER_CROP)
    from manga_localizer.services.cloud_registration import register_whole_frame

    registered, registration = register_whole_frame(quality, normalized, mask)
    return (
        registered,
        {
            **manifest,
            "profile": REGISTRATION_PROFILE,
            "contentAwareTransform": True,
            "registration": registration,
        },
        raw_grid,
        media_type,
    )


def _legacy_route_manifest(
    normalization: dict[str, Any],
    delta: dict[str, Any],
    ordered_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "profile": CLOUD_FULL_PAGE_PROFILE,
        "wholeFrame": True,
        "outsideMaskChangesAllowed": True,
        "normalizationDigest": _digest(normalization),
        "deltaDigest": _digest(delta),
        "orderedInputDigest": _digest(ordered_inputs),
    }


def _strict_route_manifest(
    normalization: dict[str, Any],
    composite: dict[str, Any],
    delta: dict[str, Any],
    ordered_inputs: list[dict[str, Any]],
    *,
    quota_class: object,
    provider_parameters: object,
) -> dict[str, Any]:
    return {
        "profile": CLOUD_FULL_PAGE_PROFILE,
        "providerOutputWholeFrame": True,
        "acceptedCandidate": STRICT_COMPOSITE_PROFILE,
        "outsideMaskChangesAllowed": False,
        "maskComposite": True,
        "quotaClass": quota_class,
        "providerParameters": provider_parameters,
        "providerParameterDigest": _digest(provider_parameters),
        "normalizationDigest": _digest(normalization),
        "compositeManifest": composite,
        "compositeDigest": _digest(composite),
        "deltaDigest": _digest(delta),
        "orderedInputDigest": _digest(ordered_inputs),
    }


def _delta_manifest(quality: bytes, normalized: bytes, mask: bytes) -> dict[str, Any]:
    try:
        with (
            Image.open(io.BytesIO(quality)) as source_image,
            Image.open(io.BytesIO(normalized)) as candidate_image,
            Image.open(io.BytesIO(mask)) as mask_image,
        ):
            source = np.asarray(source_image.convert("RGB"), dtype=np.uint8)
            candidate = np.asarray(candidate_image.convert("RGB"), dtype=np.uint8)
            binary_mask = np.asarray(mask_image.convert("L"), dtype=np.uint8) > 0
    except OSError as error:
        raise ProjectError("Cloud input rasters could not be decoded") from error
    if source.shape != candidate.shape or binary_mask.shape != source.shape[:2]:
        raise ProjectError("Cloud input rasters do not share the canonical target grid")
    changed = np.any(source != candidate, axis=2)
    inside = int(np.count_nonzero(changed & binary_mask))
    outside = int(np.count_nonzero(changed & ~binary_mask))
    total = int(np.count_nonzero(changed))
    support = int(np.count_nonzero(binary_mask))
    pixels = int(changed.size)
    return {
        "profile": "full-page-delta-v1",
        "pixelCount": pixels,
        "maskSupportCount": support,
        "changedPixelCount": total,
        "insideMaskChangedPixelCount": inside,
        "outsideMaskChangedPixelCount": outside,
        "changedRatio": total / pixels,
        "insideMaskChangedRatio": inside / support if support else 0.0,
        "outsideMaskChangedRatio": outside / (pixels - support) if pixels > support else 0.0,
    }


def ingest_cloud_full_page_candidate(
    store: ProjectStore,
    image_id: str,
    *,
    raw_bytes: bytes,
    normalized_bytes: bytes,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    _validate_metadata_contract(metadata)
    if metadata.get("routeProfile") != CLOUD_FULL_PAGE_PROFILE:
        raise ProjectError("Cloud ingest route profile is invalid")
    invocation_id = metadata.get("invocationId")
    if not raw_bytes or not normalized_bytes:
        raise ProjectError("Cloud ingest requires raw and normalized image bytes")
    if len(raw_bytes) > MAX_RAW_BYTES or len(normalized_bytes) > MAX_NORMALIZED_BYTES:
        raise ProjectError("Cloud ingest image exceeds the byte limit")
    request_digest = _digest(
        {"metadata": metadata, "raw": _sha256(raw_bytes), "normalized": _sha256(normalized_bytes)}
    )
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            existing = session.scalar(
                select(PageCloudFullPageCandidate).where(
                    PageCloudFullPageCandidate.image_id == image.id,
                    PageCloudFullPageCandidate.invocation_id == invocation_id,
                )
            )
            if existing is not None:
                if (
                    existing.raw_checksum != _sha256(raw_bytes)
                    or existing.normalized_checksum != _sha256(normalized_bytes)
                    or (
                        existing.normalization_manifest != metadata.get("normalizationManifest")
                        or existing.normalization_digest != metadata.get("normalizationDigest")
                        or existing.normalization_digest
                        != _digest(metadata.get("normalizationManifest"))
                    )
                ):
                    raise PageLineageConflict(
                        "Cloud invocation retry bytes or normalization changed",
                        resource=f"cloud-invocation:{invocation_id}",
                        reason="g8-cloud-invocation-conflict",
                    )
                # A successful first ingest necessarily advances the image revision and
                # lineage sequence.  A process-level retry therefore carries a refreshed
                # CAS context even though the invocation and immutable image bytes are
                # identical.  Reuse the stored, replay-validated candidate; never rewrite
                # its original metadata or relax byte identity.
                _publish_once(_target(store, Path(existing.raw_relative_path)), raw_bytes)
                _publish_once(
                    _target(store, Path(existing.normalized_relative_path)),
                    normalized_bytes,
                )
                replay = cloud_full_page_replay(
                    store, session, image, session.get(PageGeneration, existing.generation_id)
                )
                return public_cloud_candidate(existing, replay["reviews"])
            expected_revision = metadata.get("expectedRevision")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before cloud ingest",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            lineage = metadata.get("lineage")
            generation, actor, expected_sequence = require_image_mutation_lineage(
                store, session, image, lineage
            )
            legacy = _g8_replay(store, session, image, generation)
            _require_closed_rejected_legacy_prefix(legacy, generation.id)
            cloud_candidates, cloud_reviews = _cloud_rows(session, generation)
            if cloud_reviews and any(row.state == "accepted" for row in cloud_reviews):
                raise PageLineageConflict(
                    "Accepted cloud G8 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g8-cloud-accepted",
                )
            if cloud_candidates and (
                len(cloud_reviews) != len(cloud_candidates)
                or any(row.state != "rejected" for row in cloud_reviews)
            ):
                raise PageLineageConflict(
                    "A pending or unreviewed cloud candidate already exists",
                    resource=f"image:{image.id}",
                    reason="g8-cloud-candidate-exists",
                )
            g7, mask, quality, background, quality_bytes, mask_bytes = _input_bindings(
                store, session, image, generation
            )
            with Image.open(io.BytesIO(quality_bytes)) as quality_image:
                target_grid = quality_image.size
            ordered = [
                {
                    "position": 1,
                    "role": "quality-plate",
                    "sha256": quality["checksum"],
                    "width": target_grid[0],
                    "height": target_grid[1],
                },
                {
                    "position": 2,
                    "role": "accepted-g7-mask",
                    "sha256": mask.mask_checksum,
                    "width": mask.width,
                    "height": mask.height,
                },
            ]
            project_checksum = _project_checksum(store, session)
            expected_bindings = {
                "projectChecksum": project_checksum,
                "sourceChecksum": image.checksum,
                "g7Checksum": g7,
                "legacyStateChecksum": legacy["stateChecksum"],
                "qualityChecksum": quality["checksum"],
                "backgroundChecksum": background,
                "maskArtifactId": mask.id,
                "maskChecksum": mask.mask_checksum,
                "orderedInputs": ordered,
                "orderedInputDigest": _digest(ordered),
            }
            if any(metadata.get(key) != value for key, value in expected_bindings.items()):
                raise PageLineageConflict(
                    "Cloud ingest bindings changed",
                    resource=f"image:{image.id}",
                    reason="g8-cloud-cas-mismatch",
                )
            for key in ("promptSha256", "rawSha256", "normalizedSha256"):
                if not _is_sha256(metadata.get(key)):
                    raise ProjectError(f"{key} must be a SHA-256 digest")
            if metadata["rawSha256"] != _sha256(raw_bytes) or metadata[
                "normalizedSha256"
            ] != _sha256(normalized_bytes):
                raise ProjectError("Uploaded cloud artifact checksum does not match metadata")
            provider_normalized, normalization_manifest, raw_grid, raw_media_type = (
                _normalize_for_profile(
                    raw_bytes,
                    target_grid,
                    quality=quality_bytes,
                    mask=mask_bytes,
                    profile=metadata["normalizationManifest"].get("profile"),
                )
            )
            canonical, composite_manifest = _strict_mask_composite(
                quality_bytes, provider_normalized, mask_bytes
            )
            if canonical != normalized_bytes:
                raise ProjectError("Uploaded normalized image is not the strict G8 mask composite")
            if metadata.get("rawMediaType") != raw_media_type:
                raise ProjectError("Raw cloud image media type does not match metadata")
            if metadata.get("normalizationManifest") != normalization_manifest or metadata.get(
                "normalizationDigest"
            ) != _digest(normalization_manifest):
                raise ProjectError("Cloud normalization manifest is invalid")
            delta = _delta_manifest(quality_bytes, normalized_bytes, mask_bytes)
            if delta["outsideMaskChangedPixelCount"] != 0:
                raise ProjectError("Cloud candidate changed pixels outside the accepted G7 mask")
            if metadata.get("deltaManifest") != delta or metadata.get("deltaDigest") != _digest(
                delta
            ):
                raise ProjectError("Cloud full-page delta manifest is invalid")
            route_manifest = _strict_route_manifest(
                normalization_manifest,
                composite_manifest,
                delta,
                ordered,
                quota_class=metadata["quotaClass"],
                provider_parameters=metadata["providerParameters"],
            )
            if metadata.get("routeManifest") != route_manifest or metadata.get(
                "routeChecksum"
            ) != _digest(route_manifest):
                raise ProjectError("Cloud route manifest is invalid")
            ancestry = metadata.get("ancestry")
            if ancestry != _ANCESTRY:
                raise ProjectError(
                    "Cloud ancestry must identify the raw AI output and strict composite"
                )
            prior_state = (
                cloud_reviews[-1].state_checksum if cloud_reviews else legacy["stateChecksum"]
            )
            candidate_sequence = len(cloud_candidates) + 1
            candidate_id = str(uuid.uuid5(_NAMESPACE, f"{generation.id}:{invocation_id}"))
            job_id = str(uuid.uuid5(_NAMESPACE, f"job:{generation.id}:{invocation_id}"))
            item_id = str(uuid.uuid5(_NAMESPACE, f"item:{generation.id}:{invocation_id}"))
            now = datetime.now(UTC)
            job = Job(
                id=job_id,
                project_id=image.project_id,
                kind="cloud-full-page-repair",
                status="completed",
                progress=1.0,
                total=1,
                completed=1,
                options={"routeProfile": CLOUD_FULL_PAGE_PROFILE},
                lineage_context={
                    "generationId": generation.id,
                    "sourceChecksum": image.checksum,
                    "expectedSequence": expected_sequence,
                    "actor": actor,
                },
                created_at=now,
                updated_at=now,
            )
            item = JobItem(
                id=item_id,
                job_id=job_id,
                image_id=image.id,
                region_id=None,
                position=0,
                status="completed",
                progress=1.0,
                output={
                    "candidateId": candidate_id,
                    "rawChecksum": _sha256(raw_bytes),
                    "normalizedChecksum": _sha256(normalized_bytes),
                    "routeChecksum": _digest(route_manifest),
                },
                started_at=now,
                finished_at=now,
            )
            session.add_all([job, item])
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-cloud-full-page-candidate",
                entity_id=candidate_id,
                operation="create",
                before={},
                after=_candidate_revision_after(
                    candidate_id=candidate_id,
                    generation_id=generation.id,
                    image_id=image.id,
                    job_id=job_id,
                    job_item_id=item_id,
                    raw_checksum=_sha256(raw_bytes),
                    normalized_checksum=_sha256(normalized_bytes),
                    request_revision=expected_revision,
                ),
            )
            session.flush()
            candidate = PageCloudFullPageCandidate(
                id=candidate_id,
                generation_id=generation.id,
                image_id=image.id,
                job_id=job_id,
                job_item_id=item_id,
                revision_id=revision.id,
                sequence=candidate_sequence,
                route_profile=CLOUD_FULL_PAGE_PROFILE,
                invocation_id=invocation_id,
                parent_checksum=g7,
                legacy_state_checksum=legacy["stateChecksum"],
                project_checksum=project_checksum,
                source_checksum=image.checksum,
                quality_checksum=quality["checksum"],
                background_checksum=background,
                mask_artifact_id=mask.id,
                mask_checksum=mask.mask_checksum,
                provider=metadata["provider"],
                tool=metadata["tool"],
                model_version=metadata["modelVersion"],
                prompt_sha256=metadata["promptSha256"],
                ordered_input_manifest=ordered,
                ordered_input_digest=_digest(ordered),
                raw_checksum=_sha256(raw_bytes),
                raw_relative_path=_relative(generation.id, candidate_id, "raw.bin").as_posix(),
                raw_media_type=raw_media_type,
                raw_width=raw_grid[0],
                raw_height=raw_grid[1],
                normalized_checksum=_sha256(normalized_bytes),
                normalized_relative_path=_relative(
                    generation.id, candidate_id, "normalized.png"
                ).as_posix(),
                normalized_media_type="image/png",
                normalized_width=target_grid[0],
                normalized_height=target_grid[1],
                normalization_manifest=normalization_manifest,
                normalization_digest=_digest(normalization_manifest),
                delta_manifest=delta,
                delta_digest=_digest(delta),
                route_manifest=route_manifest,
                route_checksum=_digest(route_manifest),
                parameter_hash=request_digest,
                state_checksum="0" * 64,
                ancestry=ancestry,
                created_at=now,
            )
            candidate.state_checksum = _cloud_state(
                legacy["stateChecksum"], [*cloud_candidates, candidate], []
            )
            session.add(candidate)
            session.flush()
            raw_path = _target(store, _relative(generation.id, candidate_id, "raw.bin"))
            normalized_path = _target(
                store, _relative(generation.id, candidate_id, "normalized.png")
            )
            _publish_once(raw_path, raw_bytes)
            _publish_once(normalized_path, normalized_bytes)
            for offset, (operation, state) in enumerate(
                (
                    ("cloud-full-page-job-enqueued", "pending"),
                    ("cloud-full-page-candidate-produced", "pending"),
                    ("cloud-full-page-job-completed", "pending"),
                )
            ):
                _append_event(
                    session,
                    generation,
                    operation=operation,
                    gate="G8_cloudFullPage",
                    state=state,
                    actor=actor,
                    input_checksum=(prior_state if offset < 2 else candidate.state_checksum),
                    output_checksum=(prior_state if offset == 0 else candidate.state_checksum),
                    parent_checksum=g7,
                    stage="inpaint",
                    provider=candidate.provider,
                    model_version=candidate.model_version,
                    parameter_hash=request_digest,
                    job_id=job_id,
                    job_item_id=item_id,
                    revision_id=revision.id if offset == 1 else None,
                    reason=(
                        "job-enqueued"
                        if offset == 0
                        else "candidate-produced"
                        if offset == 1
                        else "review-required"
                    ),
                    evidence={
                        "eventType": operation,
                        "routeProfile": CLOUD_FULL_PAGE_PROFILE,
                        "claimStatus": CLAIM_STATUS,
                        "candidateId": candidate.id,
                        "rawChecksum": candidate.raw_checksum,
                        "normalizedChecksum": candidate.normalized_checksum,
                        "routeChecksum": candidate.route_checksum,
                        "stateChecksum": candidate.state_checksum,
                    },
                    started_at=now,
                    finished_at=now,
                    expected_sequence=expected_sequence + offset,
                )
        store.write_snapshot()
    return public_cloud_candidate(candidate, [])


def record_cloud_full_page_review(
    store: ProjectStore,
    image_id: str,
    *,
    candidate_id: str,
    observed_checksum: str,
    checks: list[dict[str, Any]],
    decision: str,
    reason: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> dict[str, Any]:
    by_key = {
        entry.get("check"): entry.get("passed") for entry in checks if isinstance(entry, dict)
    }
    if (
        len(checks) != 10
        or set(by_key) != set(CLOUD_FULL_PAGE_CHECKS)
        or [entry.get("check") for entry in checks] != list(CLOUD_FULL_PAGE_CHECKS)
        or any(type(value) is not bool for value in by_key.values())
        or any(set(entry) != {"check", "passed"} for entry in checks)
    ):
        raise PageLineageConflict(
            "Cloud review must contain the exact ten checks",
            resource="cloud-review",
            reason="g8-cloud-review-checks-invalid",
        )
    failed = [key for key in CLOUD_FULL_PAGE_CHECKS if not by_key[key]]
    if decision == "accept":
        if failed or reason != "cloud-full-page-repair-complete":
            raise PageLineageConflict(
                "Cloud acceptance requires all ten checks",
                resource="cloud-review",
                reason="g8-cloud-review-decision-invalid",
            )
        state = "accepted"
    else:
        expected_reason = (
            "multiple-visual-failures" if len(failed) > 1 else (failed[0] if failed else None)
        )
        if decision != "reject" or not failed or reason != expected_reason:
            raise PageLineageConflict(
                "Cloud rejection reason must identify failed checks",
                resource="cloud-review",
                reason="g8-cloud-review-decision-invalid",
            )
        state = "rejected"
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before cloud review",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            generation, actor, expected_sequence = require_image_mutation_lineage(
                store, session, image, lineage
            )
            replay = cloud_full_page_replay(store, session, image, generation)
            candidate = session.get(PageCloudFullPageCandidate, candidate_id)
            if (
                candidate is None
                or candidate not in replay["candidates"]
                or candidate.normalized_checksum != observed_checksum
            ):
                raise PageLineageConflict(
                    "Cloud review does not bind the immutable candidate",
                    resource=f"image:{image.id}",
                    reason="g8-cloud-review-candidate-invalid",
                )
            if any(row.candidate_id == candidate.id for row in replay["reviews"]):
                raise PageLineageConflict(
                    "Cloud candidate already has a review",
                    resource=f"cloud-candidate:{candidate.id}",
                    reason="g8-cloud-candidate-already-reviewed",
                )
            _validate_candidate_file(store, candidate)
            image.revision += 1
            review_id = str(uuid.uuid4())
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-cloud-full-page-review",
                entity_id=review_id,
                operation=state,
                before={},
                after=_review_revision_after(
                    review_id=review_id,
                    candidate=candidate,
                    state=state,
                    reason=reason,
                    checks=checks,
                ),
            )
            session.flush()
            now = datetime.now(UTC)
            review = PageCloudFullPageReview(
                id=review_id,
                generation_id=generation.id,
                image_id=image.id,
                candidate_id=candidate.id,
                revision_id=revision.id,
                sequence=len(replay["reviews"]) + 1,
                state=state,
                reason=reason,
                parent_checksum=candidate.state_checksum,
                candidate_checksum=candidate.normalized_checksum,
                checks=checks,
                reviewer=actor,
                state_checksum="0" * 64,
                created_at=now,
            )
            review.state_checksum = _cloud_state(
                replay["legacy"]["stateChecksum"],
                replay["candidates"],
                [*replay["reviews"], review],
            )
            session.add(review)
            session.flush()
            _append_event(
                session,
                generation,
                operation="cloud-full-page-stage-review",
                gate="G8_cloudFullPage",
                state=state,
                actor=actor,
                input_checksum=candidate.state_checksum,
                output_checksum=review.state_checksum,
                parent_checksum=replay["legacy"]["g7Checksum"],
                stage="inpaint",
                provider=candidate.provider,
                model_version=candidate.model_version,
                parameter_hash=candidate.parameter_hash,
                revision_id=revision.id,
                decision=f"cloud-full-page-{state}",
                reason=reason,
                evidence={
                    "eventType": "cloud-full-page-stage-review",
                    "routeProfile": CLOUD_FULL_PAGE_PROFILE,
                    "claimStatus": CLAIM_STATUS,
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.normalized_checksum,
                    "g7Checksum": replay["legacy"]["g7Checksum"],
                    "checks": checks,
                    "reviewId": review.id,
                    "stateChecksum": review.state_checksum,
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return {
        "imageId": image.id,
        "imageRevision": image.revision,
        "generationId": generation.id,
        "stateChecksum": review.state_checksum,
        "review": {
            "id": review.id,
            "state": review.state,
            "reason": review.reason,
            "checks": review.checks,
        },
    }


def cloud_full_page_artifact_path(store: ProjectStore, image_id: str, candidate_id: str) -> Path:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        candidate = session.get(PageCloudFullPageCandidate, candidate_id)
        if image is None or candidate is None or candidate.image_id != image.id:
            raise ProjectError("Cloud full-page candidate was not found")
        generation = session.get(PageGeneration, candidate.generation_id)
        if generation is None or generation.state != "active":
            raise ProjectError("Cloud full-page candidate was not found")
        cloud_full_page_replay(store, session, image, generation)
        return _validate_candidate_file(store, candidate)


def cloud_full_page_raw_artifact_path(
    store: ProjectStore, image_id: str, candidate_id: str
) -> tuple[Path, str]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        candidate = session.get(PageCloudFullPageCandidate, candidate_id)
        if image is None or candidate is None or candidate.image_id != image.id:
            raise ProjectError("Cloud full-page candidate was not found")
        generation = session.get(PageGeneration, candidate.generation_id)
        if generation is None or generation.state != "active":
            raise ProjectError("Cloud full-page candidate was not found")
        cloud_full_page_replay(store, session, image, generation)
        expected = _relative(generation.id, candidate.id, "raw.bin")
        if Path(candidate.raw_relative_path) != expected:
            raise PageLineageConflict(
                "Cloud raw artifact path binding changed",
                resource=f"cloud-candidate:{candidate.id}",
                reason="g8-cloud-replay-invalid",
            )
        path = _target(store, expected)
        _read_verified(path, candidate.raw_checksum, message="Cloud raw artifact is unavailable")
        return path, candidate.raw_media_type


def current_cloud_full_page_acceptance(
    store: ProjectStore, session, image: ImageAsset, generation: PageGeneration
):
    replay = cloud_full_page_replay(store, session, image, generation)
    terminal = replay["terminal"]
    if terminal is None:
        return None
    candidate = session.get(PageCloudFullPageCandidate, terminal.candidate_id)
    if candidate is None or terminal.candidate_checksum != candidate.normalized_checksum:
        raise PageLineageConflict(
            "Accepted cloud candidate is stale",
            resource=f"image:{image.id}",
            reason="g8-cloud-review-candidate-invalid",
        )
    return replay["stateChecksum"], _validate_candidate_file(store, candidate), candidate
