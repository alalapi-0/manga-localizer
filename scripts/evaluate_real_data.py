from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient
from manga_localizer.config import Settings
from manga_localizer.main import create_app
from PIL import Image

TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}
SUPPORTED_STAGES = (
    "preprocess",
    "detect",
    "ocr",
    "translate",
    "inpaint",
    "typeset",
    "export",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real-data pipeline locally and write a private aggregate report.",
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="Image file or directory to import"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New private run directory; the command refuses to reuse a non-empty directory",
    )
    parser.add_argument("--label", default="real-data-run")
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--timeout-seconds", type=float, default=14_400)
    parser.add_argument(
        "--stages",
        default=",".join(SUPPORTED_STAGES),
        help=f"Comma-separated subset of: {', '.join(SUPPORTED_STAGES)}",
    )
    parser.add_argument(
        "--direction",
        default="auto",
        choices=("auto", "horizontal", "vertical"),
    )
    parser.add_argument("--detector-provider", default="tesseract")
    parser.add_argument("--ocr-provider", default="tesseract")
    parser.add_argument("--inpainter-provider", default="opencv")
    parser.add_argument(
        "--repair-policy",
        default="safe",
        choices=("safe", "recognized", "all"),
    )
    parser.add_argument("--preprocessor-provider", default="opencv-pillow")
    parser.add_argument(
        "--preprocessing-profile",
        default="ocr-friendly",
        choices=("off", "ocr-friendly", "balanced", "visual-quality"),
    )
    parser.add_argument("--ppocr-model", type=Path)
    parser.add_argument("--lama-model", type=Path)
    return parser.parse_args()


def selected_stages(value: str) -> list[str]:
    stages = [stage.strip() for stage in value.split(",") if stage.strip()]
    unknown = set(stages) - set(SUPPORTED_STAGES)
    if unknown:
        raise ValueError(f"Unsupported stages: {', '.join(sorted(unknown))}")
    return [stage for stage in SUPPORTED_STAGES if stage in stages]


def require_new_run_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError(f"Output directory is not empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def wait_for_job(
    client: TestClient,
    job: dict[str, Any],
    *,
    poll_seconds: float,
    timeout_seconds: float,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    last_progress: tuple[int, int, str] | None = None
    while job["status"] not in TERMINAL_JOB_STATES:
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise TimeoutError(
                f"Job {job['id']} exceeded {timeout_seconds:.0f} seconds"
            )
        progress = (int(job["completed"]), int(job["total"]), str(job["status"]))
        if progress != last_progress:
            print(
                f"[{job['kind']}] {progress[2]} {progress[0]}/{progress[1]} "
                f"({elapsed:.1f}s)",
                flush=True,
            )
            last_progress = progress
        time.sleep(poll_seconds)
        response = client.get(f"/api/jobs/{job['id']}")
        response.raise_for_status()
        job = response.json()
    elapsed = time.monotonic() - started
    failures = sum(item["status"] == "failed" for item in job["items"])
    print(
        f"[{job['kind']}] {job['status']} {job['completed']}/{job['total']} "
        f"with {failures} failures ({elapsed:.1f}s)",
        flush=True,
    )
    return job, elapsed


def job_options(stage: str, args: argparse.Namespace, output: Path) -> dict[str, Any]:
    common: dict[str, Any] = {"concurrency": args.concurrency}
    if stage == "preprocess":
        return {
            **common,
            "provider": args.preprocessor_provider,
            "profile": args.preprocessing_profile,
        }
    if stage == "detect":
        return {
            **common,
            "provider": args.detector_provider,
            "direction": args.direction,
        }
    if stage == "ocr":
        return {**common, "provider": args.ocr_provider, "direction": args.direction}
    if stage == "translate":
        return {**common, "provider": "mock"}
    if stage == "inpaint":
        return {
            **common,
            "provider": args.inpainter_provider,
            "repairPolicy": args.repair_policy,
        }
    if stage == "export":
        return {
            **common,
            "outputPath": str(output / "export-bundle"),
            "format": "both",
            "conflict": "rename",
            "preserveTree": True,
        }
    return common


def report_configuration(args: argparse.Namespace, stages: list[str]) -> dict[str, Any]:
    """Return the non-sensitive requested configuration needed to interpret a run."""

    return {
        "stages": stages,
        "concurrency": args.concurrency,
        "direction": args.direction,
        "preprocessorProvider": args.preprocessor_provider,
        "preprocessingProfile": args.preprocessing_profile,
        "detectorProvider": args.detector_provider,
        "ocrProvider": args.ocr_provider,
        "translatorProvider": "mock",
        "inpainterProvider": args.inpainter_provider,
        "repairPolicy": args.repair_policy,
        "optionalModelsProvided": {
            "ppocr": args.ppocr_model is not None,
            "lama": args.lama_model is not None,
        },
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_metrics(workspace: Path, relative_path: str) -> dict[str, Any]:
    relative = Path(relative_path).with_suffix(".png")
    source_path = workspace / "source" / relative_path
    preprocessed_path = workspace / "generated" / "preprocessed" / relative
    mask_path = workspace / "generated" / "masks" / relative
    repaired_path = workspace / "generated" / "inpainted" / relative
    result: dict[str, Any] = {
        "preprocessedSize": None,
        "repairOutputSizeMatchesSource": None,
        "maskCoverage": None,
        "changedOutsideMaskPixels": None,
        "changedInsideMaskPixels": None,
    }
    if preprocessed_path.is_file():
        with Image.open(preprocessed_path) as processed:
            result["preprocessedSize"] = list(processed.size)
    if not (source_path.is_file() and mask_path.is_file() and repaired_path.is_file()):
        return result
    with (
        Image.open(source_path) as source_image,
        Image.open(mask_path) as mask_image,
        Image.open(repaired_path) as repaired_image,
    ):
        source = np.asarray(source_image.convert("RGB"), dtype=np.uint8)
        mask = np.asarray(mask_image.convert("L"), dtype=np.uint8)
        repaired = np.asarray(repaired_image.convert("RGB"), dtype=np.uint8)
    dimensions_match = source.shape == repaired.shape and source.shape[:2] == mask.shape
    result["repairOutputSizeMatchesSource"] = dimensions_match
    if not dimensions_match:
        return result
    changed = np.any(source != repaired, axis=2)
    result["maskCoverage"] = round(float(np.count_nonzero(mask) / mask.size), 6)
    result["changedOutsideMaskPixels"] = int(np.count_nonzero(changed & (mask == 0)))
    result["changedInsideMaskPixels"] = int(np.count_nonzero(changed & (mask > 0)))
    return result


def summarize_run(
    client: TestClient,
    project_id: str,
    *,
    label: str,
    configuration: dict[str, Any],
    stages: list[dict[str, Any]],
    import_failures: int,
    workspace: Path,
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    image_response = client.get(f"/api/projects/{project_id}/images")
    image_response.raise_for_status()
    images = image_response.json()
    per_image: list[dict[str, Any]] = []
    confidences: list[float] = []
    total_regions = 0
    recognized_regions = 0
    total_characters = 0
    retried_regions = 0
    selected_original_regions = 0
    source_checksum_failures = 0
    dimension_mismatches = 0
    changed_outside_mask_pixels = 0
    mask_coverages: list[float] = []
    for image in images:
        region_response = client.get(f"/api/images/{image['id']}/regions")
        region_response.raise_for_status()
        regions = region_response.json()
        image_confidences = [
            float(region["confidence"])
            for region in regions
            if region.get("confidence") is not None
        ]
        recognized = [
            region for region in regions if str(region.get("sourceText", "")).strip()
        ]
        image_retried = sum(
            int((region.get("repair") or {}).get("ocrAttemptCount", 1)) > 1
            for region in regions
        )
        image_selected_original = sum(
            (region.get("repair") or {}).get("ocrInputVariant") == "original"
            for region in regions
        )
        character_count = sum(
            len(str(region.get("sourceText", "")).strip()) for region in regions
        )
        confidences.extend(image_confidences)
        total_regions += len(regions)
        recognized_regions += len(recognized)
        total_characters += character_count
        retried_regions += image_retried
        selected_original_regions += image_selected_original
        source_path = workspace / "source" / image["relativePath"]
        source_unchanged = source_path.is_file() and source_hashes.get(
            image["relativePath"]
        ) == file_sha256(source_path)
        if not source_unchanged:
            source_checksum_failures += 1
        structural = artifact_metrics(workspace, image["relativePath"])
        if structural["repairOutputSizeMatchesSource"] is False:
            dimension_mismatches += 1
        changed_outside_mask_pixels += structural["changedOutsideMaskPixels"] or 0
        if structural["maskCoverage"] is not None:
            mask_coverages.append(float(structural["maskCoverage"]))
        per_image.append(
            {
                "relativePath": image["relativePath"],
                "width": image["width"],
                "height": image["height"],
                "statuses": image["status"],
                "processingErrors": image.get("processingErrors", []),
                "regionCount": len(regions),
                "recognizedRegionCount": len(recognized),
                "emptyRegionCount": len(regions) - len(recognized),
                "characterCount": character_count,
                "ocrRetriedRegionCount": image_retried,
                "ocrSelectedOriginalRegionCount": image_selected_original,
                "meanConfidence": round(statistics.fmean(image_confidences), 4)
                if image_confidences
                else None,
                "minimumConfidence": round(min(image_confidences), 4)
                if image_confidences
                else None,
                "sourceChecksumUnchanged": source_unchanged,
                **structural,
            }
        )
    failed_images = sum(bool(image.get("processingErrors")) for image in images)
    empty_pages = sum(item["recognizedRegionCount"] == 0 for item in per_image)
    return {
        "schemaVersion": 3,
        "label": label,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "configuration": configuration,
        "aggregate": {
            "importedImages": len(images),
            "importFailures": import_failures,
            "failedImages": failed_images,
            "detectedRegions": total_regions,
            "recognizedRegions": recognized_regions,
            "emptyRecognizedPages": empty_pages,
            "recognizedCharacters": total_characters,
            "ocrRetriedRegions": retried_regions,
            "ocrSelectedOriginalRegions": selected_original_regions,
            "recognizedRegionRate": round(recognized_regions / total_regions, 4)
            if total_regions
            else 0.0,
            "meanConfidence": round(statistics.fmean(confidences), 4)
            if confidences
            else None,
            "sourceChecksumFailures": source_checksum_failures,
            "repairDimensionMismatches": dimension_mismatches,
            "changedOutsideMaskPixels": changed_outside_mask_pixels,
            "meanMaskCoverage": round(statistics.fmean(mask_coverages), 6)
            if mask_coverages
            else None,
        },
        "stages": stages,
        "images": per_image,
    }


def markdown_report(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    configuration = report["configuration"]
    lines = [
        f"# {report['label']}",
        "",
        "This report contains aggregate and per-file metrics only; recognized text is omitted.",
        "",
        "## Configuration",
        "",
        f"- Stages: {', '.join(configuration['stages'])}",
        f"- Concurrency / direction: {configuration['concurrency']} / {configuration['direction']}",
        f"- Preprocessor / profile: {configuration['preprocessorProvider']} / {configuration['preprocessingProfile']}",
        f"- Detector / OCR: {configuration['detectorProvider']} / {configuration['ocrProvider']}",
        f"- Translator / inpainter: {configuration['translatorProvider']} / {configuration['inpainterProvider']}",
        f"- Repair policy: {configuration['repairPolicy']}",
        "",
        "## Aggregate",
        "",
        f"- Imported images: {aggregate['importedImages']}",
        f"- Import failures: {aggregate['importFailures']}",
        f"- Images with processing errors: {aggregate['failedImages']}",
        f"- Detected regions: {aggregate['detectedRegions']}",
        f"- Recognized regions: {aggregate['recognizedRegions']}",
        f"- Pages with no recognized region: {aggregate['emptyRecognizedPages']}",
        f"- Recognized region rate: {aggregate['recognizedRegionRate']:.1%}",
        f"- Mean OCR confidence: {aggregate['meanConfidence']}",
        f"- OCR regions retried on original: {aggregate['ocrRetriedRegions']}",
        f"- OCR regions selecting original: {aggregate['ocrSelectedOriginalRegions']}",
        f"- Source checksum failures: {aggregate['sourceChecksumFailures']}",
        f"- Repair dimension mismatches: {aggregate['repairDimensionMismatches']}",
        f"- Changed pixels outside masks: {aggregate['changedOutsideMaskPixels']}",
        f"- Mean mask coverage: {aggregate['meanMaskCoverage']}",
        "",
        "## Stages",
        "",
        "| Stage | Status | Completed | Failed items | Seconds |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for stage in report["stages"]:
        lines.append(
            f"| {stage['kind']} | {stage['status']} | {stage['completed']}/{stage['total']} "
            f"| {stage['failedItems']} | {stage['seconds']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Per-image metrics",
            "",
            "See `report.json` in this private run directory. OCR text is deliberately not stored.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    input_path = args.input.expanduser().resolve(strict=True)
    output = require_new_run_directory(args.output)
    stages = selected_stages(args.stages)
    settings = Settings(
        data_dir=output / "catalog",
        worker_poll_seconds=min(max(args.poll_seconds / 2, 0.01), 0.5),
        max_upload_bytes=200 * 1024 * 1024,
        ppocr_detection_model=args.ppocr_model,
        lama_inpainting_model=args.lama_model,
    )
    app = create_app(settings, start_worker=True)
    stage_reports: list[dict[str, Any]] = []
    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            json={"name": args.label, "outputPath": str(output / "workspace")},
        )
        created.raise_for_status()
        project = created.json()
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(input_path)]},
        )
        imported.raise_for_status()
        image_ids = [image["id"] for image in imported.json()]
        import_failures = int(
            imported.headers.get("X-Manga-Localizer-Import-Failures", "0")
        )
        print(
            f"[import] completed {len(image_ids)} images with {import_failures} failures",
            flush=True,
        )
        workspace = output / "workspace"
        source_hashes = {
            image["relativePath"]: file_sha256(
                workspace / "source" / image["relativePath"]
            )
            for image in imported.json()
        }
        for stage in stages:
            response = client.post(
                f"/api/projects/{project['id']}/{stage}",
                json={
                    "imageIds": image_ids,
                    "options": job_options(stage, args, output),
                },
            )
            response.raise_for_status()
            job, elapsed = wait_for_job(
                client,
                response.json(),
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            stage_reports.append(
                {
                    "kind": stage,
                    "status": job["status"],
                    "total": job["total"],
                    "completed": job["completed"],
                    "failedItems": sum(
                        item["status"] == "failed" for item in job["items"]
                    ),
                    "seconds": round(elapsed, 3),
                }
            )
        report = summarize_run(
            client,
            project["id"],
            label=args.label,
            configuration=report_configuration(args, stages),
            stages=stage_reports,
            import_failures=import_failures,
            workspace=workspace,
            source_hashes=source_hashes,
        )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "report.md").write_text(markdown_report(report), encoding="utf-8")
    print(f"[report] {output / 'report.json'}", flush=True)
    return 0 if all(stage["status"] == "completed" for stage in stage_reports) else 1


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
