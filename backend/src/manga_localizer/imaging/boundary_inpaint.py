from __future__ import annotations

import cv2
import numpy as np

MAX_DIRECTIONAL_QUERY_PIXELS = 16_384


def two_tone_background_model(
    pixels: np.ndarray,
    *,
    target: np.ndarray,
    allowed: np.ndarray | None = None,
    residual_threshold: int = 20,
    use_directional_prediction: bool = True,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Infer a discrete two-tone background and its visible overlay pixels.

    Directional agreement supplies the high-fidelity prediction wherever both
    sides match. A threshold-label inpaint continues only the dark/light class
    through the narrow high-contrast seam where no direction can agree. The
    function declines one-sided, weakly separated, or poorly supported scenes.
    """
    if pixels.dtype != np.uint8 or pixels.ndim not in {2, 3}:
        raise ValueError("pixels must be a uint8 grayscale or color array")
    height, width = pixels.shape[:2]
    if target.shape != (height, width):
        raise ValueError("target mask must match the pixel grid")
    target_mask = target.astype(bool, copy=False)
    if not np.any(target_mask):
        return None
    if allowed is None:
        allowed_mask = np.ones((height, width), dtype=bool)
    else:
        if allowed.shape != (height, width):
            raise ValueError("allowed mask must match the pixel grid")
        allowed_mask = allowed.astype(bool, copy=False)
    if pixels.ndim == 2:
        grayscale = pixels
    else:
        if pixels.shape[2] < 3:
            raise ValueError("color pixels must have at least three channels")
        grayscale = cv2.cvtColor(pixels[..., :3], cv2.COLOR_RGB2GRAY)

    protected = cv2.dilate(
        target_mask.astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
        iterations=1,
    ).astype(bool)
    ring = allowed_mask & ~protected
    ring_values = grayscale[ring]
    if ring_values.size < 32:
        return None
    threshold, _ = cv2.threshold(
        ring_values.reshape(-1, 1),
        0,
        255,
        cv2.THRESH_BINARY | cv2.THRESH_OTSU,
    )
    dark_ring = ring & (grayscale <= threshold)
    light_ring = ring & (grayscale > threshold)
    dark_values = grayscale[dark_ring]
    light_values = grayscale[light_ring]
    minimum_group = max(8, round(ring_values.size * 0.14))
    if dark_values.size < minimum_group or light_values.size < minimum_group:
        return None
    if float(np.mean(light_values)) - float(np.mean(dark_values)) < 72.0:
        return None

    tone_labels = np.where(grayscale > threshold, 255, 0).astype(np.uint8)
    filled_labels = cv2.inpaint(
        tone_labels,
        target_mask.astype(np.uint8) * 255,
        3,
        cv2.INPAINT_TELEA,
    )
    model_light = filled_labels > 127
    dark_color = np.median(pixels[dark_ring], axis=0)
    light_color = np.median(pixels[light_ring], axis=0)

    directional, directional_confident = directional_background_consensus(
        pixels,
        blocked=protected,
        query=target_mask,
        allowed=allowed_mask,
        max_distance=max(height, width),
        max_endpoint_gap=40,
    )
    if pixels.ndim == 2:
        directional_gray = directional
    else:
        directional_gray = (
            directional[..., 0] * 0.299 + directional[..., 1] * 0.587 + directional[..., 2] * 0.114
        )
    directional_light = directional_gray > threshold
    use_directional = directional_confident
    if float(np.mean(use_directional[target_mask])) < 0.25:
        return None
    background_light = model_light.copy()
    background_light[use_directional] = directional_light[use_directional]
    prediction = np.zeros_like(pixels, dtype=np.float32)
    prediction[target_mask & ~background_light] = dark_color
    prediction[target_mask & background_light] = light_color
    if use_directional_prediction:
        prediction[use_directional] = directional[use_directional]

    source_light = grayscale > threshold
    directional_residual = (
        np.abs(grayscale.astype(np.float32) - directional_gray) >= residual_threshold
    )
    overlay = target_mask & (
        (use_directional & directional_residual)
        | (~use_directional & (source_light != background_light))
    )
    return prediction, overlay


def directional_background_consensus(
    pixels: np.ndarray,
    *,
    blocked: np.ndarray,
    query: np.ndarray,
    allowed: np.ndarray | None = None,
    max_distance: int = 96,
    max_endpoint_gap: int = 40,
) -> tuple[np.ndarray, np.ndarray]:
    """Predict occluded pixels from the most consistent opposing samples.

    Four axes are considered for each query pixel. A prediction is returned
    only when both first unblocked samples exist and their grayscale values
    agree closely enough. This keeps the helper conservative on texture,
    curves, page edges, and one-sided context.
    """
    if pixels.dtype != np.uint8 or pixels.ndim not in {2, 3}:
        raise ValueError("pixels must be a uint8 grayscale or color array")
    height, width = pixels.shape[:2]
    if blocked.shape != (height, width) or query.shape != (height, width):
        raise ValueError("blocked and query masks must match the pixel grid")
    if allowed is None:
        allowed_mask = np.ones((height, width), dtype=bool)
    else:
        if allowed.shape != (height, width):
            raise ValueError("allowed mask must match the pixel grid")
        allowed_mask = allowed.astype(bool, copy=False)
    blocked_mask = blocked.astype(bool, copy=False)
    query_mask = query.astype(bool, copy=False)
    if pixels.ndim == 2:
        grayscale = pixels
        prediction = np.zeros((height, width), dtype=np.float32)
    else:
        if pixels.shape[2] < 3:
            raise ValueError("color pixels must have at least three channels")
        grayscale = cv2.cvtColor(pixels[..., :3], cv2.COLOR_RGB2GRAY)
        prediction = np.zeros(pixels.shape, dtype=np.float32)
    confidence = np.zeros((height, width), dtype=bool)
    # This helper is a conservative refinement, not the base mask. Decline an
    # unusually dense query instead of letting Python ray scans stall a page;
    # callers retain their existing mask when confidence is empty.
    if np.count_nonzero(query_mask) > MAX_DIRECTIONAL_QUERY_PIXELS:
        return prediction, confidence
    limit = max(1, min(int(max_distance), 96, max(height, width)))
    directions = ((0, 1), (1, 0), (1, 1), (1, -1))

    for row, column in zip(*np.nonzero(query_mask), strict=True):
        best: tuple[float, int, int, int, int, int, int, int, int] | None = None
        for delta_row, delta_column in directions:
            endpoints: list[tuple[int, int, int] | None] = []
            for sign in (-1, 1):
                endpoint: tuple[int, int, int] | None = None
                for distance in range(1, limit + 1):
                    sample_row = row + sign * delta_row * distance
                    sample_column = column + sign * delta_column * distance
                    if not (0 <= sample_row < height and 0 <= sample_column < width):
                        break
                    if not allowed_mask[sample_row, sample_column]:
                        break
                    if not blocked_mask[sample_row, sample_column]:
                        endpoint = (sample_row, sample_column, distance)
                        break
                endpoints.append(endpoint)
            if endpoints[0] is None or endpoints[1] is None:
                continue
            first, second = endpoints
            assert first is not None and second is not None
            first_row, first_column, first_distance = first
            second_row, second_column, second_distance = second
            first_value = int(grayscale[first_row, first_column])
            second_value = int(grayscale[second_row, second_column])
            endpoint_gap = abs(first_value - second_value)
            score = endpoint_gap + 0.35 * (first_distance + second_distance)
            if best is None or score < best[0]:
                best = (
                    score,
                    endpoint_gap,
                    first_row,
                    first_column,
                    first_distance,
                    second_row,
                    second_column,
                    second_distance,
                    first_distance + second_distance,
                )
        if best is None or best[1] > max_endpoint_gap:
            continue
        (
            _,
            _,
            first_row,
            first_column,
            first_distance,
            second_row,
            second_column,
            second_distance,
            total_distance,
        ) = best
        prediction[row, column] = (
            pixels[first_row, first_column].astype(np.float32) * second_distance
            + pixels[second_row, second_column].astype(np.float32) * first_distance
        ) / max(1, total_distance)
        confidence[row, column] = True
    return prediction, confidence
