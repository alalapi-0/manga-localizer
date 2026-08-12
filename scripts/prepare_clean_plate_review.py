from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_REPAIR = {
    "maskMode": "text",
    "maskPadding": 4,
    "dilation": 2,
    "feather": 2,
    "method": "telea",
    "radius": 3,
    "fillColor": "#ffffff",
}


class PreparationError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a private, OCR-text-free visual review pack for a clean-plate run. "
            "The output must be inside a Git-ignored path."
        )
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="Immutable source image directory"
    )
    parser.add_argument(
        "--project-db",
        type=Path,
        action="append",
        required=True,
        help=(
            "Existing private project.sqlite3 containing detector candidates; "
            "repeat to merge complementary detector runs"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New, empty, Git-ignored review-pack directory",
    )
    parser.add_argument("--expected-count", type=int, default=130)
    parser.add_argument("--images-per-sheet", type=int, default=6)
    parser.add_argument("--region-crops-per-sheet", type=int, default=20)
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _git_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise PreparationError(
            "The output must be inside a Git worktree with ignore rules"
        )
    return Path(result.stdout.strip()).resolve()


def require_ignored_empty_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    probe_parent = resolved if resolved.exists() else resolved.parent
    probe_parent = probe_parent.resolve(strict=True)
    git_root = _git_root(probe_parent)
    try:
        resolved.relative_to(git_root)
    except ValueError as error:
        raise PreparationError(
            "The output must stay inside the selected repository"
        ) from error
    result = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "--quiet", str(resolved)],
        check=False,
    )
    if result.returncode:
        raise PreparationError(
            "The output path is not covered by the repository ignore rules"
        )
    if resolved.exists() and any(resolved.iterdir()):
        raise PreparationError("The output directory already exists and is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def image_files(input_root: Path) -> list[Path]:
    files = sorted(
        (
            path
            for path in input_root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        ),
        key=lambda path: path.relative_to(input_root).as_posix().casefold(),
    )
    if not files:
        raise PreparationError("The input directory contains no supported images")
    return files


def _decode_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _load_candidate_database(
    database_path: Path,
    *,
    source_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    database = database_path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        images = {
            str(row["relative_path"]): {
                "databaseChecksum": str(row["checksum"]),
                "databaseWidth": int(row["width"]),
                "databaseHeight": int(row["height"]),
                "imageDatabaseRefs": [{"sourceId": source_id, "id": str(row["id"])}],
            }
            for row in connection.execute(
                "SELECT id, relative_path, checksum, width, height FROM images"
            )
        }
        candidates: dict[str, list[dict[str, Any]]] = {key: [] for key in images}
        rows = connection.execute(
            """
            SELECT i.relative_path,
                   r.id,
                   r.x,
                   r.y,
                   r.width,
                   r.height,
                   r.rotation,
                   r.confidence,
                   r.ignored,
                   r.confirmed,
                   r.repair,
                   CASE WHEN trim(r.source_text) <> '' THEN 1 ELSE 0 END AS ocr_nonempty
              FROM text_regions AS r
              JOIN images AS i ON i.id = r.image_id
             ORDER BY i.relative_path, r.reading_order, r.created_at
            """
        )
        for row in rows:
            relative_path = str(row["relative_path"])
            repair = _decode_json_object(row["repair"])
            candidate = {
                "databaseRegionRefs": [{"sourceId": source_id, "id": str(row["id"])}],
                "candidateSourceIds": [source_id],
                "geometry": {
                    "x": round(float(row["x"]), 3),
                    "y": round(float(row["y"]), 3),
                    "width": round(float(row["width"]), 3),
                    "height": round(float(row["height"]), 3),
                    "rotation": round(float(row["rotation"]), 3),
                },
                "polygon": repair.get("maskPolygon"),
                "confidence": (
                    round(float(row["confidence"]), 4)
                    if row["confidence"] is not None
                    else None
                ),
                "ocrNonempty": bool(row["ocr_nonempty"]),
                "detectorGenerated": bool(repair.get("detectorGenerated", False)),
                "priorIgnored": bool(row["ignored"]),
                "priorConfirmed": bool(row["confirmed"]),
            }
            candidates.setdefault(relative_path, []).append(candidate)
    finally:
        connection.close()
    return images, candidates


def _box(candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    geometry = candidate["geometry"]
    left = float(geometry["x"])
    top = float(geometry["y"])
    return (
        left,
        top,
        left + float(geometry["width"]),
        top + float(geometry["height"]),
    )


def _overlap(candidate: dict[str, Any], other: dict[str, Any]) -> tuple[float, float]:
    left, top, right, bottom = _box(candidate)
    other_left, other_top, other_right, other_bottom = _box(other)
    intersection_width = max(0.0, min(right, other_right) - max(left, other_left))
    intersection_height = max(0.0, min(bottom, other_bottom) - max(top, other_top))
    intersection = intersection_width * intersection_height
    area = max(0.0, right - left) * max(0.0, bottom - top)
    other_area = max(0.0, other_right - other_left) * max(0.0, other_bottom - other_top)
    union = area + other_area - intersection
    return (
        intersection / union if union else 0.0,
        intersection / min(area, other_area) if min(area, other_area) else 0.0,
    )


def _merge_candidate(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    left, top, right, bottom = _box(target)
    other_left, other_top, other_right, other_bottom = _box(incoming)
    merged_left = min(left, other_left)
    merged_top = min(top, other_top)
    merged_right = max(right, other_right)
    merged_bottom = max(bottom, other_bottom)
    target["geometry"] = {
        "x": round(merged_left, 3),
        "y": round(merged_top, 3),
        "width": round(merged_right - merged_left, 3),
        "height": round(merged_bottom - merged_top, 3),
        "rotation": 0.0,
    }
    if target.get("polygon") != incoming.get("polygon"):
        target["polygon"] = None
    target["databaseRegionRefs"].extend(incoming["databaseRegionRefs"])
    target["candidateSourceIds"] = sorted(
        set(target["candidateSourceIds"]) | set(incoming["candidateSourceIds"])
    )
    confidences = [
        value
        for value in (target.get("confidence"), incoming.get("confidence"))
        if value is not None
    ]
    target["confidence"] = max(confidences) if confidences else None
    target["ocrNonempty"] = bool(target["ocrNonempty"] or incoming["ocrNonempty"])
    target["detectorGenerated"] = bool(
        target["detectorGenerated"] or incoming["detectorGenerated"]
    )
    target["priorIgnored"] = bool(target["priorIgnored"] and incoming["priorIgnored"])
    target["priorConfirmed"] = bool(
        target["priorConfirmed"] or incoming["priorConfirmed"]
    )


def _deduplicate_candidates(
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        best_index: int | None = None
        best_score = 0.0
        for index, existing in enumerate(merged):
            intersection_over_union, containment = _overlap(existing, candidate)
            score = max(intersection_over_union, containment)
            if (
                intersection_over_union >= 0.45 or containment >= 0.78
            ) and score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            merged.append(candidate)
        else:
            _merge_candidate(merged[best_index], candidate)
    return sorted(
        merged,
        key=lambda candidate: (
            float(candidate["geometry"]["y"]),
            float(candidate["geometry"]["x"]),
        ),
    )


def load_candidates(
    database_paths: Sequence[Path],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    if not database_paths:
        raise PreparationError("At least one candidate project database is required")
    merged_images: dict[str, dict[str, Any]] | None = None
    combined_candidates: dict[str, list[dict[str, Any]]] = {}
    for index, database_path in enumerate(database_paths, start=1):
        source_id = f"detector-run-{index:02d}"
        images, candidates = _load_candidate_database(
            database_path, source_id=source_id
        )
        if merged_images is None:
            merged_images = images
        else:
            if set(merged_images) != set(images):
                raise PreparationError(
                    "Candidate projects do not contain the same image set"
                )
            for relative_path, image in images.items():
                merged = merged_images[relative_path]
                if (
                    merged["databaseChecksum"] != image["databaseChecksum"]
                    or merged["databaseWidth"] != image["databaseWidth"]
                    or merged["databaseHeight"] != image["databaseHeight"]
                ):
                    raise PreparationError(
                        "Candidate projects disagree about an immutable source"
                    )
                merged["imageDatabaseRefs"].extend(image["imageDatabaseRefs"])
        for relative_path, image_candidates in candidates.items():
            combined_candidates.setdefault(relative_path, []).extend(image_candidates)
    assert merged_images is not None
    deduplicated = {
        relative_path: _deduplicate_candidates(image_candidates)
        for relative_path, image_candidates in combined_candidates.items()
    }
    return merged_images, deduplicated


def _load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        opened.load()
        return ImageOps.exif_transpose(opened).convert("RGB")


def _font(size: int = 24) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("Arial.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _fit(
    image: Image.Image, size: tuple[int, int]
) -> tuple[Image.Image, float, int, int]:
    target_width, target_height = size
    scale = min(target_width / image.width, target_height / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (target_width - resized.width) // 2
    top = (target_height - resized.height) // 2
    return resized, scale, left, top


def _candidate_outline(
    draw: ImageDraw.ImageDraw,
    candidate: dict[str, Any],
    *,
    scale: float,
    offset_x: int,
    offset_y: int,
) -> None:
    polygon = candidate.get("polygon")
    if isinstance(polygon, list) and len(polygon) >= 3:
        points = [
            (offset_x + float(point[0]) * scale, offset_y + float(point[1]) * scale)
            for point in polygon
            if isinstance(point, Sequence) and len(point) >= 2
        ]
        if len(points) >= 3:
            draw.line([*points, points[0]], fill=(255, 48, 48), width=3, joint="curve")
            return
    geometry = candidate["geometry"]
    left = offset_x + float(geometry["x"]) * scale
    top = offset_y + float(geometry["y"]) * scale
    right = left + float(geometry["width"]) * scale
    bottom = top + float(geometry["height"]) * scale
    draw.rectangle((left, top, right, bottom), outline=(255, 48, 48), width=3)


def _candidate_points(
    candidate: dict[str, Any],
) -> tuple[list[tuple[float, float]] | None, tuple[float, float, float, float]]:
    polygon = candidate.get("polygon")
    if isinstance(polygon, list) and len(polygon) >= 3:
        points = [
            (float(point[0]), float(point[1]))
            for point in polygon
            if isinstance(point, Sequence) and len(point) >= 2
        ]
        if len(points) >= 3:
            columns = [point[0] for point in points]
            rows = [point[1] for point in points]
            return points, (min(columns), min(rows), max(columns), max(rows))
    return None, _box(candidate)


def make_numbered_page_overlays(
    records: Sequence[dict[str, Any]],
    *,
    input_root: Path,
    output: Path,
) -> tuple[int, int]:
    """Save anonymous full-resolution candidate and coordinate review pages."""
    numbered_root = output / "numbered-pages"
    coordinate_root = output / "coordinate-pages"
    numbered_root.mkdir()
    coordinate_root.mkdir()
    for record in records:
        source = _load_rgb(input_root / record["sourceRelativePath"])
        line_width = max(2, round(max(source.size) / 700))
        font = _font(max(14, min(28, round(source.width / 35))))

        numbered = source.copy()
        numbered_draw = ImageDraw.Draw(numbered)
        for candidate in record["regions"]:
            points, bounds = _candidate_points(candidate)
            if points:
                numbered_draw.line(
                    [*points, points[0]],
                    fill=(255, 32, 32),
                    width=line_width,
                    joint="curve",
                )
            else:
                numbered_draw.rectangle(
                    bounds,
                    outline=(255, 32, 32),
                    width=line_width,
                )
            label = str(candidate["regionId"]).removeprefix("region-")
            label_box = numbered_draw.textbbox((0, 0), label, font=font)
            label_width = label_box[2] - label_box[0] + 8
            label_height = label_box[3] - label_box[1] + 6
            label_left = max(0, min(source.width - label_width, round(bounds[0])))
            label_top = max(0, min(source.height - label_height, round(bounds[1])))
            numbered_draw.rectangle(
                (
                    label_left,
                    label_top,
                    label_left + label_width,
                    label_top + label_height,
                ),
                fill=(210, 0, 0),
            )
            numbered_draw.text(
                (label_left + 4, label_top + 2),
                label,
                fill="white",
                font=font,
            )
        numbered.save(numbered_root / f"{record['imageId']}.png", optimize=True)

        coordinates = source.convert("RGBA")
        grid = Image.new("RGBA", coordinates.size, (0, 0, 0, 0))
        grid_draw = ImageDraw.Draw(grid)
        grid_font = _font(max(12, min(24, round(source.width / 45))))
        raw_step = max(source.size) / 10
        step = max(50, round(raw_step / 50) * 50)
        grid_width = max(1, line_width // 2)
        for x in range(step, source.width, step):
            grid_draw.line(
                (x, 0, x, source.height), fill=(0, 170, 255, 92), width=grid_width
            )
            grid_draw.text((x + 3, 3), str(x), fill=(0, 100, 180, 255), font=grid_font)
        for y in range(step, source.height, step):
            grid_draw.line(
                (0, y, source.width, y), fill=(0, 170, 255, 92), width=grid_width
            )
            grid_draw.text((3, y + 3), str(y), fill=(0, 100, 180, 255), font=grid_font)
        Image.alpha_composite(coordinates, grid).convert("RGB").save(
            coordinate_root / f"{record['imageId']}.png",
            optimize=True,
        )
        source.close()
        numbered.close()
        coordinates.close()
        grid.close()
    return len(records), len(records)


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def make_page_sheets(
    records: Sequence[dict[str, Any]],
    *,
    input_root: Path,
    output: Path,
    images_per_sheet: int,
    overlay: bool,
) -> int:
    if images_per_sheet <= 0:
        raise PreparationError("images-per-sheet must be positive")
    columns = 2
    rows = max(1, (images_per_sheet + columns - 1) // columns)
    cell_width, cell_height = 960, 720
    label_height = 42
    font = _font(24)
    sheet_count = 0
    prefix = "candidate-overlay" if overlay else "source"
    for sheet_count, group in enumerate(_chunks(records, images_per_sheet), start=1):
        canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "#202020")
        draw = ImageDraw.Draw(canvas)
        for index, record in enumerate(group):
            column = index % columns
            row = index // columns
            cell_left = column * cell_width
            cell_top = row * cell_height
            source = _load_rgb(input_root / record["sourceRelativePath"])
            fitted, scale, image_left, image_top = _fit(
                source,
                (cell_width - 16, cell_height - label_height - 16),
            )
            paste_x = cell_left + 8 + image_left
            paste_y = cell_top + label_height + 8 + image_top
            canvas.paste(fitted, (paste_x, paste_y))
            draw.text(
                (cell_left + 12, cell_top + 8),
                f"{record['imageId']}  candidates={len(record['regions'])}",
                fill="white",
                font=font,
            )
            if overlay:
                for candidate in record["regions"]:
                    _candidate_outline(
                        draw,
                        candidate,
                        scale=scale,
                        offset_x=paste_x,
                        offset_y=paste_y,
                    )
            draw.rectangle(
                (
                    cell_left,
                    cell_top,
                    cell_left + cell_width - 1,
                    cell_top + cell_height - 1,
                ),
                outline="#606060",
                width=1,
            )
        canvas.save(output / f"{prefix}-contact-{sheet_count:03d}.png", optimize=True)
    return sheet_count


def _expanded_crop_box(
    geometry: dict[str, Any], image_size: tuple[int, int], context_ratio: float = 0.7
) -> tuple[int, int, int, int]:
    x = float(geometry["x"])
    y = float(geometry["y"])
    width = float(geometry["width"])
    height = float(geometry["height"])
    context = max(16.0, min(width, height) * context_ratio)
    image_width, image_height = image_size
    return (
        max(0, int(x - context)),
        max(0, int(y - context)),
        min(image_width, int(x + width + context + 0.999)),
        min(image_height, int(y + height + context + 0.999)),
    )


def make_region_crop_sheets(
    records: Sequence[dict[str, Any]],
    *,
    input_root: Path,
    output: Path,
    crops_per_sheet: int,
) -> tuple[int, int]:
    if crops_per_sheet <= 0:
        raise PreparationError("region-crops-per-sheet must be positive")
    crop_records = [
        (record, region) for record in records for region in record["regions"]
    ]
    columns = 4
    rows = max(1, (crops_per_sheet + columns - 1) // columns)
    cell_width, cell_height = 480, 360
    label_height = 38
    font = _font(20)
    sheet_count = 0
    source_cache: dict[str, Image.Image] = {}
    try:
        for sheet_count, group in enumerate(
            _chunks(crop_records, crops_per_sheet), start=1
        ):
            canvas = Image.new(
                "RGB", (columns * cell_width, rows * cell_height), "#202020"
            )
            draw = ImageDraw.Draw(canvas)
            for index, (record, region) in enumerate(group):
                column = index % columns
                row = index // columns
                cell_left = column * cell_width
                cell_top = row * cell_height
                relative_path = record["sourceRelativePath"]
                source = source_cache.get(relative_path)
                if source is None:
                    source = _load_rgb(input_root / relative_path)
                    source_cache[relative_path] = source
                crop_box = _expanded_crop_box(region["geometry"], source.size)
                crop = source.crop(crop_box)
                fitted, scale, image_left, image_top = _fit(
                    crop,
                    (cell_width - 12, cell_height - label_height - 12),
                )
                paste_x = cell_left + 6 + image_left
                paste_y = cell_top + label_height + 6 + image_top
                canvas.paste(fitted, (paste_x, paste_y))
                recognized = "ocr+" if region["ocrNonempty"] else "ocr-"
                confidence = region["confidence"]
                confidence_label = "n/a" if confidence is None else f"{confidence:.2f}"
                draw.text(
                    (cell_left + 8, cell_top + 7),
                    f"{record['imageId']}/{region['regionId']} {recognized} c={confidence_label}",
                    fill="white",
                    font=font,
                )
                shifted = {
                    **region,
                    "polygon": (
                        [
                            [
                                float(point[0]) - crop_box[0],
                                float(point[1]) - crop_box[1],
                            ]
                            for point in region["polygon"]
                        ]
                        if region.get("polygon")
                        else None
                    ),
                    "geometry": {
                        **region["geometry"],
                        "x": float(region["geometry"]["x"]) - crop_box[0],
                        "y": float(region["geometry"]["y"]) - crop_box[1],
                    },
                }
                _candidate_outline(
                    draw,
                    shifted,
                    scale=scale,
                    offset_x=paste_x,
                    offset_y=paste_y,
                )
                draw.rectangle(
                    (
                        cell_left,
                        cell_top,
                        cell_left + cell_width - 1,
                        cell_top + cell_height - 1,
                    ),
                    outline="#606060",
                    width=1,
                )
            canvas.save(output / f"region-crops-{sheet_count:03d}.png", optimize=True)
    finally:
        for source in source_cache.values():
            source.close()
    return sheet_count, len(crop_records)


def build_records(
    input_root: Path,
    sources: Sequence[Path],
    database_images: dict[str, dict[str, Any]],
    candidates: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    source_relative_paths = {
        path.relative_to(input_root).as_posix() for path in sources
    }
    database_relative_paths = set(database_images)
    if source_relative_paths != database_relative_paths:
        raise PreparationError(
            "The input set and candidate project do not contain the same images"
        )
    for image_index, source_path in enumerate(sources, start=1):
        relative_path = source_path.relative_to(input_root).as_posix()
        database_image = database_images[relative_path]
        checksum = file_sha256(source_path)
        with Image.open(source_path) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        if checksum != database_image["databaseChecksum"]:
            raise PreparationError(
                "A source checksum differs from the immutable project copy"
            )
        if [width, height] != [
            database_image["databaseWidth"],
            database_image["databaseHeight"],
        ]:
            raise PreparationError(
                "A source dimension differs from the candidate project"
            )
        image_id = f"page-{image_index:04d}"
        regions: list[dict[str, Any]] = []
        for region_index, candidate in enumerate(
            candidates.get(relative_path, []), start=1
        ):
            regions.append(
                {
                    "regionId": f"region-{region_index:04d}",
                    **candidate,
                    "decision": "pending",
                    "classification": "unknown",
                    "backgroundClass": "unknown",
                    "repair": dict(DEFAULT_REPAIR),
                    "maskReview": "pending",
                    "outputReview": "pending",
                    "attempts": [],
                    "notes": [],
                }
            )
        records.append(
            {
                "imageId": image_id,
                "sourceRelativePath": relative_path,
                "sourceChecksum": checksum,
                "width": width,
                "height": height,
                "databaseImageRefs": database_image["imageDatabaseRefs"],
                "regions": regions,
                "detectionReview": "pending",
                "maskReview": "pending",
                "outputReview": "pending",
                "reviewStatus": "pending",
                "retryCount": 0,
                "unresolvedRegions": [],
                "final": None,
            }
        )
    return records


def run(args: argparse.Namespace) -> int:
    if args.expected_count <= 0:
        raise PreparationError("expected-count must be positive")
    input_root = args.input.expanduser().resolve(strict=True)
    if not input_root.is_dir():
        raise PreparationError("The input path must be a directory")
    sources = image_files(input_root)
    if len(sources) != args.expected_count:
        raise PreparationError(
            f"Expected {args.expected_count} inputs but found {len(sources)}"
        )
    database_images, candidates = load_candidates(args.project_db)
    records = build_records(input_root, sources, database_images, candidates)
    output = require_ignored_empty_output(args.output)
    contacts = output / "contacts"
    contacts.mkdir()
    source_sheets = make_page_sheets(
        records,
        input_root=input_root,
        output=contacts,
        images_per_sheet=args.images_per_sheet,
        overlay=False,
    )
    overlay_sheets = make_page_sheets(
        records,
        input_root=input_root,
        output=contacts,
        images_per_sheet=args.images_per_sheet,
        overlay=True,
    )
    region_sheets, region_count = make_region_crop_sheets(
        records,
        input_root=input_root,
        output=contacts,
        crops_per_sheet=args.region_crops_per_sheet,
    )
    numbered_pages, coordinate_pages = make_numbered_page_overlays(
        records,
        input_root=input_root,
        output=output,
    )
    manifest = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "createdAt": datetime.now(UTC).isoformat(),
        "purpose": "private-clean-plate-visual-review",
        "privacy": {
            "gitIgnored": True,
            "ocrTextStored": False,
            "absolutePathsStored": False,
        },
        "expectedImages": args.expected_count,
        "candidateSourceCount": len(args.project_db),
        "images": records,
    }
    summary = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "imageCount": len(records),
        "candidateRegionCount": region_count,
        "candidateSourceCount": len(args.project_db),
        "zeroCandidateImageCount": sum(not record["regions"] for record in records),
        "sourceContactSheets": source_sheets,
        "overlayContactSheets": overlay_sheets,
        "regionCropSheets": region_sheets,
        "numberedCandidatePages": numbered_pages,
        "coordinatePages": coordinate_pages,
        "allDetectionReviewsPending": True,
        "allSourceChecksumsMatched": True,
    }
    atomic_json(output / "review-manifest.json", manifest)
    atomic_json(output / "summary.json", summary)
    print(
        "[review-pack] "
        f"images={len(records)} candidates={region_count} "
        f"source_sheets={source_sheets} overlay_sheets={overlay_sheets} "
        f"region_sheets={region_sheets}",
        flush=True,
    )
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (OSError, PreparationError, sqlite3.DatabaseError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
