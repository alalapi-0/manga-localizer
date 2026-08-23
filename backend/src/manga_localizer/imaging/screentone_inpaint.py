from __future__ import annotations

import cv2
import numpy as np

_MIN_PERIOD = 3
_MAX_PERIOD = 32
_MAX_TARGET_PIXELS = 32_768


def _period_score(
    grayscale: np.ndarray,
    dark: np.ndarray,
    support: np.ndarray,
    *,
    axis: int,
    shift: int,
    contrast: float,
) -> float | None:
    if axis == 1:
        first = (slice(None), slice(0, -shift))
        second = (slice(None), slice(shift, None))
    else:
        first = (slice(0, -shift), slice(None))
        second = (slice(shift, None), slice(None))
    valid = support[first] & support[second]
    if int(np.count_nonzero(valid)) < 256:
        return None
    class_error = float(np.mean(dark[first][valid] != dark[second][valid]))
    intensity_delta = np.abs(
        grayscale[first].astype(np.int16)[valid] - grayscale[second].astype(np.int16)[valid]
    )
    normalized_delta = min(1.0, float(np.median(intensity_delta)) / max(contrast, 1.0))
    return class_error + 0.25 * normalized_delta


def _detect_period(
    grayscale: np.ndarray,
    dark: np.ndarray,
    support: np.ndarray,
    *,
    axis: int,
    contrast: float,
) -> int | None:
    scores = {
        period: score
        for period in range(_MIN_PERIOD, _MAX_PERIOD + 1)
        if (
            score := _period_score(
                grayscale,
                dark,
                support,
                axis=axis,
                shift=period,
                contrast=contrast,
            )
        )
        is not None
    }
    if not scores:
        return None
    selected = min(scores, key=lambda period: scores[period] + 0.0005 * period)
    selected_score = scores[selected]
    neighbors = [
        scores[period]
        for period in range(max(_MIN_PERIOD, selected - 2), min(_MAX_PERIOD, selected + 2) + 1)
        if period != selected and period in scores
    ]
    if selected_score > 0.08 or not neighbors:
        return None
    if float(np.median(neighbors)) - selected_score < 0.025:
        return None
    return selected


def _small_dark_components(dark: np.ndarray, support: np.ndarray) -> np.ndarray:
    candidates = np.where(dark & support, 255, 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(candidates, connectivity=8)
    kept = np.zeros_like(support, dtype=bool)
    for label in range(1, count):
        _x, _y, width, height, area = stats[label]
        if area <= 96 and width <= 12 and height <= 12:
            kept[labels == label] = True
    return kept


def _phase_template(
    source: np.ndarray,
    dark: np.ndarray,
    support: np.ndarray,
    target: np.ndarray,
    *,
    period_x: int,
    period_y: int,
) -> tuple[np.ndarray, np.ndarray, float] | None:
    height, width = support.shape
    rows, columns = np.indices((height, width))
    phase = (columns % period_x) + period_x * (rows % period_y)
    phase_count = period_x * period_y
    support_phase = phase[support]
    counts = np.bincount(support_phase, minlength=phase_count)
    dark_counts = np.bincount(support_phase, weights=dark[support], minlength=phase_count)
    required_phases = np.unique(phase[target])
    if required_phases.size == 0 or np.any(counts[required_phases] < 5):
        return None

    dark_rate = np.divide(
        dark_counts,
        counts,
        out=np.zeros_like(dark_counts, dtype=np.float64),
        where=counts > 0,
    )
    dark_share = float(np.mean(dark[support]))
    predicted_dark = dark_rate >= max(0.08, 1.25 * dark_share)

    tile_dark_share = float(np.mean(predicted_dark))
    if not 0.01 <= tile_dark_share <= 0.35:
        return None
    recalled_dark = float(np.sum(dark_counts[predicted_dark]))
    total_dark = float(np.sum(dark_counts))
    predicted_count = float(np.sum(counts[predicted_dark]))
    recall = recalled_dark / max(total_dark, 1.0)
    precision = recalled_dark / max(predicted_count, 1.0)
    enrichment = precision / max(dark_share, 1e-6)
    if recall < 0.65 or precision < 0.18 or enrichment < 3.0:
        return None

    template = np.zeros((phase_count, 3), dtype=np.float32)
    for phase_index in range(phase_count):
        phase_support = support & (phase == phase_index)
        matching = phase_support & (dark == predicted_dark[phase_index])
        if int(np.count_nonzero(matching)) < 3:
            return None
        template[phase_index] = np.median(source[matching], axis=0)

    template_gray = np.mean(template, axis=1)
    if (
        not np.any(predicted_dark)
        or not np.any(~predicted_dark)
        or float(np.median(template_gray[~predicted_dark]))
        - float(np.median(template_gray[predicted_dark]))
        < 60.0
    ):
        return None
    return template, phase, recall * enrichment


def _fit_phase_model(
    source: np.ndarray,
    target: np.ndarray,
    domain: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float] | None:
    grayscale = cv2.cvtColor(source, cv2.COLOR_RGB2GRAY)
    protected = (
        cv2.dilate(
            target.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
            iterations=1,
        )
        > 0
    )
    initial_support = domain & ~protected
    target_area = int(np.count_nonzero(target))
    if int(np.count_nonzero(initial_support)) < max(512, target_area // 2):
        return None

    threshold, _ = cv2.threshold(
        grayscale[initial_support].reshape(-1, 1),
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    dark = grayscale <= threshold
    small_dark = _small_dark_components(dark, initial_support)
    support = initial_support & (~dark | small_dark)
    dark_values = grayscale[support & dark]
    light_values = grayscale[support & ~dark]
    if min(dark_values.size, light_values.size) < 64:
        return None
    contrast = float(np.median(light_values)) - float(np.median(dark_values))
    dark_share = float(np.mean(dark[support]))
    if contrast < 72.0 or not 0.01 <= dark_share <= 0.45:
        return None

    period_x = _detect_period(
        grayscale,
        dark,
        support,
        axis=1,
        contrast=contrast,
    )
    period_y = _detect_period(
        grayscale,
        dark,
        support,
        axis=0,
        contrast=contrast,
    )
    if period_x is None or period_y is None:
        return None
    return _phase_template(
        source,
        dark,
        support,
        target,
        period_x=period_x,
        period_y=period_y,
    )


def _background_field_model(
    source: np.ndarray,
    target: np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Infer a flat dark field or long dark edge crossing the periodic target."""
    grayscale = cv2.cvtColor(source, cv2.COLOR_RGB2GRAY)
    protected = (
        cv2.dilate(
            target.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
            iterations=1,
        )
        > 0
    )
    outside = ~protected
    if int(np.count_nonzero(outside)) < 512:
        return None
    threshold, _ = cv2.threshold(
        grayscale[outside].reshape(-1, 1),
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    dark = grayscale <= threshold
    small_dark = _small_dark_components(dark, outside)
    structural_dark = dark & outside & ~small_dark
    collar = (
        cv2.dilate(
            target.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)),
            iterations=1,
        )
        > 0
    ) & outside
    if (
        int(np.count_nonzero(structural_dark & collar)) < 32
        or int(np.count_nonzero((~dark) & collar)) < 32
    ):
        return None

    linear_prediction = _linear_field_prediction(
        structural_dark,
        protected=protected,
        target=target,
    )
    if linear_prediction is None:
        # The collar proves that a structural field crosses the target.  Falling
        # back to the periodic template would turn its dark side into screentone,
        # while a generic binary inpaint stamps the rounded mask silhouette.  Refuse
        # this explicit method unless the interface geometry is independently known.
        raise ValueError("Screentone repair could not verify the structural field boundary")
    prediction = np.where(linear_prediction, 0, 255).astype(np.uint8)
    nearby_dark = structural_dark & collar
    dark_color = np.median(source[nearby_dark], axis=0).astype(np.float32)
    return prediction, dark_color


def _linear_field_prediction(
    structural_dark: np.ndarray,
    *,
    protected: np.ndarray,
    target: np.ndarray,
) -> np.ndarray | None:
    """Continue a locally straight boundary of a filled dark field through target."""
    near = (
        cv2.dilate(
            target.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (81, 81)),
            iterations=1,
        )
        > 0
    )
    away_from_target = ~(
        cv2.dilate(
            protected.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
            iterations=1,
        )
        > 0
    )
    boundary = structural_dark & ~(
        cv2.erode(structural_dark.astype(np.uint8), np.ones((3, 3), dtype=np.uint8)) > 0
    )
    boundary &= near & away_from_target
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        boundary.astype(np.uint8),
        connectivity=8,
    )
    rows, columns = np.indices(target.shape)
    best: tuple[float, np.ndarray] | None = None
    for label in range(1, count):
        component_boundary = labels == label
        if int(stats[label, cv2.CC_STAT_AREA]) < 16:
            continue
        boundary_rows, boundary_columns = np.nonzero(component_boundary)
        if boundary_rows.size < 16:
            continue
        points = np.column_stack((boundary_columns, boundary_rows)).astype(np.float32)
        direction_x, direction_y, origin_x, origin_y = cv2.fitLine(
            points,
            cv2.DIST_WELSCH,
            0,
            0.01,
            0.01,
        ).reshape(-1)
        residual = np.abs(
            (points[:, 0] - origin_x) * direction_y - (points[:, 1] - origin_y) * direction_x
        )
        if float(np.median(residual)) > 2.5 or float(np.percentile(residual, 90)) > 6.0:
            continue
        along = points[:, 0] * direction_x + points[:, 1] * direction_y
        if float(np.ptp(along)) < 20.0:
            continue

        signed = (columns - origin_x) * -direction_y + (rows - origin_y) * direction_x
        band = near & away_from_target & (np.abs(signed) <= 48.0)
        positive = band & (signed >= 4.0)
        negative = band & (signed <= -4.0)
        if min(int(np.count_nonzero(positive)), int(np.count_nonzero(negative))) < 64:
            continue
        positive_density = float(np.mean(structural_dark[positive]))
        negative_density = float(np.mean(structural_dark[negative]))
        if abs(positive_density - negative_density) < 0.40:
            continue
        predicted_dark = signed > 0 if positive_density > negative_density else signed < 0
        validation_band = band & (np.abs(signed) >= 4.0)
        dark_side = predicted_dark & validation_band
        light_side = ~predicted_dark & validation_band
        dark_density = float(np.mean(structural_dark[dark_side]))
        light_density = float(np.mean(structural_dark[light_side]))
        # The light side is a screentone rather than a flat white plate, so a
        # conservative amount of residual structural-dark sampling is expected.
        # Require a dense dark half-plane and a large side-to-side contrast instead
        # of treating every connected/antialiased dot as a false boundary.
        if dark_density < 0.50 or light_density > 0.30:
            continue
        score = dark_density - light_density - float(np.median(residual)) / 20.0
        if best is None or score > best[0]:
            best = (score, predicted_dark)
    return None if best is None else best[1]


def screentone_inpaint(source: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Restore a locally periodic, near-grayscale screentone under an explicit mask.

    This is intentionally strict and has no generic inpainting fallback. Callers must
    choose it explicitly; non-periodic or insufficient context raises ``ValueError``.
    """
    if source.ndim != 3 or source.shape[2] != 3 or source.dtype != np.uint8:
        raise ValueError("Screentone repair requires an RGB uint8 source")
    if mask.shape != source.shape[:2] or mask.dtype != np.uint8:
        raise ValueError("Screentone repair mask must match the source dimensions")
    target = mask > 0
    target_area = int(np.count_nonzero(target))
    if target_area == 0:
        return source.copy()
    if target_area > _MAX_TARGET_PIXELS:
        raise ValueError("Screentone repair mask is too large for conservative reconstruction")

    ys, xs = np.nonzero(target)
    margin = 96
    left = max(0, int(xs.min()) - margin)
    top = max(0, int(ys.min()) - margin)
    right = min(source.shape[1], int(xs.max()) + margin + 1)
    bottom = min(source.shape[0], int(ys.max()) + margin + 1)
    local_source = source[top:bottom, left:right]
    local_target = target[top:bottom, left:right]

    chroma = np.ptp(local_source.astype(np.int16), axis=2)
    if float(np.percentile(chroma[~local_target], 95)) > 8.0:
        raise ValueError("Screentone repair requires a near-grayscale background")

    local_rows, local_columns = np.nonzero(local_target)
    horizontal = np.zeros_like(local_target, dtype=bool)
    horizontal[
        max(0, int(local_rows.min()) - 16) : min(local_target.shape[0], int(local_rows.max()) + 17),
        :,
    ] = True
    vertical = np.zeros_like(local_target, dtype=bool)
    vertical[
        :,
        max(0, int(local_columns.min()) - 16) : min(
            local_target.shape[1], int(local_columns.max()) + 17
        ),
    ] = True
    above = np.zeros_like(local_target, dtype=bool)
    above[: max(0, int(local_rows.min()) - 4), :] = True
    below = np.zeros_like(local_target, dtype=bool)
    below[min(local_target.shape[0], int(local_rows.max()) + 5) :, :] = True
    left_side = np.zeros_like(local_target, dtype=bool)
    left_side[:, : max(0, int(local_columns.min()) - 4)] = True
    right_side = np.zeros_like(local_target, dtype=bool)
    right_side[:, min(local_target.shape[1], int(local_columns.max()) + 5) :] = True
    models = [
        model
        for domain in (
            horizontal,
            vertical,
            above,
            below,
            left_side,
            right_side,
            np.ones_like(local_target, dtype=bool),
        )
        if (model := _fit_phase_model(local_source, local_target, domain)) is not None
    ]
    if not models:
        raise ValueError("Screentone repair could not verify a phase-consistent texture")
    template, phase, _quality = max(models, key=lambda model: model[2])

    local_result = local_source.copy()
    prediction = template[phase[local_target]]
    field_model = _background_field_model(local_source, local_target)
    if field_model is not None:
        field_prediction, dark_color = field_model
        structural_target = field_prediction[local_target] < 128
        prediction[structural_target] = dark_color
    weights = mask[top:bottom, left:right][local_target].astype(np.float32)[:, None] / 255.0
    blended = local_source[local_target].astype(np.float32) * (1.0 - weights)
    blended += prediction * weights
    local_result[local_target] = np.rint(blended).clip(0, 255).astype(np.uint8)

    result = source.copy()
    result[top:bottom, left:right] = local_result
    result[~target] = source[~target]
    return result
