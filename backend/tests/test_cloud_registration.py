from __future__ import annotations

import io
import json
import subprocess
import sys

import cv2
import numpy as np
import pytest
from PIL import Image

from manga_localizer.services import cloud_full_page_clean_plates as cloud
from manga_localizer.services import cloud_registration as registration
from manga_localizer.services.projects import ProjectError


def _texture_inputs(shift: float = 2.0):
    rng = np.random.default_rng(217)
    canvas = np.full((768, 1024, 3), 245, dtype=np.uint8)
    for _ in range(1400):
        x, y = rng.integers([12, 12], [1012, 756])
        color = int(rng.integers(0, 200))
        cv2.circle(canvas, (int(x), int(y)), int(rng.integers(2, 8)), (color,) * 3, -1)
    for _ in range(200):
        x, y = rng.integers([12, 12], [990, 730])
        dx, dy = rng.integers(5, 30, 2)
        cv2.line(canvas, (int(x), int(y)), (int(x + dx), int(y + dy)), (30,) * 3, 2)
    mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
    mask[310:410, 460:540] = 255
    transform = np.array([[1.001, 0.0002, shift], [0.0001, 0.999, -1.0]])
    raw = cv2.warpAffine(
        canvas, transform, (1024, 768), flags=cv2.INTER_CUBIC, borderValue=(245,) * 3
    )
    raw[305:420, 455:545] = 220
    return tuple(cloud._png_bytes(Image.fromarray(a)) for a in (canvas, raw, mask)), transform


def test_registration_recovers_bounded_geometry_and_is_deterministic():
    (quality, raw, mask), transform = _texture_inputs()
    registered, manifest = registration.register_whole_frame(quality, raw, mask)
    repeated, repeated_manifest = registration.register_whole_frame(quality, raw, mask)
    assert registered == repeated
    assert manifest == repeated_manifest
    matrix = np.array(manifest["providerToQualityAffine"])
    expected = cv2.invertAffineTransform(transform)
    np.testing.assert_allclose(matrix[:, :2], expected[:, :2], atol=0.0006)
    np.testing.assert_allclose(matrix[:, 2], expected[:, 2], atol=0.4)
    assert manifest["validationInliers"] >= 20
    assert manifest["trainingMatchesSha256"] != manifest["validationMatchesSha256"]
    assert manifest["validationCoverage"]["quadrants"] >= 3
    normalized, _ = cloud._strict_mask_composite(quality, registered, mask)
    delta = cloud._delta_manifest(quality, normalized, mask)
    assert delta["outsideMaskChangedPixelCount"] == 0
    assert delta["insideMaskChangedPixelCount"] > 0


def test_registration_profile_preserves_default_bytes_and_manifest():
    (quality, raw, mask), _ = _texture_inputs()
    assert cloud._normalize_for_profile(
        raw, (1024, 768), quality=quality, mask=mask
    ) == cloud._normalize(raw, (1024, 768))
    result = cloud._normalize_for_profile(
        raw, (1024, 768), quality=quality, mask=mask, profile=registration.PROFILE
    )
    assert result[1]["profile"] == registration.PROFILE
    assert result[1]["contentAwareTransform"] is True
    assert result[1]["registration"]["localWarp"] is False


def test_default_profile_preserves_v1_upscaled_target_above_raw_pixel_limit(monkeypatch):
    monkeypatch.setattr(cloud, "MAX_RASTER_PIXELS", 100)
    raw = cloud._png_bytes(Image.new("RGB", (4, 4), "white"))
    expected = cloud._normalize(raw, (12, 12))
    assert cloud._normalize_for_profile(raw, (12, 12), quality=b"", mask=b"") == expected
    assert (
        cloud._normalize_for_profile(
            raw, (12, 12), quality=b"", mask=b"", profile=cloud.NORMALIZATION_PROFILE
        )
        == expected
    )
    with pytest.raises(ProjectError, match="target exceeds the raster pixel limit"):
        cloud._normalize_for_profile(
            raw, (12, 12), quality=b"", mask=b"", profile=registration.PROFILE
        )


@pytest.mark.parametrize("profile", ["unknown", None, {}, [], True])
def test_unknown_profile_never_falls_back(profile):
    with pytest.raises(ProjectError, match="Unknown"):
        cloud._normalize_for_profile(b"", (1024, 768), quality=b"", mask=b"", profile=profile)


def test_registration_rejects_large_drift():
    (quality, raw, mask), _ = _texture_inputs(30)
    with pytest.raises(ProjectError, match="bounds"):
        registration.register_whole_frame(quality, raw, mask)


def test_registration_rejects_blank_and_small_images():
    for size in [(64, 64), (512, 512)]:
        quality = cloud._png_bytes(Image.new("RGB", size, "white"))
        mask_image = Image.new("L", size)
        mask_image.paste(255, (20, 20, 40, 40))
        with pytest.raises(ProjectError, match=r"small|features"):
            registration.register_whole_frame(quality, quality, cloud._png_bytes(mask_image))


@pytest.mark.parametrize("value", [0, 255])
def test_registration_requires_editable_and_protected_support(value):
    (quality, raw, _), _ = _texture_inputs()
    with pytest.raises(ProjectError, match="support"):
        registration.register_whole_frame(
            quality, raw, cloud._png_bytes(Image.new("L", (1024, 768), value))
        )


def test_registration_fails_before_features_when_pixel_budget_exceeded(monkeypatch):
    monkeypatch.setitem(registration._LIMITS, "maxPixels", 100)
    monkeypatch.setattr(cv2, "SIFT_create", lambda **kwargs: pytest.fail("feature allocation"))
    payload = cloud._png_bytes(Image.new("RGB", (20, 20)))
    with pytest.raises(ProjectError, match="pixel limit"):
        registration.register_whole_frame(payload, payload, payload)


def test_registration_does_not_fit_held_out_matches(monkeypatch):
    (quality, raw, mask), _ = _texture_inputs()
    points = np.array(
        [(x, y) for y in range(80, 720, 60) for x in range(80, 1000, 60)], dtype=float
    )
    cells = (points / [1024, 768] * 8).astype(int)
    held_out = (cells[:, 0] + 3 * cells[:, 1]) % 4 == 0
    source = points.copy()
    source[held_out, 0] += 5
    monkeypatch.setattr(registration, "_matches", lambda *args: (source, points))
    with pytest.raises(ProjectError, match="held-out"):
        registration.register_whole_frame(quality, raw, mask)


def test_registration_rejects_spatially_degenerate_support():
    points = np.array([[x, y] for x in range(10, 100, 10) for y in range(10, 100, 10)])
    with pytest.raises(ProjectError, match="spatially"):
        registration._coverage(points, (1024, 768))


@pytest.mark.parametrize(
    "matrix",
    [
        [[1.1, 0, 0], [0, 1, 0]],
        [[1, 0.02, 0], [0, 1, 0]],
        [[-1, 0, 0], [0, 1, 0]],
        [[1, 0, 20], [0, 1, 0]],
        [[float("nan"), 0, 0], [0, 1, 0]],
    ],
)
def test_registration_rejects_unbounded_or_invalid_transform(matrix):
    with pytest.raises(ProjectError):
        registration._bound(np.array(matrix, dtype=float), (1024, 768))


def test_registration_rejects_editable_padding():
    (quality, _raw, mask), _ = _texture_inputs(0)
    mask_image = Image.open(io.BytesIO(mask))
    mask_image.paste(255, (0, 310, 4, 410))
    with pytest.raises(ProjectError, match="padded boundary"):
        registration.register_whole_frame(quality, quality, cloud._png_bytes(mask_image))


def test_registration_numerical_failure_is_sanitized(monkeypatch):
    def fail(*args):
        raise cv2.error("private path must not reach the CLI")

    monkeypatch.setattr(registration, "_register_whole_frame", fail)
    with pytest.raises(ProjectError, match="numerical operation failed") as error:
        registration.register_whole_frame(b"", b"", b"")
    assert "private path" not in str(error.value)


def test_registration_reproduces_in_independent_processes(tmp_path):
    inputs, _ = _texture_inputs()
    paths = [tmp_path / name for name in ("quality.png", "raw.png", "mask.png")]
    for path, content in zip(paths, inputs, strict=True):
        path.write_bytes(content)
    script = (
        "import hashlib,json,sys; from pathlib import Path; "
        "from manga_localizer.services.cloud_registration import register_whole_frame; "
        "image,evidence=register_whole_frame(*[Path(p).read_bytes() for p in sys.argv[1:]]); "
        "print(json.dumps([hashlib.sha256(image).hexdigest(),evidence],sort_keys=True))"
    )
    results = [
        subprocess.run(
            [sys.executable, "-c", script, *map(str, paths)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        for _ in range(2)
    ]
    assert json.loads(results[0]) == json.loads(results[1])
