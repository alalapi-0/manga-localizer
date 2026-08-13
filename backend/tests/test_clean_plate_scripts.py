from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / name
    specification = importlib.util.spec_from_file_location(path.stem, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_bootstrap_materializes_and_verifies_explicit_no_text_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = load_script("bootstrap_clean_plate_run.py")
    input_root = tmp_path / "input"
    input_root.mkdir()
    source = input_root / "page.png"
    Image.new("RGB", (32, 24), "#f4f4f4").save(source)
    manifest_path = tmp_path / "review-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "privacy": {
                    "gitIgnored": True,
                    "ocrTextStored": False,
                    "absolutePathsStored": False,
                },
                "images": [
                    {
                        "imageId": "page-0001",
                        "sourceRelativePath": "page.png",
                        "sourceChecksum": script.file_sha256(source),
                        "width": 32,
                        "height": 24,
                        "regions": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "run"

    def allow_test_output(path: Path) -> Path:
        resolved = path.resolve()
        resolved.mkdir()
        return resolved

    monkeypatch.setattr(script, "require_ignored_empty_output", allow_test_output)
    result = script.run(
        argparse.Namespace(
            input=input_root,
            review_manifest=manifest_path,
            output=output,
            expected_count=1,
            no_text_pages="1",
            label="test-clean-plate",
            poll_seconds=0.01,
            timeout_seconds=30.0,
        )
    )
    assert result == 0
    summary = json.loads((output / "summary.json").read_text("utf-8"))
    assert summary == {
        "schemaVersion": 1,
        "imageCount": 1,
        "pageVisualReviewCount": 1,
        "detectionCompleteImageCount": 1,
        "detectionPendingImageCount": 0,
        "completedNoTextImageCount": 1,
        "pendingTextImageCount": 0,
        "sourceChecksumFailures": 0,
        "dimensionFailures": 0,
        "changedOutsideMaskPixels": 0,
        "noTextMaskNonzeroPixels": 0,
        "allCompletedOutputsReviewed": True,
        "allImagesCompleted": False,
    }
    run_manifest = json.loads((output / "run-manifest.json").read_text("utf-8"))
    page = run_manifest["images"][0]
    assert page["reviewStatus"] == "no-text-reviewed"
    assert page["final"]["maskNonzeroPixels"] == 0
    assert page["final"]["changedOutsideMaskPixels"] == 0
    with sqlite3.connect(output / "workspace" / "project" / "project.sqlite3") as database:
        status = json.loads(database.execute("SELECT status FROM images").fetchone()[0])
        region_count = database.execute("SELECT count(*) FROM text_regions").fetchone()[0]
    assert status["reviewState"] == "no-text-reviewed"
    assert region_count == 0


def test_bootstrap_candidates_persist_review_only_detection_evidence(
    tmp_path: Path,
) -> None:
    script = load_script("bootstrap_clean_plate_run.py")
    input_root = tmp_path / "input"
    input_root.mkdir()
    Image.new("RGB", (32, 24), "white").save(input_root / "page.png")
    app = script.create_app(
        script.Settings(data_dir=tmp_path / "catalog"),
        start_worker=False,
    )

    with script.TestClient(app) as client:
        project = client.post(
            "/api/projects",
            json={"name": "generated-review", "outputPath": str(tmp_path / "project")},
        ).json()
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(input_root)]},
        )
        assert imported.status_code == 201
        image_by_path = script.bootstrap_candidates(
            client,
            project["id"],
            [
                {
                    "imageId": "page-0001",
                    "sourceRelativePath": "page.png",
                    "regions": [
                        {
                            "geometry": {
                                "x": 2,
                                "y": 3,
                                "width": 10,
                                "height": 8,
                                "rotation": 0,
                            },
                            "confidence": 0.73,
                        }
                    ],
                }
            ],
            set(),
        )
        regions = client.get(f"/api/images/{image_by_path['page.png']['id']}/regions").json()

    assert len(regions) == 1
    assert regions[0]["detectorConfidence"] == 0.73
    assert regions[0]["ocrConfidence"] is None
    assert regions[0]["trustDisposition"] == "review"
    assert regions[0]["trustReason"] == "automatic-proposal"
    assert regions[0]["recognition"]["detection"] == {
        "confidence": 0.73,
        "provider": "visual-review-union-candidates",
        "inputVariant": "original",
        "language": None,
    }


def test_review_pack_writes_anonymous_full_resolution_overlays(tmp_path: Path) -> None:
    script = load_script("prepare_clean_plate_review.py")
    input_root = tmp_path / "input"
    output = tmp_path / "output"
    input_root.mkdir()
    output.mkdir()
    Image.new("RGB", (120, 80), "white").save(input_root / "source.png")
    record = {
        "imageId": "page-0001",
        "sourceRelativePath": "source.png",
        "regions": [
            {
                "regionId": "region-0001",
                "geometry": {
                    "x": 20,
                    "y": 10,
                    "width": 40,
                    "height": 30,
                    "rotation": 0,
                },
                "polygon": None,
            }
        ],
    }
    numbered_count, coordinate_count = script.make_numbered_page_overlays(
        [record],
        input_root=input_root,
        output=output,
    )
    assert (numbered_count, coordinate_count) == (1, 1)
    for relative in (
        "numbered-pages/page-0001.png",
        "coordinate-pages/page-0001.png",
    ):
        with Image.open(output / relative) as image:
            image.load()
            assert image.size == (120, 80)
