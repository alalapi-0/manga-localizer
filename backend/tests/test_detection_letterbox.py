from __future__ import annotations

import numpy as np

from manga_localizer.providers.detection import (
    detection_min_side_for_image,
    detection_region_is_usable,
    detection_tile_origins,
    letterbox_detection_image,
    suppress_overlapping_detections,
    unletterbox_detection_points,
)
from manga_localizer.providers.ocr import OCRRegion


def test_letterbox_keeps_tall_page_aspect_ratio() -> None:
    source = np.full((1843, 627, 3), 240, dtype=np.uint8)
    source[200:400, 80:200] = 10
    canvas, scale, pad_x, pad_y = letterbox_detection_image(source, (736, 736))

    assert canvas.shape == (736, 736, 3)
    assert scale == min(736 / 627, 736 / 1843)
    placed_width = round(627 * scale)
    placed_height = round(1843 * scale)
    assert placed_width < 736
    assert placed_height == 736 or placed_height == 735
    assert pad_x > 0
    assert pad_y == 0 or pad_y <= 1
    y0 = int(pad_y)
    x0 = int(pad_x)
    content = canvas[y0 : y0 + placed_height, x0 : x0 + placed_width]
    assert content.shape[0] >= placed_height - 1
    assert (content < 40).any()
    assert not (canvas[:, : max(1, x0 - 1)] < 40).any()


def test_unletterbox_roundtrips_a_box_on_a_tall_page() -> None:
    source = np.zeros((1800, 600, 3), dtype=np.uint8)
    _canvas, scale, pad_x, pad_y = letterbox_detection_image(source, (736, 736))
    original = np.array(
        [[80.0, 240.0], [200.0, 240.0], [200.0, 400.0], [80.0, 400.0]],
        dtype=np.float32,
    )
    letterboxed = original * scale
    letterboxed[:, 0] += pad_x
    letterboxed[:, 1] += pad_y
    mapped = unletterbox_detection_points(
        letterboxed,
        scale=scale,
        pad_x=pad_x,
        pad_y=pad_y,
        image_width=600,
        image_height=1800,
    )
    assert np.allclose(mapped, original, atol=1.5)


def test_detection_tiles_cover_a_tall_narrow_page() -> None:
    tiles = detection_tile_origins(627, 1843, 736, 736, overlap=184)
    assert tiles[0] == (0, 0, 627, 736)
    assert tiles[-1][1] + tiles[-1][3] == 1843
    cover = np.zeros((1843, 627), dtype=np.uint8)
    for x, y, width, height in tiles:
        cover[y : y + height, x : x + width] = 1
    assert bool(cover.all())
    assert len(tiles) > 1


def test_detection_min_side_drops_tile_fragments_on_a_wide_plate() -> None:
    wide_plate = detection_min_side_for_image(4440, 1248)
    original_page = detection_min_side_for_image(1110, 312)
    small_page = detection_min_side_for_image(340, 594)
    assert wide_plate == 32
    assert original_page == 13
    assert small_page == 14
    assert not detection_region_is_usable(6, 7, min_side=original_page)
    assert not detection_region_is_usable(8, 25, min_side=original_page)
    assert detection_region_is_usable(15, 40, min_side=original_page)
    assert detection_region_is_usable(42, 74, min_side=original_page)
    assert not detection_region_is_usable(20, 20, min_side=wide_plate)
    assert detection_region_is_usable(60, 160, min_side=wide_plate)


def test_suppress_overlapping_detections_keeps_the_stronger_box() -> None:
    kept = suppress_overlapping_detections(
        [
            OCRRegion(10, 10, 40, 40, "", 0.4, "horizontal"),
            OCRRegion(12, 12, 40, 40, "", 0.9, "horizontal"),
            OCRRegion(200, 200, 30, 30, "", 0.7, "horizontal"),
        ]
    )
    assert [(region.x, region.y, region.confidence) for region in kept] == [
        (12, 12, 0.9),
        (200, 200, 0.7),
    ]
