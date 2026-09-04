import io
import json
import sqlite3
import uuid

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageGeneration,
    PageLineageEvent,
    Project,
    Revision,
)
from manga_localizer.services import reconstructions as service
from manga_localizer.services.page_lineage import require_current_quality_plate

from .conftest import png_bytes
from .test_page_lineage import (
    _accept_g1_preprocess,
    _accept_g3_text_present,
    _generation_body,
    _mutation_lineage,
    _prepare_g7_accepted_page,
    _source_and_target,
)


def _snapshot(store):
    with sqlite3.connect(store.database_path) as db:
        db.execute("PRAGMA query_only=ON")
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows = {
            table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in tables
        }
    return {
        "database": service.digest(rows),
        "files": {
            str(path.relative_to(store.root)): service.sha(path.read_bytes())
            for path in store.root.rglob("*")
            if path.is_file() and not path.name.startswith("project.sqlite3")
        },
    }


def _prepare(client, app, tmp_path):
    data, sp, si, project, image = _source_and_target(client, tmp_path)
    gid = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{image['id']}/page-generations",
        json=_generation_body(
            source_project_id=sp["id"],
            source_image_id=si["id"],
            checksum=service.sha(data),
            generation_id=gid,
        ),
    )
    assert created.status_code == 201, created.text
    checksum, accepted = _accept_g1_preprocess(
        client, app, target_project=project, target_image=image, generation_id=gid
    )
    generation = client.get(f"/api/images/{image['id']}/page-generations").json()[0]
    decision = client.patch(
        f"/api/images/{image['id']}/page-gates/reconstruction",
        json={
            "decision": "yes",
            "reason": "fine-lines-remain-insufficient",
            "observedQualityChecksum": checksum,
            "expectedRevision": accepted["revision"],
            "lineage": _mutation_lineage(gid, generation["nextSequence"]),
        },
    )
    assert decision.status_code == 200, decision.text
    return {
        "data": data,
        "sourceProject": sp,
        "sourceImage": si,
        "targetProject": project,
        "targetImage": image,
        "generationId": gid,
        "qualityChecksum": checksum,
        "store": app.state.registry.get(project["id"]),
    }


def _metadata(client, prepared, invocation="native-test-1"):
    iid = prepared["targetImage"]["id"]
    response = client.get(f"/api/images/{iid}/page-gates/reconstruction")
    assert response.status_code == 200, response.text
    ctx = response.json()
    return {
        "profile": service.PROFILE,
        "runtime": "codex",
        "tool": "image_gen",
        "provider": "unreported",
        "modelVersion": "native-image-model-unreported",
        "claimStatus": "operator-attested-client-supplied-unverified",
        "invocationId": invocation,
        "promptSha256": "a" * 64,
        **{
            key: ctx[key]
            for key in ("sourceChecksum", "baselineChecksum", "baselineEventId", "decisionEventId")
        },
        "expectedRevision": ctx["imageRevision"],
        "lineage": _mutation_lineage(ctx["generationId"], ctx["nextSequence"]),
    }


def _png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _lettering_mask(size, box):
    image = Image.new("L", size, 0)
    ImageDraw.Draw(image).rectangle(box, fill=255)
    return _png(image)


def _upload(client, prepared, metadata=None, raw=None, lettering_mask=None):
    iid = prepared["targetImage"]["id"]
    if metadata is None:
        metadata = _metadata(client, prepared)
    if raw is None:
        baseline = client.get(
            f"/api/images/{iid}/page-gates/reconstruction/inputs/baseline"
        ).content
        image = Image.open(io.BytesIO(baseline)).convert("RGB")
        image.putpixel((0, 0), (171, 172, 173))
        raw = _png(image)
    files = {"raw": ("native.png", raw)}
    if lettering_mask is not None:
        files["letteringMask"] = ("lettering-mask.png", lettering_mask)
    result = client.post(
        f"/api/images/{iid}/page-gates/reconstruction/candidates",
        files=files,
        data={"metadata": json.dumps(metadata)},
    )
    return result, metadata, raw


def _review(client, prepared, candidate, decision="accept"):
    iid = prepared["targetImage"]["id"]
    ctx = client.get(f"/api/images/{iid}/page-gates/reconstruction").json()
    checks = [
        {"check": check, "passed": decision == "accept" or check != "clarity-improved"}
        for check in service.CHECKS
    ]
    return client.patch(
        f"/api/images/{iid}/page-gates/reconstruction/candidates",
        json={
            "candidateId": candidate["candidateId"],
            "observedChecksum": candidate["checksum"],
            "decision": decision,
            "checks": checks,
            "expectedRevision": ctx["imageRevision"],
            "lineage": _mutation_lineage(ctx["generationId"], ctx["nextSequence"]),
        },
    )


def test_native_candidate_pending_replay_accept_and_true_quality_binding(client, app, tmp_path):
    prepared = _prepare(client, app, tmp_path)
    iid = prepared["targetImage"]["id"]
    response, metadata, raw = _upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["state"] == "pending"
    assert client.get(f"/api/images/{iid}/generated/quality").status_code == 409
    before = _snapshot(prepared["store"])
    replay, _, _ = _upload(client, prepared, metadata, raw)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert _snapshot(prepared["store"]) == before
    reviewed = _review(client, prepared, candidate)
    assert reviewed.status_code == 200, reviewed.text
    current = client.get(f"/api/images/{iid}/generated/quality")
    assert current.status_code == 200, current.text
    assert service.sha(current.content) == candidate["checksum"] != prepared["qualityChecksum"]
    assert (
        service.sha(client.get(f"/api/images/{iid}/generated/preprocessed").content)
        == prepared["qualityChecksum"]
    )
    with prepared["store"].session() as session:
        binding = require_current_quality_plate(
            prepared["store"],
            session,
            session.get(ImageAsset, iid),
            session.get(PageGeneration, prepared["generationId"]),
        )
        assert binding["targetKind"] == "reconstruction"
        assert "lineage-reconstructions" in str(binding["path"])
        jobs = session.scalars(select(Job).where(Job.kind == "native-reconstruction")).all()
        assert len(jobs) == 1 and jobs[0].status == "completed"
    raw_read = client.get(
        f"/api/images/{iid}/page-gates/reconstruction/candidates/{candidate['candidateId']}/raw"
    )
    assert raw_read.content == raw
    before = _snapshot(prepared["store"])
    replay, _, _ = _upload(client, prepared, metadata, raw)
    assert replay.status_code == 200
    assert replay.json()["state"] == "accepted"
    assert _snapshot(prepared["store"]) == before


def test_reconstruction_cover_crops_native_discrete_aspect(client, app, tmp_path):
    prepared = _prepare(client, app, tmp_path)
    iid = prepared["targetImage"]["id"]
    metadata = _metadata(client, prepared, invocation="native-cover-crop-1")
    raw = png_bytes(size=(1024, 1536), color="gray")
    response, _, _ = _upload(client, prepared, metadata, raw)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["state"] == "pending"
    assert candidate["rawChecksum"] == service.sha(raw)
    with prepared["store"].session() as session:
        produced = session.scalars(
            select(PageLineageEvent).where(PageLineageEvent.operation == service.PRODUCED)
        ).one()
        normalization = produced.evidence["normalization"]
    assert normalization["crop"] is True
    assert normalization["sourceGrid"] == {"width": 1024, "height": 1536}
    assert candidate["targetGrid"] == {"width": 240, "height": 320}
    fitted = (
        normalization["fittedGrid"]["width"],
        normalization["fittedGrid"]["height"],
    )
    from manga_localizer.services.cloud_full_page_clean_plates import (
        ASPECT_LIMIT,
        _aspect_error,
    )

    assert _aspect_error(*fitted, (240, 320)) <= ASPECT_LIMIT
    raw_read = client.get(
        f"/api/images/{iid}/page-gates/reconstruction/candidates/{candidate['candidateId']}/raw"
    )
    assert raw_read.content == raw
    before = _snapshot(prepared["store"])
    replay, _, _ = _upload(client, prepared, metadata, raw)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert _snapshot(prepared["store"]) == before


def test_lettering_lock_keeps_g1_inside_mask_and_native_outside(client, app, tmp_path):
    prepared = _prepare(client, app, tmp_path)
    iid = prepared["targetImage"]["id"]
    baseline = client.get(f"/api/images/{iid}/page-gates/reconstruction/inputs/baseline").content
    with Image.open(io.BytesIO(baseline)) as opened:
        g1 = opened.convert("RGB")
        size = g1.size
        g1_left = g1.getpixel((8, 8))
        native = g1.copy()
        native.paste((200, 30, 40), (0, 0, 40, 40))
        native.paste((30, 40, 200), (size[0] - 40, 0, size[0], 40))
        native_right = native.getpixel((size[0] - 8, 8))
        raw = _png(native)
    mask = _lettering_mask(size, (0, 0, 40, 40))
    metadata = _metadata(client, prepared, invocation="native-lettering-1")
    metadata["letteringLock"] = True
    metadata["letteringMaskSha256"] = service.sha(mask)
    response, _, _ = _upload(client, prepared, metadata, raw, lettering_mask=mask)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["state"] == "pending"
    assert candidate["rawChecksum"] == service.sha(raw)
    native_only, _ = service.normalize(raw, size)
    assert candidate["checksum"] != service.sha(native_only)
    locked = client.get(
        f"/api/images/{iid}/page-gates/reconstruction/candidates/{candidate['candidateId']}/normalized"
    ).content
    assert service.sha(locked) == candidate["checksum"]
    with Image.open(io.BytesIO(locked)) as opened:
        result = opened.convert("RGB")
        assert result.getpixel((8, 8)) == g1_left
        assert result.getpixel((size[0] - 8, 8)) == native_right
    stored_mask = client.get(
        f"/api/images/{iid}/page-gates/reconstruction/candidates/{candidate['candidateId']}/lettering-mask"
    )
    assert stored_mask.status_code == 200
    assert stored_mask.content == mask
    with prepared["store"].session() as session:
        produced = session.scalars(
            select(PageLineageEvent).where(PageLineageEvent.operation == service.PRODUCED)
        ).one()
        request = produced.evidence["request"]
        normalization = produced.evidence["normalization"]
    assert request["letteringLock"] is True
    assert request["letteringMaskSha256"] == service.sha(mask)
    assert normalization["letteringLock"] is True
    assert normalization["letteringLockProfile"] == service.LETTERING_LOCK_PROFILE
    assert normalization["nativeNormalizedSha256"] == service.sha(native_only)
    assert produced.evidence["rawChecksum"] == service.sha(raw)
    before = _snapshot(prepared["store"])
    replay, _, _ = _upload(client, prepared, metadata, raw, lettering_mask=mask)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["candidateId"] == candidate["candidateId"]
    assert _snapshot(prepared["store"]) == before
    unlocked = _metadata(client, prepared, invocation="native-unlocked-same-raw")
    other, _, _ = _upload(client, prepared, unlocked, raw)
    assert other.status_code == 200, other.text
    assert other.json()["candidateId"] != candidate["candidateId"]
    assert other.json()["checksum"] == service.sha(native_only)


def test_lettering_lock_bad_mask_or_changed_replay_is_zero_write(client, app, tmp_path):
    prepared = _prepare(client, app, tmp_path)
    iid = prepared["targetImage"]["id"]
    baseline = client.get(f"/api/images/{iid}/page-gates/reconstruction/inputs/baseline").content
    with Image.open(io.BytesIO(baseline)) as opened:
        size = opened.size
        native = opened.convert("RGB")
        native.paste((10, 20, 30), (0, 0, 16, 16))
        raw = _png(native)
    mask = _lettering_mask(size, (0, 0, 24, 24))
    metadata = _metadata(client, prepared, invocation="native-lettering-bad")
    metadata["letteringLock"] = True
    metadata["letteringMaskSha256"] = service.sha(mask)
    created, _, _ = _upload(client, prepared, metadata, raw, lettering_mask=mask)
    assert created.status_code == 200, created.text
    before = _snapshot(prepared["store"])
    empty = _png(Image.new("L", size, 0))
    claimed = dict(metadata)
    claimed["letteringMaskSha256"] = service.sha(empty)
    empty_response, _, _ = _upload(client, prepared, claimed, raw, lettering_mask=empty)
    assert empty_response.status_code == 400
    mismatch, _, _ = _upload(
        client, prepared, metadata, raw, lettering_mask=_lettering_mask(size, (0, 0, 8, 8))
    )
    assert mismatch.status_code == 400
    unclaimed, _, _ = _upload(
        client,
        prepared,
        _metadata(client, prepared, invocation="no-lock"),
        raw,
        lettering_mask=mask,
    )
    assert unclaimed.status_code == 400
    missing = _upload(client, prepared, metadata, raw)
    assert missing[0].status_code == 400
    wrong_size = _lettering_mask((16, 16), (0, 0, 8, 8))
    sized = dict(metadata)
    sized["letteringMaskSha256"] = service.sha(wrong_size)
    sized["invocationId"] = "native-lettering-wrong-size"
    size_response, _, _ = _upload(client, prepared, sized, raw, lettering_mask=wrong_size)
    assert size_response.status_code == 400
    changed = dict(metadata)
    other_mask = _lettering_mask(size, (8, 8, 32, 32))
    changed["letteringMaskSha256"] = service.sha(other_mask)
    replay_conflict, _, _ = _upload(client, prepared, changed, raw, lettering_mask=other_mask)
    assert replay_conflict.status_code == 409
    assert _snapshot(prepared["store"]) == before


@pytest.mark.parametrize(
    "change",
    [
        "raw",
        "prompt",
        "runtime",
        "source",
        "sequence",
        "revision",
        "extra",
        "invalid-raster",
        "aspect",
    ],
)
def test_reconstruction_bad_import_or_changed_replay_is_zero_write(client, app, tmp_path, change):
    prepared = _prepare(client, app, tmp_path)
    metadata = _metadata(client, prepared)
    raw = png_bytes()
    if change in {"raw", "prompt", "runtime"}:
        response, _, _ = _upload(client, prepared, metadata, raw)
        assert response.status_code == 200, response.text
    if change == "raw":
        raw = png_bytes(color="gray")
    if change == "prompt":
        metadata["promptSha256"] = "b" * 64
    if change == "runtime":
        metadata["runtime"] = "cursor"
    if change == "source":
        metadata["sourceChecksum"] = "c" * 64
    if change == "sequence":
        metadata["lineage"]["expectedSequence"] -= 1
    if change == "revision":
        metadata["expectedRevision"] -= 1
    if change == "extra":
        metadata["providerEndpoint"] = "forbidden"
    if change == "invalid-raster":
        raw = b"not an image"
    if change == "aspect":
        raw = png_bytes(size=(8, 2))
    before = _snapshot(prepared["store"])
    response, _, _ = _upload(client, prepared, metadata, raw)
    assert response.status_code in {400, 409, 422}, response.text
    assert _snapshot(prepared["store"]) == before


def test_rejected_stale_and_tampered_candidates_never_become_quality(client, app, tmp_path):
    prepared = _prepare(client, app, tmp_path)
    iid = prepared["targetImage"]["id"]
    response, metadata, raw = _upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert _review(client, prepared, candidate, "reject").status_code == 200
    assert client.get(f"/api/images/{iid}/generated/quality").status_code == 409
    assert _review(client, prepared, candidate).status_code == 409
    metadata = _metadata(client, prepared, invocation="native-test-2")
    second, _, _ = _upload(client, prepared, metadata, raw)
    assert second.status_code == 200, second.text
    assert _review(client, prepared, second.json()).status_code == 200
    artifact = service.artifact_path(prepared["store"], iid, second.json()["candidateId"])
    artifact.write_bytes(png_bytes(color="red"))
    assert client.get(f"/api/images/{iid}/generated/quality").status_code == 409


def test_new_g2_decision_invalidates_candidate_without_erasing_history(client, app, tmp_path):
    prepared = _prepare(client, app, tmp_path)
    response, metadata, raw = _upload(client, prepared)
    assert response.status_code == 200, response.text
    iid = prepared["targetImage"]["id"]
    ctx = client.get(f"/api/images/{iid}/page-gates/reconstruction").json()
    new = client.patch(
        f"/api/images/{iid}/page-gates/reconstruction",
        json={
            "decision": "yes",
            "reason": "structure-remains-uncertain",
            "observedQualityChecksum": ctx["baselineChecksum"],
            "expectedRevision": ctx["imageRevision"],
            "lineage": _mutation_lineage(ctx["generationId"], ctx["nextSequence"]),
        },
    )
    assert new.status_code == 200
    before = _snapshot(prepared["store"])
    assert _review(client, prepared, response.json()).status_code == 409
    replay, _, _ = _upload(client, prepared, metadata, raw)
    assert replay.status_code == 409
    assert _snapshot(prepared["store"]) == before
    assert service.artifact_path(prepared["store"], iid, response.json()["candidateId"]).is_file()


@pytest.mark.parametrize(
    "change",
    [
        "producer-project",
        "producer-operation",
        "producer-before",
        "review-project",
        "review-operation",
        "review-before",
        "job-progress",
        "job-error",
        "item-progress",
        "item-error",
        "item-position",
        "extra-item",
    ],
)
def test_inexact_producer_or_review_never_resolves_as_quality(client, app, tmp_path, change):
    prepared = _prepare(client, app, tmp_path)
    response, _, _ = _upload(client, prepared)
    candidate = response.json()
    accepted = _review(client, prepared, candidate)
    assert accepted.status_code == 200, accepted.text
    store = prepared["store"]
    with store.session() as session:
        producer = session.get(PageLineageEvent, candidate["producerEventId"])
        review_event = session.get(PageLineageEvent, accepted.json()["eventId"])
        if change.startswith(("producer-", "review-")):
            event = producer if change.startswith("producer-") else review_event
            revision = session.get(Revision, event.revision_id)
            field = change.split("-", 1)[1]
            if field == "project":
                session.add(
                    Project(
                        id=prepared["sourceProject"]["id"],
                        name="synthetic-unrelated-project",
                        root_path=str(tmp_path / "unrelated"),
                    )
                )
                session.flush()
                revision.project_id = prepared["sourceProject"]["id"]
            elif field == "operation":
                revision.operation = "unrelated"
            else:
                revision.before = {"unexpected": True}
        else:
            job = session.get(Job, producer.job_id)
            item = session.get(JobItem, producer.job_item_id)
            if change == "extra-item":
                session.add(
                    JobItem(
                        job_id=job.id,
                        image_id=item.image_id,
                        position=1,
                        status="failed",
                        progress=0.0,
                        error="unexpected second item",
                    )
                )
            else:
                target, field = change.split("-")
                row = job if target == "job" else item
                setattr(row, field, "unexpected failure" if field == "error" else 0.5)
    before = _snapshot(store)
    iid = prepared["targetImage"]["id"]
    assert client.get(f"/api/images/{iid}/generated/quality").status_code == 409
    assert _snapshot(store) == before


@pytest.mark.parametrize("text_present", [False, True])
@pytest.mark.parametrize("duplicate_pixels", [False, True])
def test_reconstructed_quality_reaches_real_consumers_and_strict_freeze(
    client, app, tmp_path, text_present, duplicate_pixels
):
    prepared = _prepare(client, app, tmp_path)
    image = prepared["targetImage"]
    response, _, raw = _upload(client, prepared)
    assert response.status_code == 200, response.text
    candidate = response.json()
    if duplicate_pixels:
        second, _, _ = _upload(
            client, prepared, _metadata(client, prepared, "different-native-invocation"), raw
        )
        assert second.status_code == 200, second.text
        assert second.json()["checksum"] == candidate["checksum"]
        assert second.json()["candidateId"] != candidate["candidateId"]
        assert second.json()["state"] == "pending"
    accepted = _review(client, prepared, candidate)
    assert accepted.status_code == 200, accepted.text
    quality_hash = candidate["checksum"]
    state = accepted.json()
    if text_present:
        g3 = _accept_g3_text_present(
            client,
            source_checksum=service.sha(prepared["data"]),
            image_id=image["id"],
            generation_id=prepared["generationId"],
            quality_checksum=quality_hash,
            image_revision=state["imageRevision"],
            expected_sequence=state["nextSequence"],
        )
        prepared.update(qualityChecksum=quality_hash, acceptedG3=g3)
        prepared = _prepare_g7_accepted_page(client, app, tmp_path, prepared=prepared)
        from .test_cloud_full_page_clean_plates import _start_cloud_route
        from .test_typesets import _complete_g9_terminal, _review_body, _run_typeset

        # The cloud fixture must read the accepted quality endpoint, not G1.
        cloud = _start_cloud_route(client, prepared, "accepted")
        prepared = _complete_g9_terminal(client, prepared)
        assert prepared["g9Context"]["cleanPlateChecksum"] == cloud["normalizedChecksum"]
        _job, typeset = _run_typeset(client, app, prepared)
        final = typeset["candidates"][0]
        reviewed = client.patch(
            f"/api/images/{image['id']}/page-gates/typeset/candidates/{final['candidateId']}",
            json=_review_body(typeset, final, prepared["generationId"]),
        )
        assert reviewed.status_code == 200, reviewed.text
    else:
        g3 = client.patch(
            f"/api/images/{image['id']}/page-gates/text-presence",
            json={
                "decision": "no",
                "reason": "no-processable-text-visible",
                "evidence": ["original-and-quality-compared", "no-processable-text-visible"],
                "observedOriginalChecksum": service.sha(prepared["data"]),
                "observedQualityChecksum": quality_hash,
                "expectedRevision": state["imageRevision"],
                "lineage": _mutation_lineage(prepared["generationId"], state["nextSequence"]),
            },
        )
        assert g3.status_code == 200, g3.text
    batch = client.post(
        "/api/final-review-batches",
        json={
            "name": "native G2 frozen proof",
            "outputPath": str(tmp_path / "frozen"),
            "sourceProjectIds": [prepared["targetProject"]["id"]],
            "expectedItemCount": 1,
        },
    )
    assert batch.status_code == 201, batch.text
    item = batch.json()["items"][0]
    assert item["strictEvidence"] is True
    assert item["currentArtifactStale"] is False
    assert item["evidence"]["quality"]["checksum"] == quality_hash
    with prepared["store"].session() as session:
        producer = session.get(PageLineageEvent, item["evidence"]["quality"]["producerId"])
        assert producer.operation == service.PRODUCED
        assert producer.id == candidate["producerEventId"]
        assert item["evidence"]["quality"]["producerRevisionId"] == producer.revision_id
        terminal = session.get(PageLineageEvent, item["evidence"]["quality"]["terminalId"])
        assert terminal.operation == service.REVIEWED
    if not text_present:
        assert item["evidence"]["final"]["checksum"] == quality_hash
