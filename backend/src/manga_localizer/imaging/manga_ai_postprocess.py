from __future__ import annotations

import cv2
import numpy as np

MIN_COMPONENT_PIXELS = 64
MIN_RING_PIXELS = 64
MIN_TONE_PIXELS = 16
MIN_TONE_SHARE = 0.08
MIN_TONE_SEPARATION = 72
MAX_RING_CHROMA_P95 = 8
OVERVIEW_LINEART_BLOCK_CANONICAL = 25
OVERVIEW_LINEART_THRESHOLD_C = 12
OVERVIEW_LINEART_MIN_BLACK_SHARE = 0.002
OVERVIEW_LINEART_MAX_BLACK_SHARE = 0.25
OVERVIEW_LINEART_MAX_ISOLATED_SHARE = 0.05


def manga_tone_cleanup(
    source_rgb: np.ndarray,
    ai_rgb: np.ndarray,
    mask: np.ndarray,
    *,
    radius: float = 3.0,
) -> np.ndarray | None:
    """Snap a confident grayscale AI redraw back to a local manga ink palette.

    The AI result remains authoritative for the dark/light classification. Source
    pixels outside the repair support are used only to estimate the local paper
    and ink tones. Ambiguous components make the optional candidate decline.
    """
    if source_rgb.dtype != np.uint8 or ai_rgb.dtype != np.uint8:
        raise ValueError("Manga cleanup images must use uint8 pixels")
    if source_rgb.ndim != 3 or source_rgb.shape[2] != 3:
        raise ValueError("Manga cleanup source must have shape HxWx3")
    if ai_rgb.shape != source_rgb.shape:
        raise ValueError("Manga cleanup source and AI result dimensions differ")
    if mask.dtype != np.uint8 or mask.shape != source_rgb.shape[:2]:
        raise ValueError("Manga cleanup mask must be a matching uint8 array")
    support = mask > 0
    if not np.any(support):
        return None

    source_chroma = np.max(source_rgb, axis=2) - np.min(source_rgb, axis=2)
    source_gray = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2GRAY)
    ai_gray = cv2.cvtColor(ai_rgb, cv2.COLOR_RGB2GRAY)
    component_count, labels = cv2.connectedComponents(support.astype(np.uint8), connectivity=8)
    ring_radius = max(4, min(48, round(float(radius) * 2.0)))
    ring_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (ring_radius * 2 + 1, ring_radius * 2 + 1),
    )
    snapped = ai_rgb.copy()

    for label in range(1, component_count):
        target = labels == label
        target_count = int(np.count_nonzero(target))
        if target_count < MIN_COMPONENT_PIXELS:
            return None
        expanded = cv2.dilate(target.astype(np.uint8), ring_kernel, iterations=1) > 0
        ring = expanded & ~support
        ring_count = int(np.count_nonzero(ring))
        if ring_count < MIN_RING_PIXELS:
            return None
        if float(np.percentile(source_chroma[ring], 95)) > MAX_RING_CHROMA_P95:
            return None

        ring_gray = source_gray[ring]
        threshold, _ = cv2.threshold(
            ring_gray.reshape(-1, 1),
            0,
            255,
            cv2.THRESH_BINARY | cv2.THRESH_OTSU,
        )
        dark = ring_gray <= threshold
        light = ~dark
        required = max(MIN_TONE_PIXELS, round(ring_count * MIN_TONE_SHARE))
        if int(np.count_nonzero(dark)) < required or int(np.count_nonzero(light)) < required:
            return None
        dark_tone = int(np.median(ring_gray[dark]))
        light_tone = int(np.median(ring_gray[light]))
        if light_tone - dark_tone < MIN_TONE_SEPARATION:
            return None

        local_ai = ai_gray[target]
        split = (dark_tone + light_tone) / 2.0
        dark_prediction = local_ai <= split
        prediction_required = max(4, round(target_count * 0.005))
        if (
            int(np.count_nonzero(dark_prediction)) < prediction_required
            or int(np.count_nonzero(~dark_prediction)) < prediction_required
        ):
            return None
        palette = np.where(dark_prediction, dark_tone, light_tone).astype(np.uint8)
        snapped[target] = palette[:, None]

    weights = mask.astype(np.float32) / 255.0
    result = source_rgb.astype(np.float32) * (1.0 - weights[..., None])
    result += snapped.astype(np.float32) * weights[..., None]
    output = np.clip(np.rint(result), 0, 255).astype(np.uint8)
    output[~support] = source_rgb[~support]
    return output


def manga_overview_lineart_cleanup(
    source_rgb: np.ndarray,
    ai_overview_rgb: np.ndarray,
    mask: np.ndarray,
    *,
    render_scale: int = 1,
) -> np.ndarray | None:
    """Crisp a LaMa overview on confident light-paper manga backgrounds.

    Only the AI overview determines pixels inside the persisted repair support.
    Source pixels are sampled exclusively outside that support to validate that
    the surrounding substrate is light, nearly grayscale paper. Unsupported or
    ambiguous inputs decline instead of publishing a misleading comparison.
    """
    if source_rgb.dtype != np.uint8 or ai_overview_rgb.dtype != np.uint8:
        raise ValueError("Overview cleanup images must use uint8 pixels")
    if source_rgb.ndim != 3 or source_rgb.shape[2] != 3:
        raise ValueError("Overview cleanup source must have shape HxWx3")
    if ai_overview_rgb.shape != source_rgb.shape:
        raise ValueError("Overview cleanup source and AI result dimensions differ")
    if mask.dtype != np.uint8 or mask.shape != source_rgb.shape[:2]:
        raise ValueError("Overview cleanup mask must be a matching uint8 array")
    if type(render_scale) is not int or not 1 <= render_scale <= 4:
        raise ValueError("render_scale must be an integer from 1 through 4")
    unique_mask = np.unique(mask)
    if np.any((unique_mask != 0) & (unique_mask != 255)):
        return None
    support = mask == 255
    support_count = int(np.count_nonzero(support))
    if support_count == 0 or support_count == mask.size:
        return None

    outer_radius = 12 * render_scale
    inner_radius = 2 * render_scale
    outer_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (outer_radius * 2 + 1, outer_radius * 2 + 1),
    )
    inner_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (inner_radius * 2 + 1, inner_radius * 2 + 1),
    )
    outer = cv2.dilate(support.astype(np.uint8), outer_kernel, iterations=1) > 0
    inner = cv2.dilate(support.astype(np.uint8), inner_kernel, iterations=1) > 0
    ring = outer & ~inner
    ring_count = int(np.count_nonzero(ring))
    required_ring = max(256 * render_scale * render_scale, round(support_count * 0.02))
    if ring_count < required_ring:
        return None

    ring_rgb = source_rgb[ring]
    ring_chroma = np.max(ring_rgb, axis=1).astype(np.int16) - np.min(
        ring_rgb,
        axis=1,
    ).astype(np.int16)
    if float(np.percentile(ring_chroma, 95)) > 8:
        return None
    ring_gray = cv2.cvtColor(ring_rgb.reshape(-1, 1, 3), cv2.COLOR_RGB2GRAY).reshape(-1)
    if (
        float(np.median(ring_gray)) < 230
        or float(np.percentile(ring_gray, 25)) < 215
        or float(np.mean(ring_gray >= 224)) < 0.70
    ):
        return None

    ai_support_rgb = ai_overview_rgb[support]
    ai_chroma = np.max(ai_support_rgb, axis=1).astype(np.int16) - np.min(
        ai_support_rgb,
        axis=1,
    ).astype(np.int16)
    if float(np.percentile(ai_chroma, 95)) > 8:
        return None

    working_gray = np.empty(mask.shape, dtype=np.uint8)
    outside = ~support
    source_outside_rgb = source_rgb[outside]
    ai_inside_rgb = ai_overview_rgb[support]
    working_gray[outside] = cv2.cvtColor(
        source_outside_rgb.reshape(-1, 1, 3),
        cv2.COLOR_RGB2GRAY,
    ).reshape(-1)
    working_gray[support] = cv2.cvtColor(
        ai_inside_rgb.reshape(-1, 1, 3),
        cv2.COLOR_RGB2GRAY,
    ).reshape(-1)
    block_size = OVERVIEW_LINEART_BLOCK_CANONICAL * render_scale
    if block_size % 2 == 0:
        block_size += 1
    if min(mask.shape) < block_size:
        return None
    thresholded = cv2.adaptiveThreshold(
        working_gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block_size,
        OVERVIEW_LINEART_THRESHOLD_C,
    )
    black = support & (thresholded == 0)
    black_count = int(np.count_nonzero(black))
    black_share = black_count / support_count
    if not OVERVIEW_LINEART_MIN_BLACK_SHARE <= black_share <= OVERVIEW_LINEART_MAX_BLACK_SHARE:
        return None
    neighbours = cv2.filter2D(
        black.astype(np.uint8),
        cv2.CV_16U,
        np.ones((3, 3), dtype=np.uint8),
        borderType=cv2.BORDER_CONSTANT,
    )
    isolated_share = float(np.count_nonzero(black & (neighbours <= 1))) / black_count
    if isolated_share > OVERVIEW_LINEART_MAX_ISOLATED_SHARE:
        return None

    output = source_rgb.copy()
    output[support] = thresholded[support, None]
    output[outside] = source_rgb[outside]
    return output


__all__ = ["manga_overview_lineart_cleanup", "manga_tone_cleanup"]
