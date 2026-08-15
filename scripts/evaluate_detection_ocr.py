from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from manga_localizer.config import Settings
from manga_localizer.evaluation.detection_ocr import (
    AnnotationBox,
    PageAnnotation,
    evaluate_detection_ocr,
    load_annotation_document,
    sanitize_report,
)
from manga_localizer.evaluation.synthetic import (
    generate_detection_stress_pages,
    write_detection_stress_set,
)
from manga_localizer.providers.detection import (
    PPOCRTextDetectionProvider,
    UnionTextDetectionProvider,
)
from manga_localizer.providers.ocr import OCRRegion, TesseractOCRProvider
from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


class EvaluateError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate detection/OCR boxes against annotations. The default report stores "
            "only anonymous page IDs and aggregate metrics."
        )
    )
    parser.add_argument(
        "--annotations", type=Path, help="Directory of annotation JSON files"
    )
    parser.add_argument(
        "--images", type=Path, help="Directory of page images matching annotation stems"
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Evaluate the public synthetic stress set",
    )
    parser.add_argument(
        "--write-synthetic",
        type=Path,
        help="Write synthetic images/JSON into this directory",
    )
    parser.add_argument(
        "--detector",
        default="ppocr-v3+tesseract",
        choices=("tesseract", "ppocr-v3", "ppocr-v3+tesseract"),
    )
    parser.add_argument("--ppocr-model", type=Path)
    parser.add_argument("--ocr", default="tesseract", choices=("tesseract", "none"))
    parser.add_argument(
        "--direction", default="auto", choices=("auto", "horizontal", "vertical")
    )
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument(
        "--reviewed-only", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="detection-ocr-eval")
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
        raise EvaluateError("Output must be inside a Git worktree")
    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        resolved.relative_to(git_root)
    except ValueError as error:
        raise EvaluateError(
            "Output must stay inside the selected repository"
        ) from error
    ignored = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "--quiet", str(resolved)],
        check=False,
    )
    if ignored.returncode:
        raise EvaluateError("Output is not covered by repository ignore rules")
    if resolved.exists() and any(resolved.iterdir()):
        raise EvaluateError("Output directory is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_ppocr_model(explicit: Path | None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    filename = "text_detection_cn_ppocrv3_2023may.onnx"
    candidates.append(Path.cwd() / ".manga-localizer" / "models" / filename)
    candidates.append(Settings().ppocr_detection_model_path)
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0] if explicit is not None else None


def build_detector(args: argparse.Namespace) -> Any:
    tesseract = TesseractOCRProvider()
    ppocr = PPOCRTextDetectionProvider(resolve_ppocr_model(args.ppocr_model))
    if args.detector == "tesseract":
        return tesseract
    if args.detector == "ppocr-v3":
        return ppocr
    return UnionTextDetectionProvider(ppocr, tesseract)


def load_annotation_dir(path: Path) -> dict[str, PageAnnotation]:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise EvaluateError(f"Annotation directory does not exist: {resolved}")
    pages: dict[str, PageAnnotation] = {}
    for child in sorted(resolved.glob("*.json")):
        if child.name == "manifest.json":
            continue
        payload = json.loads(child.read_text(encoding="utf-8"))
        pages[child.stem] = load_annotation_document(
            payload, default_page_id=child.stem
        )
    if not pages:
        raise EvaluateError("No annotation JSON files found")
    return pages


def find_image(directory: Path, stem: str) -> Path | None:
    for suffix in IMAGE_SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def region_to_prediction(
    region: OCRRegion,
    *,
    image: Image.Image,
    ocr: TesseractOCRProvider | None,
) -> AnnotationBox:
    text = region.text or ""
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
            direction=region.direction or "auto",
        )
        text = recognized.text
        ocr_confidence = recognized.confidence
    return AnnotationBox(
        x=int(region.x),
        y=int(region.y),
        width=int(region.width),
        height=int(region.height),
        text=text,
        direction=region.direction,
        detector_confidence=region.confidence,
        ocr_confidence=ocr_confidence,
        status="prediction",
    )


def predict_image(
    image: Image.Image,
    *,
    detector: Any,
    ocr: TesseractOCRProvider | None,
    direction: str,
) -> list[AnnotationBox]:
    detections = detector.detect_text_regions(image, direction=direction)
    return [region_to_prediction(region, image=image, ocr=ocr) for region in detections]


def markdown_report(report: dict[str, Any], *, label: str) -> str:
    detection = report["detection"]
    ocr = report["ocr"]
    negatives = report["negatives"]
    lines = [
        f"# {label}",
        "",
        "Sanitized detection/OCR evaluation. Transcriptions, filenames, checksums, and paths are omitted.",
        "",
        f"- Annotation independence: `{report['annotationIndependence']}`",
        f"- Pages: {report['pages']}",
        f"- IoU threshold: {report['iouThreshold']}",
        f"- Detection precision: {detection['precision']}",
        f"- Detection recall: {detection['recall']}",
        f"- Detection F1: {detection['f1']}",
        f"- OCR CER (matched, NFKC compact): {ocr['cer']}",
        f"- Transcription coverage: {ocr['transcriptionCoverage']}",
        f"- Negative pages: {negatives['pages']}",
        f"- False positives on negative pages: {negatives['falsePositiveRegions']}",
        f"- Confidence used to drop predictions: {report['confidence']['usedToDropPredictions']}",
        "",
        "## Categories",
        "",
        "| Category | Precision | Recall | TP | FP | FN |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, payload in (report.get("categories") or {}).items():
        lines.append(
            f"| {name} | {payload['precision']} | {payload['recall']} | "
            f"{payload['truePositives']} | {payload['falsePositives']} | {payload['falseNegatives']} |"
        )
    lines.extend(["", "## Anonymous pages", ""])
    for page in report.get("pageSummaries") or []:
        lines.append(
            f"- {page['id']}: gt={page['groundTruth']} pred={page['predictions']} "
            f"tp={page['truePositives']} fp={page['falsePositives']} fn={page['falseNegatives']}"
            f"{' negative' if page['negative'] else ''}"
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    if args.write_synthetic is not None:
        write_detection_stress_set(args.write_synthetic)

    pairs: list[tuple[PageAnnotation, list[AnnotationBox]]] = []
    detector = None
    ocr = None
    if args.synthetic:
        detector = build_detector(args)
        if not detector.health_check().get("available"):
            raise EvaluateError(
                detector.health_check().get("error") or "Detector is unavailable"
            )
        if args.ocr == "tesseract":
            ocr = TesseractOCRProvider()
            if not ocr.health_check().get("available"):
                raise EvaluateError("Tesseract OCR is unavailable")
        for page in generate_detection_stress_pages():
            predictions = predict_image(
                page.image,
                detector=detector,
                ocr=ocr,
                direction=args.direction,
            )
            pairs.append((page.annotation, predictions))
    elif args.annotations is not None:
        annotations = load_annotation_dir(args.annotations)
        if args.images is None:
            raise EvaluateError(
                "Live evaluation requires --images as well as --annotations"
            )
        image_root = args.images.expanduser().resolve()
        detector = build_detector(args)
        if not detector.health_check().get("available"):
            raise EvaluateError(
                detector.health_check().get("error") or "Detector is unavailable"
            )
        if args.ocr == "tesseract":
            ocr = TesseractOCRProvider()
            if not ocr.health_check().get("available"):
                raise EvaluateError("Tesseract OCR is unavailable")
        for stem, annotation in annotations.items():
            image_path = find_image(image_root, stem)
            if image_path is None:
                raise EvaluateError(f"Missing image for annotation stem {stem!r}")
            with Image.open(image_path) as opened:
                predictions = predict_image(
                    opened.convert("RGB"),
                    detector=detector,
                    ocr=ocr,
                    direction=args.direction,
                )
            pairs.append((annotation, predictions))
    else:
        raise EvaluateError("Specify --synthetic or --annotations")

    raw = evaluate_detection_ocr(
        pairs,
        iou_threshold=args.iou,
        reviewed_only=args.reviewed_only,
    )
    if detector is not None:
        raw["detector"] = args.detector
        raw["ocrProvider"] = args.ocr
    report = sanitize_report(raw)
    report["label"] = args.label
    rendered = json.dumps(report, indent=2) + "\n"
    markdown = markdown_report(report, label=args.label)
    if args.output is not None:
        output = require_ignored_empty_output(args.output)
        (output / "report.json").write_text(rendered, encoding="utf-8")
        (output / "report.md").write_text(markdown, encoding="utf-8")
    print(rendered, end="")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except EvaluateError as error:
        print(f"error: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
