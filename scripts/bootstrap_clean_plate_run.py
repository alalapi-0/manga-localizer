from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient
from manga_localizer.config import Settings
from manga_localizer.database import ImageAsset, TextRegion
from manga_localizer.main import create_app
from PIL import Image, ImageOps
from sqlalchemy import select

TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
DEFAULT_REPAIR: dict[str, Any] = {
    "maskMode": "text",
    "maskPadding": 4,
    "dilation": 2,
    "feather": 2,
    "method": "telea",
    "radius": 3,
    "fillColor": "#ffffff",
}


class BootstrapError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap a private clean-plate project from an OCR-text-free review "
            "manifest and materialize explicitly reviewed no-text pages."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--review-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=130)
    parser.add_argument(
        "--no-text-pages",
        required=True,
        help="Comma-separated anonymous page numbers or page-#### identifiers",
    )
    parser.add_argument("--label", default="clean-plate-review")
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
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


def parse_page_ids(value: str, expected_count: int) -> set[str]:
    page_ids: set[str] = set()
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        if token.startswith("page-"):
            suffix = token.removeprefix("page-")
        else:
            suffix = token
        try:
            number = int(suffix)
        except ValueError as error:
            raise BootstrapError(
                f"Invalid anonymous page identifier: {token}"
            ) from error
        if not 1 <= number <= expected_count:
            raise BootstrapError(f"Anonymous page is out of range: {token}")
        page_ids.add(f"page-{number:04d}")
    if not page_ids:
        raise BootstrapError(
            "At least one explicitly reviewed no-text page is required"
        )
    return page_ids


def safe_relative_path(value: Any) -> Path:
    path = Path(str(value))
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BootstrapError("Review manifest contains an unsafe relative path")
    return path


def load_review_manifest(
    path: Path,
    *,
    input_root: Path,
    expected_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        manifest = json.loads(resolved.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise BootstrapError("Review manifest is not readable JSON") from error
    privacy = manifest.get("privacy") if isinstance(manifest, dict) else None
    if not isinstance(privacy, dict) or not privacy.get("ocrTextStored") is False:
        raise BootstrapError("Review manifest must explicitly omit OCR text")
    if privacy.get("absolutePathsStored") is not False:
        raise BootstrapError("Review manifest must explicitly omit absolute paths")
    records = manifest.get("images")
    if not isinstance(records, list) or len(records) != expected_count:
        raise BootstrapError(
            f"Expected {expected_count} reviewed image records in the manifest"
        )
    expected_ids = [f"page-{index:04d}" for index in range(1, expected_count + 1)]
    if [record.get("imageId") for record in records] != expected_ids:
        raise BootstrapError(
            "Review manifest anonymous page ids are incomplete or unordered"
        )
    input_files = sorted(
        (
            source
            for source in input_root.rglob("*")
            if source.is_file() and source.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        ),
        key=lambda source: source.relative_to(input_root).as_posix().casefold(),
    )
    if len(input_files) != expected_count:
        raise BootstrapError(f"Expected {expected_count} immutable input images")
    manifest_paths: set[str] = set()
    for record in records:
        relative = safe_relative_path(record.get("sourceRelativePath"))
        manifest_paths.add(relative.as_posix())
        source = (input_root / relative).resolve(strict=True)
        if not source.is_relative_to(input_root):
            raise BootstrapError(
                "Review manifest path escapes the immutable input root"
            )
        if file_sha256(source) != record.get("sourceChecksum"):
            raise BootstrapError(
                "Immutable input checksum differs from the review manifest"
            )
        with Image.open(source) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        if [width, height] != [record.get("width"), record.get("height")]:
            raise BootstrapError(
                "Immutable input dimensions differ from the review manifest"
            )
    actual_paths = {source.relative_to(input_root).as_posix() for source in input_files}
    if actual_paths != manifest_paths:
        raise BootstrapError("Review manifest and immutable input set differ")
    return manifest, records


def wait_for_job(
    client: TestClient,
    job: dict[str, Any],
    *,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    while job["status"] not in TERMINAL_JOB_STATES:
        if time.monotonic() - started > timeout_seconds:
            raise BootstrapError("No-text materialization job timed out")
        time.sleep(poll_seconds)
        response = client.get(f"/api/jobs/{job['id']}")
        response.raise_for_status()
        job = response.json()
    if job["status"] != "completed" or any(
        item["status"] != "completed" for item in job["items"]
    ):
        raise BootstrapError("No-text materialization job did not complete cleanly")
    return job


def _candidate_repair(candidate: dict[str, Any]) -> dict[str, Any]:
    supplied = candidate.get("repair")
    repair = {
        **DEFAULT_REPAIR,
        **(supplied if isinstance(supplied, dict) else {}),
        "detectorGenerated": True,
    }
    if repair.get("inpainterProvider") in {"", "inherit", None}:
        repair.pop("inpainterProvider", None)
    polygon = candidate.get("polygon")
    if isinstance(polygon, list) and len(polygon) >= 3:
        repair["maskPolygon"] = polygon
    return repair


def bootstrap_candidates(
    client: TestClient,
    project_id: str,
    records: list[dict[str, Any]],
    no_text_pages: set[str],
) -> dict[str, dict[str, Any]]:
    response = client.get(f"/api/projects/{project_id}/images")
    response.raise_for_status()
    api_images = {image["relativePath"]: image for image in response.json()}
    store = client.app.state.registry.get(project_id)
    with store.session() as session:
        database_images = {
            image.relative_path: image
            for image in session.scalars(select(ImageAsset)).all()
        }
        for record in records:
            relative = str(record["sourceRelativePath"])
            image = database_images.get(relative)
            if image is None or relative not in api_images:
                raise BootstrapError("Imported project image mapping is incomplete")
            status = dict(image.status or {})
            no_text = str(record["imageId"]) in no_text_pages
            status["detection"] = "done" if no_text else "pending"
            status["detectorProvider"] = (
                "visual-review" if no_text else "visual-review-union-candidates"
            )
            status["reviewState"] = "pending"
            status["reviewedAt"] = ""
            image.status = status
            if not no_text:
                for order, candidate in enumerate(record.get("regions") or []):
                    geometry = candidate.get("geometry") or {}
                    session.add(
                        TextRegion(
                            image_id=image.id,
                            x=float(geometry["x"]),
                            y=float(geometry["y"]),
                            width=float(geometry["width"]),
                            height=float(geometry["height"]),
                            rotation=float(geometry.get("rotation", 0)),
                            source_text="",
                            translation_text="",
                            region_type="unknown",
                            direction="auto",
                            reading_order=order,
                            confidence=candidate.get("confidence"),
                            ignored=False,
                            confirmed=False,
                            repair=_candidate_repair(candidate),
                            revision=1,
                        )
                    )
            image.revision += 1
        session.flush()
    store.write_snapshot()
    refreshed = client.get(f"/api/projects/{project_id}/images")
    refreshed.raise_for_status()
    return {image["relativePath"]: image for image in refreshed.json()}


def verify_no_text_page(
    *,
    input_root: Path,
    workspace: Path,
    record: dict[str, Any],
) -> dict[str, Any]:
    relative = safe_relative_path(record["sourceRelativePath"])
    original = input_root / relative
    workspace_source = workspace / "source" / relative
    generated_relative = relative.with_suffix(".png")
    clean = workspace / "generated" / "inpainted" / generated_relative
    mask_path = workspace / "generated" / "masks" / generated_relative
    if not workspace_source.is_file() or not clean.is_file() or not mask_path.is_file():
        raise BootstrapError("A no-text page is missing source, clean, or mask output")
    if file_sha256(original) != record["sourceChecksum"]:
        raise BootstrapError("An immutable original changed during materialization")
    if file_sha256(workspace_source) != record["sourceChecksum"]:
        raise BootstrapError(
            "A project-owned source copy differs from its immutable input"
        )
    with (
        Image.open(workspace_source) as source_image,
        Image.open(clean) as clean_image,
        Image.open(mask_path) as mask_image,
    ):
        source_pixels = np.asarray(source_image.convert("RGBA"), dtype=np.uint8)
        clean_pixels = np.asarray(clean_image.convert("RGBA"), dtype=np.uint8)
        mask = np.asarray(mask_image.convert("L"), dtype=np.uint8)
        clean_size = clean_image.size
        mask_size = mask_image.size
    if (
        source_pixels.shape != clean_pixels.shape
        or source_pixels.shape[:2] != mask.shape
    ):
        raise BootstrapError(
            "A no-text clean plate or mask has incompatible dimensions"
        )
    changed = np.any(source_pixels != clean_pixels, axis=2)
    nonzero_mask_pixels = int(np.count_nonzero(mask))
    changed_pixels = int(np.count_nonzero(changed))
    if nonzero_mask_pixels or changed_pixels:
        raise BootstrapError(
            "An explicitly reviewed no-text page changed during materialization"
        )
    return {
        "sourceChecksum": record["sourceChecksum"],
        "sourceChecksumUnchanged": True,
        "cleanChecksum": file_sha256(clean),
        "maskChecksum": file_sha256(mask_path),
        "sourceDimensions": [record["width"], record["height"]],
        "cleanDimensions": list(clean_size),
        "maskDimensions": list(mask_size),
        "maskNonzeroPixels": nonzero_mask_pixels,
        "changedOutsideMaskPixels": changed_pixels,
        "changedInsideMaskPixels": 0,
        "outputReview": "accepted-identical-no-text",
    }


def run(args: argparse.Namespace) -> int:
    if args.expected_count <= 0:
        raise BootstrapError("expected-count must be positive")
    input_root = args.input.expanduser().resolve(strict=True)
    if not input_root.is_dir():
        raise BootstrapError("Input must be an immutable image directory")
    no_text_pages = parse_page_ids(args.no_text_pages, args.expected_count)
    _review_manifest, records = load_review_manifest(
        args.review_manifest,
        input_root=input_root,
        expected_count=args.expected_count,
    )
    known_ids = {str(record["imageId"]) for record in records}
    if not no_text_pages <= known_ids:
        raise BootstrapError("A no-text page is absent from the review manifest")
    output = require_ignored_empty_output(args.output)
    workspace = output / "workspace"
    settings = Settings(
        data_dir=output / "catalog",
        worker_poll_seconds=min(max(args.poll_seconds / 2, 0.01), 0.25),
        max_upload_bytes=200 * 1024 * 1024,
    )
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            json={"name": args.label, "outputPath": str(workspace)},
        )
        created.raise_for_status()
        project = created.json()
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(input_root)]},
        )
        imported.raise_for_status()
        if len(imported.json()) != args.expected_count:
            raise BootstrapError(
                "The private project did not import the complete input set"
            )
        if int(imported.headers.get("X-Manga-Localizer-Import-Failures", "0")):
            raise BootstrapError("The private project reported import failures")
        image_by_path = bootstrap_candidates(
            client,
            project["id"],
            records,
            no_text_pages,
        )
        no_text_image_ids = [
            image_by_path[str(record["sourceRelativePath"])]["id"]
            for record in records
            if str(record["imageId"]) in no_text_pages
        ]
        queued = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={
                "imageIds": no_text_image_ids,
                "options": {
                    "provider": "opencv",
                    "repairPolicy": "safe",
                    "concurrency": 4,
                },
            },
        )
        queued.raise_for_status()
        wait_for_job(
            client,
            queued.json(),
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        refreshed = client.get(f"/api/projects/{project['id']}/images")
        refreshed.raise_for_status()
        image_by_path = {image["relativePath"]: image for image in refreshed.json()}
        for record in records:
            if str(record["imageId"]) not in no_text_pages:
                continue
            image = image_by_path[str(record["sourceRelativePath"])]
            reviewed = client.patch(
                f"/api/images/{image['id']}/review",
                json={
                    "reviewState": "no-text-reviewed",
                    "expectedRevision": image["revision"],
                },
            )
            reviewed.raise_for_status()

    per_page: list[dict[str, Any]] = []
    for record in records:
        image_id = str(record["imageId"])
        if image_id in no_text_pages:
            final = verify_no_text_page(
                input_root=input_root,
                workspace=workspace,
                record=record,
            )
            per_page.append(
                {
                    "imageId": image_id,
                    "pageVisualReview": "checked-no-text",
                    "detectionReview": "reviewed-no-text",
                    "candidateDecision": "all-visual-false-positives",
                    "candidateCount": len(record.get("regions") or []),
                    "regionReview": "not-applicable",
                    "reviewStatus": "no-text-reviewed",
                    "retryCount": 0,
                    "unresolvedRegions": [],
                    "final": final,
                }
            )
        else:
            per_page.append(
                {
                    "imageId": image_id,
                    "pageVisualReview": "checked-has-text",
                    "detectionReview": "pending-candidate-completion",
                    "candidateDecision": "pending-region-review",
                    "candidateCount": len(record.get("regions") or []),
                    "regionReview": "pending",
                    "reviewStatus": "pending",
                    "retryCount": 0,
                    "unresolvedRegions": ["manual-region-and-mask-review-required"],
                    "final": None,
                }
            )
    run_manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat(),
        "purpose": "private-clean-plate-production",
        "privacy": {
            "gitIgnored": True,
            "ocrTextStored": False,
            "absolutePathsStored": False,
        },
        "reviewManifestChecksum": file_sha256(
            args.review_manifest.expanduser().resolve()
        ),
        "expectedImages": args.expected_count,
        "sourceChecksumsValidatedBeforeRun": True,
        "images": per_page,
    }
    atomic_json(output / "run-manifest.json", run_manifest)
    atomic_json(
        output / "summary.json",
        {
            "schemaVersion": 1,
            "imageCount": args.expected_count,
            "pageVisualReviewCount": args.expected_count,
            "detectionCompleteImageCount": len(no_text_pages),
            "detectionPendingImageCount": args.expected_count - len(no_text_pages),
            "completedNoTextImageCount": len(no_text_pages),
            "pendingTextImageCount": args.expected_count - len(no_text_pages),
            "sourceChecksumFailures": 0,
            "dimensionFailures": 0,
            "changedOutsideMaskPixels": 0,
            "noTextMaskNonzeroPixels": 0,
            "allCompletedOutputsReviewed": True,
            "allImagesCompleted": False,
        },
    )
    print(
        "[clean-plate-bootstrap] "
        f"images={args.expected_count} no_text_complete={len(no_text_pages)} "
        f"pending_text={args.expected_count - len(no_text_pages)}",
        flush=True,
    )
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (BootstrapError, KeyError, OSError, ValueError) as error:
        print(f"error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
