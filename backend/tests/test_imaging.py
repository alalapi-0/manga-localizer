from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging import OpenCVInpaintingProvider, create_mask, inpaint, typeset_image
from manga_localizer.imaging.typesetting import font_capabilities


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
    assert np.any(np.asarray(result.image.convert("RGB")) != 255)
    pixels = np.asarray(result.image.convert("RGB"))
    assert np.any((pixels[..., 0] > 150) & (pixels[..., 1] < 80) & (pixels[..., 2] < 80))
