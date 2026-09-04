from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3

import numpy as np
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

from .conftest import create_project, png_bytes, upload_image
from .legacy_g8_fixture import historical_local_g8
from .test_page_lineage import (
    _ACTOR,
    _CLEAN_PLATE_CHECKS,
    _current_lineage_context,
    _mutation_lineage,
    _prepare_g3_yes_page,
    _prepare_g7_accepted_page,
)
from .test_typesets import _complete_g9_terminal, _review_body, _run_typeset


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("size", [(4, 6), (6, 4), (4, 4)])
def test_cloud_normalization_accepts_portrait_landscape_and_square(
    size: tuple[int, int],
):
    raw = cloud_service._png_bytes(Image.new("RGB", size, "white"))
    normalized, manifest, raw_grid, media_type = cloud_service._normalize(raw, size)
    assert raw_grid == size
    assert manifest["sourceGrid"] == {"width": size[0], "height": size[1]}
    assert manifest["targetGrid"] == {"width": size[0], "height": size[1]}
    assert media_type == "image/png"
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == size


def test_cloud_normalization_retains_pixel_cap_and_aspect_gate(monkeypatch):
    raw = cloud_service._png_bytes(Image.new("RGB", (6, 4), "white"))
    monkeypatch.setattr(cloud_service, "MAX_RASTER_PIXELS", 23)
    with pytest.raises(ProjectError, match="pixel limit"):
        cloud_service._normalize(raw, (6, 4))
    monkeypatch.setattr(cloud_service, "MAX_RASTER_PIXELS", 32_000_000)
    with pytest.raises(ProjectError, match="aspect"):
        cloud_service._normalize(raw, (4, 6))


def test_cloud_normalization_cover_crops_discrete_native_bucket():
    raw = cloud_service._png_bytes(Image.new("RGB", (1536, 1024), "white"))
    target = (2380, 1332)
    with pytest.raises(ProjectError, match="aspect"):
        cloud_service._normalize(raw, target)
    normalized, manifest, raw_grid, media_type = cloud_service._normalize(
        raw, target, fit=cloud_service.FIT_COVER_CROP
    )
    assert raw_grid == (1536, 1024)
    assert media_type == "image/png"
    assert manifest["crop"] is True
    assert manifest["sourceGrid"] == {"width": 1536, "height": 1024}
    assert manifest["targetGrid"] == {"width": 2380, "height": 1332}
    fitted = (manifest["fittedGrid"]["width"], manifest["fittedGrid"]["height"])
    assert cloud_service._aspect_error(*fitted, target) <= cloud_service.ASPECT_LIMIT
    box = manifest["cropBox"]
    assert box["width"] == fitted[0]
    assert box["height"] == fitted[1]
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= 1536
    assert box["y"] + box["height"] <= 1024
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == target
    same_aspect = cloud_service._png_bytes(Image.new("RGB", target, "white"))
    _normalized, unchanged, _grid, _media = cloud_service._normalize(
        same_aspect, target, fit=cloud_service.FIT_COVER_CROP
    )
    assert unchanged["crop"] is False
    assert "fittedGrid" not in unchanged
    unfittable = cloud_service._png_bytes(Image.new("RGB", (6, 4), "white"))
    with pytest.raises(ProjectError, match="aspect"):
        cloud_service._normalize(unfittable, (4, 6), fit=cloud_service.FIT_COVER_CROP)


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


def _candidate_upload(
    client: TestClient,
    prepared: dict[str, object],
    mutate=None,
    *,
    raw_grid: tuple[int, int] | None = None,
    invocation_id: str = "synthetic-cloud-call-1",
    inside_rgb: tuple[int, int, int] = (1, 2, 3),
):
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    width = context["targetGrid"]["width"]
    height = context["targetGrid"]["height"]
    quality_response = client.get(f"/api/images/{image['id']}/generated/quality")
    assert quality_response.status_code == 200, quality_response.text
    quality_bytes = quality_response.content
    assert _sha(quality_bytes) == context["qualityChecksum"]
    with Image.open(io.BytesIO(quality_bytes)) as opened:
        changed = opened.convert("RGB")
    with prepared["store"].session() as session:
        mask_row = session.get(PageMaskArtifact, context["maskArtifactId"])
        assert mask_row is not None
        mask_path = prepared["store"].root / mask_row.relative_path
    mask_bytes = mask_path.read_bytes()
    with Image.open(mask_path) as mask_image:
        mask = np.asarray(mask_image.convert("L"), dtype=np.uint8)
    inside_y, inside_x = np.argwhere(mask > 0)[0]
    outside_y, outside_x = np.argwhere(mask == 0)[0]
    changed.putpixel((int(inside_x), int(inside_y)), inside_rgb)
    changed.putpixel((int(outside_x), int(outside_y)), (4, 5, 6))
    if raw_grid is None:
        raw = cloud_service._png_bytes(changed)
        provider_normalized, normalization, _, raw_media_type = cloud_service._normalize(
            raw, (width, height)
        )
    else:
        raw = cloud_service._png_bytes(Image.new("RGB", raw_grid, "white"))
        provider_normalized, normalization, _, raw_media_type = (
            cloud_service._normalize_for_profile(
                raw,
                (width, height),
                quality=quality_bytes,
                mask=mask_bytes,
            )
        )
    normalized, composite = cloud_service._strict_mask_composite(
        quality_bytes, provider_normalized, mask_bytes
    )
    delta = cloud_service._delta_manifest(quality_bytes, normalized, mask_bytes)
    route = cloud_service._strict_route_manifest(
        normalization,
        composite,
        delta,
        context["orderedInputs"],
        quota_class="included",
        provider_parameters={
            "apiProfile": "synthetic-image-edit-v1",
            "responseMimeType": "image/png",
            "inputRoles": ["quality-plate", "accepted-g7-mask"],
            "outputCount": 1,
        },
    )
    metadata = {
        "routeProfile": cloud_service.CLOUD_FULL_PAGE_PROFILE,
        "invocationId": invocation_id,
        "promptSha256": "a" * 64,
        "provider": "synthetic-provider",
        "tool": "synthetic-image-edit",
        "modelVersion": "synthetic-v1",
        "quotaClass": "included",
        "providerParameters": route["providerParameters"],
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
        "ancestry": cloud_service._ANCESTRY,
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


def test_cloud_ingest_accepts_cover_cropped_native_bucket(tmp_path, client: TestClient, app):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    side = max(context["targetGrid"]["width"], context["targetGrid"]["height"]) + 80
    response, metadata, raw, _normalized = _candidate_upload(
        client,
        prepared,
        raw_grid=(side, side),
        invocation_id="synthetic-cloud-cover-crop-1",
    )
    assert response.status_code == 200, response.text
    assert metadata["normalizationManifest"]["crop"] is True
    with Image.open(io.BytesIO(raw)) as opened:
        assert opened.size == (side, side)
    with prepared["store"].session() as session:
        candidate = session.get(PageCloudFullPageCandidate, response.json()["candidateId"])
        assert candidate is not None
        assert candidate.raw_width == side
        assert candidate.raw_height == side
        assert candidate.normalized_width == context["targetGrid"]["width"]
        assert candidate.normalized_height == context["targetGrid"]["height"]
        assert candidate.normalization_manifest["crop"] is True
        fitted = candidate.normalization_manifest["fittedGrid"]
        assert (
            cloud_service._aspect_error(
                fitted["width"],
                fitted["height"],
                (candidate.normalized_width, candidate.normalized_height),
            )
            <= cloud_service.ASPECT_LIMIT
        )


def test_cloud_ingest_cover_crop_trigger_still_enforces_fitted_aspect(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)

    def mutate(metadata, raw, normalized):
        manifest = {
            **metadata["normalizationManifest"],
            "fittedGrid": {"width": 6, "height": 4},
            "cropBox": {**metadata["normalizationManifest"]["cropBox"], "width": 6, "height": 4},
        }
        return {**metadata, "normalizationManifest": manifest}, raw, normalized

    image = prepared["targetImage"]
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    side = max(context["targetGrid"]["width"], context["targetGrid"]["height"]) + 80
    response, _metadata, _raw, _normalized = _candidate_upload(
        client,
        prepared,
        mutate,
        raw_grid=(side, side),
        invocation_id="synthetic-cloud-cover-crop-tamper-1",
    )
    assert response.status_code == 400


def test_cloud_candidate_trigger_rejects_incomplete_or_extended_route_manifest(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate_id = response.json()["candidateId"]
    store = prepared["store"]

    with sqlite3.connect(store.database_path) as database:
        database.row_factory = sqlite3.Row
        database.execute("BEGIN")
        row = database.execute(
            "SELECT * FROM page_cloud_full_page_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        assert row is not None
        values = dict(row)
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        database.execute("DROP TRIGGER page_cloud_full_page_candidates_no_delete")
        database.execute(
            "DELETE FROM page_cloud_full_page_candidates WHERE id = ?",
            (candidate_id,),
        )

        route = json.loads(values["route_manifest"])
        mutations = []
        for field in route:
            changed = json.loads(json.dumps(route))
            changed.pop(field)
            mutations.append((f"missing route field {field}", changed))
        changed = json.loads(json.dumps(route))
        changed["unexpectedRouteField"] = True
        mutations.append(("unexpected route field", changed))

        composite = route["compositeManifest"]
        for field in composite:
            changed = json.loads(json.dumps(route))
            changed["compositeManifest"].pop(field)
            mutations.append((f"missing composite field {field}", changed))
        changed = json.loads(json.dumps(route))
        changed["compositeManifest"]["unexpectedCompositeField"] = True
        mutations.append(("unexpected composite field", changed))

        for field in ("width", "height"):
            changed = json.loads(json.dumps(route))
            changed["compositeManifest"]["targetGrid"].pop(field)
            mutations.append((f"missing composite target-grid field {field}", changed))
        changed = json.loads(json.dumps(route))
        changed["compositeManifest"]["targetGrid"]["unexpected"] = 1
        mutations.append(("unexpected composite target-grid field", changed))
        for field in ("width", "height"):
            changed = json.loads(json.dumps(route))
            changed["compositeManifest"]["targetGrid"][field] += 1
            mutations.append((f"mismatched composite target-grid {field}", changed))

        for field in (
            "providerParameterDigest",
            "normalizationDigest",
            "compositeDigest",
            "deltaDigest",
            "orderedInputDigest",
        ):
            changed = json.loads(json.dumps(route))
            changed[field] = "g" * 64
            mutations.append((f"invalid route digest {field}", changed))

        for field in ("normalizationDigest", "deltaDigest", "orderedInputDigest"):
            changed = json.loads(json.dumps(route))
            changed[field] = "0" * 64 if changed[field] != "0" * 64 else "1" * 64
            mutations.append((f"mismatched route digest {field}", changed))

        changed = json.loads(json.dumps(route))
        changed["providerParameterDigest"] = "a" * 63
        mutations.append(("short provider-parameter digest", changed))
        changed = json.loads(json.dumps(route))
        changed["compositeDigest"] = "A" * 64
        mutations.append(("uppercase composite digest", changed))

        changed = json.loads(json.dumps(route))
        changed["providerParameters"]["unexpected"] = True
        mutations.append(("unexpected provider-parameter field", changed))
        changed = json.loads(json.dumps(route))
        changed["providerParameters"].pop("apiProfile")
        changed["providerParameters"]["unexpected"] = "replacement"
        mutations.append(("unknown provider-parameter substitution", changed))
        changed = json.loads(json.dumps(route))
        changed["providerParameters"]["apiProfile"] = "_invalid-first-character"
        mutations.append(("invalid provider API profile", changed))
        changed = json.loads(json.dumps(route))
        changed["providerParameters"]["outputCount"] = True
        mutations.append(("boolean provider output count", changed))

        for field, value in (
            ("qualitySha256", "0" * 64),
            ("maskSha256", "0" * 64),
            ("providerNormalizedSha256", "g" * 64),
            ("maskRule", "different-mask-rule"),
            ("outsideMaskSource", "provider-output"),
            ("output", "different-output"),
        ):
            changed = json.loads(json.dumps(route))
            changed["compositeManifest"][field] = value
            mutations.append((f"invalid composite field {field}", changed))

        for label, changed in mutations:
            values["route_manifest"] = json.dumps(changed, separators=(",", ":"))
            try:
                database.execute(
                    f"INSERT INTO page_cloud_full_page_candidates "
                    f"({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(values[column] for column in columns),
                )
            except sqlite3.IntegrityError as error:
                assert "candidate manifests" in str(error), label
            else:
                pytest.fail(f"trigger accepted {label}")
        database.rollback()


def _convert_candidate_to_pre_strict(
    prepared: dict[str, object],
    *,
    candidate_id: str,
    strict_metadata: dict[str, object],
    raw: bytes,
) -> dict[str, object]:
    """Rewrite one temporary candidate to the exact pre-strict immutable format."""
    store = prepared["store"]
    with store.session() as session:
        candidate = session.get(PageCloudFullPageCandidate, candidate_id)
        assert candidate is not None
        mask_row = session.get(PageMaskArtifact, candidate.mask_artifact_id)
        revision = session.get(Revision, candidate.revision_id)
        item = session.get(JobItem, candidate.job_item_id)
        assert mask_row is not None and revision is not None and item is not None
        quality_path = next(
            path
            for path in (store.root / "generated" / "preprocessed").rglob("*.png")
            if _sha(path.read_bytes()) == candidate.quality_checksum
        )
        quality_bytes = quality_path.read_bytes()
        mask_bytes = (store.root / mask_row.relative_path).read_bytes()
        provider_normalized, normalization, _grid, _media_type = cloud_service._normalize(
            raw, (candidate.normalized_width, candidate.normalized_height)
        )
        delta = cloud_service._delta_manifest(quality_bytes, provider_normalized, mask_bytes)
        route = cloud_service._legacy_route_manifest(
            normalization,
            delta,
            candidate.ordered_input_manifest,
        )
        legacy_metadata = json.loads(json.dumps(strict_metadata))
        legacy_metadata.update(
            {
                "normalizedSha256": _sha(provider_normalized),
                "deltaManifest": delta,
                "deltaDigest": cloud_service._digest(delta),
                "routeManifest": route,
                "routeChecksum": cloud_service._digest(route),
                "ancestry": cloud_service._LEGACY_ANCESTRY,
            }
        )
        legacy_metadata.pop("quotaClass")
        legacy_metadata.pop("providerParameters")
        actor = legacy_metadata["lineage"]["actor"]
        legacy_metadata["lineage"]["actor"] = {
            key: actor.get(key) for key in sorted(cloud_service._ACTOR_KEYS)
        }
        parameter_hash = cloud_service._digest(
            {
                "metadata": legacy_metadata,
                "raw": _sha(raw),
                "normalized": _sha(provider_normalized),
            }
        )

        connection = session.connection()
        trigger_names = (
            "page_cloud_full_page_candidates_no_update",
            "page_lineage_events_no_update",
            "revisions_g8_cloud_no_update",
        )
        trigger_sql = {
            name: sql
            for name, sql in connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (?, ?, ?)",
                trigger_names,
            ).all()
        }
        assert set(trigger_sql) == set(trigger_names)
        for name in trigger_names:
            connection.exec_driver_sql(f"DROP TRIGGER {name}")

        candidate.normalized_checksum = _sha(provider_normalized)
        candidate.normalization_manifest = normalization
        candidate.normalization_digest = cloud_service._digest(normalization)
        candidate.delta_manifest = delta
        candidate.delta_digest = cloud_service._digest(delta)
        candidate.route_manifest = route
        candidate.route_checksum = cloud_service._digest(route)
        candidate.parameter_hash = parameter_hash
        candidate.ancestry = cloud_service._LEGACY_ANCESTRY
        item.output = {
            "candidateId": candidate.id,
            "rawChecksum": candidate.raw_checksum,
            "normalizedChecksum": candidate.normalized_checksum,
            "routeChecksum": candidate.route_checksum,
        }
        revision.after = cloud_service._candidate_revision_after(
            candidate_id=candidate.id,
            generation_id=candidate.generation_id,
            image_id=candidate.image_id,
            job_id=candidate.job_id,
            job_item_id=candidate.job_item_id,
            raw_checksum=candidate.raw_checksum,
            normalized_checksum=candidate.normalized_checksum,
            request_revision=revision.after["requestRevision"],
        )
        candidate.state_checksum = cloud_service._cloud_state(
            candidate.legacy_state_checksum, [candidate], []
        )
        events = list(
            session.scalars(
                select(PageLineageEvent)
                .where(PageLineageEvent.generation_id == candidate.generation_id)
                .where(PageLineageEvent.gate == "G8_cloudFullPage")
                .order_by(PageLineageEvent.sequence)
            ).all()
        )
        assert [event.operation for event in events] == [
            "cloud-full-page-job-enqueued",
            "cloud-full-page-candidate-produced",
            "cloud-full-page-job-completed",
        ]
        for index, event in enumerate(events):
            event.parameter_hash = parameter_hash
            event.input_checksum = (
                candidate.legacy_state_checksum if index < 2 else candidate.state_checksum
            )
            event.output_checksum = (
                candidate.legacy_state_checksum if index == 0 else candidate.state_checksum
            )
            event.evidence = {
                "eventType": event.operation,
                "routeProfile": cloud_service.CLOUD_FULL_PAGE_PROFILE,
                "claimStatus": cloud_service.CLAIM_STATUS,
                "candidateId": candidate.id,
                "rawChecksum": candidate.raw_checksum,
                "normalizedChecksum": candidate.normalized_checksum,
                "routeChecksum": candidate.route_checksum,
                "stateChecksum": candidate.state_checksum,
            }
        session.flush()
        for name in trigger_names:
            connection.exec_driver_sql(trigger_sql[name])
        normalized_path = store.root / candidate.normalized_relative_path
        normalized_path.write_bytes(provider_normalized)
        return {
            "candidateId": candidate.id,
            "normalizedChecksum": candidate.normalized_checksum,
            "parameterHash": parameter_hash,
            "routeManifest": route,
            "outsideMaskChangedPixelCount": delta["outsideMaskChangedPixelCount"],
        }


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


def test_cloud_landscape_candidate_passes_trigger_and_replays(tmp_path, client: TestClient, app):
    data = png_bytes(size=(320, 240), color="white", rectangle=(20, 20, 100, 80))
    source_project = create_project(client, tmp_path / "source", "landscape-source")
    source_image = upload_image(
        client,
        source_project["id"],
        relative_path="chapter/landscape.png",
        data=data,
    )
    target_project = create_project(client, tmp_path / "target", "landscape-target")
    target_image = upload_image(
        client,
        target_project["id"],
        relative_path="chapter/landscape.png",
        data=data,
    )
    prepared = _prepare_g3_yes_page(
        client,
        app,
        tmp_path / "lineage",
        prepared={
            "data": data,
            "sourceProject": source_project,
            "sourceImage": source_image,
            "targetProject": target_project,
            "targetImage": target_image,
        },
    )
    prepared = _prepare_g7_accepted_page(
        client,
        app,
        tmp_path / "lineage",
        prepared=prepared,
    )
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    response, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert (candidate["width"], candidate["height"]) == (320, 240)
    replay = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page")
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert [row["candidateId"] for row in replay_body["candidates"]] == [candidate["candidateId"]]
    assert (replay_body["candidates"][0]["width"], replay_body["candidates"][0]["height"]) == (
        320,
        240,
    )
    assert replay_body["acceptedCandidateId"] is None


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
    assert candidate["deltaManifest"]["insideMaskChangedPixelCount"] > 0
    assert candidate["deltaManifest"]["outsideMaskChangedPixelCount"] == 0
    assert candidate["routeChecksum"] == metadata["routeChecksum"]
    assert metadata["routeManifest"]["outsideMaskChangesAllowed"] is False
    assert metadata["routeManifest"]["maskComposite"] is True
    quality_path = store.root / "generated" / "preprocessed" / image["id"] / "quality.png"
    if not quality_path.is_file():
        quality_path = next(
            path
            for path in (store.root / "generated" / "preprocessed").rglob("*.png")
            if _sha(path.read_bytes()) == metadata["qualityChecksum"]
        )
    with store.session() as session:
        mask_row = session.get(PageMaskArtifact, metadata["maskArtifactId"])
        assert mask_row is not None
        mask_bytes = (store.root / mask_row.relative_path).read_bytes()
    provider_normalized, _manifest, _grid, _media_type = cloud_service._normalize(
        raw,
        (
            candidate["width"],
            candidate["height"],
        ),
    )
    provider_delta = cloud_service._delta_manifest(
        quality_path.read_bytes(), provider_normalized, mask_bytes
    )
    assert provider_delta["outsideMaskChangedPixelCount"] > 0
    with store.session() as session:
        assert len(list(session.scalars(select(PageCloudFullPageCandidate)).all())) == 1
        stored = session.scalar(select(PageCloudFullPageCandidate))
        assert stored is not None
        assert (store.root / stored.raw_relative_path).read_bytes() == raw
        assert (store.root / stored.normalized_relative_path).read_bytes() == normalized
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

    refreshed_context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    refreshed_metadata = json.loads(json.dumps(metadata))
    refreshed_metadata["expectedRevision"] = refreshed_context["imageRevision"]
    refreshed_metadata["projectChecksum"] = refreshed_context["projectChecksum"]
    refreshed_metadata["lineage"]["expectedSequence"] = refreshed_context["nextSequence"]
    process_retry = client.post(
        f"/api/images/{image['id']}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(refreshed_metadata), "application/json"),
        },
    )
    assert process_retry.status_code == 200, process_retry.text
    assert process_retry.json()["candidateId"] == candidate["candidateId"]

    changed_raw = raw + b"different-retry-bytes"
    changed_retry = client.post(
        f"/api/images/{image['id']}/page-gates/cloud-full-page/candidates",
        files={
            "raw": ("raw.png", changed_raw, "image/png"),
            "normalized": ("normalized.png", normalized, "image/png"),
            "metadata": (None, json.dumps(refreshed_metadata), "application/json"),
        },
    )
    assert changed_retry.status_code == 409
    assert changed_retry.json()["detail"]["reason"] == "g8-cloud-invocation-conflict"

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


def test_pre_strict_candidate_replays_after_strict_route_migration(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    assert isinstance(image, dict)
    uploaded, metadata, raw, _normalized = _candidate_upload(client, prepared)
    assert uploaded.status_code == 200, uploaded.text
    legacy = _convert_candidate_to_pre_strict(
        prepared,
        candidate_id=uploaded.json()["candidateId"],
        strict_metadata=metadata,
        raw=raw,
    )
    assert legacy["outsideMaskChangedPixelCount"] > 0
    assert legacy["routeManifest"]["outsideMaskChangesAllowed"] is True

    replayed = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page")
    assert replayed.status_code == 200, replayed.text
    candidate = replayed.json()["candidates"][0]
    assert candidate["candidateId"] == legacy["candidateId"]
    assert candidate["normalizedChecksum"] == legacy["normalizedChecksum"]
    assert candidate["parameterHash"] == legacy["parameterHash"]
    assert candidate["quotaClass"] is None
    assert candidate["providerParameters"] is None
    assert candidate["routeChecksum"] == cloud_service._digest(legacy["routeManifest"])
    assert candidate["deltaManifest"]["outsideMaskChangedPixelCount"] > 0
    with store.session() as session:
        stored = session.get(PageCloudFullPageCandidate, legacy["candidateId"])
        assert stored is not None
        assert stored.route_manifest == legacy["routeManifest"]
    with store.engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    assert {
        "page_cloud_full_page_candidates_no_update",
        "page_lineage_events_no_update",
        "revisions_g8_cloud_no_update",
    } <= triggers


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
    assert blocked.json()["detail"]["reason"] == "g8-native-cloud-required"
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
    assert blocked.json()["detail"]["reason"] == "g8-native-cloud-required"
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


def test_cloud_ingest_rejects_provider_whole_frame_as_the_accepted_candidate(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)

    def bypass_composite(metadata, raw, _normalized):
        target = metadata["routeManifest"]["compositeManifest"]["targetGrid"]
        provider_normalized, _manifest, _grid, _media_type = cloud_service._normalize(
            raw, (target["width"], target["height"])
        )
        return (
            {**metadata, "normalizedSha256": _sha(provider_normalized)},
            raw,
            provider_normalized,
        )

    rejected, _metadata, _raw, _normalized = _candidate_upload(client, prepared, bypass_composite)
    assert rejected.status_code == 400


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


def test_rejected_cloud_candidate_allows_method_changed_successor(
    tmp_path, client: TestClient, app
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    first, _metadata, _raw, _normalized = _candidate_upload(client, prepared)
    assert first.status_code == 200, first.text
    pending = _candidate_upload(
        client,
        prepared,
        invocation_id="synthetic-cloud-call-pending",
        inside_rgb=(7, 8, 9),
    )[0]
    assert pending.status_code == 409, pending.text
    assert pending.json()["detail"]["reason"] == "g8-cloud-candidate-exists"
    context = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page").json()
    checks = [
        {"check": check, "passed": check != "background-continuous"}
        for check in cloud_service.CLOUD_FULL_PAGE_CHECKS
    ]
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/cloud-full-page",
        json={
            "candidateId": first.json()["candidateId"],
            "observedChecksum": first.json()["normalizedChecksum"],
            "decision": "reject",
            "reason": "background-continuous",
            "checks": checks,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(context["generationId"], context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    second, _metadata, _raw, _normalized = _candidate_upload(
        client,
        prepared,
        invocation_id="synthetic-cloud-call-2",
        inside_rgb=(9, 8, 7),
    )
    assert second.status_code == 200, second.text
    listed = client.get(f"/api/images/{image['id']}/page-gates/cloud-full-page")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert [row["sequence"] for row in payload["candidates"]] == [1, 2]
    assert payload["candidates"][0]["review"]["state"] == "rejected"
    assert payload["candidates"][1]["review"] is None
    assert payload["acceptedCandidateId"] is None


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
@historical_local_g8()
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


@historical_local_g8()
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
