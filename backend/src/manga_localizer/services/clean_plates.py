from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageCleanPlateCandidate,
    PageCleanPlateReview,
    PageCloudFullPageCandidate,
    PageCloudFullPageReview,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    Revision,
    TextRegion,
)
from manga_localizer.imaging.inpainting import inpaint as opencv_inpaint
from manga_localizer.imaging.layered_structure import (
    LayeredStructureError,
    canonicalize_layered_structure_guide,
    render_layered_structure,
)
from manga_localizer.security import atomic_write_bytes, resolve_write_target
from manga_localizer.services.inpaint_candidates import (
    load_layered_structure_snapshots,
    snapshot_layered_structure_references,
)
from manga_localizer.services.masks import (
    eligible_mask_regions,
    require_current_mask_acceptance,
)
from manga_localizer.services.page_lineage import (
    JobMutationBinding,
    PageLineageConflict,
    _append_event,
    _safe_actor,
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

CLEAN_PLATE_CHECKS = (
    "outside-mask-unchanged",
    "source-text-unreadable",
    "no-white-or-gray-hole",
    "no-blur-band",
    "no-repeated-texture",
    "background-continuous",
    "structure-preserved",
)

_CHECK_REASON = {
    "outside-mask-unchanged": "outside-mask-changed",
    "source-text-unreadable": "residual-text-readable",
    "no-white-or-gray-hole": "hole-or-block",
    "no-blur-band": "blur-band",
    "no-repeated-texture": "repeated-texture",
    "background-continuous": "background-discontinuous",
    "structure-preserved": "structure-damaged",
}
_COMPLEX_CATEGORIES = {"complex-lineart", "illustration/character"}
_ROUTE_BY_CATEGORY = {
    "white-solid": "deterministic-solid",
    "black-solid": "deterministic-solid",
    "other-solid": "deterministic-solid",
    "simple-gradient": "controlled-gradient",
    "screentone": "screentone-preserving",
    "complex-lineart": "ai-inpaint-redraw",
    "illustration/character": "ai-inpaint-redraw",
}
_AI_PROVIDER_ALIASES = {"lama": "lama-onnx", "lama-onnx": "lama-onnx"}
_CONNECTED_CONTRACT_UNION = "connected-contract-union-v1"
_CLASSIFIED_SOLID_FILL = "classified-color"
_CLASSIFIED_SOLID_OPAQUE_MASK = "classified-color-opaque-mask"
_LAYERED_STRUCTURE_MODEL = "layered-structure-guide-v1"
_ALLOWED_G8_OPERATIONS = {
    "clean-plate-fallback-enabled",
    "clean-plate-fallback-disabled",
    "inpaint-job-enqueued",
    "clean-plate-candidate-produced",
    "inpaint-job-completed",
    "inpaint-job-failed",
    "clean-plate-stage-review",
}


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_legacy_route_mutable(session, generation: PageGeneration) -> None:
    cloud_candidate_id = session.scalar(
        select(PageCloudFullPageCandidate.id)
        .where(PageCloudFullPageCandidate.generation_id == generation.id)
        .limit(1)
    )
    cloud_review_id = session.scalar(
        select(PageCloudFullPageReview.id)
        .where(PageCloudFullPageReview.generation_id == generation.id)
        .limit(1)
    )
    cloud_event_id = session.scalar(
        select(PageLineageEvent.id)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.gate == "G8_cloudFullPage",
        )
        .limit(1)
    )
    if cloud_candidate_id is not None or cloud_review_id is not None or cloud_event_id is not None:
        raise PageLineageConflict(
            "Legacy G8 mutations are unavailable after the cloud route starts",
            resource=f"page-generation:{generation.id}",
            reason="g8-cloud-route-started",
        )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _candidate_relative(generation_id: str, candidate_id: str) -> Path:
    return Path("generated") / "lineage-clean-plates" / generation_id / f"{candidate_id}.png"


def _candidate_target(store: ProjectStore, generation_id: str, candidate_id: str) -> Path:
    return resolve_write_target(
        store.root,
        _candidate_relative(generation_id, candidate_id),
        protected_roots=(store.source_root,),
    )


def _active(session, image: ImageAsset) -> PageGeneration:
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.image_id == image.id,
            PageGeneration.project_id == image.project_id,
            PageGeneration.state == "active",
        )
    )
    if generation is None:
        raise PageLineageConflict(
            "G8 requires an active page generation",
            resource=f"image:{image.id}",
            reason="active-generation-missing",
        )
    return generation


def clean_plate_job_items_for_generation(
    session,
    generation: PageGeneration,
    *,
    statuses: tuple[str, ...] | None = None,
    exclude_job_id: str | None = None,
) -> list[tuple[JobItem, Job]]:
    candidates = list(
        session.execute(
            select(JobItem, Job)
            .join(Job)
            .where(
                Job.kind == "inpaint",
                Job.lineage_context.is_not(None),
                JobItem.image_id == generation.image_id,
                *([JobItem.status.in_(statuses)] if statuses is not None else []),
                *([Job.id != exclude_job_id] if exclude_job_id is not None else []),
            )
        ).all()
    )
    matched: list[tuple[JobItem, Job]] = []
    for item, job in candidates:
        context = job.lineage_context
        pages = context.get("pages") if isinstance(context, dict) else None
        if not isinstance(pages, list):
            continue
        image_pages = [
            page
            for page in pages
            if isinstance(page, dict) and page.get("imageId") == generation.image_id
        ]
        current_pages = [
            page for page in image_pages if page.get("pageGenerationId") == generation.id
        ]
        if not current_pages:
            continue
        if (
            len(image_pages) != 1
            or len(current_pages) != 1
            or set(current_pages[0]) != {"imageId", "pageGenerationId", "expectedSequence"}
            or type(current_pages[0].get("expectedSequence")) is not int
        ):
            raise PageLineageConflict(
                "Clean-plate job has an ambiguous current-generation lineage binding",
                resource=f"job:{job.id}",
                reason="g8-clean-plate-replay-invalid",
            )
        matched.append((item, job))
    return matched


def _fallback_events(session, generation_id: str) -> list[PageLineageEvent]:
    return list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.operation.in_(
                    ("clean-plate-fallback-enabled", "clean-plate-fallback-disabled")
                ),
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )


def _fallback_enabled(events: list[PageLineageEvent]) -> bool:
    return bool(events) and events[-1].operation == "clean-plate-fallback-enabled"


def _fallback_records(events: list[PageLineageEvent]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": event.sequence,
            "enabled": event.operation == "clean-plate-fallback-enabled",
            "reason": event.reason,
            "reviewer": {
                "actorKind": event.actor_kind,
                "actorId": event.actor_id,
                "taskId": event.task_id,
                "threadId": event.thread_id,
                "sessionId": event.session_id,
                "operationSource": event.operation_source,
            },
        }
        for event in events
    ]


def _candidate_identity(row: PageCleanPlateCandidate) -> list[Any]:
    return [
        row.id,
        row.sequence,
        row.job_id,
        row.job_item_id,
        row.parent_checksum,
        row.quality_checksum,
        row.background_checksum,
        row.mask_artifact_id,
        row.mask_checksum,
        row.route_manifest,
        row.route_checksum,
        row.origin_kind,
        row.provider_ids,
        row.model_versions,
        row.parameter_hash,
        row.candidate_checksum,
        row.relative_path,
        row.width,
        row.height,
        int(row.render_scale),
        row.outside_mask_change_count,
        row.anomalies,
    ]


def _review_identity(row: PageCleanPlateReview) -> list[Any]:
    return [
        row.id,
        row.sequence,
        row.candidate_id,
        row.state,
        row.reason,
        row.parent_checksum,
        row.candidate_checksum,
        row.mask_checksum,
        row.checks,
        row.reviewer,
    ]


def _state_checksum(
    g7_checksum: str,
    background_checksum: str,
    quality_checksum: str,
    mask_artifact_id: str | None,
    mask_checksum: str | None,
    candidates: list[PageCleanPlateCandidate],
    reviews: list[PageCleanPlateReview],
    fallback_events: list[PageLineageEvent],
) -> str:
    if not candidates and not reviews and not fallback_events:
        return g7_checksum
    return _digest(
        {
            "g7Checksum": g7_checksum,
            "backgroundChecksum": background_checksum,
            "qualityChecksum": quality_checksum,
            "maskArtifactId": mask_artifact_id,
            "maskChecksum": mask_checksum,
            "candidates": [_candidate_identity(row) for row in candidates],
            "reviews": [_review_identity(row) for row in reviews],
            "fallback": _fallback_records(fallback_events),
        }
    )


def _supported_options(options: dict[str, Any]) -> dict[str, Any]:
    def bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
        raw = options.get(key, default)
        if isinstance(raw, bool):
            raise ProjectError(f"{key} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as error:
            raise ProjectError(f"{key} must be an integer") from error
        if value != raw or not minimum <= value <= maximum:
            raise ProjectError(f"{key} is outside the supported range")
        return value

    radius_raw = options.get("radius", 3.0)
    if (
        isinstance(radius_raw, bool)
        or not isinstance(radius_raw, (int, float))
        or not math.isfinite(float(radius_raw))
        or not 0 < float(radius_raw) <= 256
    ):
        raise ProjectError("radius must be a finite number from 0 to 256")
    owner_mask_strategy = options.get("ownerMaskStrategy")
    if owner_mask_strategy is not None and owner_mask_strategy != _CONNECTED_CONTRACT_UNION:
        raise ProjectError("ownerMaskStrategy is unsupported")
    solid_fill_strategy = options.get("solidFillStrategy")
    if solid_fill_strategy not in {
        None,
        _CLASSIFIED_SOLID_FILL,
        _CLASSIFIED_SOLID_OPAQUE_MASK,
    }:
        raise ProjectError("solidFillStrategy is unsupported")
    return {
        "classicalFallback": options.get("classicalFallback", False) is True,
        "contextPadding": bounded_int("contextPadding", 64, 0, 4096),
        "inferencePadding": bounded_int("inferencePadding", 32, 0, 512),
        "radius": float(radius_raw),
        "ownerMaskStrategy": owner_mask_strategy,
        "solidFillStrategy": solid_fill_strategy,
    }


def _route_manifest(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
    *,
    options: dict[str, Any],
    fallback_enabled: bool,
    accepted_g7_checksum: str | None = None,
    bound_snapshots: list[dict[str, Any]] | None = None,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    background_checksum, _ = require_current_background_classifications(
        store, session, image, generation
    )
    quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
    normalized = _supported_options(options)
    fallback_requested = normalized["classicalFallback"]
    raw_guide = options.get("layeredStructureGuide")
    raw_references = options.get("layeredStructureReferences")
    layered_requested = raw_guide is not None or raw_references is not None
    if layered_requested and (raw_guide is None or raw_references is None):
        raise ProjectError(
            "layeredStructureGuide and layeredStructureReferences must be provided together"
        )
    if layered_requested and set(options) - {
        "provider",
        "concurrency",
        "classicalFallback",
        "contextPadding",
        "inferencePadding",
        "radius",
        "ownerMaskStrategy",
        "solidFillStrategy",
        "layeredStructureGuide",
        "layeredStructureReferences",
    }:
        raise ProjectError("Layered structure request contains unsupported options")
    if layered_requested and not fallback_requested:
        raise ProjectError("Layered structure route requires classicalFallback")
    if fallback_requested and not fallback_enabled:
        raise PageLineageConflict(
            "Classical fallback is disabled for this page",
            resource=f"image:{image.id}",
            reason="g8-classical-fallback-disabled",
        )
    guide: dict[str, Any] | None = None
    snapshots: list[dict[str, Any]] = []
    if layered_requested:
        if not _is_sha256(accepted_g7_checksum):
            raise ProjectError("Layered structure route requires an accepted G7 checksum")
        try:
            guide = canonicalize_layered_structure_guide(
                raw_guide,
                source_size=(image.width, image.height),
            )
        except LayeredStructureError as error:
            raise ProjectError(str(error)) from error
        try:
            with Image.open(quality["path"]) as quality_image:
                expected_grid = quality_image.size
        except (OSError, ValueError) as error:
            raise PageLineageConflict(
                "G8 quality plate is unavailable",
                resource=f"image:{image.id}",
                reason="g8-input-checksum-mismatch",
            ) from error
        if bound_snapshots is None:
            snapshots = snapshot_layered_structure_references(
                store,
                session,
                image=image,
                generation=generation,
                references=raw_references,
                expected_grid=expected_grid,
            )
        else:
            expected_reference_keys = {
                "id",
                "imageId",
                "candidateId",
                "expectedSourceChecksum",
                "expectedArtifactChecksum",
                "expectedManifestDigest",
                "expectedMaskChecksum",
            }
            requested_by_id = (
                {
                    item.get("id"): item
                    for item in raw_references
                    if isinstance(item, dict) and set(item) == expected_reference_keys
                }
                if isinstance(raw_references, list)
                else {}
            )
            if (
                len(requested_by_id) != len(raw_references or [])
                or sorted(requested_by_id) != [item.get("referenceId") for item in bound_snapshots]
                or any(
                    requested_by_id[snapshot["referenceId"]]
                    != {
                        "id": snapshot["referenceId"],
                        "imageId": snapshot["referenceImageId"],
                        "candidateId": snapshot["referenceCandidateId"],
                        "expectedSourceChecksum": snapshot["sourceChecksum"],
                        "expectedArtifactChecksum": snapshot["artifactChecksum"],
                        "expectedManifestDigest": snapshot["legacyManifestDigest"],
                        "expectedMaskChecksum": snapshot["maskChecksum"],
                    }
                    for snapshot in bound_snapshots
                )
            ):
                raise ProjectError("Layered structure snapshot binding changed")
            snapshots = bound_snapshots
            load_layered_structure_snapshots(store, generation.id, snapshots)
        declared = {item["referenceId"] for item in snapshots}
        used = {
            item["referenceId"]
            for item in [*guide["domains"], *guide["strokes"]]
            if item["mode"] == "reference"
        }
        if used != declared:
            raise ProjectError("Layered structure declared and used reference sets must match")
        normalized["layeredStructureGuide"] = guide
        normalized["layeredStructureSnapshots"] = snapshots
        normalized["layeredStructureGuideDigest"] = _digest(guide)
    requested_provider_raw = str(
        options.get("provider")
        or store.project(session).settings.get("inpainterProvider")
        or "lama"
    )
    requested_provider = _AI_PROVIDER_ALIASES.get(
        requested_provider_raw,
        requested_provider_raw,
    )
    rows = eligible_mask_regions(session, image.id)
    manifest: list[dict[str, Any]] = []
    for row in rows:
        category = row.background_category
        if category not in _ROUTE_BY_CATEGORY or row.background_generation_id != generation.id:
            raise PageLineageConflict(
                "G8 route has no current G5 classification",
                resource=f"region:{row.id}",
                reason="g8-background-route-invalid",
            )
        route = _ROUTE_BY_CATEGORY[category]
        if layered_requested:
            route = "layered-structure"
        elif category in _COMPLEX_CATEGORIES and fallback_requested:
            route = "classical-fallback"
        if route == "ai-inpaint-redraw":
            if requested_provider_raw not in _AI_PROVIDER_ALIASES:
                raise PageLineageConflict(
                    "Complex backgrounds require an allowlisted AI redraw provider",
                    resource=f"image:{image.id}",
                    reason="g8-ai-provider-required",
                )
            provider = requested_provider
            model_version = "lama-onnx-local-v1"
            origin = "ai"
            parameters = {
                "contextPadding": normalized["contextPadding"],
                "inferencePadding": normalized["inferencePadding"],
                "renderScaleAppliedAtRuntime": True,
            }
        elif route == "layered-structure":
            provider = "opencv"
            model_version = _LAYERED_STRUCTURE_MODEL
            reference_origins = {snapshot["ancestry"]["originKind"] for snapshot in snapshots}
            origin = "classical" if reference_origins == {"classical"} else "mixed"
            parameters = {
                "guideDigest": normalized["layeredStructureGuideDigest"],
                "snapshots": snapshots,
                "acceptedG7Checksum": accepted_g7_checksum,
            }
        elif route == "classical-fallback":
            provider = "opencv"
            model_version = "telea-v1"
            origin = "classical"
            parameters = {"radius": normalized["radius"]}
        elif route == "deterministic-solid":
            provider = "opencv"
            origin = "deterministic"
            if normalized["solidFillStrategy"] in {
                _CLASSIFIED_SOLID_FILL,
                _CLASSIFIED_SOLID_OPAQUE_MASK,
            } and category in {"white-solid", "black-solid"}:
                opaque_mask = normalized["solidFillStrategy"] == _CLASSIFIED_SOLID_OPAQUE_MASK
                model_version = "classified-solid-v2" if opaque_mask else "classified-solid-v1"
                parameters = {
                    "backgroundCategory": category,
                    "fillRgb": [255, 255, 255] if category == "white-solid" else [0, 0, 0],
                    "solidFillStrategy": normalized["solidFillStrategy"],
                }
                if opaque_mask:
                    parameters["maskApplication"] = "opaque-nonzero-support"
            else:
                model_version = "boundary-median-solid-v1"
                parameters = {"ringRadius": 16, "renderScaleAppliedAtRuntime": True}
        elif route == "controlled-gradient":
            provider = "opencv"
            model_version = "boundary-plane-gradient-v1"
            origin = "deterministic"
            parameters = {"ringRadius": 24, "renderScaleAppliedAtRuntime": True}
        else:
            provider = "opencv"
            model_version = "phase-preserving-screentone-v1"
            origin = "deterministic"
            parameters = {"phaseFit": "multi-domain-v1"}
        if normalized["ownerMaskStrategy"] is not None:
            parameters["ownerMaskStrategy"] = normalized["ownerMaskStrategy"]
        manifest.append(
            {
                "regionId": row.id,
                "backgroundCategory": category,
                "route": route,
                "originKind": origin,
                "provider": provider,
                "modelVersion": model_version,
                "parameterHash": _digest(parameters),
                **({"lineageInputs": snapshots} if route == "layered-structure" else {}),
            }
        )
    if not manifest:
        raise PageLineageConflict(
            "A zero-eligible page must use the G8 not-applicable decision",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-not-applicable",
        )
    route_checksum = _digest(
        {
            "backgroundChecksum": background_checksum,
            "qualityChecksum": quality["checksum"],
            "classicalFallback": fallback_requested,
            "routes": manifest,
            **(
                {
                    "acceptedG7Checksum": accepted_g7_checksum,
                    "layeredStructureGuideDigest": normalized["layeredStructureGuideDigest"],
                    "layeredStructureSnapshots": snapshots,
                }
                if layered_requested
                else {}
            ),
        }
    )
    return (
        background_checksum,
        quality["checksum"],
        manifest,
        {
            **normalized,
            "provider": requested_provider,
            "routeChecksum": route_checksum,
        },
    )


def _origin_for_manifest(manifest: list[dict[str, Any]]) -> str:
    origins = {entry["originKind"] for entry in manifest}
    if origins == {"ai"}:
        return "ai"
    if origins == {"classical"}:
        return "classical"
    if origins == {"deterministic"}:
        return "deterministic"
    return "mixed"


def _mask_owners(
    mask: np.ndarray,
    rows: list[TextRegion],
    *,
    scale: int,
) -> list[np.ndarray]:
    target_y, target_x = np.nonzero(mask > 0)
    if not len(target_x):
        raise PageLineageConflict(
            "Accepted G7 mask is empty",
            resource="g7-mask",
            reason="g8-mask-empty",
        )
    distances: list[np.ndarray] = []
    for row in rows:
        left = float(row.x) * scale
        top = float(row.y) * scale
        right = (float(row.x) + float(row.width)) * scale
        bottom = (float(row.y) + float(row.height)) * scale
        dx = np.maximum(np.maximum(left - target_x, 0), target_x - right)
        dy = np.maximum(np.maximum(top - target_y, 0), target_y - bottom)
        distances.append(dx * dx + dy * dy)
    owner = np.argmin(np.stack(distances, axis=0), axis=0)
    outputs: list[np.ndarray] = []
    for index in range(len(rows)):
        region_mask = np.zeros_like(mask)
        selected = owner == index
        region_mask[target_y[selected], target_x[selected]] = mask[
            target_y[selected], target_x[selected]
        ]
        outputs.append(region_mask)
    return outputs


def _route_execution_key(route: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(route["backgroundCategory"]),
        str(route["route"]),
        str(route["originKind"]),
        str(route["provider"]),
        str(route["modelVersion"]),
        str(route["parameterHash"]),
    )


def _render_groups(
    page_mask: np.ndarray,
    route_masks: list[np.ndarray],
    manifest: list[dict[str, Any]],
    *,
    strategy: str | None,
) -> list[tuple[list[int], np.ndarray]]:
    if strategy is None:
        return [([index], route_mask) for index, route_mask in enumerate(route_masks)]
    if strategy != _CONNECTED_CONTRACT_UNION:
        raise ProjectError("ownerMaskStrategy is unsupported")

    _component_count, component_labels = cv2.connectedComponents(
        (page_mask > 0).astype(np.uint8),
        connectivity=8,
    )
    grouped: dict[tuple[int, tuple[str, ...]], tuple[set[int], np.ndarray]] = {}
    for index, route_mask in enumerate(route_masks):
        labels = np.unique(component_labels[route_mask > 0])
        for label_value in labels:
            label = int(label_value)
            if label == 0:
                continue
            key = (label, _route_execution_key(manifest[index]))
            members, union_mask = grouped.setdefault(
                key,
                (set(), np.zeros_like(page_mask)),
            )
            members.add(index)
            component_mask = np.where(component_labels == label, route_mask, 0).astype(np.uint8)
            np.maximum(union_mask, component_mask, out=union_mask)
    ordered = [
        (sorted(members), union_mask, label, execution_key)
        for (label, execution_key), (members, union_mask) in grouped.items()
    ]
    ordered.sort(key=lambda entry: (entry[0][0], entry[2], entry[3]))
    return [(members, union_mask) for members, union_mask, _label, _key in ordered]


def _ring(mask: np.ndarray, radius: int) -> np.ndarray:
    binary = (mask > 0).astype(np.uint8)
    size = max(3, radius * 2 + 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    return (cv2.dilate(binary, kernel) > 0) & (binary == 0)


def _solid_candidate(source: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    context = _ring(mask, radius)
    samples = source[context]
    if len(samples) < 8:
        raise ProjectError("Solid repair has insufficient unmasked boundary context")
    fill = np.median(samples, axis=0)
    result = source.copy()
    alpha = mask.astype(np.float32)[..., None] / 255.0
    result = np.rint(result * (1.0 - alpha) + fill * alpha).astype(np.uint8)
    return result


def _classified_solid_candidate(
    source: np.ndarray,
    mask: np.ndarray,
    background_category: str,
    *,
    opaque_mask: bool = False,
) -> np.ndarray:
    if background_category == "white-solid":
        fill = np.full(3, 255, dtype=np.float32)
    elif background_category == "black-solid":
        fill = np.zeros(3, dtype=np.float32)
    else:
        raise ProjectError("Classified solid repair requires a white or black background")
    if opaque_mask:
        result = source.copy()
        result[mask > 0] = fill.astype(np.uint8)
        return result
    alpha = mask.astype(np.float32)[..., None] / 255.0
    return np.rint(source * (1.0 - alpha) + fill * alpha).astype(np.uint8)


def _gradient_candidate(source: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    context = _ring(mask, radius)
    ys, xs = np.nonzero(context)
    if len(xs) < 24:
        raise ProjectError("Gradient repair has insufficient unmasked boundary context")
    design = np.column_stack(
        (
            xs / max(1, source.shape[1] - 1),
            ys / max(1, source.shape[0] - 1),
            np.ones(len(xs)),
        )
    )
    target_y, target_x = np.nonzero(mask > 0)
    target_design = np.column_stack(
        (
            target_x / max(1, source.shape[1] - 1),
            target_y / max(1, source.shape[0] - 1),
            np.ones(len(target_x)),
        )
    )
    prediction = np.column_stack(
        [
            np.clip(
                target_design @ np.linalg.lstsq(design, source[ys, xs, channel], rcond=None)[0],
                0,
                255,
            )
            for channel in range(source.shape[2])
        ]
    )
    result = source.copy().astype(np.float32)
    weights = mask[target_y, target_x].astype(np.float32)[:, None] / 255.0
    result[target_y, target_x] *= 1.0 - weights
    result[target_y, target_x] += prediction * weights
    return np.rint(result).astype(np.uint8)


def _outside_mask_change_count(
    source: Image.Image,
    candidate: Image.Image,
    mask: Image.Image,
) -> int:
    if source.size != candidate.size or mask.size != source.size:
        raise ValueError("Clean-plate source, candidate, and mask grids differ")
    source_rgba = np.asarray(source.convert("RGBA"), dtype=np.uint8)
    candidate_rgba = np.asarray(candidate.convert("RGBA"), dtype=np.uint8)
    mask_grid = np.asarray(mask.convert("L"), dtype=np.uint8)
    outside = mask_grid == 0
    return int(np.count_nonzero(np.any(candidate_rgba[outside] != source_rgba[outside], axis=1)))


def _validate_candidate_file(
    store: ProjectStore,
    row: PageCleanPlateCandidate,
    *,
    session=None,
) -> Path:
    expected = _candidate_relative(row.generation_id, row.id).as_posix()
    if row.relative_path != expected or row.outside_mask_change_count != 0:
        raise PageLineageConflict(
            "Clean-plate artifact path or outside-mask metric is invalid",
            resource=f"clean-plate-candidate:{row.id}",
            reason="g8-candidate-invalid",
        )
    path = _candidate_target(store, row.generation_id, row.id)
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row.candidate_checksum:
            raise ValueError("checksum")
        with Image.open(io.BytesIO(payload)) as opened:
            if opened.format != "PNG" or opened.size != (row.width, row.height):
                raise ValueError("grid")
            opened.load()
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "Clean-plate artifact is unavailable or changed",
            resource=f"clean-plate-candidate:{row.id}",
            reason="g8-candidate-invalid",
        ) from error
    if session is None:
        return path
    image = session.get(ImageAsset, row.image_id)
    generation = session.get(PageGeneration, row.generation_id)
    mask = session.get(PageMaskArtifact, row.mask_artifact_id)
    if image is None or generation is None or mask is None:
        raise PageLineageConflict(
            "Clean-plate artifact has no current quality or mask anchor",
            resource=f"clean-plate-candidate:{row.id}",
            reason="g8-candidate-invalid",
        )
    quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
    mask_path = resolve_write_target(
        store.root, Path(mask.relative_path), protected_roots=(store.source_root,)
    )
    try:
        quality_payload = Path(quality["path"]).read_bytes()
        mask_payload = mask_path.read_bytes()
        if (
            quality["checksum"] != row.quality_checksum
            or hashlib.sha256(quality_payload).hexdigest() != row.quality_checksum
            or mask.mask_checksum != row.mask_checksum
            or mask.generation_id != row.generation_id
            or mask.image_id != row.image_id
            or hashlib.sha256(mask_payload).hexdigest() != row.mask_checksum
        ):
            raise ValueError("anchor-checksum")
        with Image.open(io.BytesIO(quality_payload)) as quality_image:
            quality_image.load()
            quality_copy = quality_image.copy()
        with Image.open(io.BytesIO(mask_payload)) as mask_image:
            mask_image.load()
            if mask_image.mode != "L":
                raise ValueError("mask-mode")
            mask_copy = mask_image.copy()
        with Image.open(io.BytesIO(payload)) as candidate_image:
            candidate_image.load()
            candidate_copy = candidate_image.copy()
        outside_change_count = _outside_mask_change_count(quality_copy, candidate_copy, mask_copy)
        if outside_change_count != 0:
            raise ValueError("outside-mask")
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "Clean-plate artifact changed pixels outside its accepted mask",
            resource=f"clean-plate-candidate:{row.id}",
            reason="g8-outside-mask-changed",
        ) from error
    return path


def _event_actor(event: PageLineageEvent) -> dict[str, str | None]:
    return _safe_actor(
        {
            "actorKind": event.actor_kind,
            "actorId": event.actor_id,
            "taskId": event.task_id,
            "threadId": event.thread_id,
            "sessionId": event.session_id,
            "operationSource": event.operation_source,
        }
    )


def _job_actor(job: Job) -> dict[str, str | None]:
    context = job.lineage_context
    actor = context.get("actor") if isinstance(context, dict) else None
    if not isinstance(actor, dict):
        raise PageLineageConflict(
            "Clean-plate job has no actor binding",
            resource=f"job:{job.id}",
            reason="g8-clean-plate-replay-invalid",
        )
    return _safe_actor(actor)


def _job_expected_sequence(job: Job, generation: PageGeneration) -> int | None:
    context = job.lineage_context
    pages = context.get("pages") if isinstance(context, dict) else None
    if not isinstance(pages, list):
        return None
    page = next(
        (
            value
            for value in pages
            if isinstance(value, dict)
            and value.get("imageId") == generation.image_id
            and value.get("pageGenerationId") == generation.id
        ),
        None,
    )
    return page.get("expectedSequence") if isinstance(page, dict) else None


def _revision_matches(
    session,
    generation: PageGeneration,
    event: PageLineageEvent,
    *,
    entity_type: str,
    entity_id: str,
    operation: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> bool:
    revision = session.get(Revision, event.revision_id)
    return bool(
        revision is not None
        and revision.project_id == generation.project_id
        and revision.entity_type == entity_type
        and revision.entity_id == entity_id
        and revision.operation == operation
        and revision.before == before
        and revision.after == after
    )


def _base_bindings(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, PageMaskArtifact | None, str, str]:
    g7_checksum, mask_artifact = require_current_mask_acceptance(store, session, image, generation)
    quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
    background_checksum, _ = require_current_background_classifications(
        store, session, image, generation
    )
    return g7_checksum, mask_artifact, quality["checksum"], background_checksum


def _g8_replay(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
) -> dict[str, Any]:
    def invalid(message: str, resource: str | None = None) -> None:
        raise PageLineageConflict(
            message,
            resource=resource or f"page-generation:{generation.id}",
            reason="g8-clean-plate-replay-invalid",
        )

    g7_checksum, mask_artifact, quality_checksum, background_checksum = _base_bindings(
        store, session, image, generation
    )
    all_events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(PageLineageEvent.generation_id == generation.id)
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    if [event.sequence for event in all_events] != list(range(1, len(all_events) + 1)) or (
        generation.next_sequence != len(all_events) + 1
    ):
        invalid("Page generation event sequence is not contiguous")
    g7_terminal = next(
        (
            event
            for event in reversed(all_events)
            if event.gate == "G7_mask"
            and event.operation == "mask-stage-review"
            and event.state in {"accepted", "not-applicable"}
        ),
        None,
    )
    if g7_terminal is None or g7_terminal.output_checksum != g7_checksum:
        invalid("G8 has no exact accepted G7 parent")
    after_g7 = all_events[g7_terminal.sequence :]
    g8_events: list[PageLineageEvent] = []
    downstream_seen = False
    cloud_route_started = False
    for event in after_g7:
        is_g8 = event.gate == "G8_cleanPlate"
        if is_g8:
            if downstream_seen:
                invalid("G8 evidence is interleaved with a downstream gate", f"event:{event.id}")
            if event.operation not in _ALLOWED_G8_OPERATIONS:
                invalid("G8 operation/gate matrix is invalid", f"event:{event.id}")
            g8_events.append(event)
        else:
            if not downstream_seen and event.gate == "G8_cloudFullPage":
                cloud_route_started = True
            downstream_seen = True
    if after_g7 and not g8_events and after_g7[0].gate != "G8_cloudFullPage":
        invalid("A downstream gate started before G8")

    rows = list(
        session.scalars(
            select(PageCleanPlateCandidate)
            .where(PageCleanPlateCandidate.generation_id == generation.id)
            .order_by(PageCleanPlateCandidate.sequence)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(PageCleanPlateReview)
            .where(PageCleanPlateReview.generation_id == generation.id)
            .order_by(PageCleanPlateReview.sequence)
        ).all()
    )
    for row in rows:
        _validate_candidate_file(store, row, session=session)
    matched = clean_plate_job_items_for_generation(session, generation)
    items = {item.id: item for item, _job in matched}
    jobs = {item.id: job for item, job in matched}
    if any(item.region_id is not None for item in items.values()):
        invalid("G8 jobs must use whole-page items")

    current_state = g7_checksum
    candidate_prefix: list[PageCleanPlateCandidate] = []
    review_prefix: list[PageCleanPlateReview] = []
    fallback_prefix: list[PageLineageEvent] = []
    row_index = 0
    review_index = 0
    open_item: str | None = None
    enqueue_state: dict[str, str] = {}
    enqueue_routes: dict[str, tuple[list[dict[str, Any]], str]] = {}
    enqueued: set[str] = set()
    produced: dict[str, PageLineageEvent] = {}
    completed: set[str] = set()
    failed: set[str] = set()
    terminal: PageLineageEvent | None = None

    def expected_state() -> str:
        return _state_checksum(
            g7_checksum,
            background_checksum,
            quality_checksum,
            mask_artifact.id if mask_artifact else None,
            mask_artifact.mask_checksum if mask_artifact else None,
            candidate_prefix,
            review_prefix,
            fallback_prefix,
        )

    def common(event: PageLineageEvent) -> dict[str, Any]:
        if (
            event.gate != "G8_cleanPlate"
            or event.stage != "inpaint"
            or event.parent_checksum != g7_checksum
        ):
            invalid("G8 event has a stale parent or stage", f"event:{event.id}")
        evidence = event.evidence
        if not isinstance(evidence, dict):
            invalid("G8 event evidence is invalid", f"event:{event.id}")
        return evidence

    for event in g8_events:
        if terminal is not None:
            invalid("G8 changed after its immutable terminal review", f"event:{event.id}")
        evidence = common(event)
        if event.operation in {
            "clean-plate-fallback-enabled",
            "clean-plate-fallback-disabled",
        }:
            enabled = event.operation.endswith("enabled")
            if (
                event.state != "pending"
                or event.job_id is not None
                or event.job_item_id is not None
                or event.revision_id is None
                or event.input_checksum != current_state
                or event.decision
                != ("classical-fallback-enabled" if enabled else "classical-fallback-disabled")
                or event.reason
                != ("all-ai-candidates-rejected" if enabled else "resume-ai-candidates")
                or event.provider != "operator"
                or event.model_version != "page-scoped-fallback-v1"
                or not _is_sha256(event.parameter_hash)
                or set(evidence)
                != {
                    "eventType",
                    "qualityState",
                    "enabled",
                    "candidateCount",
                    "aiCandidateCount",
                    "imageRevision",
                }
                or evidence.get("eventType") != event.operation
                or evidence.get("qualityState") != "pending-review"
                or evidence.get("enabled") is not enabled
                or evidence.get("candidateCount") != len(candidate_prefix)
                or evidence.get("aiCandidateCount")
                != sum(
                    any(route["originKind"] == "ai" for route in row.route_manifest)
                    for row in candidate_prefix
                )
                or type(evidence.get("imageRevision")) is not int
                or open_item is not None
            ):
                invalid("G8 fallback event matrix is invalid", f"event:{event.id}")
            prior_enabled = _fallback_enabled(fallback_prefix)
            if prior_enabled == enabled:
                invalid("G8 fallback decision did not change page state", f"event:{event.id}")
            if enabled and (
                not _fallback_allowed(
                    candidate_prefix,
                    review_prefix,
                    {
                        row.id
                        for row in eligible_mask_regions(session, image.id)
                        if row.background_category in _COMPLEX_CATEGORIES
                    },
                )
            ):
                invalid(
                    "G8 fallback was enabled before every applicable AI candidate was rejected",
                    f"event:{event.id}",
                )
            if not _revision_matches(
                session,
                generation,
                event,
                entity_type="page-clean-plate-fallback",
                entity_id=generation.id,
                operation="enable" if enabled else "disable",
                before={"enabled": prior_enabled},
                after={"enabled": enabled},
            ):
                invalid("G8 fallback event has no exact revision", f"event:{event.id}")
            fallback_prefix.append(event)
            current_state = expected_state()
            if event.output_checksum != current_state:
                invalid("G8 fallback checksum is not its exact prefix", f"event:{event.id}")
            continue

        if event.operation in {
            "inpaint-job-enqueued",
            "clean-plate-candidate-produced",
            "inpaint-job-completed",
            "inpaint-job-failed",
        }:
            item_id = event.job_item_id
            if (
                not isinstance(item_id, str)
                or item_id not in items
                or event.job_id != jobs[item_id].id
                or _event_actor(event) != _job_actor(jobs[item_id])
            ):
                invalid("G8 job event has no exact current-lineage item", f"event:{event.id}")
        else:
            item_id = None

        if event.operation == "inpaint-job-enqueued":
            if (
                mask_artifact is None
                or event.state != "pending"
                or event.reason != "job-enqueued"
                or event.decision is not None
                or event.revision_id is not None
                or item_id in enqueued
                or open_item is not None
                or event.sequence != _job_expected_sequence(jobs[str(item_id)], generation)
                or event.input_checksum != current_state
                or event.output_checksum != current_state
                or evidence.get("eventType") != "job-enqueued"
                or evidence.get("qualityState") != "pending-review"
                or evidence.get("targetKind") != "image"
                or evidence.get("g7Checksum") != g7_checksum
                or evidence.get("backgroundChecksum") != background_checksum
                or evidence.get("qualityChecksum") != quality_checksum
                or evidence.get("maskArtifactId") != mask_artifact.id
                or evidence.get("maskChecksum") != mask_artifact.mask_checksum
                or not isinstance(evidence.get("routeManifest"), list)
                or not _is_sha256(evidence.get("routeChecksum"))
                or event.parameter_hash != evidence.get("routeChecksum")
            ):
                invalid("G8 enqueue event matrix is invalid", f"event:{event.id}")
            expected_background, expected_quality, manifest, normalized = _route_manifest(
                store,
                session,
                image,
                generation,
                options=dict(jobs[str(item_id)].options),
                fallback_enabled=_fallback_enabled(fallback_prefix),
                accepted_g7_checksum=g7_checksum,
                bound_snapshots=evidence.get("layeredStructureSnapshots"),
            )
            if (
                expected_background != background_checksum
                or expected_quality != quality_checksum
                or evidence.get("routeManifest") != manifest
                or evidence.get("routeChecksum") != normalized["routeChecksum"]
            ):
                invalid("G8 enqueue route evidence is stale", f"event:{event.id}")
            enqueued.add(str(item_id))
            enqueue_state[str(item_id)] = current_state
            enqueue_routes[str(item_id)] = (manifest, normalized["routeChecksum"])
            open_item = str(item_id)
            continue

        if event.operation == "clean-plate-candidate-produced":
            if row_index >= len(rows):
                invalid("G8 produced event has no candidate row", f"event:{event.id}")
            row = rows[row_index]
            if (
                item_id != open_item
                or row.job_item_id != item_id
                or row.job_id != event.job_id
                or row.generation_id != generation.id
                or row.image_id != image.id
                or row.parent_checksum != g7_checksum
                or row.quality_checksum != quality_checksum
                or row.background_checksum != background_checksum
                or mask_artifact is None
                or row.mask_artifact_id != mask_artifact.id
                or row.mask_checksum != mask_artifact.mask_checksum
                or row.route_manifest != enqueue_routes[str(item_id)][0]
                or row.route_checksum != enqueue_routes[str(item_id)][1]
                or row.parameter_hash != enqueue_routes[str(item_id)][1]
                or row.origin_kind != _origin_for_manifest(enqueue_routes[str(item_id)][0])
                or row.provider_ids
                != sorted({route["provider"] for route in enqueue_routes[str(item_id)][0]})
                or row.model_versions
                != sorted({route["modelVersion"] for route in enqueue_routes[str(item_id)][0]})
                or row.sequence != row_index + 1
                or row.outside_mask_change_count != 0
                or event.state != "pending"
                or event.reason != "clean-plate-review-required"
                or event.decision is not None
                or event.revision_id is None
                or event.input_checksum != current_state
                or event.provider
                != (row.provider_ids[0] if len(row.provider_ids) == 1 else "mixed")
                or event.model_version != "route-manifest-v1"
                or event.parameter_hash != row.parameter_hash
                or evidence
                != {
                    "eventType": "clean-plate-candidate-produced",
                    "qualityState": "pending-review",
                    "targetKind": "clean-plate-candidate",
                    "candidateId": row.id,
                    "candidateChecksum": row.candidate_checksum,
                    "g7Checksum": g7_checksum,
                    "backgroundChecksum": background_checksum,
                    "qualityChecksum": quality_checksum,
                    "maskArtifactId": row.mask_artifact_id,
                    "maskChecksum": row.mask_checksum,
                    "routeManifest": row.route_manifest,
                    "routeChecksum": row.route_checksum,
                    "originKind": row.origin_kind,
                    "providerIds": row.provider_ids,
                    "modelVersions": row.model_versions,
                    "parameterHash": row.parameter_hash,
                    "width": row.width,
                    "height": row.height,
                    "renderScale": int(row.render_scale),
                    "outsideMaskChangeCount": 0,
                    "anomalies": row.anomalies,
                    "imageRevision": evidence.get("imageRevision"),
                }
                or type(evidence.get("imageRevision")) is not int
            ):
                invalid("G8 produced event does not match its exact candidate", f"event:{event.id}")
            if not _revision_matches(
                session,
                generation,
                event,
                entity_type="page-clean-plate-candidate",
                entity_id=row.id,
                operation="produce",
                before={},
                after={
                    "candidateChecksum": row.candidate_checksum,
                    "maskChecksum": row.mask_checksum,
                    "routeChecksum": row.route_checksum,
                },
            ):
                invalid("G8 candidate event has no exact revision", f"event:{event.id}")
            candidate_prefix.append(row)
            row_index += 1
            current_state = expected_state()
            if event.output_checksum != current_state:
                invalid("G8 candidate checksum is not its exact prefix", f"event:{event.id}")
            produced[str(item_id)] = event
            continue

        if event.operation == "inpaint-job-completed":
            row = next(
                (candidate for candidate in candidate_prefix if candidate.job_item_id == item_id),
                None,
            )
            produced_event = produced.get(str(item_id))
            if (
                item_id != open_item
                or row is None
                or produced_event is None
                or item_id in completed
                or item_id in failed
                or items[str(item_id)].status != "completed"
                or event.state != "pending"
                or event.reason != "review-required"
                or event.decision is not None
                or event.revision_id is not None
                or event.input_checksum != enqueue_state[str(item_id)]
                or event.output_checksum != current_state
                or event.output_checksum != produced_event.output_checksum
                or event.provider != produced_event.provider
                or event.model_version != produced_event.model_version
                or event.parameter_hash != produced_event.parameter_hash
                or evidence
                != {
                    "eventType": "job-completed",
                    "qualityState": "pending-review",
                    "targetKind": "image",
                    "candidateId": row.id,
                    "candidateChecksum": row.candidate_checksum,
                    "maskArtifactId": row.mask_artifact_id,
                    "maskChecksum": row.mask_checksum,
                    "routeChecksum": row.route_checksum,
                    "outsideMaskChangeCount": 0,
                }
            ):
                invalid("G8 completion event is missing or stale", f"event:{event.id}")
            completed.add(str(item_id))
            open_item = None
            continue

        if event.operation == "inpaint-job-failed":
            if (
                item_id != open_item
                or item_id in produced
                or item_id in completed
                or item_id in failed
                or items[str(item_id)].status != "failed"
                or event.state != "blocked"
                or event.reason != "job-execution-failed"
                or event.decision is not None
                or event.revision_id is not None
                or event.input_checksum != enqueue_state[str(item_id)]
                or event.output_checksum is not None
                or evidence
                != {
                    "eventType": "job-failed",
                    "qualityState": "blocked",
                    "targetKind": "image",
                    "routeChecksum": evidence.get("routeChecksum"),
                }
                or not _is_sha256(evidence.get("routeChecksum"))
            ):
                invalid("G8 failed-job evidence is inconsistent", f"event:{event.id}")
            failed.add(str(item_id))
            open_item = None
            continue

        if event.operation != "clean-plate-stage-review":
            invalid("G8 contains unsupported evidence", f"event:{event.id}")
        if review_index >= len(reviews):
            invalid("G8 review event has no immutable review row", f"event:{event.id}")
        review = reviews[review_index]
        candidate = (
            next((row for row in candidate_prefix if row.id == review.candidate_id), None)
            if review.candidate_id
            else None
        )
        if (
            event.job_id is not None
            or event.job_item_id is not None
            or event.revision_id is None
            or event.input_checksum != current_state
            or open_item is not None
            or review.sequence != review_index + 1
            or review.generation_id != generation.id
            or review.image_id != image.id
            or review.parent_checksum != g7_checksum
            or _event_actor(event) != review.reviewer
            or event.state != review.state
            or event.reason != review.reason
            or event.decision
            != {
                "accepted": "clean-plate-accepted",
                "rejected": "clean-plate-rejected",
                "not-applicable": "clean-plate-not-applicable",
            }[review.state]
            or event.provider
            != (
                candidate.provider_ids[0]
                if candidate and len(candidate.provider_ids) == 1
                else "mixed"
                if candidate
                else "none"
            )
            or event.model_version
            != ("route-manifest-v1" if candidate else "quality-plate-pass-through-v1")
            or event.parameter_hash
            != (candidate.parameter_hash if candidate else background_checksum)
            or evidence
            != {
                "eventType": "clean-plate-stage-review",
                "qualityState": review.state,
                "candidateId": review.candidate_id,
                "candidateChecksum": review.candidate_checksum,
                "g7Checksum": g7_checksum,
                "backgroundChecksum": background_checksum,
                "qualityChecksum": quality_checksum,
                "maskArtifactId": candidate.mask_artifact_id if candidate else None,
                "maskChecksum": review.mask_checksum,
                "routeChecksum": candidate.route_checksum if candidate else None,
                "originKind": candidate.origin_kind if candidate else "no-op",
                "checks": review.checks,
                "imageRevision": evidence.get("imageRevision"),
            }
            or type(evidence.get("imageRevision")) is not int
        ):
            invalid("G8 review event does not match its exact row", f"event:{event.id}")
        if candidate is not None:
            completion = next(
                (
                    candidate_event
                    for candidate_event in g8_events
                    if candidate_event.operation == "inpaint-job-completed"
                    and candidate_event.job_item_id == candidate.job_item_id
                    and candidate_event.sequence < event.sequence
                ),
                None,
            )
            if (
                completion is None
                or review.candidate_checksum != candidate.candidate_checksum
                or review.mask_checksum != candidate.mask_checksum
            ):
                invalid("G8 review candidate is not completed and current", f"event:{event.id}")
        elif mask_artifact is not None or review.state != "not-applicable":
            invalid("G8 not-applicable review has residual candidate evidence", f"event:{event.id}")
        if not _revision_matches(
            session,
            generation,
            event,
            entity_type="page-clean-plate-review",
            entity_id=review.id,
            operation=review.state,
            before={},
            after={
                "state": review.state,
                "candidateId": review.candidate_id,
                "candidateChecksum": review.candidate_checksum,
            },
        ):
            invalid("G8 review event has no exact revision", f"event:{event.id}")
        review_prefix.append(review)
        review_index += 1
        current_state = expected_state()
        if event.output_checksum != current_state:
            invalid("G8 review checksum is not its exact prefix", f"event:{event.id}")
        if review.state in {"accepted", "not-applicable"}:
            terminal = event

    if row_index != len(rows) or review_index != len(reviews):
        invalid("Stored G8 rows do not have one-to-one publication events")
    if downstream_seen and terminal is None and not cloud_route_started:
        invalid("A downstream gate started before terminal G8 acceptance")
    return {
        "g7Checksum": g7_checksum,
        "maskArtifact": mask_artifact,
        "qualityChecksum": quality_checksum,
        "backgroundChecksum": background_checksum,
        "stateChecksum": current_state,
        "candidates": rows,
        "reviews": reviews,
        "fallbackEvents": fallback_prefix,
        "fallbackEnabled": _fallback_enabled(fallback_prefix),
        "terminal": terminal,
        "openItemId": open_item,
    }


def prepare_clean_plate_enqueue(
    store: ProjectStore,
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    job: Job,
    item: JobItem,
) -> dict[str, Any]:
    _require_legacy_route_mutable(session, generation)
    replay = _g8_replay(store, session, image, generation)
    if replay["terminal"] is not None:
        raise PageLineageConflict(
            "Accepted G8 evidence is immutable",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-accepted",
        )
    if replay["maskArtifact"] is None:
        raise PageLineageConflict(
            "A zero-eligible page must use the G8 not-applicable decision",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-not-applicable",
        )
    if item.region_id is not None:
        raise PageLineageConflict(
            "Strict G8 inpaint requires one whole-page job item",
            resource=f"job-item:{item.id}",
            reason="g8-whole-page-required",
        )
    if replay["openItemId"] is not None or clean_plate_job_items_for_generation(
        session,
        generation,
        statuses=("queued", "running"),
        exclude_job_id=job.id,
    ):
        raise PageLineageConflict(
            "Another strict clean-plate job is already active for this page",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-job-active",
        )
    background, quality, manifest, normalized = _route_manifest(
        store,
        session,
        image,
        generation,
        options=dict(job.options),
        fallback_enabled=bool(replay["fallbackEnabled"]),
        accepted_g7_checksum=replay["g7Checksum"],
    )
    mask = replay["maskArtifact"]
    provider_ids = sorted({entry["provider"] for entry in manifest})
    return {
        "stateChecksum": replay["stateChecksum"],
        "g7Checksum": replay["g7Checksum"],
        "backgroundChecksum": background,
        "qualityChecksum": quality,
        "maskArtifactId": mask.id,
        "maskChecksum": mask.mask_checksum,
        "routeManifest": manifest,
        "routeChecksum": normalized["routeChecksum"],
        "provider": provider_ids[0] if len(provider_ids) == 1 else "mixed",
        "modelVersion": "route-manifest-v1",
        "parameterHash": normalized["routeChecksum"],
        **(
            {"layeredStructureSnapshots": normalized["layeredStructureSnapshots"]}
            if "layeredStructureSnapshots" in normalized
            else {}
        ),
    }


def _load_generation_inputs(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
    replay: dict[str, Any],
) -> tuple[bytes, bytes, int]:
    quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
    mask = replay["maskArtifact"]
    if mask is None:
        raise PageLineageConflict(
            "G8 candidate generation requires an accepted G7 artifact",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-not-applicable",
        )
    mask_path = resolve_write_target(
        store.root, Path(mask.relative_path), protected_roots=(store.source_root,)
    )
    try:
        quality_bytes = Path(quality["path"]).read_bytes()
        mask_bytes = mask_path.read_bytes()
        if hashlib.sha256(quality_bytes).hexdigest() != replay["qualityChecksum"]:
            raise ValueError("quality")
        if hashlib.sha256(mask_bytes).hexdigest() != mask.mask_checksum:
            raise ValueError("mask")
        with Image.open(io.BytesIO(quality_bytes)) as quality_image:
            width, height = quality_image.size
        with Image.open(io.BytesIO(mask_bytes)) as mask_image:
            if mask_image.mode != "L" or mask_image.size != (width, height):
                raise ValueError("mask-grid")
        scale_x, scale_y = width / image.width, height / image.height
        scale = round(scale_x)
        if (
            not math.isclose(scale_x, scale_y, rel_tol=1e-6, abs_tol=1e-6)
            or not math.isclose(scale_x, scale, rel_tol=1e-6, abs_tol=1e-6)
            or scale not in {1, 2, 3, 4}
        ):
            raise ValueError("quality-grid")
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "G8 quality plate or accepted mask is unavailable or changed",
            resource=f"image:{image.id}",
            reason="g8-input-checksum-mismatch",
        ) from error
    return quality_bytes, mask_bytes, scale


def _render_candidate(
    *,
    quality_bytes: bytes,
    mask_bytes: bytes,
    rows: list[TextRegion],
    manifest: list[dict[str, Any]],
    normalized: dict[str, Any],
    scale: int,
    inpainter: Callable[[str], Any],
    layered_reference_bytes: dict[str, bytes] | None = None,
) -> tuple[bytes, int, int, int, list[str]]:
    with Image.open(io.BytesIO(quality_bytes)) as opened:
        opened.load()
        has_alpha = "A" in opened.getbands()
        source_image = opened.convert("RGBA" if has_alpha else "RGB")
    with Image.open(io.BytesIO(mask_bytes)) as opened_mask:
        page_mask = np.asarray(opened_mask.convert("L"), dtype=np.uint8).copy()
    source_rgb = np.asarray(source_image.convert("RGB"), dtype=np.uint8).copy()
    working = source_rgb.copy()
    route_masks = _mask_owners(page_mask, rows, scale=scale)
    supported_routes = {
        "deterministic-solid",
        "controlled-gradient",
        "screentone-preserving",
        "classical-fallback",
        "ai-inpaint-redraw",
        "layered-structure",
    }
    if len(rows) != len(manifest) or any(
        row.id != route_entry.get("regionId") or route_entry.get("route") not in supported_routes
        for row, route_entry in zip(rows, manifest, strict=True)
    ):
        raise PageLineageConflict(
            "G8 route order changed before rendering",
            resource="clean-plate-candidate",
            reason="g8-background-route-invalid",
        )
    if any(entry["route"] == "layered-structure" for entry in manifest):
        if not all(entry["route"] == "layered-structure" for entry in manifest):
            raise PageLineageConflict(
                "Layered structure route must own the complete accepted mask",
                resource="clean-plate-candidate",
                reason="g8-background-route-invalid",
            )
        references: dict[str, np.ndarray] = {}
        for reference_id, encoded in (layered_reference_bytes or {}).items():
            try:
                with Image.open(io.BytesIO(encoded)) as opened_reference:
                    opened_reference.load()
                    references[reference_id] = np.asarray(
                        opened_reference.convert("RGB"), dtype=np.uint8
                    ).copy()
            except (OSError, ValueError) as error:
                raise ProjectError("Layered structure snapshot image is invalid") from error
        try:
            working = render_layered_structure(
                source_rgb,
                page_mask,
                normalized["layeredStructureGuide"],
                references,
                scale=scale,
            )
        except LayeredStructureError as error:
            raise PageLineageConflict(
                str(error),
                resource="clean-plate-candidate",
                reason="g8-layered-structure-invalid",
            ) from error
        render_groups: list[tuple[list[int], np.ndarray]] = []
    else:
        render_groups = _render_groups(
            page_mask,
            route_masks,
            manifest,
            strategy=normalized["ownerMaskStrategy"],
        )
    anomalies: list[str] = []
    for member_indices, route_mask in render_groups:
        if not np.any(route_mask):
            continue
        route_entry = manifest[member_indices[0]]
        route = route_entry["route"]
        before = working.copy()
        if route == "deterministic-solid":
            if route_entry["modelVersion"] in {
                "classified-solid-v1",
                "classified-solid-v2",
            }:
                generated = _classified_solid_candidate(
                    working,
                    route_mask,
                    str(route_entry["backgroundCategory"]),
                    opaque_mask=route_entry["modelVersion"] == "classified-solid-v2",
                )
            elif route_entry["modelVersion"] == "boundary-median-solid-v1":
                generated = _solid_candidate(working, route_mask, 16 * scale)
            else:
                raise PageLineageConflict(
                    "G8 deterministic solid model changed before rendering",
                    resource="clean-plate-candidate",
                    reason="g8-background-route-invalid",
                )
        elif route == "controlled-gradient":
            generated = _gradient_candidate(working, route_mask, 24 * scale)
        elif route == "screentone-preserving":
            generated_image = opencv_inpaint(
                Image.fromarray(working, mode="RGB"),
                route_mask,
                method="screentone",
                render_scale=scale,
            )
            generated = np.asarray(generated_image.convert("RGB"), dtype=np.uint8).copy()
        elif route == "classical-fallback":
            generated_image = opencv_inpaint(
                Image.fromarray(working, mode="RGB"),
                route_mask,
                radius=float(normalized["radius"]) * scale,
                method="telea",
                render_scale=scale,
            )
            generated = np.asarray(generated_image.convert("RGB"), dtype=np.uint8).copy()
            anomalies.extend(
                f"classical-complex-fallback:{rows[index].id}" for index in member_indices
            )
        elif route == "ai-inpaint-redraw":
            provider = inpainter(str(route_entry["provider"]))
            observed = str(getattr(provider, "name", route_entry["provider"]))
            if observed != route_entry["provider"]:
                raise ProjectError("Inpainting provider identity changed before generation")
            generated_image = provider.inpaint(
                Image.fromarray(working, mode="RGB"),
                route_mask,
                context_padding=int(normalized["contextPadding"]) * scale,
                inference_padding=int(normalized["inferencePadding"]) * scale,
                feather=0,
                render_scale=scale,
            )
            generated = np.asarray(generated_image.convert("RGB"), dtype=np.uint8).copy()
        if generated.shape != working.shape:
            raise ProjectError("Inpainting provider returned an incompatible image grid")
        selected = route_mask > 0
        working[selected] = generated[selected]
        working[~selected] = before[~selected]
    outside = page_mask == 0
    working[outside] = source_rgb[outside]
    outside_count = int(np.count_nonzero(np.any(working[outside] != source_rgb[outside], axis=1)))
    if outside_count:
        raise PageLineageConflict(
            "Clean-plate candidate changed pixels outside the accepted mask",
            resource="clean-plate-candidate",
            reason="g8-outside-mask-changed",
        )
    result = Image.fromarray(working, mode="RGB")
    if has_alpha:
        result.putalpha(source_image.getchannel("A"))
    payload = _png_bytes(result)
    with Image.open(io.BytesIO(payload)) as verified:
        verified.load()
        verified_width, verified_height = verified.size
        persisted_outside = _outside_mask_change_count(
            source_image,
            verified,
            Image.fromarray(page_mask, mode="L"),
        )
    if persisted_outside:
        raise PageLineageConflict(
            "Persisted clean plate changed pixels outside the accepted mask",
            resource="clean-plate-candidate",
            reason="g8-outside-mask-changed",
        )
    return payload, verified_width, verified_height, persisted_outside, sorted(anomalies)


def publish_clean_plate_candidate(
    store: ProjectStore,
    *,
    job: Job,
    item: JobItem,
    binding: JobMutationBinding,
    inpainter: Callable[[str], Any],
) -> dict[str, Any]:
    if item.image_id is None:
        raise ProjectError("Clean-plate job item has no image")
    candidate_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"g8-clean-plate:{binding['generationId']}:{item.id}",
        )
    )
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, item.image_id)
            generation = session.get(PageGeneration, binding["generationId"])
            if image is None or generation is None:
                raise ProjectError("Clean-plate target disappeared")
            replay = _g8_replay(store, session, image, generation)
            existing = session.scalar(
                select(PageCleanPlateCandidate).where(
                    PageCleanPlateCandidate.job_item_id == item.id
                )
            )
            if existing is not None:
                _validate_candidate_file(store, existing, session=session)
                produced = session.scalar(
                    select(PageLineageEvent).where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.job_item_id == item.id,
                        PageLineageEvent.operation == "clean-plate-candidate-produced",
                    )
                )
                if produced is None or produced.output_checksum != replay["stateChecksum"]:
                    raise PageLineageConflict(
                        "Recovered clean-plate candidate has no exact publication event",
                        resource=f"clean-plate-candidate:{existing.id}",
                        reason="g8-publication-missing",
                    )
                return {
                    "candidateId": existing.id,
                    "candidateChecksum": existing.candidate_checksum,
                    "maskArtifactId": existing.mask_artifact_id,
                    "maskChecksum": existing.mask_checksum,
                    "routeChecksum": existing.route_checksum,
                    "outsideMaskChangeCount": 0,
                    "provider": (
                        existing.provider_ids[0] if len(existing.provider_ids) == 1 else "mixed"
                    ),
                    "modelVersion": "route-manifest-v1",
                    "parameterHash": existing.parameter_hash,
                    "recovered": True,
                }
            if replay["openItemId"] != item.id or replay["terminal"] is not None:
                raise PageLineageConflict(
                    "Clean-plate job is not the current open lineage item",
                    resource=f"job-item:{item.id}",
                    reason="g8-clean-plate-job-stale",
                )
            enqueue = session.scalar(
                select(PageLineageEvent).where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.job_item_id == item.id,
                    PageLineageEvent.operation == "inpaint-job-enqueued",
                )
            )
            if enqueue is None:
                raise PageLineageConflict(
                    "Clean-plate job has no enqueue evidence",
                    resource=f"job-item:{item.id}",
                    reason="g8-publication-missing",
                )
            background, quality, manifest, normalized = _route_manifest(
                store,
                session,
                image,
                generation,
                options=dict(job.options),
                fallback_enabled=bool(replay["fallbackEnabled"]),
                accepted_g7_checksum=replay["g7Checksum"],
                bound_snapshots=(enqueue.evidence or {}).get("layeredStructureSnapshots"),
            )
            if (
                enqueue.input_checksum != replay["stateChecksum"]
                or enqueue.output_checksum != replay["stateChecksum"]
                or (enqueue.evidence or {}).get("routeManifest") != manifest
                or (enqueue.evidence or {}).get("routeChecksum") != normalized["routeChecksum"]
            ):
                raise PageLineageConflict(
                    "Clean-plate route changed after enqueue",
                    resource=f"job-item:{item.id}",
                    reason="g8-background-route-invalid",
                )
            quality_bytes, mask_bytes, scale = _load_generation_inputs(
                store, session, image, generation, replay
            )
            rows = eligible_mask_regions(session, image.id)
            layered_reference_bytes = (
                load_layered_structure_snapshots(
                    store,
                    generation.id,
                    normalized["layeredStructureSnapshots"],
                )
                if "layeredStructureSnapshots" in normalized
                else None
            )
            expected_revision = image.revision
            expected_state = replay["stateChecksum"]

    payload, width, height, outside_count, anomalies = _render_candidate(
        quality_bytes=quality_bytes,
        mask_bytes=mask_bytes,
        rows=rows,
        manifest=manifest,
        normalized=normalized,
        scale=scale,
        inpainter=inpainter,
        layered_reference_bytes=layered_reference_bytes,
    )
    checksum = hashlib.sha256(payload).hexdigest()
    target = _candidate_target(store, binding["generationId"], candidate_id)
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, item.image_id)
            generation = session.get(PageGeneration, binding["generationId"])
            current_job = session.get(Job, job.id)
            current_item = session.get(JobItem, item.id)
            if image is None or generation is None or current_job is None or current_item is None:
                raise ProjectError("Clean-plate target disappeared before publication")
            replay = _g8_replay(store, session, image, generation)
            existing = session.scalar(
                select(PageCleanPlateCandidate).where(
                    PageCleanPlateCandidate.job_item_id == item.id
                )
            )
            if existing is not None:
                _validate_candidate_file(store, existing, session=session)
                return {
                    "candidateId": existing.id,
                    "candidateChecksum": existing.candidate_checksum,
                    "maskArtifactId": existing.mask_artifact_id,
                    "maskChecksum": existing.mask_checksum,
                    "routeChecksum": existing.route_checksum,
                    "outsideMaskChangeCount": 0,
                    "provider": existing.provider_ids[0]
                    if len(existing.provider_ids) == 1
                    else "mixed",
                    "modelVersion": "route-manifest-v1",
                    "parameterHash": existing.parameter_hash,
                    "recovered": True,
                }
            if image.revision != expected_revision or replay["stateChecksum"] != expected_state:
                raise PageLineageConflict(
                    "Page changed while the clean-plate candidate was rendering",
                    resource=f"image:{image.id}",
                    reason="g8-clean-plate-job-stale",
                )
            if current_job.status == "cancelled" or current_item.status == "cancelled":
                raise PageLineageConflict(
                    "Clean-plate job was cancelled before immutable publication",
                    resource=f"job-item:{item.id}",
                    reason="g8-clean-plate-job-cancelled",
                )
            mask = replay["maskArtifact"]
            if mask is None:
                raise PageLineageConflict(
                    "Accepted G7 mask disappeared before publication",
                    resource=f"image:{image.id}",
                    reason="g8-input-checksum-mismatch",
                )
            sequence = len(replay["candidates"]) + 1
            provider_ids = sorted({entry["provider"] for entry in manifest})
            model_versions = sorted({entry["modelVersion"] for entry in manifest})
            # Publish only after the second lineage check and while holding the
            # project lock. A duplicate worker can render concurrently, but it
            # can no longer replace bytes after another worker made them immutable.
            if target.exists():
                try:
                    if target.read_bytes() != payload:
                        raise PageLineageConflict(
                            "Clean-plate candidate write-once artifact collided",
                            resource=f"clean-plate-candidate:{candidate_id}",
                            reason="g8-candidate-artifact-collision",
                        )
                except OSError as error:
                    raise ProjectError("Clean-plate candidate artifact is unavailable") from error
            else:
                atomic_write_bytes(target, payload)
            candidate = PageCleanPlateCandidate(
                id=candidate_id,
                generation_id=generation.id,
                image_id=image.id,
                job_id=current_job.id,
                job_item_id=current_item.id,
                sequence=sequence,
                parent_checksum=replay["g7Checksum"],
                quality_checksum=quality,
                background_checksum=background,
                mask_artifact_id=mask.id,
                mask_checksum=mask.mask_checksum,
                route_manifest=manifest,
                route_checksum=normalized["routeChecksum"],
                origin_kind=_origin_for_manifest(manifest),
                provider_ids=provider_ids,
                model_versions=model_versions,
                parameter_hash=normalized["routeChecksum"],
                candidate_checksum=checksum,
                relative_path=_candidate_relative(generation.id, candidate_id).as_posix(),
                width=width,
                height=height,
                render_scale=scale,
                outside_mask_change_count=outside_count,
                anomalies=anomalies,
            )
            session.add(candidate)
            session.flush()
            after_state = _state_checksum(
                replay["g7Checksum"],
                replay["backgroundChecksum"],
                replay["qualityChecksum"],
                mask.id,
                mask.mask_checksum,
                [*replay["candidates"], candidate],
                replay["reviews"],
                replay["fallbackEvents"],
            )
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-clean-plate-candidate",
                entity_id=candidate.id,
                operation="produce",
                before={},
                after={
                    "candidateChecksum": candidate.candidate_checksum,
                    "maskChecksum": candidate.mask_checksum,
                    "routeChecksum": candidate.route_checksum,
                },
            )
            session.flush()
            provider = provider_ids[0] if len(provider_ids) == 1 else "mixed"
            _append_event(
                session,
                generation,
                operation="clean-plate-candidate-produced",
                gate="G8_cleanPlate",
                state="pending",
                actor=binding["actor"],
                input_checksum=replay["stateChecksum"],
                output_checksum=after_state,
                parent_checksum=replay["g7Checksum"],
                stage="inpaint",
                provider=provider,
                model_version="route-manifest-v1",
                parameter_hash=candidate.parameter_hash,
                job_id=current_job.id,
                job_item_id=current_item.id,
                revision_id=revision.id,
                reason="clean-plate-review-required",
                evidence={
                    "eventType": "clean-plate-candidate-produced",
                    "qualityState": "pending-review",
                    "targetKind": "clean-plate-candidate",
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.candidate_checksum,
                    "g7Checksum": replay["g7Checksum"],
                    "backgroundChecksum": candidate.background_checksum,
                    "qualityChecksum": candidate.quality_checksum,
                    "maskArtifactId": candidate.mask_artifact_id,
                    "maskChecksum": candidate.mask_checksum,
                    "routeManifest": candidate.route_manifest,
                    "routeChecksum": candidate.route_checksum,
                    "originKind": candidate.origin_kind,
                    "providerIds": candidate.provider_ids,
                    "modelVersions": candidate.model_versions,
                    "parameterHash": candidate.parameter_hash,
                    "width": candidate.width,
                    "height": candidate.height,
                    "renderScale": int(candidate.render_scale),
                    "outsideMaskChangeCount": 0,
                    "anomalies": candidate.anomalies,
                    "imageRevision": image.revision,
                },
                started_at=current_item.started_at,
                finished_at=datetime.now(UTC),
            )
        store.write_snapshot()
    return {
        "candidateId": candidate_id,
        "candidateChecksum": checksum,
        "maskArtifactId": mask.id,
        "maskChecksum": mask.mask_checksum,
        "routeChecksum": normalized["routeChecksum"],
        "outsideMaskChangeCount": 0,
        "provider": provider,
        "modelVersion": "route-manifest-v1",
        "parameterHash": normalized["routeChecksum"],
    }


def clean_plate_completion_evidence(
    store: ProjectStore,
    session,
    *,
    job: Job,
    item: JobItem,
    succeeded: bool,
) -> dict[str, Any]:
    if item.image_id is None:
        raise PageLineageConflict(
            "Clean-plate completion has no image",
            resource=f"job-item:{item.id}",
            reason="g8-publication-missing",
        )
    image = session.get(ImageAsset, item.image_id)
    generation_id = next(
        (
            page.get("pageGenerationId")
            for page in (job.lineage_context or {}).get("pages", [])
            if isinstance(page, dict) and page.get("imageId") == item.image_id
        ),
        None,
    )
    generation = session.get(PageGeneration, generation_id) if generation_id else None
    if image is None or generation is None:
        raise PageLineageConflict(
            "Clean-plate completion lost its generation",
            resource=f"job-item:{item.id}",
            reason="generation-mismatch",
        )
    replay = _g8_replay(store, session, image, generation)
    enqueue = session.scalar(
        select(PageLineageEvent).where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "inpaint-job-enqueued",
        )
    )
    candidate = session.scalar(
        select(PageCleanPlateCandidate).where(PageCleanPlateCandidate.job_item_id == item.id)
    )
    produced = session.scalar(
        select(PageLineageEvent).where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "clean-plate-candidate-produced",
        )
    )
    if enqueue is None or replay["openItemId"] != item.id:
        raise PageLineageConflict(
            "Clean-plate completion has no current enqueue evidence",
            resource=f"job-item:{item.id}",
            reason="g8-publication-missing",
        )
    if succeeded:
        if (
            candidate is None
            or produced is None
            or produced.output_checksum != replay["stateChecksum"]
        ):
            raise PageLineageConflict(
                "Strict clean plate cannot complete without an exact candidate publication",
                resource=f"job-item:{item.id}",
                reason="g8-publication-missing",
            )
        _validate_candidate_file(store, candidate, session=session)
        return {
            "outputChecksum": replay["stateChecksum"],
            "provider": candidate.provider_ids[0] if len(candidate.provider_ids) == 1 else "mixed",
            "modelVersion": "route-manifest-v1",
            "parameterHash": candidate.parameter_hash,
            "evidence": {
                "candidateId": candidate.id,
                "candidateChecksum": candidate.candidate_checksum,
                "maskArtifactId": candidate.mask_artifact_id,
                "maskChecksum": candidate.mask_checksum,
                "routeChecksum": candidate.route_checksum,
                "outsideMaskChangeCount": 0,
            },
        }
    if candidate is not None or produced is not None:
        raise PageLineageConflict(
            "A published clean-plate candidate must recover to completion",
            resource=f"job-item:{item.id}",
            reason="g8-published-job-failed",
        )
    return {
        "outputChecksum": None,
        "provider": enqueue.provider,
        "modelVersion": enqueue.model_version,
        "parameterHash": enqueue.parameter_hash,
        "evidence": {"routeChecksum": (enqueue.evidence or {}).get("routeChecksum")},
    }


def _candidate_out(
    session, row: PageCleanPlateCandidate, reviews: list[PageCleanPlateReview]
) -> dict[str, Any]:
    review = next((entry for entry in reviews if entry.candidate_id == row.id), None)
    completed = session.scalar(
        select(PageLineageEvent.id).where(
            PageLineageEvent.generation_id == row.generation_id,
            PageLineageEvent.job_item_id == row.job_item_id,
            PageLineageEvent.operation == "inpaint-job-completed",
        )
    )
    return {
        "candidateId": row.id,
        "sequence": row.sequence,
        "jobId": row.job_id,
        "jobItemId": row.job_item_id,
        "parentChecksum": row.parent_checksum,
        "qualityChecksum": row.quality_checksum,
        "backgroundChecksum": row.background_checksum,
        "maskArtifactId": row.mask_artifact_id,
        "maskChecksum": row.mask_checksum,
        "routeManifest": row.route_manifest,
        "routeChecksum": row.route_checksum,
        "originKind": row.origin_kind,
        "providerIds": row.provider_ids,
        "modelVersions": row.model_versions,
        "parameterHash": row.parameter_hash,
        "candidateChecksum": row.candidate_checksum,
        "width": row.width,
        "height": row.height,
        "renderScale": row.render_scale,
        "outsideMaskChangeCount": row.outside_mask_change_count,
        "anomalies": row.anomalies,
        "completed": completed is not None,
        "review": (
            {
                "id": review.id,
                "state": review.state,
                "reason": review.reason,
                "checks": review.checks,
                "reviewer": review.reviewer,
                "createdAt": review.created_at,
            }
            if review
            else None
        ),
        "createdAt": row.created_at,
    }


def _fallback_allowed(
    candidates: list[PageCleanPlateCandidate],
    reviews: list[PageCleanPlateReview],
    required_complex_ids: set[str],
) -> bool:
    ai_candidates = [
        row
        for row in candidates
        if any(route.get("originKind") == "ai" for route in row.route_manifest)
    ]
    rejected = {
        review.candidate_id
        for review in reviews
        if review.candidate_id is not None and review.state == "rejected"
    }
    ai_ids = {
        route["regionId"]
        for row in ai_candidates
        for route in row.route_manifest
        if route.get("originKind") == "ai"
    }
    return (
        bool(required_complex_ids)
        and bool(ai_candidates)
        and required_complex_ids <= ai_ids
        and all(row.id in rejected for row in ai_candidates)
    )


def clean_plate_gate_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = _active(session, image)
        replay = _g8_replay(store, session, image, generation)
        eligible = eligible_mask_regions(session, image.id)
        routes = [
            {
                "regionId": row.id,
                "backgroundCategory": row.background_category,
                "defaultRoute": _ROUTE_BY_CATEGORY.get(str(row.background_category)),
            }
            for row in eligible
        ]
        terminal = replay["terminal"]
        latest_review = replay["reviews"][-1] if replay["reviews"] else None
        state = (
            terminal.state
            if terminal is not None
            else "rejected"
            if latest_review is not None and latest_review.state == "rejected"
            else "pending"
        )
        accepted = next((row for row in replay["reviews"] if row.state == "accepted"), None)
        return {
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "nextSequence": generation.next_sequence,
            "g7Checksum": replay["g7Checksum"],
            "qualityChecksum": replay["qualityChecksum"],
            "backgroundChecksum": replay["backgroundChecksum"],
            "maskArtifactId": (replay["maskArtifact"].id if replay["maskArtifact"] else None),
            "maskChecksum": (
                replay["maskArtifact"].mask_checksum if replay["maskArtifact"] else None
            ),
            "cleanPlateStateChecksum": replay["stateChecksum"],
            "state": state,
            "routes": routes,
            "candidates": [
                _candidate_out(session, row, replay["reviews"]) for row in replay["candidates"]
            ],
            "acceptedCandidateId": accepted.candidate_id if accepted else None,
            "fallbackEnabled": replay["fallbackEnabled"],
            "fallbackAllowed": bool(eligible)
            and any(row.background_category in _COMPLEX_CATEGORIES for row in eligible)
            and _fallback_allowed(
                replay["candidates"],
                replay["reviews"],
                {row.id for row in eligible if row.background_category in _COMPLEX_CATEGORIES},
            ),
        }


def record_clean_plate_fallback(
    store: ProjectStore,
    image_id: str,
    *,
    enabled: bool,
    reason: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> dict[str, Any]:
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before classical fallback decision",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            generation, actor, expected_sequence = require_image_mutation_lineage(
                store, session, image, lineage
            )
            _require_legacy_route_mutable(session, generation)
            replay = _g8_replay(store, session, image, generation)
            if replay["terminal"] is not None:
                raise PageLineageConflict(
                    "Accepted G8 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g8-clean-plate-accepted",
                )
            if replay["openItemId"] is not None:
                raise PageLineageConflict(
                    "A clean-plate job is active",
                    resource=f"image:{image.id}",
                    reason="g8-clean-plate-job-active",
                )
            if enabled == replay["fallbackEnabled"]:
                raise PageLineageConflict(
                    "Classical fallback already has the requested page state",
                    resource=f"image:{image.id}",
                    reason="g8-classical-fallback-unchanged",
                )
            expected_reason = "all-ai-candidates-rejected" if enabled else "resume-ai-candidates"
            if reason != expected_reason:
                raise PageLineageConflict(
                    "Classical fallback reason does not match the requested state",
                    resource=f"image:{image.id}",
                    reason="g8-classical-fallback-invalid",
                )
            if enabled:
                rows = eligible_mask_regions(session, image.id)
                if not any(row.background_category in _COMPLEX_CATEGORIES for row in rows):
                    raise PageLineageConflict(
                        "Classical fallback is only a complex-background page escape hatch",
                        resource=f"image:{image.id}",
                        reason="g8-classical-fallback-not-applicable",
                    )
                if not _fallback_allowed(
                    replay["candidates"],
                    replay["reviews"],
                    {row.id for row in rows if row.background_category in _COMPLEX_CATEGORIES},
                ):
                    raise PageLineageConflict(
                        "Every applicable same-generation AI candidate must be rejected first",
                        resource=f"image:{image.id}",
                        reason="g8-ai-candidates-not-all-rejected",
                    )
            before_state = replay["stateChecksum"]
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-clean-plate-fallback",
                entity_id=generation.id,
                operation="enable" if enabled else "disable",
                before={"enabled": not enabled},
                after={"enabled": enabled},
            )
            session.flush()
            operation = (
                "clean-plate-fallback-enabled" if enabled else "clean-plate-fallback-disabled"
            )
            provisional = PageLineageEvent(
                generation_id=generation.id,
                sequence=expected_sequence,
                operation=operation,
                state="pending",
                actor_kind=str(actor["actorKind"]),
                actor_id=actor["actorId"],
                task_id=actor["taskId"],
                thread_id=actor["threadId"],
                session_id=actor["sessionId"],
                operation_source=str(actor["operationSource"]),
                reason=reason,
                evidence={},
            )
            after_state = _state_checksum(
                replay["g7Checksum"],
                replay["backgroundChecksum"],
                replay["qualityChecksum"],
                replay["maskArtifact"].id if replay["maskArtifact"] else None,
                replay["maskArtifact"].mask_checksum if replay["maskArtifact"] else None,
                replay["candidates"],
                replay["reviews"],
                [*replay["fallbackEvents"], provisional],
            )
            _append_event(
                session,
                generation,
                operation=operation,
                gate="G8_cleanPlate",
                state="pending",
                actor=actor,
                input_checksum=before_state,
                output_checksum=after_state,
                parent_checksum=replay["g7Checksum"],
                stage="inpaint",
                provider="operator",
                model_version="page-scoped-fallback-v1",
                parameter_hash=_digest({"enabled": enabled, "reason": reason}),
                revision_id=revision.id,
                decision=(
                    "classical-fallback-enabled" if enabled else "classical-fallback-disabled"
                ),
                reason=reason,
                evidence={
                    "eventType": operation,
                    "qualityState": "pending-review",
                    "enabled": enabled,
                    "candidateCount": len(replay["candidates"]),
                    "aiCandidateCount": sum(
                        any(route["originKind"] == "ai" for route in row.route_manifest)
                        for row in replay["candidates"]
                    ),
                    "imageRevision": image.revision,
                },
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return clean_plate_gate_context(store, image_id)


def _validate_review_checks(decision: str, reason: str, checks: list[dict[str, Any]]) -> str:
    by_key = {entry.get("check"): entry.get("passed") for entry in checks}
    if (
        len(checks) != len(CLEAN_PLATE_CHECKS)
        or set(by_key) != set(CLEAN_PLATE_CHECKS)
        or any(type(value) is not bool for value in by_key.values())
        or any(set(entry) != {"check", "passed"} for entry in checks)
    ):
        raise PageLineageConflict(
            "Clean-plate review must contain the exact seven checks",
            resource="clean-plate-review",
            reason="g8-review-checks-invalid",
        )
    failed = [key for key in CLEAN_PLATE_CHECKS if not by_key[key]]
    if decision == "accept":
        if failed or reason != "clean-plate-complete":
            raise PageLineageConflict(
                "Clean-plate acceptance requires all checks to pass",
                resource="clean-plate-review",
                reason="g8-review-decision-invalid",
            )
        return "accepted"
    expected_reason = (
        "multiple-visual-failures"
        if len(failed) > 1
        else _CHECK_REASON.get(failed[0] if failed else "")
    )
    if decision != "reject" or not failed or reason != expected_reason:
        raise PageLineageConflict(
            "Clean-plate rejection must identify its failed visual check",
            resource="clean-plate-review",
            reason="g8-review-decision-invalid",
        )
    return "rejected"


def record_clean_plate_review(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    candidate_id: str | None,
    observed_candidate_checksum: str | None,
    observed_width: int | None,
    observed_height: int | None,
    checks: list[dict[str, Any]],
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before clean-plate review",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            generation, actor, expected_sequence = require_image_mutation_lineage(
                store, session, image, lineage
            )
            _require_legacy_route_mutable(session, generation)
            replay = _g8_replay(store, session, image, generation)
            if replay["terminal"] is not None:
                raise PageLineageConflict(
                    "Accepted G8 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g8-clean-plate-accepted",
                )
            if replay["openItemId"] is not None:
                raise PageLineageConflict(
                    "A clean-plate job is active",
                    resource=f"image:{image.id}",
                    reason="g8-clean-plate-job-active",
                )
            candidate = session.get(PageCleanPlateCandidate, candidate_id) if candidate_id else None
            if replay["maskArtifact"] is None:
                if (
                    decision != "not-applicable"
                    or reason != "no-clean-plate-required"
                    or candidate_id is not None
                    or observed_candidate_checksum is not None
                    or observed_width is not None
                    or observed_height is not None
                    or checks
                    or replay["candidates"]
                ):
                    raise PageLineageConflict(
                        "G8 N/A must be an exact artifact-free decision",
                        resource=f"image:{image.id}",
                        reason="g8-clean-plate-na-invalid",
                    )
                state = "not-applicable"
            else:
                if (
                    decision not in {"accept", "reject"}
                    or candidate is None
                    or candidate.generation_id != generation.id
                    or candidate.image_id != image.id
                    or candidate.candidate_checksum != observed_candidate_checksum
                    or candidate.width != observed_width
                    or candidate.height != observed_height
                    or candidate.parent_checksum != replay["g7Checksum"]
                    or candidate.mask_artifact_id != replay["maskArtifact"].id
                    or candidate.mask_checksum != replay["maskArtifact"].mask_checksum
                    or candidate.outside_mask_change_count != 0
                ):
                    raise PageLineageConflict(
                        "Clean-plate review does not bind the observed immutable candidate",
                        resource=f"image:{image.id}",
                        reason="g8-review-candidate-invalid",
                    )
                if any(review.candidate_id == candidate.id for review in replay["reviews"]):
                    raise PageLineageConflict(
                        "A clean-plate candidate already has an immutable conclusion",
                        resource=f"clean-plate-candidate:{candidate.id}",
                        reason="g8-candidate-already-reviewed",
                    )
                completed = session.scalar(
                    select(PageLineageEvent).where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.job_item_id == candidate.job_item_id,
                        PageLineageEvent.operation == "inpaint-job-completed",
                    )
                )
                if completed is None:
                    raise PageLineageConflict(
                        "Clean-plate candidate job is not completed",
                        resource=f"clean-plate-candidate:{candidate.id}",
                        reason="g8-candidate-not-completed",
                    )
                _validate_candidate_file(store, candidate, session=session)
                state = _validate_review_checks(decision, reason, checks)
                if (
                    state == "accepted"
                    and any(
                        route["route"] in {"classical-fallback", "layered-structure"}
                        for route in candidate.route_manifest
                    )
                    and (
                        not replay["fallbackEnabled"]
                        or not _fallback_allowed(
                            replay["candidates"],
                            replay["reviews"],
                            {
                                row.id
                                for row in eligible_mask_regions(session, image.id)
                                if row.background_category in _COMPLEX_CATEGORIES
                            },
                        )
                    )
                ):
                    raise PageLineageConflict(
                        "Fallback-dependent candidates require enabled fallback and every "
                        "applicable AI candidate rejection",
                        resource=f"clean-plate-candidate:{candidate.id}",
                        reason="g8-ai-candidates-not-all-rejected",
                    )
            review = PageCleanPlateReview(
                generation_id=generation.id,
                image_id=image.id,
                candidate_id=candidate.id if candidate else None,
                sequence=len(replay["reviews"]) + 1,
                state=state,
                reason=reason,
                parent_checksum=replay["g7Checksum"],
                candidate_checksum=candidate.candidate_checksum if candidate else None,
                mask_checksum=candidate.mask_checksum if candidate else None,
                checks=checks,
                reviewer=actor,
            )
            session.add(review)
            session.flush()
            after_state = _state_checksum(
                replay["g7Checksum"],
                replay["backgroundChecksum"],
                replay["qualityChecksum"],
                replay["maskArtifact"].id if replay["maskArtifact"] else None,
                replay["maskArtifact"].mask_checksum if replay["maskArtifact"] else None,
                replay["candidates"],
                [*replay["reviews"], review],
                replay["fallbackEvents"],
            )
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-clean-plate-review",
                entity_id=review.id,
                operation=state,
                before={},
                after={
                    "state": state,
                    "candidateId": review.candidate_id,
                    "candidateChecksum": review.candidate_checksum,
                },
            )
            session.flush()
            provider = (
                candidate.provider_ids[0]
                if candidate and len(candidate.provider_ids) == 1
                else "mixed"
                if candidate
                else "none"
            )
            now = datetime.now(UTC)
            event = _append_event(
                session,
                generation,
                operation="clean-plate-stage-review",
                gate="G8_cleanPlate",
                state=state,
                actor=actor,
                input_checksum=replay["stateChecksum"],
                output_checksum=after_state,
                parent_checksum=replay["g7Checksum"],
                stage="inpaint",
                provider=provider,
                model_version=(
                    "route-manifest-v1" if candidate else "quality-plate-pass-through-v1"
                ),
                parameter_hash=(
                    candidate.parameter_hash if candidate else replay["backgroundChecksum"]
                ),
                revision_id=revision.id,
                decision={
                    "accepted": "clean-plate-accepted",
                    "rejected": "clean-plate-rejected",
                    "not-applicable": "clean-plate-not-applicable",
                }[state],
                reason=reason,
                evidence={
                    "eventType": "clean-plate-stage-review",
                    "qualityState": state,
                    "candidateId": review.candidate_id,
                    "candidateChecksum": review.candidate_checksum,
                    "g7Checksum": replay["g7Checksum"],
                    "backgroundChecksum": replay["backgroundChecksum"],
                    "qualityChecksum": replay["qualityChecksum"],
                    "maskArtifactId": candidate.mask_artifact_id if candidate else None,
                    "maskChecksum": review.mask_checksum,
                    "routeChecksum": candidate.route_checksum if candidate else None,
                    "originKind": candidate.origin_kind if candidate else "no-op",
                    "checks": checks,
                    "imageRevision": image.revision,
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def clean_plate_artifact_path(store: ProjectStore, image_id: str, candidate_id: str) -> Path:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        candidate = session.get(PageCleanPlateCandidate, candidate_id)
        if image is None or candidate is None or candidate.image_id != image.id:
            raise ProjectError("Clean-plate candidate was not found")
        generation = _active(session, image)
        if candidate.generation_id != generation.id:
            raise ProjectError("Clean-plate candidate was not found")
        _g8_replay(store, session, image, generation)
        return _validate_candidate_file(store, candidate, session=session)


def require_current_clean_plate_acceptance(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, Path, Any | None]:
    # The cloud whole-page lane is additive and has its own immutable replay.
    # Import locally to avoid coupling the legacy renderer to the ingest service.
    from manga_localizer.services.cloud_full_page_clean_plates import (
        current_cloud_full_page_acceptance,
    )

    cloud = current_cloud_full_page_acceptance(store, session, image, generation)
    if cloud is not None:
        return cloud
    replay = _g8_replay(store, session, image, generation)
    terminal = replay["terminal"]
    if terminal is None or terminal.state not in {"accepted", "not-applicable"}:
        raise PageLineageConflict(
            "G8 clean plate is not currently accepted",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-not-currently-accepted",
        )
    review = replay["reviews"][-1] if replay["reviews"] else None
    if review is None or review.state != terminal.state:
        raise PageLineageConflict(
            "G8 terminal review is inconsistent",
            resource=f"image:{image.id}",
            reason="g8-clean-plate-terminal-invalid",
        )
    if review.state == "not-applicable":
        if replay["candidates"] or review.candidate_id is not None:
            raise PageLineageConflict(
                "G8 N/A has residual candidate evidence",
                resource=f"image:{image.id}",
                reason="g8-clean-plate-na-invalid",
            )
        quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
        return replay["stateChecksum"], Path(quality["path"]), None
    candidate = session.get(PageCleanPlateCandidate, review.candidate_id)
    if (
        candidate is None
        or candidate.candidate_checksum != review.candidate_checksum
        or candidate.mask_checksum != review.mask_checksum
        or candidate.parent_checksum != replay["g7Checksum"]
    ):
        raise PageLineageConflict(
            "Accepted clean-plate candidate is stale",
            resource=f"image:{image.id}",
            reason="g8-review-candidate-invalid",
        )
    return (
        replay["stateChecksum"],
        _validate_candidate_file(store, candidate, session=session),
        candidate,
    )
