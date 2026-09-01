from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from manga_localizer.config import Settings
from manga_localizer.imaging.inpainting import inpaint
from manga_localizer.imaging.lineart_inpaint import (
    CANDIDATE_LINEART,
    CANDIDATE_OPENCV_NS,
    CANDIDATE_OPENCV_TELEA,
    CANDIDATE_PRIMARY,
    build_inpaint_candidates,
    candidate_metrics,
)
from manga_localizer.model_bundle import apply_model_bundle
from manga_localizer.providers.inpainting_lama import (
    LaMaONNXInpaintingProvider,
    LaMaUnavailable,
)
from PIL import Image, ImageDraw

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class CompareError(RuntimeError):
    pass


def require_ignored_empty_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    probe = resolved
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    probe = probe.resolve(strict=True)
    root_result = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if root_result.returncode:
        raise CompareError("Output must be inside a Git worktree")
    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        resolved.relative_to(git_root)
    except ValueError as error:
        raise CompareError("Output must stay inside the selected repository") from error
    ignored = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "--quiet", str(resolved)],
        check=False,
    )
    if ignored.returncode:
        raise CompareError("Output is not covered by repository ignore rules")
    if resolved.exists() and any(resolved.iterdir()):
        raise CompareError("Output directory is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def synthetic_lineart_page() -> tuple[Image.Image, np.ndarray]:
    pixels = np.full((256, 256), 236, dtype=np.uint8)
    for y in range(16, 240, 10):
        pixels[y : y + 1, 12:244] = 28 if y % 20 == 16 else 170
    pixels[118:138, :] = 18
    pixels[118:138, 96:160] = 236
    draw = Image.fromarray(pixels, mode="L")
    painter = ImageDraw.Draw(draw)
    painter.rectangle((88, 72, 168, 184), fill=24)
    painter.rectangle((96, 80, 160, 176), fill=236)
    painter.line((108, 92, 148, 164), fill=24, width=3)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[70:186, 86:170] = 255
    return draw.convert("RGB"), mask


def write_contact_sheet(
    panels: list[tuple[str, Image.Image]],
    destination: Path,
    *,
    crop: tuple[int, int, int, int],
) -> None:
    cropped = [(label, image.crop(crop).convert("RGB")) for label, image in panels]
    panel_w = max(image.width for _, image in cropped)
    panel_h = max(image.height for _, image in cropped)
    columns = min(3, len(cropped))
    rows = (len(cropped) + columns - 1) // columns
    sheet = Image.new("RGB", (panel_w * columns + 16, panel_h * rows + 28), "#111111")
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(cropped):
        column = index % columns
        row = index // columns
        left = 8 + column * panel_w
        top = 20 + row * panel_h
        sheet.paste(image, (left, top))
        draw.text((left, top - 14), label, fill="#f4f4f4")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG")


def laplacian_variance(image: Image.Image, mask: np.ndarray) -> float:
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    inside = mask > 0
    if int(np.count_nonzero(inside)) < 16:
        return 0.0
    return float(cv2.Laplacian(gray, cv2.CV_32F)[inside].var())


def compare_page(
    source: Image.Image,
    mask: np.ndarray,
    *,
    page_id: str,
    output: Path,
    lama: LaMaONNXInpaintingProvider | None,
) -> dict[str, Any]:
    primary = source.copy()
    used_lama = False
    if lama is not None:
        try:
            primary = lama.inpaint(source, mask, feather=0)
            used_lama = True
        except (LaMaUnavailable, OSError, ValueError):
            primary = inpaint(source, mask, method="telea")
    else:
        primary = inpaint(source, mask, method="telea")
    candidates = build_inpaint_candidates(source, mask, primary)
    page_dir = output / "pages" / page_id
    page_dir.mkdir(parents=True, exist_ok=True)
    source.save(page_dir / "source.png", format="PNG")
    Image.fromarray(mask, mode="L").save(page_dir / "mask.png", format="PNG")
    records: list[dict[str, Any]] = []
    panels = [("source", source)]
    for item in candidates:
        encoded_path = page_dir / f"{item['id']}.png"
        item["image"].save(encoded_path, format="PNG")
        metrics = candidate_metrics(source, item["image"], mask)
        records.append(
            {
                "id": item["id"],
                "label": item["label"],
                "changedPixelsOutsideMask": metrics["changedPixelsOutsideMask"],
                "meanAbsDeltaInsideMask": metrics["meanAbsDeltaInsideMask"],
                "chromaInsideMask": metrics["chromaInsideMask"],
                "anomalies": metrics["anomalies"],
                "laplacianVarInsideMask": round(
                    laplacian_variance(item["image"], mask), 3
                ),
            }
        )
        panels.append((item["id"], item["image"]))
    rows, columns = np.nonzero(mask > 0)
    crop = (
        max(0, int(columns.min()) - 12),
        max(0, int(rows.min()) - 12),
        min(source.width, int(columns.max()) + 13),
        min(source.height, int(rows.max()) + 13),
    )
    write_contact_sheet(panels, output / "contact-sheets" / f"{page_id}.png", crop=crop)
    return {
        "pageId": page_id,
        "usedLama": used_lama,
        "defaultCandidate": CANDIDATE_LINEART if used_lama else CANDIDATE_PRIMARY,
        "maskCoverage": round(float(np.mean(mask > 0)), 4),
        "candidates": records,
    }


def collect_images(paths: list[Path]) -> list[Path]:
    collected: list[Path] = []
    for raw in paths:
        path = raw.expanduser().resolve()
        if path.is_file():
            collected.append(path)
            continue
        if not path.is_dir():
            raise CompareError(f"Input path does not exist: {path.name}")
        collected.extend(
            sorted(
                child
                for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES
            )
        )
    if not collected:
        raise CompareError("No input images were found")
    return collected


def load_mask(path: Path, size: tuple[int, int]) -> np.ndarray:
    with Image.open(path) as opened:
        mask = opened.convert("L").resize(size, Image.Resampling.NEAREST)
    return np.asarray(mask, dtype=np.uint8)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare local inpainting candidates for line-art restoration. "
            "Writes ignored-directory metrics and contact sheets without OCR text "
            "or absolute personal paths."
        )
    )
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--input", nargs="*", type=Path, default=[])
    parser.add_argument("--mask", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lama-model", type=Path)
    parser.add_argument("--label", default="inpaint-compare")
    return parser.parse_args()


def resolve_lama(args: argparse.Namespace) -> LaMaONNXInpaintingProvider | None:
    candidates = [args.lama_model.expanduser()] if args.lama_model is not None else []
    settings = Settings()
    if settings.model_bundle is not None:
        settings, _ = apply_model_bundle(settings)
        candidates.append(settings.lama_inpainting_model_path)
    for path in candidates:
        if path.is_file():
            provider = LaMaONNXInpaintingProvider(path)
            if provider.health_check()["available"]:
                return provider
    return None


def run(args: argparse.Namespace) -> int:
    if not args.synthetic and not args.input:
        raise CompareError("Provide --synthetic or --input")
    output = require_ignored_empty_output(args.output)
    lama = resolve_lama(args)
    pages: list[dict[str, Any]] = []
    if args.synthetic:
        source, mask = synthetic_lineart_page()
        pages.append(
            compare_page(
                source,
                mask,
                page_id="page-0001",
                output=output,
                lama=lama,
            )
        )
    for index, path in enumerate(
        collect_images(args.input) if args.input else [], start=1
    ):
        with Image.open(path) as opened:
            opened.load()
            source = opened.convert("RGB")
        if args.mask is None:
            raise CompareError("Private image comparison requires --mask")
        mask = load_mask(args.mask, source.size)
        pages.append(
            compare_page(
                source,
                mask,
                page_id=f"page-{index:04d}",
                output=output,
                lama=lama,
            )
        )
    outside_failures = sum(
        item["changedPixelsOutsideMask"]
        for page in pages
        for item in page["candidates"]
    )
    report = {
        "schemaVersion": 1,
        "label": args.label,
        "createdAt": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "privacy": {
            "gitIgnored": True,
            "ocrTextStored": False,
            "absolutePathsStored": False,
            "imageNamesStored": False,
        },
        "configuration": {
            "synthetic": args.synthetic,
            "lamaAvailable": lama is not None,
            "candidates": [
                CANDIDATE_PRIMARY,
                CANDIDATE_OPENCV_NS,
                CANDIDATE_OPENCV_TELEA,
                CANDIDATE_LINEART,
            ],
        },
        "aggregate": {
            "pages": len(pages),
            "maskOutsideChangeFailures": outside_failures,
            "anomalyCounts": {},
        },
        "pages": pages,
    }
    counts: dict[str, int] = {}
    for page in pages:
        for item in page["candidates"]:
            for flag in item["anomalies"]:
                counts[flag] = counts.get(flag, 0) + 1
    report["aggregate"]["anomalyCounts"] = counts
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "pages": len(pages),
                "maskOutsideChangeFailures": outside_failures,
                "lamaAvailable": lama is not None,
                "anomalyCounts": counts,
            },
            ensure_ascii=True,
        )
    )
    return 0 if outside_failures == 0 else 1


def main() -> int:
    try:
        return run(parse_args())
    except CompareError as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
