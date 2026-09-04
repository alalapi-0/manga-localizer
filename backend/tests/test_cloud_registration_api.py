from __future__ import annotations

import argparse
import email.parser
import io
import json

import cv2
import httpx
import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from manga_localizer import cloud_image_cli as cli
from manga_localizer.database import PageCloudFullPageCandidate, PageMaskArtifact
from manga_localizer.services import cloud_full_page_clean_plates as cloud

from .conftest import create_project, upload_image
from .test_cloud_full_page_clean_plates import _route_snapshot
from .test_cloud_registration import _texture_inputs
from .test_page_lineage import _ACTOR, _prepare_g3_yes_page, _prepare_g7_accepted_page


def _prepared(client, app, tmp_path):
    (data, _, _), _ = _texture_inputs()
    source = create_project(client, tmp_path / "source", "registration-source")
    target = create_project(client, tmp_path / "target", "registration-target")
    source_image = upload_image(client, source["id"], relative_path="texture.png", data=data)
    target_image = upload_image(client, target["id"], relative_path="texture.png", data=data)
    prepared = _prepare_g3_yes_page(
        client,
        app,
        tmp_path,
        prepared={
            "data": data,
            "sourceProject": source,
            "sourceImage": source_image,
            "targetProject": target,
            "targetImage": target_image,
        },
    )
    prepared = _prepare_g7_accepted_page(client, app, tmp_path, prepared=prepared)
    image_id = target_image["id"]
    context = client.get(f"/api/images/{image_id}/page-gates/cloud-full-page").json()
    store = prepared["store"]
    quality = next(
        path.read_bytes()
        for path in (store.root / "generated/preprocessed").rglob("*.png")
        if cloud._sha256(path.read_bytes()) == context["qualityChecksum"]
    )
    with store.session() as session:
        mask_row = session.get(PageMaskArtifact, context["maskArtifactId"])
        mask = (store.root / mask_row.relative_path).read_bytes()
    with Image.open(io.BytesIO(quality)) as image:
        pixels = np.array(image.convert("RGB"))
    raw = cv2.warpAffine(
        pixels, np.array([[1.0, 0.0, 2.0], [0.0, 1.0, -1.0]]), image.size, flags=cv2.INTER_CUBIC
    )
    with Image.open(io.BytesIO(mask)) as image:
        editable = np.array(image) > 0
    raw[editable] = 220
    return prepared, cli.LocalInputs(context, quality, mask), cloud._png_bytes(Image.fromarray(raw))


def _post(client, image_id, metadata, raw, normalized):
    return client.post(
        f"/api/images/{image_id}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(metadata), "application/json"),
        },
    )


def _metadata(local, raw):
    return cli._metadata(
        local=local,
        raw=raw,
        raw_media_type="image/png",
        route=cli._native_route("codex", None),
        quota_class="included",
        invocation_evidence="synthetic-registration-test",
        task_id="test",
        thread_id="test",
        session_id="test",
        normalization_profile=cloud.REGISTRATION_PROFILE,
    )


def test_registration_native_cli_full_ingest_replay_retry_review_and_tamper(tmp_path, client, app):
    prepared, local, raw = _prepared(client, app, tmp_path)
    image_id = prepared["targetImage"]["id"]
    store = prepared["store"]
    raw_path = tmp_path / "raw.png"
    raw_path.write_bytes(raw)
    observed = {}

    def local_transport(request):
        assert request.url.host == "127.0.0.1"
        if request.method == "POST":
            message = email.parser.BytesParser().parsebytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
                + request.content
            )
            observed["metadata"] = json.loads(
                next(
                    part.get_payload(decode=True)
                    for part in message.walk()
                    if part.get_param("name", header="content-disposition") == "metadata"
                )
            )
        result = client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(
            result.status_code, content=result.content, headers=dict(result.headers)
        )

    args = argparse.Namespace(
        api_base="http://127.0.0.1:8000",
        image_id=image_id,
        mode="native",
        runtime="codex",
        raw_image=None,
        prepare_dir=str(tmp_path / "inputs"),
        model_label=None,
        gemini_model=None,
        quota_class=None,
        task_id="test",
        thread_id="test",
        session_id="registration-1",
        execute=False,
        normalization_profile=cloud.REGISTRATION_PROFILE,
    )
    with httpx.Client(transport=httpx.MockTransport(local_transport)) as transport:
        receipt = cli.execute(args, environ={}, local_client=transport)
        manifest = json.loads((tmp_path / "inputs/request.json").read_text())
        assert manifest["normalizationProfile"] == cloud.REGISTRATION_PROFILE
        assert receipt["status"] == "native-generation-prepared"
        args.execute, args.prepare_dir, args.raw_image = True, None, str(raw_path)
        receipt = cli.execute(args, environ={}, local_client=transport)
        metadata = observed["metadata"]
        candidate_id = receipt["candidateId"]
        snapshot = _route_snapshot(store, image_id, local.context["generationId"])
        replay = cli.execute(args, environ={}, local_client=transport)
        assert replay["candidateId"] == candidate_id
        assert _route_snapshot(store, image_id, local.context["generationId"]) == snapshot
    with store.session() as session:
        candidate = session.scalar(select(PageCloudFullPageCandidate))
        assert candidate.normalization_manifest["profile"] == cloud.REGISTRATION_PROFILE
        normalized = (store.root / candidate.normalized_relative_path).read_bytes()
        assert (store.root / candidate.raw_relative_path).read_bytes() == raw
    assert (
        cloud._delta_manifest(local.quality, normalized, local.mask)["outsideMaskChangedPixelCount"]
        == 0
    )
    assert _post(client, image_id, metadata, raw, normalized).status_code == 200  # stale CAS
    for field, value in [
        ("profile", cloud.NORMALIZATION_PROFILE),
        ("registration", {"forged": True}),
    ]:
        changed = json.loads(json.dumps(metadata))
        changed["normalizationManifest"][field] = value
        changed["normalizationDigest"] = cloud._digest(changed["normalizationManifest"])
        response = _post(client, image_id, changed, raw, normalized)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["reason"] == "g8-cloud-invocation-conflict"
        assert _route_snapshot(store, image_id, local.context["generationId"]) == snapshot
    context = client.get(f"/api/images/{image_id}/page-gates/cloud-full-page").json()
    assert client.get(f"/api/images/{image_id}/page-gates/translation").status_code == 409
    accepted = client.patch(
        f"/api/images/{image_id}/page-gates/cloud-full-page",
        json={
            "candidateId": candidate_id,
            "observedChecksum": receipt["normalizedSha256"],
            "decision": "accept",
            "reason": "cloud-full-page-repair-complete",
            "checks": [{"check": check, "passed": True} for check in cloud.CLOUD_FULL_PAGE_CHECKS],
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
    consumed = client.get(f"/api/images/{image_id}/page-gates/translation")
    assert consumed.status_code == 200, consumed.text
    assert consumed.json()["cleanPlateCandidateId"] == candidate_id
    # Only this temporary fixture is tampered; replay must independently fail.
    changed = json.loads(json.dumps(metadata["normalizationManifest"]))
    changed["registration"]["providerToQualityAffine"][0][2] += 1
    with store.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER page_cloud_full_page_candidates_no_update")
        connection.exec_driver_sql(
            "UPDATE page_cloud_full_page_candidates "
            "SET normalization_manifest=?,normalization_digest=? WHERE id=?",
            (json.dumps(changed), cloud._digest(changed), candidate_id),
        )
    response = client.get(f"/api/images/{image_id}/page-gates/cloud-full-page")
    assert response.status_code == 409


def test_registration_rejects_non_native_ingest_and_replay_without_writes(tmp_path, client, app):
    prepared, local, raw = _prepared(client, app, tmp_path)
    image_id = prepared["targetImage"]["id"]
    store = prepared["store"]
    api_metadata, normalized = cli._metadata(
        local=local,
        raw=raw,
        raw_media_type="image/png",
        route=cli._gemini_route("codex", cli.DEFAULT_GEMINI_MODEL),
        quota_class="prepaid",
        invocation_evidence="synthetic-registration-api-rejection",
        task_id="test",
        thread_id="test",
        session_id="test",
        normalization_profile=cloud.REGISTRATION_PROFILE,
    )
    before = _route_snapshot(store, image_id, local.context["generationId"])
    before_files = {str(path) for path in store.generated_root.rglob("*")}
    rejected = _post(client, image_id, api_metadata, raw, normalized)
    assert rejected.status_code == 400, rejected.text
    assert "requires a native subscription route" in rejected.text
    assert _route_snapshot(store, image_id, local.context["generationId"]) == before
    assert {str(path) for path in store.generated_root.rglob("*")} == before_files

    native_metadata, native_normalized = _metadata(local, raw)
    accepted = _post(client, image_id, native_metadata, raw, native_normalized)
    assert accepted.status_code == 200, accepted.text
    before = _route_snapshot(store, image_id, local.context["generationId"])
    with store.session() as session:
        candidate = session.scalar(select(PageCloudFullPageCandidate))
        assert candidate is not None
        session.expunge(candidate)  # Only mutate a detached synthetic record in memory.
    candidate.route_manifest = {
        **candidate.route_manifest,
        "providerParameters": api_metadata["providerParameters"],
    }
    candidate.route_checksum = cloud._digest(candidate.route_manifest)
    with pytest.raises(cloud.PageLineageConflict, match="normalization does not replay"):
        cloud._validate_candidate_evidence(
            store,
            candidate,
            quality_bytes=local.quality,
            mask_bytes=local.mask,
            ordered_inputs=local.context["orderedInputs"],
        )
    assert _route_snapshot(store, image_id, local.context["generationId"]) == before


def test_registration_first_ingest_recomputes_every_evidence_field(tmp_path, client, app):
    prepared, local, raw = _prepared(client, app, tmp_path)
    image_id = prepared["targetImage"]["id"]
    metadata, normalized = _metadata(local, raw)
    snapshot = _route_snapshot(prepared["store"], image_id, local.context["generationId"])
    mutations = [
        ("providerToQualityAffine", [[1, 0, 7], [0, 1, 0]]),
        ("validationCount", 99999),
        ("qualitySha256", "a" * 64),
        ("trainingMatchesSha256", "b" * 64),
        ("dependencies", {}),
    ]
    for key, value in mutations:
        changed = json.loads(json.dumps(metadata))
        changed["normalizationManifest"]["registration"][key] = value
        changed["normalizationDigest"] = cloud._digest(changed["normalizationManifest"])
        changed["routeManifest"]["normalizationDigest"] = changed["normalizationDigest"]
        changed["routeChecksum"] = cloud._digest(changed["routeManifest"])
        response = _post(client, image_id, changed, raw, normalized)
        assert response.status_code == 400, response.text
        assert (
            _route_snapshot(prepared["store"], image_id, local.context["generationId"]) == snapshot
        )
    for value in [None, [], {"profile": "unknown"}]:
        changed = {**metadata, "normalizationManifest": value}
        assert _post(client, image_id, changed, raw, normalized).status_code == 400
        assert (
            _route_snapshot(prepared["store"], image_id, local.context["generationId"]) == snapshot
        )
    response = _post(client, image_id, metadata, raw, normalized)
    assert response.status_code == 200, response.text
