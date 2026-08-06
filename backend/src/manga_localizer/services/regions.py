from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from manga_localizer.database import ImageAsset, TextRegion
from manga_localizer.services.images import invalidate_image_pipeline
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
    region_payload,
)


class RegionNotFound(ProjectError):
    pass


def _changed_region_stages(values: dict[str, Any], region: TextRegion) -> set[str]:
    keys = set(values)
    stages = {"export"}
    if keys & {"source_text"}:
        stages.update(("translation", "typeset"))
    if keys & {
        "translation_text",
        "style",
        "direction",
        "order",
    }:
        stages.add("typeset")
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
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise RegionNotFound("Image was not found in this project")
        _validate_bounds(image, values)
        project = store.project(session)
        requested_order = values.pop("order", None)
        if requested_order is None:
            requested_order = session.scalar(
                select(func.count()).select_from(TextRegion).where(TextRegion.image_id == image_id)
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
            confidence=values.get("confidence"),
            ignored=values.get("ignored", False),
            confirmed=values.get("confirmed", False),
            style=values.get("style", {}),
            repair=values.get("repair", {}),
            ocr_provider="manual" if values.get("source_text") else None,
            translation_provider="manual" if values.get("translation_text") else None,
            revision=1,
        )
        session.add(region)
        stages = {"inpaint", "typeset", "export"}
        if not region.ignored and not region.source_text:
            stages.add("ocr")
        if not region.ignored and not region.translation_text:
            stages.add("translation")
        invalidate_image_pipeline(store, image, stages)
        image.revision += 1
        session.flush()
        add_revision(
            session,
            project,
            entity_type="region",
            entity_id=region.id,
            operation="create",
            before=None,
            after=region_payload(region),
        )
    store.write_snapshot()
    return region


def update_region(store: ProjectStore, region_id: str, values: dict[str, Any]) -> TextRegion:
    expected_revision = values.pop("expected_revision", None)
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
        proposed = {
            "x": values.get("x", region.x),
            "y": values.get("y", region.y),
            "width": values.get("width", region.width),
            "height": values.get("height", region.height),
        }
        _validate_bounds(image, proposed)
        project = store.project(session)
        before = region_payload(region)
        mapping = {
            "type": "region_type",
            "order": "reading_order",
        }
        changed_values = {
            key: value
            for key, value in values.items()
            if value is not None and getattr(region, mapping.get(key, key)) != value
        }
        if not changed_values:
            return region
        for key, value in changed_values.items():
            setattr(region, mapping.get(key, key), value)
        if "source_text" in changed_values:
            region.ocr_provider = "manual"
        if "translation_text" in changed_values:
            region.translation_provider = "manual"
        invalidate_image_pipeline(store, image, _changed_region_stages(changed_values, region))
        region.revision += 1
        image.revision += 1
        session.flush()
        after = region_payload(region)
        add_revision(
            session,
            project,
            entity_type="region",
            entity_id=region.id,
            operation="update",
            before=before,
            after=after,
        )
    store.write_snapshot()
    return region


def delete_region(
    store: ProjectStore, region_id: str, expected_revision: int | None = None
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
        invalidate_image_pipeline(store, image, {"inpaint", "typeset", "export"})
        image.revision += 1
        before = region_payload(region)
        session.delete(region)
        add_revision(
            session,
            project,
            entity_type="region",
            entity_id=region.id,
            operation="delete",
            before=before,
            after=None,
        )
    store.write_snapshot()


def apply_reading_order(
    store: ProjectStore,
    image_id: str,
    *,
    region_ids: list[str] | None = None,
    mode: str = "manga-vertical",
) -> list[TextRegion]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise RegionNotFound("Image was not found")
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
        for index, region in enumerate(ordered):
            if region.reading_order == index:
                continue
            before = region_payload(region)
            changed = True
            region.reading_order = index
            region.revision += 1
            session.flush()
            add_revision(
                session,
                project,
                entity_type="region",
                entity_id=region.id,
                operation="reorder",
                before=before,
                after=region_payload(region),
            )
        if changed:
            invalidate_image_pipeline(
                store,
                image,
                {"translation", "typeset", "export"},
            )
            image.revision += 1
    store.write_snapshot()
    return ordered
