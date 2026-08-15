from __future__ import annotations

import math
import os
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFont

FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}


@lru_cache(maxsize=1)
def discover_system_fonts() -> tuple[Path, ...]:
    roots = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    ]
    fonts: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                fonts.add(path.resolve())
    return tuple(sorted(fonts, key=lambda item: str(item).casefold()))


def _font_score(path: Path) -> tuple[int, str]:
    name = path.name.casefold()
    preferred = (
        "hiragino sans gb",
        "pingfang",
        "noto sans cjk",
        "sourcehansans",
        "wenquanyi",
        "simhei",
        "msyh",
        "stheiti",
    )
    score = next((index for index, token in enumerate(preferred) if token in name), 999)
    return score, name


@lru_cache(maxsize=1)
def default_cjk_font() -> Path | None:
    fonts = discover_system_fonts()
    if not fonts:
        return None
    return min(fonts, key=_font_score)


def _font_for_family(family: str | None) -> Path | None:
    if not family:
        return default_cjk_font()
    requested = [
        item.strip().strip("'\"").casefold()
        for item in family.split(",")
        if item.strip().strip("'\"")
    ]
    generic = {"system-ui", "sans-serif", "serif", "monospace", "cursive", "fantasy"}
    for item in requested:
        if item in generic:
            continue
        normalized = "".join(character for character in item if character.isalnum())
        for font in discover_system_fonts():
            font_name = "".join(
                character for character in font.stem.casefold() if character.isalnum()
            )
            if normalized and normalized in font_name:
                return font
    return default_cjk_font()


def _load_font(
    path: str | Path | None,
    size: int,
    family: str | None = None,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    selected = Path(path).expanduser().resolve(strict=True) if path else _font_for_family(family)
    if selected is None:
        return ImageFont.load_default(size=size)
    if selected.suffix.lower() not in FONT_SUFFIXES:
        raise ValueError("Font must be an installed TTF, TTC, or OTF file")
    return ImageFont.truetype(str(selected), size=size)


def font_capabilities() -> dict[str, Any]:
    fonts = discover_system_fonts()
    selected = default_cjk_font()
    usable = False
    error: str | None = None
    if selected is not None:
        try:
            _load_font(selected, 24).getbbox("中文测试")
            usable = True
        except OSError as caught:
            error = str(caught)
    return {
        "available": usable,
        "defaultFont": str(selected) if selected else None,
        "fontCount": len(fonts),
        "platform": platform.system(),
        "error": error,
    }


@dataclass
class TypesetResult:
    image: Image.Image
    layouts: list[dict[str, Any]]


def overflow_region_ids(layouts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return unique overflowing region IDs in layout order."""
    ids: list[str] = []
    seen: set[str] = set()
    for layout in layouts:
        if not layout.get("overflow"):
            continue
        region_id = layout.get("regionId")
        if not isinstance(region_id, str) or not region_id or region_id in seen:
            continue
        seen.add(region_id)
        ids.append(region_id)
    return ids


def typeset_overflow_from_status(status: object) -> tuple[int, list[str]]:
    """Project persisted overflow IDs only while typesetting is current."""
    if not isinstance(status, dict) or status.get("typeset") != "done":
        return 0, []
    raw_ids = status.get("typesetOverflowRegionIds")
    if not isinstance(raw_ids, list):
        return 0, []
    ids = overflow_region_ids(
        [{"regionId": item, "overflow": True} for item in raw_ids if isinstance(item, str)],
    )
    return len(ids), ids


_VERTICAL_PUNCTUATION = str.maketrans(
    {
        "\u300c": "\ufe41",
        "\u300d": "\ufe42",
        "\u300e": "\ufe43",
        "\u300f": "\ufe44",
        "\uff08": "\ufe35",
        "\uff09": "\ufe36",
        "(": "\ufe35",
        ")": "\ufe36",
        "\u3014": "\ufe39",
        "\u3015": "\ufe3a",
        "\u3010": "\ufe3b",
        "\u3011": "\ufe3c",
        "\u300a": "\ufe3d",
        "\u300b": "\ufe3e",
        "\u3008": "\ufe3f",
        "\u3009": "\ufe40",
        "\u2014": "\ufe31",
        "\u2013": "\ufe31",
        "\u30fc": "\ufe31",
        "\u2026": "\ufe19",
        "\u22ef": "\ufe19",
        "\uff1a": "\ufe13",
        "\uff1b": "\ufe14",
        "\uff01": "\ufe15",
        "\uff1f": "\ufe16",
    }
)
_HANGING_PUNCTUATION = frozenset("\u3001\u3002\uff0c\uff0e,.")
_FRAGMENT_MAX_SHORT_SIDE = 56.0
_FRAGMENT_MAX_AREA = 2800.0
_FRAGMENT_GAP = 10.0
_FRAGMENT_ALIGN = 0.3


def verticalize_punctuation(text: str) -> str:
    """Map horizontal CJK punctuation to vertical presentation forms."""
    return text.translate(_VERTICAL_PUNCTUATION)


def _style_value(style: Mapping[str, Any], camel: str, snake: str, default: Any) -> Any:
    return style.get(camel, style.get(snake, default))


def _region_box(region: Mapping[str, Any]) -> tuple[float, float, float, float]:
    x = float(region.get("x", 0))
    y = float(region.get("y", 0))
    width = max(0.0, float(region.get("width", 0)))
    height = max(0.0, float(region.get("height", 0)))
    return x, y, x + width, y + height


def _region_direction(region: Mapping[str, Any]) -> str:
    direction = str(region.get("direction", "vertical"))
    if direction in {"horizontal", "vertical"}:
        return direction
    x1, y1, x2, y2 = _region_box(region)
    return "vertical" if (y2 - y1) >= (x2 - x1) else "horizontal"


def _is_fragment_region(region: Mapping[str, Any]) -> bool:
    x1, y1, x2, y2 = _region_box(region)
    width = x2 - x1
    height = y2 - y1
    return min(width, height) <= _FRAGMENT_MAX_SHORT_SIDE or width * height <= _FRAGMENT_MAX_AREA


def _box_gap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = left
    bx1, by1, bx2, by2 = right
    dx = max(0.0, ax1 - bx2, bx1 - ax2)
    dy = max(0.0, ay1 - by2, by1 - ay2)
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.hypot(dx, dy)


def _boxes_aligned(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    direction: str,
) -> bool:
    ax1, ay1, ax2, ay2 = left
    bx1, by1, bx2, by2 = right
    if direction == "vertical":
        overlap = min(ax2, bx2) - max(ax1, bx1)
        span = min(ax2 - ax1, bx2 - bx1)
    else:
        overlap = min(ay2, by2) - max(ay1, by1)
        span = min(ay2 - ay1, by2 - by1)
    return span > 0 and overlap >= span * _FRAGMENT_ALIGN


def _region_sort_key(region: Mapping[str, Any]) -> tuple[int, float, float]:
    raw_order = region.get("order", region.get("reading_order", 0))
    try:
        order = int(raw_order)
    except (TypeError, ValueError):
        order = 0
    x1, y1, _, _ = _region_box(region)
    if _region_direction(region) == "vertical":
        return order, -x1, y1
    return order, y1, x1


def cluster_fragment_regions(
    regions: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Group adjacent small boxes that can share one typeset run."""
    parent = list(range(len(regions)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left, region in enumerate(regions):
        if region.get("ignored") or not _is_fragment_region(region):
            continue
        left_box = _region_box(region)
        left_direction = _region_direction(region)
        for right in range(left + 1, len(regions)):
            other = regions[right]
            if other.get("ignored") or not _is_fragment_region(other):
                continue
            if _region_direction(other) != left_direction:
                continue
            other_box = _region_box(other)
            if _box_gap(left_box, other_box) <= _FRAGMENT_GAP and _boxes_aligned(
                left_box, other_box, left_direction
            ):
                union(left, right)

    grouped: dict[int, list[Mapping[str, Any]]] = {}
    order: list[int] = []
    for index, region in enumerate(regions):
        root = find(index)
        if root not in grouped:
            grouped[root] = []
            order.append(root)
        grouped[root].append(region)
    clusters: list[list[Mapping[str, Any]]] = []
    for root in order:
        members = grouped[root]
        if len(members) > 1:
            members = sorted(members, key=_region_sort_key)
        clusters.append(members)
    return clusters


def _cluster_text(regions: Sequence[Mapping[str, Any]]) -> str:
    texts = [
        str(region.get("translationText", region.get("translation_text", ""))).replace("\n", "")
        for region in regions
    ]
    texts = [item for item in texts if item]
    unique = list(dict.fromkeys(texts))
    if len(unique) == 1:
        return unique[0]
    return "".join(texts)


def _effective_padding(width: int, height: int, padding: int) -> int:
    if padding <= 0:
        return 0
    budget = max(0, min(width, height) // 6)
    return min(padding, budget)


def _vertical_capacity(
    width: int,
    height: int,
    size: int,
    line_spacing: float,
    padding: int,
) -> int:
    pad = _effective_padding(width, height, padding)
    content_width = max(1, width - pad * 2)
    content_height = max(1, height - pad * 2)
    cell = max(1, math.ceil(size * (1 + line_spacing)))
    rows = max(1, content_height // cell)
    columns = max(1, content_width // cell)
    return rows * columns


def _horizontal_capacity(
    width: int,
    height: int,
    size: int,
    line_spacing: float,
    letter_spacing: float,
    padding: int,
) -> int:
    pad = _effective_padding(width, height, padding)
    content_width = max(1, width - pad * 2)
    content_height = max(1, height - pad * 2)
    line_height = max(1, math.ceil(size * (1 + line_spacing)))
    lines = max(1, content_height // line_height)
    glyph = max(1, math.ceil(size + max(0.0, letter_spacing)))
    per_line = max(1, content_width // glyph)
    return lines * per_line


def _assign_cluster_slices(text: str, capacities: Sequence[int]) -> tuple[list[str], list[bool]]:
    characters = list(text)
    slices: list[str] = []
    overflow = [False] * len(capacities)
    cursor = 0
    last_index = len(capacities) - 1
    for index, capacity in enumerate(capacities):
        if index == last_index:
            take = characters[cursor:]
            cursor = len(characters)
            slices.append("".join(take))
            overflow[index] = len(take) > capacity
            continue
        take = characters[cursor : cursor + max(0, capacity)]
        cursor += len(take)
        slices.append("".join(take))
    return slices, overflow


def _region_style_metrics(region: Mapping[str, Any]) -> tuple[int, int, float, float, int, bool]:
    width = max(1, round(float(region["width"])))
    height = max(1, round(float(region["height"])))
    style = region.get("style") or {}
    padding = _effective_padding(width, height, max(0, int(style.get("padding", 4))))
    min_size = max(4, int(_style_value(style, "minFontSize", "min_font_size", 8)))
    max_size = max(
        min_size, int(_style_value(style, "fontSize", "font_size", min(width, height) // 3 or 12))
    )
    max_size = max(max_size, int(_style_value(style, "maxFontSize", "max_font_size", max_size)))
    auto_fit = bool(_style_value(style, "autoFit", "auto_fit", True))
    if not auto_fit:
        min_size = max_size
    if "lineHeight" in style or "line_height" in style:
        line_spacing = max(0.0, float(_style_value(style, "lineHeight", "line_height", 1.15)) - 1.0)
    else:
        line_spacing = max(0.0, float(_style_value(style, "lineSpacing", "line_spacing", 0.15)))
    letter_spacing = float(_style_value(style, "letterSpacing", "letter_spacing", 0.0))
    return min_size, max_size, line_spacing, letter_spacing, padding, auto_fit


def _cluster_slices(
    regions: Sequence[Mapping[str, Any]],
    text: str,
) -> tuple[int, list[str], list[bool]]:
    direction = _region_direction(regions[0])
    prepared = (
        verticalize_punctuation(text).replace("\n", "")
        if direction == "vertical"
        else text.replace("\n", "")
    )
    min_size, max_size, line_spacing, letter_spacing, _, _ = _region_style_metrics(regions[0])
    chosen_size = min_size
    chosen_slices = [""] * len(regions)
    chosen_overflow = [True] * len(regions)
    for size in range(max_size, min_size - 1, -1):
        capacities: list[int] = []
        for region in regions:
            width = max(1, round(float(region["width"])))
            height = max(1, round(float(region["height"])))
            padding = _region_style_metrics(region)[4]
            if direction == "vertical":
                capacities.append(_vertical_capacity(width, height, size, line_spacing, padding))
            else:
                capacities.append(
                    _horizontal_capacity(width, height, size, line_spacing, letter_spacing, padding)
                )
        slices, overflow = _assign_cluster_slices(prepared, capacities)
        chosen_size, chosen_slices, chosen_overflow = size, slices, overflow
        if not any(overflow):
            return chosen_size, chosen_slices, chosen_overflow
    return chosen_size, chosen_slices, chosen_overflow


def _horizontal_lines(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    stroke_width: int,
    letter_spacing: float,
    auto_wrap: bool,
) -> list[str]:
    if not auto_wrap:
        return text.split("\n") or [""]
    lines: list[str] = []
    current = ""
    for character in text:
        if character == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + character
        if current and _text_width(draw, candidate, font, stroke_width, letter_spacing) > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    lines.append(current)
    return lines or [""]


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    stroke_width: int,
    letter_spacing: float,
) -> float:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return max(0, box[2] - box[0]) + max(0, len(text) - 1) * letter_spacing


def _draw_horizontal_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    stroke_width: int,
    stroke_fill: tuple[int, int, int, int],
    letter_spacing: float,
) -> None:
    if letter_spacing == 0:
        draw.text(
            position,
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        return
    x, y = position
    for character in text:
        draw.text(
            (x, y),
            character,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        x += float(draw.textlength(character, font=font)) + letter_spacing


def _horizontal_fit(
    text: str,
    *,
    width: int,
    height: int,
    font_path: str | Path | None,
    font_family: str | None,
    min_size: int,
    max_size: int,
    line_spacing: float,
    stroke_width: int,
    letter_spacing: float,
    auto_wrap: bool,
) -> tuple[ImageFont.ImageFont, list[str], bool, float]:
    measuring = ImageDraw.Draw(Image.new("L", (1, 1)))
    chosen: tuple[ImageFont.ImageFont, list[str], bool, float] | None = None
    for size in range(max_size, min_size - 1, -1):
        font = _load_font(font_path, size, font_family)
        lines = _horizontal_lines(
            text,
            font,
            width,
            measuring,
            stroke_width,
            letter_spacing,
            auto_wrap,
        )
        line_height = max(1, font.getbbox("国Ag")[3] - font.getbbox("国Ag")[1])
        total_height = line_height * len(lines) + line_height * line_spacing * (len(lines) - 1)
        fits = total_height <= height and all(
            _text_width(measuring, line, font, stroke_width, letter_spacing) <= width
            for line in lines
        )
        chosen = (font, lines, not fits, line_height)
        if fits:
            return chosen
    assert chosen is not None
    return chosen


def _vertical_fit(
    text: str,
    *,
    width: int,
    height: int,
    font_path: str | Path | None,
    font_family: str | None,
    min_size: int,
    max_size: int,
    line_spacing: float,
) -> tuple[ImageFont.ImageFont, list[list[str]], bool, int]:
    characters = list(verticalize_punctuation(text).replace("\n", ""))
    chosen: tuple[ImageFont.ImageFont, list[list[str]], bool, int] | None = None
    for size in range(max_size, min_size - 1, -1):
        font = _load_font(font_path, size, font_family)
        cell = max(1, math.ceil(size * (1 + line_spacing)))
        rows = max(1, height // cell)
        columns = [characters[index : index + rows] for index in range(0, len(characters), rows)]
        fits = max(1, len(columns)) * cell <= width
        chosen = (font, columns or [[]], not fits, cell)
        if fits:
            return chosen
    assert chosen is not None
    return chosen


def _draw_region(text: str, region: Mapping[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
    width = max(1, round(float(region["width"])))
    height = max(1, round(float(region["height"])))
    style = region.get("style") or {}
    padding = _effective_padding(width, height, max(0, int(style.get("padding", 4))))
    content_width = max(1, width - padding * 2)
    content_height = max(1, height - padding * 2)
    min_size = max(4, int(_style_value(style, "minFontSize", "min_font_size", 8)))
    max_size = max(
        min_size, int(_style_value(style, "fontSize", "font_size", min(width, height) // 3 or 12))
    )
    max_size = max(max_size, int(_style_value(style, "maxFontSize", "max_font_size", max_size)))
    if not bool(_style_value(style, "autoFit", "auto_fit", True)):
        min_size = max_size
    if "lineHeight" in style or "line_height" in style:
        line_spacing = max(
            0.0,
            float(_style_value(style, "lineHeight", "line_height", 1.15)) - 1.0,
        )
    else:
        line_spacing = max(
            0.0,
            float(_style_value(style, "lineSpacing", "line_spacing", 0.15)),
        )
    letter_spacing = float(_style_value(style, "letterSpacing", "letter_spacing", 0.0))
    stroke_width = max(0, int(_style_value(style, "strokeWidth", "stroke_width", 1)))
    fill = ImageColor.getcolor(
        str(style.get("color", style.get("fill", "#111111"))),
        "RGBA",
    )
    stroke_fill = ImageColor.getcolor(
        str(
            style.get(
                "strokeColor",
                _style_value(style, "strokeFill", "stroke_fill", "#ffffff"),
            )
        ),
        "RGBA",
    )
    font_path = _style_value(style, "fontPath", "font_path", None)
    font_family = str(_style_value(style, "fontFamily", "font_family", "system-ui"))
    align = str(style.get("align", "center"))
    if align not in {"start", "center", "end"}:
        align = "center"
    auto_wrap = bool(_style_value(style, "autoWrap", "auto_wrap", True))
    opacity = min(1.0, max(0.0, float(style.get("opacity", 1.0))))
    direction = region.get("direction", "vertical")
    background = style.get("backgroundColor", style.get("background_color"))
    layer = Image.new(
        "RGBA",
        (width, height),
        ImageColor.getcolor(str(background), "RGBA") if background else (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(layer)
    if direction == "horizontal":
        font, lines, overflow, line_height = _horizontal_fit(
            text,
            width=content_width,
            height=content_height,
            font_path=font_path,
            font_family=font_family,
            min_size=min_size,
            max_size=max_size,
            line_spacing=line_spacing,
            stroke_width=stroke_width,
            letter_spacing=letter_spacing,
            auto_wrap=auto_wrap,
        )
        total_height = line_height * len(lines) + line_height * line_spacing * (len(lines) - 1)
        y = padding + max(0, (content_height - total_height) / 2)
        for line in lines:
            line_width = _text_width(draw, line, font, stroke_width, letter_spacing)
            x = (
                padding
                if align == "start"
                else padding + max(0, content_width - line_width)
                if align == "end"
                else padding + max(0, (content_width - line_width) / 2)
            )
            _draw_horizontal_text(
                draw,
                (x, y),
                line,
                font=font,
                fill=fill,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
                letter_spacing=letter_spacing,
            )
            y += line_height * (1 + line_spacing)
        layout_lines: Any = lines
    else:
        font, columns, overflow, cell = _vertical_fit(
            text,
            width=content_width,
            height=content_height,
            font_path=font_path,
            font_family=font_family,
            min_size=min_size,
            max_size=max_size,
            line_spacing=line_spacing,
        )
        block_width = max(1, len(columns)) * cell
        x = (
            width - padding - cell
            if align == "start"
            else padding + block_width - cell
            if align == "end"
            else padding + max(0, (content_width + block_width) / 2) - cell
        )
        for column in columns:
            y = padding
            for character in column:
                box = draw.textbbox((0, 0), character, font=font, stroke_width=stroke_width)
                glyph_width = box[2] - box[0]
                if character in _HANGING_PUNCTUATION:
                    glyph_x = x + max(0, cell - glyph_width)
                else:
                    glyph_x = x + max(0, (cell - glyph_width) / 2)
                draw.text(
                    (glyph_x, y),
                    character,
                    font=font,
                    fill=fill,
                    stroke_width=stroke_width,
                    stroke_fill=stroke_fill,
                )
                y += cell
            x -= cell
        layout_lines = ["".join(column) for column in columns]
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda value: round(value * opacity)))
    font_size = getattr(font, "size", min_size)
    return layer, {
        "regionId": region.get("id"),
        "fontSize": font_size,
        "overflow": overflow,
        "direction": direction,
        "lines": layout_lines,
        "rotation": float(region.get("rotation", 0)),
        "align": align,
        "lineSpacing": line_spacing,
        "letterSpacing": letter_spacing,
        "fill": fill,
        "strokeFill": stroke_fill,
        "opacity": opacity,
    }


def _composite_region(
    canvas: Image.Image,
    region: Mapping[str, Any],
    layer: Image.Image,
) -> None:
    rotation = float(region.get("rotation", 0))
    if rotation:
        layer = layer.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
    center_x = float(region["x"]) + float(region["width"]) / 2
    center_y = float(region["y"]) + float(region["height"]) / 2
    position = (round(center_x - layer.width / 2), round(center_y - layer.height / 2))
    canvas.alpha_composite(layer, position)


def typeset_image(
    image: Path | Image.Image,
    regions: Sequence[Mapping[str, Any]],
) -> TypesetResult:
    if isinstance(image, Path):
        with Image.open(image) as source:
            canvas = source.convert("RGBA")
    else:
        canvas = image.convert("RGBA")
    layouts: list[dict[str, Any]] = []
    drawable = []
    for region in regions:
        if region.get("ignored"):
            continue
        text = str(region.get("translationText", region.get("translation_text", "")))
        if not text:
            continue
        drawable.append(region)
    for group in cluster_fragment_regions(drawable):
        if len(group) == 1:
            region = group[0]
            text = str(region.get("translationText", region.get("translation_text", "")))
            layer, layout = _draw_region(text, region)
            _composite_region(canvas, region, layer)
            layouts.append(layout)
            continue
        shared = _cluster_text(group)
        font_size, slices, overflow_flags = _cluster_slices(group, shared)
        for region, slice_text, overflow in zip(group, slices, overflow_flags, strict=True):
            if not slice_text:
                continue
            style = dict(region.get("style") or {})
            style.update(
                {
                    "autoFit": False,
                    "fontSize": font_size,
                    "minFontSize": font_size,
                }
            )
            layer, layout = _draw_region(slice_text, {**dict(region), "style": style})
            layout["overflow"] = overflow
            _composite_region(canvas, region, layer)
            layouts.append(layout)
    return TypesetResult(image=canvas, layouts=layouts)
