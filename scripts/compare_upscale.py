from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from manga_localizer.imaging import (
    OpenCVPillowPreprocessProvider,
    RealESRGANONNXPreprocessProvider,
)
from manga_localizer.imaging.preprocessing import PreprocessUnavailable
from PIL import Image, ImageDraw

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class CompareError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_ignored_empty_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    probe = resolved if resolved.exists() else resolved.parent
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


def collect_images(paths: list[Path]) -> list[Path]:
    collected: list[Path] = []
    for raw in paths:
        path = raw.expanduser().resolve()
        if path.is_file():
            collected.append(path)
            continue
        if not path.is_dir():
            raise CompareError(f"Input path does not exist: {path}")
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


def laplacian_variance(image: Image.Image) -> float:
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    if min(gray.shape) < 3:
        return 0.0
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


def sobel_mean(image: Image.Image) -> float:
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    if min(gray.shape) < 3:
        return 0.0
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(np.hypot(grad_x, grad_y)))


def mean_abs_diff(left: Image.Image, right: Image.Image) -> float:
    left_pixels = np.asarray(left.convert("RGB"), dtype=np.int16)
    right_pixels = np.asarray(right.convert("RGB"), dtype=np.int16)
    return float(np.mean(np.abs(left_pixels - right_pixels)))


def unique_colors(image: Image.Image) -> int:
    pixels = np.asarray(image.convert("RGB")).reshape(-1, 3)
    return int(np.unique(pixels, axis=0).shape[0])


def _scale_box(
    box: tuple[int, int, int, int],
    source: Image.Image,
    destination: Image.Image,
) -> tuple[int, int, int, int]:
    scale_x = destination.width / source.width
    scale_y = destination.height / source.height
    left, top, right, bottom = box
    return (
        int(left * scale_x),
        int(top * scale_y),
        int(right * scale_x),
        int(bottom * scale_y),
    )


def write_contact_sheet(
    source: Image.Image,
    classic: Image.Image,
    ai: Image.Image,
    destination: Path,
    *,
    crop: int = 160,
) -> None:
    width, height = source.size
    box = (
        max(0, width // 2 - crop // 2),
        max(0, height // 2 - crop // 2),
        min(width, width // 2 + crop // 2),
        min(height, height // 2 + crop // 2),
    )
    panels = [
        ("source", source.crop(box)),
        ("classic", classic.crop(_scale_box(box, source, classic))),
        ("ai", ai.crop(_scale_box(box, source, ai))),
    ]
    panel_w = max(image.width for _, image in panels)
    panel_h = max(image.height for _, image in panels)
    sheet = Image.new("RGB", (panel_w * 3 + 24, panel_h + 36), "#111111")
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(panels):
        left = 8 + index * (panel_w + 4)
        sheet.paste(image.convert("RGB"), (left, 24))
        draw.text((left, 6), label, fill="#f4f4f4")
    sheet.save(destination, format="PNG")


def compare_one(
    path: Path,
    *,
    classic_provider: OpenCVPillowPreprocessProvider,
    ai_provider: RealESRGANONNXPreprocessProvider,
    factor: int,
    output: Path,
) -> dict[str, Any]:
    source_checksum = file_sha256(path)
    with Image.open(path) as opened:
        opened.load()
        source = opened.copy()
    source_before = path.read_bytes()
    started = time.monotonic()
    classic = classic_provider.preprocess(
        source,
        profile="off",
        enable_upscale=True,
        upscale_factor=factor,
    )
    classic_seconds = time.monotonic() - started
    started = time.monotonic()
    ai = ai_provider.preprocess(
        source,
        profile="off",
        enable_upscale=True,
        upscale_factor=factor,
    )
    ai_seconds = time.monotonic() - started
    if path.read_bytes() != source_before:
        raise CompareError(f"Source bytes changed: {path.name}")
    if file_sha256(path) != source_checksum:
        raise CompareError(f"Source checksum changed: {path.name}")
    if classic.processed_size != ai.processed_size:
        raise CompareError("Classic and AI output sizes differ")
    expected = (source.width * factor, source.height * factor)
    if classic.processed_size != expected:
        raise CompareError("Output size does not match the requested scale")

    stem = path.name
    classic_path = output / "classic" / f"{path.stem}.png"
    ai_path = output / "ai" / f"{path.stem}.png"
    sheet_path = output / "contact-sheets" / f"{path.stem}.png"
    classic_path.parent.mkdir(parents=True, exist_ok=True)
    ai_path.parent.mkdir(parents=True, exist_ok=True)
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    classic.image.save(classic_path, format="PNG")
    ai.image.save(ai_path, format="PNG")
    write_contact_sheet(source, classic.image, ai.image, sheet_path)

    return {
        "name": stem,
        "sourceSize": list(source.size),
        "outputSize": list(classic.processed_size),
        "sourceChecksumUnchanged": True,
        "classicSeconds": round(classic_seconds, 3),
        "aiSeconds": round(ai_seconds, 3),
        "classicLaplacianVar": round(laplacian_variance(classic.image), 3),
        "aiLaplacianVar": round(laplacian_variance(ai.image), 3),
        "classicSobelMean": round(sobel_mean(classic.image), 3),
        "aiSobelMean": round(sobel_mean(ai.image), 3),
        "meanAbsDiffVsClassic": round(mean_abs_diff(classic.image, ai.image), 3),
        "classicUniqueColors": unique_colors(classic.image),
        "aiUniqueColors": unique_colors(ai.image),
        "aiDiffersFromClassic": not np.array_equal(
            np.asarray(classic.image.convert("RGB")),
            np.asarray(ai.image.convert("RGB")),
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare classic Lanczos upscaling with local Real-ESRGAN ONNX. "
            "Writes ignored-directory metrics and contact sheets; it never "
            "prints OCR text or absolute personal paths."
        )
    )
    parser.add_argument("--input", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--data-dir", type=Path, default=Path.home() / ".manga-localizer"
    )
    parser.add_argument("--model", type=Path)
    parser.add_argument("--factor", type=int, default=2, choices=(2, 3, 4))
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--label", default="upscale-compare")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    output = require_ignored_empty_output(args.output)
    model_path = args.model or (
        args.data_dir.expanduser() / "models" / "RealESRGAN_x4plus_anime_6B.onnx"
    )
    ai_provider = RealESRGANONNXPreprocessProvider(
        model_path,
        profile="off",
        tile_size=args.tile_size,
    )
    health = ai_provider.health_check()
    if not health["available"]:
        raise CompareError(health["error"] or "Real-ESRGAN ONNX is unavailable")
    classic_provider = OpenCVPillowPreprocessProvider(profile="off")
    images = collect_images(args.input)
    records = [
        compare_one(
            path,
            classic_provider=classic_provider,
            ai_provider=ai_provider,
            factor=args.factor,
            output=output,
        )
        for path in images
    ]
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
        },
        "configuration": {
            "classicProvider": "opencv-pillow",
            "aiProvider": "realesrgan-onnx",
            "model": health["model"],
            "license": health["license"],
            "nativeScale": health["nativeScale"],
            "requestedFactor": args.factor,
            "tileSize": args.tile_size,
            "modelPresent": health["modelExists"],
        },
        "aggregate": {
            "images": len(records),
            "sourceChecksumFailures": 0,
            "aiDiffersFromClassicCount": sum(
                item["aiDiffersFromClassic"] for item in records
            ),
            "meanClassicLaplacianVar": round(
                float(np.mean([item["classicLaplacianVar"] for item in records])),
                3,
            ),
            "meanAiLaplacianVar": round(
                float(np.mean([item["aiLaplacianVar"] for item in records])),
                3,
            ),
            "meanAbsDiffVsClassic": round(
                float(np.mean([item["meanAbsDiffVsClassic"] for item in records])),
                3,
            ),
            "aiSeconds": round(float(sum(item["aiSeconds"] for item in records)), 3),
        },
        "images": records,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"# {args.label}",
        "",
        f"- Images: {report['aggregate']['images']}",
        f"- Requested factor: {args.factor} (native AI scale {health['nativeScale']})",
        f"- Model / license: {health['model']} / {health['license']}",
        f"- Source checksum failures: {report['aggregate']['sourceChecksumFailures']}",
        f"- AI differs from classic: {report['aggregate']['aiDiffersFromClassicCount']}",
        f"- Mean classic Laplacian variance: {report['aggregate']['meanClassicLaplacianVar']}",
        f"- Mean AI Laplacian variance: {report['aggregate']['meanAiLaplacianVar']}",
        f"- Mean abs diff vs classic: {report['aggregate']['meanAbsDiffVsClassic']}",
        f"- AI seconds: {report['aggregate']['aiSeconds']}",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report["aggregate"], ensure_ascii=True))
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (CompareError, PreprocessUnavailable, OSError, ValueError) as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
