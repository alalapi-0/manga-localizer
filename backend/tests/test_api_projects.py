from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manga_localizer.database import ImageAsset, Revision
from manga_localizer.main import create_app
from manga_localizer.security import (
    UnsafePathError,
    portable_path_key,
    resolve_within,
    safe_relative_path,
)
from manga_localizer.services.images import invalidate_image_pipeline, stage_reviews

from .conftest import create_project, png_bytes, upload_image


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_visual_stage_review_is_normalized_revision_guarded_and_reopenable(
    app, client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "stage-review-project")
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {**image.status, "inpaint": "done", "stageReviews": {"legacy": "bad"}}
    generated = tmp_path / "stage-review-project/generated"
    (generated / "inpainted/第一章").mkdir(parents=True)
    (generated / "masks/第一章").mkdir(parents=True)
    artifact_bytes = png_bytes()
    mask_bytes = png_bytes()
    (generated / "inpainted/第一章/ページ一.png").write_bytes(artifact_bytes)
    (generated / "masks/第一章/ページ一.png").write_bytes(mask_bytes)

    initial = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert initial["stageReviews"] == {}
    accepted = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": "accepted",
            "expectedRevision": initial["revision"],
            "observedArtifactChecksum": _checksum(artifact_bytes),
            "observedMaskChecksum": _checksum(mask_bytes),
        },
    )
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["status"]["inpaint"] == "done"
    assert "stageReviews" not in accepted_body["status"]
    assert accepted_body["stageReviews"]["inpaint"] == {
        "state": "accepted",
        "reviewedAt": accepted_body["stageReviews"]["inpaint"]["reviewedAt"],
        "resultRevision": initial["revision"],
        "artifactChecksum": accepted_body["stageReviews"]["inpaint"]["artifactChecksum"],
        "maskChecksum": accepted_body["stageReviews"]["inpaint"]["maskChecksum"],
    }
    assert accepted_body["revision"] == initial["revision"] + 1
    snapshot = json.loads((tmp_path / "stage-review-project/project/project.json").read_text())
    snapshot_image = next(entry for entry in snapshot["images"] if entry["id"] == imported["id"])
    assert snapshot_image["status"]["stageReviews"]["inpaint"]["state"] == "accepted"
    fresh_settings = app.state.settings.model_copy(
        update={"data_dir": tmp_path / "fresh-stage-review-catalog"}
    )
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        reopened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(tmp_path / "stage-review-project/project/project.json")},
        )
        assert reopened.status_code == 200, reopened.text
        persisted = fresh.get(f"/api/projects/{project['id']}/images").json()[0]
        assert persisted["stageReviews"]["inpaint"]["state"] == "accepted"

    conflict = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": "rejected",
            "expectedRevision": initial["revision"],
            "observedArtifactChecksum": _checksum(artifact_bytes),
            "observedMaskChecksum": _checksum(mask_bytes),
        },
    )
    assert conflict.status_code == 409
    withdrawn = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={"state": "pending", "expectedRevision": accepted_body["revision"]},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["stageReviews"] == {}
    assert client.get(f"/api/projects/{project['id']}/images").json()[0]["stageReviews"] == {}

    with store.session() as session:
        operations = [
            revision.operation
            for revision in session.query(Revision).filter(Revision.entity_id == imported["id"])
        ]
    assert operations[-2:] == ["stage-review", "stage-review"]


@pytest.mark.parametrize("state", ["accepted", "rejected"])
def test_visual_stage_review_binds_the_exact_served_artifacts(
    app, client: TestClient, tmp_path: Path, state: str
) -> None:
    project_root = tmp_path / f"observed-stage-{state}"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    artifact_path = project_root / "generated/inpainted/第一章/ページ一.png"
    mask_path = project_root / "generated/masks/第一章/ページ一.png"
    artifact_path.parent.mkdir(parents=True)
    mask_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(png_bytes(color="red"))
    mask_path.write_bytes(png_bytes(color="black"))
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {**image.status, "inpaint": "done"}

    current = client.get(f"/api/projects/{project['id']}/images").json()[0]
    served_artifact = client.get(f"/api/images/{imported['id']}/generated/inpainted")
    served_mask = client.get(f"/api/images/{imported['id']}/generated/mask")
    assert served_artifact.status_code == 200
    assert served_mask.status_code == 200
    artifact_checksum = _checksum(served_artifact.content)
    mask_checksum = _checksum(served_mask.content)

    response = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": state,
            "expectedRevision": current["revision"],
            "observedArtifactChecksum": artifact_checksum,
            "observedMaskChecksum": mask_checksum,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["stageReviews"]["inpaint"] == {
        "state": state,
        "reviewedAt": response.json()["stageReviews"]["inpaint"]["reviewedAt"],
        "resultRevision": current["revision"],
        "artifactChecksum": artifact_checksum,
        "maskChecksum": mask_checksum,
    }


@pytest.mark.parametrize(
    ("mismatch_field", "mismatches"),
    (
        ("observedArtifactChecksum", ["artifactChecksum"]),
        ("observedMaskChecksum", ["maskChecksum"]),
    ),
)
def test_observed_stage_checksum_conflict_has_zero_mutation(
    app,
    client: TestClient,
    tmp_path: Path,
    mismatch_field: str,
    mismatches: list[str],
) -> None:
    project_root = tmp_path / f"observed-conflict-{mismatch_field}"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    artifact_bytes = png_bytes(color="red")
    mask_bytes = png_bytes(color="black")
    typeset_bytes = png_bytes(color="blue")
    paths = {
        "inpainted": artifact_bytes,
        "masks": mask_bytes,
        "typeset": typeset_bytes,
    }
    for directory, data in paths.items():
        target = project_root / "generated" / directory / "第一章/ページ一.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
    reviewed_at = "2026-08-13T10:00:00+00:00"
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {
            **image.status,
            "inpaint": "done",
            "typeset": "done",
            "stageReviews": {
                "inpaint": {
                    "state": "accepted",
                    "reviewedAt": reviewed_at,
                    "resultRevision": image.revision,
                    "artifactChecksum": _checksum(artifact_bytes),
                    "maskChecksum": _checksum(mask_bytes),
                },
                "typeset": {
                    "state": "accepted",
                    "reviewedAt": reviewed_at,
                    "resultRevision": image.revision,
                    "artifactChecksum": _checksum(typeset_bytes),
                },
            },
        }

    current = client.get(f"/api/projects/{project['id']}/images").json()[0]
    project_before = client.get(f"/api/projects/{project['id']}").json()["revision"]
    with store.session() as session:
        audit_before = session.query(Revision).count()
    request = {
        "state": "accepted",
        "expectedRevision": current["revision"],
        "observedArtifactChecksum": _checksum(artifact_bytes),
        "observedMaskChecksum": _checksum(mask_bytes),
        mismatch_field: "0" * 64,
    }

    response = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json=request,
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "message": "The reviewed visual no longer matches the current stage output",
            "resource": f"image:{imported['id']}",
            "stage": "inpaint",
            "mismatches": mismatches,
        }
    }
    unchanged = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert unchanged["revision"] == current["revision"]
    assert unchanged["status"] == current["status"]
    assert unchanged["stageReviews"] == current["stageReviews"]
    assert client.get(f"/api/projects/{project['id']}").json()["revision"] == project_before
    with store.session() as session:
        assert session.query(Revision).count() == audit_before


def test_visual_stage_review_observed_checksum_field_rules(
    app, client: TestClient, tmp_path: Path
) -> None:
    project_root = tmp_path / "observed-field-rules"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    data = png_bytes()
    for directory in ("preprocessed", "inpainted", "masks", "typeset"):
        target = project_root / "generated" / directory / "第一章/ページ一.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {
            **image.status,
            "preprocess": "done",
            "inpaint": "done",
            "typeset": "done",
        }
    current = client.get(f"/api/projects/{project['id']}/images").json()[0]
    endpoint = f"/api/images/{imported['id']}/stage-reviews"
    checksum = _checksum(data)

    invalid_requests = (
        ("inpaint", {"state": "accepted", "expectedRevision": current["revision"]}, 422),
        ("inpaint", {"state": "rejected", "expectedRevision": current["revision"]}, 422),
        (
            "inpaint",
            {
                "state": "pending",
                "expectedRevision": current["revision"],
                "observedArtifactChecksum": checksum,
            },
            422,
        ),
        (
            "inpaint",
            {
                "state": "accepted",
                "expectedRevision": current["revision"],
                "observedArtifactChecksum": "A" * 64,
                "observedMaskChecksum": checksum,
            },
            422,
        ),
        (
            "inpaint",
            {
                "state": "accepted",
                "expectedRevision": current["revision"],
                "observedArtifactChecksum": checksum,
            },
            400,
        ),
        (
            "preprocess",
            {
                "state": "accepted",
                "expectedRevision": current["revision"],
                "observedArtifactChecksum": checksum,
                "observedMaskChecksum": checksum,
            },
            400,
        ),
        (
            "typeset",
            {
                "state": "rejected",
                "expectedRevision": current["revision"],
                "observedArtifactChecksum": checksum,
                "observedMaskChecksum": checksum,
            },
            400,
        ),
    )
    for stage, body, expected_status in invalid_requests:
        response = client.patch(f"{endpoint}/{stage}", json=body)
        assert response.status_code == expected_status, response.text

    unchanged = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert unchanged["revision"] == current["revision"]
    assert unchanged["stageReviews"] == {}


def test_visual_stage_review_revision_conflict_precedes_observation_conflict(
    app, client: TestClient, tmp_path: Path
) -> None:
    project_root = tmp_path / "observed-revision-precedence"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    data = png_bytes()
    for directory in ("inpainted", "masks"):
        target = project_root / "generated" / directory / "第一章/ページ一.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {**image.status, "inpaint": "done"}
        image.revision += 1

    response = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": "accepted",
            "expectedRevision": imported["revision"],
            "observedArtifactChecksum": "0" * 64,
            "observedMaskChecksum": "0" * 64,
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["expectedRevision"] == imported["revision"]
    assert detail["actualRevision"] == imported["revision"] + 1
    assert "mismatches" not in detail


def test_rejecting_upstream_visual_stage_clears_dependent_reviews(
    app, client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "dependent-stage-review")
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    record = {
        "state": "accepted",
        "reviewedAt": "2026-08-13T10:00:00+00:00",
        "resultRevision": imported["revision"],
        "artifactChecksum": "a" * 64,
        "maskChecksum": "b" * 64,
    }
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {
            **image.status,
            "preprocess": "done",
            "inpaint": "done",
            "typeset": "done",
            "stageReviews": {
                "preprocess": record,
                "inpaint": record,
                "typeset": record,
            },
        }
    generated = tmp_path / "dependent-stage-review/generated"
    generated_bytes = png_bytes()
    for directory in ("preprocessed", "inpainted", "masks", "typeset"):
        target = generated / directory / "第一章/ページ一.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generated_bytes)

    current = client.get(f"/api/projects/{project['id']}/images").json()[0]
    rejected = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": "rejected",
            "expectedRevision": current["revision"],
            "observedArtifactChecksum": _checksum(generated_bytes),
            "observedMaskChecksum": _checksum(generated_bytes),
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert set(rejected.json()["stageReviews"]) == {"preprocess", "inpaint"}
    assert rejected.json()["stageReviews"]["inpaint"]["state"] == "rejected"

    reopened = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/preprocess",
        json={"state": "pending", "expectedRevision": rejected.json()["revision"]},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["stageReviews"] == {}


def test_accepting_changed_upstream_artifact_clears_dependent_review(
    app, client: TestClient, tmp_path: Path
) -> None:
    project_root = tmp_path / "changed-upstream-stage-review"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    inpaint = project_root / "generated/inpainted/第一章/ページ一.png"
    mask = project_root / "generated/masks/第一章/ページ一.png"
    typeset = project_root / "generated/typeset/第一章/ページ一.png"
    generated_bytes = png_bytes()
    for target in (inpaint, mask, typeset):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generated_bytes)
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {**image.status, "inpaint": "done", "typeset": "done"}

    current = client.get(f"/api/projects/{project['id']}/images").json()[0]
    current = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": "accepted",
            "expectedRevision": current["revision"],
            "observedArtifactChecksum": _checksum(generated_bytes),
            "observedMaskChecksum": _checksum(generated_bytes),
        },
    ).json()
    current = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/typeset",
        json={
            "state": "accepted",
            "expectedRevision": current["revision"],
            "observedArtifactChecksum": _checksum(generated_bytes),
        },
    ).json()
    assert set(current["stageReviews"]) == {"inpaint", "typeset"}

    changed_bytes = png_bytes(color="red")
    inpaint.write_bytes(changed_bytes)
    changed = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/inpaint",
        json={
            "state": "accepted",
            "expectedRevision": current["revision"],
            "observedArtifactChecksum": _checksum(changed_bytes),
            "observedMaskChecksum": _checksum(generated_bytes),
        },
    )
    assert changed.status_code == 200, changed.text
    assert set(changed.json()["stageReviews"]) == {"inpaint"}


def test_visual_stage_review_requires_completed_stage(client: TestClient, tmp_path: Path) -> None:
    project = create_project(client, tmp_path / "unfinished-stage-review")
    image = upload_image(client, project["id"])
    response = client.patch(
        f"/api/images/{image['id']}/stage-reviews/typeset",
        json={
            "state": "accepted",
            "expectedRevision": image["revision"],
            "observedArtifactChecksum": "a" * 64,
        },
    )
    assert response.status_code == 400
    assert "until that stage is done" in response.text


def test_pipeline_invalidation_clears_only_dependent_visual_reviews(
    app, client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "stage-invalidation-project")
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    record = {
        "state": "accepted",
        "reviewedAt": "2026-08-13T10:00:00+00:00",
        "resultRevision": imported["revision"],
        "artifactChecksum": "a" * 64,
        "maskChecksum": "b" * 64,
    }
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {
            **image.status,
            "stageReviews": {
                "preprocess": record,
                "inpaint": record,
                "typeset": record,
            },
        }
        invalidate_image_pipeline(store, image, {"typeset", "export"})
        assert set(stage_reviews(image)) == {"preprocess", "inpaint"}
        invalidate_image_pipeline(store, image, {"inpaint", "typeset", "export"})
        assert set(stage_reviews(image)) == {"preprocess"}


def test_openai_session_key_enables_provider_without_persisting_or_returning_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MANGA_LOCALIZER_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from manga_localizer.config import Settings

    data_dir = tmp_path / "session-catalog"
    app = create_app(Settings(data_dir=data_dir), start_worker=False)
    secret = "sk-session-test-value"

    with TestClient(app) as session_client:
        before = session_client.get("/api/config")
        assert before.status_code == 200
        before_openai = before.json()["providers"]["translation"]["openai-compatible"]
        assert before_openai["available"] is False
        assert before_openai["configurable"] is True

        rejected_secret = "session-rejected-credential-value"
        rejected = session_client.put(
            "/api/config/translation/openai-session",
            json={
                "apiKey": rejected_secret,
                "baseUrl": "https://user:embedded-secret@translator.example/v1",
                "model": "test-model",
            },
        )
        assert rejected.status_code == 400
        assert rejected_secret not in rejected.text
        assert "embedded-secret" not in rejected.text
        still_healthy = session_client.get("/api/config")
        assert still_healthy.status_code == 200
        assert (
            still_healthy.json()["providers"]["translation"]["openai-compatible"]["available"]
            is False
        )

        configured = session_client.put(
            "/api/config/translation/openai-session",
            json={
                "apiKey": secret,
                "baseUrl": "https://translator.example/v1",
                "model": "test-model",
            },
        )
        assert configured.status_code == 200
        configured_text = configured.text
        configured_openai = configured.json()["providers"]["translation"]["openai-compatible"]
        assert configured_openai["available"] is True
        assert configured_openai["configurable"] is True
        assert secret not in configured_text

        fetched_text = session_client.get("/api/config").text
        assert secret not in fetched_text

    persisted = "\n".join(
        path.read_text("utf-8", errors="ignore") for path in data_dir.rglob("*") if path.is_file()
    )
    assert secret not in persisted
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_runtime_environment_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "127.0.0.2")
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("OCR_LANGUAGES", "jpn,jpn_vert")
    monkeypatch.setenv("MANGA_LOCALIZER_TESSERACT_CMD", "custom-tesseract")
    monkeypatch.setenv("MANGA_LOCALIZER_OPENAI_MODEL", "")
    from manga_localizer.config import Settings

    settings = Settings()
    assert settings.host == "127.0.0.2"
    assert settings.port == 8123
    assert settings.log_level == "debug"
    assert settings.ocr_language_list == ["jpn", "jpn_vert"]
    assert settings.tesseract_command == "custom-tesseract"
    assert settings.openai_model == "gpt-4.1-mini"

    monkeypatch.setenv("MANGA_LOCALIZER_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        Settings()


def test_health_config_and_sanitized_portable_project(client: TestClient, tmp_path: Path) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    preflight = client.options(
        "/api/config/translation/openai-session",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert preflight.status_code == 200
    assert "PUT" in preflight.headers["access-control-allow-methods"]

    config = client.get("/api/config")
    assert config.status_code == 200
    capabilities = config.json()["capabilities"]
    tesseract = capabilities["ocr"]["tesseract"]
    assert tesseract["directions"]["horizontal"] == ("jpn" in tesseract["languages"])
    assert tesseract["directions"]["vertical"] == ("jpn_vert" in tesseract["languages"])
    assert isinstance(capabilities["fonts"]["available"], bool)

    root = tmp_path / "portable"
    response = client.post(
        "/api/projects",
        json={
            "name": "Unicode 漫画",
            "outputPath": str(root),
            "settings": {
                "theme": "dark",
                "apiKey": "must-not-persist",
                "api-key": "hyphenated-secret",
                "Authorization": "Bearer response-secret",
                "nested": {
                    "accessToken": "also-secret",
                    "serviceCredential": "credential-secret",
                    "language": "jpn",
                },
            },
        },
    )
    assert response.status_code == 201
    project = response.json()
    assert project["outputRoot"] == str(root.resolve())
    assert project["inputRoot"] is None
    assert project["schemaVersion"] == 1
    assert project["settings"]["translatorProvider"] == "manual"
    assert project["settings"]["detectorProvider"] == "tesseract"
    assert project["settings"]["ocrProvider"] == "tesseract"
    assert project["settings"]["inpainterProvider"] == "opencv"
    assert project["settings"]["sourceLanguage"] == "ja"
    assert project["settings"]["targetLanguage"] == "zh-CN"
    assert project["settings"]["theme"] == "dark"
    assert project["settings"]["nested"] == {"language": "jpn"}
    assert (root / "project/project.sqlite3").is_file()
    manifest = json.loads((root / "project/project.json").read_text("utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "must-not-persist" not in serialized
    assert "also-secret" not in serialized
    assert "hyphenated-secret" not in serialized
    assert "response-secret" not in serialized
    assert "credential-secret" not in serialized
    assert str(root) not in serialized
    assert manifest["project"]["id"] == project["id"]
    with sqlite3.connect(root / "project/project.sqlite3") as database:
        stored_settings = database.execute("SELECT settings FROM projects").fetchone()[0]
    assert "hyphenated-secret" not in stored_settings
    assert "response-secret" not in stored_settings
    assert "credential-secret" not in stored_settings


def test_open_portable_project_with_a_fresh_catalog(
    client: TestClient, tmp_path: Path, settings
) -> None:
    root = tmp_path / "portable"
    project = create_project(client, root)
    manifest = root / "project/project.json"

    from manga_localizer.config import Settings

    fresh_settings = Settings(data_dir=tmp_path / "other-catalog")
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        opened = fresh.post("/api/projects/open", json={"manifestPath": str(manifest)})
        assert opened.status_code == 200, opened.text
        assert opened.json()["id"] == project["id"]
        assert fresh.get("/api/projects").json()[0]["id"] == project["id"]


def test_open_scrubs_legacy_secrets_from_response_history_and_database(tmp_path: Path) -> None:
    from manga_localizer.config import Settings

    root = tmp_path / "legacy"
    setup_app = create_app(Settings(data_dir=tmp_path / "setup-catalog"), start_worker=False)
    with TestClient(setup_app) as setup_client:
        project = create_project(setup_client, root)
        upload_image(setup_client, project["id"])
    for store in setup_app.state.registry.stores():
        store.engine.dispose()

    secret = "s" + "k-legacy-should-never-leak"
    with sqlite3.connect(root / "project/project.sqlite3") as database:
        database.execute(
            "UPDATE projects SET settings = ?",
            (
                json.dumps(
                    {
                        "apiKey": secret,
                        "targetLanguage": "zh-CN",
                        "remoteEndpoint": f"https://user:{secret}@translator.example/v1",
                    }
                ),
            ),
        )
        database.execute(
            "UPDATE revisions SET before = ?, after = ?",
            (
                json.dumps(
                    {
                        "apiKey": secret,
                        "note": secret,
                        "settings": {
                            "remoteEndpoint": f"https://translator.example/v1?token={secret}"
                        },
                    }
                ),
                json.dumps(
                    {
                        "nested": {"accessToken": secret},
                        "note": secret,
                        "baseUrl": f"https://user:{secret}@translator.example/v1",
                    }
                ),
            ),
        )

    fresh_app = create_app(Settings(data_dir=tmp_path / "fresh-catalog"), start_worker=False)
    with TestClient(fresh_app) as fresh:
        opened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert opened.status_code == 200, opened.text
        revisions = fresh.get(f"/api/projects/{project['id']}/revisions")
        assert revisions.status_code == 200
        assert secret not in opened.text
        assert secret not in revisions.text
        assert "remoteEndpoint" not in opened.json()["settings"]

    with sqlite3.connect(root / "project/project.sqlite3") as database:
        persisted = "\n".join(
            str(value)
            for row in database.execute(
                "SELECT settings FROM projects UNION ALL SELECT before FROM revisions "
                "UNION ALL SELECT after FROM revisions"
            ).fetchall()
            for value in row
        )
    assert secret not in persisted


@pytest.mark.parametrize(
    "unsafe_endpoint",
    (
        "https://user:embedded-secret@translator.example/v1",
        "https://translator.example/v1?token=embedded-secret",
        "https://translator.example/v1#embedded-secret",
        "http://translator.example/v1",
    ),
)
def test_project_settings_reject_unsafe_remote_endpoints_without_persisting_them(
    client: TestClient,
    tmp_path: Path,
    unsafe_endpoint: str,
) -> None:
    rejected_root = tmp_path / "rejected"
    rejected = client.post(
        "/api/projects",
        json={
            "name": "拒绝不安全端点",
            "outputPath": str(rejected_root),
            "settings": {"remoteEndpoint": unsafe_endpoint},
        },
    )
    assert rejected.status_code == 400
    assert "embedded-secret" not in rejected.text
    assert not rejected_root.exists()

    project_root = tmp_path / "project"
    project = create_project(client, project_root)
    changed = client.patch(
        f"/api/projects/{project['id']}",
        json={
            "settings": {"remoteEndpoint": unsafe_endpoint},
            "expectedRevision": project["revision"],
        },
    )
    assert changed.status_code == 400
    assert "embedded-secret" not in changed.text
    current = client.get(f"/api/projects/{project['id']}").json()
    assert "remoteEndpoint" not in current["settings"]
    persisted = (project_root / "project/project.json").read_text("utf-8")
    with sqlite3.connect(project_root / "project/project.sqlite3") as database:
        persisted += database.execute("SELECT settings FROM projects").fetchone()[0]
    assert "embedded-secret" not in persisted


def test_unicode_nested_upload_content_thumbnail_and_duplicate_rename(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image_data = png_bytes((180, 220), rectangle=(20, 20, 80, 80))
    image = upload_image(client, project["id"], data=image_data)
    assert image["relativePath"] == "第一章/ページ一.png"
    assert image["width"] == 180
    assert image["height"] == 220

    content = client.get(image["contentUrl"])
    assert content.status_code == 200
    assert hashlib.sha256(content.content).hexdigest() == hashlib.sha256(image_data).hexdigest()
    thumbnail = client.get(image["thumbnailUrl"])
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/jpeg")

    duplicate = upload_image(client, project["id"], data=image_data)
    assert duplicate["relativePath"] == "第一章/ページ一-2.png"
    assert (tmp_path / "project/source/第一章/ページ一.png").read_bytes() == image_data


def test_upload_renames_unicode_and_case_collisions_for_portable_projects(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    first = upload_image(client, project["id"], relative_path="章/\uff21.png")
    second = upload_image(client, project["id"], relative_path="章/a.png")
    composed = upload_image(client, project["id"], relative_path="章/é.png")
    decomposed = upload_image(client, project["id"], relative_path="章/é.png")

    relative_paths = [
        first["relativePath"],
        second["relativePath"],
        composed["relativePath"],
        decomposed["relativePath"],
    ]
    assert relative_paths == ["章/\uff21.png", "章/a-2.png", "章/é.png", "章/é-2.png"]
    assert len({portable_path_key(path) for path in relative_paths}) == 4


@pytest.mark.parametrize(
    "unsafe",
    [
        "CON.png",
        "chapter/aux.jpg",
        "chapter/trailing.",
        "chapter/trailing.png ",
        "chapter/a:b.png",
        "chapter/question?.png",
        "chapter/control\x1f.png",
        "chapter//empty.png",
    ],
)
def test_portable_path_rules_reject_cross_platform_invalid_names(unsafe: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_relative_path(unsafe)
    assert safe_relative_path("日本語 漫画/第一章/页.png").as_posix() == "日本語 漫画/第一章/页.png"


def test_browser_folder_upload_strips_only_the_selected_root_directory(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    response = client.post(
        f"/api/projects/{project['id']}/images/upload",
        files=[("files", ("page.png", png_bytes(), "image/png"))],
        data={
            "relativePaths": json.dumps(["input 漫画/第一章/ページ一.png"]),
            "stripCommonRoot": "true",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()[0]["relativePath"] == "第一章/ページ一.png"
    assert (tmp_path / "project/source/第一章/ページ一.png").is_file()


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape.png",
        "chapter/../../escape.png",
        "/tmp/absolute.png",
        "C:\\drive\\escape.png",
        "\\\\server\\share\\escape.png",
        "nul\x00name.png",
    ],
)
def test_browser_upload_rejects_unsafe_paths_before_write(
    client: TestClient, tmp_path: Path, unsafe: str
) -> None:
    project_root = tmp_path / "project"
    project = create_project(client, project_root)
    response = client.post(
        f"/api/projects/{project['id']}/images/upload",
        files=[("files", ("page.png", png_bytes(), "image/png"))],
        data={"relativePaths": json.dumps([unsafe])},
    )
    assert response.status_code == 400
    assert list((project_root / "source").rglob("*")) == []


def test_path_guard_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        resolve_within(root, "link/page.png")
    assert safe_relative_path("日本語/章/页.png").as_posix() == "日本語/章/页.png"


def test_project_creation_rejects_reserved_directory_symlink(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "project-root"
    outside = tmp_path / "outside-project"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "project").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform")

    response = client.post(
        "/api/projects",
        json={"name": "拒绝符号链接", "outputPath": str(root)},
    )
    assert response.status_code == 400
    assert "symlink" in response.text
    assert list(outside.iterdir()) == []


def test_manifest_symlink_cannot_redirect_project_save_into_source(
    client: TestClient, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    project = create_project(client, project_root)
    image_data = png_bytes(color="ivory")
    upload_image(client, project["id"], data=image_data)
    source = project_root / "source/第一章/ページ一.png"
    manifest = project_root / "project/project.json"
    manifest.unlink()
    try:
        manifest.symlink_to(source)
    except OSError:
        pytest.skip("File symlinks are unavailable on this platform")

    response = client.patch(
        f"/api/projects/{project['id']}",
        json={"settings": {"targetLanguage": "zh-TW"}},
    )

    assert response.status_code == 400
    assert "symlink" in response.text
    assert source.read_bytes() == image_data


def test_generated_read_and_thumbnail_temp_symlinks_cannot_escape_project(
    client: TestClient,
    tmp_path: Path,
    app,
) -> None:
    project_root = tmp_path / "project"
    project = create_project(client, project_root)
    source_data = png_bytes(color="ivory")
    image = upload_image(client, project["id"], data=source_data)
    source = project_root / "source/第一章/ページ一.png"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    thumbnail_temp = project_root / f"project/cache/thumbnails/{image['id']}.tmp"
    thumbnail_temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        thumbnail_temp.symlink_to(source)
    except OSError:
        pytest.skip("File symlinks are unavailable on this platform")
    thumbnail = client.get(f"/api/images/{image['id']}/thumbnail")
    assert thumbnail.status_code == 200
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash

    outside = tmp_path / "outside"
    outside_relative = outside / "第一章/ページ一.png"
    outside_relative.parent.mkdir(parents=True)
    outside_relative.write_bytes(png_bytes(color="red"))
    generated_typeset = project_root / "generated/typeset"
    generated_typeset.symlink_to(outside, target_is_directory=True)
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        stored = session.get(ImageAsset, image["id"])
        assert stored is not None
        stored.status = {**dict(stored.status), "typeset": "done"}

    escaped = client.get(f"/api/images/{image['id']}/content?variant=typeset")
    assert escaped.status_code == 400
    assert escaped.content != outside_relative.read_bytes()


def test_open_rejects_database_symlink_without_mutating_the_other_project(
    tmp_path: Path,
) -> None:
    from manga_localizer.config import Settings

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    app = create_app(Settings(data_dir=tmp_path / "catalog"), start_worker=False)
    with TestClient(app) as setup_client:
        create_project(setup_client, first_root, "first")
        create_project(setup_client, second_root, "second")
    for store in app.state.registry.stores():
        store.engine.dispose()

    first_database = first_root / "project/project.sqlite3"
    second_database = second_root / "project/project.sqlite3"
    first_database.unlink()
    try:
        first_database.symlink_to(second_database)
    except OSError:
        pytest.skip("File symlinks are unavailable on this platform")
    before = hashlib.sha256(second_database.read_bytes()).hexdigest()

    fresh = create_app(Settings(data_dir=tmp_path / "fresh-catalog"), start_worker=False)
    with TestClient(fresh) as fresh_client:
        response = fresh_client.post(
            "/api/projects/open",
            json={"manifestPath": str(first_root / "project/project.json")},
        )

    assert response.status_code == 400
    assert "symlink" in response.text
    assert hashlib.sha256(second_database.read_bytes()).hexdigest() == before


def test_trusted_local_directory_import_preserves_tree_and_source(
    client: TestClient, tmp_path: Path
) -> None:
    incoming = tmp_path / "原稿"
    nested = incoming / "第二巻/章一"
    nested.mkdir(parents=True)
    original = png_bytes((90, 110), color="ivory")
    source = nested / "ページ.png"
    source.write_bytes(original)
    (incoming / "notes.txt").write_text("not an image", "utf-8")
    project = create_project(client, tmp_path / "project")
    response = client.post(
        f"/api/projects/{project['id']}/images/import-local",
        json={"paths": [str(incoming)]},
    )
    assert response.status_code == 201, response.text
    imported = response.json()
    assert len(imported) == 1
    assert imported[0]["relativePath"] == "第二巻/章一/ページ.png"
    assert imported[0]["sourceKind"] == "trusted-local-import"
    assert response.headers["X-Manga-Localizer-Import-Failures"] == "1"
    assert source.read_bytes() == original
    assert (tmp_path / "project/source/第二巻/章一/ページ.png").read_bytes() == original
    opened = client.get(f"/api/projects/{project['id']}").json()
    assert opened["inputRoot"] == str(incoming.resolve())


def test_trusted_import_across_drives_uses_exact_boundaries_without_a_common_root(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "drive-a/one.png"
    second = tmp_path / "drive-b/two.png"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(png_bytes(color="ivory"))
    second.write_bytes(png_bytes(color="lavender"))
    project = create_project(client, tmp_path / "project")

    def no_common_drive(_paths) -> str:
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr("manga_localizer.services.images.os.path.commonpath", no_common_drive)
    imported = client.post(
        f"/api/projects/{project['id']}/images/import-local",
        json={"paths": [str(first), str(second)]},
    )

    assert imported.status_code == 201, imported.text
    assert len(imported.json()) == 2
    assert client.get(f"/api/projects/{project['id']}").json()["inputRoot"] is None
    with sqlite3.connect(tmp_path / "project/project/project.sqlite3") as database:
        assert database.execute("SELECT count(*) FROM import_boundaries").fetchone()[0] == 2
