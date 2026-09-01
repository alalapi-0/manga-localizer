from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np


class LayeredStructureError(ValueError):
    pass


def _bounded_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LayeredStructureError(f"{label} is outside the supported range")
    return value


def _point(value: object, *, width: int, height: int, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
        or any(not math.isfinite(float(item)) for item in value)
        or any(int(item) != item for item in value)
    ):
        raise LayeredStructureError(f"{label} must contain finite integer source coordinates")
    x, y = int(value[0]), int(value[1])
    if not 0 <= x < width or not 0 <= y < height:
        raise LayeredStructureError(f"{label} is outside the source grid")
    return [x, y]


def _layer(
    value: object,
    *,
    source_size: tuple[int, int],
    stroke: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LayeredStructureError("Layer definitions must be objects")
    required = {"id", "mode", "points" if stroke else "polygon"}
    mode = value.get("mode")
    mode_key = "referenceId" if mode == "reference" else "rgb" if mode == "solid" else None
    expected = required | ({mode_key} if mode_key else set()) | ({"width"} if stroke else set())
    if mode_key is None or set(value) != expected:
        raise LayeredStructureError("Layer definitions contain unsupported keys or modes")
    layer_id = value.get("id")
    if (
        not isinstance(layer_id, str)
        or not 1 <= len(layer_id) <= 64
        or not all(character.isalnum() or character in "._-" for character in layer_id)
    ):
        raise LayeredStructureError("Layer id is invalid")
    raw_points = value["points" if stroke else "polygon"]
    minimum, maximum = (2, 128) if stroke else (3, 128)
    if not isinstance(raw_points, list) or not minimum <= len(raw_points) <= maximum:
        raise LayeredStructureError("Layer geometry has an unsupported point count")
    width, height = source_size
    points = [
        _point(point, width=width, height=height, label=f"layer {layer_id}") for point in raw_points
    ]
    normalized: dict[str, Any] = {
        "id": layer_id,
        "mode": mode,
        "points" if stroke else "polygon": points,
    }
    if stroke:
        normalized["width"] = _bounded_int(
            value["width"], minimum=1, maximum=128, label="stroke width"
        )
    if mode == "reference":
        reference_id = value["referenceId"]
        if (
            not isinstance(reference_id, str)
            or not 1 <= len(reference_id) <= 64
            or not all(character.isalnum() or character in "._-" for character in reference_id)
        ):
            raise LayeredStructureError("Reference id is invalid")
        normalized["referenceId"] = reference_id
    else:
        rgb = value["rgb"]
        if (
            not isinstance(rgb, list)
            or len(rgb) != 3
            or any(
                isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255
                for channel in rgb
            )
        ):
            raise LayeredStructureError("Solid layer rgb is invalid")
        normalized["rgb"] = list(rgb)
    return normalized


def canonicalize_layered_structure_guide(
    value: object,
    *,
    source_size: tuple[int, int],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "domains",
        "strokes",
        "featherRadius",
    }:
        raise LayeredStructureError("Layered structure guide has unsupported keys")
    if value.get("version") != 1:
        raise LayeredStructureError("Layered structure guide version is unsupported")
    domains = value.get("domains")
    strokes = value.get("strokes")
    if not isinstance(domains, list) or not 1 <= len(domains) <= 64:
        raise LayeredStructureError("Layered structure guide domains are invalid")
    if not isinstance(strokes, list) or len(strokes) > 128:
        raise LayeredStructureError("Layered structure guide strokes are invalid")
    normalized_domains = [_layer(item, source_size=source_size, stroke=False) for item in domains]
    normalized_strokes = [_layer(item, source_size=source_size, stroke=True) for item in strokes]
    all_ids = [item["id"] for item in [*normalized_domains, *normalized_strokes]]
    if len(all_ids) != len(set(all_ids)):
        raise LayeredStructureError("Layer ids must be unique")
    return {
        "version": 1,
        "domains": sorted(normalized_domains, key=lambda item: item["id"]),
        "strokes": sorted(normalized_strokes, key=lambda item: item["id"]),
        "featherRadius": _bounded_int(
            value["featherRadius"], minimum=0, maximum=64, label="featherRadius"
        ),
    }


def _raster_points(points: list[list[int]]) -> np.ndarray:
    return np.asarray(points, dtype=np.int32)


def _expand_source_mask(
    mask: np.ndarray, *, scale: int, target_shape: tuple[int, int]
) -> np.ndarray:
    expanded = np.repeat(np.repeat(mask, scale, axis=0), scale, axis=1)
    if expanded.shape != target_shape:
        raise LayeredStructureError("Layered structure scale does not match the quality grid")
    return expanded


def render_layered_structure(
    source: np.ndarray,
    support: np.ndarray,
    guide: dict[str, Any],
    references: dict[str, np.ndarray],
    *,
    scale: int,
) -> np.ndarray:
    if source.ndim != 3 or source.shape[2] != 3 or support.shape != source.shape[:2]:
        raise LayeredStructureError("Layered structure input grids are incompatible")
    if scale not in {1, 2, 3, 4} or source.shape[0] % scale or source.shape[1] % scale:
        raise LayeredStructureError("Layered structure scale does not match the quality grid")
    source_shape = (source.shape[0] // scale, source.shape[1] // scale)
    support_binary = support > 0
    coverage = np.zeros(support.shape, dtype=np.uint16)
    domain_masks: list[tuple[dict[str, Any], np.ndarray]] = []
    for domain in guide["domains"]:
        source_mask = np.zeros(source_shape, dtype=np.uint8)
        cv2.fillPoly(
            source_mask,
            [_raster_points(domain["polygon"])],
            255,
            lineType=cv2.LINE_8,
        )
        selector = _expand_source_mask(
            source_mask > 0,
            scale=scale,
            target_shape=support.shape,
        )
        selected = selector & support_binary
        coverage[selected] += 1
        domain_masks.append((domain, selected))
    if np.any(coverage[support_binary] != 1):
        raise LayeredStructureError(
            "Layered structure domains must partition the accepted mask without gaps or overlaps"
        )
    output = source.copy()

    def paint(layer: dict[str, Any], selected: np.ndarray) -> None:
        if layer["mode"] == "solid":
            output[selected] = np.asarray(layer["rgb"], dtype=np.uint8)
            return
        reference = references.get(layer["referenceId"])
        if reference is None or reference.shape != source.shape:
            raise LayeredStructureError("Layered structure reference grid is incompatible")
        output[selected] = reference[selected]

    for domain, selected in domain_masks:
        paint(domain, selected)
    for stroke in guide["strokes"]:
        source_stroke_mask = np.zeros(source_shape, dtype=np.uint8)
        cv2.polylines(
            source_stroke_mask,
            [_raster_points(stroke["points"])],
            False,
            255,
            thickness=stroke["width"],
            lineType=cv2.LINE_8,
        )
        stroke_mask = _expand_source_mask(
            source_stroke_mask > 0,
            scale=scale,
            target_shape=support.shape,
        )
        if np.any((stroke_mask > 0) & ~support_binary):
            raise LayeredStructureError(
                "Layered structure strokes must stay inside the accepted mask"
            )
        paint(stroke, stroke_mask > 0)
    feather_radius = guide["featherRadius"] * scale
    if feather_radius:
        distance = cv2.distanceTransform(support_binary.astype(np.uint8), cv2.DIST_L2, 3)
        alpha = np.minimum(distance / float(feather_radius), 1.0)[..., None]
        blended = np.rint(
            output.astype(np.float32) * alpha + source.astype(np.float32) * (1 - alpha)
        )
        output[support_binary] = blended.astype(np.uint8)[support_binary]
    output[~support_binary] = source[~support_binary]
    return output
