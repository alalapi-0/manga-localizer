from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging import OpenCVInpaintingProvider, create_mask, inpaint, typeset_image
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
    assert provider.get_capabilities()["textPolarities"] == ["auto", "dark", "light"]


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
