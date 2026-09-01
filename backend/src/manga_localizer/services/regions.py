from __future__ import annotations

import hashlib
import math
from typing import Any

from sqlalchemy import func, select

from manga_localizer.database import ImageAsset, JobItem, RegionOCRAttempt, TextRegion
from manga_localizer.imaging import DEFAULT_REPAIR_SETTINGS
from manga_localizer.services.images import invalidate_image_pipeline, reset_image_review
from manga_localizer.services.page_lineage import (
    OCR_QC_CHECKS,
    PageLineageConflict,
    background_classification_required,
    derive_ocr_qc_flags,
    g4_region_state_checksum,
    g5_background_state_checksum,
    g6_ocr_state_checksum,
    ocr_source_review_required,
    reconcile_committed_g4_reorder,
    record_background_classification_mutation,
    record_g4_region_mutation,
    record_ocr_source_review_mutation,
    require_image_mutation_lineage,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
    region_payload,
)
from manga_localizer.services.trust import (
    invalidate_trust,
    is_region_trusted,
    manual_recognition,
    recognition_payload,
    with_human_confirmation,
    with_human_ignore,
    with_human_unignore,
)


class RegionNotFound(ProjectError):
    pass


_G4_EDITABLE_REGION_KEYS = {
    "x",
    "y",
    "width",
    "height",
    "rotation",
    "type",
    "direction",
    "order",
    "paragraph_group_id",
    "ruby_parent_id",
    "content_disposition",
}

_BACKGROUND_RATIONALE_ANCHORS = {
    "white-solid": "uniform-near-white",
    "black-solid": "uniform-near-black",
    "other-solid": "uniform-other-color",
    "simple-gradient": "smooth-gradient-continuity",
    "screentone": "periodic-screentone",
    "complex-lineart": "structural-lines-cross-region",
    "illustration/character": "character-or-illustration-detail",
}
_BACKGROUND_RATIONALE_CODES = set(_BACKGROUND_RATIONALE_ANCHORS.values()) | {"mixed-visual-signals"}


def _require_image_revision(
    image: ImageAsset,
    expected_revision: int | None,
    *,
    lineage_bound: bool,
) -> None:
    if lineage_bound and expected_revision is None:
        raise PageLineageConflict(
            "Active-generation region mutations require an image revision",
            resource=f"image:{image.id}",
            reason="image-revision-required",
        )
    if expected_revision is not None and image.revision != expected_revision:
        raise RevisionConflict(
            "Image changed before the region mutation",
            expected_revision=expected_revision,
            actual_revision=image.revision,
            resource=f"image:{image.id}",
        )


def _validate_ruby_relationship(
    session,
    *,
    image_id: str,
    region_id: str | None,
    region_type: str,
    paragraph_group_id: str | None,
    ruby_parent_id: str | None,
    content_disposition: str | None,
) -> None:
    if ruby_parent_id is not None:
        if region_type != "ruby":
            raise ProjectError("Only ruby regions may reference a ruby parent")
        if region_id is not None and ruby_parent_id == region_id:
            raise ProjectError("A ruby region cannot reference itself")
        parent = session.get(TextRegion, ruby_parent_id)
        if parent is None or parent.image_id != image_id:
            raise ProjectError("Ruby parent must be a region on the same image")
        if parent.region_type == "ruby":
            raise ProjectError("A ruby region cannot use another ruby region as its parent")
        if parent.content_disposition == "false-positive":
            raise ProjectError("A ruby region cannot use a false-positive region as its parent")
        if (
            paragraph_group_id is not None
            and parent.paragraph_group_id is not None
            and paragraph_group_id != parent.paragraph_group_id
        ):
            raise ProjectError("Ruby and parent regions must share a paragraph group")
    if region_id is not None:
        children = list(
            session.scalars(select(TextRegion).where(TextRegion.ruby_parent_id == region_id)).all()
        )
        if children and region_type == "ruby":
            raise ProjectError("A ruby parent cannot itself become a ruby region")
        if children and content_disposition == "false-positive":
            raise ProjectError("A ruby parent cannot become a false-positive region")
        if any(
            paragraph_group_id is not None
            and child.paragraph_group_id is not None
            and paragraph_group_id != child.paragraph_group_id
            for child in children
        ):
            raise ProjectError("Ruby children must share their parent paragraph group")


def _canonical_repair(repair: dict[str, Any] | None) -> dict[str, Any]:
    canonical = {**DEFAULT_REPAIR_SETTINGS, **(repair or {})}
    if "maskPadding" not in (repair or {}) and "padding" in (repair or {}):
        canonical["maskPadding"] = (repair or {})["padding"]
    canonical.pop("padding", None)
    method = canonical.get("method")
    if method in {"ns", "navier_stokes"}:
        canonical["method"] = "navier-stokes"
    return canonical


def _apply_nested_patch(current: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current or {})
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _validate_repair_bounds(image: ImageAsset, repair: dict[str, Any]) -> None:
    polygon = repair.get("maskPolygon")
    if isinstance(polygon, list):
        for point in polygon:
            if float(point[0]) > image.width or float(point[1]) > image.height:
                raise ProjectError("Mask polygon points must remain within image bounds")
    edits = repair.get("maskEdits")
    if not isinstance(edits, dict):
        return
    for stroke in edits.get("strokes", []):
        for point in stroke.get("points", []):
            if float(point[0]) > image.width or float(point[1]) > image.height:
                raise ProjectError("Mask edit points must remain within image bounds")


def _changed_region_stages(values: dict[str, Any], region: TextRegion) -> set[str]:
    keys = set(values)
    stages = {"export"}
    if "recognition" in keys:
        # Any trust promotion, withdrawal, ignore, or evidence invalidation changes
        # which regions may enter translation and default safe image processing.
        stages.update(("translation", "inpaint", "typeset"))
    if keys & {"source_text"}:
        stages.update(("translation", "inpaint", "typeset"))
    if "confidence" in keys:
        stages.update(("inpaint", "typeset"))
    # Translation confirmation is an explicit typesetting prerequisite. A
    # withdrawal must invalidate any previously accepted typeset artifact, and
    # a new confirmation must not revive a plate produced under older evidence.
    if "confirmed" in keys:
        stages.add("typeset")
    if keys & {
        "translation_text",
        "style",
        "type",
        "direction",
    }:
        stages.add("typeset")
    if "order" in keys:
        # Per-region inpainting is intentionally sequential in reading order,
        # so a reorder changes both translation context and rendered pixels.
        stages.update(("translation", "inpaint", "typeset"))
    if keys & {"paragraph_group_id", "ruby_parent_id", "content_disposition"}:
        stages.update(("ocr", "translation", "inpaint", "typeset"))
    if keys & {"x", "y", "width", "height", "rotation", "repair", "ignored"}:
        stages.update(("inpaint", "typeset"))
    if "ignored" in keys:
        stages.add("translation")
        if not region.ignored and not region.source_text:
            stages.add("ocr")
    if "source_text" in keys and not region.source_text:
        stages.add("ocr")
    if "translation_text" in keys and not region.translation_text:
        stages.add("translation")
    return stages


def _validate_bounds(image: ImageAsset, values: dict[str, Any]) -> None:
    x = float(values.get("x", 0))
    y = float(values.get("y", 0))
    width = float(values.get("width", 0))
    height = float(values.get("height", 0))
    rotation = float(values.get("rotation", 0))
    if not all(math.isfinite(value) for value in (x, y, width, height, rotation)):
        raise ProjectError("Region geometry must be finite")
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        raise ProjectError("Region geometry must be positive")
    if x + width > image.width + 0.001 or y + height > image.height + 0.001:
        raise ProjectError("Region must remain within image bounds")


def list_regions(store: ProjectStore, image_id: str) -> list[TextRegion]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise RegionNotFound("Image was not found in this project")
        return list(
            session.scalars(
                select(TextRegion)
                .where(TextRegion.image_id == image_id)
                .order_by(TextRegion.reading_order, TextRegion.created_at)
            ).all()
        )


def create_region(store: ProjectStore, image_id: str, values: dict[str, Any]) -> TextRegion:
    lineage = values.pop("lineage", None)
    expected_image_revision = values.pop("expected_image_revision", None)
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise RegionNotFound("Image was not found in this project")
        binding = require_image_mutation_lineage(store, session, image, lineage)
        _require_image_revision(
            image,
            expected_image_revision,
            lineage_bound=binding is not None,
        )
        before_checksum = g4_region_state_checksum(session, image_id) if binding else None
        if binding is not None and (
            values.get("source_text")
            or values.get("translation_text")
            or values.get("ignored")
            or values.get("confirmed")
            or values.get("confidence") is not None
            or bool(values.get("style"))
            or bool(values.get("repair"))
        ):
            raise PageLineageConflict(
                "G4 manual region creation cannot write later-stage fields",
                resource=f"image:{image.id}",
                reason="g4-field-not-supported",
            )
        _validate_bounds(image, values)
        project = store.project(session)
        requested_order = values.pop("order", None)
        if requested_order is None:
            requested_order = session.scalar(
                select(func.count()).select_from(TextRegion).where(TextRegion.image_id == image_id)
            )
        style = _apply_nested_patch({}, values.get("style", {}))
        repair = _apply_nested_patch({}, values.get("repair", {}))
        if values.get("ignored", False) and values.get("confirmed", False):
            raise ProjectError("A region cannot be both ignored and confirmed")
        if "maskPadding" not in repair and "padding" in repair:
            repair["maskPadding"] = repair["padding"]
        _validate_repair_bounds(image, repair)
        _validate_ruby_relationship(
            session,
            image_id=image_id,
            region_id=None,
            region_type=values.get("type", "dialogue"),
            paragraph_group_id=values.get("paragraph_group_id"),
            ruby_parent_id=values.get("ruby_parent_id"),
            content_disposition=values.get("content_disposition"),
        )
        region = TextRegion(
            image_id=image_id,
            x=values["x"],
            y=values["y"],
            width=values["width"],
            height=values["height"],
            rotation=values.get("rotation", 0),
            source_text=values.get("source_text", ""),
            translation_text=values.get("translation_text", ""),
            region_type=values.get("type", "dialogue"),
            direction=values.get("direction", "vertical"),
            reading_order=requested_order,
            paragraph_group_id=values.get("paragraph_group_id"),
            ruby_parent_id=values.get("ruby_parent_id"),
            content_disposition=values.get("content_disposition"),
            confidence=values.get("confidence"),
            ignored=values.get("ignored", False),
            confirmed=values.get("confirmed", False),
            style=style,
            repair={**DEFAULT_REPAIR_SETTINGS, **repair},
            recognition=manual_recognition(
                confirmed=values.get("confirmed", False),
                ignored=values.get("ignored", False),
            ),
            ocr_provider="manual" if values.get("source_text") else None,
            translation_provider="manual" if values.get("translation_text") else None,
            revision=1,
        )
        session.add(region)
        stages = {"inpaint", "typeset", "export"}
        if not region.ignored and not region.source_text:
            stages.add("ocr")
        if not region.ignored:
            stages.add("translation")
        invalidate_image_pipeline(store, image, stages)
        reset_image_review(image)
        image.revision += 1
        session.flush()
        revision = add_revision(
            session,
            project,
            entity_type="region",
            entity_id=region.id,
            operation="create",
            before=None,
            after=region_payload(region),
        )
        session.flush()
        if binding is not None and before_checksum is not None:
            after_checksum = g4_region_state_checksum(session, image_id)
            region_count = session.scalar(
                select(func.count()).select_from(TextRegion).where(TextRegion.image_id == image_id)
            )
            record_g4_region_mutation(
                store,
                session,
                image=image,
                binding=binding,
                operation="regions-created",
                before_checksum=before_checksum,
                after_checksum=after_checksum,
                revision_id=revision.id,
                region_count=int(region_count or 0),
            )
    store.write_snapshot()
    return region


def update_region(store: ProjectStore, region_id: str, values: dict[str, Any]) -> TextRegion:
    expected_revision = values.pop("expected_revision", None)
    expected_image_revision = values.pop("expected_image_revision", None)
    lineage = values.pop("lineage", None)
    requested_keys = set(values)
    with store.session() as session:
        region = session.get(TextRegion, region_id)
        if region is None:
            raise RegionNotFound("Region was not found")
        if expected_revision is not None and region.revision != expected_revision:
            raise RevisionConflict(
                f"Region revision is {region.revision}, expected {expected_revision}",
                expected_revision=expected_revision,
                actual_revision=region.revision,
                resource=f"region:{region.id}",
            )
        image = session.get(ImageAsset, region.image_id)
        assert image is not None
        binding = require_image_mutation_lineage(store, session, image, lineage)
        _require_image_revision(
            image,
            expected_image_revision,
            lineage_bound=binding is not None,
        )
        if binding is not None and requested_keys - _G4_EDITABLE_REGION_KEYS:
            raise PageLineageConflict(
                "This region field is blocked until its lineage gate is implemented",
                resource=f"region:{region.id}",
                reason="g4-field-not-supported",
            )
        before_checksum = g4_region_state_checksum(session, image.id) if binding else None
        proposed = {
            "x": values.get("x", region.x),
            "y": values.get("y", region.y),
            "width": values.get("width", region.width),
            "height": values.get("height", region.height),
            "rotation": values.get("rotation", region.rotation),
        }
        _validate_bounds(image, proposed)
        project = store.project(session)
        before = region_payload(region)
        previous_recognition = recognition_payload(region)
        explicit_trust_confirmation = values.get("confirmed") is True and not is_region_trusted(
            region
        )
        if values.get("ignored") is True:
            if values.get("confirmed") is True:
                raise ProjectError("A region cannot be both ignored and confirmed")
            values["confirmed"] = False
        elif values.get("confirmed") is True and values.get("ignored", region.ignored):
            raise ProjectError("An ignored region cannot be confirmed")
        mapping = {
            "type": "region_type",
            "order": "reading_order",
        }
        storage_normalized = False
        if values.get("style") is None:
            values.pop("style", None)
        else:
            style_patch = values["style"]
            proposed_style = _apply_nested_patch(region.style, style_patch)
            if proposed_style == (region.style or {}):
                values.pop("style")
            else:
                values["style"] = proposed_style
        if values.get("repair") is None:
            values.pop("repair", None)
        else:
            repair_patch = values["repair"]
            proposed_repair = _apply_nested_patch(region.repair, repair_patch)
            if _canonical_repair(region.repair) == _canonical_repair(proposed_repair):
                if any(value is None for value in repair_patch.values()) and proposed_repair != (
                    region.repair or {}
                ):
                    region.repair = proposed_repair
                    storage_normalized = True
                values.pop("repair")
            else:
                values["repair"] = proposed_repair
        nullable_g4_keys = {
            "paragraph_group_id",
            "ruby_parent_id",
            "content_disposition",
        }
        changed_values = {
            key: value
            for key, value in values.items()
            if (
                (key in nullable_g4_keys or value is not None)
                and getattr(region, mapping.get(key, key)) != value
            )
        }
        confirmation_stale_keys = {
            "x",
            "y",
            "width",
            "height",
            "rotation",
            "source_text",
            "translation_text",
            "type",
            "direction",
            "order",
            "paragraph_group_id",
            "ruby_parent_id",
            "content_disposition",
            "confidence",
            "style",
            "repair",
            "ignored",
        }
        if (
            changed_values.keys() & confirmation_stale_keys
            and region.confirmed
            and not explicit_trust_confirmation
        ):
            changed_values["confirmed"] = False
        trust_input_keys = {
            "x",
            "y",
            "width",
            "height",
            "rotation",
            "source_text",
            "direction",
            "confidence",
        }
        prior_repair = region.repair or {}
        next_repair = changed_values.get("repair", prior_repair)
        recognition_repair_keys = {
            "detectedTextCandidate",
            "detectorGenerated",
            "ocrAttemptCount",
            "ocrInputVariant",
        }
        repair_changes_recognition = "repair" in changed_values and any(
            prior_repair.get(key) != next_repair.get(key) for key in recognition_repair_keys
        )
        if changed_values.get("ignored") is True:
            changed_values["recognition"] = with_human_ignore(previous_recognition)
        elif explicit_trust_confirmation:
            changed_values["recognition"] = with_human_confirmation(previous_recognition)
        elif "ignored" in changed_values:
            changed_values["recognition"] = with_human_unignore(previous_recognition)
        elif (changed_values.keys() & trust_input_keys) or repair_changes_recognition:
            changed_values["recognition"] = invalidate_trust(previous_recognition)
        geometry_keys = {"x", "y", "width", "height", "rotation"}
        if geometry_keys & changed_values.keys():
            repair = dict(changed_values.get("repair", region.repair or {}))
            # Detector polygons use the previous canonical coordinates. Once the
            # user edits the region geometry, the visible box becomes the manual
            # mask boundary instead of a stale hidden polygon. Preserve only a
            # genuinely new polygon supplied alongside the geometry update.
            previous_polygon = (region.repair or {}).get("maskPolygon")
            if (
                repair.get("maskPolygon") == previous_polygon
                and repair.pop("maskPolygon", None) is not None
            ):
                changed_values["repair"] = repair
        _validate_ruby_relationship(
            session,
            image_id=image.id,
            region_id=region.id,
            region_type=str(changed_values.get("type", region.region_type)),
            paragraph_group_id=changed_values.get("paragraph_group_id", region.paragraph_group_id),
            ruby_parent_id=changed_values.get("ruby_parent_id", region.ruby_parent_id),
            content_disposition=changed_values.get(
                "content_disposition", region.content_disposition
            ),
        )
        if not changed_values and not storage_normalized:
            return region
        if changed_values:
            if "repair" in changed_values:
                _validate_repair_bounds(image, changed_values["repair"])
            for key, value in changed_values.items():
                setattr(region, mapping.get(key, key), value)
            if "source_text" in changed_values:
                region.ocr_provider = "manual"
            if "translation_text" in changed_values:
                region.translation_provider = "manual"
            invalidate_image_pipeline(store, image, _changed_region_stages(changed_values, region))
            reset_image_review(image)
            region.revision += 1
            image.revision += 1
            session.flush()
            after = region_payload(region)
            revision = add_revision(
                session,
                project,
                entity_type="region",
                entity_id=region.id,
                operation="update",
                before=before,
                after=after,
            )
            session.flush()
            if binding is not None and before_checksum is not None:
                after_checksum = g4_region_state_checksum(session, image.id)
                region_count = session.scalar(
                    select(func.count())
                    .select_from(TextRegion)
                    .where(TextRegion.image_id == image.id)
                )
                record_g4_region_mutation(
                    store,
                    session,
                    image=image,
                    binding=binding,
                    operation="regions-updated",
                    before_checksum=before_checksum,
                    after_checksum=after_checksum,
                    revision_id=revision.id,
                    region_count=int(region_count or 0),
                )
        else:
            session.flush()
    store.write_snapshot()
    return region


def set_background_classification(
    store: ProjectStore,
    region_id: str,
    values: dict[str, Any],
) -> TextRegion:
    expected_revision = values["expected_revision"]
    expected_image_revision = values["expected_image_revision"]
    lineage = values["lineage"]
    category = values["category"]
    confidence = values["confidence"]
    rationale_codes = values["rationale_codes"]
    anchor = _BACKGROUND_RATIONALE_ANCHORS.get(category)
    if (
        anchor is None
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or float(confidence) < 0
        or float(confidence) > 1
        or not isinstance(rationale_codes, list)
        or not rationale_codes
        or any(not isinstance(code, str) for code in rationale_codes)
        or len(set(rationale_codes)) != len(rationale_codes)
        or not set(rationale_codes).issubset(_BACKGROUND_RATIONALE_CODES)
        or anchor not in rationale_codes
    ):
        raise ProjectError("Background classification evidence is invalid")

    with store.session() as session:
        region = session.get(TextRegion, region_id)
        if region is None:
            raise RegionNotFound("Region was not found")
        if region.revision != expected_revision:
            raise RevisionConflict(
                f"Region revision is {region.revision}, expected {expected_revision}",
                expected_revision=expected_revision,
                actual_revision=region.revision,
                resource=f"region:{region.id}",
            )
        image = session.get(ImageAsset, region.image_id)
        assert image is not None
        binding = require_image_mutation_lineage(store, session, image, lineage)
        if binding is None:
            raise PageLineageConflict(
                "G5 classification requires an active page generation",
                resource=f"region:{region.id}",
                reason="active-generation-missing",
            )
        _require_image_revision(
            image,
            expected_image_revision,
            lineage_bound=True,
        )
        if not background_classification_required(region):
            raise PageLineageConflict(
                "Only translate or redraw-art regions require G5 classification",
                resource=f"region:{region.id}",
                reason="g5-region-not-eligible",
            )

        before_checksum = g5_background_state_checksum(session, image.id)
        project = store.project(session)
        before = region_payload(region)
        generation, actor, _expected_sequence = binding
        region.background_category = category
        region.background_confidence = float(confidence)
        region.background_rationale_codes = sorted(rationale_codes)
        region.background_reviewer = dict(actor)
        region.background_generation_id = generation.id
        invalidate_image_pipeline(
            store,
            image,
            {"ocr", "translation", "inpaint", "typeset", "export"},
        )
        reset_image_review(image)
        region.revision += 1
        image.revision += 1
        session.flush()
        revision = add_revision(
            session,
            project,
            entity_type="region-background",
            entity_id=region.id,
            operation="review",
            before=before,
            after=region_payload(region),
        )
        session.flush()
        after_checksum = g5_background_state_checksum(session, image.id)
        record_background_classification_mutation(
            store,
            session,
            image=image,
            region=region,
            binding=binding,
            before_checksum=before_checksum,
            after_checksum=after_checksum,
            revision_id=revision.id,
        )
    store.write_snapshot()
    return region


def set_ocr_source_review(
    store: ProjectStore,
    region_id: str,
    values: dict[str, Any],
) -> TextRegion:
    expected_revision = values["expected_revision"]
    expected_image_revision = values["expected_image_revision"]
    lineage = values["lineage"]
    source_text = str(values["source_text"]).strip()
    source_mode = values["source_mode"]
    selected_attempt_id = values["selected_attempt_id"]
    qc_checks = values["qc_checks"]
    if (
        not source_text
        or source_mode
        not in {
            "original-attempt",
            "quality-attempt",
            "manual-correction",
        }
        or not isinstance(selected_attempt_id, str)
        or not isinstance(qc_checks, list)
        or len(qc_checks) != len(set(qc_checks))
        or set(qc_checks) != OCR_QC_CHECKS
    ):
        raise ProjectError("OCR source review evidence is invalid")

    with store.session() as session:
        region = session.get(TextRegion, region_id)
        if region is None:
            raise RegionNotFound("Region was not found")
        if region.revision != expected_revision:
            raise RevisionConflict(
                f"Region revision is {region.revision}, expected {expected_revision}",
                expected_revision=expected_revision,
                actual_revision=region.revision,
                resource=f"region:{region.id}",
            )
        image = session.get(ImageAsset, region.image_id)
        assert image is not None
        binding = require_image_mutation_lineage(store, session, image, lineage)
        if binding is None:
            raise PageLineageConflict(
                "G6 source review requires an active page generation",
                resource=f"region:{region.id}",
                reason="active-generation-missing",
            )
        _require_image_revision(image, expected_image_revision, lineage_bound=True)
        if not ocr_source_review_required(region):
            raise PageLineageConflict(
                "Only non-ruby translate or redraw-art regions require G6 source review",
                resource=f"region:{region.id}",
                reason="g6-region-not-eligible",
            )
        generation, actor, _expected_sequence = binding
        selected_attempt = session.get(RegionOCRAttempt, selected_attempt_id)
        if (
            selected_attempt is None
            or selected_attempt.region_id != region.id
            or selected_attempt.image_id != image.id
            or selected_attempt.generation_id != generation.id
        ):
            raise PageLineageConflict(
                "Selected OCR attempt is not current for this region",
                resource=f"region:{region.id}",
                reason="g6-selected-attempt-stale",
            )
        item = session.get(JobItem, selected_attempt.job_item_id)
        if item is None or item.status != "completed":
            raise PageLineageConflict(
                "Selected OCR attempt has no completed job evidence",
                resource=f"job-item:{selected_attempt.job_item_id}",
                reason="g6-producer-not-completed",
            )
        pair = list(
            session.scalars(
                select(RegionOCRAttempt).where(
                    RegionOCRAttempt.region_id == region.id,
                    RegionOCRAttempt.generation_id == generation.id,
                    RegionOCRAttempt.job_item_id == selected_attempt.job_item_id,
                )
            ).all()
        )
        if {attempt.input_variant for attempt in pair} != {"original", "quality"}:
            raise PageLineageConflict(
                "Source review requires original and quality OCR attempts from one job",
                resource=f"region:{region.id}",
                reason="g6-dual-attempts-incomplete",
            )
        expected_variant = {
            "original-attempt": "original",
            "quality-attempt": "quality",
        }.get(source_mode)
        if expected_variant is not None and selected_attempt.input_variant != expected_variant:
            raise ProjectError("OCR source mode does not match the selected attempt")
        if source_mode != "manual-correction" and source_text != selected_attempt.text.strip():
            raise ProjectError("Trusted source text must match the selected OCR attempt")

        before_checksum = g6_ocr_state_checksum(session, image.id, generation.id)
        project = store.project(session)
        before = region_payload(region)
        qc_flags = derive_ocr_qc_flags(
            source_text,
            source_mode=source_mode,
            selected_attempt=selected_attempt,
            attempts=pair,
        )
        region.source_text = source_text
        region.ocr_review = {
            "sourceMode": source_mode,
            "selectedAttemptId": selected_attempt.id,
            "sourceTextChecksum": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "qcChecks": sorted(qc_checks),
            "qcFlags": qc_flags,
        }
        region.ocr_reviewer = dict(actor)
        region.ocr_generation_id = generation.id
        region.ocr_provider = selected_attempt.provider
        invalidate_image_pipeline(
            store,
            image,
            {"translation", "inpaint", "typeset", "export"},
        )
        reset_image_review(image)
        region.revision += 1
        image.revision += 1
        session.flush()
        revision = add_revision(
            session,
            project,
            entity_type="region-ocr-review",
            entity_id=region.id,
            operation="review",
            before=before,
            after=region_payload(region),
        )
        session.flush()
        after_checksum = g6_ocr_state_checksum(session, image.id, generation.id)
        record_ocr_source_review_mutation(
            store,
            session,
            image=image,
            region=region,
            selected_attempt_id=selected_attempt.id,
            binding=binding,
            before_checksum=before_checksum,
            after_checksum=after_checksum,
            revision_id=revision.id,
        )
    store.write_snapshot()
    return region


def delete_region(
    store: ProjectStore,
    region_id: str,
    expected_revision: int | None = None,
    *,
    expected_image_revision: int | None = None,
    lineage: dict[str, Any] | None = None,
) -> None:
    with store.session() as session:
        region = session.get(TextRegion, region_id)
        if region is None:
            raise RegionNotFound("Region was not found")
        if expected_revision is not None and region.revision != expected_revision:
            raise RevisionConflict(
                f"Region revision is {region.revision}, expected {expected_revision}",
                expected_revision=expected_revision,
                actual_revision=region.revision,
                resource=f"region:{region.id}",
            )
        project = store.project(session)
        image = session.get(ImageAsset, region.image_id)
        assert image is not None
        binding = require_image_mutation_lineage(store, session, image, lineage)
        _require_image_revision(
            image,
            expected_image_revision,
            lineage_bound=binding is not None,
        )
        if binding is not None and (
            region.detector_job_item_id is not None or region.detector_candidate_index is not None
        ):
            raise PageLineageConflict(
                "Detector candidates require an explicit G4 disposition and cannot be deleted",
                resource=f"region:{region.id}",
                reason="detector-candidate-disposition-required",
            )
        child = session.scalar(
            select(TextRegion.id).where(TextRegion.ruby_parent_id == region.id).limit(1)
        )
        if child is not None:
            raise ProjectError("A region with ruby children cannot be deleted")
        before_checksum = g4_region_state_checksum(session, image.id) if binding else None
        invalidate_image_pipeline(
            store,
            image,
            {"ocr", "translation", "inpaint", "typeset", "export"},
        )
        reset_image_review(image)
        image.revision += 1
        before = region_payload(region)
        session.delete(region)
        revision = add_revision(
            session,
            project,
            entity_type="region",
            entity_id=region.id,
            operation="delete",
            before=before,
            after=None,
        )
        session.flush()
        if binding is not None and before_checksum is not None:
            after_checksum = g4_region_state_checksum(session, image.id)
            region_count = session.scalar(
                select(func.count()).select_from(TextRegion).where(TextRegion.image_id == image.id)
            )
            record_g4_region_mutation(
                store,
                session,
                image=image,
                binding=binding,
                operation="regions-deleted",
                before_checksum=before_checksum,
                after_checksum=after_checksum,
                revision_id=revision.id,
                region_count=int(region_count or 0),
            )
    store.write_snapshot()


def apply_reading_order(
    store: ProjectStore,
    image_id: str,
    *,
    region_ids: list[str] | None = None,
    mode: str = "manga-vertical",
    expected_image_revision: int | None = None,
    lineage: dict[str, Any] | None = None,
) -> list[TextRegion]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise RegionNotFound("Image was not found")
        binding = require_image_mutation_lineage(store, session, image, lineage)
        _require_image_revision(
            image,
            expected_image_revision,
            lineage_bound=binding is not None,
        )
        before_checksum = g4_region_state_checksum(session, image.id) if binding else None
        regions = list(
            session.scalars(select(TextRegion).where(TextRegion.image_id == image_id)).all()
        )
        by_id = {region.id: region for region in regions}
        if region_ids is not None:
            if len(set(region_ids)) != len(region_ids) or set(region_ids) != set(by_id):
                raise ProjectError("Explicit reading order must contain every region exactly once")
            ordered = [by_id[region_id] for region_id in region_ids]
        elif mode == "horizontal-ltr":
            ordered = sorted(regions, key=lambda region: (region.y + region.height / 2, region.x))
        else:
            # Manga default: rightmost column top-to-bottom, then columns to the left.
            ordered = sorted(
                regions,
                key=lambda region: (-(region.x + region.width / 2), region.y + region.height / 2),
            )
        project = store.project(session)
        changed = False
        last_revision = None
        for index, region in enumerate(ordered):
            if region.reading_order == index:
                continue
            before = region_payload(region)
            changed = True
            region.reading_order = index
            region.revision += 1
            session.flush()
            revision = add_revision(
                session,
                project,
                entity_type="region",
                entity_id=region.id,
                operation="reorder",
                before=before,
                after=region_payload(region),
            )
            last_revision = revision
        if changed:
            invalidate_image_pipeline(
                store,
                image,
                {"ocr", "translation", "inpaint", "typeset", "export"},
            )
            reset_image_review(image)
            image.revision += 1
            session.flush()
            if binding is not None and before_checksum is not None and last_revision is not None:
                if not last_revision.id:
                    raise ProjectError("Region reorder revision was not persisted")
                after_checksum = g4_region_state_checksum(session, image.id)
                record_g4_region_mutation(
                    store,
                    session,
                    image=image,
                    binding=binding,
                    operation="regions-reordered",
                    before_checksum=before_checksum,
                    after_checksum=after_checksum,
                    revision_id=last_revision.id,
                    region_count=len(ordered),
                )
        elif binding is not None and region_ids is not None:
            reconcile_committed_g4_reorder(
                store,
                session,
                image=image,
                binding=binding,
                requested_region_ids=region_ids,
            )
    store.write_snapshot()
    return ordered
