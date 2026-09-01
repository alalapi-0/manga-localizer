from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, inspect, select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageCleanPlateCandidate,
    PageCleanPlateReview,
    PageCloudFullPageCandidate,
    PageCloudFullPageReview,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    PageTypesetCandidate,
    Revision,
)
from manga_localizer.services import cloud_full_page_clean_plates as cloud_service
from manga_localizer.services.page_lineage import _append_event
from manga_localizer.services.projects import ProjectError

from .test_page_lineage import (
    _ACTOR,
    _CLEAN_PLATE_CHECKS,
    _current_lineage_context,
    _mutation_lineage,
    _prepare_g7_accepted_page,
)
from .test_typesets import _complete_g9_terminal, _review_body, _run_typeset


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _route_snapshot(store, image_id: str, generation_id: str) -> dict[str, int]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        generation = session.get(PageGeneration, generation_id)
        assert image is not None and generation is not None
        return {
            "projectRevision": store.project(session).revision,
            "imageRevision": image.revision,
            "nextSequence": generation.next_sequence,
            "jobs": session.scalar(select(func.count()).select_from(Job)),
            "jobItems": session.scalar(select(func.count()).select_from(JobItem)),
            "events": session.scalar(select(func.count()).select_from(PageLineageEvent)),
            "revisions": session.scalar(select(func.count()).select_from(Revision)),
            "legacyCandidates": session.scalar(
                select(func.count()).select_from(PageCleanPlateCandidate)
            ),
            "legacyReviews": session.scalar(select(func.count()).select_from(PageCleanPlateReview)),
            "cloudCandidates": session.scalar(
                select(func.count()).select_from(PageCloudFullPageCandidate)
            ),
            "cloudReviews": session.scalar(
                select(func.count()).select_from(PageCloudFullPageReview)
            ),
        }


def _candidate_upload(client: TestClient, prepared: dict[str, object], mutate=None):
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    width = context["targetGrid"]["width"]
    height = context["targetGrid"]["height"]
    quality_path = (
        prepared["store"].root / "generated" / "preprocessed" / image["id"] / "quality.png"
    )
    if not quality_path.is_file():
        # The public context is authoritative; locate its checksum-bound G2 file
        # without encoding any path in uploaded metadata.
        candidates = list((prepared["store"].root / "generated" / "preprocessed").rglob("*.png"))
        quality_path = next(
            path for path in candidates if _sha(path.read_bytes()) == context["qualityChecksum"]
        )
    with Image.open(quality_path) as opened:
        changed = opened.convert("RGB")
    changed.putpixel((0, 0), (1, 2, 3))
    raw = cloud_service._png_bytes(changed)
    normalized, normalization, _, raw_media_type = cloud_service._normalize(raw, (width, height))
    with prepared["store"].session() as session:
        mask_row = session.get(PageMaskArtifact, context["maskArtifactId"])
        assert mask_row is not None
        mask_path = prepared["store"].root / mask_row.relative_path
    delta = cloud_service._delta_manifest(
        quality_path.read_bytes(), normalized, mask_path.read_bytes()
    )
    route = {
        "profile": cloud_service.CLOUD_FULL_PAGE_PROFILE,
        "wholeFrame": True,
        "outsideMaskChangesAllowed": True,
        "normalizationDigest": cloud_service._digest(normalization),
        "deltaDigest": cloud_service._digest(delta),
        "orderedInputDigest": context["orderedInputDigest"],
    }
    metadata = {
        "routeProfile": cloud_service.CLOUD_FULL_PAGE_PROFILE,
        "invocationId": "synthetic-cloud-call-1",
        "promptSha256": "a" * 64,
        "provider": "synthetic-provider",
        "tool": "synthetic-image-edit",
        "modelVersion": "synthetic-v1",
        "claimStatus": cloud_service.CLAIM_STATUS,
        "rawSha256": _sha(raw),
        "rawMediaType": raw_media_type,
        "normalizedSha256": _sha(normalized),
        "normalizationManifest": normalization,
        "normalizationDigest": cloud_service._digest(normalization),
        "deltaManifest": delta,
        "deltaDigest": cloud_service._digest(delta),
        "routeManifest": route,
        "routeChecksum": cloud_service._digest(route),
        "ancestry": {
            "originKind": "direct-ai",
            "providerClaimStatus": cloud_service.CLAIM_STATUS,
            "operatorAttestation": {
                "attested": True,
                "scope": "provider-tool-model-claim",
            },
        },
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(context["generationId"], context["nextSequence"]),
        **{
            key: context[key]
            for key in (
                "projectChecksum",
                "sourceChecksum",
                "g7Checksum",
                "legacyStateChecksum",
                "qualityChecksum",
                "backgroundChecksum",
                "maskArtifactId",
                "maskChecksum",
                "orderedInputs",
                "orderedInputDigest",
            )
        },
    }
    if mutate is not None:
        metadata, raw, normalized = mutate(metadata, raw, normalized)
    response = client.post(
        f"/api/images/{image['id']}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(metadata), "application/json"),
        },
    )
    return response, metadata, raw, normalized


def _start_cloud_route(
    client: TestClient,
    prepared: dict[str, object],
    state: str,
) -> dict[str, object]:
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    uploaded, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert uploaded.status_code == 200, uploaded.text
    candidate = uploaded.json()
    if state == "pending":
        return candidate
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    failed_check = "background-continuous" if state == "rejected" else None
    reviewed = client.patch(
        f"/api/images/{image['id']}/page-gates/cloud-full-page",
        json={
            "candidateId": candidate["candidateId"],
            "observedChecksum": candidate["normalizedChecksum"],
            "decision": "reject" if state == "rejected" else "accept",
            "reason": failed_check or "cloud-full-page-repair-complete",
            "checks": [
                {"check": check, "passed": check != failed_check}
                for check in cloud_service.CLOUD_FULL_PAGE_CHECKS
            ],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    return candidate


def test_cloud_tables_are_additive_and_append_only(tmp_path, client: TestClient, app):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    store = prepared["store"]
    tables = set(inspect(store.engine).get_table_names())
    assert "page_clean_plate_candidates" in tables
    assert "page_cloud_full_page_candidates" in tables
    assert "page_cloud_full_page_reviews" in tables
    triggers = {
        row[0]
        for row in store.engine.connect().exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    assert "page_cloud_full_page_candidates_no_update" in triggers
    assert "page_cloud_full_page_candidates_no_delete" in triggers
    assert "page_cloud_full_page_candidates_validate_insert" in triggers


def test_cloud_ingest_rejects_extra_multipart_parts(tmp_path, client: TestClient, app):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    response, metadata, raw, normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200, response.text
    metadata["invocationId"] = "extra-multipart-part"
    rejected = client.post(
        f"/api/images/{image['id']}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(metadata), "application/json"),
            "prompt": (None, "must-not-be-accepted", "text/plain"),
        },
    )
    assert rejected.status_code == 422


def test_cloud_replay_rejects_orphan_cloud_lineage_event(tmp_path, client: TestClient, app):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    with store.session() as session:
        generation = session.get(PageGeneration, context["generationId"])
        assert generation is not None
        _append_event(
            session,
            generation,
            operation="cloud-full-page-job-enqueued",
            gate="G8_cloudFullPage",
            state="pending",
            actor={"actorId": None, **_ACTOR},
            input_checksum=context["g7Checksum"],
            output_checksum=context["g7Checksum"],
            parent_checksum=context["g7Checksum"],
            stage="inpaint",
            provider="synthetic-provider",
            model_version="synthetic-v1",
            parameter_hash="a" * 64,
            expected_sequence=context["nextSequence"],
            evidence={"routeProfile": cloud_service.CLOUD_FULL_PAGE_PROFILE},
        )
    rejected = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["reason"] == "g8-cloud-replay-invalid"


def test_cloud_whole_page_ingest_is_idempotent_and_acceptance_is_consumable(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    assert isinstance(image, dict)
    response, metadata, raw, normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["deltaManifest"]["outsideMaskChangedPixelCount"] > 0
    with store.session() as session:
        assert len(list(session.scalars(select(PageCloudFullPageCandidate)).all())) == 1
    from manga_localizer.services.exporting import (
        _current_export_clean_path,
        _portable_assets,
        export_image,
        validate_image_export_readiness,
    )

    assert not any(
        "lineage-cloud-full-pages" in destination.as_posix()
        for _source, destination, _checksum in _portable_assets(store)[1]
    )
    blocked = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert blocked.status_code == 409

    retry = client.post(
        f"/api/images/{image['id']}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(metadata), "application/json"),
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["candidateId"] == candidate["candidateId"]

    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    checks = [{"check": check, "passed": True} for check in cloud_service.CLOUD_FULL_PAGE_CHECKS]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/cloud-full-page",
        json={
            "candidateId": candidate["candidateId"],
            "observedChecksum": candidate["normalizedChecksum"],
            "decision": "accept",
            "reason": "cloud-full-page-repair-complete",
            "checks": checks,
            "expectedRevision": context["imageRevision"],
            "lineage": {
                "runId": context["runId"],
                "pageGenerationId": context["generationId"],
                "expectedSequence": context["nextSequence"],
                "actor": _ACTOR,
            },
        },
    )
    assert accepted.status_code == 200, accepted.text
    translation = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert translation.status_code == 200, translation.text
    assert translation.json()["cleanPlateCandidateId"] == candidate["candidateId"]
    assert translation.json()["cleanPlateChecksum"] == candidate["normalizedChecksum"]
    with store.session() as session:
        assert len(list(session.scalars(select(PageCloudFullPageReview)).all())) == 1
        current = cloud_service.current_cloud_full_page_acceptance(
            store,
            session,
            session.get(ImageAsset, image["id"]),
            session.get(PageGeneration, context["generationId"]),
        )
        assert current is not None
        assert current[2].candidate_checksum == candidate["normalizedChecksum"]
        image_row = session.get(ImageAsset, image["id"])
    assert _current_export_clean_path(store, image_row).read_bytes() == normalized
    assert (
        sum(
            "lineage-cloud-full-pages" in destination.as_posix()
            for _source, destination, _checksum in _portable_assets(store)[1]
        )
        == 2
    )
    # The strict cloud review is authoritative.  It must not require a forged
    # legacy inpaint status/review projection before the real export path can
    # consume the accepted whole-page bytes.
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        status = dict(image_row.status)
        status["reviewState"] = "reviewed"
        status["inpaint"] = "pending"
        stage_review_rows = dict(status.get("stageReviews") or {})
        stage_review_rows.pop("inpaint", None)
        status["stageReviews"] = stage_review_rows
        image_row.status = status
    export_reviews = validate_image_export_readiness(
        store,
        image["id"],
        export_format="images",
        image_variant="inpainted",
    )
    assert export_reviews["inpaint"]["artifactChecksum"] == candidate["normalizedChecksum"]
    export_root = tmp_path / "cloud-export"
    exported = export_image(
        store,
        image["id"],
        export_root=export_root,
        export_format="images",
        conflict="rename",
        image_variant="inpainted",
    )
    exported_relative = exported["cleanImage"]["artifact"]
    assert isinstance(exported_relative, str)
    assert (export_root / exported_relative).read_bytes() == normalized


def test_cloud_route_reaches_g10_and_strict_final_review(tmp_path, client: TestClient, app):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)

    uploaded, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert uploaded.status_code == 200, uploaded.text
    cloud_candidate = uploaded.json()
    cloud_context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    cloud_accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/cloud-full-page",
        json={
            "candidateId": cloud_candidate["candidateId"],
            "observedChecksum": cloud_candidate["normalizedChecksum"],
            "decision": "accept",
            "reason": "cloud-full-page-repair-complete",
            "checks": [
                {"check": check, "passed": True} for check in cloud_service.CLOUD_FULL_PAGE_CHECKS
            ],
            "expectedRevision": cloud_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, cloud_context["nextSequence"]),
        },
    )
    assert cloud_accepted.status_code == 200, cloud_accepted.text

    prepared = _complete_g9_terminal(client, prepared)
    g9_context = prepared["g9Context"]
    assert isinstance(g9_context, dict)
    assert g9_context["cleanPlateCandidateId"] == cloud_candidate["candidateId"]
    assert g9_context["cleanPlateChecksum"] == cloud_candidate["normalizedChecksum"]

    _job, typeset_context = _run_typeset(client, app, prepared)
    typeset_candidate = typeset_context["candidates"][0]
    assert typeset_candidate["cleanPlateCandidateId"] == cloud_candidate["candidateId"]
    assert typeset_candidate["cloudFullPageCandidateId"] == cloud_candidate["candidateId"]
    assert typeset_candidate["cleanPlateChecksum"] == cloud_candidate["normalizedChecksum"]
    accepted_typeset = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/"
        f"{typeset_candidate['candidateId']}",
        json=_review_body(typeset_context, typeset_candidate, generation_id),
    )
    assert accepted_typeset.status_code == 200, accepted_typeset.text

    with store.session() as session:
        stored_typeset = session.get(PageTypesetCandidate, typeset_candidate["candidateId"])
        assert stored_typeset is not None
        assert stored_typeset.clean_plate_candidate_id is None
        assert stored_typeset.cloud_full_page_candidate_id == cloud_candidate["candidateId"]

    created = client.post(
        "/api/final-review-batches",
        json={
            "name": "cloud route strict review",
            "outputPath": str(tmp_path / "cloud-final-review"),
            "sourceProjectIds": [project["id"]],
            "expectedItemCount": 1,
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()["items"][0]
    assert item["strictEvidence"] is True
    assert set(item["evidence"]) == {"original", "quality", "mask", "clean", "final"}
    clean = item["evidence"]["clean"]
    final = item["evidence"]["final"]
    assert clean["checksum"] == cloud_candidate["normalizedChecksum"]
    with store.session() as session:
        clean_producer = session.get(PageLineageEvent, clean["producerId"])
        clean_terminal = session.get(PageLineageEvent, clean["terminalId"])
        final_producer = session.get(PageLineageEvent, final["producerId"])
        final_terminal = session.get(PageLineageEvent, final["terminalId"])
        assert clean_producer is not None and clean_terminal is not None
        assert final_producer is not None and final_terminal is not None
        assert clean_producer.gate == clean_terminal.gate == "G8_cloudFullPage"
        assert clean_producer.operation == "cloud-full-page-candidate-produced"
        assert clean_terminal.operation == "cloud-full-page-stage-review"
        assert clean_producer.evidence["candidateId"] == cloud_candidate["candidateId"]
        assert clean_terminal.evidence["candidateId"] == cloud_candidate["candidateId"]
        assert final_producer.gate == final_terminal.gate == "G10_typeset"
        assert final_producer.operation == "typeset-candidate-produced"
        assert final_terminal.operation == "typeset-candidate-reviewed"


@pytest.mark.parametrize("cloud_state", ["pending", "accepted", "rejected"])
def test_legacy_enqueue_is_zero_write_after_cloud_route_started(
    tmp_path, client: TestClient, app, cloud_state: str
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    _start_cloud_route(client, prepared, cloud_state)
    lineage = _current_lineage_context(client, image["id"], generation_id)
    before = _route_snapshot(store, image["id"], generation_id)
    blocked = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={"imageIds": [image["id"]], "options": {}, "lineage": lineage},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g8-cloud-route-started"
    assert _route_snapshot(store, image["id"], generation_id) == before


@pytest.mark.parametrize("mutation", ["fallback", "review"])
def test_legacy_decisions_are_zero_write_after_cloud_route_started(
    tmp_path, client: TestClient, app, mutation: str
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    candidate = _start_cloud_route(client, prepared, "pending")
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    before = _route_snapshot(store, image["id"], generation_id)
    if mutation == "fallback":
        blocked = client.patch(
            f"/api/images/{image['id']}/page-gates/clean-plate/fallback",
            json={
                "enabled": True,
                "reason": "all-ai-candidates-rejected",
                "expectedRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
    else:
        blocked = client.patch(
            f"/api/images/{image['id']}/page-gates/clean-plate",
            json={
                "decision": "accept",
                "reason": "clean-plate-complete",
                "candidateId": candidate["candidateId"],
                "observedCandidateChecksum": candidate["normalizedChecksum"],
                "observedWidth": context["targetGrid"]["width"],
                "observedHeight": context["targetGrid"]["height"],
                "checks": _CLEAN_PLATE_CHECKS,
                "expectedRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g8-cloud-route-started"
    assert _route_snapshot(store, image["id"], generation_id) == before


@pytest.mark.parametrize("field", ["apiKey", "baseUrl", "clientPath", "prompt"])
def test_cloud_ingest_rejects_credentials_paths_and_prompt_body(
    tmp_path, client: TestClient, app, field: str
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    response, metadata, raw, normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200
    metadata["invocationId"] = f"prohibited-{field}"
    metadata[field] = "/private/value" if field == "clientPath" else "not-allowed"
    rejected = client.post(
        f"/api/images/{image['id']}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(metadata), "application/json"),
        },
    )
    assert rejected.status_code == 400


@pytest.mark.parametrize(
    "mutate",
    [
        lambda metadata, raw, normalized: ({**metadata, "extra": "value"}, raw, normalized),
        lambda metadata, raw, normalized: (
            {**metadata, "provider": "/private/provider"},
            raw,
            normalized,
        ),
        lambda metadata, raw, normalized: (
            {
                **metadata,
                "ancestry": {**metadata["ancestry"], "authorization": "secret"},
            },
            raw,
            normalized,
        ),
    ],
)
def test_cloud_ingest_exact_metadata_allowlist_rejects_spoofing(
    tmp_path, client: TestClient, app, mutate
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared, mutate)
    assert response.status_code == 400


def test_cloud_ingest_rejects_byte_limit_canonical_and_cas_mismatches(
    tmp_path, client: TestClient, app, monkeypatch
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    monkeypatch.setattr(cloud_service, "MAX_RAW_BYTES", 1)
    too_large, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert too_large.status_code == 400
    monkeypatch.setattr(cloud_service, "MAX_RAW_BYTES", 32 * 1024 * 1024)

    def canonical_mismatch(metadata, raw, normalized):
        changed = bytearray(normalized)
        changed[-12] ^= 1
        payload = bytes(changed)
        return {**metadata, "normalizedSha256": _sha(payload)}, raw, payload

    mismatch, _metadata, _raw, _normalized = _candidate_upload(
        client, _prepare_g7_accepted_page(client, app, tmp_path / "canonical"), canonical_mismatch
    )
    assert mismatch.status_code == 400

    def cas_mismatch(metadata, raw, normalized):
        return {**metadata, "sourceChecksum": "f" * 64}, raw, normalized

    cas, _metadata, _raw, _normalized = _candidate_upload(
        client, _prepare_g7_accepted_page(client, app, tmp_path / "cas"), cas_mismatch
    )
    assert cas.status_code == 409


@pytest.mark.parametrize("kind", ["checksum", "grid", "aspect"])
def test_cloud_ingest_rejects_checksum_grid_and_aspect_mismatches(
    tmp_path, client: TestClient, app, kind: str
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)

    def mutate(metadata, raw, normalized):
        if kind == "checksum":
            return {**metadata, "rawSha256": "f" * 64}, raw, normalized
        if kind == "grid":
            manifest = {
                **metadata["normalizationManifest"],
                "targetGrid": {"width": 1, "height": 2},
            }
            return {**metadata, "normalizationManifest": manifest}, raw, normalized
        landscape = cloud_service._png_bytes(Image.new("RGB", (200, 100), "white"))
        return (
            {
                **metadata,
                "rawSha256": _sha(landscape),
                "rawMediaType": "image/png",
            },
            landscape,
            normalized,
        )

    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared, mutate)
    assert response.status_code == 400


@pytest.mark.parametrize("target", ["candidate", "event", "revision", "job"])
def test_cloud_replay_fails_closed_on_raw_sql_tamper(
    tmp_path, client: TestClient, app, target: str
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate_id = response.json()["candidateId"]
    with store.session() as session:
        candidate = session.get(PageCloudFullPageCandidate, candidate_id)
        assert candidate is not None
        event = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == candidate.generation_id,
                PageLineageEvent.gate == "G8_cloudFullPage",
            )
        )
        assert event is not None
        identifiers = {
            "candidate": candidate.id,
            "event": event.id,
            "revision": candidate.revision_id,
            "job": candidate.job_id,
        }
    with store.engine.begin() as connection:
        if target == "candidate":
            connection.exec_driver_sql("DROP TRIGGER page_cloud_full_page_candidates_no_update")
            connection.exec_driver_sql(
                "UPDATE page_cloud_full_page_candidates SET provider='tampered' WHERE id=?",
                (identifiers[target],),
            )
        elif target == "event":
            connection.exec_driver_sql("DROP TRIGGER page_lineage_events_no_update")
            connection.exec_driver_sql(
                "UPDATE page_lineage_events SET reason='tampered' WHERE id=?",
                (identifiers[target],),
            )
        elif target == "revision":
            connection.exec_driver_sql("DROP TRIGGER revisions_g8_cloud_no_update")
            connection.exec_driver_sql(
                "UPDATE revisions SET operation='tampered' WHERE id=?", (identifiers[target],)
            )
        else:
            connection.exec_driver_sql(
                "UPDATE jobs SET completed=0 WHERE id=?", (identifiers[target],)
            )
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page")
    assert context.status_code == 409
    assert context.json()["detail"]["reason"] == "g8-cloud-replay-invalid"


def test_rejected_cloud_candidate_blocks_downstream_and_export_selection(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    candidate = response.json()
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    checks = [
        {"check": check, "passed": check != "background-continuous"}
        for check in cloud_service.CLOUD_FULL_PAGE_CHECKS
    ]
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/cloud-full-page",
        json={
            "candidateId": candidate["candidateId"],
            "observedChecksum": candidate["normalizedChecksum"],
            "decision": "reject",
            "reason": "background-continuous",
            "checks": checks,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(context["generationId"], context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert client.get(f"/api/images/{image['id']}/page-gates/translation").status_code == 409
    from manga_localizer.services.exporting import _current_export_clean_path

    with prepared["store"].session() as session:
        image_row = session.get(ImageAsset, image["id"])
    with pytest.raises(ProjectError):
        _current_export_clean_path(prepared["store"], image_row)


def test_cloud_review_requires_ordered_exact_ten_and_truthful_rejection(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    candidate = response.json()
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    checks = [
        {"check": check, "passed": check != "background-continuous"}
        for check in cloud_service.CLOUD_FULL_PAGE_CHECKS
    ]

    def review(test_checks, reason):
        return client.patch(
            f"/api/images/{image['id']}/page-gates/cloud-full-page",
            json={
                "candidateId": candidate["candidateId"],
                "observedChecksum": candidate["normalizedChecksum"],
                "decision": "reject",
                "reason": reason,
                "checks": test_checks,
                "expectedRevision": context["imageRevision"],
                "lineage": _mutation_lineage(context["generationId"], context["nextSequence"]),
            },
        )

    assert review(list(reversed(checks)), "background-continuous").status_code == 409
    assert review(checks, "multiple-visual-failures").status_code == 409
    assert review(checks, "background-continuous").status_code == 200


@pytest.mark.parametrize("execute", [False, True])
def test_cloud_route_rejects_open_or_unreviewed_legacy_prefix(
    tmp_path, client: TestClient, app, execute: bool
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    if execute:
        claimed = app.state.queue._claim_next()
        assert claimed == (prepared["store"], queued.json()["id"])
        asyncio.run(app.state.queue._execute(*claimed))
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "g8-cloud-legacy-prefix-open"


def test_cloud_route_accepts_fully_closed_rejected_legacy_prefix(tmp_path, client: TestClient, app):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    candidate = context["candidates"][0]
    checks = [dict(entry) for entry in _CLEAN_PLATE_CHECKS]
    checks[1]["passed"] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "reject",
            "reason": "residual-text-readable",
            "candidateId": candidate["candidateId"],
            "observedCandidateChecksum": candidate["candidateChecksum"],
            "observedWidth": candidate["width"],
            "observedHeight": candidate["height"],
            "checks": checks,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200, response.text
