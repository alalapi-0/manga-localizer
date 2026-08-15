from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import manga_localizer.queue as queue_module
from manga_localizer.config import Settings
from manga_localizer.database import ImageAsset, Job, JobStatus, TextRegion
from manga_localizer.imaging import create_mask
from manga_localizer.main import create_app
from manga_localizer.providers.ocr import OCRRegion
from manga_localizer.services.images import review_image_stage, stage_reviews
from manga_localizer.services.projects import RevisionConflict

from .conftest import create_project, png_bytes, upload_image


def _wait_job(client: TestClient, job_id: str, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not finish")


def _stage_review_json(
    client: TestClient,
    image: dict[str, Any],
    stage: str,
    state: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "state": state,
        "expectedRevision": image["revision"],
    }
    if state == "pending":
        return body
    generated_stage = {
        "preprocess": "preprocessed",
        "inpaint": "inpainted",
        "typeset": "typeset",
    }[stage]
    artifact = client.get(f"/api/images/{image['id']}/generated/{generated_stage}")
    assert artifact.status_code == 200, artifact.text
    body["observedArtifactChecksum"] = hashlib.sha256(artifact.content).hexdigest()
    if stage == "inpaint":
        mask = client.get(f"/api/images/{image['id']}/generated/mask")
        assert mask.status_code == 200, mask.text
        body["observedMaskChecksum"] = hashlib.sha256(mask.content).hexdigest()
    return body


def _add_region(
    client: TestClient,
    image_id: str,
    *,
    source: str = "こんにちは",
    translation: str = "人工译文",
    confirmed: bool = False,
) -> dict[str, Any]:
    response = client.post(
        f"/api/images/{image_id}/regions",
        json={
            "x": 30,
            "y": 40,
            "width": 100,
            "height": 120,
            "sourceText": source,
            "translationText": translation,
            "confirmed": confirmed,
            "direction": "vertical",
            "repair": {"padding": 3, "maskMode": "region"},
            "style": {"fontSize": 26, "minFontSize": 10, "strokeWidth": 1},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _confirm_region(client: TestClient, region: dict[str, Any]) -> dict[str, Any]:
    confirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["trustDisposition"] == "trusted"
    return payload


def _review_image(
    client: TestClient,
    project_id: str,
    image_id: str,
    review_state: str = "reviewed",
) -> dict[str, Any]:
    image = next(
        item
        for item in client.get(f"/api/projects/{project_id}/images").json()
        if item["id"] == image_id
    )
    response = client.patch(
        f"/api/images/{image_id}/review",
        json={"reviewState": review_state, "expectedRevision": image["revision"]},
    )
    assert response.status_code == 200, response.text
    reviewed = response.json()
    for stage in ("preprocess", "inpaint", "typeset"):
        if reviewed["status"].get(stage) != "done":
            continue
        stage_response = client.patch(
            f"/api/images/{image_id}/stage-reviews/{stage}",
            json=_stage_review_json(client, reviewed, stage, "accepted"),
        )
        assert stage_response.status_code == 200, stage_response.text
        reviewed = stage_response.json()
    return reviewed


def _set_stage_review(
    client: TestClient,
    image: dict[str, Any],
    stage: str,
    state: str,
) -> dict[str, Any]:
    response = client.patch(
        f"/api/images/{image['id']}/stage-reviews/{stage}",
        json=_stage_review_json(client, image, stage, state),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_queue_control_actions_and_job_options_drop_credentials(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    region = _add_region(client, image["id"])
    response = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "regionIds": [region["id"]],
            "options": {
                "provider": "mock",
                "apiKey": "never-persist",
                "api-key": "never-persist-hyphenated",
                "Authorization": "Bearer never-persist-auth",
                "nested": {"token": "x", "serviceCredential": "never-persist-credential"},
            },
        },
    )
    assert response.status_code == 202
    job = response.json()
    assert "options" not in job
    assert "never-persist" not in json.dumps(job)
    store = client.app.state.registry.get(project["id"])
    internal = client.app.state.queue.get_job(store, job["id"])
    serialized_options = json.dumps(internal.options)
    assert internal.options["provider"] == "mock"
    assert "never-persist" not in serialized_options
    assert "Authorization" not in serialized_options
    assert "token" not in serialized_options

    paused = client.post(f"/api/jobs/{job['id']}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    resumed = client.post(f"/api/jobs/{job['id']}/resume")
    assert resumed.json()["status"] == "queued"
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/jobs/{job['id']}/retry")
    assert retried.json()["status"] == "queued"
    assert retried.json()["items"][0]["status"] == "queued"


def test_export_enqueue_rejects_invalid_typed_options_before_writing(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    invalid_options = (
        ({"format": "archive"}, "Export format"),
        ({"format": ["json"]}, "Export format"),
        ({"imageVariant": "original"}, "Export imageVariant"),
        ({"imageVariant": ["typeset"]}, "Export imageVariant"),
        ({"conflict": "replace"}, "Export conflict"),
        ({"preserveTree": "false"}, "Export preserveTree"),
        ({"preserveTree": 1}, "Export preserveTree"),
    )
    for index, (invalid, expected_error) in enumerate(invalid_options):
        target = tmp_path / f"fresh-target-{index}"
        response = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {**invalid, "outputPath": str(target)},
            },
        )
        assert response.status_code == 400, response.text
        assert expected_error in response.json()["detail"]
        assert not target.exists()


def test_job_item_concurrency_is_bounded_and_export_is_serialized(
    client: TestClient,
    tmp_path: Path,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(client, tmp_path / "project")
    images = [
        upload_image(client, project["id"], relative_path=f"pages/{index}.png")
        for index in range(4)
    ]
    rejected = client.post(
        f"/api/projects/{project['id']}/detect",
        json={"imageIds": [images[0]["id"]], "options": {"concurrency": 9}},
    )
    assert rejected.status_code == 400

    submitted = client.post(
        f"/api/projects/{project['id']}/detect",
        json={
            "imageIds": [image["id"] for image in images],
            "options": {"concurrency": 2},
        },
    )
    assert submitted.status_code == 202
    job_id = submitted.json()["id"]
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value

    lock = threading.Lock()
    active = 0
    peak = 0

    def process_item(_store, _job_id: str, item_id: str) -> dict[str, str]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return {"itemId": item_id}

    monkeypatch.setattr(app.state.queue, "_process_item", process_item)
    asyncio.run(app.state.queue._execute(store, job_id))

    completed = app.state.queue.get_job(store, job_id)
    assert completed.status == JobStatus.COMPLETED.value
    assert completed.completed == 4
    assert completed.options["concurrency"] == 2
    assert peak == 2
    assert app.state.queue._job_concurrency("export", {"concurrency": 8}) == 1


def test_export_failure_guard_refreshes_after_acquiring_the_export_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        submitted = client.post(
            f"/api/projects/{project['id']}/export",
            json={"imageIds": [image["id"]], "options": {"format": "images"}},
        ).json()
        claimed = app.state.queue._claim_next()
        assert claimed is not None
        store, job_id = claimed
        assert job_id == submitted["id"]
        running = app.state.queue.get_job(store, job_id)
        item_id = running.items[0].id
        assert app.state.queue._begin_item(store, job_id, item_id) is True

        original_lock = store.lock
        edited_revision: list[int] = []

        class EditBeforeSecondAcquisition:
            def __init__(self) -> None:
                self.acquisitions = 0

            def __enter__(self):
                self.acquisitions += 1
                if self.acquisitions == 2:
                    with store.sessions() as session:
                        current = session.get(ImageAsset, image["id"])
                        assert current is not None
                        current.status = {**dict(current.status), "manualMarker": "edited"}
                        current.revision += 1
                        edited_revision.append(current.revision)
                        session.commit()
                original_lock.acquire()
                return self

            def __exit__(self, *_args) -> None:
                original_lock.release()

        def fail_current_export(*_args, **_kwargs):
            raise RuntimeError("current export failure")

        monkeypatch.setattr(app.state.queue, "_process_item", fail_current_export)
        store.lock = EditBeforeSecondAcquisition()
        try:
            app.state.queue._execute_item_sync(store, job_id, item_id)
        finally:
            store.lock = original_lock

        failed = app.state.queue.get_job(store, job_id)
        assert failed.items[0].status == JobStatus.FAILED.value
        assert failed.items[0].error == "current export failure"
        with store.session() as session:
            persisted_image = session.get(ImageAsset, image["id"])
            assert persisted_image is not None
            assert persisted_image.status["manualMarker"] == "edited"
        current_image = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert current_image["status"]["export"] == "failed"
        assert current_image["revision"] == edited_revision[0] + 1
        assert current_image["processingErrors"][-1]["error"] == (
            "Export failed; inspect the private project log"
        )


def test_mock_and_manual_translation_jobs_preserve_reviewed_text(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], translation="不要覆盖", confirmed=True)

        manual = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {}},
        ).json()
        assert _wait_job(client, manual["id"])["status"] == "completed"
        reviewed = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert reviewed["translationText"] == "不要覆盖"

        mock = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        ).json()
        assert _wait_job(client, mock["id"])["status"] == "completed"
        translated = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert translated["translationText"] == "你好"
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["translatorProvider"] == "mock"
        assert image_state["status"]["translation"] == "done"


def test_argos_translation_job_uses_local_pivot_without_mock_prefix(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)

    class FakeHop:
        def __init__(self, mapping: dict[str, str]):
            self.mapping = mapping

        def translate(self, text: str) -> str:
            return self.mapping.get(text, text)

    def factory(path: Path):
        if path.name == "argos-ja-en":
            return FakeHop({"こんにちは": "Hello"})
        return FakeHop({"Hello": "你好"})

    app.state.providers.argos._hop_factory = factory
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        job = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "argos-ja-zh"}},
        ).json()
        completed = _wait_job(client, job["id"])
        assert completed["status"] == "completed"
        translated = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert translated["translationText"] == "你好"
        assert translated["translationText"].startswith("【模拟译文】") is False
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["translatorProvider"] == "argos-ja-zh"


def test_confidence_never_promotes_trust_and_human_disposition_gates_pipeline(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = client.post(
            f"/api/images/{image['id']}/regions",
            json={
                "x": 30,
                "y": 40,
                "width": 100,
                "height": 120,
                "sourceText": "高置信也必须人工确认",
                "translationText": "待翻译",
                "confidence": 0.99,
                "repair": {"detectorGenerated": True, "maskMode": "region"},
            },
        ).json()
        assert region["trustDisposition"] == "review"

        rejected = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        failed = _wait_job(client, rejected.json()["id"])
        assert failed["status"] == "failed"
        assert failed["items"][0]["error"] == (
            "Translation failed; inspect the private project log"
        )

        safe = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {"repairPolicy": "safe"}},
        )
        safe_job = _wait_job(client, safe.json()["id"])
        assert safe_job["status"] == "completed", safe_job
        assert safe_job["items"][0]["output"]["eligibleRegionCount"] == 0

        low = client.patch(
            f"/api/regions/{region['id']}",
            json={"confidence": 0.1, "expectedRevision": region["revision"]},
        ).json()
        confirmed = client.patch(
            f"/api/regions/{region['id']}",
            json={"confirmed": True, "expectedRevision": low["revision"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["trustDisposition"] == "trusted"

        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={
                "regionIds": [region["id"]],
                "options": {"provider": "mock"},
            },
        )
        completed = _wait_job(client, translated.json()["id"])
        assert completed["status"] == "completed", completed
        translation_output = completed["items"][0]["output"]
        assert translation_output["count"] == 1
        assert "translation" not in translation_output
        serialized_output = json.dumps(translation_output, ensure_ascii=False)
        assert region["id"] not in serialized_output
        assert "高置信也必须人工确认" not in serialized_output
        assert "你好" not in serialized_output
        after_translation = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert after_translation["confirmed"] is False
        assert after_translation["trustDisposition"] == "trusted"

        ignored = client.patch(
            f"/api/regions/{region['id']}",
            json={"ignored": True, "expectedRevision": after_translation["revision"]},
        )
        assert ignored.status_code == 200, ignored.text
        assert ignored.json()["trustDisposition"] == "ignored"
        ignored_translation = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        assert _wait_job(client, ignored_translation.json()["id"])["status"] == "failed"


def test_legacy_confirmation_is_materialized_before_translation_resets_page_review(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        store = app.state.registry.get(project["id"])
        with store.session() as session:
            legacy = session.get(TextRegion, region["id"])
            assert legacy is not None
            legacy.recognition = {}

        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        assert translated.status_code == 202, translated.text
        completed = _wait_job(client, translated.json()["id"])
        assert completed["status"] == "completed", completed
        current = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["confirmed"] is False
        assert current["trustDisposition"] == "trusted"
        assert current["trustReason"] == "legacy-confirmed"


def test_region_repair_settings_drive_the_inpainting_job(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        updated = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "repair": {
                    "method": "solid",
                    "maskMode": "region",
                    "maskPadding": 0,
                    "dilation": 0,
                    "radius": 1,
                    "fillColor": "#ef233c",
                },
                "expectedRevision": region["revision"],
            },
        )
        assert updated.status_code == 200, updated.text

        queued = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {"provider": "opencv"}},
        )
        completed = _wait_job(client, queued.json()["id"])
        assert completed["status"] == "completed", completed
        erased = client.get(f"/api/images/{image['id']}/content?variant=erased")
        with Image.open(io.BytesIO(erased.content)) as result:
            assert result.convert("RGB").getpixel((50, 60)) == (239, 35, 60)


def test_inpainting_routes_each_region_to_its_selected_provider_and_preserves_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)

    class RecordingInpainter:
        def __init__(self, name: str, color: str) -> None:
            self.name = name
            self.color = color
            self.calls = 0
            self.inpaint_options: list[dict[str, Any]] = []
            self.masks: list[np.ndarray] = []

        def create_mask(self, image, regions, **options):
            return create_mask(image, regions, **options)

        def inpaint(self, image, mask, **options):
            self.calls += 1
            self.inpaint_options.append(options)
            self.masks.append(np.asarray(mask, dtype=np.uint8).copy())
            if isinstance(image, Path):
                with Image.open(image) as opened:
                    size = opened.size
            else:
                size = image.size
            return Image.new("RGB", size, self.color)

    opencv = RecordingInpainter("opencv", "#ff0000")
    lama = RecordingInpainter("lama-onnx", "#0000ff")

    def routed_provider(name: str):
        return {"opencv": opencv, "lama-onnx": lama}[name]

    monkeypatch.setattr(app.state.providers, "inpainter", routed_provider)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"], data=png_bytes((120, 80), color="white"))
        regions = []
        for x, provider in ((10, "opencv"), (70, "lama-onnx")):
            response = client.post(
                f"/api/images/{image['id']}/regions",
                json={
                    "x": x,
                    "y": 20,
                    "width": 30,
                    "height": 30,
                    "sourceText": "文字",
                    "confirmed": True,
                    "repair": {
                        "maskMode": "region",
                        "maskPadding": 0,
                        "dilation": 0,
                        "feather": 3,
                        "inpainterProvider": provider,
                        "maskEdits": {
                            "version": 1,
                            "strokes": [
                                {"mode": "add", "radius": 3, "points": [[5, 5]]},
                                {
                                    "mode": "erase",
                                    "radius": 5,
                                    "points": [[x + 15, 35]],
                                },
                            ],
                        },
                    },
                },
            )
            assert response.status_code == 201, response.text
            regions.append(response.json())

        queued = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {"provider": "opencv"}},
        )
        completed = _wait_job(client, queued.json()["id"])
        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["inpaintingProvider"] == "mixed"
        assert output["inpaintingProviders"] == ["lama-onnx", "opencv"]
        assert "regionInpaintingProviders" not in output
        assert opencv.calls == 1
        assert lama.calls == 1
        assert lama.inpaint_options == [{"context_padding": 64, "feather": 0}]
        for provider, region in ((opencv, regions[0]), (lama, regions[1])):
            expected_mask = create_mask(
                (120, 80),
                [{**region, **region["repair"], "padding": 0}],
                padding=0,
                dilation=0,
                feather=3,
                mask_mode="region",
            )
            assert np.array_equal(provider.masks[0], expected_mask)
            center_x = int(region["x"] + region["width"] / 2)
            assert provider.masks[0][35, center_x] == 0
            assert provider.masks[0][35, center_x + 4] == 0
            assert provider.masks[0][5, 5] > provider.masks[0][5, 9] > 0
        persisted_mask_response = client.get(f"/api/images/{image['id']}/generated/mask")
        assert persisted_mask_response.status_code == 200, persisted_mask_response.text
        with Image.open(io.BytesIO(persisted_mask_response.content)) as persisted:
            persisted_mask = np.asarray(persisted.convert("L"), dtype=np.uint8)
        assert np.array_equal(persisted_mask, np.maximum(opencv.masks[0], lama.masks[0]))
        erased = client.get(f"/api/images/{image['id']}/content?variant=inpainted")
        with Image.open(io.BytesIO(erased.content)) as result:
            pixels = result.convert("RGB")
            assert pixels.getpixel((15, 35)) == (255, 0, 0)
            assert pixels.getpixel((75, 35)) == (0, 0, 255)
            assert pixels.getpixel((25, 35)) == (255, 255, 255)
            assert pixels.getpixel((85, 35)) == (255, 255, 255)
            assert pixels.getpixel((55, 70)) == (255, 255, 255)


def test_inpaint_stores_comparison_candidates_and_selection_keeps_mask_outside(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        _add_region(client, image["id"], confirmed=True)
        queued = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {"provider": "opencv"}},
        )
        completed = _wait_job(client, queued.json()["id"])
        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["inpaintCandidate"] == "primary"
        assert output["inpaintCandidateCount"] == 4

        listed = client.get(f"/api/projects/{project['id']}/images").json()
        current = next(item for item in listed if item["id"] == image["id"])
        assert current["inpaintCandidate"] == "primary"
        ids = [item["id"] for item in current["inpaintCandidates"]]
        assert ids == ["primary", "opencv-ns", "opencv-telea", "lineart-guided"]
        primary = client.get(f"/api/images/{image['id']}/generated/inpaint-candidates/primary")
        assert primary.status_code == 200, primary.text
        selected = client.patch(
            f"/api/images/{image['id']}/inpaint-candidate",
            json={
                "candidateId": "lineart-guided",
                "expectedRevision": current["revision"],
            },
        )
        assert selected.status_code == 200, selected.text
        payload = selected.json()
        assert payload["inpaintCandidate"] == "lineart-guided"
        assert payload["revision"] > current["revision"]
        assert payload["status"]["inpaint"] == "done"
        assert payload["stageReviews"] == {}
        erased = client.get(f"/api/images/{image['id']}/content?variant=inpainted")
        with Image.open(io.BytesIO(erased.content)) as result:
            pixels = result.convert("RGB")
            assert pixels.getpixel((5, 5)) == (255, 255, 255)
        missing = client.get(f"/api/images/{image['id']}/generated/inpaint-candidates/missing")
        assert missing.status_code == 404


def test_typeset_provider_is_not_misrouted_to_inpainting(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        _add_region(client, image["id"], translation="排版")

        queued = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"provider": "pillow"}},
        )
        completed = _wait_job(client, queued.json()["id"])

        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["provider"] == "pillow"
        assert output["inpaintingProvider"] == "opencv"
        assert output["typesettingProvider"] == "pillow"


def test_typeset_persists_overflow_review_fields(tmp_path: Path) -> None:
    from manga_localizer.imaging.typesetting import font_capabilities

    if not font_capabilities()["available"]:
        pytest.skip("No usable system CJK font")
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(
            client,
            image["id"],
            translation="非常非常非常长的文本",
            confirmed=True,
        )
        updated = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "x": 10,
                "y": 10,
                "width": 48,
                "height": 24,
                "direction": "horizontal",
                "style": {
                    "fontSize": 18,
                    "minFontSize": 18,
                    "autoFit": False,
                    "autoWrap": False,
                    "strokeWidth": 0,
                },
                "expectedRevision": region["revision"],
            },
        )
        assert updated.status_code == 200, updated.text
        confirmed = client.patch(
            f"/api/regions/{region['id']}",
            json={"confirmed": True, "expectedRevision": updated.json()["revision"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["trustDisposition"] == "trusted"

        queued = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"provider": "pillow"}},
        )
        completed = _wait_job(client, queued.json()["id"])
        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["typesetEligibleRegionCount"] == 1, output
        assert output["overflowCount"] == 1, output
        assert output["overflowRegionIds"] == [region["id"]]

        current = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert current["status"]["typeset"] == "done"
        assert current["typesetOverflowCount"] == 1
        assert current["typesetOverflowRegionIds"] == [region["id"]]

        stale = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "translationText": "短",
                "expectedRevision": confirmed.json()["revision"],
            },
        )
        assert stale.status_code == 200, stale.text
        after_edit = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert after_edit["status"]["typeset"] == "pending"
        assert after_edit["typesetOverflowCount"] == 0
        assert after_edit["typesetOverflowRegionIds"] == []


def test_typeset_region_ids_overlay_selected_boxes_only(tmp_path: Path) -> None:
    from manga_localizer.imaging.typesetting import font_capabilities

    if not font_capabilities()["available"]:
        pytest.skip("No usable system CJK font")
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    style = {
        "fontSize": 28,
        "minFontSize": 28,
        "autoFit": False,
        "autoWrap": False,
        "strokeWidth": 0,
        "color": "#cc0000",
        "align": "start",
        "padding": 2,
    }
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"], data=png_bytes((400, 160)))
        first = _add_region(client, image["id"], translation="甲甲")
        first = client.patch(
            f"/api/regions/{first['id']}",
            json={
                "x": 16,
                "y": 24,
                "width": 168,
                "height": 88,
                "direction": "horizontal",
                "style": style,
                "expectedRevision": first["revision"],
            },
        )
        assert first.status_code == 200, first.text
        first = _confirm_region(client, first.json())
        second = _add_region(client, image["id"], translation="乙乙乙乙")
        second = client.patch(
            f"/api/regions/{second['id']}",
            json={
                "x": 216,
                "y": 24,
                "width": 168,
                "height": 88,
                "direction": "horizontal",
                "style": style,
                "expectedRevision": second["revision"],
            },
        )
        assert second.status_code == 200, second.text
        second = _confirm_region(client, second.json())

        queued = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"provider": "pillow"}},
        )
        completed = _wait_job(client, queued.json()["id"])
        assert completed["status"] == "completed", completed
        before = client.get(f"/api/images/{image['id']}/generated/typeset")
        assert before.status_code == 200, before.text
        with Image.open(io.BytesIO(before.content)) as opened:
            previous = np.asarray(opened.convert("RGB")).copy()

        edited = client.patch(
            f"/api/regions/{first['id']}",
            json={
                "translationText": "丙丙丙丙丙丙丙丙",
                "expectedRevision": first["revision"],
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["translationText"] == "丙丙丙丙丙丙丙丙"
        assert edited.json()["trustDisposition"] == "trusted"
        stale = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert stale["status"]["inpaint"] == "done"
        assert stale["status"]["typeset"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404

        rerun = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={
                "imageIds": [image["id"]],
                "regionIds": [first["id"]],
                "options": {"provider": "pillow"},
            },
        )
        finished = _wait_job(client, rerun.json()["id"])
        assert finished["status"] == "completed", finished
        output = finished["items"][0]["output"]
        assert output["typesetEligibleRegionCount"] == 1, output
        assert output["typesetSkippedRegionCount"] == 1, output
        after = client.get(f"/api/images/{image['id']}/generated/typeset")
        assert after.status_code == 200, after.text
        with Image.open(io.BytesIO(after.content)) as opened:
            current = np.asarray(opened.convert("RGB"))

        def region_pixels(pixels: np.ndarray, region: dict[str, Any]) -> np.ndarray:
            left = max(0, int(region["x"]))
            top = max(0, int(region["y"]))
            right = min(pixels.shape[1], left + int(region["width"]))
            bottom = min(pixels.shape[0], top + int(region["height"]))
            return pixels[top:bottom, left:right]

        first_before = region_pixels(previous, first)
        first_after = region_pixels(current, first)
        second_before = region_pixels(previous, second)
        second_after = region_pixels(current, second)
        red_before = int(np.sum((first_before[..., 0] > 150) & (first_before[..., 1] < 80)))
        red_after = int(np.sum((first_after[..., 0] > 150) & (first_after[..., 1] < 80)))
        assert red_before > 0
        assert red_after > red_before
        assert not np.array_equal(previous, current)
        assert np.array_equal(second_before, second_after)


def test_partial_typeset_keeps_overflow_ids_for_untouched_boxes(tmp_path: Path) -> None:
    from manga_localizer.imaging.typesetting import font_capabilities

    if not font_capabilities()["available"]:
        pytest.skip("No usable system CJK font")
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        overflowing = _add_region(
            client,
            image["id"],
            translation="非常非常非常长的文本",
        )
        cramped = client.patch(
            f"/api/regions/{overflowing['id']}",
            json={
                "x": 10,
                "y": 10,
                "width": 48,
                "height": 24,
                "direction": "horizontal",
                "style": {
                    "fontSize": 18,
                    "minFontSize": 18,
                    "autoFit": False,
                    "autoWrap": False,
                    "strokeWidth": 0,
                },
                "expectedRevision": overflowing["revision"],
            },
        )
        assert cramped.status_code == 200, cramped.text
        overflowing = _confirm_region(client, cramped.json())
        fitting = _add_region(client, image["id"], translation="短")
        moved = client.patch(
            f"/api/regions/{fitting['id']}",
            json={"x": 140, "y": 40, "expectedRevision": fitting["revision"]},
        )
        assert moved.status_code == 200, moved.text
        fitting = _confirm_region(client, moved.json())

        queued = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"provider": "pillow"}},
        )
        completed = _wait_job(client, queued.json()["id"])
        assert completed["status"] == "completed", completed
        assert completed["items"][0]["output"]["overflowRegionIds"] == [overflowing["id"]]

        edited = client.patch(
            f"/api/regions/{fitting['id']}",
            json={
                "translationText": "更短",
                "expectedRevision": fitting["revision"],
            },
        )
        assert edited.status_code == 200, edited.text

        rerun = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={
                "imageIds": [image["id"]],
                "regionIds": [fitting["id"]],
                "options": {"provider": "pillow"},
            },
        )
        finished = _wait_job(client, rerun.json()["id"])
        assert finished["status"] == "completed", finished
        current = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert current["typesetOverflowRegionIds"] == [overflowing["id"]]
        assert current["typesetOverflowCount"] == 1


def test_safe_typesetting_does_not_overlay_an_unrepaired_detection(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], translation="不应覆盖")
        updated = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "confidence": 0.2,
                "repair": {"detectorGenerated": True},
                "expectedRevision": region["revision"],
            },
        )
        assert updated.status_code == 200, updated.text

        queued = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"provider": "pillow"}},
        )
        completed = _wait_job(client, queued.json()["id"])

        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["eligibleRegionCount"] == 0
        assert output["typesetEligibleRegionCount"] == 0
        assert output["typesetSkippedRegionCount"] == 1
        assert "layouts" not in output


def test_safe_typesetting_rebuilds_inpaint_created_with_all_policy(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        _add_region(client, image["id"], translation="不可信区域", confirmed=False)

        unsafe = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {"repairPolicy": "all"}},
        )
        unsafe_job = _wait_job(client, unsafe.json()["id"])
        assert unsafe_job["status"] == "completed", unsafe_job
        assert unsafe_job["items"][0]["output"]["eligibleRegionCount"] == 1
        unsafe_mask = client.get(f"/api/images/{image['id']}/generated/mask")
        assert unsafe_mask.status_code == 200, unsafe_mask.text
        with Image.open(io.BytesIO(unsafe_mask.content)) as opened:
            assert np.any(np.asarray(opened.convert("L"), dtype=np.uint8))

        safe = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"repairPolicy": "safe"}},
        )
        safe_job = _wait_job(client, safe.json()["id"])
        assert safe_job["status"] == "completed", safe_job
        output = safe_job["items"][0]["output"]
        assert (
            not {
                "inpaintedArtifact",
                "inpaintedUrl",
                "maskArtifact",
                "maskUrl",
                "typesetArtifact",
                "typesetUrl",
            }
            & output.keys()
        )
        assert output["repairPolicy"] == "safe"
        assert output["eligibleRegionCount"] == 0
        assert output["repairedRegionCount"] == 0
        assert output["typesetEligibleRegionCount"] == 0
        safe_mask = client.get(f"/api/images/{image['id']}/generated/mask")
        assert safe_mask.status_code == 200, safe_mask.text
        with Image.open(io.BytesIO(safe_mask.content)) as opened:
            assert not np.any(np.asarray(opened.convert("L"), dtype=np.uint8))
        store = app.state.registry.get(project["id"])
        with store.session() as session:
            asset = session.get(ImageAsset, image["id"])
            assert asset is not None
            assert asset.status["inpaintingRepairPolicy"] == "safe"


@pytest.mark.parametrize(
    ("field", "value"),
    (("type", "sound_effect"), ("direction", "horizontal")),
)
def test_trust_input_edit_discards_repair_cache_before_safe_typesetting(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / f"project-{field}")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], translation="已信任译文", confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        before = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert before["status"]["inpaint"] == "done"
        assert before["status"]["typeset"] == "done"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 200

        changed = client.patch(
            f"/api/regions/{region['id']}",
            json={field: value, "expectedRevision": region["revision"]},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["trustDisposition"] == "review"
        assert changed.json()["trustReason"] == "trust-input-changed"
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["translation"] == "pending"
        assert state["status"]["inpaint"] == "pending"
        assert state["status"]["typeset"] == "pending"
        assert state["status"]["export"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404

        typeset = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"repairPolicy": "safe"}},
        )
        completed = _wait_job(client, typeset.json()["id"])
        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["eligibleRegionCount"] == 0
        assert output["typesetEligibleRegionCount"] == 0


def test_typesetting_skips_an_eligible_region_with_an_empty_text_mask(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"], data=png_bytes(color="white"))
        region = _add_region(client, image["id"], translation="不应覆盖空蒙版", confirmed=True)
        updated = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "repair": {"maskMode": "text", "maskPadding": 0, "dilation": 0},
                "expectedRevision": region["revision"],
            },
        )
        assert updated.status_code == 200, updated.text

        queued = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {"provider": "pillow"}},
        )
        completed = _wait_job(client, queued.json()["id"])

        assert completed["status"] == "completed", completed
        output = completed["items"][0]["output"]
        assert output["eligibleRegionCount"] == 1
        assert output["repairedRegionCount"] == 0
        assert output["typesetEligibleRegionCount"] == 0
        assert "layouts" not in output


def test_region_and_translation_edits_invalidate_stale_render_and_export(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], translation="第一版", confirmed=True)
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, render.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "images", "conflict": "rename"},
            },
        )
        assert _wait_job(client, exported.json()["id"])["status"] == "completed"

        edited = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "translationText": "第二版",
                "confirmed": True,
                "expectedRevision": region["revision"],
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["confirmed"] is False
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["inpaint"] == "done"
        assert state["status"]["typeset"] == "pending"
        assert state["status"]["export"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 200
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404

        stale_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "images", "conflict": "rename"},
            },
        )
        failed = _wait_job(client, stale_export.json()["id"])
        assert failed["status"] == JobStatus.FAILED.value
        assert failed["items"][0]["error"] == ("Export failed; inspect the private project log")

        typeset = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, typeset.json()["id"])["status"] == "completed"
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 200

        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        assert _wait_job(client, translated.json()["id"])["status"] == "completed"
        final_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert final_state["status"]["translation"] == "done"
        assert final_state["status"]["typeset"] == "pending"
        assert final_state["status"]["export"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404


def test_full_region_snapshot_invalidates_only_fields_that_actually_changed(
    client: TestClient,
    tmp_path: Path,
    app,
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    region = _add_region(client, image["id"], translation="机器译文")
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        asset = session.get(ImageAsset, image["id"])
        assert asset is not None
        asset.status = {
            **dict(asset.status),
            "ocr": "done",
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
        }

    response = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "x": region["x"],
            "y": region["y"],
            "width": region["width"],
            "height": region["height"],
            "rotation": region["rotation"],
            "sourceText": region["sourceText"],
            "translationText": "人工校对译文",
            "type": region["type"],
            "direction": region["direction"],
            "order": region["order"],
            "confidence": region["confidence"],
            "ignored": region["ignored"],
            "confirmed": True,
            "style": region["style"],
            "repair": region["repair"],
            "expectedRevision": region["revision"],
        },
    )
    assert response.status_code == 200, response.text
    state = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert state["status"]["ocr"] == "done"
    assert state["status"]["translation"] == "pending"
    assert state["status"]["inpaint"] == "pending"
    assert state["status"]["typeset"] == "pending"
    assert state["status"]["export"] == "pending"


def test_rotation_and_reactivating_empty_region_invalidate_required_stages(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    project_root = tmp_path / "project"
    with TestClient(app) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"

        rotated = client.patch(
            f"/api/regions/{region['id']}",
            json={"rotation": 19, "expectedRevision": region["revision"]},
        )
        assert rotated.status_code == 200, rotated.text
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["inpaint"] == "pending"
        assert state["status"]["typeset"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404

        store = app.state.registry.get(project["id"])
        with store.session() as session:
            stored_image = session.get(ImageAsset, image["id"])
            assert stored_image is not None
            stored_image.status = {
                **dict(stored_image.status),
                "ocr": "done",
                "translation": "done",
            }
        ignored = client.post(
            f"/api/images/{image['id']}/regions",
            json={
                "x": 150,
                "y": 180,
                "width": 60,
                "height": 80,
                "sourceText": "",
                "translationText": "",
                "ignored": True,
            },
        )
        assert ignored.status_code == 201, ignored.text
        before_reactivate = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert before_reactivate["status"]["ocr"] == "done"
        assert before_reactivate["status"]["translation"] == "done"
        reactivated = client.patch(
            f"/api/regions/{ignored.json()['id']}",
            json={"ignored": False, "expectedRevision": ignored.json()["revision"]},
        )
        assert reactivated.status_code == 200, reactivated.text
        after_reactivate = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert after_reactivate["status"]["ocr"] == "pending"
        assert after_reactivate["status"]["translation"] == "pending"


def test_translation_settings_invalidate_rendered_and_exported_output(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        assert _wait_job(client, translated.json()["id"])["status"] == "completed"
        translated_region = client.get(f"/api/images/{image['id']}/regions").json()[0]
        confirmed = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "confirmed": True,
                "expectedRevision": translated_region["revision"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={"imageIds": [image["id"]], "options": {"format": "images"}},
        )
        assert _wait_job(client, exported.json()["id"])["status"] == "completed"

        current_project = client.get(f"/api/projects/{project['id']}").json()
        changed = client.patch(
            f"/api/projects/{project['id']}",
            json={
                "settings": {"targetLanguage": "zh-TW"},
                "expectedRevision": current_project["revision"],
            },
        )
        assert changed.status_code == 200, changed.text
        preserved_region = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert preserved_region["trustDisposition"] == "trusted"
        assert preserved_region["trustReason"] == "human-confirmed"
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["translation"] == "pending"
        assert state["status"]["inpaint"] == "done"
        assert state["status"]["typeset"] == "pending"
        assert state["status"]["export"] == "pending"
        assert state["status"]["reviewState"] == "pending"
        assert state["status"]["reviewedAt"] == ""
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 200
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404


@pytest.mark.parametrize(
    ("setting_name", "setting_value"),
    (
        ("remoteEndpoint", "https://translator.example/v1"),
        ("remoteModel", "replacement-model"),
    ),
)
def test_remote_translation_connection_settings_invalidate_derived_output(
    tmp_path: Path,
    setting_name: str,
    setting_value: str,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        store = app.state.registry.get(project["id"])
        with store.session() as session:
            asset = session.get(ImageAsset, image["id"])
            assert asset is not None
            asset.status = {
                "detection": "done",
                "ocr": "done",
                "translation": "done",
                "inpaint": "done",
                "typeset": "done",
                "export": "done",
            }

        current = client.get(f"/api/projects/{project['id']}").json()
        changed = client.patch(
            f"/api/projects/{project['id']}",
            json={
                "settings": {setting_name: setting_value},
                "expectedRevision": current["revision"],
            },
        )
        assert changed.status_code == 200, changed.text
        status = client.get(f"/api/projects/{project['id']}/images").json()[0]["status"]
        assert status["translation"] == "pending"
        assert status["typeset"] == "pending"
        assert status["export"] == "pending"
        assert status["inpaint"] == "done"


class _FakeOCR:
    def __init__(self) -> None:
        self.detection_options: dict[str, Any] = {}

    def recognize_region(self, _image, region, **_options) -> OCRRegion:
        return OCRRegion(
            x=round(region["x"]),
            y=round(region["y"]),
            width=round(region["width"]),
            height=round(region["height"]),
            text="実際のOCR",
            confidence=0.91,
            direction="vertical",
        )

    def detect_text_regions(self, _image, **options) -> list[OCRRegion]:
        self.detection_options = options
        return [OCRRegion(10, 10, 80, 100, "検出", 0.88, "vertical")]


class _DirectionAwareOCR:
    def recognize_region(self, _image, region, **options) -> OCRRegion:
        direction = str(options["direction"])
        return OCRRegion(
            x=round(region["x"]),
            y=round(region["y"]),
            width=round(region["width"]),
            height=round(region["height"]),
            text=f"ocr-{direction}",
            confidence=0.8,
            direction=direction,
        )


def test_ocr_job_endpoint_updates_region_without_http_blocking(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    app.state.providers.ocr = _FakeOCR()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], source="旧文本")
        confirmed = client.patch(
            f"/api/regions/{region['id']}",
            json={"confirmed": True, "expectedRevision": region["revision"]},
        ).json()
        _review_image(client, project["id"], image["id"])
        submitted = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"regionIds": [confirmed["id"]], "options": {}},
        )
        assert submitted.status_code == 202
        result = _wait_job(client, submitted.json()["id"])
        assert result["status"] == "completed"
        updated = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert updated["sourceText"] == "実際のOCR"
        assert updated["confidence"] == 0.91
        assert updated["confirmed"] is False
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["ocrProvider"] == "tesseract"
        assert image_state["status"]["ocr"] == "done"
        assert image_state["status"]["reviewState"] == "pending"
        assert image_state["status"]["reviewedAt"] == ""


def test_ocr_persists_each_target_direction_language(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    app.state.providers.ocr = _DirectionAwareOCR()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "mixed-direction-ocr")
        image = upload_image(client, project["id"])
        for x, direction in ((10, "horizontal"), (120, "vertical")):
            created = client.post(
                f"/api/images/{image['id']}/regions",
                json={
                    "x": x,
                    "y": 20,
                    "width": 80,
                    "height": 100,
                    "direction": direction,
                },
            )
            assert created.status_code == 201, created.text

        submitted = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"imageIds": [image["id"]], "options": {}},
        )
        completed = _wait_job(client, submitted.json()["id"])
        assert completed["status"] == "completed", completed
        regions = client.get(f"/api/images/{image['id']}/regions").json()
        by_direction = {region["direction"]: region for region in regions}
        assert by_direction["horizontal"]["recognition"]["ocr"]["language"] == "jpn"
        assert by_direction["vertical"]["recognition"]["ocr"]["language"] == "jpn_vert"
        for direction, language in (("horizontal", "jpn"), ("vertical", "jpn_vert")):
            recognition = by_direction[direction]["recognition"]["ocr"]
            assert recognition["attempts"][recognition["selectedIndex"]]["language"] == language


def test_targeted_ocr_preserves_human_ignore_and_attempt_evidence_on_reopen(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    app.state.providers.ocr = _FakeOCR()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "ignored-targeted-ocr")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], source="忽略前")
        ignored = client.patch(
            f"/api/regions/{region['id']}",
            json={"ignored": True, "expectedRevision": region["revision"]},
        )
        assert ignored.status_code == 200, ignored.text
        assert ignored.json()["trustDisposition"] == "ignored"
        assert ignored.json()["trustReason"] == "human-ignored"

        submitted = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"regionIds": [region["id"]], "options": {}},
        )
        assert submitted.status_code == 202, submitted.text
        completed = _wait_job(client, submitted.json()["id"])
        assert completed["status"] == "completed", completed
        current = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["ignored"] is True
        assert current["confirmed"] is False
        assert current["trustDisposition"] == "ignored"
        assert current["trustReason"] == "human-ignored"
        assert current["ocrConfidence"] == 0.91
        assert current["recognition"]["ocr"]["attemptCount"] == 1
        assert current["recognition"]["ocr"]["selectedIndex"] == 0
        attempt = current["recognition"]["ocr"]["attempts"][0]
        assert attempt["provider"] == "tesseract"
        assert attempt["inputVariant"] == "original"
        assert attempt["confidence"] == 0.91
        assert attempt["direction"] == "vertical"

        repeated = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"regionIds": [region["id"]], "options": {}},
        )
        assert repeated.status_code == 202, repeated.text
        repeated_job = _wait_job(client, repeated.json()["id"])
        assert repeated_job["status"] == "completed", repeated_job
        current = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["trustDisposition"] == "ignored"
        assert current["recognition"]["ocr"]["attemptCount"] == 2
        assert current["recognition"]["ocr"]["selectedIndex"] == 1
        assert current["repair"]["ocrAttemptCount"] == 2
        assert [item["confidence"] for item in current["recognition"]["ocr"]["attempts"]] == [
            0.91,
            0.91,
        ]

    with TestClient(create_app(settings, start_worker=False)) as reopened:
        persisted = reopened.get(f"/api/images/{image['id']}/regions")
        assert persisted.status_code == 200, persisted.text
        current = persisted.json()[0]
        assert current["ignored"] is True
        assert current["trustDisposition"] == "ignored"
        assert current["trustReason"] == "human-ignored"
        assert current["recognition"]["ocr"]["attemptCount"] == 2
        assert current["recognition"]["ocr"]["selectedIndex"] == 1
        assert [item["confidence"] for item in current["recognition"]["ocr"]["attempts"]] == [
            0.91,
            0.91,
        ]


def test_cancel_leaves_active_item_running_then_records_its_real_completion(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    entered = threading.Event()
    release = threading.Event()

    class BlockingFirstOCR(_FakeOCR):
        def recognize_region(self, image, region, **options) -> OCRRegion:
            if not entered.is_set():
                entered.set()
                assert release.wait(3)
            return super().recognize_region(image, region, **options)

    app.state.providers.ocr = BlockingFirstOCR()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        first_image = upload_image(client, project["id"], relative_path="pages/1.png")
        second_image = upload_image(client, project["id"], relative_path="pages/2.png")
        first_region = _add_region(client, first_image["id"], source="first-old")
        second_region = _add_region(client, second_image["id"], source="second-old")
        queued = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"regionIds": [first_region["id"], second_region["id"]], "options": {}},
        ).json()
        assert entered.wait(3)

        cancelled = client.post(f"/api/jobs/{queued['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        cancelled_job = cancelled.json()
        assert cancelled_job["status"] == JobStatus.CANCELLED.value
        assert [item["status"] for item in cancelled_job["items"]] == [
            JobStatus.RUNNING.value,
            JobStatus.CANCELLED.value,
        ]
        release.set()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            final_job = client.get(f"/api/jobs/{queued['id']}").json()
            if final_job["items"][0]["status"] == JobStatus.COMPLETED.value:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("The active item did not finish after cooperative cancellation")
        assert final_job["status"] == JobStatus.CANCELLED.value
        assert [item["status"] for item in final_job["items"]] == [
            JobStatus.COMPLETED.value,
            JobStatus.CANCELLED.value,
        ]
        first_text = client.get(f"/api/images/{first_image['id']}/regions").json()[0]
        second_text = client.get(f"/api/images/{second_image['id']}/regions").json()[0]
        assert first_text["sourceText"] == "実際のOCR"
        assert second_text["sourceText"] == "second-old"


def test_background_ocr_and_translation_never_overwrite_newer_manual_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    ocr_entered = threading.Event()
    ocr_release = threading.Event()

    class BlockingOCR(_FakeOCR):
        def recognize_region(self, image, region, **options) -> OCRRegion:
            ocr_entered.set()
            assert ocr_release.wait(3)
            return super().recognize_region(image, region, **options)

    app.state.providers.ocr = BlockingOCR()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], source="旧 OCR", translation="旧译文")

        ocr = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"regionIds": [region["id"]], "options": {}},
        )
        assert ocr_entered.wait(3)
        manual_ocr = client.patch(
            f"/api/regions/{region['id']}",
            json={"sourceText": "用户新原文", "expectedRevision": region["revision"]},
        )
        assert manual_ocr.status_code == 200, manual_ocr.text
        ocr_release.set()
        failed_ocr = _wait_job(client, ocr.json()["id"])
        assert failed_ocr["status"] == "failed"
        assert failed_ocr["items"][0]["error"] == ("OCR failed; inspect the private project log")
        current = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["sourceText"] == "用户新原文"

        confirmed = client.patch(
            f"/api/regions/{region['id']}",
            json={"confirmed": True, "expectedRevision": current["revision"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        current = confirmed.json()

        translation_entered = threading.Event()
        translation_release = threading.Event()

        def blocking_translate(*_args, **_kwargs) -> str:
            translation_entered.set()
            assert translation_release.wait(3)
            return "后台旧译文"

        monkeypatch.setattr(app.state.providers.mock, "translate_text", blocking_translate)
        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        assert translation_entered.wait(3)
        manual_translation = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "translationText": "用户新译文",
                "expectedRevision": current["revision"],
            },
        )
        assert manual_translation.status_code == 200, manual_translation.text
        translation_release.set()
        failed_translation = _wait_job(client, translated.json()["id"])
        assert failed_translation["status"] == "failed"
        assert failed_translation["items"][0]["error"] == (
            "Translation failed; inspect the private project log"
        )
        final = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert final["translationText"] == "用户新译文"


def test_background_render_discards_results_when_region_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    entered = threading.Event()
    release = threading.Event()
    original_inpaint = app.state.providers.inpainting.inpaint

    def blocking_inpaint(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_inpaint(*args, **kwargs)

    monkeypatch.setattr(app.state.providers.inpainting, "inpaint", blocking_inpaint)
    project_root = tmp_path / "project"
    with TestClient(app) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert entered.wait(3)
        edited = client.patch(
            f"/api/regions/{region['id']}",
            json={"rotation": 12, "expectedRevision": region["revision"]},
        )
        assert edited.status_code == 200, edited.text
        state_after_edit = client.get(f"/api/projects/{project['id']}/images").json()[0]
        release.set()
        failed = _wait_job(client, render.json()["id"])
        assert failed["status"] == "failed"
        assert failed["items"][0]["error"] == (
            "Image rendering failed; inspect the private project log"
        )
        final_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert final_state["revision"] == state_after_edit["revision"]
        assert final_state["status"] == state_after_edit["status"]
        assert final_state["processingErrors"] == state_after_edit["processingErrors"]
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404
        assert not list((project_root / "generated").rglob("*.png"))


def test_provider_failure_after_edit_does_not_pollute_new_image_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    entered = threading.Event()
    release = threading.Event()

    def failing_inpaint(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("obsolete provider failure")

    monkeypatch.setattr(app.state.providers.inpainting, "inpaint", failing_inpaint)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], confirmed=True)
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert entered.wait(3)
        edited = client.patch(
            f"/api/regions/{region['id']}",
            json={"rotation": 12, "expectedRevision": region["revision"]},
        )
        assert edited.status_code == 200, edited.text
        state_after_edit = client.get(f"/api/projects/{project['id']}/images").json()[0]
        release.set()

        failed = _wait_job(client, render.json()["id"])
        assert failed["status"] == "failed"
        assert failed["items"][0]["error"] == (
            "Image rendering failed; inspect the private project log"
        )
        store = app.state.registry.get(project["id"])
        internal = app.state.queue.get_job(store, render.json()["id"])
        assert "stale failure was discarded" in internal.items[0].error
        assert "obsolete provider failure" not in internal.items[0].error
        final_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert final_state["revision"] == state_after_edit["revision"]
        assert final_state["status"] == state_after_edit["status"]
        assert final_state["processingErrors"] == state_after_edit["processingErrors"]


@pytest.mark.parametrize(
    ("job_kind", "review_stage", "blocked_directory"),
    (
        ("preprocess", "preprocess", "preprocessed"),
        ("inpaint", "inpaint", "masks"),
        ("typeset", "typeset", "typeset"),
    ),
)
def test_artifact_publication_and_revision_commit_are_atomic_with_stage_review(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_kind: str,
    review_stage: str,
    blocked_directory: str,
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    store = app.state.registry.get(project["id"])
    if job_kind == "preprocess":
        app.state.queue._process_preprocess(store, image["id"], {})
    else:
        _add_region(client, image["id"], confirmed=True)
        app.state.queue._process_render(store, image["id"], {}, "render")

    with store.session() as session:
        current = session.get(ImageAsset, image["id"])
        assert current is not None
        expected_revision = current.revision

    artifact_published = threading.Event()
    release_publication = threading.Event()
    review_started = threading.Event()
    review_finished = threading.Event()
    producer_errors: list[Exception] = []
    review_errors: list[Exception] = []
    original_atomic_write = queue_module.atomic_write_bytes
    blocked_root = store.root / "generated" / blocked_directory

    def blocking_atomic_write(path: Path, data: bytes) -> None:
        original_atomic_write(path, data)
        if path.is_relative_to(blocked_root) and not artifact_published.is_set():
            artifact_published.set()
            assert release_publication.wait(3)

    monkeypatch.setattr(queue_module, "atomic_write_bytes", blocking_atomic_write)

    def publish() -> None:
        try:
            if job_kind == "preprocess":
                app.state.queue._process_preprocess(store, image["id"], {})
            else:
                app.state.queue._process_render(store, image["id"], {}, job_kind)
        except Exception as error:  # pragma: no cover - asserted below
            producer_errors.append(error)

    def review() -> None:
        review_started.set()
        try:
            review_image_stage(
                store,
                image["id"],
                stage=review_stage,
                state="accepted",
                expected_revision=expected_revision,
            )
        except Exception as error:  # pragma: no cover - asserted below
            review_errors.append(error)
        finally:
            review_finished.set()

    producer = threading.Thread(target=publish)
    reviewer = threading.Thread(target=review)
    producer.start()
    assert artifact_published.wait(3)
    reviewer.start()
    assert review_started.wait(3)
    assert not review_finished.wait(0.1)
    release_publication.set()
    producer.join(3)
    reviewer.join(3)

    assert not producer.is_alive()
    assert not reviewer.is_alive()
    assert producer_errors == []
    assert len(review_errors) == 1
    assert isinstance(review_errors[0], RevisionConflict)
    with store.session() as session:
        current = session.get(ImageAsset, image["id"])
        assert current is not None
        assert review_stage not in stage_reviews(current)


def test_detection_is_independent_from_ocr_and_creates_unknown_empty_regions(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    provider = _FakeOCR()
    app.state.providers.ocr = provider
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        _review_image(client, project["id"], image["id"], "no-text-reviewed")
        detect = client.post(
            f"/api/projects/{project['id']}/detect",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert detect.status_code == 202
        detected_job = _wait_job(client, detect.json()["id"])
        assert detected_job["status"] == "completed"
        assert provider.detection_options == {"direction": "auto", "language": None}
        regions = client.get(f"/api/images/{image['id']}/regions").json()
        assert len(regions) == 1
        assert regions[0]["type"] == "unknown"
        assert regions[0]["sourceText"] == ""
        assert regions[0]["ocrProvider"] is None
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["status"]["detection"] == "done"
        assert image_state["status"]["ocr"] == "pending"
        assert image_state["status"]["reviewState"] == "pending"
        assert image_state["detectorProvider"] == "tesseract"
        assert image_state["regionCount"] == 1

        ocr = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, ocr.json()["id"])["status"] == "completed"
        recognized = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert recognized["sourceText"] == "実際のOCR"
        assert recognized["ocrProvider"] == "tesseract"


def test_render_content_variants_and_safe_tree_export(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    source_data = png_bytes((240, 320), rectangle=(45, 60, 100, 120))
    with TestClient(app) as client:
        project_root = tmp_path / "project"
        project = create_project(client, project_root)
        image = upload_image(
            client,
            project["id"],
            relative_path="巻一/章二/页.png",
            data=source_data,
        )
        _add_region(client, image["id"], translation="翻译完成", confirmed=True)
        before_hash = hashlib.sha256(
            (project_root / "source/巻一/章二/页.png").read_bytes()
        ).hexdigest()

        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {"radius": 3}},
        )
        assert render.status_code == 202
        rendered = _wait_job(client, render.json()["id"])
        assert rendered["status"] == "completed", rendered
        assert client.get(f"/api/images/{image['id']}/content?variant=original").status_code == 200
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 200
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 200
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["inpaintingProvider"] == "opencv"
        assert image_state["typesettingProvider"] == "pillow"
        _review_image(client, project["id"], image["id"])

        output_root = tmp_path / "safe-export"
        export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(output_root),
                    "conflict": "rename",
                    "preserveTree": True,
                },
            },
        )
        export_job_id = export.json()["id"]
        exported = _wait_job(client, export_job_id)
        assert exported["status"] == "completed", exported
        assert (output_root / "translated/巻一/章二/页.png").is_file()
        assert (output_root / "original-text/巻一/章二/页.json").is_file()
        assert (output_root / "translated-text/巻一/章二/页.json").is_file()
        assert (output_root / "masks/巻一/章二/页.png").is_file()
        assert (output_root / "project/project.json").is_file()
        assert (output_root / "project/project.sqlite3").is_file()
        assert (output_root / "source/巻一/章二/页.png").read_bytes() == source_data
        assert (output_root / "generated/inpainted/巻一/章二/页.png").is_file()
        assert (output_root / "generated/typeset/巻一/章二/页.png").is_file()
        assert (output_root / "generated/masks/巻一/章二/页.png").is_file()
        bundle = json.loads((output_root / "project/project.json").read_text("utf-8"))
        bundled_job = next(job for job in bundle["jobs"] if job["id"] == export_job_id)
        assert bundled_job["status"] == JobStatus.COMPLETED.value
        assert bundled_job["completed"] == 1
        assert bundled_job["items"][0]["status"] == JobStatus.COMPLETED.value
        bundled_image = next(entry for entry in bundle["images"] if entry["id"] == image["id"])
        assert bundled_image["status"]["export"] == "done"
        with sqlite3.connect(output_root / "project/project.sqlite3") as bundle_database:
            project_row = bundle_database.execute(
                "SELECT root_path, input_root FROM projects"
            ).fetchone()
            job_row = bundle_database.execute(
                "SELECT status, completed FROM jobs WHERE id = ?", (export_job_id,)
            ).fetchone()
            job_options = bundle_database.execute(
                "SELECT options FROM jobs WHERE id = ?", (export_job_id,)
            ).fetchone()[0]
            item_row = bundle_database.execute(
                "SELECT status FROM job_items WHERE job_id = ?", (export_job_id,)
            ).fetchone()
            input_path = bundle_database.execute("SELECT input_path FROM images").fetchone()[0]
        assert project_row == (".", None)
        assert input_path is None
        assert "outputPath" not in job_options
        assert job_row == (JobStatus.COMPLETED.value, 1)
        assert item_row == (JobStatus.COMPLETED.value,)
        exported_image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert exported_image_state["status"]["export"] == "done"
        output_serialized = str(exported["items"][0]["output"])
        assert str(output_root) not in output_serialized
        assert str(project_root) not in output_serialized
        assert set(exported["items"][0]["output"]) == {
            "writtenArtifactCount",
            "skippedArtifactCount",
        }

        fresh_settings = Settings(
            data_dir=tmp_path / "fresh-catalog",
            worker_poll_seconds=0.01,
        )
        with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
            reopened = fresh.post(
                "/api/projects/open",
                json={"manifestPath": str(output_root / "project/project.json")},
            )
            assert reopened.status_code == 200, reopened.text
            reopened_image = fresh.get(f"/api/projects/{project['id']}/images").json()[0]
            assert reopened_image["id"] == image["id"]
            for variant in ("original", "erased", "typeset"):
                content = fresh.get(f"/api/images/{image['id']}/content?variant={variant}")
                assert content.status_code == 200, (variant, content.text)
            assert fresh.get(f"/api/images/{image['id']}/thumbnail").status_code == 200

        flat_root = tmp_path / "flat-export"
        flat_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(flat_root),
                    "conflict": "rename",
                    "preserveTree": False,
                },
            },
        )
        flat_done = _wait_job(client, flat_export.json()["id"])
        assert flat_done["status"] == "completed", flat_done
        assert (flat_root / "translated/页.png").is_file()
        assert (flat_root / "original-text/页.json").is_file()
        assert (flat_root / "translated-text/页.json").is_file()
        assert (flat_root / "masks/页.png").is_file()

        default_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "both", "conflict": "rename"},
            },
        )
        default_done = _wait_job(client, default_export.json()["id"])
        assert default_done["status"] == "completed", default_done
        assert (project_root / "translated/巻一/章二/页.png").is_file()
        assert (project_root / "original-text/巻一/章二/页.json").is_file()
        assert (project_root / "translated-text/巻一/章二/页.json").is_file()
        assert (project_root / "masks/巻一/章二/页.png").is_file()
        after_hash = hashlib.sha256(
            (project_root / "source/巻一/章二/页.png").read_bytes()
        ).hexdigest()
        assert before_hash == after_hash


def test_clean_plate_export_review_gate_variants_and_byte_identity(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(
            client,
            project["id"],
            relative_path="chapter/page.png",
            data=png_bytes((240, 320), rectangle=(30, 40, 130, 160)),
        )
        _add_region(
            client,
            image["id"],
            translation="未排版也可导出",
            confirmed=True,
        )
        before_inpaint = _review_image(client, project["id"], image["id"])
        assert before_inpaint["status"]["reviewState"] == "reviewed"
        inpainted = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, inpainted.json()["id"])["status"] == "completed"
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["typeset"] == "pending"
        assert state["status"]["reviewState"] == "pending"
        assert state["status"]["reviewedAt"] == ""

        gated = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "imageVariant": "inpainted",
                    "outputPath": str(tmp_path / "gated"),
                },
            },
        )
        gated_job = _wait_job(client, gated.json()["id"])
        assert gated_job["status"] == "failed"
        assert gated_job["items"][0]["error"] == ("Export failed; inspect the private project log")

        page_reviewed = _review_image(client, project["id"], image["id"])
        _set_stage_review(client, page_reviewed, "inpaint", "rejected")
        rejected_root = tmp_path / "rejected-clean-export"
        rejected_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "imageVariant": "inpainted",
                    "outputPath": str(rejected_root),
                },
            },
        )
        rejected_job = _wait_job(client, rejected_export.json()["id"])
        assert rejected_job["status"] == "failed"
        assert rejected_job["items"][0]["error"] == (
            "Export failed; inspect the private project log"
        )
        assert not rejected_root.exists()
        current_after_rejection = client.get(f"/api/projects/{project['id']}/images").json()[0]
        page_reviewed = _set_stage_review(
            client,
            current_after_rejection,
            "inpaint",
            "accepted",
        )
        clean_root = tmp_path / "clean-export"
        clean = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "imageVariant": "inpainted",
                    "outputPath": str(clean_root),
                    "conflict": "overwrite",
                },
            },
        )
        clean_job = _wait_job(client, clean.json()["id"])
        assert clean_job["status"] == "completed", clean_job
        generated = project_root / "generated/inpainted/chapter/page.png"
        exported_clean = clean_root / "clean/chapter/page.png"
        assert exported_clean.read_bytes() == generated.read_bytes()
        assert not (clean_root / "translated").exists()
        assert not (clean_root / "project").exists()
        assert not (clean_root / "source").exists()
        assert not (clean_root / "generated").exists()

        reviewed_generated = generated.read_bytes()
        generated.write_bytes(png_bytes((240, 320), color="red"))
        tampered = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "imageVariant": "inpainted",
                    "outputPath": str(tmp_path / "tampered-export"),
                },
            },
        )
        tampered_job = _wait_job(client, tampered.json()["id"])
        assert tampered_job["status"] == "failed"
        assert tampered_job["items"][0]["error"] == (
            "Export failed; inspect the private project log"
        )
        generated.write_bytes(reviewed_generated)

        typeset = client.post(
            f"/api/projects/{project['id']}/typeset",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, typeset.json()["id"])["status"] == "completed"
        after_typeset = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert after_typeset["status"]["reviewState"] == "pending"
        assert after_typeset["status"]["reviewedAt"] == ""
        assert set(after_typeset["stageReviews"]) == {"inpaint"}
        default_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={"imageIds": [image["id"]], "options": {"format": "images"}},
        )
        default_failed = _wait_job(client, default_export.json()["id"])
        assert default_failed["status"] == "failed"
        assert "options" not in default_export.json()
        assert default_failed["items"][0]["error"] == (
            "Export failed; inspect the private project log"
        )

        json_root = tmp_path / "json-only"
        json_only = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "json", "outputPath": str(json_root)},
            },
        )
        assert _wait_job(client, json_only.json()["id"])["status"] == "completed"
        assert (json_root / "original-text/chapter/page.json").is_file()
        assert (json_root / "translated-text/chapter/page.json").is_file()
        assert (json_root / "export.json").is_file()
        assert not (json_root / "project").exists()
        assert not (json_root / "source").exists()
        assert not (json_root / "generated").exists()
        assert not list(json_root.rglob("*.png"))
        original_regions = json.loads(
            (json_root / "original-text/chapter/page.json").read_text("utf-8")
        )["regions"]
        assert original_regions[0]["detectorConfidence"] is None
        assert original_regions[0]["ocrConfidence"] is None
        assert original_regions[0]["trustDisposition"] == "trusted"
        assert original_regions[0]["trustReason"] == "human-confirmed"
        assert original_regions[0]["trustPolicyVersion"] == 1
        json_job = client.get(f"/api/jobs/{json_only.json()['id']}").json()
        assert "options" not in json_job
        assert "project" not in json_job["items"][0]["output"]
        assert "artifact" not in json.dumps(json_job["items"][0]["output"])
        summary_text = (json_root / "export.json").read_text("utf-8")
        assert str(project_root) not in summary_text
        assert str(json_root) not in summary_text
        assert "source/" not in summary_text
        assert "generated/" not in summary_text
        summary = json.loads(summary_text)
        assert summary["kind"] == "manga-localizer-json-export"
        assert summary["images"] == [
            {
                "imageId": image["id"],
                "relativePath": "chapter/page.png",
                "exportRelativePath": "chapter/page.png",
                "originalText": "original-text/chapter/page.json",
                "translatedText": "translated-text/chapter/page.json",
            }
        ]

        page_and_stage_reviewed = _review_image(client, project["id"], image["id"])
        assert set(page_and_stage_reviewed["stageReviews"]) == {"inpaint", "typeset"}
        both_root = tmp_path / "both-export"
        both = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "imageVariant": "both",
                    "outputPath": str(both_root),
                    "conflict": "overwrite",
                },
            },
        )
        both_job = _wait_job(client, both.json()["id"])
        assert both_job["status"] == "completed", both_job
        assert (both_root / "clean/chapter/page.png").is_file()
        assert (both_root / "translated/chapter/page.png").is_file()
        assert (both_root / "project/project.json").is_file()
        assert (both_root / "project/project.sqlite3").is_file()
        assert (both_root / "source/chapter/page.png").is_file()
        assert (both_root / "generated/inpainted/chapter/page.png").is_file()


def test_portable_bundle_excludes_unreviewed_generated_artifacts(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        accepted = upload_image(client, project["id"], relative_path="a/accepted.png")
        unreviewed = upload_image(client, project["id"], relative_path="b/unreviewed.png")
        _add_region(client, accepted["id"], confirmed=True)
        _add_region(client, unreviewed["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [accepted["id"], unreviewed["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], accepted["id"])

        output_root = tmp_path / "portable-reviewed-only"
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [accepted["id"]],
                "options": {
                    "format": "both",
                    "imageVariant": "typeset",
                    "outputPath": str(output_root),
                    "conflict": "overwrite",
                },
            },
        )
        completed = _wait_job(client, exported.json()["id"])
        assert completed["status"] == "completed", completed
        assert (output_root / "generated/inpainted/a/accepted.png").is_file()
        assert (output_root / "generated/typeset/a/accepted.png").is_file()
        assert (output_root / "generated/masks/a/accepted.png").is_file()
        assert not (output_root / "generated/inpainted/b/unreviewed.png").exists()
        assert not (output_root / "generated/typeset/b/unreviewed.png").exists()
        assert not (output_root / "generated/masks/b/unreviewed.png").exists()
        assert (output_root / "source/a/accepted.png").is_file()
        assert (output_root / "source/b/unreviewed.png").is_file()

        bundle = json.loads((output_root / "project/project.json").read_text("utf-8"))
        bundled_unreviewed = next(
            image for image in bundle["images"] if image["id"] == unreviewed["id"]
        )
        assert bundled_unreviewed["status"]["inpaint"] == "pending"
        assert bundled_unreviewed["status"]["typeset"] == "pending"
        assert bundled_unreviewed["status"].get("stageReviews", {}) == {}
        assert "inpaintingProvider" not in bundled_unreviewed["status"]
        assert "typesettingProvider" not in bundled_unreviewed["status"]

        with sqlite3.connect(output_root / "project/project.sqlite3") as database:
            encoded_status = database.execute(
                "SELECT status FROM images WHERE id = ?", (unreviewed["id"],)
            ).fetchone()[0]
        database_status = json.loads(encoded_status)
        assert database_status["inpaint"] == "pending"
        assert database_status["typeset"] == "pending"
        assert database_status.get("stageReviews", {}) == {}


def test_verified_export_copy_never_publishes_changed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import manga_localizer.services.exporting as exporting_module

    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"], relative_path="chapter/page.png")
        _add_region(client, image["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])
        generated = project_root / "generated/typeset/chapter/page.png"
        reviewed_bytes = generated.read_bytes()
        replacement = png_bytes((240, 320), color="red")
        original_copy = exporting_module._atomic_copy_verified
        changed_once = False

        def change_before_copy(source, destination, expected_checksum, *, label):
            nonlocal changed_once
            if source == generated and not changed_once:
                changed_once = True
                generated.write_bytes(replacement)
            return original_copy(
                source,
                destination,
                expected_checksum,
                label=label,
            )

        monkeypatch.setattr(exporting_module, "_atomic_copy_verified", change_before_copy)
        output_root = tmp_path / "verified-copy"
        existing = output_root / "translated/chapter/page.png"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(reviewed_bytes)
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "imageVariant": "typeset",
                    "outputPath": str(output_root),
                    "conflict": "overwrite",
                },
            },
        )
        failed = _wait_job(client, exported.json()["id"])
        assert failed["status"] == "failed"
        assert failed["items"][0]["error"] == ("Export failed; inspect the private project log")
        assert existing.read_bytes() == reviewed_bytes
        assert not list(existing.parent.glob(".page.png.*.tmp"))


def test_reading_order_changes_invalidate_clean_output_and_review(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"], relative_path="chapter/page.png")
        first = _add_region(client, image["id"], translation="第一", confirmed=True)
        second = _add_region(client, image["id"], translation="第二", confirmed=True)

        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [first["id"], second["id"]], "options": {}},
        )
        assert _wait_job(client, translated.json()["id"])["status"] == "completed"
        inpainted = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, inpainted.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])
        initial_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "imageVariant": "inpainted",
                    "outputPath": str(tmp_path / "before-reorder"),
                },
            },
        )
        assert _wait_job(client, initial_export.json()["id"])["status"] == "completed"

        current_first = next(
            region
            for region in client.get(f"/api/images/{image['id']}/regions").json()
            if region["id"] == first["id"]
        )
        patched_order = client.patch(
            f"/api/regions/{first['id']}",
            json={
                "order": 2,
                "confirmed": True,
                "expectedRevision": current_first["revision"],
            },
        )
        assert patched_order.status_code == 200, patched_order.text
        assert patched_order.json()["confirmed"] is False
        after_patch = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert after_patch["status"]["translation"] == "pending"
        assert after_patch["status"]["inpaint"] == "pending"
        assert after_patch["status"]["reviewState"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404

        stale_patch_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "images", "imageVariant": "inpainted"},
            },
        )
        stale_patch_job = _wait_job(client, stale_patch_export.json()["id"])
        assert stale_patch_job["status"] == "failed"
        assert stale_patch_job["items"][0]["error"] == (
            "Export failed; inspect the private project log"
        )

        reconfirmed = client.patch(
            f"/api/regions/{first['id']}",
            json={
                "confirmed": True,
                "expectedRevision": patched_order.json()["revision"],
            },
        )
        assert reconfirmed.status_code == 200, reconfirmed.text
        retranslated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [first["id"], second["id"]], "options": {}},
        )
        assert _wait_job(client, retranslated.json()["id"])["status"] == "completed"
        rerendered = client.post(
            f"/api/projects/{project['id']}/inpaint",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rerendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])

        ordered = client.post(
            f"/api/images/{image['id']}/reading-order",
            json={"regionIds": [second["id"], first["id"]]},
        )
        assert ordered.status_code == 200, ordered.text
        assert [region["id"] for region in ordered.json()] == [second["id"], first["id"]]
        after_bulk = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert after_bulk["status"]["translation"] == "pending"
        assert after_bulk["status"]["inpaint"] == "pending"
        assert after_bulk["status"]["reviewState"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404

        stale_bulk_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "images", "imageVariant": "inpainted"},
            },
        )
        stale_bulk_job = _wait_job(client, stale_bulk_export.json()["id"])
        assert stale_bulk_job["status"] == "failed"
        assert stale_bulk_job["items"][0]["error"] == (
            "Export failed; inspect the private project log"
        )


def test_mixed_source_extensions_receive_distinct_render_and_export_stems(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    output_root = tmp_path / "export"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        first = upload_image(
            client,
            project["id"],
            relative_path="chapter/page.jpg",
            data=png_bytes(color="ivory"),
        )
        second = upload_image(
            client,
            project["id"],
            relative_path="chapter/page.png",
            data=png_bytes(color="lavender"),
        )
        assert first["relativePath"] == "chapter/page.jpg"
        assert second["relativePath"] == "chapter/page-2.png"
        _add_region(client, first["id"], translation="第一张", confirmed=True)
        _add_region(client, second["id"], translation="第二张", confirmed=True)

        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={
                "imageIds": [first["id"], second["id"]],
                "options": {"concurrency": 2},
            },
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], first["id"])
        _review_image(client, project["id"], second["id"])
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [first["id"], second["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(output_root),
                    "conflict": "overwrite",
                },
            },
        )
        assert _wait_job(client, exported.json()["id"])["status"] == "completed"

        first_json = json.loads(
            (output_root / "translated-text/chapter/page.json").read_text("utf-8")
        )
        second_json = json.loads(
            (output_root / "translated-text/chapter/page-2.json").read_text("utf-8")
        )
        assert first_json["image"]["id"] == first["id"]
        assert second_json["image"]["id"] == second["id"]
        assert first_json["regions"][0]["trustDisposition"] == "trusted"
        assert first_json["regions"][0]["trustPolicyVersion"] == 1
        assert (output_root / "translated/chapter/page.png").is_file()
        assert (output_root / "translated/chapter/page-2.png").is_file()


def test_export_rejects_another_project_bundle_without_writing(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        first = create_project(client, tmp_path / "first", name="项目 A")
        second_root = tmp_path / "second"
        second = create_project(client, second_root, name="项目 B")
        image = upload_image(client, first["id"], relative_path="chapter/page.png")
        _add_region(client, image["id"])
        manifest_before = (second_root / "project/project.json").read_bytes()

        queued = client.post(
            f"/api/projects/{first['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "json",
                    "outputPath": str(second_root),
                    "conflict": "skip",
                },
            },
        )
        failed = _wait_job(client, queued.json()["id"])
        assert failed["status"] == JobStatus.FAILED.value
        assert failed["items"][0]["error"] == ("Export failed; inspect the private project log")
        assert (second_root / "project/project.json").read_bytes() == manifest_before
        with sqlite3.connect(second_root / "project/project.sqlite3") as database:
            assert database.execute("SELECT id FROM projects").fetchone()[0] == second["id"]
        assert not (second_root / "original-text").exists()
        assert not (second_root / "translated-text").exists()


def test_export_rejects_symlinked_project_bundle_target(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "source-project")
        image = upload_image(client, project["id"])
        target = tmp_path / "target"
        outside = tmp_path / "outside"
        target.mkdir()
        outside.mkdir()
        try:
            (target / "project").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks are unavailable on this platform")

        queued = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "json",
                    "outputPath": str(target),
                    "conflict": "rename",
                },
            },
        )
        failed = _wait_job(client, queued.json()["id"])
        assert failed["status"] == JobStatus.FAILED.value
        assert failed["items"][0]["error"] == ("Export failed; inspect the private project log")
        assert list(outside.iterdir()) == []


def test_generated_and_export_symlinks_cannot_overwrite_immutable_source(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    source_data = png_bytes((240, 320), color="ivory")
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"], data=source_data)
        _add_region(client, image["id"], confirmed=True)
        source = project_root / "source/第一章/ページ一.png"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        generated_target = project_root / "generated/typeset/第一章/ページ一.png"
        generated_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            generated_target.symlink_to(source)
        except OSError:
            pytest.skip("File symlinks are unavailable on this platform")
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        failed_render = _wait_job(client, render.json()["id"])
        assert failed_render["status"] == "failed"
        assert failed_render["items"][0]["error"] == (
            "Image rendering failed; inspect the private project log"
        )
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash

        generated_target.unlink()
        rerender = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rerender.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])
        export_target = project_root / "translated/第一章/ページ一.png"
        export_target.parent.mkdir(parents=True, exist_ok=True)
        export_target.symlink_to(source)
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "images", "conflict": "overwrite"},
            },
        )
        failed_export = _wait_job(client, exported.json()["id"])
        assert failed_export["status"] == "failed"
        assert failed_export["items"][0]["error"] == (
            "Export failed; inspect the private project log"
        )
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


def test_export_never_overwrites_a_trusted_local_original(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        originals = tmp_path / "originals"
        collision = originals / "translated/a.png"
        originals.mkdir()
        collision.parent.mkdir()
        (originals / "a.png").write_bytes(png_bytes(color="white"))
        collision_bytes = b"corrupt pre-existing source-side output"
        collision.write_bytes(collision_bytes)
        project = create_project(client, tmp_path / "project")
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(originals)]},
        )
        assert imported.status_code == 201, imported.text
        assert imported.headers["X-Manga-Localizer-Import-Failures"] == "1"
        image = next(item for item in imported.json() if item["relativePath"] == "a.png")
        later_source = tmp_path / "later-import/other.png"
        later_source.parent.mkdir()
        later_source.write_bytes(png_bytes(color="gold"))
        later_import = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(later_source)]},
        )
        assert later_import.status_code == 201, later_import.text
        _add_region(
            client,
            image["id"],
            translation="不会写回原稿",
            confirmed=True,
        )
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])

        queued = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "images",
                    "outputPath": str(originals),
                    "conflict": "overwrite",
                    "preserveTree": True,
                },
            },
        )
        failed = _wait_job(client, queued.json()["id"])
        assert failed["status"] == JobStatus.FAILED.value
        assert failed["items"][0]["error"] == ("Export failed; inspect the private project log")
        assert collision.read_bytes() == collision_bytes
        assert not (originals / "masks/a.png").exists()


def test_multiple_file_import_roots_do_not_block_unrelated_exports(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    custom_root = tmp_path / "custom-output"
    with TestClient(create_app(settings, start_worker=True)) as client:
        first_source = tmp_path / "incoming-a/a.png"
        second_source = tmp_path / "incoming-b/b.png"
        first_source.parent.mkdir()
        second_source.parent.mkdir()
        first_source.write_bytes(png_bytes(color="ivory"))
        second_source.write_bytes(png_bytes(color="lavender"))
        project = create_project(client, project_root)
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(first_source), str(second_source)]},
        )
        assert imported.status_code == 201, imported.text
        image_ids = [image["id"] for image in imported.json()]

        default_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={"imageIds": image_ids, "options": {"format": "json"}},
        )
        assert _wait_job(client, default_export.json()["id"])["status"] == "completed"
        assert (project_root / "original-text/a.json").is_file()
        assert (project_root / "original-text/b.json").is_file()

        custom_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": image_ids,
                "options": {
                    "format": "json",
                    "outputPath": str(custom_root),
                    "conflict": "overwrite",
                },
            },
        )
        assert _wait_job(client, custom_export.json()["id"])["status"] == "completed"
        assert (custom_root / "translated-text/a.json").is_file()
        assert (custom_root / "translated-text/b.json").is_file()
        with sqlite3.connect(project_root / "project/project.sqlite3") as database:
            assert database.execute("SELECT count(*) FROM import_boundaries").fetchone()[0] == 2
        assert (custom_root / "export.json").is_file()
        assert not (custom_root / "project").exists()
        assert not (custom_root / "source").exists()
        assert not (custom_root / "generated").exists()
        summary = (custom_root / "export.json").read_text("utf-8")
        assert str(first_source) not in summary
        assert str(second_source) not in summary


def test_export_recovers_stale_atomic_temps_for_skip_and_renamed_destinations(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"], relative_path="chapter/page.png")
        _add_region(client, image["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])

        skip_root = tmp_path / "skip-output"
        (skip_root / "translated/chapter").mkdir(parents=True)
        (skip_root / "original-text/chapter").mkdir(parents=True)
        (skip_root / f"translated/chapter/.page.png.{'a' * 32}.tmp").write_bytes(b"partial")
        (skip_root / f"original-text/chapter/.page.json.{'b' * 32}.tmp").write_bytes(b"partial")
        skipped = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(skip_root),
                    "conflict": "skip",
                },
            },
        )
        assert _wait_job(client, skipped.json()["id"])["status"] == "completed"
        assert (skip_root / "translated/chapter/page.png").is_file()
        assert (skip_root / "original-text/chapter/page.json").is_file()
        assert not list(skip_root.rglob(".*.tmp"))

        rename_root = tmp_path / "rename-output"
        translated = rename_root / "translated/chapter"
        original_text = rename_root / "original-text/chapter"
        translated.mkdir(parents=True)
        original_text.mkdir(parents=True)
        (translated / "page.png").write_bytes(b"existing")
        (original_text / "page.json").write_text("{}", "utf-8")
        (translated / f".page-2.png.{'c' * 32}.tmp").write_bytes(b"partial artwork")
        (original_text / f".page-2.json.{'d' * 32}.tmp").write_bytes(b"partial metadata")
        renamed = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(rename_root),
                    "conflict": "rename",
                },
            },
        )
        assert _wait_job(client, renamed.json()["id"])["status"] == "completed"
        with Image.open(translated / "page-2.png") as exported_image:
            exported_image.verify()
        assert (
            json.loads((original_text / "page-2.json").read_text("utf-8"))["image"]["id"]
            == image["id"]
        )
        assert not list(rename_root.rglob(".*.tmp"))


def test_flat_export_renames_cross_platform_equivalent_filenames(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    output_root = tmp_path / "flat-output"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        first = upload_image(client, project["id"], relative_path="a/Page.png")
        second = upload_image(client, project["id"], relative_path="b/page.png")
        _add_region(client, first["id"], confirmed=True)
        _add_region(client, second["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [first["id"], second["id"]], "options": {"concurrency": 2}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], first["id"])
        _review_image(client, project["id"], second["id"])
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [first["id"], second["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(output_root),
                    "conflict": "rename",
                    "preserveTree": False,
                },
            },
        )
        assert _wait_job(client, exported.json()["id"])["status"] == "completed"

        translated_names = sorted(path.name for path in (output_root / "translated").glob("*.png"))
        metadata_names = sorted(
            path.name for path in (output_root / "original-text").glob("*.json")
        )
        assert translated_names == ["Page.png", "page-2.png"]
        assert metadata_names == ["Page.json", "page-2.json"]
        assert len({name.casefold() for name in translated_names}) == 2


def test_export_bundle_never_overwrites_an_original_with_an_arbitrary_extension(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        originals = tmp_path / "originals"
        originals.mkdir()
        (originals / "page.png").write_bytes(png_bytes(color="white"))
        disguised_original = originals / "project/project.json"
        disguised_original.parent.mkdir()
        disguised_bytes = png_bytes(color="purple")
        disguised_original.write_bytes(disguised_bytes)
        project = create_project(client, tmp_path / "project")
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(originals)]},
        )
        assert imported.status_code == 201, imported.text
        page = next(item for item in imported.json() if item["relativePath"] == "page.png")

        queued = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [page["id"]],
                "options": {
                    "format": "json",
                    "outputPath": str(originals),
                    "conflict": "overwrite",
                },
            },
        )
        failed = _wait_job(client, queued.json()["id"])
        assert failed["status"] == JobStatus.FAILED.value
        assert failed["items"][0]["error"] == ("Export failed; inspect the private project log")
        assert disguised_original.read_bytes() == disguised_bytes
        assert not (originals / "original-text/page.json").exists()


def test_export_reports_every_missing_typeset_page_as_failure(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        first = upload_image(client, project["id"], relative_path="a/one.png")
        second = upload_image(client, project["id"], relative_path="b/two.png")
        response = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [first["id"], second["id"]],
                "options": {"format": "images", "outputPath": str(tmp_path / "export")},
            },
        )
        failed = _wait_job(client, response.json()["id"])
        assert failed["status"] == "failed"
        assert failed["completed"] == 0
        assert len(failed["items"]) == 2
        assert all(item["status"] == "failed" for item in failed["items"])
        assert all(
            item["error"] == "Export failed; inspect the private project log"
            for item in failed["items"]
        )
        images = client.get(f"/api/projects/{project['id']}/images").json()
        assert all(image["processingErrors"] for image in images)
        assert all(image["revision"] >= 2 for image in images)


def test_export_retry_requeues_a_completed_page_that_changed_after_partial_failure(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    output_root = tmp_path / "export"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        first = upload_image(client, project["id"], relative_path="a/one.png")
        second = upload_image(client, project["id"], relative_path="b/two.png")
        first_region = _add_region(client, first["id"], confirmed=True)
        _add_region(client, second["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [first["id"], second["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], first["id"])

        response = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [first["id"], second["id"]],
                "options": {
                    "format": "images",
                    "outputPath": str(output_root),
                    "conflict": "overwrite",
                },
            },
        )
        failed = _wait_job(client, response.json()["id"])
        assert [item["status"] for item in failed["items"]] == ["completed", "failed"]
        first_export = output_root / "translated/a/one.png"
        before = first_export.read_bytes()

        changed = client.patch(
            f"/api/regions/{first_region['id']}",
            json={
                "translationText": "修改后的译文",
                "expectedRevision": first_region["revision"],
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["confirmed"] is False
        reconfirmed = client.patch(
            f"/api/regions/{first_region['id']}",
            json={
                "confirmed": True,
                "expectedRevision": changed.json()["revision"],
            },
        )
        assert reconfirmed.status_code == 200, reconfirmed.text
        assert reconfirmed.json()["confirmed"] is True
        rerendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [first["id"]], "options": {}},
        )
        assert _wait_job(client, rerendered.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], first["id"])
        _review_image(client, project["id"], second["id"])

        retried = client.post(f"/api/jobs/{response.json()['id']}/retry")
        assert retried.status_code == 200, retried.text
        assert [item["status"] for item in retried.json()["items"]] == ["queued", "queued"]
        assert retried.json()["completed"] == 0
        assert retried.json()["progress"] == 0
        completed = _wait_job(client, response.json()["id"])
        assert completed["status"] == "completed"
        assert first_export.read_bytes() != before
        assert all(item["status"] == "completed" for item in completed["items"])


def test_running_jobs_and_items_recover_to_queued(client: TestClient, tmp_path: Path, app) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    region = _add_region(client, image["id"])
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
    ).json()
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        job = session.scalar(
            select(Job).options(selectinload(Job.items)).where(Job.id == queued["id"])
        )
        assert job is not None
        job.status = JobStatus.RUNNING.value
        job.items[0].status = JobStatus.RUNNING.value
    assert store.recover_jobs() == 1
    recovered = app.state.queue.get_job(store, queued["id"])
    assert recovered.status == JobStatus.QUEUED.value
    assert recovered.items[0].status == JobStatus.QUEUED.value

    with store.session() as session:
        job = session.scalar(
            select(Job).options(selectinload(Job.items)).where(Job.id == queued["id"])
        )
        assert job is not None
        job.status = JobStatus.PAUSED.value
        job.items[0].status = JobStatus.RUNNING.value
    store.write_snapshot()
    before = json.loads(store.manifest_path.read_text("utf-8"))
    assert before["jobs"][0]["status"] == JobStatus.PAUSED.value
    assert before["jobs"][0]["items"][0]["status"] == JobStatus.RUNNING.value

    assert store.recover_jobs() == 1
    paused = app.state.queue.get_job(store, queued["id"])
    assert paused.status == JobStatus.PAUSED.value
    assert paused.items[0].status == JobStatus.QUEUED.value
    snapshot = json.loads(store.manifest_path.read_text("utf-8"))
    assert snapshot["jobs"][0]["status"] == JobStatus.PAUSED.value
    assert snapshot["jobs"][0]["items"][0]["status"] == JobStatus.QUEUED.value


def test_cancelled_running_item_recovers_as_cancelled_and_can_be_retried(
    client: TestClient,
    tmp_path: Path,
    app,
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    region = _add_region(client, image["id"])
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
    ).json()
    claimed = app.state.queue._claim_next()
    assert claimed is not None
    store, job_id = claimed
    assert job_id == queued["id"]
    running = app.state.queue.get_job(store, job_id)
    assert app.state.queue._begin_item(store, job_id, running.items[0].id) is True

    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["items"][0]["status"] == JobStatus.RUNNING.value

    assert store.recover_jobs() == 1
    recovered = app.state.queue.get_job(store, job_id)
    assert recovered.status == JobStatus.CANCELLED.value
    assert recovered.items[0].status == JobStatus.CANCELLED.value
    retried = client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == JobStatus.QUEUED.value
    assert retried.json()["items"][0]["status"] == JobStatus.QUEUED.value


def test_export_job_api_never_exposes_options_targets_or_internal_output(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        private_output = tmp_path / "private-export-target"
        created = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "json",
                    "outputPath": str(private_output),
                    "nested": {
                        "text": "never-public-text",
                        "regionIds": ["never-public-region"],
                    },
                },
            },
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]

        responses = [
            created,
            client.get(f"/api/jobs/{job_id}"),
            client.get(f"/api/jobs?projectId={project['id']}"),
            client.post(f"/api/jobs/{job_id}/pause"),
            client.post(f"/api/jobs/{job_id}/resume"),
            client.post(f"/api/jobs/{job_id}/cancel"),
            client.post(f"/api/jobs/{job_id}/retry"),
        ]
        for response in responses:
            assert response.status_code in {200, 202}, response.text
            serialized = response.text
            assert "options" not in serialized
            assert str(private_output) not in serialized
            assert "never-public-text" not in serialized
            assert "never-public-region" not in serialized
            assert "regionIds" not in serialized


def test_public_job_and_image_errors_never_echo_provider_content_or_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    sentinel = "private OCR text /outside/private/page.png region-secret-id x=123,y=456"

    def fail_preprocess(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(app.state.providers.preprocessing, "preprocess", fail_preprocess)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        submitted = client.post(
            f"/api/projects/{project['id']}/preprocess",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert submitted.status_code == 202, submitted.text
        failed = _wait_job(client, submitted.json()["id"])
        assert failed["status"] == "failed"

        public_responses = (
            client.get(f"/api/jobs/{failed['id']}"),
            client.get(f"/api/jobs?projectId={project['id']}"),
            client.get(f"/api/projects/{project['id']}/images"),
        )
        for response in public_responses:
            assert response.status_code == 200, response.text
            assert sentinel not in response.text
            assert "/outside/private/page.png" not in response.text
            assert "region-secret-id" not in response.text
            assert "x=123" not in response.text

        assert failed["items"][0]["error"] == (
            "Image preprocessing failed; inspect the private project log"
        )
        image_state = public_responses[-1].json()[0]
        assert image_state["processingErrors"][-1]["error"] == (
            "Image preprocessing failed; inspect the private project log"
        )
        store = app.state.registry.get(project["id"])
        internal = app.state.queue.get_job(store, failed["id"])
        assert sentinel in internal.items[0].error


def test_export_bundle_finalization_recovers_temp_only_and_database_only_crashes(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=False)

    def execute_next() -> tuple[Any, str]:
        claimed = app.state.queue._claim_next()
        assert claimed is not None
        claimed_store, claimed_job_id = claimed
        asyncio.run(app.state.queue._execute(claimed_store, claimed_job_id))
        return claimed_store, claimed_job_id

    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        _add_region(client, image["id"], confirmed=True)
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        ).json()
        store, rendered_job_id = execute_next()
        assert rendered_job_id == render["id"]
        assert app.state.queue.get_job(store, rendered_job_id).status == "completed"
        _review_image(client, project["id"], image["id"])

        output_root = tmp_path / "portable-export"
        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(output_root),
                    "conflict": "overwrite",
                },
            },
        ).json()
        store, export_job_id = execute_next()
        assert export_job_id == exported["id"]
        completed = app.state.queue.get_job(store, export_job_id)
        assert completed.status == "completed"
        assert completed.options["bundleFinalized"] is True

        project_directory = output_root / "project"
        owner_files = list(project_directory.glob(".manga-localizer-bundle.*.owner"))
        assert len(owner_files) == 1
        assert owner_files[0].read_bytes() == b""

        for crash_shape in ("temp-only", "sqlite-sidecars", "database-only"):
            with store.session() as session:
                job = session.get(Job, export_job_id)
                assert job is not None
                job.status = JobStatus.COMPLETED.value
                job.options = {**dict(job.options), "bundleFinalized": False}
            (project_directory / "project.json").unlink(missing_ok=True)
            if crash_shape == "temp-only":
                (project_directory / "project.sqlite3").unlink(missing_ok=True)
                (project_directory / f".project.json.{'a' * 32}.tmp").write_text(
                    "interrupted",
                    "utf-8",
                )
            elif crash_shape == "sqlite-sidecars":
                (project_directory / "project.sqlite3").unlink(missing_ok=True)
                temporary = project_directory / f".project.sqlite3.{'b' * 32}.tmp"
                temporary.write_bytes(b"interrupted sqlite copy")
                for suffix in ("-journal", "-wal", "-shm"):
                    temporary.with_name(temporary.name + suffix).write_bytes(b"interrupted")
            else:
                assert (project_directory / "project.sqlite3").is_file()
                portable_source = output_root / "source/第一章/ページ一.png"
                portable_source.write_bytes(b"corrupt portable source")

            assert store.recover_jobs() == 1
            recovered_store, recovered_job_id = execute_next()
            assert recovered_store is store
            assert recovered_job_id == export_job_id
            recovered = app.state.queue.get_job(store, export_job_id)
            assert recovered.status == JobStatus.COMPLETED.value
            assert recovered.options["bundleFinalized"] is True
            assert (project_directory / "project.json").is_file()
            assert (project_directory / "project.sqlite3").is_file()
            assert not list(project_directory.glob(".project.*.tmp*"))
            assert (output_root / "source/第一章/ページ一.png").read_bytes() == (
                store.root / "source/第一章/ページ一.png"
            ).read_bytes()
            with sqlite3.connect(project_directory / "project.sqlite3") as database:
                encoded = database.execute(
                    "SELECT options FROM jobs WHERE id = ?",
                    (export_job_id,),
                ).fetchone()[0]
            assert json.loads(encoded)["bundleFinalized"] is True


def test_relative_export_output_is_canonicalized_before_worker_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=False)

    def execute_next() -> tuple[Any, str]:
        claimed = app.state.queue._claim_next()
        assert claimed is not None
        claimed_store, claimed_job_id = claimed
        asyncio.run(app.state.queue._execute(claimed_store, claimed_job_id))
        return claimed_store, claimed_job_id

    project_root = tmp_path / "project"
    with TestClient(app) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"])
        _add_region(client, image["id"], confirmed=True)
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        ).json()
        _, rendered_job_id = execute_next()
        assert rendered_job_id == rendered["id"]
        _review_image(client, project["id"], image["id"])

        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": "relative-output",
                    "conflict": "overwrite",
                },
            },
        ).json()
        expected_root = (project_root / "relative-output").resolve()
        assert "options" not in exported
        store = app.state.registry.get(project["id"])
        with store.session() as session:
            stored = session.get(Job, exported["id"])
            assert stored is not None
            assert stored.options["outputPath"] == str(expected_root)
        changed_cwd = tmp_path / "different-working-directory"
        changed_cwd.mkdir()
        monkeypatch.chdir(changed_cwd)

        store, export_job_id = execute_next()
        assert export_job_id == exported["id"]
        final = app.state.queue.get_job(store, export_job_id)
        assert final.status == JobStatus.COMPLETED.value
        assert (expected_root / "translated/第一章/ページ一.png").is_file()
        assert (expected_root / "project/project.sqlite3").is_file()


def test_export_stays_nonterminal_until_bundle_finalization_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import manga_localizer.queue as queue_module

    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    entered = threading.Event()
    release = threading.Event()
    original_finalize = queue_module.ensure_project_bundle

    def blocking_finalize(*args, **kwargs) -> None:
        entered.set()
        assert release.wait(3)
        original_finalize(*args, **kwargs)

    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        _add_region(client, image["id"], confirmed=True)
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, render.json()["id"])["status"] == "completed"
        _review_image(client, project["id"], image["id"])
        monkeypatch.setattr(queue_module, "ensure_project_bundle", blocking_finalize)

        exported = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {
                    "format": "both",
                    "outputPath": str(tmp_path / "custom"),
                },
            },
        ).json()
        assert entered.wait(3)
        store = app.state.registry.get(project["id"])
        with sqlite3.connect(store.database_path) as database:
            status, options = database.execute(
                "SELECT status, options FROM jobs WHERE id = ?",
                (exported["id"],),
            ).fetchone()
        assert status == JobStatus.RUNNING.value
        assert json.loads(options)["bundleFinalized"] is False

        polled: list[dict[str, Any]] = []

        def poll() -> None:
            polled.append(client.get(f"/api/jobs/{exported['id']}").json())

        polling_thread = threading.Thread(target=poll)
        polling_thread.start()
        time.sleep(0.05)
        assert polling_thread.is_alive()
        release.set()
        polling_thread.join(3)
        assert not polling_thread.is_alive()
        assert polled[0]["status"] == JobStatus.COMPLETED.value
        assert (tmp_path / "custom/project/project.json").is_file()
