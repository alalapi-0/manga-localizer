from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from PIL import Image, ImageColor

from manga_localizer.imaging.boundary_inpaint import (
    directional_background_consensus,
    two_tone_background_model,
)
from manga_localizer.imaging.screentone_inpaint import screentone_inpaint

DEFAULT_REPAIR_SETTINGS: dict[str, Any] = {
    "method": "telea",
    "maskMode": "text",
    "textPolarity": "auto",
    "maskPadding": 4,
    "dilation": 2,
    "feather": 2,
    "radius": 3,
    "fillColor": "#ffffff",
}

_MAX_MASK_PADDING = 512
_MAX_MASK_DILATION = 128
_MAX_MASK_FEATHER = 128
_MAX_MASK_STROKES = 256
_MAX_STROKE_POINTS = 4096
_MAX_TOTAL_STROKE_POINTS = 16384
_MAX_STROKE_RADIUS = 512.0
_MAX_EXPLICIT_TEXT_MASK_COVERAGE = 0.8
MAX_RENDER_SCALE = 4


def validate_render_scale(value: Any) -> int:
    """Return a supported canonical-to-render scale."""
    if type(value) is not int or not 1 <= value <= MAX_RENDER_SCALE:
        raise ValueError(f"render_scale must be an integer from 1 through {MAX_RENDER_SCALE}")
    return value


def _validate_mask_integer(name: str, value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def validate_mask_edits(
    value: Any,
    *,
    width: int,
    height: int,
    render_scale: int = 1,
) -> list[dict[str, Any]]:
    """Return validated version-1 canonical mask strokes."""
    render_scale = validate_render_scale(render_scale)
    if type(value) is not dict or set(value) != {"version", "strokes"}:
        raise ValueError("maskEdits must contain exactly version and strokes")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("maskEdits version must be 1")
    strokes = value["strokes"]
    if not isinstance(strokes, list):
        raise ValueError("maskEdits strokes must be a list")
    if len(strokes) > _MAX_MASK_STROKES:
        raise ValueError(f"maskEdits must contain at most {_MAX_MASK_STROKES} strokes")
    validated: list[dict[str, Any]] = []
    total_points = 0
    for stroke in strokes:
        if type(stroke) is not dict or set(stroke) != {"mode", "radius", "points"}:
            raise ValueError("Each mask edit stroke must contain exactly mode, radius, and points")
        if stroke["mode"] not in {"add", "erase"}:
            raise ValueError("Mask edit stroke mode must be add or erase")
        radius = stroke["radius"]
        if (
            isinstance(radius, bool)
            or not isinstance(radius, (int, float))
            or not math.isfinite(float(radius))
            or float(radius) <= 0
            or float(radius) > _MAX_STROKE_RADIUS * render_scale
        ):
            raise ValueError(
                "Mask edit stroke radius must be between 0 and "
                f"{_MAX_STROKE_RADIUS * render_scale:g}"
            )
        points = stroke["points"]
        if not isinstance(points, list) or not points:
            raise ValueError("Mask edit stroke points must be a non-empty list")
        if len(points) > _MAX_STROKE_POINTS:
            raise ValueError(
                f"Each mask edit stroke must contain at most {_MAX_STROKE_POINTS} points"
            )
        total_points += len(points)
        if total_points > _MAX_TOTAL_STROKE_POINTS:
            raise ValueError(f"maskEdits must contain at most {_MAX_TOTAL_STROKE_POINTS} points")
        validated_points: list[tuple[float, float]] = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Each mask edit point must contain exactly two coordinates")
            x, y = point
            if any(
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(float(coordinate))
                for coordinate in (x, y)
            ):
                raise ValueError("Mask edit points must use finite numeric coordinates")
            if not 0 <= float(x) <= width or not 0 <= float(y) <= height:
                raise ValueError("Mask edit points must remain within image bounds")
            validated_points.append((float(x), float(y)))
        validated.append(
            {
                "mode": stroke["mode"],
                "radius": float(radius),
                "points": validated_points,
            }
        )
    return validated


def _apply_mask_edits(
    mask: np.ndarray,
    regions: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    render_scale: int = 1,
) -> None:
    height, width = mask.shape
    for region in regions:
        raw_edits = region.get("maskEdits", region.get("mask_edits"))
        if raw_edits is None:
            continue
        for stroke in validate_mask_edits(
            raw_edits,
            width=width,
            height=height,
            render_scale=render_scale,
        ):
            if stroke["mode"] != mode:
                continue
            color = 255 if mode == "add" else 0
            radius = max(1, round(stroke["radius"]))
            points = np.rint(np.asarray(stroke["points"], dtype=np.float64)).astype(np.int32)
            for start, end in pairwise(points):
                cv2.line(
                    mask,
                    tuple(int(value) for value in start),
                    tuple(int(value) for value in end),
                    color,
                    thickness=radius * 2,
                    lineType=cv2.LINE_8,
                )
            for point in points:
                cv2.circle(
                    mask,
                    tuple(int(value) for value in point),
                    radius,
                    color,
                    thickness=-1,
                    lineType=cv2.LINE_8,
                )


def _dimensions(image: Path | Image.Image | tuple[int, int] | np.ndarray) -> tuple[int, int]:
    if isinstance(image, tuple):
        return int(image[0]), int(image[1])
    if isinstance(image, Path):
        with Image.open(image) as opened:
            return opened.size
    if isinstance(image, Image.Image):
        return image.size
    if image.ndim < 2:
        raise ValueError("Image array must have at least two dimensions")
    return int(image.shape[1]), int(image.shape[0])


def create_mask(
    image: Path | Image.Image | tuple[int, int] | np.ndarray,
    regions: Sequence[Mapping[str, Any]],
    *,
    padding: int = 3,
    dilation: int = 1,
    feather: int = 0,
    mask_mode: str = "region",
    text_polarity: str = "auto",
    render_scale: int = 1,
) -> np.ndarray:
    """Create an automatic or explicit manual mask in canonical image coordinates."""
    width, height = _dimensions(image)
    if width <= 0 or height <= 0:
        raise ValueError("Mask dimensions must be positive")
    render_scale = validate_render_scale(render_scale)
    padding = _validate_mask_integer("padding", padding, _MAX_MASK_PADDING * render_scale)
    dilation = _validate_mask_integer("dilation", dilation, _MAX_MASK_DILATION * render_scale)
    feather = _validate_mask_integer("feather", feather, _MAX_MASK_FEATHER * render_scale)
    mask = np.zeros((height, width), dtype=np.uint8)
    normalized_default_mode = mask_mode.lower().replace("_", "-")
    if normalized_default_mode not in {"region", "text", "manual"}:
        raise ValueError("Mask mode must be 'region', 'text', or 'manual'")
    normalized_default_polarity = text_polarity.lower().replace("_", "-")
    if normalized_default_polarity not in {"auto", "dark", "light"}:
        raise ValueError("Text polarity must be 'auto', 'dark', or 'light'")
    grayscale: np.ndarray | None = None
    if not isinstance(image, tuple):
        grayscale = np.asarray(_pil_image(image).convert("L"), dtype=np.uint8)
    automatic_regions: list[Mapping[str, Any]] = []
    manual_regions: list[Mapping[str, Any]] = []
    for region in regions:
        selected_mode = (
            str(region.get("maskMode", region.get("mask_mode", normalized_default_mode)))
            .lower()
            .replace("_", "-")
        )
        if selected_mode not in {"region", "text", "manual"}:
            raise ValueError("Mask mode must be 'region', 'text', or 'manual'")
        selected_polarity = (
            str(
                region.get(
                    "textPolarity",
                    region.get("text_polarity", normalized_default_polarity),
                )
            )
            .lower()
            .replace("_", "-")
        )
        if selected_polarity not in {"auto", "dark", "light"}:
            raise ValueError("Text polarity must be 'auto', 'dark', or 'light'")
        if selected_mode == "manual":
            # Manual mode is a strict authority boundary: geometry, detector
            # polygons, image segmentation, and morphology cannot create any
            # support. Only persisted add strokes contribute pixels below.
            manual_regions.append(region)
            continue
        automatic_regions.append(region)
        region_mask = np.zeros_like(mask)
        # Full-region mode is an explicit escape hatch from detector geometry.
        # A persisted detection polygon only constrains text-aware mode.
        polygon = (
            (region.get("polygon") or region.get("maskPolygon"))
            if selected_mode == "text"
            else None
        )
        if polygon is not None:
            if not isinstance(polygon, (list, tuple)) or not 3 <= len(polygon) <= 4096:
                raise ValueError("Mask polygon must contain between 3 and 4096 points")
            normalized_polygon: list[list[float]] = []
            for point in polygon:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("Each mask polygon point must contain two coordinates")
                coordinates = (float(point[0]), float(point[1]))
                if (
                    not all(math.isfinite(value) for value in coordinates)
                    or not 0 <= coordinates[0] <= width
                    or not 0 <= coordinates[1] <= height
                ):
                    raise ValueError("Mask polygon points must remain within image bounds")
                normalized_polygon.append([coordinates[0], coordinates[1]])
            points = np.asarray(normalized_polygon, dtype=np.float32)
            cv2.fillPoly(region_mask, [np.rint(points).astype(np.int32)], 255)
        else:
            x = float(region["x"])
            y = float(region["y"])
            region_width = float(region["width"])
            region_height = float(region["height"])
            if region_width <= 0 or region_height <= 0:
                raise ValueError("Mask region dimensions must be positive")
            region_padding = _validate_mask_integer(
                "padding",
                region.get("padding", padding),
                _MAX_MASK_PADDING * render_scale,
            )
            geometric_padding = region_padding if selected_mode == "region" else 0
            center = (x + region_width / 2, y + region_height / 2)
            size = (
                region_width + geometric_padding * 2,
                region_height + geometric_padding * 2,
            )
            angle = float(region.get("rotation", 0))
            points = cv2.boxPoints((center, size, angle))
            cv2.fillPoly(region_mask, [np.rint(points).astype(np.int32)], 255)
        region_padding = _validate_mask_integer(
            "padding",
            region.get("padding", padding),
            _MAX_MASK_PADDING * render_scale,
        )
        if selected_mode == "text" and grayscale is not None:
            region_mask = _text_pixels(
                grayscale,
                region_mask,
                selected_polarity,
                planned_expansion=region_padding + dilation + feather,
            )
            if region_padding > 0 and np.any(region_mask):
                size = region_padding * 2 + 1
                region_mask = cv2.dilate(
                    region_mask,
                    np.ones((size, size), dtype=np.uint8),
                    iterations=1,
                )
        elif polygon is not None and region_padding > 0:
            size = region_padding * 2 + 1
            region_mask = cv2.dilate(
                region_mask,
                np.ones((size, size), dtype=np.uint8),
                iterations=1,
            )
        mask = np.maximum(mask, region_mask)
    if dilation > 0:
        size = dilation * 2 + 1
        mask = cv2.dilate(mask, np.ones((size, size), dtype=np.uint8), iterations=1)
    _apply_mask_edits(
        mask,
        automatic_regions,
        mode="add",
        render_scale=render_scale,
    )
    if feather > 0:
        size = feather * 2 + 1
        mask = cv2.GaussianBlur(mask, (size, size), 0)
    if manual_regions:
        manual_mask = np.zeros_like(mask)
        _apply_mask_edits(
            manual_mask,
            manual_regions,
            mode="add",
            render_scale=render_scale,
        )
        mask = np.maximum(mask, manual_mask)
    # Erase strokes are the final authority: their zeroes cannot be
    # repopulated by feathering automatic or explicitly added mask pixels.
    _apply_mask_edits(mask, regions, mode="erase", render_scale=render_scale)
    return mask


def _text_pixels(
    grayscale: np.ndarray,
    geometric_mask: np.ndarray,
    text_polarity: str = "auto",
    *,
    planned_expansion: int = 0,
) -> np.ndarray:
    rows, columns = np.nonzero(geometric_mask)
    if not len(rows):
        return geometric_mask
    height, width = geometric_mask.shape
    region_left, region_right = int(columns.min()), int(columns.max()) + 1
    region_top, region_bottom = int(rows.min()), int(rows.max()) + 1
    region_short_edge = min(region_right - region_left, region_bottom - region_top)
    morphology_size = max(3, region_short_edge // 7)
    if morphology_size % 2 == 0:
        morphology_size += 1
    # Segment through a guard band wider than the largest local morphology
    # radius. A glyph may legitimately touch a tight detector box, while
    # artwork that enters the box continues through this band to its boundary.
    guard = max(8, min(31, morphology_size))
    left = max(0, region_left - guard)
    right = min(width, region_right + guard)
    top = max(0, region_top - guard)
    bottom = min(height, region_bottom + guard)
    crop = grayscale[top:bottom, left:right]
    target = geometric_mask[top:bottom, left:right] > 0
    guard_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (guard * 2 + 1, guard * 2 + 1),
    )
    allowed = cv2.dilate(
        target.astype(np.uint8),
        guard_kernel,
        iterations=1,
        borderType=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)
    short_edge = max(3, min(crop.shape) // 7)
    kernel_size = min(31, short_edge if short_edge % 2 else short_edge + 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    black_hat = cv2.morphologyEx(crop, cv2.MORPH_BLACKHAT, kernel)
    top_hat = cv2.morphologyEx(crop, cv2.MORPH_TOPHAT, kernel)
    black_values = black_hat[allowed]
    white_values = top_hat[allowed]
    if not len(black_values) or max(int(black_values.max()), int(white_values.max())) < 6:
        return np.zeros_like(geometric_mask)
    black_threshold, _ = cv2.threshold(
        black_values.reshape(-1, 1),
        0,
        255,
        cv2.THRESH_BINARY | cv2.THRESH_OTSU,
    )
    white_threshold, _ = cv2.threshold(
        white_values.reshape(-1, 1),
        0,
        255,
        cv2.THRESH_BINARY | cv2.THRESH_OTSU,
    )
    adaptive_size = min(51, max(3, min(crop.shape) // 3))
    if adaptive_size % 2 == 0:
        adaptive_size += 1
    adaptive_dark = cv2.adaptiveThreshold(
        crop,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        adaptive_size,
        7,
    )
    adaptive_light = cv2.adaptiveThreshold(
        255 - crop,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        adaptive_size,
        7,
    )
    eroded_allowed = cv2.erode(
        allowed.astype(np.uint8),
        np.ones((3, 3), dtype=np.uint8),
        iterations=1,
        borderType=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)
    analysis_boundary = allowed & ~eroded_allowed
    # A clipped page edge is not evidence that a component entered the region
    # from surrounding artwork; there are no pixels beyond that image edge.
    if top == 0:
        analysis_boundary[0, :] = False
    if bottom == height:
        analysis_boundary[-1, :] = False
    if left == 0:
        analysis_boundary[:, 0] = False
    if right == width:
        analysis_boundary[:, -1] = False

    target_area = int(np.count_nonzero(target))
    safe_components_by_stream: list[list[np.ndarray]] = []
    exterior_by_stream: list[list[np.ndarray]] = []
    # Adaptive thresholding is only corroborating evidence. Requiring a small
    # local black/top-hat response prevents flat screentone and slow gradients
    # from becoming text solely because they are locally darker or lighter.
    candidate_streams = (
        (black_hat >= max(6, black_threshold)) | ((adaptive_dark > 0) & (black_hat >= 3)),
        (top_hat >= max(6, white_threshold)) | ((adaptive_light > 0) & (top_hat >= 3)),
    )
    for raw_candidate in candidate_streams:
        safe_components: list[np.ndarray] = []
        exterior_components: list[np.ndarray] = []
        candidate = np.where(allowed & raw_candidate, 255, 0).astype(np.uint8)
        candidate = cv2.morphologyEx(
            candidate,
            cv2.MORPH_CLOSE,
            np.ones((3, 3), dtype=np.uint8),
        )
        candidate = np.where(allowed, candidate, 0).astype(np.uint8)
        component_count, labels, _, _ = cv2.connectedComponentsWithStats(candidate, 8)
        for index in range(1, component_count):
            component = labels == index
            inside = component & target
            if not np.any(inside):
                continue
            if np.any(component & analysis_boundary):
                exterior_components.append(inside)
                continue
            safe_components.append(inside)
        safe_components_by_stream.append(safe_components)
        exterior_by_stream.append(exterior_components)

    # An outlined glyph can reach the analysis boundary. Rescue only a bounded
    # collar of the selected-polarity component next to opposite-polarity text
    # evidence. The opposite pixels support the decision but are never written
    # into a single-polarity mask.
    rescue_radius = max(2, min(7, (kernel_size - 1) // 2 + 1))
    rescue_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (rescue_radius * 2 + 1, rescue_radius * 2 + 1),
    )
    raw_stream_masks = [
        np.where(allowed & stream, 255, 0).astype(np.uint8) for stream in candidate_streams
    ]

    def filtered_stream(stream_index: int, *, rescue_exterior: bool) -> np.ndarray:
        pieces = list(safe_components_by_stream[stream_index])
        if rescue_exterior:
            opposite_zone = (
                cv2.dilate(
                    raw_stream_masks[1 - stream_index],
                    rescue_kernel,
                    iterations=1,
                )
                > 0
            )
            for inside in exterior_by_stream[stream_index]:
                rescued = inside & opposite_zone
                if np.any(rescued):
                    pieces.append(rescued)
        filtered = np.zeros_like(crop, dtype=np.uint8)
        for piece in pieces:
            area = int(np.count_nonzero(piece))
            if area >= 2 and area <= max(8, target_area * 0.9):
                filtered[piece] = 255
        return filtered

    if text_polarity in {"dark", "light"}:
        # A forced polarity is intentionally strict. Opposite-polarity pixels
        # can support auto-mode outline rescue, but on a dark/light artwork
        # edge they also surround every exterior art component. Never rescue
        # a component that reaches the analysis boundary in explicit mode.
        selected = filtered_stream(
            0 if text_polarity == "dark" else 1,
            rescue_exterior=False,
        )
        expanded_coverage = _expanded_mask_coverage(selected, target, planned_expansion)
        if expanded_coverage >= _MAX_EXPLICIT_TEXT_MASK_COVERAGE:
            # Explicit polarity is a safety control, not permission to replace
            # a nearly complete detector box. Dense tone/texture fragments can
            # be individually valid components and then merge under ordinary
            # padding, dilation, and feathering. Try the conservative
            # two-tone background residual model, keeping only pixels already
            # present in the requested polarity. If the model declines or the
            # planned expansion remains dense, fail closed; persisted manual
            # add strokes remain the operator-controlled recovery path.
            refined = _refine_mixed_background_text(
                crop,
                target=target,
                allowed=allowed,
                combined=selected,
            )
            selected = np.where((selected > 0) & (refined > 0), 255, 0).astype(np.uint8)
            if _expanded_mask_coverage(selected, target, planned_expansion) >= (
                _MAX_EXPLICIT_TEXT_MASK_COVERAGE
            ):
                selected = np.zeros_like(selected)
        result = np.zeros_like(geometric_mask)
        result[top:bottom, left:right] = selected
        return result

    polarity_masks = [
        filtered_stream(0, rescue_exterior=True),
        filtered_stream(1, rescue_exterior=True),
    ]
    # Dense lettering is still segmented as the union of its two polarity
    # streams. Never replace that union with the complete detector geometry:
    # real outlined captions often overlap artwork, so a full-region fallback
    # destroys the very line art that text-aware mode promises to preserve.
    combined = np.maximum(polarity_masks[0], polarity_masks[1])
    combined = _complete_mixed_background_outline(
        crop,
        target=target,
        allowed=allowed,
        raw_dark=candidate_streams[0],
        raw_light=candidate_streams[1],
        dark_mask=polarity_masks[0],
        light_mask=polarity_masks[1],
        combined=combined,
        radius=rescue_radius,
    )
    combined = _refine_mixed_background_text(
        crop,
        target=target,
        allowed=allowed,
        combined=combined,
    )
    result = np.zeros_like(geometric_mask)
    result[top:bottom, left:right] = combined
    return result


def _expanded_mask_coverage(
    mask: np.ndarray,
    target: np.ndarray,
    expansion: int,
) -> float:
    """Measure whether planned morphology would approximate full geometry."""
    if not np.any(mask) or not np.any(target):
        return 0.0
    expanded_mask = (mask > 0).astype(np.uint8)
    if expansion > 0:
        # Any nonempty pixel reaches the whole crop once the Chebyshev radius
        # spans its longest edge. Short-circuit before constructing a kernel
        # from the independently bounded padding/dilation/feather totals.
        if expansion >= max(mask.shape):
            return 1.0
        size = int(expansion) * 2 + 1
        kernel = np.ones((size, size), dtype=np.uint8)
        expanded_mask = cv2.dilate(expanded_mask, kernel, iterations=1)
    support_area = int(np.count_nonzero(target))
    if support_area == 0:
        return 0.0
    covered = np.count_nonzero((expanded_mask > 0) & target)
    return float(covered) / float(support_area)


def _complete_mixed_background_outline(
    grayscale: np.ndarray,
    *,
    target: np.ndarray,
    allowed: np.ndarray,
    raw_dark: np.ndarray,
    raw_light: np.ndarray,
    dark_mask: np.ndarray,
    light_mask: np.ndarray,
    combined: np.ndarray,
    radius: int,
) -> np.ndarray:
    """Complete only the visible half of an outlined glyph across a tone boundary.

    A light outline connected to light paper (and its inverse on dark art) can
    legitimately reach the analysis boundary. The guard-band filter keeps only
    a small collar next to the opposite-polarity core, which otherwise leaves a
    scalloped remnant. Use two-sided local background agreement to extend that
    collar, but only through raw opposite-polarity evidence close to the mask.
    Explicit dark/light modes never call this helper.
    """
    if not np.any(combined):
        return combined
    completion_radius = max(2, min(7, int(radius)))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (completion_radius * 2 + 1, completion_radius * 2 + 1),
    )
    dark_support = cv2.dilate((dark_mask > 0).astype(np.uint8), kernel) > 0
    light_support = cv2.dilate((light_mask > 0).astype(np.uint8), kernel) > 0
    near_mask = cv2.dilate((combined > 0).astype(np.uint8), kernel) > 0
    candidates = target & near_mask & (combined == 0) & dark_support & light_support
    if not np.any(candidates):
        return combined

    # Do not sample from any raw text-like pixel inside the repair geometry.
    # A narrow dilation skips antialiasing and the existing truncated collar;
    # samples in the surrounding guard remain available as true context.
    raw_inside = target & (raw_dark | raw_light)
    blocked = cv2.dilate(
        raw_inside.astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
        iterations=1,
    ).astype(bool)
    prediction, confident = directional_background_consensus(
        grayscale,
        blocked=blocked,
        query=candidates,
        allowed=allowed,
        max_distance=96,
        max_endpoint_gap=40,
    )
    completed = combined.copy()
    residual = np.abs(grayscale.astype(np.float32) - prediction) >= 20
    completed[candidates & confident & residual] = 255
    return completed


def _refine_mixed_background_text(
    grayscale: np.ndarray,
    *,
    target: np.ndarray,
    allowed: np.ndarray,
    combined: np.ndarray,
) -> np.ndarray:
    """Reject background pixels from dense auto masks on two-tone artwork.

    Outlined lettering may use the same two tones as the artwork below it. In
    that case the per-polarity union can be almost the whole detector box even
    though only pixels that disagree with the background need repair. Infer
    that background from opposing samples outside the box and retain only
    corroborated text-like residuals. Low-confidence, one-sided, and textured
    cases keep the existing conservative mask.
    """
    target_area = int(np.count_nonzero(target))
    if target_area < 8 or np.count_nonzero(combined) / target_area < 0.2:
        return combined
    model = two_tone_background_model(
        grayscale,
        target=target,
        allowed=allowed,
    )
    if model is None:
        return combined
    _, context_mask = model
    context_area = int(np.count_nonzero(context_mask))
    if context_area < 8:
        return combined
    return np.where(context_mask, 255, 0).astype(np.uint8)


def _pil_image(image: Path | Image.Image | np.ndarray) -> Image.Image:
    if isinstance(image, Path):
        with Image.open(image) as opened:
            # Preserve the raw project pixel grid used by persisted region coordinates.
            return opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
    if isinstance(image, Image.Image):
        return image.copy()
    if image.ndim == 2:
        return Image.fromarray(image.astype(np.uint8), mode="L")
    return Image.fromarray(image.astype(np.uint8))


def inpaint(
    image: Path | Image.Image | np.ndarray,
    mask: Path | Image.Image | np.ndarray,
    *,
    radius: float = 3.0,
    method: str = "telea",
    fill_color: str = "#ffffff",
    render_scale: int = 1,
) -> Image.Image:
    """Inpaint locally with an explicit OpenCV repair method, preserving source alpha."""
    source = _pil_image(image)
    mask_image = _pil_image(mask).convert("L")
    if source.size != mask_image.size:
        raise ValueError("Image and inpainting mask dimensions differ")
    render_scale = validate_render_scale(render_scale)
    max_radius = 256 * render_scale
    if (
        isinstance(radius, bool)
        or not isinstance(radius, (int, float))
        or not math.isfinite(float(radius))
        or not 0 < float(radius) <= max_radius
    ):
        raise ValueError(f"Inpainting radius must be between 0 and {max_radius}")
    source_array = np.asarray(source.convert("RGB"), dtype=np.uint8)
    normalized_method = method.lower().replace("_", "-")
    mask_array = np.asarray(mask_image, dtype=np.uint8)
    if normalized_method == "solid":
        fill = np.asarray(ImageColor.getrgb(fill_color), dtype=np.float32)
        alpha = mask_array.astype(np.float32)[..., np.newaxis] / 255.0
        blended = source_array.astype(np.float32) * (1.0 - alpha) + fill * alpha
        result = Image.fromarray(np.rint(blended).astype(np.uint8), mode="RGB")
        if source.mode == "RGBA":
            result.putalpha(source.getchannel("A"))
        return result
    if normalized_method == "screentone":
        restored = screentone_inpaint(source_array, mask_array)
        result = Image.fromarray(restored, mode="RGB")
        if source.mode == "RGBA":
            result.putalpha(source.getchannel("A"))
        elif source.mode == "L":
            result = result.convert("L")
        return result
    source_bgr = cv2.cvtColor(source_array, cv2.COLOR_RGB2BGR)
    binary_mask = np.where(mask_array > 0, 255, 0).astype(np.uint8)
    flag = cv2.INPAINT_TELEA if normalized_method == "telea" else cv2.INPAINT_NS
    if normalized_method not in {"telea", "ns", "navier-stokes"}:
        raise ValueError(
            "Inpainting method must be 'telea', 'navier-stokes', 'solid', or 'screentone'"
        )
    result_bgr = cv2.inpaint(source_bgr, binary_mask, radius, flag)
    generated = cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB)
    weights = mask_array.astype(np.float32)[..., np.newaxis] / 255.0
    blended = source_array.astype(np.float32) * (1.0 - weights)
    blended += generated.astype(np.float32) * weights
    result = Image.fromarray(np.rint(blended).astype(np.uint8), mode="RGB")
    if source.mode == "RGBA":
        result.putalpha(source.getchannel("A"))
    return result


class InpaintingProvider(Protocol):
    def create_mask(
        self,
        image: Path | Image.Image | tuple[int, int] | np.ndarray,
        regions: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> np.ndarray: ...

    def inpaint(
        self,
        image: Path | Image.Image | np.ndarray,
        mask: Path | Image.Image | np.ndarray,
        **options: Any,
    ) -> Image.Image: ...

    def health_check(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


class OpenCVInpaintingProvider:
    name = "opencv"

    @staticmethod
    def create_mask(
        image: Path | Image.Image | tuple[int, int] | np.ndarray,
        regions: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> np.ndarray:
        return create_mask(image, regions, **options)

    @staticmethod
    def inpaint(
        image: Path | Image.Image | np.ndarray,
        mask: Path | Image.Image | np.ndarray,
        **options: Any,
    ) -> Image.Image:
        return inpaint(image, mask, **options)

    def health_check(self) -> dict[str, Any]:
        return {"available": True, "version": cv2.__version__}

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "methods": ["telea", "navier-stokes", "solid", "screentone"],
            "editableMask": True,
            "maskEditVersion": 1,
            "maskModes": ["text", "region", "manual"],
            "textPolarities": ["auto", "dark", "light"],
            "softMaskComposite": True,
        }
