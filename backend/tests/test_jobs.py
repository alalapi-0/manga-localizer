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

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from manga_localizer.config import Settings
from manga_localizer.database import ImageAsset, Job, JobStatus
from manga_localizer.main import create_app
from manga_localizer.providers.ocr import OCRRegion

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


def _add_region(
    client: TestClient,
    image_id: str,
    *,
    source: str = "こんにちは",
    translation: str = "人工译文",
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
            "direction": "vertical",
            "repair": {"padding": 3},
            "style": {"fontSize": 26, "minFontSize": 10, "strokeWidth": 1},
        },
    )
    assert response.status_code == 201, response.text
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
    assert "apiKey" not in job["options"]
    assert "api-key" not in job["options"]
    assert "Authorization" not in job["options"]
    assert "token" not in job["options"]["nested"]
    assert "serviceCredential" not in job["options"]["nested"]

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


def test_mock_and_manual_translation_jobs_preserve_reviewed_text(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], translation="不要覆盖")

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


def test_region_repair_settings_drive_the_inpainting_job(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"])
        updated = client.patch(
            f"/api/regions/{region['id']}",
            json={
                "repair": {
                    "method": "solid",
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


def test_region_and_translation_edits_invalidate_stale_render_and_export(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], translation="第一版")
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, render.json()["id"])["status"] == "completed"
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
            json={"translationText": "第二版", "expectedRevision": region["revision"]},
        )
        assert edited.status_code == 200, edited.text
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["inpaint"] == "done"
        assert state["status"]["typeset"] == "pending"
        assert state["status"]["export"] == "pending"
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 200
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404
        assert not (project_root / "generated/typeset/第一章/ページ一.png").exists()

        stale_export = client.post(
            f"/api/projects/{project['id']}/export",
            json={
                "imageIds": [image["id"]],
                "options": {"format": "images", "conflict": "rename"},
            },
        )
        failed = _wait_job(client, stale_export.json()["id"])
        assert failed["status"] == JobStatus.FAILED.value
        assert "stale" in failed["items"][0]["error"]

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
    assert state["status"]["translation"] == "done"
    assert state["status"]["inpaint"] == "done"
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
        region = _add_region(client, image["id"])
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
        region = _add_region(client, image["id"])
        translated = client.post(
            f"/api/projects/{project['id']}/translate",
            json={"regionIds": [region["id"]], "options": {"provider": "mock"}},
        )
        assert _wait_job(client, translated.json()["id"])["status"] == "completed"
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
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
        state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert state["status"]["translation"] == "pending"
        assert state["status"]["inpaint"] == "done"
        assert state["status"]["typeset"] == "pending"
        assert state["status"]["export"] == "pending"
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


def test_ocr_job_endpoint_updates_region_without_http_blocking(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    app = create_app(settings, start_worker=True)
    app.state.providers.ocr = _FakeOCR()
    with TestClient(app) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"])
        region = _add_region(client, image["id"], source="旧文本")
        submitted = client.post(
            f"/api/projects/{project['id']}/ocr",
            json={"regionIds": [region["id"]], "options": {}},
        )
        assert submitted.status_code == 202
        result = _wait_job(client, submitted.json()["id"])
        assert result["status"] == "completed"
        updated = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert updated["sourceText"] == "実際のOCR"
        assert updated["confidence"] == 0.91
        image_state = client.get(f"/api/projects/{project['id']}/images").json()[0]
        assert image_state["ocrProvider"] == "tesseract"
        assert image_state["status"]["ocr"] == "done"


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
        assert "changed during OCR" in failed_ocr["items"][0]["error"]
        current = client.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["sourceText"] == "用户新原文"

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
        assert "changed during translation" in failed_translation["items"][0]["error"]
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
        region = _add_region(client, image["id"])
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
        release.set()
        failed = _wait_job(client, render.json()["id"])
        assert failed["status"] == "failed"
        assert "changed during rendering" in failed["items"][0]["error"]
        assert client.get(f"/api/images/{image['id']}/content?variant=erased").status_code == 404
        assert client.get(f"/api/images/{image['id']}/content?variant=typeset").status_code == 404
        assert not list((project_root / "generated").rglob("*.png"))


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
        _add_region(client, image["id"], translation="翻译完成")
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
        _add_region(client, first["id"], translation="第一张")
        _add_region(client, second["id"], translation="第二张")

        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={
                "imageIds": [first["id"], second["id"]],
                "options": {"concurrency": 2},
            },
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
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
        assert "different portable project" in failed["items"][0]["error"]
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
        assert "symlink" in failed["items"][0]["error"]
        assert list(outside.iterdir()) == []


def test_generated_and_export_symlinks_cannot_overwrite_immutable_source(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    project_root = tmp_path / "project"
    source_data = png_bytes((240, 320), color="ivory")
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, project_root)
        image = upload_image(client, project["id"], data=source_data)
        _add_region(client, image["id"])
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
        assert "symlink" in failed_render["items"][0]["error"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash

        generated_target.unlink()
        rerender = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rerender.json()["id"])["status"] == "completed"
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
        assert "symlink" in failed_export["items"][0]["error"]
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
        _add_region(client, image["id"], translation="不会写回原稿")
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"

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
        assert "original imported file" in failed["items"][0]["error"]
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
        with sqlite3.connect(custom_root / "project/project.sqlite3") as database:
            assert database.execute("SELECT count(*) FROM import_boundaries").fetchone()[0] == 0
        portable_bytes = (custom_root / "project/project.sqlite3").read_bytes()
        assert str(first_source).encode() not in portable_bytes
        assert str(second_source).encode() not in portable_bytes


def test_export_recovers_stale_atomic_temps_for_skip_and_renamed_destinations(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "catalog", worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=True)) as client:
        project = create_project(client, tmp_path / "project")
        image = upload_image(client, project["id"], relative_path="chapter/page.png")
        _add_region(client, image["id"])
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"

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
        _add_region(client, first["id"])
        _add_region(client, second["id"])
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [first["id"], second["id"]], "options": {"concurrency": 2}},
        )
        assert _wait_job(client, rendered.json()["id"])["status"] == "completed"
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
        assert "original imported file" in failed["items"][0]["error"]
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
        assert all("not exported" in item["error"] for item in failed["items"])
        images = client.get(f"/api/projects/{project['id']}/images").json()
        assert all(image["processingErrors"] for image in images)
        assert all(image["revision"] >= 2 for image in images)


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
        _add_region(client, image["id"])
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        ).json()
        store, rendered_job_id = execute_next()
        assert rendered_job_id == render["id"]
        assert app.state.queue.get_job(store, rendered_job_id).status == "completed"

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
        _add_region(client, image["id"])
        rendered = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        ).json()
        _, rendered_job_id = execute_next()
        assert rendered_job_id == rendered["id"]

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
        assert exported["options"]["outputPath"] == str(expected_root)
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
        _add_region(client, image["id"])
        render = client.post(
            f"/api/projects/{project['id']}/render",
            json={"imageIds": [image["id"]], "options": {}},
        )
        assert _wait_job(client, render.json()["id"])["status"] == "completed"
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
