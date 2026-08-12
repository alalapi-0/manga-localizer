from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image, ImageChops

from manga_localizer.config import Settings
from manga_localizer.main import create_app
from manga_localizer.providers.ocr import OCRRegion

from .conftest import create_project, png_bytes, upload_image


def _wait_job(client: TestClient, job_id: str, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not finish")


class _ScaledDetector:
    def __init__(self) -> None:
        self.input_size: tuple[int, int] | None = None

    def detect_text_regions(self, image: Path, **_options: Any) -> list[OCRRegion]:
        with Image.open(image) as opened:
            self.input_size = opened.size
        return [
            OCRRegion(
                x=20,
                y=30,
                width=160,
                height=200,
                text="",
                confidence=0.9,
                direction="vertical",
                polygon=((20, 30), (180, 30), (180, 230), (20, 230)),
            )
        ]


class _RetryOCR:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def recognize_region(
        self,
        image: Path,
        region: dict[str, float],
        **_options: Any,
    ) -> OCRRegion:
        self.inputs.append(image.parent.name)
        processed = image.parent.name == "preprocessed"
        return OCRRegion(
            x=round(region["x"]),
            y=round(region["y"]),
            width=round(region["width"]),
            height=round(region["height"]),
            text="" if processed else "原图回退成功",
            confidence=0.1 if processed else 0.86,
            direction="vertical",
        )


class _EmptyDetector:
    def __init__(self) -> None:
        self.calls = 0

    def detect_text_regions(self, image: Path, **_options: Any) -> list[OCRRegion]:
        self.calls += 1
        return []


def test_preprocess_artifact_drives_scaled_detection_and_ocr_fallback(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    detector = _ScaledDetector()
    ocr = _RetryOCR()
    app.state.providers.ppocr = detector
    app.state.providers.ocr = ocr

    project_root = tmp_path / "project"
    with TestClient(app) as client:
        project = create_project(client, project_root)
        source = png_bytes((240, 320), rectangle=(30, 40, 130, 180))
        image = upload_image(client, project["id"], relative_path="page.png", data=source)
        source_path = project_root / "source/page.png"
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

        submitted = client.post(
            f"/api/projects/{project['id']}/preprocess",
            json={
                "imageIds": [image["id"]],
                "options": {"provider": "opencv-pillow", "profile": "ocr-friendly"},
            },
        )
        assert submitted.status_code == 202, submitted.text
        preprocessed = _wait_job(client, submitted.json()["id"])
        assert preprocessed["status"] == "completed", preprocessed
        output = preprocessed["items"][0]["output"]
        assert output["originalSize"] == [240, 320]
        assert output["processedSize"] == [480, 640]
        assert (
            client.get(f"/api/images/{image['id']}/content?variant=preprocessed").status_code == 200
        )
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
        manifest = json.loads((project_root / "project/project.json").read_text("utf-8"))
        manifest_image = next(entry for entry in manifest["images"] if entry["id"] == image["id"])
        assert manifest_image["status"]["preprocess"] == "done"
        assert manifest_image["providers"]["preprocessing"] == "opencv-pillow"

        detect = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {"provider": "ppocr-v3"}},
        )
        assert _wait_job(client, detect.json()["id"])["status"] == "completed"
        assert detector.input_size == (480, 640)
        region = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert (region["x"], region["y"], region["width"], region["height"]) == (
            10.0,
            15.0,
            80.0,
            100.0,
        )

        inpaint = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {"provider": "opencv"}},
        )
        assert _wait_job(client, inpaint.json()["id"])["status"] == "completed"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 200

        recognized = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"imageIds": [image["id"]], "options": {"provider": "tesseract"}},
        )
        completed = _wait_job(client, recognized.json()["id"])
        assert completed["status"] == "completed", completed
        assert ocr.inputs == ["preprocessed", "source"]
        updated = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert updated["sourceText"] == "原图回退成功"
        assert updated["repair"]["ocrAttemptCount"] == 2
        assert updated["repair"]["ocrInputVariant"] == "original"
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["status"]["inpaint"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404


def test_unknown_preprocess_provider_is_rejected_by_the_job(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        submitted = client.post(
            f"/api/projects/{project['id']}/preprocess",
            json={
                "imageIds": [image["id"]],
                "options": {"provider": "definitely-unsupported"},
            },
        )
        failed = _wait_job(client, submitted.json()["id"])
        assert failed["status"] == "failed"
        assert "Unknown image preprocessing provider" in failed["items"][0]["error"]


def test_job_profile_override_resets_project_preprocessing_switches(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(
            client,
            project["id"],
            relative_path="profile-off.png",
            data=png_bytes((240, 320), rectangle=(30, 40, 130, 180)),
        )
        submitted = client.post(
            f"/api/projects/{project['id']}/preprocess",
            json={
                "imageIds": [image["id"]],
                "options": {"provider": "opencv-pillow", "profile": "off"},
            },
        )
        completed = _wait_job(client, submitted.json()["id"])

        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["profile"] == "off"
        assert output["processedSize"] == [240, 320]
        with (
            Image.open(project_root / "source/profile-off.png") as original,
            Image.open(project_root / output["artifact"]) as processed,
        ):
            assert (
                ImageChops.difference(original.convert("RGBA"), processed.convert("RGBA")).getbbox()
                is None
            )


def test_completed_empty_detection_is_not_replaced_by_ocr_fallback(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    detector = _EmptyDetector()
    app.state.providers.ppocr = detector
    # If OCR incorrectly auto-detects, the project's default Tesseract adapter is
    # reached. Replacing it with the recognition-only fake makes that regression fail.
    app.state.providers.ocr = _RetryOCR()

    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        detect = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {"provider": "ppocr-v3"}},
        )
        assert _wait_job(client, detect.json()["id"])["status"] == "completed"
        assert detector.calls == 1
        assert client.get(f"/api/images/{image['id']}/regions").json() == []

        recognized = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"imageIds": [image["id"]], "options": {"provider": "tesseract"}},
        )
        completed = _wait_job(client, recognized.json()["id"])

        assert completed["status"] == "completed", completed
        assert completed["items"][0]["output"]["count"] == 0
        assert detector.calls == 1
        assert client.get(f"/api/images/{image['id']}/regions").json() == []
