from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from manga_localizer.config import Settings
from manga_localizer.evaluation.detection_ocr import (
    DRAFT_STATUS,
    AnnotationBox,
    PageAnnotation,
    load_annotation_document,
)
from manga_localizer.model_bundle import apply_model_bundle
from manga_localizer.providers.detection import (
    PPOCRTextDetectionProvider,
    UnionTextDetectionProvider,
)
from manga_localizer.providers.ocr import OCRRegion, TesseractOCRProvider
from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


class BootstrapError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write detector-draft annotation JSON for a private image directory. "
            "Output must be Git-ignored. OCR text stays in those ignored files only."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--detector",
        default="ppocr-v3+tesseract",
        choices=("tesseract", "ppocr-v3", "ppocr-v3+tesseract"),
    )
    parser.add_argument("--ppocr-model", type=Path)
    parser.add_argument(
        "--ocr-draft", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--direction", default="auto", choices=("auto", "horizontal", "vertical")
    )
    parser.add_argument("--label", default="detection-annotation-draft")
    return parser.parse_args()


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
        raise BootstrapError("Output must be inside a Git worktree")
    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        resolved.relative_to(git_root)
    except ValueError as error:
        raise BootstrapError(
            "Output must stay inside the selected repository"
        ) from error
    ignored = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "--quiet", str(resolved)],
        check=False,
    )
    if ignored.returncode:
        raise BootstrapError("Output is not covered by repository ignore rules")
    if resolved.exists() and any(resolved.iterdir()):
        raise BootstrapError("Output directory is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_ppocr_model(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser()
    settings = Settings()
    if settings.model_bundle is not None:
        settings, _ = apply_model_bundle(settings)
        path = settings.ppocr_detection_model_path
        return path if path.is_file() else None
    return None


def collect_images(path: Path) -> list[Path]:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return [resolved]
    if not resolved.is_dir():
        raise BootstrapError(f"Input path does not exist: {resolved}")
    return sorted(
        child
        for child in resolved.iterdir()
        if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES
    )


def build_detector(args: argparse.Namespace) -> Any:
    tesseract = TesseractOCRProvider()
    ppocr = PPOCRTextDetectionProvider(resolve_ppocr_model(args.ppocr_model))
    if args.detector == "tesseract":
        return tesseract
    if args.detector == "ppocr-v3":
        return ppocr
    return UnionTextDetectionProvider(ppocr, tesseract)


def draft_categories(region: OCRRegion, width: int, height: int) -> list[str]:
    categories = ["vertical"] if region.height > region.width * 1.2 else ["horizontal"]
    area = region.width * region.height
    if (
        area > 0
        and area < width * height * 0.01
        and max(region.width, region.height) < 96
    ):
        categories.append("single-char")
    return categories


def region_to_box(
    region: OCRRegion,
    *,
    image: Image.Image,
    ocr: TesseractOCRProvider | None,
) -> AnnotationBox:
    text = ""
    ocr_confidence = None
    if ocr is not None:
        recognized = ocr.recognize_region(
            image,
            {
                "x": region.x,
                "y": region.y,
                "width": region.width,
                "height": region.height,
            },
            direction=region.direction,
        )
        text = recognized.text
        ocr_confidence = recognized.confidence
    elif region.text:
        text = region.text
    return AnnotationBox(
        x=int(region.x),
        y=int(region.y),
        width=int(region.width),
        height=int(region.height),
        text=text,
        direction=region.direction,
        categories=tuple(draft_categories(region, image.width, image.height)),
        status=DRAFT_STATUS,
        detector_confidence=region.confidence,
        ocr_confidence=ocr_confidence,
    )


def annotation_payload(page: PageAnnotation, relative_name: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "status": DRAFT_STATUS,
        "independence": "detector-draft",
        "negative": page.negative,
        "image": {
            "id": page.page_id,
            "relativeName": relative_name,
            "width": page.width,
            "height": page.height,
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
                "detectorConfidence": box.detector_confidence,
                "ocrConfidence": box.ocr_confidence,
            }
            for box in page.boxes
        ],
    }


def run(args: argparse.Namespace) -> int:
    images = collect_images(args.input)
    if not images:
        raise BootstrapError("No images found")
    output = require_ignored_empty_output(args.output)
    detector = build_detector(args)
    health = detector.health_check()
    if not health.get("available"):
        raise BootstrapError(health.get("error") or "Detector is unavailable")
    ocr = TesseractOCRProvider() if args.ocr_draft else None
    if ocr is not None and not ocr.health_check().get("available"):
        raise BootstrapError("Tesseract OCR is unavailable for drafts")
    pages = 0
    regions = 0
    for image_path in images:
        with Image.open(image_path) as opened:
            picture = opened.convert("RGB")
            detections = detector.detect_text_regions(picture, direction=args.direction)
            boxes = [
                region_to_box(region, image=picture, ocr=ocr) for region in detections
            ]
            page = PageAnnotation(
                page_id=image_path.stem,
                width=picture.width,
                height=picture.height,
                boxes=tuple(boxes),
                negative=not boxes,
                status=DRAFT_STATUS,
                independence="detector-draft",
            )
        payload = annotation_payload(page, image_path.name)
        (output / f"{image_path.stem}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        load_annotation_document(payload, default_page_id=image_path.stem)
        pages += 1
        regions += len(boxes)
    manifest = {
        "schemaVersion": 1,
        "label": args.label,
        "detector": args.detector,
        "independence": "detector-draft",
        "ocrDraft": bool(args.ocr_draft),
        "pages": pages,
        "regions": regions,
        "privacy": {
            "gitIgnored": True,
            "ocrTextStored": bool(args.ocr_draft),
            "absolutePathsStored": False,
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {pages} draft annotation files and {regions} proposal boxes",
        flush=True,
    )
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except BootstrapError as error:
        print(f"error: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
