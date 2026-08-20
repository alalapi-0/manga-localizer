from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from manga_localizer.imaging.lineart_inpaint import (
    ANOMALY_MASK_OUTSIDE,
    CANDIDATE_LAMA_FULL_CONTEXT,
    CANDIDATE_LINEART,
    CANDIDATE_PRIMARY,
    build_inpaint_candidates,
    candidate_metrics,
    choose_default_candidate,
    composite_mask_outside,
    is_effectively_grayscale,
    lineart_guided_inpaint,
    preserve_grayscale,
)
from manga_localizer.providers.inpainting_lama import LaMaONNXInpaintingProvider

from .test_inpainting_lama import FakeSession, _configured_provider


def _broken_line_page() -> tuple[Image.Image, np.ndarray]:
    pixels = np.full((64, 96, 3), 245, dtype=np.uint8)
    pixels[30:34, :] = 12
    pixels[30:34, 36:60] = 245
    mask = np.zeros((64, 96), dtype=np.uint8)
    mask[22:42, 34:62] = 255
    return Image.fromarray(pixels, mode="RGB"), mask


def test_lineart_guided_keeps_mask_outside_exact_and_continues_a_broken_stroke() -> None:
    source, mask = _broken_line_page()
    result = lineart_guided_inpaint(source, mask)
    source_rgb = np.asarray(source)
    result_rgb = np.asarray(result.convert("RGB"))
    assert np.array_equal(result_rgb[mask == 0], source_rgb[mask == 0])
    gap = result_rgb[30:34, 40:56]
    # The restored gap should continue the stroke instead of staying paper-white.
    assert float(np.min(gap)) < 160
    assert float(np.mean(gap)) < 230


def test_grayscale_source_does_not_gain_chroma() -> None:
    source = Image.new("L", (24, 24), 200)
    mask = np.zeros((24, 24), dtype=np.uint8)
    mask[6:18, 6:18] = 255
    colored = Image.new("RGB", (24, 24), (12, 200, 40))
    restored = preserve_grayscale(colored, source)
    assert is_effectively_grayscale(restored)
    composited = composite_mask_outside(source.convert("RGB"), restored, mask)
    pixels = np.asarray(composited.convert("RGB"))
    assert np.array_equal(pixels[mask == 0], np.asarray(source.convert("RGB"))[mask == 0])
    inside = pixels[mask > 0]
    assert np.max(np.max(inside, axis=1) - np.min(inside, axis=1)) <= 1


def test_candidate_metrics_flag_mask_outside_changes() -> None:
    source = Image.new("RGB", (12, 12), (10, 10, 10))
    mutated = np.asarray(source).copy()
    mutated[0, 0] = (99, 99, 99)
    mask = np.zeros((12, 12), dtype=np.uint8)
    mask[4:8, 4:8] = 255
    metrics = candidate_metrics(source, Image.fromarray(mutated), mask)
    assert metrics["changedPixelsOutsideMask"] == 1
    assert ANOMALY_MASK_OUTSIDE in metrics["anomalies"]


def test_lama_only_pages_default_to_lineart_guided_candidates() -> None:
    source, mask = _broken_line_page()
    primary = Image.new("RGB", source.size, (230, 230, 230))
    candidates = build_inpaint_candidates(source, mask, primary)
    ids = [item["id"] for item in candidates]
    assert ids == [
        "primary",
        "opencv-ns",
        "opencv-telea",
        "lineart-guided",
    ]
    assert choose_default_candidate(candidates, used_only_lama=True) == CANDIDATE_LINEART
    assert choose_default_candidate(candidates, used_only_lama=False) == CANDIDATE_PRIMARY
    for item in candidates:
        assert item["changedPixelsOutsideMask"] == 0


def test_full_context_lama_candidate_is_optional_and_keeps_mask_outside_exact() -> None:
    source, mask = _broken_line_page()
    primary = Image.new("RGB", source.size, (230, 230, 230))
    generated = np.asarray(source).copy()
    generated[:] = (91, 91, 91)

    candidates = build_inpaint_candidates(
        source,
        mask,
        primary,
        full_context=Image.fromarray(generated),
    )

    ids = [item["id"] for item in candidates]
    assert ids == [
        "primary",
        "opencv-ns",
        "opencv-telea",
        "lineart-guided",
        "lama-full-context",
    ]
    candidate = next(item for item in candidates if item["id"] == CANDIDATE_LAMA_FULL_CONTEXT)
    pixels = np.asarray(candidate["image"].convert("RGB"))
    assert np.array_equal(pixels[mask == 0], np.asarray(source)[mask == 0])
    assert np.all(pixels[mask > 0] == 91)
    assert candidate["changedPixelsOutsideMask"] == 0


def test_lama_preserves_grayscale_manga_pages(tmp_path: Path) -> None:
    session = FakeSession(color_bgr=(12, 34, 220))
    provider, _ = _configured_provider(tmp_path, session, feather=0)
    source = Image.new("L", (36, 48), 80)
    mask = np.zeros((48, 36), dtype=np.uint8)
    mask[10:22, 12:24] = 255
    result = provider.inpaint(source, mask)
    pixels = np.asarray(result.convert("RGB"))
    original = np.asarray(source.convert("RGB"))
    assert np.array_equal(pixels[mask == 0], original[mask == 0])
    inside = pixels[mask > 0]
    assert np.max(np.max(inside, axis=1) - np.min(inside, axis=1)) <= 1
    assert provider.get_capabilities()["preservesGrayscale"] is True
    missing = LaMaONNXInpaintingProvider(tmp_path / "missing.onnx")
    assert missing.get_capabilities()["preservesGrayscale"] is True
