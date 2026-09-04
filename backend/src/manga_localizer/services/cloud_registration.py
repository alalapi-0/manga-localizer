"""Opt-in, bounded whole-frame registration; never an inpaint or acceptance gate."""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

import cv2
import numpy as np
import PIL
from PIL import Image

from manga_localizer.services.projects import ProjectError

PROFILE = "canonical-whole-frame-registration-v1"
_LIMITS = {
    "maxPixels": 32_000_000,
    "analysisLongestSide": 2048,
    "minSide": 128,
    "features": 4000,
    "contrastThreshold": 0.04,
    "edgeThreshold": 10,
    "sigma": 1.6,
    "octaveLayers": 3,
    "descriptorExclusionSizeFactor": 8.0,
    "exclusionMargin": 24,
    "loweRatio": 0.7,
    "ransacSeed": 1769,
    "ransacIterations": 512,
    "residualThreshold": 2.5,
    "minTraining": 60,
    "minValidation": 20,
    "minInlierRatio": 0.75,
    "maxValidationMedian": 1.25,
    "maxValidationP95": 2.5,
    "minQuadrants": 3,
    "minSpan": 0.5,
    "minHullAreaRatio": 0.15,
    "maxScaleDifference": 0.01,
    "maxShear": 0.004,
    "maxRotationRadians": 0.005,
    "maxTranslationPixels": 8.0,
    "maxCornerDisplacementPixels": 16.0,
    "interpolationBorderMargin": 4,
}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _fail(reason: str) -> None:
    raise ProjectError(f"Cloud registration rejected: {reason}")


def _read(payload: bytes, mode: str, grid: tuple[int, int] | None = None) -> np.ndarray:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > _LIMITS["maxPixels"]:
                _fail("raster pixel limit")
            if grid is not None and image.size != grid:
                _fail("input grids differ")
            return np.array(image.convert(mode))
    except (OSError, ValueError) as error:
        raise ProjectError("Cloud registration inputs cannot be decoded") from error


def _features(gray: np.ndarray, distance: np.ndarray):
    sift = cv2.SIFT_create(
        nfeatures=_LIMITS["features"],
        nOctaveLayers=_LIMITS["octaveLayers"],
        contrastThreshold=_LIMITS["contrastThreshold"],
        edgeThreshold=_LIMITS["edgeThreshold"],
        sigma=_LIMITS["sigma"],
    )
    keypoints, descriptors = sift.detectAndCompute(
        gray, np.uint8(distance >= _LIMITS["exclusionMargin"]) * 255
    )
    if descriptors is None:
        _fail("insufficient isolated features")
    selected = []
    centers: set[tuple[int, int]] = set()
    for index, point in enumerate(keypoints):
        x, y = point.pt
        # Duplicate orientations of the same feature must not inflate support.
        center = (round(x / 2), round(y / 2))
        required = max(
            _LIMITS["exclusionMargin"],
            point.size * _LIMITS["descriptorExclusionSizeFactor"],
        )
        if distance[round(y), round(x)] >= required and center not in centers:
            centers.add(center)
            selected.append(index)
            if len(selected) == _LIMITS["features"]:
                break
    if len(selected) < _LIMITS["minTraining"] + _LIMITS["minValidation"]:
        _fail("insufficient isolated features")
    return np.array([keypoints[i].pt for i in selected]), descriptors[selected]


def _matches(source, target) -> tuple[np.ndarray, np.ndarray]:
    source_points, source_descriptors = source
    target_points, target_descriptors = target
    matcher = cv2.BFMatcher(cv2.NORM_L2)

    def unique(left, right):
        return {
            pair[0].queryIdx: pair[0].trainIdx
            for pair in matcher.knnMatch(left, right, k=2)
            if len(pair) == 2 and pair[0].distance < _LIMITS["loweRatio"] * pair[1].distance
        }

    forward = unique(source_descriptors, target_descriptors)
    backward = unique(target_descriptors, source_descriptors)
    pairs = sorted((i, j) for i, j in forward.items() if backward.get(j) == i)
    if not pairs:
        _fail("no reciprocal unambiguous matches")
    return (
        np.array([source_points[i] for i, _ in pairs]),
        np.array([target_points[j] for _, j in pairs]),
    )


def _residual(matrix, source, target):
    return np.linalg.norm(source @ matrix[:, :2].T + matrix[:, 2] - target, axis=1)


def _fit(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(source) < _LIMITS["minTraining"]:
        _fail("insufficient training matches")
    design = np.column_stack((source, np.ones(len(source))))
    rng = np.random.default_rng(_LIMITS["ransacSeed"])
    best = None
    best_score = (-1, float("-inf"))
    for _ in range(_LIMITS["ransacIterations"]):
        sample = rng.choice(len(source), 3, replace=False)
        triangle = design[sample]
        if abs(np.linalg.det(triangle)) < 64:
            continue
        matrix = np.linalg.solve(triangle, target[sample]).T
        residual = _residual(matrix, source, target)
        inliers = residual <= _LIMITS["residualThreshold"]
        score = (int(inliers.sum()), -float(np.median(residual[inliers])))
        if score > best_score:
            best, best_score = inliers, score
    if best is None or int(best.sum()) < _LIMITS["minTraining"]:
        _fail("degenerate or unsupported fit")
    for _ in range(3):
        matrix = np.linalg.lstsq(design[best], target[best], rcond=None)[0].T
        best = _residual(matrix, source, target) <= _LIMITS["residualThreshold"]
        if int(best.sum()) < _LIMITS["minTraining"]:
            _fail("insufficient robust support")
    matrix = np.round(matrix, 9)
    best = _residual(matrix, source, target) <= _LIMITS["residualThreshold"]
    if int(best.sum()) < _LIMITS["minTraining"] or best.mean() < _LIMITS["minInlierRatio"]:
        _fail("ambiguous training geometry")
    return matrix, best


def _coverage(points: np.ndarray, grid: tuple[int, int]) -> dict[str, Any]:
    normalized = points / np.array(grid)
    quadrants = len(set(map(tuple, (normalized >= 0.5).astype(int))))
    span = np.ptp(normalized, axis=0)
    area = cv2.contourArea(cv2.convexHull(normalized.astype(np.float32)))
    if (
        quadrants < _LIMITS["minQuadrants"]
        or float(span.min()) < _LIMITS["minSpan"]
        or area < _LIMITS["minHullAreaRatio"]
    ):
        _fail("spatially concentrated support")
    return {"quadrants": quadrants, "span": span.round(6).tolist(), "hullAreaRatio": round(area, 6)}


def _bound(matrix: np.ndarray, grid: tuple[int, int]) -> None:
    if matrix.shape != (2, 3) or not np.isfinite(matrix).all():
        _fail("invalid affine geometry")
    linear = matrix[:, :2]
    singular = np.linalg.svd(linear, compute_uv=False)
    gram = linear.T @ linear
    angle = abs(float(np.arctan2(linear[1, 0], linear[0, 0])))
    corners = np.array([[0, 0], [grid[0], 0], [0, grid[1]], list(grid)], dtype=float)
    displacement = _residual(matrix, corners, corners)
    if (
        np.linalg.det(linear) <= 0
        or max(abs(singular - 1)) > _LIMITS["maxScaleDifference"]
        or abs(gram[0, 1]) > _LIMITS["maxShear"]
        or angle > _LIMITS["maxRotationRadians"]
        or max(abs(matrix[:, 2])) > _LIMITS["maxTranslationPixels"]
        or max(displacement) > _LIMITS["maxCornerDisplacementPixels"]
    ):
        _fail("transform exceeds global geometry bounds")


def _register_whole_frame(
    quality: bytes, provider_normalized: bytes, mask: bytes
) -> tuple[bytes, dict[str, Any]]:
    source = _read(quality, "RGB")
    height, width = source.shape[:2]
    grid = (width, height)
    if min(grid) < _LIMITS["minSide"]:
        _fail("input too small for independent geometry validation")
    provider = _read(provider_normalized, "RGB", grid)
    binary = _read(mask, "L", grid) > 0
    if not binary.any() or binary.all():
        _fail("mask must have editable and protected support")
    scale = min(1.0, _LIMITS["analysisLongestSide"] / max(grid))
    analysis_grid = (round(width * scale), round(height * scale))
    source_gray = cv2.resize(
        cv2.cvtColor(source, cv2.COLOR_RGB2GRAY), analysis_grid, interpolation=cv2.INTER_AREA
    )
    provider_gray = cv2.resize(
        cv2.cvtColor(provider, cv2.COLOR_RGB2GRAY), analysis_grid, interpolation=cv2.INTER_AREA
    )
    occupied = (
        cv2.resize(binary.astype(np.float32), analysis_grid, interpolation=cv2.INTER_AREA) > 0
    )
    support = np.uint8(~occupied)
    support[[0, -1], :] = 0
    support[:, [0, -1]] = 0
    distance = cv2.distanceTransform(support, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    source_points, target_points = _matches(
        _features(provider_gray, distance), _features(source_gray, distance)
    )
    # Whole spatial cells are held out, not fitted points relabelled as validation.
    cells = np.minimum((target_points / np.array(analysis_grid) * 8).astype(int), 7)
    held_out = (cells[:, 0] + 3 * cells[:, 1]) % 4 == 0
    train_source, train_target = source_points[~held_out], target_points[~held_out]
    valid_source, valid_target = source_points[held_out], target_points[held_out]
    if len(valid_source) < _LIMITS["minValidation"]:
        _fail("insufficient held-out matches")
    matrix, inliers = _fit(train_source, train_target)
    residual = _residual(matrix, valid_source, valid_target)
    valid_inliers = residual <= _LIMITS["residualThreshold"]
    if (
        valid_inliers.sum() < _LIMITS["minValidation"]
        or valid_inliers.mean() < _LIMITS["minInlierRatio"]
        or np.median(residual) > _LIMITS["maxValidationMedian"]
        or np.percentile(residual, 95) > _LIMITS["maxValidationP95"]
    ):
        _fail("held-out geometry disagrees")
    train_coverage = _coverage(train_target[inliers], analysis_grid)
    validation_coverage = _coverage(valid_target[valid_inliers], analysis_grid)
    scaling = np.diag([analysis_grid[0] / width, analysis_grid[1] / height, 1.0])
    full_matrix = np.round(
        (np.linalg.inv(scaling) @ np.vstack([matrix, [0, 0, 1]]) @ scaling)[:2], 9
    )
    _bound(full_matrix, grid)
    ys = np.flatnonzero(binary.any(axis=1))
    xs = np.flatnonzero(binary.any(axis=0))
    mask_corners = np.array(
        [[xs.min(), ys.min()], [xs.max(), ys.min()], [xs.min(), ys.max()], [xs.max(), ys.max()]]
    )
    inverse = cv2.invertAffineTransform(full_matrix)
    sampled = mask_corners @ inverse[:, :2].T + inverse[:, 2]
    margin = _LIMITS["interpolationBorderMargin"]
    if (sampled < margin).any() or (sampled > np.array(grid) - 1 - margin).any():
        _fail("editable support would sample a padded boundary")
    warped = cv2.warpAffine(
        provider,
        full_matrix,
        grid,
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    buffer = io.BytesIO()
    Image.fromarray(warped).save(buffer, format="PNG", optimize=False, compress_level=6)
    manifest = {
        "algorithm": "isolated-sift-reciprocal-numpy-ransac-affine-v1",
        "limits": dict(_LIMITS),
        "dependencies": {
            "opencv": cv2.__version__,
            "numpy": np.__version__,
            "pillow": PIL.__version__,
        },
        "qualitySha256": hashlib.sha256(quality).hexdigest(),
        "providerNormalizedSha256": hashlib.sha256(provider_normalized).hexdigest(),
        "maskSha256": hashlib.sha256(mask).hexdigest(),
        "analysisGrid": list(analysis_grid),
        "supportSha256": hashlib.sha256(
            np.uint8(distance >= _LIMITS["exclusionMargin"]).tobytes()
        ).hexdigest(),
        "partition": "quality-grid-8x8-(column+3*row)-mod4-zero-held-out-v1",
        "trainingMatchesSha256": _digest(
            np.column_stack([train_source, train_target]).round(6).tolist()
        ),
        "validationMatchesSha256": _digest(
            np.column_stack([valid_source, valid_target]).round(6).tolist()
        ),
        "trainingCount": len(train_source),
        "trainingInliers": int(inliers.sum()),
        "validationCount": len(valid_source),
        "validationInliers": int(valid_inliers.sum()),
        "validationMedianAnalysisPixels": round(float(np.median(residual)), 6),
        "validationP95AnalysisPixels": round(float(np.percentile(residual, 95)), 6),
        "trainingCoverage": train_coverage,
        "validationCoverage": validation_coverage,
        "providerToQualityAffine": full_matrix.tolist(),
        "warp": "opencv-inter-cubic-constant-zero-no-editable-boundary-sampling",
        "localWarp": False,
        "acceptance": "independent-visual-review-required",
    }
    return buffer.getvalue(), manifest


def register_whole_frame(
    quality: bytes, provider_normalized: bytes, mask: bytes
) -> tuple[bytes, dict[str, Any]]:
    try:
        return _register_whole_frame(quality, provider_normalized, mask)
    except (cv2.error, np.linalg.LinAlgError, OverflowError) as error:
        raise ProjectError("Cloud registration rejected: numerical operation failed") from error
