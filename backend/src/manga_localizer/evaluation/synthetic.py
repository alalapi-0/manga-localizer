from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from manga_localizer.evaluation.detection_ocr import (
    REVIEWED_STATUS,
    AnnotationBox,
    PageAnnotation,
)
from manga_localizer.imaging.typesetting import default_cjk_font

STRESS_PAGE_SPECS = (
    "bubble-horizontal",
    "narration-horizontal",
    "sfx-art",
    "vertical-dialogue",
    "single-char",
    "negative-lineart",
    "complex-lineart-sign",
)


@dataclass(frozen=True)
class SyntheticPage:
    spec: str
    image: Image.Image
    annotation: PageAnnotation


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = default_cjk_font()
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _hatch(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], step: int = 9) -> None:
    left, top, right, bottom = box
    for offset in range(left - (bottom - top), right + (bottom - top), step):
        draw.line((offset, top, offset + (bottom - top), bottom), fill="#3a3a3a", width=1)
        draw.line((offset, bottom, offset + (bottom - top), top), fill="#5a5a5a", width=1)


def _box_from_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    *,
    padding: int = 3,
    categories: tuple[str, ...],
    direction: str,
) -> AnnotationBox:
    left, top, right, bottom = draw.textbbox(xy, text, font=font)
    return AnnotationBox(
        x=max(0, int(left) - padding),
        y=max(0, int(top) - padding),
        width=int(right - left) + padding * 2,
        height=int(bottom - top) + padding * 2,
        text=text,
        direction=direction,
        categories=categories,
        status=REVIEWED_STATUS,
    )


def _vertical_text(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    *,
    gap: int,
) -> tuple[AnnotationBox, tuple[int, int, int, int]]:
    x, y = origin
    boxes: list[tuple[int, int, int, int]] = []
    cursor = y
    for character in text:
        glyph = _box_from_text(
            draw,
            (x, cursor),
            character,
            font,
            padding=0,
            categories=(),
            direction="vertical",
        )
        draw.text((x, cursor), character, font=font, fill="#111111")
        boxes.append((glyph.x, glyph.y, glyph.x + glyph.width, glyph.y + glyph.height))
        cursor = glyph.y + glyph.height + gap
    left = min(item[0] for item in boxes) - 3
    top = min(item[1] for item in boxes) - 3
    right = max(item[2] for item in boxes) + 3
    bottom = max(item[3] for item in boxes) + 3
    region = AnnotationBox(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
        text=text,
        direction="vertical",
        categories=("bubble", "vertical"),
        status=REVIEWED_STATUS,
    )
    return region, (left, top, right, bottom)


def _page(
    spec: str,
    image: Image.Image,
    boxes: list[AnnotationBox],
    *,
    negative: bool = False,
) -> SyntheticPage:
    return SyntheticPage(
        spec=spec,
        image=image,
        annotation=PageAnnotation(
            page_id=spec,
            width=image.width,
            height=image.height,
            boxes=tuple(boxes),
            negative=negative,
            status=REVIEWED_STATUS,
            independence="ground-truth",
        ),
    )


def generate_detection_stress_pages() -> list[SyntheticPage]:
    pages: list[SyntheticPage] = []

    bubble = Image.new("RGB", (720, 960), "#f3efe4")
    draw = ImageDraw.Draw(bubble)
    draw.ellipse((90, 80, 630, 430), fill="white", outline="#171717", width=6)
    font = _font(54)
    bubble_text = "こんにちは"
    text_xy = (220, 200)
    draw.text(text_xy, bubble_text, font=font, fill="#111111")
    pages.append(
        _page(
            "bubble-horizontal",
            bubble,
            [
                _box_from_text(
                    draw,
                    text_xy,
                    bubble_text,
                    font,
                    categories=("bubble", "horizontal"),
                    direction="horizontal",
                )
            ],
        )
    )

    narration = Image.new("RGB", (720, 960), "#efe7d6")
    draw = ImageDraw.Draw(narration)
    draw.rectangle((40, 36, 680, 150), fill="#f7f1e4", outline="#171717", width=4)
    font = _font(48)
    caption = "本日の話"
    caption_xy = (210, 70)
    draw.text(caption_xy, caption, font=font, fill="#111111")
    pages.append(
        _page(
            "narration-horizontal",
            narration,
            [
                _box_from_text(
                    draw,
                    caption_xy,
                    caption,
                    font,
                    categories=("non-bubble", "horizontal", "title"),
                    direction="horizontal",
                )
            ],
        )
    )

    sfx = Image.new("RGB", (720, 960), "#d9d3c4")
    draw = ImageDraw.Draw(sfx)
    _hatch(draw, (0, 0, 720, 960), step=14)
    font = _font(160)
    sfx_text = "ドン"
    sfx_xy = (120, 340)
    draw.text(sfx_xy, sfx_text, font=font, fill="#111111", stroke_width=4, stroke_fill="#f4f1e8")
    pages.append(
        _page(
            "sfx-art",
            sfx,
            [
                _box_from_text(
                    draw,
                    sfx_xy,
                    sfx_text,
                    font,
                    padding=8,
                    categories=("sfx", "art", "horizontal", "non-bubble"),
                    direction="horizontal",
                )
            ],
        )
    )

    vertical = Image.new("RGB", (720, 960), "#f3efe4")
    draw = ImageDraw.Draw(vertical)
    draw.ellipse((220, 70, 500, 880), fill="white", outline="#171717", width=6)
    font = _font(72)
    vertical_box, _bounds = _vertical_text(draw, (310, 140), "日本語です", font, gap=8)
    pages.append(_page("vertical-dialogue", vertical, [vertical_box]))

    single = Image.new("RGB", (720, 960), "#f7f4ea")
    draw = ImageDraw.Draw(single)
    font = _font(96)
    glyph = "あ"
    glyph_xy = (310, 420)
    draw.text(glyph_xy, glyph, font=font, fill="#111111")
    pages.append(
        _page(
            "single-char",
            single,
            [
                _box_from_text(
                    draw,
                    glyph_xy,
                    glyph,
                    font,
                    categories=("single-char", "non-bubble", "horizontal"),
                    direction="horizontal",
                )
            ],
        )
    )

    negative = Image.new("RGB", (720, 960), "#e6e0d2")
    draw = ImageDraw.Draw(negative)
    _hatch(draw, (0, 0, 720, 960), step=8)
    draw.ellipse((80, 90, 300, 330), outline="#171717", width=5)
    draw.ellipse((400, 420, 650, 720), outline="#171717", width=5)
    draw.arc((180, 520, 520, 860), 20, 160, fill="#171717", width=6)
    pages.append(_page("negative-lineart", negative, [], negative=True))

    complex_page = Image.new("RGB", (720, 960), "#efe8d8")
    draw = ImageDraw.Draw(complex_page)
    _hatch(draw, (0, 0, 720, 960), step=6)
    for index in range(8):
        draw.line((40, 80 + index * 100, 680, 40 + index * 110), fill="#2b2b2b", width=3)
        draw.polygon(
            [(80 + index * 70, 200), (140 + index * 70, 260), (60 + index * 70, 320)],
            outline="#171717",
        )
    draw.rectangle((470, 70, 680, 170), fill="#f8f4e8", outline="#111111", width=4)
    font = _font(42)
    sign = "出口"
    sign_xy = (510, 95)
    draw.text(sign_xy, sign, font=font, fill="#111111")
    pages.append(
        _page(
            "complex-lineart-sign",
            complex_page,
            [
                _box_from_text(
                    draw,
                    sign_xy,
                    sign,
                    font,
                    categories=("non-bubble", "sign", "horizontal", "complex-lineart"),
                    direction="horizontal",
                )
            ],
        )
    )
    return pages


def annotation_document(page: SyntheticPage) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "status": REVIEWED_STATUS,
        "independence": "ground-truth",
        "negative": page.annotation.negative,
        "image": {
            "id": page.spec,
            "width": page.annotation.width,
            "height": page.annotation.height,
        },
        "regions": [
            {
                "x": box.x,
                "y": box.y,
                "width": box.width,
                "height": box.height,
                "text": box.text,
                "direction": box.direction,
                "categories": list(box.categories),
                "status": box.status,
            }
            for box in page.annotation.boxes
        ],
    }


def write_detection_stress_set(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for page in generate_detection_stress_pages():
        image_path = destination / f"{page.spec}.png"
        annotation_path = destination / f"{page.spec}.json"
        page.image.save(image_path)
        annotation_path.write_text(
            json.dumps(annotation_document(page), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.extend((image_path, annotation_path))
    manifest = destination / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "public-synthetic-detection-stress",
                "pages": list(STRESS_PAGE_SPECS),
                "independence": "ground-truth",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(manifest)
    return written
