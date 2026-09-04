from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manga_localizer.database import ImageAsset, Revision, TextRegion
from manga_localizer.main import create_app
from manga_localizer.security import (
    UnsafePathError,
    portable_path_key,
    resolve_within,
    safe_relative_path,
)
from manga_localizer.services.images import invalidate_image_pipeline, stage_reviews
from manga_localizer.services.trust import with_detection_evidence, with_ocr_evidence

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
    assert served_artifact.headers.get("cache-control") == "private, no-store"
    assert served_mask.headers.get("cache-control") == "private, no-store"
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


def test_generated_images_forbid_http_caching(app, client: TestClient, tmp_path: Path) -> None:
    project_root = tmp_path / "generated-no-cache"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    store, _ = app.state.registry.find_image(imported["id"])
    for directory in ("preprocessed", "inpainted", "typeset", "masks"):
        target = project_root / "generated" / directory / "第一章/ページ一.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png_bytes(color="red"))
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {
            **image.status,
            "preprocess": "done",
            "inpaint": "done",
            "typeset": "done",
        }

    original = client.get(f"/api/images/{imported['id']}/content")
    thumbnail = client.get(f"/api/images/{imported['id']}/thumbnail")
    assert original.status_code == 200
    assert thumbnail.status_code == 200
    assert original.headers.get("cache-control") != "private, no-store"
    assert thumbnail.headers.get("cache-control") != "private, no-store"

    generated_paths = (
        f"/api/images/{imported['id']}/generated/preprocessed",
        f"/api/images/{imported['id']}/generated/inpainted",
        f"/api/images/{imported['id']}/generated/typeset",
        f"/api/images/{imported['id']}/generated/mask",
        f"/api/images/{imported['id']}/content?variant=preprocessed",
        f"/api/images/{imported['id']}/content?variant=erased",
        f"/api/images/{imported['id']}/content?variant=typeset",
    )
    for path in generated_paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "private, no-store", path


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


def test_accepting_unchanged_upstream_review_keeps_dependent_reviews(
    app, client: TestClient, tmp_path: Path
) -> None:
    project_root = tmp_path / "unchanged-upstream-stage-review"
    project = create_project(client, project_root)
    imported = upload_image(client, project["id"])
    preprocess = project_root / "generated/preprocessed/第一章/ページ一.png"
    inpaint = project_root / "generated/inpainted/第一章/ページ一.png"
    mask = project_root / "generated/masks/第一章/ページ一.png"
    typeset = project_root / "generated/typeset/第一章/ページ一.png"
    generated_bytes = png_bytes()
    for target in (preprocess, inpaint, mask, typeset):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generated_bytes)
    store, _ = app.state.registry.find_image(imported["id"])
    with store.session() as session:
        image = session.get(ImageAsset, imported["id"])
        assert image is not None
        image.status = {
            **image.status,
            "preprocess": "done",
            "inpaint": "done",
            "typeset": "done",
            "stageReviews": {
                "preprocess": {
                    "state": "accepted",
                    "reviewedAt": "2026-01-01T00:00:00+00:00",
                    "resultRevision": image.revision,
                    "artifactChecksum": _checksum(generated_bytes),
                }
            },
        }

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
    assert set(current["stageReviews"]) == {"preprocess", "inpaint", "typeset"}

    accepted = client.patch(
        f"/api/images/{imported['id']}/stage-reviews/preprocess",
        json={
            "state": "accepted",
            "expectedRevision": current["revision"],
            "observedArtifactChecksum": _checksum(generated_bytes),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert set(accepted.json()["stageReviews"]) == {"preprocess", "inpaint", "typeset"}


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


def test_runtime_environment_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MANGA_LOCALIZER_DATA_DIR", str(tmp_path / "catalog"))
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


def test_runtime_data_dir_has_no_implicit_internal_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from manga_localizer.config import Settings

    monkeypatch.delenv("MANGA_LOCALIZER_DATA_DIR")
    with pytest.raises(ValidationError, match="data_dir"):
        Settings()


def test_lan_access_binds_private_ipv4_only(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from manga_localizer.config import Settings

    data_dir = tmp_path / "catalog"
    with pytest.raises(ValidationError, match="loopback"):
        Settings(data_dir=data_dir, host="192.168.1.20")
    settings = Settings(data_dir=data_dir, host="192.168.1.20", lan_access=True)
    assert settings.host == "192.168.1.20"
    with pytest.raises(ValidationError, match="loopback"):
        Settings(data_dir=data_dir, host="0.0.0.0", lan_access=True)
    with pytest.raises(ValidationError, match="loopback"):
        Settings(data_dir=data_dir, host="8.8.8.8", lan_access=True)
    with pytest.raises(ValidationError, match="loopback"):
        Settings(data_dir=data_dir, host="169.254.1.1", lan_access=True)


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
    providers = config.json()["providers"]
    assert providers["preprocessing"]["opencv-pillow"]["classicInterpolation"] is True
    assert providers["preprocessing"]["realesrgan-onnx"]["aiUpscale"] is True
    assert providers["preprocessing"]["realesrgan-onnx"]["downloadsModelsAtStartup"] is False
    assert providers["preprocessing"]["realesrgan-ncnn"]["aiUpscale"] is True
    union = providers["detection"]["ppocr-v3+tesseract"]
    assert union["mergesOverlaps"] is True
    assert union["dropsLowConfidence"] is False
    assert union["keepsAllProposals"] is False
    assert union["expandsBoxes"] is True
    assert union["tesseractContourFallback"] is False
    argos = providers["translation"]["argos-ja-zh"]
    assert argos["remote"] is False
    assert argos["downloadsModelsAtStartup"] is False
    assert argos["sendsImages"] is False
    assert argos["available"] is False

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
    assert project["schemaVersion"] == 2
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


def test_open_migrates_legacy_region_recognition_and_preserves_human_confirmation(
    client: TestClient, app, tmp_path: Path
) -> None:
    root = tmp_path / "legacy-recognition"
    project = create_project(client, root)
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "sourceText": "人工确认",
            "confirmed": True,
        },
    )
    assert created.status_code == 201, created.text
    region_id = created.json()["id"]
    unconfirmed = client.post(
        f"/api/images/{image['id']}/regions",
        json={"x": 50, "y": 10, "width": 30, "height": 30, "sourceText": "未确认"},
    )
    assert unconfirmed.status_code == 201, unconfirmed.text
    unconfirmed_id = unconfirmed.json()["id"]
    project_root = root
    generated_bytes = png_bytes()
    generated_paths = {
        directory: project_root / "generated" / directory / "第一章/ページ一.png"
        for directory in ("preprocessed", "inpainted", "masks", "typeset")
    }
    for path in generated_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(generated_bytes)
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        asset = session.get(ImageAsset, image["id"])
        assert asset is not None
        asset.status = {
            **asset.status,
            "preprocess": "done",
            "detection": "done",
            "ocr": "done",
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
            "reviewState": "reviewed",
            "reviewedAt": "2026-08-13T10:00:00+00:00",
            "stageReviews": {
                "inpaint": {
                    "state": "accepted",
                    "reviewedAt": "2026-08-13T10:00:00+00:00",
                    "resultRevision": asset.revision,
                    "artifactChecksum": _checksum(generated_bytes),
                    "maskChecksum": _checksum(generated_bytes),
                },
                "typeset": {
                    "state": "accepted",
                    "reviewedAt": "2026-08-13T10:00:00+00:00",
                    "resultRevision": asset.revision,
                    "artifactChecksum": _checksum(generated_bytes),
                },
            },
        }
    for store in app.state.registry.stores():
        store.engine.dispose()
    database_path = root / "project/project.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.execute("ALTER TABLE text_regions DROP COLUMN recognition")
        database.execute("ALTER TABLE images DROP COLUMN inpaint_provenance")
        database.execute("ALTER TABLE images DROP COLUMN inpaint_classical_approval")
        database.execute("ALTER TABLE images DROP COLUMN inpaint_ai_candidate_reviews")
        database.execute("UPDATE projects SET schema_version = 1")

    fresh_settings = app.state.settings.model_copy(
        update={"data_dir": tmp_path / "legacy-recognition-catalog"}
    )
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        reopened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["schemaVersion"] == 2
        regions = fresh.get(f"/api/images/{image['id']}/regions")
        assert regions.status_code == 200, regions.text
        migrated = next(value for value in regions.json() if value["id"] == region_id)
        assert migrated["trustDisposition"] == "trusted"
        assert migrated["trustReason"] == "legacy-confirmed"
        assert migrated["recognition"]["version"] == 1
        legacy_review = next(value for value in regions.json() if value["id"] == unconfirmed_id)
        assert legacy_review["trustDisposition"] == "review"
        assert legacy_review["trustReason"] == "legacy-unverified"
        migrated_image = fresh.get(f"/api/projects/{project['id']}/images").json()[0]
        assert migrated_image["status"]["preprocess"] == "done"
        assert migrated_image["status"]["detection"] == "done"
        assert migrated_image["status"]["ocr"] == "done"
        for stage in ("translation", "inpaint", "typeset", "export"):
            assert migrated_image["status"][stage] == "pending"
        assert migrated_image["status"]["reviewState"] == "pending"
        assert migrated_image["stageReviews"] == {}
        first_migrated_revision = migrated_image["revision"]
        assert generated_paths["preprocessed"].is_file()
        for directory in ("inpainted", "masks", "typeset"):
            assert not generated_paths[directory].exists()

    with TestClient(create_app(fresh_settings, start_worker=False)) as second_fresh:
        reopened_again = second_fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert reopened_again.status_code == 200, reopened_again.text
        idempotent_image = second_fresh.get(f"/api/projects/{project['id']}/images").json()[0]
        assert idempotent_image["revision"] == first_migrated_revision

    with sqlite3.connect(database_path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(text_regions)")}
        assert "recognition" in columns
        image_columns = {row[1] for row in database.execute("PRAGMA table_info(images)")}
        assert "inpaint_provenance" in image_columns
        assert "inpaint_classical_approval" in image_columns
        assert "inpaint_ai_candidate_reviews" in image_columns
        assert database.execute("SELECT schema_version FROM projects").fetchone()[0] == 2
        stored_recognition = json.loads(
            database.execute(
                "SELECT recognition FROM text_regions WHERE id = ?", (region_id,)
            ).fetchone()[0]
        )
        assert stored_recognition["trust"] == {
            "policyVersion": 1,
            "disposition": "trusted",
            "reason": "legacy-confirmed",
        }


def test_open_adds_nullable_g4_region_fields_without_fabricating_legacy_evidence(
    client: TestClient, app, tmp_path: Path
) -> None:
    root = tmp_path / "legacy-g4-fields"
    project = create_project(client, root)
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={"x": 10, "y": 10, "width": 30, "height": 30, "sourceText": "旧记录"},
    )
    assert created.status_code == 201, created.text
    region_id = created.json()["id"]
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        project_row = store.project(session)
        image_row = session.get(ImageAsset, image["id"])
        assert image_row is not None
        preimage = {
            "projectRevision": project_row.revision,
            "imageRevision": image_row.revision,
            "revisionCount": session.query(Revision).count(),
        }
    store.engine.dispose()
    database_path = root / "project/project.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA foreign_keys=OFF")
        database.executescript(
            """
            -- This test starts from a current database and removes all G4/G6
            -- region schema objects to reproduce a genuinely pre-G4 project.
            -- Those later triggers and the attempt table would not exist in
            -- that legacy preimage.
            DROP TRIGGER IF EXISTS images_g4_delete_regions;
            DROP TRIGGER IF EXISTS text_regions_g6_validate_insert;
            DROP TRIGGER IF EXISTS text_regions_g6_validate_update;
            DROP TRIGGER IF EXISTS region_ocr_attempts_validate_insert;
            DROP TRIGGER IF EXISTS region_ocr_attempts_append_only_update;
            DROP TRIGGER IF EXISTS region_ocr_attempts_append_only_delete;
            DROP TABLE IF EXISTS region_ocr_attempts;
            CREATE TABLE text_regions_legacy (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                image_id VARCHAR(36) NOT NULL,
                x FLOAT NOT NULL,
                y FLOAT NOT NULL,
                width FLOAT NOT NULL,
                height FLOAT NOT NULL,
                rotation FLOAT NOT NULL,
                source_text TEXT NOT NULL,
                translation_text TEXT NOT NULL,
                region_type VARCHAR(50) NOT NULL,
                direction VARCHAR(20) NOT NULL,
                reading_order INTEGER NOT NULL,
                confidence FLOAT,
                ignored BOOLEAN NOT NULL,
                confirmed BOOLEAN NOT NULL,
                style JSON NOT NULL,
                repair JSON NOT NULL,
                recognition JSON NOT NULL DEFAULT '{}',
                ocr_provider VARCHAR(80),
                translation_provider VARCHAR(80),
                revision INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
            );
            INSERT INTO text_regions_legacy (
                id, image_id, x, y, width, height, rotation, source_text,
                translation_text, region_type, direction, reading_order,
                confidence, ignored, confirmed, style, repair, recognition,
                ocr_provider, translation_provider, revision, created_at, updated_at
            )
            SELECT
                id, image_id, x, y, width, height, rotation, source_text,
                translation_text, region_type, direction, reading_order,
                confidence, ignored, confirmed, style, repair, recognition,
                ocr_provider, translation_provider, revision, created_at, updated_at
            FROM text_regions;
            DROP TABLE text_regions;
            ALTER TABLE text_regions_legacy RENAME TO text_regions;
            """
        )
        database.commit()

    fresh_settings = app.state.settings.model_copy(
        update={"data_dir": tmp_path / "legacy-g4-catalog"}
    )
    for _attempt in range(2):
        with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
            reopened = fresh.post(
                "/api/projects/open",
                json={"manifestPath": str(root / "project/project.json")},
            )
            assert reopened.status_code == 200, reopened.text
            migrated = fresh.get(f"/api/images/{image['id']}/regions").json()[0]
            assert migrated["id"] == region_id
            assert migrated["paragraphGroupId"] is None
            assert migrated["rubyParentId"] is None
            assert migrated["contentDisposition"] is None
            assert migrated["detectorJobItemId"] is None
            assert migrated["detectorCandidateIndex"] is None
            assert migrated["backgroundCategory"] is None
            assert migrated["backgroundConfidence"] is None
            assert migrated["backgroundRationaleCodes"] is None
            assert migrated["backgroundReviewer"] is None
            assert migrated["backgroundGenerationId"] is None
            reopened_store = fresh.app.state.registry.get(project["id"])
            with reopened_store.session() as session:
                project_row = reopened_store.project(session)
                image_row = session.get(ImageAsset, image["id"])
                assert image_row is not None
                assert project_row.revision == preimage["projectRevision"]
                assert image_row.revision == preimage["imageRevision"]
                assert session.query(Revision).count() == preimage["revisionCount"]

    with sqlite3.connect(database_path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(text_regions)")}
        assert {
            "paragraph_group_id",
            "ruby_parent_id",
            "region_disposition",
            "detector_job_item_id",
            "detector_candidate_index",
            "background_category",
            "background_confidence",
            "background_rationale_codes",
            "background_reviewer",
            "background_generation_id",
        } <= columns
        indexes = {row[1] for row in database.execute("PRAGMA index_list(text_regions)")}
        assert "uq_text_region_detector_candidate" in indexes
        triggers = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'text_regions'"
            )
        }
        assert {
            "text_regions_g4_validate_insert",
            "text_regions_g4_validate_update",
            "text_regions_g4_validate_parent_update",
            "text_regions_g4_restrict_parent_delete",
            "text_regions_g5_validate_insert",
            "text_regions_g5_validate_update",
        } <= triggers


def test_open_schema_two_empty_recognition_invalidates_derived_artifacts(
    client: TestClient, app, tmp_path: Path
) -> None:
    root = tmp_path / "schema-two-empty-recognition"
    project = create_project(client, root)
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "sourceText": "旧人工确认",
            "confirmed": True,
        },
    )
    assert created.status_code == 201, created.text
    generated_paths = {
        directory: root / "generated" / directory / "第一章/ページ一.png"
        for directory in ("inpainted", "masks", "typeset")
    }
    for path in generated_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes())
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        region = session.get(TextRegion, created.json()["id"])
        asset = session.get(ImageAsset, image["id"])
        assert region is not None and asset is not None
        region.recognition = {}
        asset.status = {
            **asset.status,
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
        }
    for opened_store in app.state.registry.stores():
        opened_store.engine.dispose()

    fresh_settings = app.state.settings.model_copy(
        update={"data_dir": tmp_path / "schema-two-empty-catalog"}
    )
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        reopened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert reopened.status_code == 200, reopened.text
        current = fresh.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["trustDisposition"] == "trusted"
        assert current["trustReason"] == "legacy-confirmed"
        state = fresh.get(f"/api/projects/{project['id']}/images").json()[0]
        for stage in ("translation", "inpaint", "typeset", "export"):
            assert state["status"][stage] == "pending"
        assert all(not path.exists() for path in generated_paths.values())


def test_open_persists_noncanonical_evidence_fail_closed_and_discards_old_artifacts(
    client: TestClient, app, tmp_path: Path
) -> None:
    root = tmp_path / "policy-migration-artifacts"
    project = create_project(client, root)
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "sourceText": "公開合成",
            "confirmed": True,
        },
    )
    assert created.status_code == 201, created.text
    generated_bytes = png_bytes()
    generated_paths = {
        directory: root / "generated" / directory / "第一章/ページ一.png"
        for directory in ("inpainted", "masks", "typeset")
    }
    for path in generated_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(generated_bytes)
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        region = session.get(TextRegion, created.json()["id"])
        asset = session.get(ImageAsset, image["id"])
        assert region is not None and asset is not None
        recognition = with_detection_evidence({}, 0.88, "generated-detector")
        recognition["detection"].pop("inputVariant")
        recognition["trust"] = {
            "policyVersion": 1,
            "disposition": "trusted",
            "reason": "human-confirmed",
        }
        region.recognition = recognition
        asset.status = {
            **asset.status,
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
            "reviewState": "reviewed",
            "reviewedAt": "2026-08-13T10:00:00+00:00",
            "stageReviews": {
                "inpaint": {
                    "state": "accepted",
                    "reviewedAt": "2026-08-13T10:00:00+00:00",
                    "resultRevision": asset.revision,
                    "artifactChecksum": _checksum(generated_bytes),
                    "maskChecksum": _checksum(generated_bytes),
                },
                "typeset": {
                    "state": "accepted",
                    "reviewedAt": "2026-08-13T10:00:00+00:00",
                    "resultRevision": asset.revision,
                    "artifactChecksum": _checksum(generated_bytes),
                },
            },
        }
    for opened_store in app.state.registry.stores():
        opened_store.engine.dispose()

    fresh_settings = app.state.settings.model_copy(
        update={"data_dir": tmp_path / "policy-migration-catalog"}
    )
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        reopened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert reopened.status_code == 200, reopened.text
        current_region = fresh.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current_region["detectorConfidence"] == 0.88
        assert current_region["trustDisposition"] == "review"
        assert current_region["trustReason"] == "policy-version-changed"
        current_image = fresh.get(f"/api/projects/{project['id']}/images").json()[0]
        for stage in ("translation", "inpaint", "typeset", "export"):
            assert current_image["status"][stage] == "pending"
        assert current_image["stageReviews"] == {}
        for path in generated_paths.values():
            assert not path.exists()

    with sqlite3.connect(root / "project/project.sqlite3") as database:
        stored = json.loads(
            database.execute(
                "SELECT recognition FROM text_regions WHERE id = ?",
                (created.json()["id"],),
            ).fetchone()[0]
        )
    assert stored["detection"] == {
        "confidence": 0.88,
        "provider": "generated-detector",
        "inputVariant": None,
        "language": None,
    }
    assert stored["trust"] == {
        "policyVersion": 1,
        "disposition": "review",
        "reason": "policy-version-changed",
    }


def test_open_preserves_current_recognition_evidence_and_human_trust(
    client: TestClient, app, tmp_path: Path
) -> None:
    root = tmp_path / "recognition-reopen"
    project = create_project(client, root)
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "sourceText": "公開合成テスト",
        },
    )
    assert created.status_code == 201, created.text
    region = created.json()
    recognition = with_detection_evidence({}, 0.37, "generated-detector")
    recognition = with_ocr_evidence(
        recognition,
        0.81,
        "generated-ocr",
        attempts=[
            {
                "provider": "generated-ocr",
                "inputVariant": "preprocessed",
                "confidence": 0.18,
                "direction": "vertical",
                "language": None,
            },
            {
                "provider": "generated-ocr",
                "inputVariant": "original",
                "confidence": 0.81,
                "direction": "vertical",
                "language": None,
            },
        ],
        selected_index=1,
    )
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        persisted = session.get(TextRegion, region["id"])
        assert persisted is not None
        persisted.recognition = recognition
    trusted = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert trusted.status_code == 200, trusted.text

    fresh_settings = app.state.settings.model_copy(
        update={"data_dir": tmp_path / "recognition-reopen-catalog"}
    )
    with TestClient(create_app(fresh_settings, start_worker=False)) as fresh:
        reopened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert reopened.status_code == 200, reopened.text
        current = fresh.get(f"/api/images/{image['id']}/regions").json()[0]
        assert current["detectorConfidence"] == 0.37
        assert current["ocrConfidence"] == 0.81
        assert current["trustDisposition"] == "trusted"
        assert current["trustReason"] == "human-confirmed"
        assert current["recognition"]["ocr"]["selectedIndex"] == 1
        assert current["recognition"]["ocr"]["attempts"] == [
            {
                "provider": "generated-ocr",
                "inputVariant": "preprocessed",
                "confidence": 0.18,
                "direction": "vertical",
                "language": None,
            },
            {
                "provider": "generated-ocr",
                "inputVariant": "original",
                "confidence": 0.81,
                "direction": "vertical",
                "language": None,
            },
        ]


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


def test_upload_projects_per_page_preprocess_suggestion(client: TestClient, tmp_path: Path) -> None:
    project = create_project(client, tmp_path / "project")
    small = upload_image(
        client,
        project["id"],
        data=png_bytes((400, 600), color="#8c8c8c"),
    )
    assert small["preprocessSuggestion"]["profile"] == "ocr-friendly"
    assert "small-page" in small["preprocessSuggestion"]["reasons"]
    assert small["preprocessSuggestion"]["metrics"]["sampled"] is True
    assert small["preprocessSuggestion"]["metrics"]["minSide"] == 400

    listed = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert listed["preprocessSuggestion"] == small["preprocessSuggestion"]
    snapshot = json.loads((tmp_path / "project/project/project.json").read_text("utf-8"))
    assert snapshot["images"][0]["status"]["preprocessSuggestion"]["profile"] == "ocr-friendly"


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


@pytest.mark.parametrize(
    ("setting_name", "setting_value", "affected_evidence", "unaffected_evidence"),
    (
        ("detectorProvider", "mock", "detection", "ocr"),
        ("ocrProvider", "mock", "ocr", "detection"),
        ("sourceLanguage", "en", "ocr", "detection"),
    ),
)
def test_recognition_provider_defaults_do_not_wipe_existing_pages(
    client: TestClient,
    app,
    tmp_path: Path,
    setting_name: str,
    setting_value: str,
    affected_evidence: str,
    unaffected_evidence: str,
) -> None:
    project = create_project(client, tmp_path / f"trust-settings-{setting_name}")
    image = upload_image(client, project["id"])

    def create_trusted(x: int) -> dict:
        response = client.post(
            f"/api/images/{image['id']}/regions",
            json={
                "x": x,
                "y": 10,
                "width": 30,
                "height": 30,
                "sourceText": "公開テスト",
                "confirmed": True,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    affected = create_trusted(10)
    unaffected = create_trusted(50)
    ignored = create_trusted(90)
    ignored_response = client.patch(
        f"/api/regions/{ignored['id']}",
        json={"ignored": True, "expectedRevision": ignored["revision"]},
    )
    assert ignored_response.status_code == 200, ignored_response.text

    def evidence_payload(recognition: dict, kind: str) -> dict:
        if kind == "detection":
            return with_detection_evidence(recognition, 0.81, "tesseract")
        return with_ocr_evidence(
            recognition,
            0.87,
            "tesseract",
            input_variant="original",
            direction="vertical",
            attempts=[
                {
                    "provider": "tesseract",
                    "inputVariant": "original",
                    "confidence": 0.87,
                    "direction": "vertical",
                }
            ],
            selected_index=0,
        )

    store = app.state.registry.get(project["id"])
    with store.session() as session:
        affected_row = session.get(TextRegion, affected["id"])
        unaffected_row = session.get(TextRegion, unaffected["id"])
        ignored_row = session.get(TextRegion, ignored["id"])
        asset = session.get(ImageAsset, image["id"])
        assert all(value is not None for value in (affected_row, unaffected_row, ignored_row))
        assert asset is not None
        for row, kind, disposition, reason in (
            (affected_row, affected_evidence, "trusted", "human-confirmed"),
            (unaffected_row, unaffected_evidence, "trusted", "human-confirmed"),
            (ignored_row, affected_evidence, "ignored", "human-ignored"),
        ):
            assert row is not None
            recognition = evidence_payload(row.recognition, kind)
            recognition["trust"] = {
                "policyVersion": 1,
                "disposition": disposition,
                "reason": reason,
            }
            row.recognition = recognition
        asset.status = {
            **asset.status,
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
        }

    current_project = client.get(f"/api/projects/{project['id']}").json()
    changed = client.patch(
        f"/api/projects/{project['id']}",
        json={
            "settings": {setting_name: setting_value},
            "expectedRevision": current_project["revision"],
        },
    )
    assert changed.status_code == 200, changed.text

    regions = {
        region["id"]: region for region in client.get(f"/api/images/{image['id']}/regions").json()
    }
    assert regions[affected["id"]]["confirmed"] is True
    assert regions[affected["id"]]["trustDisposition"] == "trusted"
    assert regions[affected["id"]]["trustReason"] == "human-confirmed"
    assert regions[unaffected["id"]]["confirmed"] is True
    assert regions[unaffected["id"]]["trustDisposition"] == "trusted"
    assert regions[unaffected["id"]]["trustReason"] == "human-confirmed"
    assert regions[ignored["id"]]["ignored"] is True
    assert regions[ignored["id"]]["trustDisposition"] == "ignored"
    assert regions[ignored["id"]]["trustReason"] == "human-ignored"
    status = client.get(f"/api/projects/{project['id']}/images").json()[0]["status"]
    for stage in ("translation", "inpaint", "typeset", "export"):
        assert status[stage] == "done"


def test_detector_default_change_does_not_pending_other_pages(
    client: TestClient,
    app,
    tmp_path: Path,
) -> None:
    project = create_project(client, tmp_path / "detector-default-keeps-pages")
    first = upload_image(client, project["id"])
    second = upload_image(client, project["id"])
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        other = session.get(ImageAsset, second["id"])
        assert other is not None
        other.status = {
            **other.status,
            "detection": "done",
            "ocr": "done",
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
        }

    current_project = client.get(f"/api/projects/{project['id']}").json()
    changed = client.patch(
        f"/api/projects/{project['id']}",
        json={
            "settings": {"detectorProvider": "ppocr-v3"},
            "expectedRevision": current_project["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    listed = client.get(f"/api/projects/{project['id']}/images").json()
    images = {item["id"]: item for item in listed}
    assert images[first["id"]]["status"]["detection"] != "failed"
    for stage in ("detection", "ocr", "translation", "inpaint", "typeset", "export"):
        assert images[second["id"]]["status"][stage] == "done"


def test_preprocessing_setting_revokes_only_trust_using_preprocessed_evidence(
    client: TestClient,
    app,
    tmp_path: Path,
) -> None:
    project = create_project(client, tmp_path / "preprocessing-trust-setting")
    image = upload_image(client, project["id"])
    regions = []
    for x in (10, 50):
        created = client.post(
            f"/api/images/{image['id']}/regions",
            json={
                "x": x,
                "y": 10,
                "width": 30,
                "height": 30,
                "sourceText": "公開テスト",
                "confirmed": True,
            },
        )
        assert created.status_code == 201, created.text
        regions.append(created.json())

    store = app.state.registry.get(project["id"])
    with store.session() as session:
        for region, input_variant in zip(regions, ("preprocessed", "original"), strict=True):
            row = session.get(TextRegion, region["id"])
            assert row is not None
            recognition = with_detection_evidence(
                row.recognition,
                0.82,
                "tesseract",
                input_variant=input_variant,
            )
            recognition["trust"] = {
                "policyVersion": 1,
                "disposition": "trusted",
                "reason": "human-confirmed",
            }
            row.recognition = recognition

    current_project = client.get(f"/api/projects/{project['id']}").json()
    changed = client.patch(
        f"/api/projects/{project['id']}",
        json={
            "settings": {"preprocessing": {"threshold": 181}},
            "expectedRevision": current_project["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    current = {
        region["id"]: region for region in client.get(f"/api/images/{image['id']}/regions").json()
    }
    assert current[regions[0]["id"]]["confirmed"] is False
    assert current[regions[0]["id"]]["trustDisposition"] == "review"
    assert current[regions[0]["id"]]["trustReason"] == "trust-input-changed"
    assert current[regions[1]["id"]]["confirmed"] is True
    assert current[regions[1]["id"]]["trustDisposition"] == "trusted"
