from __future__ import annotations

import cv2
import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging import OpenCVInpaintingProvider, create_mask, inpaint, typeset_image
from manga_localizer.imaging.boundary_inpaint import directional_background_consensus
from manga_localizer.imaging.typesetting import (
    cluster_fragment_regions,
    expand_typeset_region_ids,
    font_capabilities,
    overflow_region_ids,
    restore_clean_region_boxes,
    typeset_overflow_from_status,
    verticalize_punctuation,
)


def test_opencv_mask_inpaint_and_exact_provider_interface() -> None:
    source = Image.new("RGB", (120, 100), "white")
    pixels = np.asarray(source).copy()
    pixels[35:65, 45:75] = (0, 0, 0)
    source = Image.fromarray(pixels)
    regions = [{"x": 45, "y": 35, "width": 30, "height": 30, "rotation": 20}]

    mask = create_mask(source, regions, padding=2, dilation=1)
    assert mask.shape == (100, 120)
    assert mask.dtype == np.uint8
    assert mask[50, 60] == 255
    repaired = inpaint(source, mask, radius=3, method="telea")
    assert repaired.size == source.size
    assert np.asarray(repaired)[50, 60].mean() > np.asarray(source)[50, 60].mean()

    provider = OpenCVInpaintingProvider()
    provider_mask = provider.create_mask(source, regions)
    assert provider_mask.shape == mask.shape
    assert provider.inpaint(source, provider_mask).size == source.size
    solid = provider.inpaint(source, provider_mask, method="solid", fill_color="#ef233c")
    assert np.asarray(solid)[50, 60].tolist() == [239, 35, 60]
    assert provider.health_check()["available"] is True
    assert "telea" in provider.get_capabilities()["methods"]
    assert "solid" in provider.get_capabilities()["methods"]
    assert "screentone" in provider.get_capabilities()["methods"]
    assert provider.get_capabilities()["maskModes"] == ["text", "region", "manual"]
    assert provider.get_capabilities()["textPolarities"] == ["auto", "dark", "light"]


def test_screentone_inpaint_restores_a_periodic_plate_and_preserves_soft_mask_semantics() -> None:
    height, width = 180, 240
    clean = np.full((height, width, 3), 245, dtype=np.uint8)
    for y in range(4, height, 11):
        for x in range(6, width, 11):
            cv2.circle(clean, (x, y), 2, (24, 24, 24), -1, lineType=cv2.LINE_AA)

    contaminated = clean.copy()
    outline = np.zeros((height, width), dtype=np.uint8)
    core = np.zeros((height, width), dtype=np.uint8)
    cv2.putText(
        outline,
        "AB",
        (55, 112),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.2,
        255,
        11,
        cv2.LINE_AA,
    )
    cv2.putText(
        core,
        "AB",
        (55, 112),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.2,
        255,
        5,
        cv2.LINE_AA,
    )
    contaminated[outline > 0] = 20
    contaminated[core > 0] = 250
    hard_mask = cv2.dilate(
        outline,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )
    soft_mask = cv2.GaussianBlur(hard_mask, (7, 7), 1.0)
    alpha = soft_mask.astype(np.float32)[..., np.newaxis] / 255.0
    expected = np.rint(contaminated * (1.0 - alpha) + clean * alpha).astype(np.uint8)

    source_alpha = np.arange(height * width, dtype=np.uint8).reshape(height, width)
    source = Image.fromarray(np.dstack((contaminated, source_alpha)), mode="RGBA")
    repaired = inpaint(source, soft_mask, method="screentone")
    repaired_array = np.asarray(repaired)
    target = soft_mask > 0

    assert repaired.mode == "RGBA"
    assert np.array_equal(repaired_array[..., 3], source_alpha)
    assert np.array_equal(repaired_array[~target, :3], contaminated[~target])
    assert (
        np.mean(np.abs(repaired_array[target, :3].astype(int) - expected[target].astype(int))) < 1
    )
    assert (
        np.percentile(
            np.abs(repaired_array[target, :3].astype(int) - expected[target].astype(int)),
            95,
        )
        <= 1
    )


def test_screentone_inpaint_fails_closed_for_nonperiodic_context() -> None:
    _rows, columns = np.indices((140, 180))
    gradient = np.rint(30 + columns[..., np.newaxis] * np.array([0.8, 0.8, 0.8])).clip(0, 255)
    source = Image.fromarray(gradient.astype(np.uint8), mode="RGB")
    mask = np.zeros((140, 180), dtype=np.uint8)
    mask[45:95, 60:120] = 255

    with pytest.raises(ValueError, match="Screentone repair"):
        inpaint(source, mask, method="screentone")


def test_screentone_inpaint_reconstructs_a_dark_field_boundary_across_the_mask() -> None:
    height, width = 190, 260
    clean = np.full((height, width, 3), 245, dtype=np.uint8)
    for y in range(4, height, 11):
        for x in range(6, width, 11):
            cv2.circle(clean, (x, y), 2, (24, 24, 24), -1, lineType=cv2.LINE_AA)
    rows, columns = np.indices((height, width))
    dark_field = rows < (48 + 0.55 * columns)
    clean[dark_field] = 12

    contaminated = clean.copy()
    glyph = np.zeros((height, width), dtype=np.uint8)
    cv2.putText(
        glyph,
        "AB",
        (72, 145),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.4,
        255,
        12,
        cv2.LINE_AA,
    )
    contaminated[glyph > 0] = 250
    mask = cv2.dilate(glyph, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))

    repaired = np.asarray(inpaint(contaminated, mask, method="screentone").convert("RGB"))
    target = mask > 0
    absolute_error = np.abs(repaired.astype(int) - clean.astype(int))

    assert np.array_equal(repaired[~target], contaminated[~target])
    assert float(np.mean(absolute_error[target])) < 8
    assert float(np.percentile(absolute_error[target], 95)) < 30
    assert float(np.mean(repaired[target & dark_field])) < 30
    assert float(np.mean(repaired[target & ~dark_field])) > 160


def test_screentone_inpaint_does_not_stamp_wide_mask_bands_across_a_dark_field() -> None:
    height, width = 190, 280
    clean = np.full((height, width, 3), 245, dtype=np.uint8)
    for y in range(4, height, 11):
        for x in range(6, width, 11):
            cv2.circle(clean, (x, y), 2, (24, 24, 24), -1, lineType=cv2.LINE_AA)
    rows, columns = np.indices((height, width))
    dark_field = rows < (42 + 0.58 * columns)
    clean[dark_field] = 12

    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.line(mask, (95, 16), (95, 178), 255, 48, cv2.LINE_8)
    cv2.line(mask, (143, 16), (143, 178), 255, 48, cv2.LINE_8)
    contaminated = clean.copy()
    contaminated[mask > 0] = 250

    repaired = np.asarray(inpaint(contaminated, mask, method="screentone").convert("RGB"))
    target = mask > 0
    absolute_error = np.abs(repaired.astype(int) - clean.astype(int))
    true_light = target & ~dark_field & (np.mean(clean, axis=2) >= 128)
    false_black = true_light & (np.mean(repaired, axis=2) < 128)

    expected_boundary = 42 + 0.58 * np.arange(width)
    recovered_dark = np.mean(repaired, axis=2) < 128
    boundary_errors = []
    for column in np.flatnonzero(np.any(target, axis=0)):
        target_rows = np.flatnonzero(target[:, column])
        if target_rows.size == 0:
            continue
        first_light = np.flatnonzero(~recovered_dark[target_rows, column])
        recovered_boundary = (
            float(target_rows[first_light[0]]) if first_light.size else float(target_rows.max() + 1)
        )
        boundary_errors.append(abs(recovered_boundary - expected_boundary[column]))

    assert set(np.unique(mask)) <= {0, 255}
    assert np.array_equal(repaired[~target], contaminated[~target])
    assert float(np.mean(absolute_error[target])) < 8
    assert float(np.percentile(absolute_error[target], 95)) < 30
    assert float(np.mean(repaired[target & dark_field])) < 30
    assert float(np.mean(repaired[target & ~dark_field])) > 160
    assert float(np.mean(false_black[true_light])) < 0.01
    assert float(np.percentile(boundary_errors, 95)) < 3.0


def test_screentone_inpaint_rejects_an_unverified_curved_structural_boundary() -> None:
    height, width = 190, 280
    source = np.full((height, width, 3), 245, dtype=np.uint8)
    for y in range(4, height, 11):
        for x in range(6, width, 11):
            cv2.circle(source, (x, y), 2, (24, 24, 24), -1, lineType=cv2.LINE_AA)
    rows, columns = np.indices((height, width))
    curved_field = rows < (55 + 0.0028 * (columns - 140) ** 2)
    source[curved_field] = 12

    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.line(mask, (95, 16), (95, 178), 255, 48, cv2.LINE_8)
    cv2.line(mask, (143, 16), (143, 178), 255, 48, cv2.LINE_8)
    source[mask > 0] = 250

    with pytest.raises(
        ValueError,
        match="Screentone repair could not verify the structural field boundary",
    ):
        inpaint(source, mask, method="screentone")


def test_text_mask_keeps_sparse_and_dense_strokes_without_filling_geometry() -> None:
    sparse = np.full((100, 140, 3), 255, dtype=np.uint8)
    sparse[30:70, 65:72] = 0
    sparse_region = [{"x": 40, "y": 20, "width": 60, "height": 60}]
    sparse_mask = create_mask(
        Image.fromarray(sparse),
        sparse_region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )
    sparse_geometry = create_mask(
        Image.fromarray(sparse),
        sparse_region,
        padding=0,
        dilation=0,
        mask_mode="region",
    )
    assert 0 < np.count_nonzero(sparse_mask) < np.count_nonzero(sparse_geometry)

    dense = np.full((100, 140, 3), 245, dtype=np.uint8)
    rows, columns = np.indices((60, 60))
    outlined_pattern = ((columns // 3 + rows // 3) % 2) * 230 + 15
    dense[20:80, 40:100] = outlined_pattern[..., np.newaxis]
    dense_mask = create_mask(
        Image.fromarray(dense),
        sparse_region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )
    assert 0 < np.count_nonzero(dense_mask) < np.count_nonzero(sparse_geometry)
    assert np.count_nonzero(dense_mask) > np.count_nonzero(sparse_mask)


@pytest.mark.parametrize(("background", "ink"), ((255, 0), (0, 255)))
def test_text_mask_rejects_border_connected_artwork_and_keeps_inner_glyphs(
    background: int,
    ink: int,
) -> None:
    pixels = np.full((100, 140, 3), background, dtype=np.uint8)
    pixels[46:54, 8:56] = ink
    pixels[30:39, 65:75] = ink
    pixels[39:64, 65:69] = ink
    pixels[55:64, 65:75] = ink
    region = [{"x": 30, "y": 20, "width": 70, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )

    assert np.count_nonzero(mask[46:54, 30:48]) == 0
    assert np.count_nonzero(mask[46:54, 30:56]) <= 64
    assert np.count_nonzero(mask[30:64, 65:75]) > 0


def test_text_mask_guard_keeps_a_glyph_touching_the_original_region_boundary() -> None:
    pixels = np.full((100, 140, 3), 255, dtype=np.uint8)
    pixels[34:66, 29:36] = 0
    region = [{"x": 30, "y": 20, "width": 70, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )

    assert np.count_nonzero(mask[34:66, 30:36]) > 0
    assert np.count_nonzero(mask[:, :30]) == 0


def test_text_mask_does_not_treat_a_clipped_page_edge_as_external_artwork() -> None:
    pixels = np.full((80, 100, 3), 255, dtype=np.uint8)
    pixels[25:55, 0:7] = 0
    region = [{"x": 0, "y": 15, "width": 50, "height": 50}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )

    assert np.count_nonzero(mask[25:55, 0:7]) > 0


def test_text_mask_rescues_an_outlined_glyph_without_following_border_artwork() -> None:
    pixels = np.zeros((100, 140, 3), dtype=np.uint8)
    pixels[:, 50:80] = 255
    pixels[34:66, 61:70] = 0
    pixels[46:54, 5:36] = 255
    region = [{"x": 40, "y": 20, "width": 60, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )

    assert np.count_nonzero(mask[34:66, 61:70]) > 0
    assert np.count_nonzero(mask[34:66, 55:76]) > np.count_nonzero(mask[34:66, 61:70])
    assert np.count_nonzero(mask[46:54, 40:50]) == 0


def test_auto_text_mask_completes_an_outline_across_a_light_dark_boundary() -> None:
    pixels = np.full((120, 180, 3), 245, dtype=np.uint8)
    pixels[:, 90:] = 12
    background = pixels.copy()
    cv2.putText(
        pixels,
        "AB",
        (45, 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.35,
        (250, 250, 250),
        7,
        cv2.LINE_AA,
    )
    cv2.putText(
        pixels,
        "AB",
        (45, 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.35,
        (5, 5, 5),
        2,
        cv2.LINE_AA,
    )
    region = [{"x": 35, "y": 30, "width": 100, "height": 60}]
    image = Image.fromarray(pixels)
    auto = create_mask(
        image,
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
    )
    dark = create_mask(
        image,
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
        text_polarity="dark",
    )
    light = create_mask(
        image,
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
        text_polarity="light",
    )
    visible_overlay = (
        np.abs(pixels[..., 0].astype(np.int16) - background[..., 0].astype(np.int16)) >= 20
    )
    geometry = (
        create_mask(
            image,
            region,
            padding=0,
            dilation=0,
            mask_mode="region",
        )
        > 0
    )
    explicit_union = np.maximum(dark, light)

    assert np.mean(auto[visible_overlay] > 0) >= 0.99
    assert np.mean(auto[geometry & ~visible_overlay] > 0) <= 0.02
    assert np.count_nonzero(auto[visible_overlay]) > np.count_nonzero(
        explicit_union[visible_overlay]
    )
    assert np.count_nonzero(auto[:, :35]) == 0
    assert np.count_nonzero(auto[:, 135:]) == 0


def test_directional_background_refinement_declines_an_oversized_dense_query() -> None:
    pixels = np.full((129, 129), 240, dtype=np.uint8)
    query = np.ones((129, 129), dtype=bool)

    prediction, confidence = directional_background_consensus(
        pixels,
        blocked=np.zeros_like(query),
        query=query,
        max_distance=10_000,
    )

    assert not np.any(prediction)
    assert not np.any(confidence)


@pytest.mark.parametrize(
    ("text_polarity", "plate", "glyph"),
    (("dark", 255, 0), ("light", 0, 255)),
)
def test_explicit_text_polarity_removes_only_the_glyph_and_keeps_its_plate(
    text_polarity: str,
    plate: int,
    glyph: int,
) -> None:
    pixels = np.full((100, 140, 3), 128, dtype=np.uint8)
    pixels[28:72, 55:85] = plate
    pixels[35:65, 66:74] = glyph
    pixels[46:54, 5:42] = glyph
    region = [{"x": 30, "y": 20, "width": 80, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
        text_polarity=text_polarity,
    )

    assert np.count_nonzero(mask[35:65, 66:74]) > 0
    assert np.count_nonzero(mask[28:35, 55:85]) == 0
    assert np.count_nonzero(mask[35:65, 55:66]) == 0
    assert np.count_nonzero(mask[35:65, 74:85]) == 0
    assert np.count_nonzero(mask[46:54, 30:40]) == 0


def test_explicit_text_polarity_never_uses_the_dense_full_region_fallback() -> None:
    pixels = np.full((100, 140, 3), 245, dtype=np.uint8)
    rows, columns = np.indices((60, 60))
    pixels[20:80, 40:100] = (((columns // 3 + rows // 3) % 2) * 230 + 15)[..., np.newaxis]
    region = [{"x": 40, "y": 20, "width": 60, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
        text_polarity="dark",
    )
    geometry = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="region",
    )

    assert 0 < np.count_nonzero(mask) < np.count_nonzero(geometry)


@pytest.mark.parametrize(
    ("text_polarity", "background", "foreground"),
    (("dark", 245, 15), ("light", 15, 245)),
)
def test_explicit_text_polarity_fails_closed_before_expansion_fills_geometry(
    text_polarity: str,
    background: int,
    foreground: int,
) -> None:
    pixels = np.full((100, 140, 3), background, dtype=np.uint8)
    rows, columns = np.indices((60, 60))
    texture = np.where((columns // 3 + rows // 3) % 2, foreground, background)
    pixels[20:80, 40:100] = texture[..., np.newaxis]
    region = [{"x": 40, "y": 20, "width": 60, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=3,
        dilation=2,
        feather=1,
        mask_mode="text",
        text_polarity=text_polarity,
    )

    assert np.count_nonzero(mask) == 0


@pytest.mark.parametrize(
    ("text_polarity", "background", "foreground"),
    (("dark", 245, 15), ("light", 15, 245)),
)
def test_explicit_text_polarity_fails_closed_when_sparse_stripes_merge_under_expansion(
    text_polarity: str,
    background: int,
    foreground: int,
) -> None:
    pixels = np.full((100, 140, 3), background, dtype=np.uint8)
    for column in range(40, 100, 10):
        pixels[20:80, column] = foreground
    region = [{"x": 40, "y": 20, "width": 60, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=3,
        dilation=2,
        feather=1,
        mask_mode="text",
        text_polarity=text_polarity,
    )

    assert np.count_nonzero(mask) == 0


@pytest.mark.parametrize(
    ("text_polarity", "background", "foreground"),
    (("dark", 245, 15), ("light", 15, 245)),
)
def test_explicit_text_polarity_keeps_a_narrow_glyph_with_production_expansion(
    text_polarity: str,
    background: int,
    foreground: int,
) -> None:
    pixels = np.full((100, 140, 3), background, dtype=np.uint8)
    pixels[30:70, 66:74] = foreground
    region = [{"x": 40, "y": 20, "width": 60, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=3,
        dilation=2,
        feather=1,
        mask_mode="text",
        text_polarity=text_polarity,
    )
    geometry = create_mask(
        Image.fromarray(pixels),
        region,
        padding=6,
        dilation=0,
        mask_mode="region",
    )

    assert np.count_nonzero(mask[30:70, 66:74]) > 0
    assert 0 < np.count_nonzero(mask) < np.count_nonzero(geometry) * 0.8


@pytest.mark.parametrize(
    ("text_polarity", "background", "foreground", "outline"),
    (("light", 128, 245, 10), ("dark", 128, 10, 245)),
)
def test_explicit_text_polarity_does_not_rescue_boundary_art_with_opposite_support(
    text_polarity: str,
    background: int,
    foreground: int,
    outline: int,
) -> None:
    pixels = np.full((100, 140, 3), background, dtype=np.uint8)
    pixels[40:60, 0:56] = foreground
    pixels[44:56, 0:56] = outline
    pixels[30:65, 70:78] = foreground
    region = [{"x": 40, "y": 20, "width": 60, "height": 60}]

    mask = create_mask(
        Image.fromarray(pixels),
        region,
        padding=0,
        dilation=0,
        mask_mode="text",
        text_polarity=text_polarity,
    )

    assert np.count_nonzero(mask[40:60, 40:56]) == 0
    assert np.count_nonzero(mask[30:65, 70:78]) > 0


def test_region_text_polarity_overrides_the_create_mask_default_and_is_validated() -> None:
    pixels = np.full((80, 100, 3), 128, dtype=np.uint8)
    pixels[20:60, 35:65] = 255
    pixels[28:52, 46:54] = 0
    region = {
        "x": 25,
        "y": 15,
        "width": 50,
        "height": 50,
        "textPolarity": "dark",
    }

    mask = create_mask(
        Image.fromarray(pixels),
        [region],
        padding=0,
        dilation=0,
        mask_mode="text",
        text_polarity="light",
    )

    assert np.count_nonzero(mask[28:52, 46:54]) > 0
    assert np.count_nonzero(mask[20:28, 35:65]) == 0
    with pytest.raises(ValueError, match="Text polarity"):
        create_mask(Image.fromarray(pixels), [region], text_polarity="mixed")


def test_full_region_mask_ignores_a_stale_detector_polygon() -> None:
    image = Image.new("RGB", (100, 80), "white")
    region = {
        "x": 10,
        "y": 12,
        "width": 70,
        "height": 50,
        "maskPolygon": [[10, 12], [25, 12], [25, 27], [10, 27]],
        "maskMode": "region",
    }

    mask = create_mask(image, [region], padding=0, dilation=0)

    assert mask[20, 20] == 255
    assert mask[50, 70] == 255


def test_manual_mask_mode_uses_only_persisted_add_and_erase_strokes() -> None:
    image = Image.new("RGB", (100, 80), "white")
    region = {
        "x": 2,
        "y": 2,
        "width": 96,
        "height": 76,
        "maskMode": "manual",
        "maskPolygon": [[2, 2], [98, 2], [98, 78], [2, 78]],
        "padding": 40,
        "maskEdits": {
            "version": 1,
            "strokes": [
                {"mode": "add", "radius": 3, "points": [[20, 20], [40, 20]]},
                {"mode": "erase", "radius": 2, "points": [[30, 20]]},
            ],
        },
    }

    mask = create_mask(image, [region], padding=40, dilation=20, feather=20)

    assert set(np.unique(mask)) <= {0, 255}
    assert mask[20, 20] == 255
    assert mask[20, 30] == 0
    assert mask[20, 40] == 255
    assert mask[12, 30] == 0
    assert mask[70, 90] == 0


@pytest.mark.parametrize("render_scale", (2, 3, 4))
def test_scaled_render_limits_accept_canonical_mask_maxima(render_scale: int) -> None:
    maximum_padding = 512 * render_scale
    maximum_dilation = 128 * render_scale
    maximum_feather = 128 * render_scale
    maximum_stroke_radius = 512 * render_scale
    region = {
        "x": 2,
        "y": 2,
        "width": 12,
        "height": 12,
        "maskMode": "manual",
        "maskEdits": {
            "version": 1,
            "strokes": [
                {
                    "mode": "add",
                    "radius": maximum_stroke_radius,
                    "points": [[8, 8]],
                }
            ],
        },
    }

    mask = create_mask(
        (16, 16),
        [region],
        padding=maximum_padding,
        dilation=maximum_dilation,
        feather=maximum_feather,
        render_scale=render_scale,
    )

    assert np.all(mask == 255)


@pytest.mark.parametrize(
    ("option", "value", "message"),
    (
        ("padding", 2049, "padding"),
        ("dilation", 513, "dilation"),
        ("feather", 513, "feather"),
    ),
)
def test_scaled_render_mask_limits_reject_values_above_four_x_maxima(
    option: str,
    value: int,
    message: str,
) -> None:
    options = {"padding": 0, "dilation": 0, "feather": 0, option: value}
    with pytest.raises(ValueError, match=message):
        create_mask((16, 16), [], render_scale=4, **options)

    oversized_stroke = {
        "x": 1,
        "y": 1,
        "width": 8,
        "height": 8,
        "maskMode": "manual",
        "maskEdits": {
            "version": 1,
            "strokes": [{"mode": "add", "radius": 2049, "points": [[4, 4]]}],
        },
    }
    with pytest.raises(ValueError, match="stroke radius"):
        create_mask((16, 16), [oversized_stroke], render_scale=4)


def test_scaled_render_opencv_radius_is_scale_aware_without_clamping() -> None:
    source = Image.new("RGB", (8, 8), "white")
    mask = Image.new("L", source.size, 0)

    result = inpaint(source, mask, method="solid", radius=1024, render_scale=4)

    assert np.array_equal(np.asarray(result), np.asarray(source))
    with pytest.raises(ValueError, match="1024"):
        inpaint(source, mask, method="solid", radius=1024.1, render_scale=4)
    with pytest.raises(ValueError, match="render_scale"):
        inpaint(source, mask, method="solid", radius=3, render_scale=5)


@pytest.mark.parametrize(
    "mask_edits",
    (
        None,
        {"version": 1, "strokes": []},
        {
            "version": 1,
            "strokes": [{"mode": "erase", "radius": 4, "points": [[30, 20]]}],
        },
    ),
)
def test_manual_mask_mode_fails_closed_without_add_strokes(mask_edits: dict | None) -> None:
    region: dict[str, object] = {
        "x": 5,
        "y": 5,
        "width": 80,
        "height": 60,
        "maskMode": "manual",
    }
    if mask_edits is not None:
        region["maskEdits"] = mask_edits

    mask = create_mask((100, 80), [region], padding=30, dilation=20, feather=20)

    assert not np.any(mask)


def test_versioned_mask_strokes_add_then_erase_in_canonical_coordinates() -> None:
    image = Image.new("RGB", (100, 80), "white")
    region = {
        "x": 35,
        "y": 25,
        "width": 30,
        "height": 30,
        "maskMode": "region",
        "maskEdits": {
            "version": 1,
            "strokes": [
                {"mode": "add", "radius": 3, "points": [[8, 8], [25, 8]]},
                {"mode": "erase", "radius": 5, "points": [[50, 40]]},
            ],
        },
    }

    mask = create_mask(image, [region], padding=0, dilation=0, feather=0)

    assert mask[8, 15] == 255
    assert mask[40, 50] == 0
    assert mask[28, 38] == 255
    assert mask[75, 95] == 0


def test_manual_mask_strokes_apply_in_persisted_order() -> None:
    def render(first: str, second: str) -> np.ndarray:
        return create_mask(
            (40, 40),
            [
                {
                    "x": 0,
                    "y": 0,
                    "width": 40,
                    "height": 40,
                    "maskMode": "manual",
                    "maskEdits": {
                        "version": 1,
                        "strokes": [
                            {"mode": first, "radius": 4, "points": [[20, 20]]},
                            {"mode": second, "radius": 4, "points": [[20, 20]]},
                        ],
                    },
                }
            ],
            padding=0,
            dilation=0,
            feather=0,
        )

    erase_then_add = render("erase", "add")
    add_then_erase = render("add", "erase")

    assert erase_then_add[20, 20] == 255
    assert np.count_nonzero(erase_then_add) > 0
    assert add_then_erase[20, 20] == 0
    assert np.count_nonzero(add_then_erase) == 0


def test_automatic_mask_strokes_apply_in_persisted_order_after_feathering() -> None:
    def render(first: str, second: str) -> np.ndarray:
        return create_mask(
            (48, 48),
            [
                {
                    "x": 2,
                    "y": 2,
                    "width": 6,
                    "height": 6,
                    "maskMode": "region",
                    "maskEdits": {
                        "version": 1,
                        "strokes": [
                            {"mode": first, "radius": 4, "points": [[30, 30]]},
                            {"mode": second, "radius": 4, "points": [[30, 30]]},
                        ],
                    },
                }
            ],
            padding=0,
            dilation=0,
            feather=3,
        )

    erase_then_add = render("erase", "add")
    add_then_erase = render("add", "erase")

    assert erase_then_add[30, 30] == 255
    assert add_then_erase[30, 30] == 0
    assert np.any(erase_then_add[:12, :12])
    assert np.any(add_then_erase[:12, :12])


def test_mask_erase_stays_zero_after_base_feathering() -> None:
    image = Image.new("RGB", (64, 64), "white")
    region = {
        "x": 12,
        "y": 12,
        "width": 40,
        "height": 40,
        "maskMode": "region",
        "maskEdits": {
            "version": 1,
            "strokes": [
                {"mode": "add", "radius": 3, "points": [[5, 5]]},
                {"mode": "erase", "radius": 5, "points": [[32, 32]]},
            ],
        },
    }

    mask = create_mask(image, [region], padding=0, dilation=0, feather=3)

    assert mask[5, 5] > mask[5, 9] > 0
    assert mask[32, 32] == 0
    assert mask[32, 36] == 0
    assert mask[32, 38] == 255
    assert 0 < mask[32, 11] < 255


@pytest.mark.parametrize(
    "mask_edits",
    (
        {"version": True, "strokes": []},
        {"version": 1, "strokes": [{"mode": "add", "radius": float("nan"), "points": [[1, 1]]}]},
        {"version": 1, "strokes": [{"mode": "erase", "radius": 2, "points": [[101, 1]]}]},
    ),
)
def test_mask_edit_validation_rejects_noncanonical_payloads(mask_edits: dict) -> None:
    with pytest.raises(ValueError):
        create_mask(
            (100, 80),
            [
                {
                    "x": 10,
                    "y": 10,
                    "width": 20,
                    "height": 20,
                    "maskEdits": mask_edits,
                }
            ],
            padding=0,
            dilation=0,
        )


def test_typesetting_horizontal_vertical_rotation_stroke_and_overflow() -> None:
    capabilities = font_capabilities()
    if not capabilities["available"]:
        pytest.skip("No usable system CJK font")
    source = Image.new("RGB", (360, 260), "white")
    regions = [
        {
            "id": "horizontal",
            "x": 10,
            "y": 10,
            "width": 210,
            "height": 90,
            "rotation": 8,
            "direction": "horizontal",
            "translationText": "这是横向中文排版测试",
            "style": {
                "fontSize": 36,
                "minFontSize": 12,
                "fontFamily": "system-ui",
                "autoFit": True,
                "strokeWidth": 2,
                "strokeColor": "#ffffff",
                "color": "#cc0000",
                "lineHeight": 1.25,
                "letterSpacing": 2,
                "align": "end",
            },
        },
        {
            "id": "vertical",
            "x": 250,
            "y": 10,
            "width": 90,
            "height": 220,
            "direction": "vertical",
            "translationText": "这是竖向中文排版",
            "style": {"fontSize": 30, "minFontSize": 10, "strokeWidth": 1},
        },
        {
            "id": "overflow",
            "x": 10,
            "y": 150,
            "width": 60,
            "height": 30,
            "direction": "horizontal",
            "translationText": "非常非常非常长的文本",
            "style": {"fontSize": 18, "minFontSize": 18},
        },
    ]
    result = typeset_image(source, regions)
    assert result.image.size == source.size
    assert {layout["regionId"] for layout in result.layouts} == {
        "horizontal",
        "vertical",
        "overflow",
    }
    by_id = {layout["regionId"]: layout for layout in result.layouts}
    assert by_id["horizontal"]["rotation"] == 8
    assert by_id["horizontal"]["align"] == "end"
    assert by_id["horizontal"]["lineSpacing"] == pytest.approx(0.25)
    assert by_id["horizontal"]["letterSpacing"] == 2
    assert by_id["horizontal"]["fill"] == (204, 0, 0, 255)
    assert by_id["vertical"]["direction"] == "vertical"
    assert by_id["overflow"]["overflow"] is True
    assert overflow_region_ids(result.layouts) == ["overflow"]
    assert typeset_overflow_from_status(
        {
            "typeset": "done",
            "typesetOverflowRegionIds": ["overflow", "overflow", 12, ""],
        },
    ) == (1, ["overflow"])
    assert typeset_overflow_from_status(
        {"typeset": "pending", "typesetOverflowRegionIds": ["overflow"]},
    ) == (0, [])
    assert np.any(np.asarray(result.image.convert("RGB")) != 255)
    pixels = np.asarray(result.image.convert("RGB"))
    assert np.any((pixels[..., 0] > 150) & (pixels[..., 1] < 80) & (pixels[..., 2] < 80))


def test_vertical_typesetting_uses_vertical_punctuation_forms() -> None:
    assert verticalize_punctuation("\u300c你好\u300d\u2014\u2014啊\u2026") == (
        "\ufe41你好\ufe42\ufe31\ufe31啊\ufe19"
    )
    assert verticalize_punctuation("横向\u300c引号\u300d") == "横向\ufe41引号\ufe42"
    assert verticalize_punctuation("你好\u3002") == "你好\u3002"

    capabilities = font_capabilities()
    if not capabilities["available"]:
        pytest.skip("No usable system CJK font")
    source = Image.new("RGB", (220, 260), "white")
    vertical = {
        "id": "quoted",
        "x": 20,
        "y": 10,
        "width": 80,
        "height": 230,
        "direction": "vertical",
        "translationText": "\u300c你好\u3002\u300d",
        "style": {"fontSize": 28, "minFontSize": 12, "strokeWidth": 0, "autoFit": False},
    }
    horizontal = {
        **vertical,
        "id": "horizontal-quoted",
        "x": 120,
        "direction": "horizontal",
        "width": 80,
        "height": 80,
    }
    result = typeset_image(source, [vertical, horizontal])
    by_id = {layout["regionId"]: layout for layout in result.layouts}
    assert "\ufe41" in "".join(by_id["quoted"]["lines"])
    assert "\ufe42" in "".join(by_id["quoted"]["lines"])
    assert "\u300c" not in "".join(by_id["quoted"]["lines"])
    assert "\u300c" in "".join(by_id["horizontal-quoted"]["lines"])
    assert "\u300d" in "".join(by_id["horizontal-quoted"]["lines"])


def test_short_vertical_typesetting_is_centered_inside_its_region() -> None:
    capabilities = font_capabilities()
    if not capabilities["available"]:
        pytest.skip("No usable system CJK font")
    source = Image.new("RGB", (120, 240), "white")
    result = typeset_image(
        source,
        [
            {
                "id": "short-vertical",
                "x": 20,
                "y": 20,
                "width": 80,
                "height": 200,
                "direction": "vertical",
                "translationText": "天使\uff1f",
                "style": {
                    "fontSize": 24,
                    "minFontSize": 24,
                    "padding": 4,
                    "lineSpacing": 0,
                    "verticalAlign": "center",
                    "strokeWidth": 0,
                    "autoFit": False,
                },
            }
        ],
    )

    changed = np.any(np.asarray(result.image.convert("RGB")) != 255, axis=2)
    ys, xs = np.nonzero(changed)
    assert xs.min() >= 20
    assert xs.max() < 100
    assert ys.min() >= 70
    assert ys.max() < 170
    assert (ys.min() + ys.max()) / 2 == pytest.approx(120, abs=15)


def test_fragment_clusters_pack_shared_text_across_adjacent_boxes() -> None:
    first = {
        "id": "frag-a",
        "x": 40,
        "y": 10,
        "width": 22,
        "height": 80,
        "direction": "vertical",
        "order": 0,
        "translationText": "这段译文需要两个碎框一起排",
        "style": {"fontSize": 16, "minFontSize": 8, "strokeWidth": 0},
    }
    second = {
        **first,
        "id": "frag-b",
        "y": 94,
        "order": 1,
    }
    distant = {
        **first,
        "id": "lonely",
        "x": 200,
        "y": 10,
        "translationText": "单独",
    }
    clustered = cluster_fragment_regions([first, second, distant])
    assert [tuple(item["id"] for item in group) for group in clustered] == [
        ("frag-a", "frag-b"),
        ("lonely",),
    ]

    capabilities = font_capabilities()
    if not capabilities["available"]:
        pytest.skip("No usable system CJK font")
    source = Image.new("RGB", (260, 200), "white")
    independent = typeset_image(source, [first])
    packed = typeset_image(source, [first, second])
    independent_by_id = {layout["regionId"]: layout for layout in independent.layouts}
    packed_by_id = {layout["regionId"]: layout for layout in packed.layouts}
    assert independent_by_id["frag-a"]["overflow"] is True
    assert packed_by_id["frag-a"]["overflow"] is False
    assert packed_by_id["frag-b"]["overflow"] is False
    packed_chars = "".join("".join(layout["lines"]) for layout in packed.layouts)
    assert "这" in packed_chars
    assert "排" in packed_chars


def test_fragment_cluster_thresholds_scale_with_the_render_grid() -> None:
    canonical = [
        {
            "id": "a",
            "x": 40,
            "y": 10,
            "width": 22,
            "height": 80,
            "direction": "vertical",
            "order": 0,
        },
        {
            "id": "b",
            "x": 40,
            "y": 94,
            "width": 22,
            "height": 80,
            "direction": "vertical",
            "order": 1,
        },
    ]
    scaled = [
        {
            **region,
            "x": region["x"] * 4,
            "y": region["y"] * 4,
            "width": region["width"] * 4,
            "height": region["height"] * 4,
        }
        for region in canonical
    ]

    canonical_groups = cluster_fragment_regions(canonical)
    scaled_groups = cluster_fragment_regions(scaled, geometry_scale=4)

    assert [[region["id"] for region in group] for group in scaled_groups] == [
        [region["id"] for region in group] for group in canonical_groups
    ]
    with pytest.raises(ValueError, match="geometry_scale"):
        cluster_fragment_regions(scaled, geometry_scale=0)


def test_fragment_clusters_concatenate_distinct_fragment_text() -> None:
    first = {
        "id": "part-a",
        "x": 20,
        "y": 8,
        "width": 24,
        "height": 70,
        "direction": "vertical",
        "order": 0,
        "translationText": "上段",
        "style": {"fontSize": 18, "minFontSize": 10, "strokeWidth": 0},
    }
    second = {**first, "id": "part-b", "y": 82, "order": 1, "translationText": "下段"}
    groups = cluster_fragment_regions([first, second])
    assert [tuple(item["id"] for item in group) for group in groups] == [("part-a", "part-b")]
    capabilities = font_capabilities()
    if not capabilities["available"]:
        pytest.skip("No usable system CJK font")
    result = typeset_image(Image.new("RGB", (80, 180), "white"), [first, second])
    packed = "".join("".join(layout["lines"]) for layout in result.layouts)
    assert "上" in packed
    assert "下" in packed


def test_expand_typeset_region_ids_includes_fragment_cluster_mates() -> None:
    first = {
        "id": "frag-a",
        "x": 40,
        "y": 10,
        "width": 22,
        "height": 80,
        "direction": "vertical",
        "order": 0,
        "translationText": "这段译文需要两个碎框一起排",
    }
    second = {**first, "id": "frag-b", "y": 94, "order": 1}
    distant = {**first, "id": "lonely", "x": 200, "y": 10, "translationText": "单独"}
    assert expand_typeset_region_ids([first, second, distant], ["frag-a"]) == [
        "frag-a",
        "frag-b",
    ]
    assert expand_typeset_region_ids([first, second, distant], ["lonely"]) == ["lonely"]
    assert expand_typeset_region_ids([first, second, distant], []) == []


def test_restore_clean_region_boxes_replaces_only_selected_pixels() -> None:
    typeset = Image.new("RGB", (80, 60), (0, 0, 255))
    clean = Image.new("RGB", (80, 60), (255, 255, 255))
    restored = restore_clean_region_boxes(
        typeset,
        clean,
        [{"x": 10, "y": 8, "width": 20, "height": 16}],
    )
    pixels = np.asarray(restored.convert("RGB"))
    assert pixels[12, 15].tolist() == [255, 255, 255]
    assert pixels[40, 50].tolist() == [0, 0, 255]


def test_typeset_overlay_redraws_one_horizontal_box_on_an_existing_plate() -> None:
    capabilities = font_capabilities()
    if not capabilities["available"]:
        pytest.skip("No usable system CJK font")
    style = {
        "fontSize": 32,
        "minFontSize": 32,
        "autoFit": False,
        "autoWrap": False,
        "strokeWidth": 0,
        "color": "#cc0000",
        "align": "start",
        "padding": 2,
    }
    first = {
        "id": "left",
        "x": 16,
        "y": 24,
        "width": 168,
        "height": 88,
        "direction": "horizontal",
        "translationText": "甲甲",
        "style": style,
    }
    second = {
        **first,
        "id": "right",
        "x": 216,
        "translationText": "乙乙乙乙",
    }
    source = Image.new("RGB", (400, 160), "white")
    initial = typeset_image(source, [first, second]).image
    punched = restore_clean_region_boxes(initial, source, [first])
    first = {**first, "translationText": "丙丙丙丙丙丙丙丙"}
    overlay = typeset_image(punched, [first]).image
    before = np.asarray(initial.convert("RGB"))
    after = np.asarray(overlay.convert("RGB"))
    left = np.s_[24:112, 16:184]
    right = np.s_[24:112, 216:384]
    assert not np.array_equal(before[left], after[left])
    assert np.array_equal(before[right], after[right])
