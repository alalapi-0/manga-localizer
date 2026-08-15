from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging import OpenCVInpaintingProvider, create_mask, inpaint, typeset_image
from manga_localizer.imaging.typesetting import (
    font_capabilities,
    overflow_region_ids,
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


def test_text_mask_keeps_sparse_strokes_but_closes_dense_outlined_lettering() -> None:
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
    assert np.array_equal(dense_mask, sparse_geometry)


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
