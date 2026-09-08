from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from manga_localizer.database import (
    ImageAsset,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    PageMaskReview,
    Revision,
)

from .conftest import create_project, upload_image
from .test_page_lineage import (
    _OCR_QC_CHECKS,
    _accept_current_g7_artifact,
    _accept_g4_from_latest_mutation,
    _current_lineage_context,
    _enqueue_g7,
    _mutation_lineage,
    _prepare_g3_yes_page,
    _prepare_g6_accepted_page,
    _save_g7_default_draft,
    _StrictLineageOCR,
)


def _edge_glyph_page() -> bytes:
    image = Image.new("RGB", (120, 100), "white")
    draw = ImageDraw.Draw(image)
    # Detector fixture: (20, 30)-(100, 70). The long vowel, edge glyph, and
    # antialias halo deliberately extend beyond that accepted G4 box.
    draw.rectangle((30, 34, 78, 65), fill="black")
    draw.rectangle((86, 20, 94, 91), fill=(175, 175, 175))
    draw.rectangle((88, 22, 92, 89), fill="black")
    draw.rectangle((97, 46, 109, 57), fill=(190, 190, 190))
    draw.rectangle((99, 48, 107, 55), fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _history_counts(store) -> tuple[int, int, int, int]:
    with store.session() as session:
        return (
            session.scalar(select(func.count()).select_from(PageLineageEvent)) or 0,
            session.scalar(select(func.count()).select_from(PageMaskArtifact)) or 0,
            session.scalar(select(func.count()).select_from(PageMaskReview)) or 0,
            session.scalar(select(func.count()).select_from(Revision)) or 0,
        )


def _reaccept_g5_g6(
    client: TestClient,
    app,
    prepared: dict[str, object],
    expanded_region: dict[str, object],
) -> None:
    image = prepared["targetImage"]
    project = prepared["targetProject"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict) and isinstance(project, dict)

    background = client.get(f"/api/images/{image['id']}/page-gates/background").json()
    classified = client.patch(
        f"/api/regions/{expanded_region['id']}/background-classification",
        json={
            "category": "illustration/character",
            "confidence": 0,
            "rationaleCodes": ["character-or-illustration-detail"],
            "expectedRevision": expanded_region["revision"],
            "expectedImageRevision": background["imageRevision"],
            "lineage": _mutation_lineage(generation_id, background["nextSequence"]),
        },
    )
    assert classified.status_code == 200, classified.text
    background = client.get(f"/api/images/{image['id']}/page-gates/background").json()
    accepted_g5 = client.patch(
        f"/api/images/{image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": "all-eligible-backgrounds-reviewed",
            "observedBackgroundChecksum": background["backgroundChecksum"],
            "expectedRevision": background["imageRevision"],
            "lineage": _mutation_lineage(generation_id, background["nextSequence"]),
        },
    )
    assert accepted_g5.status_code == 200, accepted_g5.text

    app.state.providers.ocr = _StrictLineageOCR()
    queued = client.post(
        f"/api/projects/{project['id']}/ocr",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    ocr = client.get(f"/api/images/{image['id']}/page-gates/ocr").json()
    region = client.get(f"/api/images/{image['id']}/regions").json()[0]
    attempt = next(
        row
        for row in ocr["attempts"]
        if row["regionId"] == region["id"] and row["inputVariant"] == "quality"
    )
    reviewed = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json={
            "sourceText": attempt["text"],
            "sourceMode": "quality-attempt",
            "selectedAttemptId": attempt["id"],
            "qcChecks": _OCR_QC_CHECKS,
            "expectedRevision": region["revision"],
            "expectedImageRevision": ocr["imageRevision"],
            "lineage": _mutation_lineage(generation_id, ocr["nextSequence"]),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    ocr = client.get(f"/api/images/{image['id']}/page-gates/ocr").json()
    accepted_g6 = client.patch(
        f"/api/images/{image['id']}/page-gates/ocr",
        json={
            "decision": "accept",
            "reason": "all-translatable-source-text-reviewed",
            "observedOcrChecksum": ocr["ocrChecksum"],
            "expectedRevision": ocr["imageRevision"],
            "lineage": _mutation_lineage(generation_id, ocr["nextSequence"]),
        },
    )
    assert accepted_g6.status_code == 200, accepted_g6.text


def test_g4_edge_expansion_revalidates_downstream_and_replaces_g7_without_history_loss(
    client: TestClient, app, tmp_path: Path
) -> None:
    data = _edge_glyph_page()
    source_project = create_project(client, tmp_path / "source", "source")
    source_image = upload_image(
        client, source_project["id"], relative_path="chapter/edge.png", data=data
    )
    target_project = create_project(client, tmp_path / "target", "target")
    target_image = upload_image(
        client, target_project["id"], relative_path="chapter/edge.png", data=data
    )
    prepared = _prepare_g3_yes_page(
        client,
        app,
        tmp_path,
        prepared={
            "data": data,
            "sourceProject": source_project,
            "sourceImage": source_image,
            "targetProject": target_project,
            "targetImage": target_image,
        },
    )
    prepared = _prepare_g6_accepted_page(
        client,
        app,
        tmp_path,
        background_category="illustration/character",
        prepared=prepared,
    )
    image = prepared["targetImage"]
    project = prepared["targetProject"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict) and isinstance(project, dict)

    _save_g7_default_draft(client, prepared)
    first_job = _enqueue_g7(client, prepared)
    assert first_job.status_code == 202, first_job.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, first_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    first_acceptance = _accept_current_g7_artifact(client, prepared)
    first_artifact_id = first_acceptance["event"]["evidence"]["artifactId"]
    first_mask = client.get(
        f"/api/images/{image['id']}/page-gates/mask/artifacts/{first_artifact_id}"
    )
    with Image.open(io.BytesIO(first_mask.content)) as opened:
        first_pixels = np.asarray(opened.convert("L"))
    source_pixels = np.asarray(Image.open(io.BytesIO(data)).convert("L"))
    mark = source_pixels < 220
    assert int(((first_pixels <= 127) & mark).sum()) > 0

    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    before_automatic_g7_reopen = _history_counts(store)
    automatic_g7_reopen = client.post(
        f"/api/images/{image['id']}/page-gates/mask/reopen",
        json={
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert automatic_g7_reopen.status_code == 409, automatic_g7_reopen.text
    assert automatic_g7_reopen.json()["detail"]["reason"] == "g7-mask-coverage-complete"
    assert _history_counts(store) == before_automatic_g7_reopen

    reopen_payload = {
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
    }
    reopened = client.post(
        f"/api/images/{image['id']}/page-gates/regions/reopen", json=reopen_payload
    )
    assert reopened.status_code == 200, reopened.text
    assert prepared["region"]["id"] in reopened.json()["leftoverRegionIds"]

    after_reopen = _history_counts(store)
    stale = client.post(f"/api/images/{image['id']}/page-gates/regions/reopen", json=reopen_payload)
    assert stale.status_code == 409, stale.text
    duplicate = client.post(
        f"/api/images/{image['id']}/page-gates/regions/reopen",
        json={
            "expectedRevision": reopened.json()["imageRevision"],
            "lineage": _mutation_lineage(generation_id, reopened.json()["nextSequence"]),
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["reason"] == "g4-regions-amendment-open"
    assert _history_counts(store) == after_reopen

    current_region = client.get(f"/api/images/{image['id']}/regions").json()[0]
    expanded = client.patch(
        f"/api/regions/{current_region['id']}",
        json={
            "x": 16,
            "y": 16,
            "width": 98,
            "height": 78,
            "expectedRevision": current_region["revision"],
            "expectedImageRevision": reopened.json()["imageRevision"],
            "lineage": _mutation_lineage(generation_id, reopened.json()["nextSequence"]),
        },
    )
    assert expanded.status_code == 200, expanded.text
    _accept_g4_from_latest_mutation(
        client,
        image_id=str(image["id"]),
        project_id=str(project["id"]),
        generation_id=generation_id,
    )
    _reaccept_g5_g6(client, app, prepared, expanded.json())

    _save_g7_default_draft(client, prepared)
    second_job = _enqueue_g7(client, prepared)
    assert second_job.status_code == 202, second_job.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, second_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    second_acceptance = _accept_current_g7_artifact(client, prepared)
    second_artifact_id = second_acceptance["event"]["evidence"]["artifactId"]
    assert second_artifact_id != first_artifact_id

    second_mask = client.get(
        f"/api/images/{image['id']}/page-gates/mask/artifacts/{second_artifact_id}"
    )
    with Image.open(io.BytesIO(second_mask.content)) as opened:
        second_pixels = np.asarray(opened.convert("L"))
    assert bool(np.all(second_pixels[mark] > 127))

    with store.session() as session:
        assert session.get(PageMaskArtifact, first_artifact_id) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageMaskArtifact)
                .where(PageMaskArtifact.generation_id == generation_id)
            )
            == 2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageMaskReview)
                .where(PageMaskReview.generation_id == generation_id)
            )
            == 2
        )
        generation = session.get(PageGeneration, generation_id)
        image_row = session.get(ImageAsset, image["id"])
        assert generation is not None and image_row is not None
        assert generation.source_checksum == hashlib.sha256(data).hexdigest()
        assert generation.state == "active"

    final_context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    before_complete_retry = _history_counts(store)
    complete = client.post(
        f"/api/images/{image['id']}/page-gates/regions/reopen",
        json={
            "expectedRevision": final_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, final_context["nextSequence"]),
        },
    )
    assert complete.status_code == 409, complete.text
    assert complete.json()["detail"]["reason"] == "g4-missed-box-complete"
    assert _history_counts(store) == before_complete_retry


def test_g7_owner_issue_reopen_requires_explicit_purpose_and_is_cas_safe(
    client: TestClient, app, tmp_path: Path, monkeypatch
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)

    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    _accept_current_g7_artifact(client, prepared)

    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    payload = {
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
    }
    before = _history_counts(store)
    blocked = client.post(f"/api/images/{image['id']}/page-gates/mask/reopen", json=payload)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g7-mask-coverage-complete"
    assert _history_counts(store) == before

    monkeypatch.setattr(
        "manga_localizer.main.g8_replace_accepted_for_linked_item",
        lambda _store, _lineage, item_verdict_lookup=None: True,
    )
    reopened = client.post(f"/api/images/{image['id']}/page-gates/mask/reopen", json=payload)
    assert reopened.status_code == 200, reopened.text
    after_reopen = _history_counts(store)

    stale = client.post(f"/api/images/{image['id']}/page-gates/mask/reopen", json=payload)
    assert stale.status_code == 409, stale.text
    duplicate = client.post(
        f"/api/images/{image['id']}/page-gates/mask/reopen",
        json={
            "expectedRevision": reopened.json()["imageRevision"],
            "lineage": _mutation_lineage(generation_id, reopened.json()["nextSequence"]),
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["reason"] == "g7-mask-amendment-open"
    assert _history_counts(store) == after_reopen
