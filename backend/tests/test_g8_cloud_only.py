"""Current G8 write policy, with no test-wide legacy-policy bypass."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from manga_localizer.database import ImageAsset, Job, JobItem, PageGeneration
from manga_localizer.services import clean_plates
from manga_localizer.services.page_lineage import PageLineageConflict

from .legacy_g8_fixture import historical_local_g8
from .test_cloud_full_page_clean_plates import _route_snapshot
from .test_page_lineage import (
    _CLEAN_PLATE_CHECKS,
    _current_lineage_context,
    _mutation_lineage,
    _prepare_g7_accepted_page,
    _prepare_g8_accepted_page,
)


def _snapshot(prepared):
    store = prepared["store"]
    return {
        "database": _route_snapshot(store, prepared["targetImage"]["id"], prepared["generationId"]),
        "files": {
            str(path.relative_to(store.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in store.root.rglob("*")
            if path.is_file() and not path.name.startswith("project.sqlite3")
        },
    }


def _enqueue(client, prepared, options):
    image_id = prepared["targetImage"]["id"]
    return client.post(
        f"/api/projects/{prepared['targetProject']['id']}/inpaint",
        json={
            "imageIds": [image_id],
            "options": options,
            "lineage": _current_lineage_context(client, image_id, prepared["generationId"]),
        },
    )


@pytest.mark.parametrize(
    "options", [{}, {"provider": "lama-onnx"}, {"provider": "opencv"}, {"classicalFallback": True}]
)
def test_local_enqueue_is_zero_write_before_any_cloud_attempt(client, app, tmp_path, options):
    # This fixture also exercises permitted local G1, OCR, mask and G7 review.
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    before = _snapshot(prepared)
    blocked = _enqueue(client, prepared, options)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g8-native-cloud-required"
    assert _snapshot(prepared) == before


def test_direct_enqueue_and_publisher_cannot_bypass_cloud_policy(client, app, tmp_path):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    store = prepared["store"]
    before = _snapshot(prepared)
    with store.session() as session:
        image = session.get(ImageAsset, prepared["targetImage"]["id"])
        generation = session.get(PageGeneration, prepared["generationId"])
        with pytest.raises(PageLineageConflict, match="native cloud"):
            clean_plates.prepare_clean_plate_enqueue(
                store, session, image=image, generation=generation, job=None, item=None
            )
    with pytest.raises(PageLineageConflict, match="native cloud"):
        clean_plates.publish_clean_plate_candidate(
            store,
            job=None,
            item=SimpleNamespace(id="old-queued-item"),
            binding={},
            inpainter=lambda _: pytest.fail("local provider called"),
        )
    assert _snapshot(prepared) == before


def test_pre_policy_queued_job_cannot_generate_or_report_success(
    client, app, tmp_path, monkeypatch
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    store = prepared["store"]
    # Only manufacture the historical queue row under the old policy.
    with historical_local_g8():
        queued = _enqueue(client, prepared, {})
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    item_id = queued.json()["items"][0]["id"]
    assert app.state.queue._claim_next() == (store, job_id)
    assert app.state.queue._begin_item(store, job_id, item_id)
    monkeypatch.setattr(
        app.state.queue.providers, "inpainter", lambda _: pytest.fail("local provider called")
    )
    before = _snapshot(prepared)
    with pytest.raises(PageLineageConflict, match="native cloud"):
        app.state.queue._process_item(store, job_id, item_id)
    with store.session() as session:
        with pytest.raises(PageLineageConflict, match="native cloud"):
            clean_plates.clean_plate_completion_evidence(
                store,
                session,
                job=session.get(Job, job_id),
                item=session.get(JobItem, item_id),
                succeeded=True,
            )
    assert _snapshot(prepared) == before


def test_pre_policy_queue_records_failure_without_image_or_review_writes(
    client, app, tmp_path, monkeypatch
):
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    with historical_local_g8():
        queued = _enqueue(client, prepared, {})
    assert queued.status_code == 202, queued.text
    monkeypatch.setattr(
        app.state.queue.providers, "inpainter", lambda _: pytest.fail("local provider called")
    )
    before = _snapshot(prepared)
    events_url = f"/api/page-generations/{prepared['generationId']}/events"
    old_events = client.get(events_url).json()
    claimed = app.state.queue._claim_next()
    assert claimed == (prepared["store"], queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    job = client.get(f"/api/jobs/{queued.json()['id']}").json()
    assert job["status"] == "failed"
    assert job["items"][0]["status"] == "failed"
    after = _snapshot(prepared)
    for key in (
        "jobs",
        "jobItems",
        "legacyCandidates",
        "legacyReviews",
        "cloudCandidates",
        "cloudReviews",
    ):
        assert after["database"][key] == before["database"][key]
    for snapshot in (before, after):
        snapshot["pixels"] = {
            path: checksum
            for path, checksum in snapshot["files"].items()
            if path.startswith(("source/", "generated/"))
        }
    assert after["pixels"] == before["pixels"]
    events = client.get(events_url).json()
    assert events[: len(old_events)] == old_events
    assert [event["operation"] for event in events[len(old_events) :]] == ["inpaint-job-failed"]


@pytest.mark.parametrize("mutation", ["accept", "reject", "enable-fallback", "disable-fallback"])
def test_historical_artifacts_replay_but_cannot_be_reviewed_again(client, app, tmp_path, mutation):
    # The helper bypasses only historical construction, then restores the guard.
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image_id = prepared["targetImage"]["id"]
    store = prepared["store"]
    context = client.get(f"/api/images/{image_id}/page-gates/clean-plate").json()
    candidate = context["candidates"][0]
    before = _snapshot(prepared)
    artifact = client.get(
        f"/api/images/{image_id}/page-gates/clean-plate/candidates/{candidate['candidateId']}"
    )
    assert artifact.status_code == 200
    assert hashlib.sha256(artifact.content).hexdigest() == candidate["candidateChecksum"]
    with store.session() as session:
        state, path, selected = clean_plates.require_current_clean_plate_acceptance(
            store,
            session,
            session.get(ImageAsset, image_id),
            session.get(PageGeneration, prepared["generationId"]),
        )
        assert state == context["cleanPlateStateChecksum"]
        assert selected.id == candidate["candidateId"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == candidate["candidateChecksum"]
    body = {
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(prepared["generationId"], context["nextSequence"]),
    }
    if mutation.endswith("fallback"):
        body.update(enabled=mutation == "enable-fallback", reason="all-ai-candidates-rejected")
        url = f"/api/images/{image_id}/page-gates/clean-plate/fallback"
    else:
        checks = [dict(check) for check in _CLEAN_PLATE_CHECKS]
        if mutation == "reject":
            checks[1]["passed"] = False
        body.update(
            decision=mutation,
            reason="clean-plate-complete" if mutation == "accept" else "residual-text-readable",
            candidateId=candidate["candidateId"],
            observedCandidateChecksum=candidate["candidateChecksum"],
            observedWidth=candidate["width"],
            observedHeight=candidate["height"],
            checks=checks,
        )
        url = f"/api/images/{image_id}/page-gates/clean-plate"
    blocked = client.patch(url, json=body)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g8-native-cloud-required"
    assert _snapshot(prepared) == before
