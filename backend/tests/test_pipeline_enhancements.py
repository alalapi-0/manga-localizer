from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw

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


class _PublicTrustFixtureDetector:
    """Deterministic proposals over a copyright-safe generated manga-like page."""

    def detect_text_regions(self, image: Path, **_options: Any) -> list[OCRRegion]:
        return [
            OCRRegion(15, 15, 90, 38, "BUBBLE-H", 0.95, "horizontal"),
            OCRRegion(165, 15, 28, 90, "NONBUBBLE-V", 0.05, "vertical"),
            OCRRegion(55, 105, 105, 55, "SFX", 0.10, "auto"),
            OCRRegion(198, 130, 20, 24, "一", 0.99, "vertical"),
            OCRRegion(15, 205, 75, 70, "", 0.99, "auto"),
        ]


def _public_trust_fixture_page() -> bytes:
    image = Image.new("RGB", (240, 320), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 112, 60), radius=16, outline="black", width=3)
    draw.rectangle((158, 8, 202, 112), outline="black", width=2)
    draw.polygon(((45, 160), (92, 90), (172, 155), (115, 175)), outline="black")
    for offset in range(0, 70, 8):
        draw.line((12 + offset, 205, 80, 275 - offset // 2), fill="black", width=2)
    draw.text((25, 24), "BUBBLE", fill="black")
    draw.text((66, 120), "SFX", fill="black")
    draw.text((201, 134), "1", fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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
        assert (
            not {
                "artifact",
                "url",
                "inpaintedArtifact",
                "inpaintedUrl",
                "maskArtifact",
                "maskUrl",
                "typesetArtifact",
                "typesetUrl",
            }
            & output.keys()
        )
        assert output["originalSize"] == [240, 320]
        assert output["processedSize"] == [480, 640]
        assert (
            client.get(f"/api/images/{image['id']}/content?variant=preprocessed").status_code == 200
        )
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
        manifest_deadline = time.monotonic() + 2.0
        manifest_image: dict[str, Any] | None = None
        while time.monotonic() < manifest_deadline:
            manifest = json.loads((project_root / "project/project.json").read_text("utf-8"))
            manifest_image = next(
                entry for entry in manifest["images"] if entry["id"] == image["id"]
            )
            if manifest_image["status"]["preprocess"] == "done":
                break
            time.sleep(0.02)
        assert manifest_image is not None
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
        assert updated["detectorConfidence"] == 0.9
        assert updated["ocrConfidence"] == 0.86
        assert updated["recognition"]["detection"]["inputVariant"] == "preprocessed"
        assert updated["recognition"]["detection"]["language"] is None
        assert updated["recognition"]["ocr"]["language"] == "jpn_vert"
        assert updated["trustDisposition"] == "review"
        assert updated["trustReason"] == "automatic-ocr-complete"
        assert updated["recognition"]["ocr"]["selectedIndex"] == 1
        assert updated["recognition"]["ocr"]["attempts"] == [
            {
                "provider": "tesseract",
                "inputVariant": "preprocessed",
                "confidence": 0.1,
                "direction": "vertical",
                "language": "jpn_vert",
            },
            {
                "provider": "tesseract",
                "inputVariant": "original",
                "confidence": 0.86,
                "direction": "vertical",
                "language": "jpn_vert",
            },
        ]
        assert updated["repair"]["ocrAttemptCount"] == 2
        assert updated["repair"]["ocrInputVariant"] == "original"
        output = completed["items"][0]["output"]
        assert output["attemptCount"] == 2
        assert output["confidenceBuckets"] == {
            "missing": 0,
            "low": 0,
            "medium": 0,
            "high": 1,
        }
        serialized_output = json.dumps(output, ensure_ascii=False)
        assert "原图回退成功" not in serialized_output
        assert updated["id"] not in serialized_output
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["status"]["inpaint"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404

        trusted = client.patch(
            f"/api/regions/{updated['id']}",
            json={"confirmed": True, "expectedRevision": updated["revision"]},
        )
        assert trusted.status_code == 200, trusted.text
        assert trusted.json()["trustDisposition"] == "trusted"
        rerun = client.post(
            f"/api/projects/{project['id']}/preprocess",
            json={
                "imageIds": [image["id"]],
                "options": {"provider": "opencv-pillow", "profile": "off"},
            },
        )
        rerun_job = _wait_job(client, rerun.json()["id"])
        assert rerun_job["status"] == "completed", rerun_job
        revoked = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert revoked["confirmed"] is False
        assert revoked["trustDisposition"] == "review"
        assert revoked["trustReason"] == "trust-input-changed"
        assert revoked["recognition"]["ocr"]["attemptCount"] == 2


def test_detection_rerun_retains_prior_candidates_and_appends_reading_order(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    app.state.providers.ppocr = _ScaledDetector()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])

        submitted = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {"provider": "ppocr-v3"}},
        )
        completed = _wait_job(client, submitted.json()["id"])
        assert completed["status"] == "completed", completed
        first = client.get(f"/api/images/{image['id']}/regions").json()[0]
        trusted = client.patch(
            f"/api/regions/{first['id']}",
            json={"confirmed": True, "expectedRevision": first["revision"]},
        )
        assert trusted.status_code == 200, trusted.text
        assert trusted.json()["trustDisposition"] == "trusted"

        rerun = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {"provider": "ppocr-v3"}},
        )
        rerun_completed = _wait_job(client, rerun.json()["id"])
        assert rerun_completed["status"] == "completed", rerun_completed

        regions = client.get(f"/api/images/{image['id']}/regions").json()
        assert len(regions) == 2
        assert [region["order"] for region in regions] == [0, 1]
        assert len({region["id"] for region in regions}) == 2
        prior = next(region for region in regions if region["id"] == first["id"])
        appended = next(region for region in regions if region["id"] != first["id"])
        assert prior["confirmed"] is True
        assert prior["trustDisposition"] == "trusted"
        assert prior["trustReason"] == "human-confirmed"
        assert appended["confirmed"] is False
        assert appended["trustDisposition"] == "review"
        assert appended["trustReason"] == "automatic-proposal"


def test_public_generated_content_classes_remain_reviewable_across_empty_rerun_and_reopen(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    app.state.providers.ppocr = _PublicTrustFixtureDetector()
    project_root = tmp_path / "public-trust-project"
    with TestClient(app) as client:
        project = create_project(client, project_root)
        image = upload_image(
            client,
            project["id"],
            relative_path="generated/trust-gate.png",
            data=_public_trust_fixture_page(),
        )
        submitted = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {"provider": "ppocr-v3"}},
        )
        completed = _wait_job(client, submitted.json()["id"])
        assert completed["status"] == "completed", completed
        serialized_job = json.dumps(completed, ensure_ascii=False)
        assert '"regionId"' not in serialized_job
        assert not any("regionId" in item for item in completed["items"])
        assert all(
            fixture_text not in serialized_job
            for fixture_text in ("BUBBLE-H", "NONBUBBLE-V", "SFX")
        )

        regions = client.get(f"/api/images/{image['id']}/regions").json()
        assert len(regions) == 5
        assert all(region["trustDisposition"] == "review" for region in regions)
        assert {region["direction"] for region in regions} == {
            "horizontal",
            "vertical",
            "auto",
        }
        by_candidate = {region["repair"]["detectedTextCandidate"]: region for region in regions}
        assert by_candidate["BUBBLE-H"]["detectorConfidence"] == 0.95
        assert by_candidate["NONBUBBLE-V"]["detectorConfidence"] == 0.05
        assert by_candidate["SFX"]["detectorConfidence"] == 0.10
        assert by_candidate["一"]["detectorConfidence"] == 0.99
        assert by_candidate[""]["detectorConfidence"] == 0.99

        for candidate, region_type in {
            "BUBBLE-H": "dialogue",
            "NONBUBBLE-V": "narration",
            "SFX": "sound_effect",
            "一": "title",
            "": "background",
        }.items():
            region = by_candidate[candidate]
            updated = client.patch(
                f"/api/regions/{region['id']}",
                json={"type": region_type, "expectedRevision": region["revision"]},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["trustDisposition"] == "review"

        app.state.providers.ppocr = _EmptyDetector()
        empty_rerun = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {"provider": "ppocr-v3"}},
        )
        empty_completed = _wait_job(client, empty_rerun.json()["id"])
        assert empty_completed["status"] == "completed", empty_completed
        assert empty_completed["items"][0]["output"]["count"] == 0
        retained = client.get(f"/api/images/{image['id']}/regions").json()
        assert len(retained) == 5
        retained_ids = {region["id"] for region in retained}

    fresh_settings = settings.model_copy(update={"data_dir": tmp_path / "fresh-catalog"})
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        reopened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(project_root / "project/project.json")},
        )
        assert reopened.status_code == 200, reopened.text
        reopened_regions = fresh.get(f"/api/images/{image['id']}/regions").json()
        assert {region["id"] for region in reopened_regions} == retained_ids
        assert all(region["trustDisposition"] == "review" for region in reopened_regions)
        editable = reopened_regions[0]
        edited = fresh.patch(
            f"/api/regions/{editable['id']}",
            json={"sourceText": "人工复核", "expectedRevision": editable["revision"]},
        )
        assert edited.status_code == 200, edited.text


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
        assert failed["items"][0]["error"] == (
            "Image preprocessing failed; inspect the private project log"
        )
        store = app.state.registry.get(project["id"])
        internal = app.state.queue.get_job(store, submitted.json()["id"])
        assert "Unknown image preprocessing provider" in internal.items[0].error


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
            Image.open(project_root / "generated/preprocessed/profile-off.png") as processed,
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
