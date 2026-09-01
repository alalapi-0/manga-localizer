from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    PageMaskDraft,
    PageMaskReview,
    Revision,
    TextRegion,
)
from manga_localizer.imaging.inpainting import (
    apply_mask_edits,
    create_mask,
    validate_mask_edits,
    validate_render_scale,
)
from manga_localizer.security import atomic_write_bytes, resolve_write_target, safe_relative_path
from manga_localizer.services.page_lineage import (
    JobMutationBinding,
    PageLineageConflict,
    _append_event,
    _safe_actor,
    require_current_ocr_trust,
    require_current_text_present_quality_plate,
    require_image_mutation_lineage,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)

COVERAGE_CHECKS = (
    "body-glyphs-covered",
    "punctuation-covered",
    "strokes-and-shadows-covered",
    "ruby-covered",
    "antialias-edges-covered",
)
COLLATERAL_CHECKS = (
    "bubble-borders-protected",
    "characters-protected",
    "speed-lines-protected",
    "screentone-protected",
    "nearby-art-protected",
)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _float64_token(value: int | float) -> str:
    """Encode a recipe float without relying on a language's JSON number spelling."""
    return struct.pack(">d", float(value)).hex()


def _draft_checksum(
    parent_checksum: str,
    quality_checksum: str,
    mapping: dict[str, list[str]],
    regions: list[dict[str, Any]],
) -> str:
    digest_regions = [
        {
            "regionId": region["regionId"],
            "maskMode": region["maskMode"],
            "polygon": (
                None
                if region["polygon"] is None
                else [
                    [_float64_token(point[0]), _float64_token(point[1])]
                    for point in region["polygon"]
                ]
            ),
            "padding": region["padding"],
            "dilation": region["dilation"],
            "feather": region["feather"],
            "polarity": region["polarity"],
            "maskEdits": {
                "version": region["maskEdits"]["version"],
                "strokes": [
                    {
                        "mode": stroke["mode"],
                        "radius": _float64_token(stroke["radius"]),
                        "points": [
                            [_float64_token(point[0]), _float64_token(point[1])]
                            for point in stroke["points"]
                        ],
                    }
                    for stroke in region["maskEdits"]["strokes"]
                ],
            },
        }
        for region in regions
    ]
    return _digest(
        {
            "parentChecksum": parent_checksum,
            "qualityChecksum": quality_checksum,
            "rubyRegionIdsByPrimary": mapping,
            "regions": digest_regions,
        }
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
            "G7 requires an active page generation",
            resource=f"image:{image.id}",
            reason="active-generation-missing",
        )
    return generation


def eligible_mask_regions(session, image_id: str) -> list[TextRegion]:
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image_id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    primary_ids = {
        row.id
        for row in rows
        if row.ruby_parent_id is None and row.content_disposition in {"translate", "redraw-art"}
    }
    return [row for row in rows if row.id in primary_ids]


def ruby_mapping(session, image_id: str, rows: list[TextRegion]) -> dict[str, list[str]]:
    primary_ids = sorted(row.id for row in rows)
    ruby_rows = (
        list(
            session.scalars(
                select(TextRegion).where(
                    TextRegion.image_id == image_id,
                    TextRegion.region_type == "ruby",
                    TextRegion.ruby_parent_id.in_(primary_ids),
                )
            ).all()
        )
        if primary_ids
        else []
    )
    return {
        primary_id: sorted(row.id for row in ruby_rows if row.ruby_parent_id == primary_id)
        for primary_id in primary_ids
    }


def mask_job_items_for_generation(
    session,
    generation: PageGeneration,
    *,
    statuses: tuple[str, ...] | None = None,
    exclude_job_id: str | None = None,
) -> list[tuple[JobItem, Job]]:
    """Return only mask items whose signed lineage maps this image to this generation."""
    candidates = list(
        session.execute(
            select(JobItem, Job)
            .join(Job)
            .where(
                Job.kind == "mask",
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
                "Mask job has an ambiguous current-generation lineage binding",
                resource=f"job:{job.id}",
                reason="g7-mask-replay-invalid",
            )
        matched.append((item, job))
    return matched


def _default_recipe(rows: list[TextRegion]) -> list[dict[str, Any]]:
    return [
        {
            "regionId": row.id,
            "maskMode": "region",
            "polygon": None,
            "padding": 2,
            "dilation": 1,
            "feather": 1,
            "polarity": "auto",
            "maskEdits": {"version": 1, "strokes": []},
        }
        for row in rows
    ]


def _canonical_recipe(
    raw: list[dict[str, Any]], rows: list[TextRegion], image: ImageAsset
) -> list[dict[str, Any]]:
    expected = {row.id for row in rows}
    if {item["regionId"] for item in raw} != expected or len(raw) != len(expected):
        raise PageLineageConflict(
            "G7 draft must contain every and only server-derived eligible region",
            resource=f"image:{image.id}",
            reason="g7-mask-eligibility-mismatch",
        )
    normalized: list[dict[str, Any]] = []
    for item in sorted(raw, key=lambda value: value["regionId"]):
        if set(item) != {
            "regionId",
            "maskMode",
            "polygon",
            "padding",
            "dilation",
            "feather",
            "polarity",
            "maskEdits",
        }:
            raise PageLineageConflict(
                "Persisted G7 recipe has unsupported fields",
                resource=f"image:{image.id}",
                reason="g7-mask-draft-invalid",
            )
        polygon = item.get("polygon")
        strokes = item["maskEdits"]
        points = list(polygon or []) + [
            point for stroke in strokes["strokes"] for point in stroke["points"]
        ]
        if any(
            len(point) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in point
            )
            or not 0 <= float(point[0]) <= image.width
            or not 0 <= float(point[1]) <= image.height
            for point in points
        ):
            raise ProjectError("Mask polygon and stroke points must be inside the source grid")
        validate_mask_edits(strokes, width=image.width, height=image.height)
        normalized.append(
            {
                "regionId": item["regionId"],
                "maskMode": item["maskMode"],
                "polygon": polygon,
                "padding": item["padding"],
                "dilation": item["dilation"],
                "feather": item["feather"],
                "polarity": item["polarity"],
                "maskEdits": strokes,
            }
        )
    return normalized


def _require_current_draft(
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    g6_checksum: str,
    quality_checksum: str,
    eligible: list[TextRegion],
    mapping: dict[str, list[str]],
) -> PageMaskDraft:
    draft = session.get(PageMaskDraft, generation.id)
    if draft is None:
        raise PageLineageConflict(
            "Current G7 draft is missing",
            resource=f"image:{image.id}",
            reason="g7-mask-draft-missing",
        )
    canonical = _canonical_recipe(draft.recipe, eligible, image)
    checksum = _draft_checksum(g6_checksum, quality_checksum, mapping, canonical)
    if (
        draft.recipe != canonical
        or draft.image_id != image.id
        or draft.parent_checksum != g6_checksum
        or draft.quality_checksum != quality_checksum
        or draft.state_checksum != checksum
    ):
        raise PageLineageConflict(
            "Persisted G7 draft checksum or recipe is inconsistent",
            resource=f"image:{image.id}",
            reason="g7-mask-draft-invalid",
        )
    return draft


def _artifact_dict(row: PageMaskArtifact) -> dict[str, Any]:
    return {
        "artifactId": row.id,
        "sequence": row.sequence,
        "jobId": row.job_id,
        "jobItemId": row.job_item_id,
        "parentChecksum": row.parent_checksum,
        "qualityChecksum": row.quality_checksum,
        "recipeChecksum": row.draft_checksum,
        "maskChecksum": row.mask_checksum,
        "width": row.width,
        "height": row.height,
        "renderScale": row.render_scale,
        "provider": row.provider,
        "modelVersion": row.model_version,
        "parameterHash": row.parameter_hash,
        "nonzeroPixelCount": row.nonzero_pixels,
        "bbox": row.bbox,
        "createdAt": row.created_at,
    }


def _review_dict(row: PageMaskReview | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "state": row.state,
        "reason": row.reason,
        "artifactId": row.artifact_id,
        "maskChecksum": row.mask_checksum,
        "coverageChecks": row.coverage_checks,
        "collateralChecks": row.collateral_checks,
        "reviewer": row.reviewer,
        "createdAt": row.created_at,
    }


def _state_checksum(
    g6: str,
    quality: str,
    mapping: dict[str, list[str]],
    draft: PageMaskDraft,
    artifacts: list[PageMaskArtifact],
    reviews: list[PageMaskReview],
) -> str:
    return _digest(
        {
            "g6Checksum": g6,
            "qualityChecksum": quality,
            "rubyRegionIdsByPrimary": mapping,
            "draftChecksum": draft.state_checksum,
            "draftRevision": int(draft.revision),
            "artifacts": [
                [
                    row.id,
                    row.sequence,
                    row.job_id,
                    row.job_item_id,
                    row.parent_checksum,
                    row.quality_checksum,
                    row.mask_checksum,
                    row.draft_checksum,
                    row.relative_path,
                    row.width,
                    row.height,
                    row.nonzero_pixels,
                    row.bbox,
                    int(row.render_scale),
                    row.provider,
                    row.model_version,
                    row.parameter_hash,
                ]
                for row in artifacts
            ],
            "reviews": [
                [
                    review.id,
                    review.sequence,
                    review.state,
                    review.reason,
                    review.artifact_id,
                    review.mask_checksum,
                    review.coverage_checks,
                    review.collateral_checks,
                    review.reviewer,
                ]
                for review in reviews
            ],
        }
    )


def _validate_g7_replay(
    session, generation: PageGeneration, *, ignore_job_item_id: str | None = None
) -> None:
    def invalid(message: str, *, resource: str | None = None) -> None:
        raise PageLineageConflict(
            message,
            resource=resource or f"page-generation:{generation.id}",
            reason="g7-mask-replay-invalid",
        )

    allowed = {
        "mask-draft-updated",
        "mask-job-enqueued",
        "mask-artifact-produced",
        "mask-job-completed",
        "mask-job-failed",
        "mask-stage-review",
    }
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
    events = [
        event for event in all_events if event.gate == "G7_mask" or event.operation in allowed
    ]
    if any(event.gate != "G7_mask" or event.operation not in allowed for event in events):
        invalid("G7 operation/gate matrix is invalid")

    g6_terminal = next(
        (
            event
            for event in reversed(all_events)
            if event.gate == "G6_ocr" and event.state in {"accepted", "not-applicable"}
        ),
        None,
    )
    quality_terminal = next(
        (
            event
            for event in reversed(all_events)
            if event.gate == "G3_textPresence"
            and event.state == "accepted"
            and event.output_checksum is not None
        ),
        None,
    )
    if events and (g6_terminal is None or quality_terminal is None):
        invalid("G7 has no exact terminal G6/quality checksum base")
    if not events and g6_terminal is not None and g6_terminal.sequence < len(all_events):
        invalid("Downstream events started before G7")
    if events and (
        quality_terminal.sequence >= g6_terminal.sequence
        or [event.sequence for event in events]
        != list(range(g6_terminal.sequence + 1, g6_terminal.sequence + len(events) + 1))
        or all_events[g6_terminal.sequence : g6_terminal.sequence + len(events)] != events
    ):
        invalid("G7 events are not an exact contiguous block after terminal G6 evidence")
    if not events:
        base_checksum = g6_terminal.output_checksum if g6_terminal else None
        quality_checksum = quality_terminal.output_checksum if quality_terminal else None
    else:
        base_checksum = g6_terminal.output_checksum
        quality_checksum = quality_terminal.output_checksum
    if events and (not _is_sha256(base_checksum) or not _is_sha256(quality_checksum)):
        invalid("G7 terminal checksum base is empty")

    replay_eligible = eligible_mask_regions(session, generation.image_id)
    replay_image = session.get(ImageAsset, generation.image_id)
    if replay_image is None:
        invalid("G7 generation image is missing")
    eligible_count = len(replay_eligible)
    replay_mapping = ruby_mapping(session, generation.image_id, replay_eligible)
    ruby_count = sum(map(len, replay_mapping.values()))
    persisted_draft = session.get(PageMaskDraft, generation.id)
    artifacts = list(
        session.scalars(
            select(PageMaskArtifact)
            .where(PageMaskArtifact.generation_id == generation.id)
            .order_by(PageMaskArtifact.sequence)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(PageMaskReview)
            .where(PageMaskReview.generation_id == generation.id)
            .order_by(PageMaskReview.sequence)
        ).all()
    )
    matched_items = [
        (item, job)
        for item, job in mask_job_items_for_generation(session, generation)
        if item.id != ignore_job_item_id
    ]
    items_by_id = {item.id: item for item, _job in matched_items}
    jobs_by_item = {item.id: job for item, job in matched_items}
    if any(
        item.region_id is not None
        or job.project_id != generation.project_id
        or sum(
            other.job_id == item.job_id and other.image_id == generation.image_id
            for other in items_by_id.values()
        )
        != 1
        for item, job in matched_items
    ):
        invalid("A G7 job does not have exactly one whole-page item for this image")
    if any(
        item.status == "cancelled" or jobs_by_item[item.id].status == "cancelled"
        for item in items_by_id.values()
    ):
        invalid("A current-generation G7 item was cancelled without lineage evidence")

    artifact_by_item = {row.job_item_id: row for row in artifacts}
    if len(artifact_by_item) != len(artifacts):
        invalid("G7 contains duplicate item artifact ownership")

    current_state = base_checksum
    active_draft: PageMaskDraft | None = None
    draft_revision = 0
    prior_draft_checksum: str | None = None
    artifact_prefix: list[PageMaskArtifact] = []
    review_prefix: list[PageMaskReview] = []
    review_index = 0
    produced_by_item: dict[str, PageLineageEvent] = {}
    completed_by_item: dict[str, PageLineageEvent] = {}
    enqueue_state_by_item: dict[str, str | None] = {}
    enqueue_recipe_by_item: dict[str, str] = {}
    enqueued_items: set[str] = set()
    terminal_items: set[str] = set()
    open_item_id: str | None = None
    latest_review: PageMaskReview | None = None
    latest_review_event: PageLineageEvent | None = None
    last_draft_sequence: int | None = None
    last_state_image_revision: int | None = None
    terminal_review_seen = False
    used_revision_ids: set[str] = set()

    common_anchor = {
        "eligibleRegionCount": eligible_count,
        "rubyRegionCount": ruby_count,
        "rubyRegionIdsByPrimary": replay_mapping,
        "qualityChecksum": quality_checksum,
    }

    def actor_for(event: PageLineageEvent) -> dict[str, str | None]:
        try:
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
        except PageLineageConflict:
            invalid("G7 event actor is invalid", resource=f"event:{event.id}")

    def actor_for_job(job: Job) -> dict[str, str | None] | None:
        context = job.lineage_context
        actor = context.get("actor") if isinstance(context, dict) else None
        if not isinstance(actor, dict):
            return None
        try:
            return _safe_actor(actor)
        except PageLineageConflict:
            invalid("G7 job actor is invalid", resource=f"job:{job.id}")

    def expected_sequence_for_job(job: Job) -> int | None:
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

    def validate_common(event: PageLineageEvent, recipe_checksum: str) -> None:
        if (
            event.gate != "G7_mask"
            or event.stage != "mask"
            or event.parent_checksum != base_checksum
            or event.provider != "deterministic-mask"
            or event.model_version != "create-mask-v1"
            or event.parameter_hash != recipe_checksum
        ):
            invalid("G7 common event matrix is invalid", resource=f"event:{event.id}")

    def validate_evidence(
        event: PageLineageEvent, expected: dict[str, Any], *, image_revision: bool = False
    ) -> None:
        nonlocal last_state_image_revision
        evidence = event.evidence if isinstance(event.evidence, dict) else {}
        expected_keys = set(expected)
        if image_revision:
            expected_keys.add("imageRevision")
        if set(evidence) != expected_keys or any(
            evidence.get(key) != value for key, value in expected.items()
        ):
            invalid("G7 event evidence matrix is invalid", resource=f"event:{event.id}")
        if image_revision:
            observed = evidence.get("imageRevision")
            if (
                type(observed) is not int
                or observed < 1
                or observed > replay_image.revision
                or (last_state_image_revision is not None and observed <= last_state_image_revision)
            ):
                invalid("G7 image revision evidence is discontinuous", resource=f"event:{event.id}")
            last_state_image_revision = observed

    def validate_revision(
        event: PageLineageEvent,
        *,
        entity_type: str,
        entity_id: str,
        operation: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        revision = session.get(Revision, event.revision_id)
        if (
            revision is None
            or revision.id in used_revision_ids
            or revision.project_id != generation.project_id
            or revision.entity_type != entity_type
            or revision.entity_id != entity_id
            or revision.operation != operation
            or revision.before != before
            or revision.after != after
        ):
            invalid(
                "G7 event does not bind an exact immutable revision", resource=f"event:{event.id}"
            )
        used_revision_ids.add(revision.id)

    for event in events:
        if terminal_review_seen:
            invalid("G7 changed after an immutable terminal review", resource=f"event:{event.id}")
        event_actor = actor_for(event)
        evidence = event.evidence if isinstance(event.evidence, dict) else {}
        recipe_checksum = evidence.get("recipeChecksum")
        if not _is_sha256(recipe_checksum):
            invalid("G7 recipe checksum evidence is invalid", resource=f"event:{event.id}")
        validate_common(event, recipe_checksum)

        if event.operation == "mask-draft-updated":
            if (
                event.state != "pending"
                or event.decision is not None
                or event.reason != "mask-recipe-updated"
                or event.job_id is not None
                or event.job_item_id is not None
                or event.revision_id is None
                or event.input_checksum != current_state
                or open_item_id is not None
            ):
                invalid("G7 draft event matrix is invalid", resource=f"event:{event.id}")
            draft_revision += 1
            active_draft = PageMaskDraft(
                generation_id=generation.id,
                image_id=generation.image_id,
                parent_checksum=str(base_checksum),
                quality_checksum=str(quality_checksum),
                recipe=[],
                state_checksum=recipe_checksum,
                revision=draft_revision,
            )
            expected_output = _state_checksum(
                str(base_checksum),
                str(quality_checksum),
                replay_mapping,
                active_draft,
                artifact_prefix,
                review_prefix,
            )
            validate_evidence(
                event,
                {
                    "eventType": "mask-draft-updated",
                    "qualityState": "pending-review",
                    **common_anchor,
                    "recipeRegionCount": eligible_count,
                    "recipeChecksum": recipe_checksum,
                },
                image_revision=True,
            )
            if event.output_checksum != expected_output:
                invalid("G7 draft output checksum is not its exact prefix state")
            validate_revision(
                event,
                entity_type="page-mask-draft",
                entity_id=generation.id,
                operation="update",
                before={"checksum": prior_draft_checksum},
                after={"checksum": recipe_checksum},
            )
            current_state = expected_output
            prior_draft_checksum = recipe_checksum
            last_draft_sequence = event.sequence
            continue

        if event.operation in {
            "mask-job-enqueued",
            "mask-artifact-produced",
            "mask-job-completed",
            "mask-job-failed",
        }:
            item_id = event.job_item_id
            if (
                item_id not in items_by_id
                or event.job_id != jobs_by_item[item_id].id
                or event_actor != actor_for_job(jobs_by_item[item_id])
                or (event.operation != "mask-artifact-produced" and event.revision_id is not None)
                or event.decision is not None
            ):
                invalid("G7 job event does not own an exact current-lineage item")
        else:
            item_id = None

        if event.operation == "mask-job-enqueued":
            if (
                active_draft is None
                or recipe_checksum != active_draft.state_checksum
                or event.state != "pending"
                or event.reason != "job-enqueued"
                or event.input_checksum != current_state
                or event.output_checksum != current_state
                or event.sequence != expected_sequence_for_job(jobs_by_item[item_id])
                or item_id in enqueued_items
                or open_item_id is not None
            ):
                invalid("G7 enqueue event matrix or open-item order is invalid")
            validate_evidence(
                event,
                {
                    "eventType": "job-enqueued",
                    "qualityState": "pending-review",
                    "targetKind": "image",
                    **common_anchor,
                    "recipeChecksum": recipe_checksum,
                },
            )
            enqueued_items.add(item_id)
            enqueue_state_by_item[item_id] = current_state
            enqueue_recipe_by_item[item_id] = recipe_checksum
            open_item_id = item_id
            continue

        if event.operation == "mask-artifact-produced":
            artifact = artifact_by_item.get(str(item_id))
            if (
                active_draft is None
                or artifact is None
                or item_id != open_item_id
                or item_id in produced_by_item
                or event.state != "pending"
                or event.reason != "mask-review-required"
                or event.input_checksum != current_state
                or event.revision_id is None
                or artifact.sequence != len(artifact_prefix) + 1
                or artifact.generation_id != generation.id
                or artifact.image_id != generation.image_id
                or artifact.job_id != event.job_id
                or artifact.parent_checksum != base_checksum
                or artifact.quality_checksum != quality_checksum
                or artifact.draft_checksum != active_draft.state_checksum
                or recipe_checksum != artifact.draft_checksum
                or artifact.provider != event.provider
                or artifact.model_version != event.model_version
                or artifact.parameter_hash != event.parameter_hash
            ):
                invalid("G7 produced event does not match its exact immutable artifact")
            expected_facts = {
                "artifactId": artifact.id,
                "maskChecksum": artifact.mask_checksum,
                "recipeChecksum": artifact.draft_checksum,
                "qualityChecksum": artifact.quality_checksum,
                "width": artifact.width,
                "height": artifact.height,
                "renderScale": int(artifact.render_scale),
                "nonzeroPixelCount": artifact.nonzero_pixels,
                "bbox": artifact.bbox,
                "eligibleRegionCount": eligible_count,
                "rubyRegionCount": ruby_count,
                "rubyRegionIdsByPrimary": replay_mapping,
                "provider": artifact.provider,
                "modelVersion": artifact.model_version,
                "parameterHash": artifact.parameter_hash,
            }
            validate_evidence(
                event,
                {
                    "eventType": "mask-artifact-produced",
                    "qualityState": "pending-review",
                    "targetKind": "page-mask",
                    **expected_facts,
                },
                image_revision=True,
            )
            artifact_prefix.append(artifact)
            expected_output = _state_checksum(
                str(base_checksum),
                str(quality_checksum),
                replay_mapping,
                active_draft,
                artifact_prefix,
                review_prefix,
            )
            if event.output_checksum != expected_output:
                invalid("G7 produced output checksum is not its exact prefix state")
            validate_revision(
                event,
                entity_type="page-mask-artifact",
                entity_id=artifact.id,
                operation="produce",
                before={},
                after={
                    "maskChecksum": artifact.mask_checksum,
                    "recipeChecksum": artifact.draft_checksum,
                    "qualityChecksum": artifact.quality_checksum,
                },
            )
            current_state = expected_output
            produced_by_item[str(item_id)] = event
            continue

        if event.operation == "mask-job-completed":
            produced = produced_by_item.get(str(item_id))
            if (
                produced is None
                or item_id != open_item_id
                or item_id in terminal_items
                or event.state != "pending"
                or event.reason != "review-required"
                or event.input_checksum != enqueue_state_by_item.get(str(item_id))
                or event.output_checksum != current_state
                or event.output_checksum != produced.output_checksum
                or recipe_checksum != (produced.evidence or {}).get("recipeChecksum")
            ):
                invalid("G7 completion event matrix or causal order is invalid")
            produced_facts = dict(produced.evidence or {})
            produced_facts.update(
                {
                    "eventType": "job-completed",
                    "qualityState": "pending-review",
                    "targetKind": "image",
                }
            )
            produced_facts.pop("imageRevision", None)
            validate_evidence(event, produced_facts)
            completed_by_item[str(item_id)] = event
            terminal_items.add(str(item_id))
            open_item_id = None
            continue

        if event.operation == "mask-job-failed":
            enqueue_recipe = recipe_checksum
            if (
                item_id != open_item_id
                or item_id in terminal_items
                or item_id in produced_by_item
                or event.state != "blocked"
                or event.reason != "job-execution-failed"
                or event.input_checksum != enqueue_state_by_item.get(str(item_id))
                or event.input_checksum != current_state
                or event.output_checksum is not None
                or recipe_checksum != enqueue_recipe_by_item.get(str(item_id))
            ):
                invalid("G7 failed event matrix or causal order is invalid")
            validate_evidence(
                event,
                {
                    "eventType": "job-failed",
                    "qualityState": "blocked",
                    "targetKind": "image",
                    **common_anchor,
                    "recipeChecksum": enqueue_recipe,
                    "provider": "deterministic-mask",
                    "modelVersion": "create-mask-v1",
                    "parameterHash": enqueue_recipe,
                },
            )
            terminal_items.add(str(item_id))
            open_item_id = None
            continue

        if event.operation == "mask-stage-review":
            if (
                event.job_id is not None
                or event.job_item_id is not None
                or event.revision_id is None
                or open_item_id is not None
                or review_index >= len(reviews)
                or event.input_checksum != current_state
            ):
                invalid("G7 review event matrix or open-item order is invalid")
            review = reviews[review_index]
            review_index += 1
            if review.sequence != review_index:
                invalid("G7 review row sequence is invalid")
            is_na = review.state == "not-applicable"
            if is_na:
                virtual_checksum = _draft_checksum(base_checksum, quality_checksum, {}, [])
                if (
                    replay_eligible
                    or replay_mapping
                    or persisted_draft is not None
                    or active_draft is not None
                    or artifacts
                    or artifact_prefix
                    or matched_items
                    or review.artifact_id is not None
                    or review.mask_checksum is not None
                    or review.image_id != generation.image_id
                    or review.reason != "no-eligible-regions"
                    or review.coverage_checks
                    or review.collateral_checks
                    or recipe_checksum != virtual_checksum
                    or event.state != "not-applicable"
                    or event.decision != "mask-not-applicable"
                    or event.reason != "no-eligible-regions"
                ):
                    invalid("G7 N/A identity or zero-eligible invariant is invalid")
                active_draft = PageMaskDraft(
                    generation_id=generation.id,
                    image_id=generation.image_id,
                    parent_checksum=str(base_checksum),
                    quality_checksum=str(quality_checksum),
                    recipe=[],
                    state_checksum=virtual_checksum,
                    revision=0,
                )
            else:
                artifact = next(
                    (row for row in artifact_prefix if row.id == review.artifact_id), None
                )
                produced = (
                    produced_by_item.get(artifact.job_item_id) if artifact is not None else None
                )
                completed = (
                    completed_by_item.get(artifact.job_item_id) if artifact is not None else None
                )
                checks = [*review.coverage_checks, *review.collateral_checks]
                coverage_failed = any(not row.get("passed") for row in review.coverage_checks)
                collateral_failed = any(not row.get("passed") for row in review.collateral_checks)
                expected_state = (
                    "accepted" if not coverage_failed and not collateral_failed else "rejected"
                )
                expected_reason = (
                    "complete-and-no-collateral"
                    if expected_state == "accepted"
                    else "coverage-and-collateral-failed"
                    if coverage_failed and collateral_failed
                    else "coverage-incomplete"
                    if coverage_failed
                    else "collateral-damage"
                )
                if (
                    active_draft is None
                    or artifact is None
                    or produced is None
                    or completed is None
                    or completed.sequence >= event.sequence
                    or artifact.generation_id != generation.id
                    or artifact.image_id != generation.image_id
                    or review.image_id != generation.image_id
                    or artifact.mask_checksum != review.mask_checksum
                    or artifact.draft_checksum != active_draft.state_checksum
                    or recipe_checksum != artifact.draft_checksum
                    or len(review.coverage_checks) != 5
                    or len(review.collateral_checks) != 5
                    or {row.get("check") for row in review.coverage_checks} != set(COVERAGE_CHECKS)
                    or {row.get("check") for row in review.collateral_checks}
                    != set(COLLATERAL_CHECKS)
                    or any(
                        set(row) != {"check", "passed"} or type(row.get("passed")) is not bool
                        for row in checks
                    )
                    or event.state != review.state
                    or event.state != expected_state
                    or event.reason != review.reason
                    or event.reason != expected_reason
                    or event.decision
                    != ("mask-accepted" if expected_state == "accepted" else "mask-rejected")
                ):
                    invalid("G7 review does not bind exact artifact/QC/completion facts")
                if (
                    latest_review is not None
                    and latest_review.state == "rejected"
                    and (expected_state == "accepted")
                ):
                    if (
                        latest_review_event is None
                        or last_draft_sequence is None
                        or last_draft_sequence <= latest_review_event.sequence
                        or produced.sequence <= last_draft_sequence
                        or artifact.id == latest_review.artifact_id
                    ):
                        invalid("Rejected G7 evidence was accepted without revision/regeneration")
            if review.reviewer != event_actor:
                invalid("G7 review actor does not match the immutable review row")
            expected_review_evidence = {
                "eventType": "mask-stage-review",
                "qualityState": review.state,
                "artifactId": review.artifact_id,
                "maskChecksum": review.mask_checksum,
                "recipeChecksum": recipe_checksum,
                "qualityChecksum": quality_checksum,
                "eligibleRegionCount": eligible_count,
                "rubyRegionCount": ruby_count,
                "rubyRegionIdsByPrimary": replay_mapping,
                "coverageChecks": review.coverage_checks,
                "collateralChecks": review.collateral_checks,
            }
            validate_evidence(event, expected_review_evidence, image_revision=True)
            review_prefix.append(review)
            expected_output = _state_checksum(
                str(base_checksum),
                str(quality_checksum),
                replay_mapping,
                active_draft,
                artifact_prefix,
                review_prefix,
            )
            if event.output_checksum != expected_output:
                invalid("G7 review output checksum is not its exact prefix state")
            validate_revision(
                event,
                entity_type="page-mask-review",
                entity_id=generation.id,
                operation=review.state,
                before={},
                after={
                    "state": review.state,
                    "artifactId": review.artifact_id,
                    "maskChecksum": review.mask_checksum,
                },
            )
            current_state = expected_output
            latest_review = review
            latest_review_event = event
            terminal_review_seen = review.state in {"accepted", "not-applicable"}
            continue

        invalid("G7 contains an unsupported event", resource=f"event:{event.id}")

    if set(items_by_id) != enqueued_items:
        invalid("Persisted G7 job items are not exactly represented by enqueue events")
    for item_id, item in items_by_id.items():
        if item.status == "completed" and item_id not in completed_by_item:
            invalid(
                "Completed G7 item has no exact completion event", resource=f"job-item:{item_id}"
            )
        if item.status == "failed" and (
            item_id not in terminal_items or item_id in completed_by_item
        ):
            invalid("Failed G7 item has no exact failure event", resource=f"job-item:{item_id}")
        if item.status in {"queued", "running"} and item_id in terminal_items:
            invalid("Active G7 item already has terminal evidence", resource=f"job-item:{item_id}")
        if item.status not in {"queued", "running", "completed", "failed"}:
            invalid("G7 item has an unsupported persisted state", resource=f"job-item:{item_id}")
    if open_item_id is not None and items_by_id[open_item_id].status not in {"queued", "running"}:
        invalid("G7 open item is not recoverable")
    if len(artifact_prefix) != len(artifacts) or review_index != len(reviews):
        invalid("G7 immutable rows are not exactly represented by the event stream")
    if (persisted_draft is None) != (draft_revision == 0):
        invalid("G7 mutable draft representation is incomplete")
    if persisted_draft is not None and (
        active_draft is None
        or persisted_draft.image_id != generation.image_id
        or persisted_draft.revision != draft_revision
        or persisted_draft.state_checksum != active_draft.state_checksum
        or persisted_draft.parent_checksum != base_checksum
        or persisted_draft.quality_checksum != quality_checksum
    ):
        invalid("G7 current draft does not match the replayed draft prefix")
    if events and events[-1].sequence < len(all_events) and not terminal_review_seen:
        invalid("Downstream events started before an immutable terminal G7 review")


def _require_latest_state_event(
    session, generation: PageGeneration, expected_checksum: str
) -> None:
    latest = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.operation.in_(
                ("mask-draft-updated", "mask-artifact-produced", "mask-stage-review")
            ),
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if latest is None or latest.output_checksum != expected_checksum:
        raise PageLineageConflict(
            "Persisted G7 state does not match the latest state-bearing event "
            f"({latest.output_checksum if latest else None} != {expected_checksum})",
            resource=f"page-generation:{generation.id}",
            reason="g7-mask-replay-invalid",
        )


def mask_gate_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = _active(session, image)
        _validate_g7_replay(session, generation)
        g6, _ = require_current_ocr_trust(store, session, image, generation)
        quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
        eligible = eligible_mask_regions(session, image.id)
        if not eligible:
            _validate_no_legacy_mask(store, image)
        mapping = ruby_mapping(session, image.id, eligible)
        draft = session.get(PageMaskDraft, generation.id)
        if draft is None:
            recipe = _default_recipe(eligible)
            draft = PageMaskDraft(
                generation_id=generation.id,
                image_id=image.id,
                parent_checksum=g6,
                quality_checksum=quality["checksum"],
                recipe=recipe,
                state_checksum=_draft_checksum(g6, quality["checksum"], mapping, recipe),
                revision=0,
            )
        else:
            draft = _require_current_draft(
                session,
                image=image,
                generation=generation,
                g6_checksum=g6,
                quality_checksum=quality["checksum"],
                eligible=eligible,
                mapping=mapping,
            )
        artifacts = list(
            session.scalars(
                select(PageMaskArtifact)
                .where(PageMaskArtifact.generation_id == generation.id)
                .order_by(PageMaskArtifact.sequence)
            ).all()
        )
        for artifact in artifacts:
            _validate_artifact_file(store, artifact)
        reviews = list(
            session.scalars(
                select(PageMaskReview)
                .where(PageMaskReview.generation_id == generation.id)
                .order_by(PageMaskReview.sequence)
            ).all()
        )
        review = reviews[-1] if reviews else None
        state = review.state if review is not None else "pending"
        mask_state_checksum = _state_checksum(
            g6, quality["checksum"], mapping, draft, artifacts, reviews
        )
        if draft.revision != 0 or reviews:
            _require_latest_state_event(session, generation, mask_state_checksum)
        return {
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "nextSequence": generation.next_sequence,
            "g6Checksum": g6,
            "qualityChecksum": quality["checksum"],
            "maskStateChecksum": mask_state_checksum,
            "state": state,
            "eligibleRegionIds": [row.id for row in eligible],
            "rubyRegionIdsByPrimary": mapping,
            "draft": {
                "revision": draft.revision,
                "stateChecksum": draft.state_checksum,
                "regions": draft.recipe,
            },
            "artifacts": [_artifact_dict(row) for row in artifacts],
            "selectedArtifactId": review.artifact_id if review is not None else None,
            "review": _review_dict(review),
        }


def current_mask_state_checksum(
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    g6_checksum: str,
    quality_checksum: str,
    replay_ignore_job_item_id: str | None = None,
) -> str:
    _validate_g7_replay(session, generation, ignore_job_item_id=replay_ignore_job_item_id)
    eligible = eligible_mask_regions(session, image.id)
    mapping = ruby_mapping(session, image.id, eligible)
    draft = _require_current_draft(
        session,
        image=image,
        generation=generation,
        g6_checksum=g6_checksum,
        quality_checksum=quality_checksum,
        eligible=eligible,
        mapping=mapping,
    )
    artifacts = list(
        session.scalars(
            select(PageMaskArtifact)
            .where(PageMaskArtifact.generation_id == generation.id)
            .order_by(PageMaskArtifact.sequence)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(PageMaskReview)
            .where(PageMaskReview.generation_id == generation.id)
            .order_by(PageMaskReview.sequence)
        ).all()
    )
    checksum = _state_checksum(g6_checksum, quality_checksum, mapping, draft, artifacts, reviews)
    _require_latest_state_event(session, generation, checksum)
    return checksum


def update_mask_draft(
    store: ProjectStore,
    image_id: str,
    *,
    regions: list[dict[str, Any]],
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
                    "Image changed before mask draft update",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            generation, actor, expected_sequence = require_image_mutation_lineage(
                store, session, image, lineage
            )
            _validate_g7_replay(session, generation)
            g6, _ = require_current_ocr_trust(store, session, image, generation)
            quality, _ = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            eligible = eligible_mask_regions(session, image.id)
            if not eligible:
                raise PageLineageConflict(
                    "Zero-eligible pages cannot have a G7 mask draft",
                    resource=f"image:{image.id}",
                    reason="g7-mask-not-applicable",
                )
            latest = session.scalar(
                select(PageMaskReview)
                .where(PageMaskReview.generation_id == generation.id)
                .order_by(PageMaskReview.sequence.desc())
                .limit(1)
            )
            if latest is not None and latest.state in {"accepted", "not-applicable"}:
                raise PageLineageConflict(
                    "Accepted G7 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g7-mask-accepted",
                )
            if mask_job_items_for_generation(session, generation, statuses=("queued", "running")):
                raise PageLineageConflict(
                    "A mask job is active",
                    resource=f"image:{image.id}",
                    reason="g7-mask-job-active",
                )
            canonical = _canonical_recipe(regions, eligible, image)
            mapping = ruby_mapping(session, image.id, eligible)
            checksum = _draft_checksum(g6, quality["checksum"], mapping, canonical)
            draft = session.get(PageMaskDraft, generation.id)
            if draft is not None:
                draft = _require_current_draft(
                    session,
                    image=image,
                    generation=generation,
                    g6_checksum=g6,
                    quality_checksum=quality["checksum"],
                    eligible=eligible,
                    mapping=mapping,
                )
            before = draft.state_checksum if draft else None
            artifacts = list(
                session.scalars(
                    select(PageMaskArtifact)
                    .where(PageMaskArtifact.generation_id == generation.id)
                    .order_by(PageMaskArtifact.sequence)
                ).all()
            )
            reviews = list(
                session.scalars(
                    select(PageMaskReview)
                    .where(PageMaskReview.generation_id == generation.id)
                    .order_by(PageMaskReview.sequence)
                ).all()
            )
            if draft is None:
                virtual = PageMaskDraft(
                    generation_id=generation.id,
                    image_id=image.id,
                    parent_checksum=g6,
                    quality_checksum=quality["checksum"],
                    recipe=_default_recipe(eligible),
                    state_checksum=_draft_checksum(
                        g6, quality["checksum"], mapping, _default_recipe(eligible)
                    ),
                    revision=0,
                )
                before_state = (
                    g6
                    if not artifacts and not reviews
                    else _state_checksum(
                        g6, quality["checksum"], mapping, virtual, artifacts, reviews
                    )
                )
            else:
                before_state = _state_checksum(
                    g6, quality["checksum"], mapping, draft, artifacts, reviews
                )
            if draft is None:
                draft = PageMaskDraft(
                    generation_id=generation.id,
                    image_id=image.id,
                    parent_checksum=g6,
                    quality_checksum=quality["checksum"],
                    recipe=canonical,
                    state_checksum=checksum,
                )
                session.add(draft)
            else:
                (
                    draft.recipe,
                    draft.state_checksum,
                    draft.parent_checksum,
                    draft.quality_checksum,
                ) = canonical, checksum, g6, quality["checksum"]
                draft.revision += 1
            session.flush()
            after_state = _state_checksum(
                g6, quality["checksum"], mapping, draft, artifacts, reviews
            )
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-mask-draft",
                entity_id=generation.id,
                operation="update",
                before={"checksum": before},
                after={"checksum": checksum},
            )
            session.flush()
            now = datetime.now(UTC)
            _append_event(
                session,
                generation,
                operation="mask-draft-updated",
                gate="G7_mask",
                state="pending",
                actor=actor,
                input_checksum=before_state,
                output_checksum=after_state,
                parent_checksum=g6,
                stage="mask",
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=checksum,
                revision_id=revision.id,
                reason="mask-recipe-updated",
                evidence={
                    "eventType": "mask-draft-updated",
                    "qualityState": "pending-review",
                    "eligibleRegionCount": len(eligible),
                    "recipeRegionCount": len(canonical),
                    "recipeChecksum": checksum,
                    "qualityChecksum": quality["checksum"],
                    "rubyRegionCount": sum(map(len, mapping.values())),
                    "rubyRegionIdsByPrimary": mapping,
                    "imageRevision": image.revision,
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return mask_gate_context(store, image_id)


def publish_mask_artifact(
    store: ProjectStore, *, job: Job, item: JobItem, binding: JobMutationBinding
) -> dict[str, Any]:
    if item.image_id is None:
        raise ProjectError("Mask job item has no image")
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, item.image_id)
            generation = session.get(PageGeneration, binding["generationId"])
            if image is None or generation is None:
                raise ProjectError("Mask target disappeared")
            _validate_g7_replay(session, generation)
            g6, _ = require_current_ocr_trust(store, session, image, generation)
            quality, _ = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            eligible = eligible_mask_regions(session, image.id)
            if not eligible:
                raise PageLineageConflict(
                    "Zero-eligible pages cannot generate a mask",
                    resource=f"image:{image.id}",
                    reason="g7-mask-not-applicable",
                )
            mapping = ruby_mapping(session, image.id, eligible)
            draft = _require_current_draft(
                session,
                image=image,
                generation=generation,
                g6_checksum=g6,
                quality_checksum=quality["checksum"],
                eligible=eligible,
                mapping=mapping,
            )
            existing = session.scalar(
                select(PageMaskArtifact).where(PageMaskArtifact.job_item_id == item.id)
            )
            if existing is not None:
                _validate_artifact_file(store, existing)
                produced = session.scalar(
                    select(PageLineageEvent).where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.job_item_id == item.id,
                        PageLineageEvent.operation == "mask-artifact-produced",
                    )
                )
                if (
                    produced is None
                    or existing.generation_id != generation.id
                    or existing.image_id != image.id
                    or existing.parent_checksum != g6
                    or existing.quality_checksum != quality["checksum"]
                    or existing.draft_checksum != draft.state_checksum
                    or produced.output_checksum is None
                    or produced.provider != existing.provider
                    or produced.model_version != existing.model_version
                    or produced.parameter_hash != existing.parameter_hash
                    or (produced.evidence or {}).get("artifactId") != existing.id
                    or (produced.evidence or {}).get("maskChecksum") != existing.mask_checksum
                ):
                    raise PageLineageConflict(
                        "Recovered mask artifact does not match its publication event",
                        resource=f"mask-artifact:{existing.id}",
                        reason="g7-publication-missing",
                    )
                return {
                    "artifactId": existing.id,
                    "maskChecksum": existing.mask_checksum,
                    "count": len(eligible),
                    "recovered": True,
                }
            all_rows = list(
                session.scalars(select(TextRegion).where(TextRegion.image_id == image.id)).all()
            )
            by_id = {row.id: row for row in all_rows}
            with Image.open(quality["path"]) as plate:
                grid_width, grid_height = plate.size
            scale_x, scale_y = grid_width / image.width, grid_height / image.height
            if abs(scale_x - scale_y) > 1e-6 or scale_x <= 0:
                raise PageLineageConflict(
                    "Quality plate grid is not a uniform source scale",
                    resource=f"image:{image.id}",
                    reason="g7-quality-grid-invalid",
                )
            scale = validate_render_scale(round(scale_x))
            if abs(scale_x - scale) > 1e-6:
                raise PageLineageConflict(
                    "Quality plate must use a 1x to 4x integer render scale",
                    resource=f"image:{image.id}",
                    reason="g7-quality-grid-invalid",
                )
            array = np.zeros((grid_height, grid_width), dtype=np.uint8)
            page_edit_regions: list[dict[str, Any]] = []
            mapping = ruby_mapping(session, image.id, eligible)
            for recipe in draft.recipe:
                group: list[dict[str, Any]] = []
                for region_id in [recipe["regionId"], *mapping[recipe["regionId"]]]:
                    row = by_id[region_id]
                    region = {
                        "x": row.x * scale,
                        "y": row.y * scale,
                        "width": row.width * scale,
                        "height": row.height * scale,
                        "rotation": row.rotation,
                        "maskMode": (
                            recipe["maskMode"] if region_id == recipe["regionId"] else "region"
                        ),
                        "textPolarity": recipe["polarity"],
                        "padding": recipe["padding"] * scale,
                        "maskEdits": {"version": 1, "strokes": []},
                    }
                    if region_id == recipe["regionId"]:
                        region["maskEdits"] = {
                            "version": 1,
                            "strokes": [
                                {
                                    "mode": stroke["mode"],
                                    "radius": stroke["radius"] * scale,
                                    "points": [
                                        [point[0] * scale, point[1] * scale]
                                        for point in stroke["points"]
                                    ],
                                }
                                for stroke in recipe["maskEdits"]["strokes"]
                            ],
                        }
                        page_edit_regions.append(region)
                    if region_id == recipe["regionId"] and recipe.get("polygon"):
                        region["polygon"] = [
                            [point[0] * scale, point[1] * scale] for point in recipe["polygon"]
                        ]
                    group.append(region)
                component = create_mask(
                    quality["path"],
                    group,
                    dilation=recipe["dilation"] * scale,
                    feather=recipe["feather"] * scale,
                    render_scale=scale,
                )
                array = np.maximum(array, component)
            # Per-recipe morphology/feathering happens before composition, but
            # the persisted page recipe is the final authority. Replay all
            # primary strokes in canonical recipe/stroke order so an eraser
            # can remove another overlapping region and a later brush can
            # restore an earlier erase.
            apply_mask_edits(array, page_edit_regions, mode=None, render_scale=scale)
            positions = np.argwhere(array > 0)
            if not positions.size:
                raise PageLineageConflict(
                    "Generated mask is empty", resource=f"image:{image.id}", reason="g7-mask-empty"
                )
            y0, x0 = positions.min(axis=0)
            y1, x1 = positions.max(axis=0)
            buffer = io.BytesIO()
            Image.fromarray(array, mode="L").save(buffer, format="PNG", optimize=False)
            payload = buffer.getvalue()
            checksum = hashlib.sha256(payload).hexdigest()
            artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"g7-mask:{generation.id}:{item.id}"))
            relative = Path("generated") / "lineage-masks" / generation.id / f"{artifact_id}.png"
            target = resolve_write_target(
                store.root, relative, protected_roots=(store.source_root,)
            )
            atomic_write_bytes(target, payload)
            sequence = (
                session.scalar(
                    select(PageMaskArtifact.sequence)
                    .where(PageMaskArtifact.generation_id == generation.id)
                    .order_by(PageMaskArtifact.sequence.desc())
                    .limit(1)
                )
                or 0
            ) + 1
            artifact = PageMaskArtifact(
                id=artifact_id,
                generation_id=generation.id,
                image_id=image.id,
                job_id=job.id,
                job_item_id=item.id,
                sequence=sequence,
                parent_checksum=g6,
                quality_checksum=quality["checksum"],
                draft_checksum=draft.state_checksum,
                mask_checksum=checksum,
                relative_path=relative.as_posix(),
                width=grid_width,
                height=grid_height,
                render_scale=scale,
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=draft.state_checksum,
                nonzero_pixels=int((array > 0).sum()),
                bbox={
                    "x": int(x0),
                    "y": int(y0),
                    "width": int(x1 - x0 + 1),
                    "height": int(y1 - y0 + 1),
                },
            )
            session.add(artifact)
            session.flush()
            mapping = ruby_mapping(session, image.id, eligible)
            artifacts = list(
                session.scalars(
                    select(PageMaskArtifact)
                    .where(PageMaskArtifact.generation_id == generation.id)
                    .order_by(PageMaskArtifact.sequence)
                ).all()
            )
            reviews = list(
                session.scalars(
                    select(PageMaskReview)
                    .where(PageMaskReview.generation_id == generation.id)
                    .order_by(PageMaskReview.sequence)
                ).all()
            )
            before_state = _state_checksum(
                g6, quality["checksum"], mapping, draft, artifacts[:-1], reviews
            )
            after_state = _state_checksum(
                g6, quality["checksum"], mapping, draft, artifacts, reviews
            )
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-mask-artifact",
                entity_id=artifact.id,
                operation="produce",
                before={},
                after={
                    "maskChecksum": checksum,
                    "recipeChecksum": draft.state_checksum,
                    "qualityChecksum": quality["checksum"],
                },
            )
            session.flush()
            _append_event(
                session,
                generation,
                operation="mask-artifact-produced",
                gate="G7_mask",
                state="pending",
                actor=binding["actor"],
                input_checksum=before_state,
                output_checksum=after_state,
                parent_checksum=g6,
                stage="mask",
                provider=artifact.provider,
                model_version=artifact.model_version,
                parameter_hash=artifact.parameter_hash,
                job_id=job.id,
                job_item_id=item.id,
                revision_id=revision.id,
                reason="mask-review-required",
                evidence={
                    "eventType": "mask-artifact-produced",
                    "qualityState": "pending-review",
                    "targetKind": "page-mask",
                    "artifactId": artifact.id,
                    "recipeChecksum": draft.state_checksum,
                    "maskChecksum": checksum,
                    "qualityChecksum": quality["checksum"],
                    "width": grid_width,
                    "height": grid_height,
                    "renderScale": scale,
                    "nonzeroPixelCount": artifact.nonzero_pixels,
                    "bbox": artifact.bbox,
                    "eligibleRegionCount": len(eligible),
                    "rubyRegionCount": sum(map(len, mapping.values())),
                    "rubyRegionIdsByPrimary": mapping,
                    "imageRevision": image.revision,
                    "provider": artifact.provider,
                    "modelVersion": artifact.model_version,
                    "parameterHash": artifact.parameter_hash,
                },
                started_at=item.started_at,
                finished_at=datetime.now(UTC),
            )
            return {
                "artifactId": artifact.id,
                "maskChecksum": checksum,
                "count": len(eligible),
                "width": grid_width,
                "height": grid_height,
                "renderScale": scale,
                "nonzeroPixelCount": artifact.nonzero_pixels,
                "bbox": artifact.bbox,
                "provider": artifact.provider,
                "modelVersion": artifact.model_version,
                "parameterHash": artifact.parameter_hash,
            }


def _validate_artifact_file(store: ProjectStore, row: PageMaskArtifact) -> Path:
    canonical_relative = (
        Path("generated") / "lineage-masks" / row.generation_id / f"{row.id}.png"
    ).as_posix()
    if row.relative_path != canonical_relative:
        raise PageLineageConflict(
            "Mask artifact path is not canonical",
            resource=f"mask-artifact:{row.id}",
            reason="g7-mask-artifact-checksum-mismatch",
        )
    path = resolve_write_target(
        store.root, Path(row.relative_path), protected_roots=(store.source_root,)
    )
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row.mask_checksum:
            raise ValueError("checksum")
        with Image.open(io.BytesIO(payload)) as opened:
            if (
                opened.format != "PNG"
                or opened.mode != "L"
                or opened.size != (row.width, row.height)
            ):
                raise ValueError("grid")
            array = np.asarray(opened)
        positions = np.argwhere(array > 0)
        if not positions.size or int(positions.shape[0]) != row.nonzero_pixels:
            raise ValueError("nonzero")
        y0, x0 = positions.min(axis=0)
        y1, x1 = positions.max(axis=0)
        if row.bbox != {
            "x": int(x0),
            "y": int(y0),
            "width": int(x1 - x0 + 1),
            "height": int(y1 - y0 + 1),
        }:
            raise ValueError("bbox")
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "Mask artifact is unavailable or changed",
            resource=f"mask-artifact:{row.id}",
            reason="g7-mask-artifact-checksum-mismatch",
        ) from error
    return path


def _validate_no_legacy_mask(store: ProjectStore, image: ImageAsset) -> None:
    legacy = resolve_write_target(
        store.root,
        Path("generated") / "masks" / safe_relative_path(image.relative_path).with_suffix(".png"),
        protected_roots=(store.source_root,),
    )
    if not legacy.is_file():
        return
    try:
        with Image.open(legacy) as opened:
            if np.any(np.asarray(opened.convert("L")) > 0):
                raise PageLineageConflict(
                    "N/A page has a nonzero legacy mask",
                    resource=f"image:{image.id}",
                    reason="g7-mask-na-residual",
                )
    except OSError as error:
        raise PageLineageConflict(
            "N/A page has an unreadable legacy mask",
            resource=f"image:{image.id}",
            reason="g7-mask-na-residual",
        ) from error


def mask_artifact_path(store: ProjectStore, image_id: str, artifact_id: str) -> Path:
    with store.session() as session:
        row = session.scalar(
            select(PageMaskArtifact).where(
                PageMaskArtifact.id == artifact_id, PageMaskArtifact.image_id == image_id
            )
        )
        if row is None:
            raise ProjectError("Mask artifact was not found")
        image = session.get(ImageAsset, image_id)
        if image is None or _active(session, image).id != row.generation_id:
            raise ProjectError("Mask artifact was not found")
        return _validate_artifact_file(store, row)


def record_mask_review(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    selected_artifact_id: str | None,
    observed_mask_checksum: str | None,
    coverage_checks: list[dict[str, Any]],
    collateral_checks: list[dict[str, Any]],
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
                    "Image changed before mask review",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            generation, actor, expected_sequence = require_image_mutation_lineage(
                store, session, image, lineage
            )
            _validate_g7_replay(session, generation)
            g6, _ = require_current_ocr_trust(store, session, image, generation)
            quality, _ = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            eligible = eligible_mask_regions(session, image.id)
            mapping = ruby_mapping(session, image.id, eligible)
            draft = session.get(PageMaskDraft, generation.id)
            if draft is not None:
                draft = _require_current_draft(
                    session,
                    image=image,
                    generation=generation,
                    g6_checksum=g6,
                    quality_checksum=quality["checksum"],
                    eligible=eligible,
                    mapping=mapping,
                )
            if mask_job_items_for_generation(session, generation, statuses=("queued", "running")):
                raise PageLineageConflict(
                    "A mask job is active",
                    resource=f"image:{image.id}",
                    reason="g7-mask-job-active",
                )
            expected_cov, expected_col = set(COVERAGE_CHECKS), set(COLLATERAL_CHECKS)
            cov = {entry["check"]: entry["passed"] for entry in coverage_checks}
            col = {entry["check"]: entry["passed"] for entry in collateral_checks}
            artifact = (
                session.get(PageMaskArtifact, selected_artifact_id)
                if selected_artifact_id
                else None
            )
            if not eligible:
                if (
                    decision != "not-applicable"
                    or reason != "no-eligible-regions"
                    or selected_artifact_id
                    or observed_mask_checksum
                    or coverage_checks
                    or collateral_checks
                ):
                    raise PageLineageConflict(
                        "Zero-eligible G7 must be exact N/A",
                        resource=f"image:{image.id}",
                        reason="g7-mask-na-invalid",
                    )
                if session.scalar(
                    select(PageMaskArtifact.id)
                    .where(PageMaskArtifact.generation_id == generation.id)
                    .limit(1)
                ):
                    raise PageLineageConflict(
                        "N/A page has residual mask artifact",
                        resource=f"image:{image.id}",
                        reason="g7-mask-na-residual",
                    )
                _validate_no_legacy_mask(store, image)
                state = "not-applicable"
            else:
                if (
                    decision not in {"accept", "reject"}
                    or artifact is None
                    or artifact.generation_id != generation.id
                    or artifact.image_id != image.id
                    or artifact.mask_checksum != observed_mask_checksum
                ):
                    raise PageLineageConflict(
                        "Mask review artifact identity is invalid",
                        resource=f"image:{image.id}",
                        reason="g7-mask-review-artifact-invalid",
                    )
                if (
                    draft is None
                    or artifact.draft_checksum != draft.state_checksum
                    or artifact.parent_checksum != g6
                    or artifact.quality_checksum != quality["checksum"]
                ):
                    raise PageLineageConflict(
                        "Mask artifact is not derived from the current draft and quality plate",
                        resource=f"mask-artifact:{artifact.id}",
                        reason="g7-mask-artifact-stale",
                    )
                _validate_artifact_file(store, artifact)
                completed = session.scalar(
                    select(PageLineageEvent).where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.job_item_id == artifact.job_item_id,
                        PageLineageEvent.operation == "mask-job-completed",
                    )
                )
                produced = session.scalar(
                    select(PageLineageEvent).where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.job_item_id == artifact.job_item_id,
                        PageLineageEvent.operation == "mask-artifact-produced",
                    )
                )
                if (
                    produced is None
                    or completed is None
                    or completed.sequence <= produced.sequence
                    or completed.output_checksum != produced.output_checksum
                    or (produced.evidence or {}).get("artifactId") != artifact.id
                    or (produced.evidence or {}).get("maskChecksum") != artifact.mask_checksum
                    or (produced.evidence or {}).get("recipeChecksum") != artifact.draft_checksum
                    or (produced.evidence or {}).get("qualityChecksum") != artifact.quality_checksum
                    or (produced.evidence or {}).get("eligibleRegionCount") != len(eligible)
                    or (produced.evidence or {}).get("rubyRegionIdsByPrimary") != mapping
                    or (produced.evidence or {}).get("rubyRegionCount")
                    != sum(map(len, mapping.values()))
                    or produced.provider != artifact.provider
                    or produced.model_version != artifact.model_version
                    or produced.parameter_hash != artifact.parameter_hash
                ):
                    raise PageLineageConflict(
                        "Mask artifact job is not strictly completed",
                        resource=f"mask-artifact:{artifact.id}",
                        reason="g7-mask-job-not-completed",
                    )
                if (
                    set(cov) != expected_cov
                    or len(coverage_checks) != 5
                    or set(col) != expected_col
                    or len(collateral_checks) != 5
                ):
                    raise PageLineageConflict(
                        "Mask review must contain the exact ten checks",
                        resource=f"image:{image.id}",
                        reason="g7-mask-review-checks-invalid",
                    )
                coverage_failed, collateral_failed = not all(cov.values()), not all(col.values())
                expected_reason = (
                    "complete-and-no-collateral"
                    if not coverage_failed and not collateral_failed
                    else "coverage-and-collateral-failed"
                    if coverage_failed and collateral_failed
                    else "coverage-incomplete"
                    if coverage_failed
                    else "collateral-damage"
                )
                if reason != expected_reason or (decision == "accept") != (
                    not coverage_failed and not collateral_failed
                ):
                    raise PageLineageConflict(
                        "Mask decision does not match check results",
                        resource=f"image:{image.id}",
                        reason="g7-mask-review-decision-invalid",
                    )
                state = "accepted" if decision == "accept" else "rejected"
            latest = session.scalar(
                select(PageMaskReview)
                .where(PageMaskReview.generation_id == generation.id)
                .order_by(PageMaskReview.sequence.desc())
                .limit(1)
            )
            if latest is not None and latest.state in {"accepted", "not-applicable"}:
                raise PageLineageConflict(
                    "Accepted G7 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g7-mask-accepted",
                )
            if state == "accepted" and latest is not None and latest.state == "rejected":
                prior_review_event = session.scalar(
                    select(PageLineageEvent)
                    .where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.operation == "mask-stage-review",
                        PageLineageEvent.state == "rejected",
                    )
                    .order_by(PageLineageEvent.sequence.desc())
                    .limit(1)
                )
                later_draft_event = session.scalar(
                    select(PageLineageEvent)
                    .where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.operation == "mask-draft-updated",
                        PageLineageEvent.sequence
                        > (prior_review_event.sequence if prior_review_event else 10**18),
                    )
                    .order_by(PageLineageEvent.sequence.desc())
                    .limit(1)
                )
                produced_event = session.scalar(
                    select(PageLineageEvent).where(
                        PageLineageEvent.generation_id == generation.id,
                        PageLineageEvent.job_item_id == artifact.job_item_id,
                        PageLineageEvent.operation == "mask-artifact-produced",
                    )
                )
                if (
                    prior_review_event is None
                    or later_draft_event is None
                    or produced_event is None
                    or produced_event.sequence <= later_draft_event.sequence
                    or artifact.id == latest.artifact_id
                ):
                    raise PageLineageConflict(
                        "Rejected mask must be revised and regenerated before acceptance",
                        resource=f"image:{image.id}",
                        reason="g7-rejected-artifact-unchanged",
                    )
            artifacts = list(
                session.scalars(
                    select(PageMaskArtifact)
                    .where(PageMaskArtifact.generation_id == generation.id)
                    .order_by(PageMaskArtifact.sequence)
                ).all()
            )
            prior_reviews = list(
                session.scalars(
                    select(PageMaskReview)
                    .where(PageMaskReview.generation_id == generation.id)
                    .order_by(PageMaskReview.sequence)
                ).all()
            )
            if draft is None:
                virtual_recipe = _default_recipe(eligible)
                draft = PageMaskDraft(
                    generation_id=generation.id,
                    image_id=image.id,
                    parent_checksum=g6,
                    quality_checksum=quality["checksum"],
                    recipe=virtual_recipe,
                    state_checksum=_draft_checksum(
                        g6, quality["checksum"], mapping, virtual_recipe
                    ),
                    revision=0,
                )
            before_state = (
                g6
                if state == "not-applicable" and not artifacts and not prior_reviews
                else _state_checksum(
                    g6, quality["checksum"], mapping, draft, artifacts, prior_reviews
                )
            )
            review = PageMaskReview(
                generation_id=generation.id,
                image_id=image.id,
                artifact_id=artifact.id if artifact else None,
                sequence=(latest.sequence + 1 if latest else 1),
                state=state,
                reason=reason,
                mask_checksum=artifact.mask_checksum if artifact else None,
                coverage_checks=coverage_checks,
                collateral_checks=collateral_checks,
                reviewer=actor,
            )
            session.add(review)
            session.flush()
            after_state = _state_checksum(
                g6,
                quality["checksum"],
                mapping,
                draft,
                artifacts,
                [*prior_reviews, review],
            )
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-mask-review",
                entity_id=generation.id,
                operation=state,
                before={},
                after={
                    "state": state,
                    "artifactId": review.artifact_id,
                    "maskChecksum": review.mask_checksum,
                },
            )
            session.flush()
            now = datetime.now(UTC)
            event = _append_event(
                session,
                generation,
                operation="mask-stage-review",
                gate="G7_mask",
                state=state,
                actor=actor,
                input_checksum=before_state,
                output_checksum=after_state,
                parent_checksum=g6,
                stage="mask",
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=draft.state_checksum,
                revision_id=revision.id,
                decision={
                    "accepted": "mask-accepted",
                    "rejected": "mask-rejected",
                    "not-applicable": "mask-not-applicable",
                }[state],
                reason=reason,
                evidence={
                    "eventType": "mask-stage-review",
                    "qualityState": state,
                    "artifactId": review.artifact_id,
                    "maskChecksum": review.mask_checksum,
                    "recipeChecksum": draft.state_checksum,
                    "qualityChecksum": quality["checksum"],
                    "eligibleRegionCount": len(eligible),
                    "rubyRegionCount": sum(map(len, mapping.values())),
                    "rubyRegionIdsByPrimary": mapping,
                    "coverageChecks": coverage_checks,
                    "collateralChecks": collateral_checks,
                    "imageRevision": image.revision,
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def require_current_mask_acceptance(
    store: ProjectStore, session, image: ImageAsset, generation: PageGeneration
) -> tuple[str, PageMaskArtifact | None]:
    _validate_g7_replay(session, generation)
    g6, _ = require_current_ocr_trust(store, session, image, generation)
    quality, _ = require_current_text_present_quality_plate(store, session, image, generation)
    eligible = eligible_mask_regions(session, image.id)
    if not eligible:
        _validate_no_legacy_mask(store, image)
    mapping = ruby_mapping(session, image.id, eligible)
    reviews = list(
        session.scalars(
            select(PageMaskReview)
            .where(PageMaskReview.generation_id == generation.id)
            .order_by(PageMaskReview.sequence)
        ).all()
    )
    review = reviews[-1] if reviews else None
    expected = "accepted" if eligible else "not-applicable"
    if review is None or review.state != expected:
        raise PageLineageConflict(
            "G7 mask is not currently accepted",
            resource=f"image:{image.id}",
            reason="g7-mask-not-currently-accepted",
        )
    if not eligible:
        if review.artifact_id or review.mask_checksum:
            raise PageLineageConflict(
                "G7 N/A has residual evidence",
                resource=f"image:{image.id}",
                reason="g7-mask-na-residual",
            )
        if session.get(PageMaskDraft, generation.id) is not None or session.scalar(
            select(PageMaskArtifact.id)
            .where(PageMaskArtifact.generation_id == generation.id)
            .limit(1)
        ):
            raise PageLineageConflict(
                "G7 N/A has residual persisted mask state",
                resource=f"image:{image.id}",
                reason="g7-mask-na-residual",
            )
        terminal = session.scalar(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.operation == "mask-stage-review",
            )
            .order_by(PageLineageEvent.sequence.desc())
            .limit(1)
        )
        virtual = PageMaskDraft(
            generation_id=generation.id,
            image_id=image.id,
            parent_checksum=g6,
            quality_checksum=quality["checksum"],
            recipe=[],
            state_checksum=_draft_checksum(g6, quality["checksum"], {}, []),
            revision=0,
        )
        state = _state_checksum(g6, quality["checksum"], {}, virtual, [], reviews)
        if terminal is None or terminal.output_checksum != state:
            raise PageLineageConflict(
                "G7 N/A terminal checksum is inconsistent",
                resource=f"image:{image.id}",
                reason="g7-mask-terminal-invalid",
            )
        return state, None
    artifact = session.get(PageMaskArtifact, review.artifact_id)
    if (
        artifact is None
        or artifact.generation_id != generation.id
        or artifact.image_id != image.id
        or artifact.mask_checksum != review.mask_checksum
        or artifact.parent_checksum != g6
        or artifact.quality_checksum != quality["checksum"]
    ):
        raise PageLineageConflict(
            "Accepted G7 artifact is stale",
            resource=f"image:{image.id}",
            reason="g7-mask-artifact-stale",
        )
    draft = _require_current_draft(
        session,
        image=image,
        generation=generation,
        g6_checksum=g6,
        quality_checksum=quality["checksum"],
        eligible=eligible,
        mapping=mapping,
    )
    artifacts = list(
        session.scalars(
            select(PageMaskArtifact)
            .where(PageMaskArtifact.generation_id == generation.id)
            .order_by(PageMaskArtifact.sequence)
        ).all()
    )
    for persisted_artifact in artifacts:
        _validate_artifact_file(store, persisted_artifact)
    if artifact.draft_checksum != draft.state_checksum:
        raise PageLineageConflict(
            "Accepted G7 recipe is stale",
            resource=f"image:{image.id}",
            reason="g7-mask-artifact-stale",
        )
    state = _state_checksum(g6, quality["checksum"], mapping, draft, artifacts, reviews)
    terminal = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.operation == "mask-stage-review",
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if (
        terminal is None
        or terminal.state != "accepted"
        or terminal.decision != "mask-accepted"
        or terminal.output_checksum != state
    ):
        raise PageLineageConflict(
            "Accepted G7 terminal event is inconsistent",
            resource=f"image:{image.id}",
            reason="g7-mask-terminal-invalid",
        )
    return state, artifact
