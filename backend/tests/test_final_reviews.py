from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from manga_localizer.database import ImageAsset, PageGeneration, PageLineageEvent, Revision
from manga_localizer.main import create_app
from manga_localizer.services import final_reviews as final_review_service
from manga_localizer.services.typesets import require_current_typeset_acceptance

from . import test_page_lineage as lineage_test_helpers
from .conftest import create_project, png_bytes, upload_image
from .test_page_lineage import (
    _accept_g1_preprocess,
    _accept_g2_without_reconstruction,
    _accept_g3_text_present,
    _mutation_lineage,
    _prepare_g6_accepted_page,
)
from .test_typesets import _prepare_g9_terminal, _review_body, _run_typeset

_ACTOR = {
    "actorKind": "human",
    "actorId": "final-review-test",
    "operationSource": "ui",
}


def _strict_project(app, client: TestClient, tmp_path: Path) -> tuple[dict, dict]:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    _job, context = _run_typeset(client, app, prepared)
    candidate = context["candidates"][0]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(context, candidate, generation_id),
    )
    assert accepted.status_code == 200, accepted.text
    return project, image


def _strict_batch(app, client: TestClient, tmp_path: Path) -> dict:
    project, _image = _strict_project(app, client, tmp_path / "strict")
    response = client.post(
        "/api/final-review-batches",
        json={
            "name": "strict review",
            "outputPath": str(tmp_path / "review"),
            "sourceProjectIds": [project["id"]],
            "expectedItemCount": 1,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_strict_batch_creation_holds_source_lock_through_evidence_freeze(
    app,
    client: TestClient,
    tmp_path: Path,
) -> None:
    project, _image = _strict_project(app, client, tmp_path / "locked-source")
    source_store = app.state.registry.get(project["id"])
    writer_started = threading.Event()
    writer_acquired = threading.Event()
    writer_thread: threading.Thread | None = None
    real_copyfile = final_review_service.shutil.copyfile

    def copy_while_writer_waits(source: Path, destination: Path) -> str:
        nonlocal writer_thread

        if writer_thread is None:

            def attempt_source_write() -> None:
                writer_started.set()
                with source_store.lock:
                    writer_acquired.set()

            writer_thread = threading.Thread(target=attempt_source_write, daemon=True)
            writer_thread.start()
            assert writer_started.wait(timeout=1)
            assert not writer_acquired.wait(timeout=0.05)
        return real_copyfile(source, destination)

    with patch.object(final_review_service.shutil, "copyfile", copy_while_writer_waits):
        response = client.post(
            "/api/final-review-batches",
            json={
                "name": "locked strict review",
                "outputPath": str(tmp_path / "locked-review"),
                "sourceProjectIds": [project["id"]],
                "expectedItemCount": 1,
            },
        )

    assert response.status_code == 201, response.text
    assert writer_thread is not None
    writer_thread.join(timeout=1)
    assert writer_acquired.is_set()
    assert response.json()["items"][0]["currentArtifactStale"] is False


def _legacy_review(
    root: Path,
    *,
    source_project_id: str = "missing-project",
    source_image_id: str = "missing-image",
    verdict: str = "approved",
) -> Path:
    return _legacy_review_for_sources(
        root,
        [(source_project_id, source_image_id)],
        verdict=verdict,
    )


def _legacy_review_for_sources(
    root: Path,
    sources: list[tuple[str, str]],
    *,
    verdict: str = "issues",
) -> Path:
    assert sources
    (root / "final-review").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "thumbnails").mkdir()
    now = "2026-08-26T00:00:00Z"
    database = root / "final-review/final-review.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE batches (id TEXT PRIMARY KEY, name TEXT, root_path TEXT,
              item_count INTEGER, revision INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE items (id TEXT PRIMARY KEY, batch_id TEXT, position INTEGER,
              source_project_id TEXT, source_image_id TEXT, source_project_name TEXT,
              source_relative_path TEXT, final_variant TEXT, artifact_checksum TEXT,
              thumbnail_checksum TEXT, snapshot_path TEXT, thumbnail_path TEXT,
              verdict TEXT, issue_codes TEXT, feedback TEXT, reviewed_at TEXT,
              revision INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE revisions (id TEXT PRIMARY KEY, batch_id TEXT, item_id TEXT,
              operation TEXT, before_json TEXT, after_json TEXT, item_revision INTEGER,
              created_at TEXT);
            """
        )
        connection.execute(
            "INSERT INTO batches VALUES (?, ?, ?, ?, 2, ?, ?)",
            ("legacy-batch", "legacy", str(root), len(sources), now, now),
        )
        colors = ("white", "black", "gray", "red")
        for position, (source_project_id, source_image_id) in enumerate(sources, start=1):
            item_id = "legacy-item" if len(sources) == 1 else f"legacy-item-{position}"
            frozen = png_bytes(color=colors[(position - 1) % len(colors)])
            snapshot_path = f"images/{item_id}.png"
            thumbnail_path = f"thumbnails/{item_id}.jpg"
            (root / snapshot_path).write_bytes(frozen)
            (root / thumbnail_path).write_bytes(frozen)
            checksum = hashlib.sha256(frozen).hexdigest()
            connection.execute(
                """INSERT INTO items
                (id, batch_id, position, source_project_id, source_image_id,
                 source_project_name, source_relative_path, final_variant,
                 artifact_checksum, thumbnail_checksum, snapshot_path, thumbnail_path,
                 verdict, issue_codes, feedback, reviewed_at, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    "legacy-batch",
                    position,
                    source_project_id,
                    source_image_id,
                    "legacy",
                    f"page-{position}.png",
                    "typeset",
                    checksum,
                    checksum,
                    snapshot_path,
                    thumbnail_path,
                    verdict,
                    "[]" if verdict == "approved" else '["other"]',
                    "",
                    now,
                    2,
                    now,
                    now,
                ),
            )
    manifest = root / "final-review/manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "kind": "manga-localizer-final-review",
                "batch": {
                    "id": "legacy-batch",
                    "name": "legacy",
                    "itemCount": len(sources),
                    "createdAt": now,
                },
            }
        ),
        "utf-8",
    )
    return manifest


def _complete_repair_g10(
    app, client: TestClient, tmp_path: Path, handoff: dict[str, object]
) -> None:
    with patch.object(lineage_test_helpers, "_RUN_ID", handoff["runId"]):
        _complete_repair_g10_for_run(app, client, tmp_path, handoff)


def _complete_repair_g10_for_run(
    app, client: TestClient, tmp_path: Path, handoff: dict[str, object]
) -> None:
    project = next(
        row for row in client.get("/api/projects").json() if row["id"] == handoff["repairProjectId"]
    )
    image = next(
        row
        for row in client.get(f"/api/projects/{project['id']}/images").json()
        if row["id"] == handoff["repairImageId"]
    )
    generation_id = str(handoff["pageGenerationId"])
    identity = client.get(f"/api/page-generations/{generation_id}/events").json()[0]
    quality_checksum, g1 = _accept_g1_preprocess(
        client,
        app,
        target_project=project,
        target_image=image,
        generation_id=generation_id,
    )
    generation = next(
        row
        for row in client.get(f"/api/images/{image['id']}/page-generations").json()
        if row["id"] == generation_id
    )
    g2 = _accept_g2_without_reconstruction(
        client,
        image_id=image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=g1["revision"],
        expected_sequence=generation["nextSequence"],
    )
    g3 = _accept_g3_text_present(
        client,
        source_checksum=identity["inputChecksum"],
        image_id=image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=g2["imageRevision"],
        expected_sequence=g2["nextSequence"],
    )
    prepared = {
        "targetProject": project,
        "targetImage": image,
        "generationId": generation_id,
        "qualityChecksum": quality_checksum,
        "acceptedG3": g3,
        "store": app.state.registry.get(project["id"]),
    }
    prepared = _prepare_g9_terminal(client, app, tmp_path, prepared=prepared)
    _job, context = _run_typeset(client, app, prepared)
    candidate = context["candidates"][0]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(context, candidate, generation_id),
    )
    assert accepted.status_code == 200, accepted.text


def _sqlite_logical_snapshot(database: Path) -> tuple[list[tuple], dict[str, list[tuple]]]:
    with sqlite3.connect(database) as connection:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if not row[0].startswith("sqlite_")
        ]
        rows = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }
    return schema, rows


def test_new_batch_is_strict_v2_with_five_immutable_evidence_routes(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    assert batch["formatVersion"] == 2
    item = batch["items"][0]
    assert item["strictEvidence"] is True
    assert item["artifactRevision"] == 1
    assert set(item["evidence"]) == {"original", "quality", "mask", "clean", "final"}
    for descriptor in item["evidence"].values():
        assert descriptor["availability"] in {"available", "not-applicable"}
        if descriptor["availability"] == "available":
            response = client.get(descriptor["url"])
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == descriptor["checksum"]
            assert descriptor["terminalId"]
            assert descriptor["terminalChecksum"]
            assert descriptor["terminalRevisionId"]
    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.session() as session:
        expected_events = {
            "original": ("G0_identity", "generation-created", "generation-created"),
            "quality": (
                "G1_baselineUpscale",
                "preprocess-artifact-produced",
                "reconstruction-decision",
            ),
            "mask": ("G7_mask", "mask-artifact-produced", "mask-stage-review"),
            "clean": (
                "G8_cleanPlate",
                "clean-plate-candidate-produced",
                "clean-plate-stage-review",
            ),
            "final": (
                "G10_typeset",
                "typeset-candidate-produced",
                "typeset-candidate-reviewed",
            ),
        }
        for kind, (gate, producer_operation, terminal_operation) in expected_events.items():
            descriptor = item["evidence"][kind]
            assert descriptor["availability"] == "available"
            producer = session.get(PageLineageEvent, descriptor["producerId"])
            terminal = session.get(PageLineageEvent, descriptor["terminalId"])
            assert producer is not None and terminal is not None
            assert producer.generation_id == terminal.generation_id == descriptor["generationId"]
            assert producer.gate == gate
            assert terminal.gate == ("G2_reconstruction" if kind == "quality" else gate)
            assert producer.operation == producer_operation
            assert terminal.operation == terminal_operation
            assert producer.revision_id == descriptor["producerRevisionId"]
            assert terminal.revision_id == descriptor["terminalRevisionId"]
            assert terminal.output_checksum == descriptor["terminalChecksum"]

        original = item["evidence"]["original"]
        original_event = session.get(PageLineageEvent, original["producerId"])
        assert original_event is not None
        assert original["producerId"] == original["terminalId"]
        assert original_event.state == "accepted"
        assert original_event.evidence["targetImageId"] == item["sourceImageId"]
        assert original_event.output_checksum == original["checksum"]

        quality = item["evidence"]["quality"]
        quality_producer = session.get(PageLineageEvent, quality["producerId"])
        quality_terminal = session.get(PageLineageEvent, quality["terminalId"])
        assert quality_producer is not None and quality_terminal is not None
        assert quality["producerId"] != quality["terminalId"]
        assert quality_producer.state == "pending"
        assert quality_producer.revision_id is None
        assert quality_producer.evidence["targetKind"] == "image"
        assert quality_producer.output_checksum == quality["checksum"]
        assert quality_terminal.state == "accepted"
        assert quality_terminal.evidence["targetKind"] == "preprocessed"
        assert quality_terminal.input_checksum == quality_producer.output_checksum
        assert quality_terminal.output_checksum == quality["checksum"]

        for kind, checksum_field, identity_field in (
            ("mask", "maskChecksum", "artifactId"),
            ("clean", "candidateChecksum", "candidateId"),
            ("final", "candidateChecksum", "candidateId"),
        ):
            descriptor = item["evidence"][kind]
            producer = session.get(PageLineageEvent, descriptor["producerId"])
            terminal = session.get(PageLineageEvent, descriptor["terminalId"])
            assert producer is not None and terminal is not None
            assert producer.state == "pending"
            assert terminal.state == "accepted"
            assert producer.sequence < terminal.sequence
            assert producer.output_checksum == terminal.input_checksum
            assert producer.evidence[identity_field] == terminal.evidence[identity_field]
            assert producer.evidence[checksum_field] == descriptor["checksum"]
            assert terminal.evidence[checksum_field] == descriptor["checksum"]
            assert descriptor["terminalChecksum"] != descriptor["checksum"]
    wrong_revision = client.get(
        f"/api/final-review-items/{item['id']}/artifacts/final?artifactRevision=2"
    )
    assert wrong_revision.status_code == 404
    thumbnail = client.get(item["thumbnailUrl"])
    assert thumbnail.status_code == 200
    assert hashlib.sha256(thumbnail.content).hexdigest() == item["thumbnailChecksum"]


def test_new_batch_rejects_legacy_item_without_publishing_directory(
    app, client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "legacy")
    upload_image(client, project["id"])
    target = tmp_path / "must-not-publish"
    response = client.post(
        "/api/final-review-batches",
        json={
            "name": "legacy rejected",
            "outputPath": str(target),
            "sourceProjectIds": [project["id"]],
            "expectedItemCount": 1,
        },
    )
    assert response.status_code == 400
    assert not target.exists()
    assert not list(tmp_path.glob(".must-not-publish.*.tmp"))


def test_text_present_g7_g8_na_evidence_binds_each_exact_terminal(
    app, client: TestClient, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(
        client,
        app,
        tmp_path / "na-source",
        disposition="keep-art",
        extra_dispositions=("ignore",),
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)

    mask_context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert mask_context["eligibleRegionIds"] == []
    g7_response = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "not-applicable",
            "reason": "no-eligible-regions",
            "selectedArtifactId": None,
            "observedMaskChecksum": None,
            "coverageChecks": [],
            "collateralChecks": [],
            "expectedRevision": mask_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, mask_context["nextSequence"]),
        },
    )
    assert g7_response.status_code == 200, g7_response.text
    g7_event_payload = g7_response.json()["event"]

    clean_context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    g8_response = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "not-applicable",
            "reason": "no-clean-plate-required",
            "candidateId": None,
            "observedCandidateChecksum": None,
            "observedWidth": None,
            "observedHeight": None,
            "checks": [],
            "expectedRevision": clean_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, clean_context["nextSequence"]),
        },
    )
    assert g8_response.status_code == 200, g8_response.text
    g8_event_payload = g8_response.json()["event"]

    translation_context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    assert translation_context["eligibleRegions"] == []
    g9_response = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "not-applicable",
            "observedTranslationStateChecksum": translation_context["translationStateChecksum"],
            "expectedRevision": translation_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, translation_context["nextSequence"]),
        },
    )
    assert g9_response.status_code == 200, g9_response.text

    _job, typeset_context = _run_typeset(client, app, prepared)
    candidate = typeset_context["candidates"][0]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(typeset_context, candidate, generation_id),
    )
    assert accepted.status_code == 200, accepted.text
    created = client.post(
        "/api/final-review-batches",
        json={
            "name": "text-present na review",
            "outputPath": str(tmp_path / "na-review"),
            "sourceProjectIds": [project["id"]],
            "expectedItemCount": 1,
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()["items"][0]
    mask = item["evidence"]["mask"]
    clean = item["evidence"]["clean"]
    assert mask["availability"] == clean["availability"] == "not-applicable"
    assert mask["producerId"] is None and clean["producerId"] is None
    assert mask["terminalId"] == g7_event_payload["id"]
    assert clean["terminalId"] == g8_event_payload["id"]
    assert mask["terminalId"] != clean["terminalId"]

    project_store = app.state.registry.get(project["id"])
    with project_store.session() as session:
        g7_event = session.get(PageLineageEvent, mask["terminalId"])
        g8_event = session.get(PageLineageEvent, clean["terminalId"])
        assert g7_event is not None and g8_event is not None
        for descriptor, event, gate, operation in (
            (mask, g7_event, "G7_mask", "mask-stage-review"),
            (clean, g8_event, "G8_cleanPlate", "clean-plate-stage-review"),
        ):
            assert event.generation_id == descriptor["generationId"] == generation_id
            assert event.gate == gate
            assert event.operation == operation
            assert event.state == "not-applicable"
            assert event.revision_id == descriptor["terminalRevisionId"]
            assert event.output_checksum == descriptor["terminalChecksum"]


def test_strict_save_returns_authoritative_batch_revision_and_noop_contract(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    body = {
        "verdict": "issues",
        "issueCodes": ["mask"],
        "feedback": "  edge residue  ",
        "expectedRevision": item["revision"],
        "expectedBatchRevision": batch["revision"],
        "actor": _ACTOR,
    }
    saved = client.patch(f"/api/final-review-items/{item['id']}", json=body)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["historyCreated"] is True
    assert result["item"]["feedback"] == "edge residue"
    assert result["batchRevision"] == batch["revision"] + 1
    body["feedback"] = "edge residue"
    body["expectedRevision"] = result["item"]["revision"]
    body["expectedBatchRevision"] = result["batchRevision"]
    noop = client.patch(f"/api/final-review-items/{item['id']}", json=body)
    assert noop.status_code == 200
    assert noop.json()["historyCreated"] is False
    assert noop.json()["batchRevision"] == result["batchRevision"]


@pytest.mark.parametrize("actor_kind", ["codex", "cursor", "system"])
def test_only_a_human_actor_can_approve_a_final_review_item(
    app, client: TestClient, tmp_path: Path, actor_kind: str
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    before_history = client.get(f"/api/final-review-items/{item['id']}/revisions")
    assert before_history.status_code == 200, before_history.text

    rejected = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": {
                "actorKind": actor_kind,
                "actorId": f"truthful-{actor_kind}",
                "operationSource": "api",
            },
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"] == "Final-review approval requires a human actor"

    current = client.get(f"/api/final-review-batches/{batch['id']}")
    assert current.status_code == 200, current.text
    current_batch = current.json()
    assert current_batch["revision"] == batch["revision"]
    assert current_batch["items"][0]["revision"] == item["revision"]
    assert current_batch["items"][0]["verdict"] == "pending"
    after_history = client.get(f"/api/final-review-items/{item['id']}/revisions")
    assert after_history.status_code == 200, after_history.text
    assert after_history.json() == before_history.json()


def test_codex_can_report_issues_but_cannot_change_them_to_approved(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    codex_actor = {
        "actorKind": "codex",
        "taskId": "truthful-codex-task",
        "operationSource": "api",
    }
    reported = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "issues",
            "issueCodes": ["typesetting"],
            "feedback": "layout still needs repair",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": codex_actor,
        },
    )
    assert reported.status_code == 200, reported.text
    reported_body = reported.json()
    assert reported_body["item"]["verdict"] == "issues"

    rejected = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "",
            "expectedRevision": reported_body["item"]["revision"],
            "expectedBatchRevision": reported_body["batchRevision"],
            "actor": codex_actor,
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"] == "Final-review approval requires a human actor"
    current = client.get(f"/api/final-review-batches/{batch['id']}").json()
    assert current["revision"] == reported_body["batchRevision"]
    assert current["items"][0]["revision"] == reported_body["item"]["revision"]
    assert current["items"][0]["verdict"] == "issues"


def test_save_refresh_and_export_batch_revision_drift_are_409_and_reloadable(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    database = Path(batch["rootPath"]) / "final-review/final-review.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE batches SET revision = revision + 1 WHERE id = ?", (batch["id"],)
        )
    expected = batch["revision"]
    actual = expected + 1

    responses = [
        client.patch(
            f"/api/final-review-items/{item['id']}",
            json={
                "verdict": "approved",
                "issueCodes": [],
                "feedback": "",
                "expectedRevision": item["revision"],
                "expectedBatchRevision": expected,
                "actor": _ACTOR,
            },
        ),
        client.post(
            f"/api/final-review-items/{item['id']}/refresh",
            json={
                "expectedRevision": item["revision"],
                "expectedBatchRevision": expected,
                "actor": _ACTOR,
            },
        ),
        client.post(
            f"/api/final-review-batches/{batch['id']}/export",
            json={
                "outputPath": str(tmp_path / "stale-export"),
                "expectedBatchRevision": expected,
                "actor": _ACTOR,
            },
        ),
    ]
    for response in responses:
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["resource"] == f"final-review-batch:{batch['id']}"
        assert detail["expectedRevision"] == expected
        assert detail["actualRevision"] == actual
    assert not (tmp_path / "stale-export").exists()

    current = client.get(f"/api/final-review-batches/{batch['id']}")
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == actual
    assert current.json()["items"][0]["revision"] == item["revision"]
    assert current.json()["items"][0]["verdict"] == "pending"


def test_issue_repair_isolated_g0_idempotent_and_verdict_unchanged(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    saved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "issues",
            "issueCodes": ["translation"],
            "feedback": "private feedback",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    ).json()
    before_repair = client.get(f"/api/final-review-batches/{batch['id']}").json()["items"][0]
    assert before_repair["currentArtifactStale"] is False
    request = {
        "expectedRevision": saved["item"]["revision"],
        "expectedBatchRevision": saved["batchRevision"],
        "actor": _ACTOR,
    }
    first = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert first.status_code == 201, first.text
    handoff = first.json()
    assert handoff["repairImageId"] != item["sourceImageId"]
    assert handoff["artifactRevision"] == item["artifactRevision"]
    assert handoff["nextSequence"] == 2
    retry = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert retry.status_code == 201
    assert retry.json()["pageGenerationId"] == handoff["pageGenerationId"]
    assert retry.json()["idempotent"] is True
    current = client.get(f"/api/final-review-batches/{batch['id']}").json()["items"][0]
    assert current["verdict"] == "issues"
    assert current["currentArtifactStale"] is False
    events = client.get(f"/api/page-generations/{handoff['pageGenerationId']}/events").json()
    text = json.dumps(events, ensure_ascii=False)
    assert "private feedback" not in text
    assert events[0]["evidence"]["feedbackChecksum"]
    old_url = item["evidence"]["final"]["url"]
    old_bytes = client.get(old_url).content
    _complete_repair_g10(app, client, tmp_path / "repair-g10", handoff)
    ready = client.get(f"/api/final-review-batches/{batch['id']}").json()["items"][0]
    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.session() as session:
        exact = []
        for generation in session.scalars(select(PageGeneration)):
            event = session.scalar(
                select(PageLineageEvent).where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.gate == "G0_identity",
                )
            )
            evidence = event.evidence if event else {}
            if evidence.get("finalReviewItemId") == item["id"]:
                exact.append(
                    (
                        generation.id,
                        generation.source_project_id,
                        generation.source_image_id,
                        evidence.get("finalReviewItemRevision"),
                        evidence.get("feedbackChecksum"),
                    )
                )
    assert len(exact) == 1, exact
    with project_store.session() as session:
        generation = session.get(PageGeneration, exact[0][0])
        assert generation is not None
        repair_image = session.get(ImageAsset, generation.image_id)
        assert repair_image is not None
        require_current_typeset_acceptance(project_store, session, repair_image, generation)
    assert ready["verdict"] == "issues"
    assert ready["currentArtifactStale"] is True, exact
    refreshed = client.post(
        f"/api/final-review-items/{item['id']}/refresh",
        json={
            "expectedRevision": saved["item"]["revision"],
            "expectedBatchRevision": saved["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    refresh_result = refreshed.json()
    assert refresh_result["historyCreated"] is True
    assert refresh_result["item"]["verdict"] == "pending"
    assert refresh_result["item"]["artifactRevision"] == item["artifactRevision"] + 1
    assert refresh_result["item"]["currentArtifactStale"] is False
    assert client.get(old_url).content == old_bytes
    missing = client.get(f"/api/final-review-items/{item['id']}/artifacts/final?artifactRevision=3")
    assert missing.status_code == 404


def test_repair_idempotence_is_bound_to_the_persisted_parameter_set(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    saved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "issues",
            "issueCodes": ["translation"],
            "feedback": "parameter-bound repair",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    ).json()
    base_request = {
        "expectedRevision": saved["item"]["revision"],
        "expectedBatchRevision": saved["batchRevision"],
        "actor": _ACTOR,
    }
    custom_request = {
        **base_request,
        "parameterSetId": "alternate-repair-v1",
        "parameterSetHash": "a" * 64,
    }

    first = client.post(f"/api/final-review-items/{item['id']}/repair", json=custom_request)
    assert first.status_code == 201, first.text
    handoff = first.json()
    assert handoff["idempotent"] is False
    assert handoff["parameterSetId"] == custom_request["parameterSetId"]
    assert handoff["parameterSetHash"] == custom_request["parameterSetHash"]

    matching_retry = client.post(
        f"/api/final-review-items/{item['id']}/repair", json=custom_request
    )
    assert matching_retry.status_code == 201, matching_retry.text
    assert matching_retry.json() == {**handoff, "idempotent": True}

    mismatched_retry = client.post(
        f"/api/final-review-items/{item['id']}/repair", json=base_request
    )
    assert mismatched_retry.status_code == 400, mismatched_retry.text
    assert mismatched_retry.json()["detail"] == (
        "Existing repair handoff parameter set does not match this request"
    )

    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.session() as session:
        exact: list[tuple[PageGeneration, PageLineageEvent]] = []
        for generation in session.scalars(select(PageGeneration)):
            event = session.scalar(
                select(PageLineageEvent).where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.gate == "G0_identity",
                )
            )
            evidence = event.evidence if event is not None else {}
            if evidence.get("finalReviewItemId") == item["id"]:
                assert event is not None
                exact.append((generation, event))
    assert len(exact) == 1
    generation, event = exact[0]
    assert generation.id == handoff["pageGenerationId"]
    assert generation.parameter_set_id == custom_request["parameterSetId"]
    assert generation.parameter_set_hash == custom_request["parameterSetHash"]
    assert event.parameter_hash == custom_request["parameterSetHash"]


def _create_issue_repair(app, client: TestClient, tmp_path: Path) -> tuple[dict, dict, dict, dict]:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    saved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "issues",
            "issueCodes": ["translation"],
            "feedback": "strict repair identity",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    ).json()
    request = {
        "expectedRevision": saved["item"]["revision"],
        "expectedBatchRevision": saved["batchRevision"],
        "actor": _ACTOR,
    }
    created = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert created.status_code == 201, created.text
    return batch, item, request, created.json()


def test_issue_repair_explicit_retry_is_linear_audited_and_idempotent(
    app,
    client: TestClient,
    tmp_path: Path,
) -> None:
    _batch, item, request, first = _create_issue_repair(app, client, tmp_path)
    assert first["repairAttempt"] == 1
    assert first["retryFromGenerationId"] is None
    project_store = app.state.registry.get(item["sourceProjectId"])
    before_events = client.get(f"/api/page-generations/{first['pageGenerationId']}/events").json()
    with project_store.session() as session:
        first_image = session.get(ImageAsset, first["repairImageId"])
        first_generation = session.get(PageGeneration, first["pageGenerationId"])
        first_g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == first["pageGenerationId"],
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert first_image is not None and first_generation is not None and first_g0 is not None
        first_path = project_store.root / first_image.source_path
        first_bytes = first_path.read_bytes()
        first_g0_evidence = json.loads(json.dumps(first_g0.evidence))

    retry_request = {
        **request,
        "retryFromGenerationId": first["pageGenerationId"],
    }
    retried = client.post(f"/api/final-review-items/{item['id']}/repair", json=retry_request)
    assert retried.status_code == 201, retried.text
    second = retried.json()
    assert second["idempotent"] is False
    assert second["repairAttempt"] == 2
    assert second["retryFromGenerationId"] == first["pageGenerationId"]
    assert second["pageGenerationId"] != first["pageGenerationId"]
    assert second["repairImageId"] != first["repairImageId"]
    assert second["runId"] == f"{first['runId']}-a2"

    with project_store.session() as session:
        first_generation = session.get(PageGeneration, first["pageGenerationId"])
        first_image = session.get(ImageAsset, first["repairImageId"])
        second_generation = session.get(PageGeneration, second["pageGenerationId"])
        second_image = session.get(ImageAsset, second["repairImageId"])
        assert first_generation is not None and first_image is not None
        assert second_generation is not None and second_image is not None
        assert first_generation.state == "superseded"
        assert first_generation.closed_at is not None
        assert second_generation.state == "active"
        assert second_generation.closed_at is None
        assert "/a000002/" in f"/{second_image.relative_path}"
        assert (project_store.root / first_image.source_path).read_bytes() == first_bytes
        assert (project_store.root / second_image.source_path).read_bytes() == first_bytes
        first_g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == first_generation.id,
                PageLineageEvent.gate == "G0_identity",
            )
        )
        second_g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == second_generation.id,
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert first_g0 is not None and second_g0 is not None
        assert first_g0.evidence == first_g0_evidence
        assert second_g0.evidence["repairIdentityVersion"] == 2
        assert second_g0.evidence["repairAttempt"] == 2
        assert second_g0.evidence["retryFromGenerationId"] == first_generation.id

    after_events = client.get(f"/api/page-generations/{first['pageGenerationId']}/events").json()
    assert after_events[:-1] == before_events
    assert after_events[-1]["operation"] == "generation-superseded"
    assert after_events[-1]["evidence"]["successorGenerationId"] == second["pageGenerationId"]

    replay = client.post(f"/api/final-review-items/{item['id']}/repair", json=retry_request)
    assert replay.status_code == 201, replay.text
    assert replay.json() == {**second, "idempotent": True}
    plain = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert plain.status_code == 201, plain.text
    assert plain.json() == {**second, "idempotent": True}

    stale = client.post(
        f"/api/final-review-items/{item['id']}/repair",
        json={**request, "retryFromGenerationId": str(uuid4())},
    )
    assert stale.status_code == 400, stale.text
    with project_store.session() as session:
        identity_events = [
            event
            for event in session.scalars(
                select(PageLineageEvent).where(PageLineageEvent.gate == "G0_identity")
            )
            if event.evidence.get("finalReviewItemId") == item["id"]
        ]
    assert len(identity_events) == 2


def _insert_repair_identity_decoy(
    app,
    item: dict,
    handoff: dict,
    *,
    generation_id: str,
    parameter_hash: str,
) -> None:
    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, project_store.session() as session:
        original = session.get(PageGeneration, handoff["pageGenerationId"])
        assert original is not None
        original_event = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == original.id,
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert original_event is not None

        generation_values = {
            column.name: getattr(original, column.name)
            for column in PageGeneration.__table__.columns
        }
        generation_values.update(
            id=generation_id,
            run_id=f"{original.run_id}-decoy-{generation_id[:8]}",
            state="completed",
        )
        session.add(PageGeneration(**generation_values))
        session.flush()

        event_values = {
            column.name: getattr(original_event, column.name)
            for column in PageLineageEvent.__table__.columns
        }
        event_values.update(
            id=str(uuid4()),
            generation_id=generation_id,
            parameter_hash=parameter_hash,
        )
        session.add(PageLineageEvent(**event_values))


def _preset_corrupt_g0_revision(project_store, revision_id: str, after: dict) -> None:
    """Model an already-corrupt database, then restore the production guards."""
    with project_store.lock, sqlite3.connect(project_store.database_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS revisions_g0_no_update")
        connection.execute("DROP TRIGGER IF EXISTS revisions_g0_no_delete")
        try:
            updated = connection.execute(
                'UPDATE revisions SET "after" = ? WHERE id = ?',
                (json.dumps(after, separators=(",", ":")), revision_id),
            )
            assert updated.rowcount == 1
        finally:
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS revisions_g0_no_update
                BEFORE UPDATE ON revisions
                WHEN EXISTS (
                    SELECT 1 FROM page_lineage_events AS event
                    WHERE event.revision_id = OLD.id
                      AND event.gate = 'G0_identity'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'G0 lineage revisions are append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS revisions_g0_no_delete
                BEFORE DELETE ON revisions
                WHEN EXISTS (
                    SELECT 1 FROM page_lineage_events AS event
                    WHERE event.revision_id = OLD.id
                      AND event.gate = 'G0_identity'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'G0 lineage revisions are append-only');
                END
                """
            )


@pytest.mark.parametrize(
    "decoys",
    [
        [
            ("00000000-0000-4000-8000-000000000001", "b" * 64),
            ("ffffffff-ffff-4fff-bfff-ffffffffffff", "c" * 64),
        ],
        [
            ("ffffffff-ffff-4fff-bfff-ffffffffffff", "c" * 64),
            ("00000000-0000-4000-8000-000000000001", "b" * 64),
        ],
    ],
)
def test_repair_idempotence_rejects_every_duplicate_identity_candidate(
    app,
    client: TestClient,
    tmp_path: Path,
    decoys: list[tuple[str, str]],
) -> None:
    _batch, item, request, handoff = _create_issue_repair(app, client, tmp_path / decoys[0][0][:8])
    clean_retry = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert clean_retry.status_code == 201, clean_retry.text
    assert clean_retry.json() == {**handoff, "idempotent": True}

    for generation_id, parameter_hash in decoys:
        _insert_repair_identity_decoy(
            app,
            item,
            handoff,
            generation_id=generation_id,
            parameter_hash=parameter_hash,
        )

    conflicted = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert conflicted.status_code == 400, conflicted.text
    assert "pageGenerationId" not in conflicted.json()
    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.session() as session:
        candidates = []
        for event in session.scalars(
            select(PageLineageEvent).where(PageLineageEvent.gate == "G0_identity")
        ):
            if event.evidence.get("finalReviewItemId") == item["id"]:
                candidates.append((event.generation_id, event.parameter_hash))
    assert sorted(candidates) == sorted(
        [
            (handoff["pageGenerationId"], handoff["parameterSetHash"]),
            *decoys,
        ]
    )


@pytest.mark.parametrize("corruption", ["target-bytes", "source-binding"])
def test_repair_idempotence_rejects_corrupt_target_binding(
    app,
    client: TestClient,
    tmp_path: Path,
    corruption: str,
) -> None:
    _batch, item, request, handoff = _create_issue_repair(app, client, tmp_path / corruption)
    clean_retry = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert clean_retry.status_code == 201, clean_retry.text
    assert clean_retry.json() == {**handoff, "idempotent": True}

    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, project_store.session() as session:
        repair_image = session.get(ImageAsset, handoff["repairImageId"])
        assert repair_image is not None
        if corruption == "target-bytes":
            corrupt_bytes = png_bytes(color="red")
            assert hashlib.sha256(corrupt_bytes).hexdigest() != repair_image.checksum
            (project_store.root / repair_image.source_path).write_bytes(corrupt_bytes)
        else:
            repair_image.source_kind = "browser-upload"

    rejected = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert rejected.status_code == 400, rejected.text
    assert "pageGenerationId" not in rejected.json()
    with project_store.session() as session:
        identity_generation_ids = [
            event.generation_id
            for event in session.scalars(
                select(PageLineageEvent).where(PageLineageEvent.gate == "G0_identity")
            )
            if event.evidence.get("finalReviewItemId") == item["id"]
        ]
    assert identity_generation_ids == [handoff["pageGenerationId"]]


def test_approved_repair_revision_drift_is_stale_and_blocks_terminal_export(
    app,
    client: TestClient,
    tmp_path: Path,
) -> None:
    batch, item, request, handoff = _create_issue_repair(
        app, client, tmp_path / "approved-repair-drift"
    )
    _complete_repair_g10(app, client, tmp_path / "approved-repair-g10", handoff)
    refreshed = client.post(
        f"/api/final-review-items/{item['id']}/refresh",
        json={
            "expectedRevision": request["expectedRevision"],
            "expectedBatchRevision": request["expectedBatchRevision"],
            "actor": _ACTOR,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_payload = refreshed.json()
    assert refreshed_payload["item"]["verdict"] == "pending"
    approved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "",
            "expectedRevision": refreshed_payload["item"]["revision"],
            "expectedBatchRevision": refreshed_payload["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert approved.status_code == 200, approved.text
    approved_payload = approved.json()

    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, project_store.session() as session:
        g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == handoff["pageGenerationId"],
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert g0 is not None and g0.revision_id is not None
        creation_revision = session.get(Revision, g0.revision_id)
        assert creation_revision is not None and creation_revision.after is not None
        corrupt_revision_id = creation_revision.id
        corrupt_after = {
            **creation_revision.after,
            "feedbackChecksum": "d" * 64,
        }
    _preset_corrupt_g0_revision(project_store, corrupt_revision_id, corrupt_after)

    current = client.get(f"/api/final-review-batches/{batch['id']}")
    assert current.status_code == 200, current.text
    current_item = current.json()["items"][0]
    assert current_item["verdict"] == "approved"
    assert current_item["currentArtifactStale"] is True

    export_target = tmp_path / "must-not-export-revision-drift"
    exported = client.post(
        f"/api/final-review-batches/{batch['id']}/export",
        json={
            "outputPath": str(export_target),
            "expectedBatchRevision": approved_payload["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert exported.status_code >= 400, exported.text
    assert not export_target.exists()


def test_approved_native_strict_g0_revision_drift_is_stale_and_blocks_export(
    app,
    client: TestClient,
    tmp_path: Path,
) -> None:
    batch = _strict_batch(app, client, tmp_path / "native-strict-drift")
    item = batch["items"][0]
    approved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    )
    assert approved.status_code == 200, approved.text
    approved_payload = approved.json()
    generation_id = item["evidence"]["final"]["generationId"]
    assert generation_id

    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, project_store.session() as session:
        g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert g0 is not None and g0.revision_id is not None
        creation_revision = session.get(Revision, g0.revision_id)
        assert creation_revision is not None and creation_revision.after is not None
        assert creation_revision.after["runId"]
        corrupt_revision_id = creation_revision.id
        corrupt_after = {
            **creation_revision.after,
            "runId": "tampered-native-strict-run",
        }
    _preset_corrupt_g0_revision(project_store, corrupt_revision_id, corrupt_after)

    current = client.get(f"/api/final-review-batches/{batch['id']}")
    assert current.status_code == 200, current.text
    current_item = current.json()["items"][0]
    assert current_item["verdict"] == "approved"
    assert current_item["currentArtifactStale"] is True

    export_target = tmp_path / "must-not-export-native-g0-drift"
    exported = client.post(
        f"/api/final-review-batches/{batch['id']}/export",
        json={
            "outputPath": str(export_target),
            "expectedBatchRevision": approved_payload["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert exported.status_code >= 400, exported.text
    assert not export_target.exists()


@pytest.mark.parametrize("trigger_tamper", ["missing", "same-name-weak"])
def test_approved_strict_missing_or_weak_lineage_event_trigger_blocks_export(
    app,
    client: TestClient,
    tmp_path: Path,
    trigger_tamper: str,
) -> None:
    batch = _strict_batch(app, client, tmp_path / f"event-trigger-{trigger_tamper}")
    item = batch["items"][0]
    approved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    )
    assert approved.status_code == 200, approved.text
    approved_payload = approved.json()

    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, sqlite3.connect(project_store.database_path) as connection:
        connection.execute("DROP TRIGGER page_lineage_events_no_update")
        if trigger_tamper == "same-name-weak":
            connection.execute(
                """
                CREATE TRIGGER page_lineage_events_no_update
                BEFORE UPDATE ON page_lineage_events
                BEGIN
                    SELECT 1;
                END
                """
            )

    current = client.get(f"/api/final-review-batches/{batch['id']}")
    assert current.status_code == 200, current.text
    current_item = current.json()["items"][0]
    assert current_item["verdict"] == "approved"
    assert current_item["currentArtifactStale"] is True

    export_target = tmp_path / f"must-not-export-{trigger_tamper}-event-trigger"
    exported = client.post(
        f"/api/final-review-batches/{batch['id']}/export",
        json={
            "outputPath": str(export_target),
            "expectedBatchRevision": approved_payload["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert exported.status_code >= 400, exported.text
    assert not export_target.exists()


def test_repair_retry_rejects_json_number_type_alias_in_creation_revision(
    app,
    client: TestClient,
    tmp_path: Path,
) -> None:
    _batch, item, request, handoff = _create_issue_repair(app, client, tmp_path / "json-type-alias")
    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, project_store.session() as session:
        g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == handoff["pageGenerationId"],
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert g0 is not None and g0.revision_id is not None
        creation_revision = session.get(Revision, g0.revision_id)
        assert creation_revision is not None and creation_revision.after is not None
        expected_revision = creation_revision.after["finalReviewItemRevision"]
        assert type(expected_revision) is int
        corrupt_revision_id = creation_revision.id
        corrupt_after = {
            **creation_revision.after,
            "finalReviewItemRevision": float(expected_revision),
        }
    _preset_corrupt_g0_revision(project_store, corrupt_revision_id, corrupt_after)

    rejected = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert rejected.status_code == 400, rejected.text
    assert "pageGenerationId" not in rejected.json()


def test_repair_retry_rejects_generation_parameter_id_drift(
    app,
    client: TestClient,
    tmp_path: Path,
) -> None:
    _batch, item, request, handoff = _create_issue_repair(
        app, client, tmp_path / "generation-parameter-drift"
    )
    project_store = app.state.registry.get(item["sourceProjectId"])
    with project_store.lock, project_store.session() as session:
        generation = session.get(PageGeneration, handoff["pageGenerationId"])
        assert generation is not None
        g0 = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G0_identity",
            )
        )
        assert g0 is not None and g0.revision_id is not None
        creation_revision = session.get(Revision, g0.revision_id)
        assert creation_revision is not None and creation_revision.after is not None
        assert g0.evidence["parameterSetId"] == generation.parameter_set_id
        assert g0.evidence["parameterSetHash"] == generation.parameter_set_hash
        assert creation_revision.after["parameterSetId"] == generation.parameter_set_id
        assert creation_revision.after["parameterSetHash"] == generation.parameter_set_hash
        assert generation.parameter_set_id == handoff["parameterSetId"]
        assert generation.parameter_set_hash == handoff["parameterSetHash"]

        generation.parameter_set_id = "different-valid-repair-parameters-v1"

    rejected = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert rejected.status_code == 400, rejected.text
    assert "pageGenerationId" not in rejected.json()


def test_export_private_batch_bound_and_history_append_only(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    saved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "private approved note",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    ).json()
    missing = client.post(
        f"/api/final-review-batches/{batch['id']}/export",
        json={"outputPath": str(tmp_path / "unbound")},
    )
    assert missing.status_code == 422
    unsafe_target = tmp_path / "unsafe-skip"
    unsafe = client.post(
        f"/api/final-review-batches/{batch['id']}/export",
        json={
            "outputPath": str(unsafe_target),
            "conflict": "skip",
            "expectedBatchRevision": saved["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert unsafe.status_code == 400
    assert unsafe.json()["detail"] == (
        "Final-review terminal export requires safe collision renaming"
    )
    assert not unsafe_target.exists()
    stale_target = tmp_path / "stale-artifact"
    store = app.state.final_reviews.get(batch["id"])
    with patch.object(store, "_current_stale", return_value=True):
        stale = client.post(
            f"/api/final-review-batches/{batch['id']}/export",
            json={
                "outputPath": str(stale_target),
                "expectedBatchRevision": saved["batchRevision"],
                "actor": _ACTOR,
            },
        )
    assert stale.status_code == 400
    assert stale.json()["detail"] == (
        "Final-review export requires every approved artifact to be current"
    )
    assert not stale_target.exists()
    drift_target = tmp_path / "stale-during-export"
    with patch.object(store, "_current_stale", side_effect=[False, True]) as stale_check:
        drift = client.post(
            f"/api/final-review-batches/{batch['id']}/export",
            json={
                "outputPath": str(drift_target),
                "expectedBatchRevision": saved["batchRevision"],
                "actor": _ACTOR,
            },
        )
    assert drift.status_code == 400
    assert drift.json()["detail"] == ("Final-review approved artifacts changed during export")
    assert stale_check.call_count == 2
    assert not drift_target.exists()

    source_store = app.state.registry.get(item["sourceProjectId"])
    writer_started = threading.Event()
    writer_acquired = threading.Event()
    writer_thread: threading.Thread | None = None
    real_copyfile = final_review_service.shutil.copyfile

    def copy_while_writer_waits(source: Path, destination: Path) -> str:
        nonlocal writer_thread

        def attempt_source_write() -> None:
            writer_started.set()
            with source_store.lock:
                writer_acquired.set()

        writer_thread = threading.Thread(target=attempt_source_write, daemon=True)
        writer_thread.start()
        assert writer_started.wait(timeout=1)
        assert not writer_acquired.wait(timeout=0.05)
        return real_copyfile(source, destination)

    target = tmp_path / "approved"
    with patch.object(
        final_review_service.shutil,
        "copyfile",
        side_effect=copy_while_writer_waits,
    ):
        exported = client.post(
            f"/api/final-review-batches/{batch['id']}/export",
            json={
                "outputPath": str(target),
                "expectedBatchRevision": saved["batchRevision"],
                "actor": _ACTOR,
            },
        )
    assert exported.status_code == 200, exported.text
    assert writer_thread is not None
    writer_thread.join(timeout=1)
    assert writer_acquired.is_set()
    manifest_text = (target / "manifest.json").read_text("utf-8")
    assert "private approved note" not in manifest_text
    assert "feedback" not in manifest_text.lower()
    database = Path(batch["rootPath"]) / "final-review/final-review.sqlite3"
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM artifact_revisions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE artifact_revisions SET evidence_digest = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM revisions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE revisions SET operation = 'tampered'")


def test_export_rejects_partially_approved_batch_without_creating_target(
    client: TestClient, tmp_path: Path
) -> None:
    manifest = _legacy_review_for_sources(
        tmp_path / "partial-review",
        [("project-a", "image-a"), ("project-b", "image-b")],
        verdict="approved",
    )
    database = manifest.parent / "final-review.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE items
               SET verdict = 'issues', issue_codes = '[\"other\"]'
               WHERE position = 2"""
        )
    opened = client.post("/api/final-review-batches/open", json={"manifestPath": str(manifest)})
    assert opened.status_code == 200, opened.text
    batch = opened.json()
    assert batch["counts"] == {"pending": 0, "approved": 1, "issues": 1}
    target = tmp_path / "must-not-exist"

    response = client.post(
        f"/api/final-review-batches/{batch['id']}/export",
        json={
            "outputPath": str(target),
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == ("Final-review export requires every item to be approved")
    assert not target.exists()


def test_evidence_metadata_tamper_fails_item_read_and_export(
    app, client: TestClient, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    database = Path(batch["rootPath"]) / "final-review/final-review.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER artifact_revisions_no_update")
        evidence = json.loads(
            connection.execute(
                "SELECT evidence_json FROM items WHERE id = ?", (item["id"],)
            ).fetchone()[0]
        )
        evidence["final"]["relativePath"] = "images/tampered.png"
        connection.execute(
            "UPDATE items SET evidence_json = ? WHERE id = ?",
            (json.dumps(evidence, separators=(",", ":")), item["id"]),
        )
    read = client.get(f"/api/final-review-batches/{batch['id']}")
    assert read.status_code == 400


def test_manifest_database_version_mismatch_fails_without_database_write(
    app, client: TestClient, settings, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    manifest_path = Path(batch["manifestPath"])
    database = Path(batch["rootPath"]) / "final-review/final-review.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    payload = json.loads(manifest_path.read_text("utf-8"))
    payload["formatVersion"] = 1
    manifest_path.write_text(json.dumps(payload), "utf-8")
    with TestClient(create_app(settings, start_worker=False)) as reopened:
        response = reopened.post(
            "/api/final-review-batches/open", json={"manifestPath": str(manifest_path)}
        )
        assert response.status_code == 400
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_legacy_open_and_list_are_schema_and_file_hash_stable(
    client: TestClient, tmp_path: Path
) -> None:
    manifest = _legacy_review(tmp_path / "legacy-review")
    database = manifest.parent / "final-review.sqlite3"
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (manifest, database)}
    opened = client.post("/api/final-review-batches/open", json={"manifestPath": str(manifest)})
    assert opened.status_code == 200, opened.text
    batch = client.get("/api/final-review-batches/legacy-batch")
    assert batch.status_code == 200, batch.text
    item = batch.json()["items"][0]
    assert item["formatVersion"] == 1
    assert item["strictEvidence"] is False
    assert item["evidence"]["original"]["availability"] == "unavailable"
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (manifest, database)}
    assert after == before
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    assert "artifact_revision" not in columns


def test_two_v1_items_refresh_in_order_preserves_each_r1_and_rolls_back_second_failure(
    app,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_1, image_1 = _strict_project(app, client, tmp_path / "legacy-source-1")
    project_2, image_2 = _strict_project(app, client, tmp_path / "legacy-source-2")
    review_root = tmp_path / "legacy-two-items"
    manifest = _legacy_review_for_sources(
        review_root,
        [
            (project_1["id"], image_1["id"]),
            (project_2["id"], image_2["id"]),
        ],
    )
    opened_response = client.post(
        "/api/final-review-batches/open", json={"manifestPath": str(manifest)}
    )
    assert opened_response.status_code == 200, opened_response.text
    opened = opened_response.json()
    items = opened["items"]
    assert [item["id"] for item in items] == ["legacy-item-1", "legacy-item-2"]
    assert all(item["strictEvidence"] is False for item in items)
    old_urls = {item["id"]: item["evidence"]["final"]["url"] for item in items}
    old_bytes = {item_id: client.get(url).content for item_id, url in old_urls.items()}
    assert old_bytes[items[0]["id"]] != old_bytes[items[1]["id"]]

    first_handoff_response = client.post(
        f"/api/final-review-items/{items[0]['id']}/repair",
        json={
            "expectedRevision": items[0]["revision"],
            "expectedBatchRevision": opened["revision"],
            "actor": _ACTOR,
        },
    )
    assert first_handoff_response.status_code == 201, first_handoff_response.text
    _complete_repair_g10(
        app,
        client,
        tmp_path / "legacy-two-repair-1",
        first_handoff_response.json(),
    )
    first_refresh = client.post(
        f"/api/final-review-items/{items[0]['id']}/refresh",
        json={
            "expectedRevision": items[0]["revision"],
            "expectedBatchRevision": opened["revision"],
            "actor": _ACTOR,
        },
    )
    assert first_refresh.status_code == 200, first_refresh.text
    first_result = first_refresh.json()
    assert first_result["item"]["artifactRevision"] == 2
    assert first_result["item"]["strictEvidence"] is True

    current_batch = client.get("/api/final-review-batches/legacy-batch").json()
    second = next(item for item in current_batch["items"] if item["id"] == items[1]["id"])
    assert second["strictEvidence"] is False
    assert second["artifactRevision"] == 1
    second_handoff_response = client.post(
        f"/api/final-review-items/{second['id']}/repair",
        json={
            "expectedRevision": second["revision"],
            "expectedBatchRevision": current_batch["revision"],
            "actor": _ACTOR,
        },
    )
    assert second_handoff_response.status_code == 201, second_handoff_response.text
    second_handoff = second_handoff_response.json()
    _complete_repair_g10(
        app,
        client,
        tmp_path / "legacy-two-repair-2",
        second_handoff,
    )

    database = manifest.parent / "final-review.sqlite3"
    before_failed_refresh = _sqlite_logical_snapshot(database)
    real_history_payload = final_review_service.FinalReviewStore._history_payload
    history_calls = 0

    def fail_after_revision_rows(row):
        nonlocal history_calls
        history_calls += 1
        payload = real_history_payload(row)
        if history_calls == 2:
            raise RuntimeError("injected second-item history failure")
        return payload

    with monkeypatch.context() as scoped:
        scoped.setattr(
            final_review_service.FinalReviewStore,
            "_history_payload",
            staticmethod(fail_after_revision_rows),
        )
        with pytest.raises(RuntimeError, match="injected second-item history failure"):
            client.post(
                f"/api/final-review-items/{second['id']}/refresh",
                json={
                    "expectedRevision": second["revision"],
                    "expectedBatchRevision": current_batch["revision"],
                    "actor": _ACTOR,
                },
            )
    assert history_calls == 2
    assert _sqlite_logical_snapshot(database) == before_failed_refresh
    assert not (review_root / f"images/{second['id']}/r000002").exists()
    assert client.get(old_urls[second["id"]]).content == old_bytes[second["id"]]

    second_refresh = client.post(
        f"/api/final-review-items/{second['id']}/refresh",
        json={
            "expectedRevision": second["revision"],
            "expectedBatchRevision": current_batch["revision"],
            "actor": _ACTOR,
        },
    )
    assert second_refresh.status_code == 200, second_refresh.text
    second_result = second_refresh.json()
    assert second_result["item"]["artifactRevision"] == 2
    assert second_result["item"]["strictEvidence"] is True

    refreshed_items = {
        item["id"]: item
        for item in client.get("/api/final-review-batches/legacy-batch").json()["items"]
    }
    for item_id, old_url in old_urls.items():
        assert client.get(old_url).content == old_bytes[item_id]
        current = refreshed_items[item_id]
        assert current["currentArtifactStale"] is False
        r2 = client.get(current["evidence"]["final"]["url"])
        assert r2.status_code == 200
        assert hashlib.sha256(r2.content).hexdigest() == current["artifactChecksum"]
        missing = client.get(
            f"/api/final-review-items/{item_id}/artifacts/final?artifactRevision=3"
        )
        assert missing.status_code == 404

    latest_batch = client.get("/api/final-review-batches/legacy-batch").json()
    first_current = next(item for item in latest_batch["items"] if item["id"] == items[0]["id"])
    first_approved = client.patch(
        f"/api/final-review-items/{first_current['id']}",
        json={
            "verdict": "approved",
            "issueCodes": [],
            "feedback": "",
            "expectedRevision": first_current["revision"],
            "expectedBatchRevision": latest_batch["revision"],
            "actor": _ACTOR,
        },
    )
    assert first_approved.status_code == 200, first_approved.text
    approved_current = client.get("/api/final-review-batches/legacy-batch").json()["items"][0]
    assert approved_current["verdict"] == "approved"
    assert approved_current["currentArtifactStale"] is False

    after_approval = client.get("/api/final-review-batches/legacy-batch").json()
    second_current = next(item for item in after_approval["items"] if item["id"] == second["id"])
    second_issues = client.patch(
        f"/api/final-review-items/{second_current['id']}",
        json={
            "verdict": "issues",
            "issueCodes": ["translation"],
            "feedback": "legacy page needs another repair",
            "expectedRevision": second_current["revision"],
            "expectedBatchRevision": after_approval["revision"],
            "actor": _ACTOR,
        },
    )
    assert second_issues.status_code == 200, second_issues.text
    second_issues_payload = second_issues.json()
    second_repair = client.post(
        f"/api/final-review-items/{second_current['id']}/repair",
        json={
            "expectedRevision": second_issues_payload["item"]["revision"],
            "expectedBatchRevision": second_issues_payload["batchRevision"],
            "actor": _ACTOR,
        },
    )
    assert second_repair.status_code == 201, second_repair.text
    second_repair_handoff = second_repair.json()
    assert second_repair_handoff["pageGenerationId"] != second_handoff["pageGenerationId"]
    assert second_repair_handoff["repairImageId"] != second_handoff["repairImageId"]

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
        batch_columns = {row["name"] for row in connection.execute("PRAGMA table_info(batches)")}
        item_rows = connection.execute(
            "SELECT id, artifact_revision, strict_evidence FROM items ORDER BY position"
        ).fetchall()
        revision_rows = connection.execute(
            """SELECT item_id, artifact_revision, evidence_json, evidence_digest
            FROM artifact_revisions ORDER BY item_id, artifact_revision"""
        ).fetchall()
    assert {
        "artifact_revision",
        "evidence_json",
        "strict_evidence",
        "evidence_digest",
        "repair_handoff_json",
    }.issubset(item_columns)
    assert "format_version" in batch_columns
    assert [(row["id"], row["artifact_revision"], row["strict_evidence"]) for row in item_rows] == [
        ("legacy-item-1", 2, 1),
        ("legacy-item-2", 2, 1),
    ]
    assert [(row["item_id"], row["artifact_revision"]) for row in revision_rows] == [
        ("legacy-item-1", 1),
        ("legacy-item-1", 2),
        ("legacy-item-2", 1),
        ("legacy-item-2", 2),
    ]
    for row in revision_rows:
        evidence = json.loads(row["evidence_json"])
        assert row["evidence_digest"] == final_review_service._digest(evidence)
        if row["artifact_revision"] == 1:
            assert (
                evidence["final"]["checksum"]
                == hashlib.sha256(old_bytes[row["item_id"]]).hexdigest()
            )


def test_legacy_refresh_failure_rolls_back_lazy_schema_rows_and_orphan(
    app, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project, image = _strict_project(app, client, tmp_path / "source")
    manifest = _legacy_review(
        tmp_path / "legacy-failure",
        source_project_id=project["id"],
        source_image_id=image["id"],
        verdict="issues",
    )
    opened = client.post(
        "/api/final-review-batches/open", json={"manifestPath": str(manifest)}
    ).json()
    item = opened["items"][0]
    handoff = client.post(
        f"/api/final-review-items/{item['id']}/repair",
        json={
            "expectedRevision": item["revision"],
            "expectedBatchRevision": opened["revision"],
            "actor": _ACTOR,
        },
    ).json()
    _complete_repair_g10(app, client, tmp_path / "legacy-repair", handoff)
    database = manifest.parent / "final-review.sqlite3"
    before = _sqlite_logical_snapshot(database)

    def fail_history(_row):
        raise RuntimeError("injected history failure")

    monkeypatch.setattr(
        final_review_service.FinalReviewStore,
        "_history_payload",
        staticmethod(fail_history),
    )
    with pytest.raises(RuntimeError, match="injected history failure"):
        client.post(
            f"/api/final-review-items/{item['id']}/refresh",
            json={
                "expectedRevision": item["revision"],
                "expectedBatchRevision": opened["revision"],
                "actor": _ACTOR,
            },
        )
    assert _sqlite_logical_snapshot(database) == before
    assert not (manifest.parent.parent / f"images/{item['id']}/r000002").exists()


def test_repair_post_project_commit_batch_drift_conflicts_but_preserves_g0(
    app, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    batch = _strict_batch(app, client, tmp_path)
    item = batch["items"][0]
    saved = client.patch(
        f"/api/final-review-items/{item['id']}",
        json={
            "verdict": "issues",
            "issueCodes": ["other"],
            "feedback": "race binding",
            "expectedRevision": item["revision"],
            "expectedBatchRevision": batch["revision"],
            "actor": _ACTOR,
        },
    ).json()
    database = Path(batch["rootPath"]) / "final-review/final-review.sqlite3"
    real_create = final_review_service.create_final_review_repair_generation
    committed: dict[str, object] = {}

    def create_then_drift(*args, **kwargs):
        target, generation = real_create(*args, **kwargs)
        committed["generationId"] = generation.id
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE batches SET revision = revision + 1 WHERE id = ?", (batch["id"],)
            )
        return target, generation

    monkeypatch.setattr(
        final_review_service, "create_final_review_repair_generation", create_then_drift
    )
    request = {
        "expectedRevision": saved["item"]["revision"],
        "expectedBatchRevision": saved["batchRevision"],
        "actor": _ACTOR,
    }
    raced = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert raced.status_code == 409, raced.text
    assert raced.json()["detail"]["resource"] == f"final-review-batch:{batch['id']}"
    assert raced.json()["detail"]["expectedRevision"] == saved["batchRevision"]
    assert raced.json()["detail"]["actualRevision"] == saved["batchRevision"] + 1
    assert "repairImageId" not in raced.text
    generation_id = str(committed["generationId"])
    events = client.get(f"/api/page-generations/{generation_id}/events")
    assert events.status_code == 200
    assert events.json()[0]["gate"] == "G0_identity"

    monkeypatch.setattr(final_review_service, "create_final_review_repair_generation", real_create)
    request["expectedBatchRevision"] += 1
    retry = client.post(f"/api/final-review-items/{item['id']}/repair", json=request)
    assert retry.status_code == 201, retry.text
    assert retry.json()["pageGenerationId"] == generation_id
    assert retry.json()["idempotent"] is True
