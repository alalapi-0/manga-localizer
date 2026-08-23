from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging.lineart_inpaint import (
    ANOMALY_MASK_OUTSIDE,
    CANDIDATE_AI_MANGA_CLEAN,
    CANDIDATE_AI_OVERVIEW_LINEART,
    CANDIDATE_LAMA_COMPONENTS,
    CANDIDATE_LAMA_FULL_CONTEXT,
    CANDIDATE_LAMA_OVERVIEW_REFINE,
    CANDIDATE_PRIMARY,
    build_inpaint_candidates,
    candidate_metrics,
    choose_default_candidate,
    composite_mask_outside,
    is_effectively_grayscale,
    lineart_guided_inpaint,
    preserve_grayscale,
)
from manga_localizer.imaging.manga_ai_postprocess import (
    manga_overview_lineart_cleanup,
    manga_tone_cleanup,
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


def test_lama_only_pages_fall_back_to_primary_when_manga_cleanup_declines() -> None:
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
    assert choose_default_candidate(candidates, used_only_lama=True) == CANDIDATE_PRIMARY
    assert choose_default_candidate(candidates, used_only_lama=False) == CANDIDATE_PRIMARY
    for item in candidates:
        assert item["changedPixelsOutsideMask"] == 0


def test_confident_ai_manga_cleanup_is_crisp_mask_exact_and_preferred() -> None:
    height, width = 80, 120
    clean = np.zeros((height, width, 3), dtype=np.uint8)
    clean[:, 60:] = 255
    source_pixels = clean.copy()
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[18:62, 34:86] = 255
    source_pixels[mask > 0] = 128
    ai_pixels = clean.copy()
    transition = np.linspace(0, 255, 52, dtype=np.uint8)
    ai_pixels[18:62, 34:86] = transition[None, :, None]
    source = Image.fromarray(source_pixels, mode="RGB")
    ai = Image.fromarray(ai_pixels, mode="RGB")

    candidates = build_inpaint_candidates(
        source,
        mask,
        ai,
        full_context=ai,
    )

    ids = [item["id"] for item in candidates]
    assert ids == [
        "primary",
        "ai-manga-clean",
        "opencv-ns",
        "opencv-telea",
        "lineart-guided",
        "lama-full-context",
    ]
    cleaned = next(item for item in candidates if item["id"] == CANDIDATE_AI_MANGA_CLEAN)
    cleaned_pixels = np.asarray(cleaned["image"].convert("RGB"))
    assert np.array_equal(cleaned_pixels[mask == 0], source_pixels[mask == 0])
    assert set(np.unique(cleaned_pixels[mask > 0])) == {0, 255}
    assert float(np.mean(np.abs(cleaned_pixels.astype(np.int16) - clean))) < 2.0
    assert choose_default_candidate(candidates, used_only_lama=True) == CANDIDATE_AI_MANGA_CLEAN


def test_manga_cleanup_declines_color_and_one_tone_context() -> None:
    mask = np.zeros((64, 96), dtype=np.uint8)
    mask[16:48, 28:68] = 255
    colored = np.zeros((64, 96, 3), dtype=np.uint8)
    colored[:] = (20, 90, 160)
    colored_ai = colored.copy()
    assert manga_tone_cleanup(colored, colored_ai, mask) is None

    white = np.full((64, 96, 3), 255, dtype=np.uint8)
    gray_ai = white.copy()
    gray_ai[mask > 0] = 100
    assert manga_tone_cleanup(white, gray_ai, mask) is None


@pytest.mark.parametrize("render_scale", [1, 2, 3, 4])
def test_overview_lineart_cleanup_removes_gray_haze_and_keeps_ai_lines(
    render_scale: int,
) -> None:
    height = 96 * render_scale
    width = 128 * render_scale
    source = np.full((height, width, 3), 248, dtype=np.uint8)
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[20 * render_scale : 76 * render_scale, 24 * render_scale : 104 * render_scale] = 255
    overview = source.copy()
    overview[mask > 0] = 218
    overview[
        46 * render_scale : 50 * render_scale,
        24 * render_scale : 104 * render_scale,
    ] = 20

    cleaned = manga_overview_lineart_cleanup(
        source,
        overview,
        mask,
        render_scale=render_scale,
    )

    assert cleaned is not None
    assert np.array_equal(cleaned[mask == 0], source[mask == 0])
    assert np.all(cleaned[48 * render_scale, 40 * render_scale] == 0)
    assert np.all(cleaned[30 * render_scale, 40 * render_scale] == 255)


def test_overview_lineart_cleanup_ignores_source_inside_and_ai_outside() -> None:
    source = np.full((120, 160, 3), 248, dtype=np.uint8)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[24:96, 30:130] = 255
    overview = source.copy()
    overview[mask > 0] = 238
    overview[56:64, 30:130] = 12
    baseline = manga_overview_lineart_cleanup(source, overview, mask)
    assert baseline is not None

    poisoned_source = source.copy()
    poisoned_source[mask > 0] = (7, 201, 93)
    poisoned_overview = overview.copy()
    poisoned_overview[mask == 0] = (203, 17, 141)
    poisoned = manga_overview_lineart_cleanup(poisoned_source, poisoned_overview, mask)

    assert poisoned is not None
    assert np.array_equal(poisoned[mask > 0], baseline[mask > 0])
    assert np.array_equal(poisoned[mask == 0], poisoned_source[mask == 0])


@pytest.mark.parametrize("invalid_value", [1, 128, 254])
def test_overview_lineart_cleanup_declines_soft_or_ambiguous_support(
    invalid_value: int,
) -> None:
    source = np.full((96, 128, 3), 248, dtype=np.uint8)
    mask = np.zeros((96, 128), dtype=np.uint8)
    mask[20:76, 24:104] = invalid_value
    assert manga_overview_lineart_cleanup(source, source, mask) is None


def test_overview_lineart_candidate_is_optional_exact_and_not_auto_selected() -> None:
    source = np.full((120, 160, 3), 248, dtype=np.uint8)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[24:96, 30:130] = 255
    overview = source.copy()
    overview[mask > 0] = 238
    overview[56:64, 30:130] = 12
    primary = Image.fromarray(source.copy(), mode="RGB")

    candidates = build_inpaint_candidates(
        Image.fromarray(source, mode="RGB"),
        mask,
        primary,
        overview_base=Image.fromarray(overview, mode="RGB"),
    )

    ids = [item["id"] for item in candidates]
    assert CANDIDATE_AI_OVERVIEW_LINEART in ids
    candidate = next(item for item in candidates if item["id"] == CANDIDATE_AI_OVERVIEW_LINEART)
    pixels = np.asarray(candidate["image"].convert("RGB"))
    assert np.array_equal(pixels[mask == 0], source[mask == 0])
    assert set(np.unique(pixels[mask > 0])) == {0, 255}
    assert choose_default_candidate(candidates, used_only_lama=True) == CANDIDATE_PRIMARY


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


def test_component_context_lama_candidate_is_optional_exact_and_not_auto_selected() -> None:
    source, mask = _broken_line_page()
    primary = Image.new("RGB", source.size, (230, 230, 230))
    component_pixels = np.asarray(source).copy()
    component_pixels[:] = (77, 77, 77)

    candidates = build_inpaint_candidates(
        source,
        mask,
        primary,
        component_context=Image.fromarray(component_pixels),
    )

    assert [item["id"] for item in candidates] == [
        "primary",
        "lama-components",
        "opencv-ns",
        "opencv-telea",
        "lineart-guided",
    ]
    candidate = next(item for item in candidates if item["id"] == CANDIDATE_LAMA_COMPONENTS)
    pixels = np.asarray(candidate["image"].convert("RGB"))
    assert np.array_equal(pixels[mask == 0], np.asarray(source)[mask == 0])
    assert np.all(pixels[mask > 0] == 77)
    assert candidate["changedPixelsOutsideMask"] == 0
    assert choose_default_candidate(candidates, used_only_lama=True) == CANDIDATE_PRIMARY


def test_overview_refine_lama_candidate_is_optional_exact_and_not_auto_selected() -> None:
    source, mask = _broken_line_page()
    primary = Image.new("RGB", source.size, (230, 230, 230))
    overview_pixels = np.asarray(source).copy()
    overview_pixels[:] = (63, 63, 63)

    candidates = build_inpaint_candidates(
        source,
        mask,
        primary,
        overview_refine=Image.fromarray(overview_pixels),
    )

    assert [item["id"] for item in candidates] == [
        "primary",
        "lama-overview-refine",
        "opencv-ns",
        "opencv-telea",
        "lineart-guided",
    ]
    candidate = next(item for item in candidates if item["id"] == CANDIDATE_LAMA_OVERVIEW_REFINE)
    pixels = np.asarray(candidate["image"].convert("RGB"))
    assert np.array_equal(pixels[mask == 0], np.asarray(source)[mask == 0])
    assert np.all(pixels[mask > 0] == 63)
    assert candidate["changedPixelsOutsideMask"] == 0
    assert choose_default_candidate(candidates, used_only_lama=True) == CANDIDATE_PRIMARY


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
