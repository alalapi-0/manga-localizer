"""Generate a copyright-safe synthetic manga-like image for tests and demos."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
        Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
        Path("C:/Windows/Fonts/meiryo.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def generate(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (900, 1200), "#f4f1e8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 860, 1160), outline="#171717", width=8)
    draw.line((450, 40, 450, 1160), fill="#171717", width=6)
    draw.ellipse((510, 130, 820, 430), fill="white", outline="#171717", width=5)
    draw.ellipse((90, 650, 390, 950), fill="white", outline="#171717", width=5)
    font = find_font(46)
    draw.multiline_text((570, 220), "こんにちは\nせかい", font=font, fill="#111111", spacing=12)
    draw.multiline_text((145, 750), "テストです", font=font, fill="#111111", spacing=12)
    draw.polygon([(150, 160), (360, 110), (400, 430), (100, 470)], fill="#d9d3c6")
    draw.line((110, 500, 410, 610), fill="#333333", width=10)
    image.save(destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    generate(args.destination)


if __name__ == "__main__":
    main()

