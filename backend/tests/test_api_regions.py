from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manga_localizer.database import ImageAsset, TextRegion
from manga_localizer.services.trust import with_detection_evidence, with_ocr_evidence

from .conftest import create_project, upload_image


def _create_region(client: TestClient, image_id: str, x: int, y: int) -> dict:
    response = client.post(
        f"/api/images/{image_id}/regions",
        json={
            "x": x,
            "y": y,
            "width": 40,
            "height": 50,
            "sourceText": "日本語",
            "direction": "vertical",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_region_crud_revision_history_and_conflict(client: TestClient, tmp_path: Path) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    region = _create_region(client, image["id"], 20, 30)
    assert region["revision"] == 1
    assert region["type"] == "dialogue"
    assert region["ocrProvider"] == "manual"
    assert region["trustDisposition"] == "review"
    assert region["trustReason"] == "manual-unconfirmed"
    assert region["trustPolicyVersion"] == 1
    assert region["detectorConfidence"] is None
    assert region["ocrConfidence"] is None

    updated = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "x": 25,
            "rotation": 12,
            "translationText": "中文",
            "confirmed": True,
            "style": {"fontSize": 20, "strokeWidth": 2},
            "expectedRevision": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["translationText"] == "中文"

    stale = client.patch(
        f"/api/regions/{region['id']}",
        json={"translationText": "覆盖", "expectedRevision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "message": "Region revision is 2, expected 1",
        "expectedRevision": 1,
        "actualRevision": 2,
        "resource": f"region:{region['id']}",
    }
    assert client.get(f"/api/images/{image['id']}/regions").json()[0]["translationText"] == "中文"

    revisions = client.get(f"/api/projects/{project['id']}/revisions").json()
    assert {revision["operation"] for revision in revisions} >= {"create", "update"}
    deleted = client.delete(f"/api/regions/{region['id']}", params={"expectedRevision": 2})
    assert deleted.status_code == 204
    assert client.get(f"/api/images/{image['id']}/regions").json() == []


def test_deleting_a_trusted_region_invalidates_translation(
    client: TestClient, app, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "delete-trusted-region")
    image = upload_image(client, project["id"])
    region = _create_region(client, image["id"], 20, 30)
    confirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        asset = session.get(ImageAsset, image["id"])
        assert asset is not None
        asset.status = {**asset.status, "translation": "done"}

    deleted = client.delete(
        f"/api/regions/{region['id']}",
        params={"expectedRevision": confirmed.json()["revision"]},
    )
    assert deleted.status_code == 204, deleted.text
    state = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert state["status"]["translation"] == "pending"


def test_recognition_trust_is_read_only_and_has_fail_closed_transitions(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "recognition-project")
    image = upload_image(client, project["id"])
    rejected = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "recognition": {
                "version": 1,
                "trust": {
                    "policyVersion": 1,
                    "disposition": "trusted",
                    "reason": "human-confirmed",
                },
            },
        },
    )
    assert rejected.status_code == 422

    region = _create_region(client, image["id"], 20, 30)
    assert region["recognition"]["version"] == 1
    assert region["trustDisposition"] == "review"

    confirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    trusted = confirmed.json()
    assert trusted["confirmed"] is True
    assert trusted["trustDisposition"] == "trusted"
    assert trusted["trustReason"] == "human-confirmed"

    translated = client.patch(
        f"/api/regions/{region['id']}",
        json={"translationText": "下游译文", "expectedRevision": trusted["revision"]},
    )
    assert translated.status_code == 200, translated.text
    downstream = translated.json()
    assert downstream["confirmed"] is False
    assert downstream["trustDisposition"] == "trusted"
    assert downstream["trustReason"] == "human-confirmed"
    image_counts = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert image_counts["confirmedCount"] == 0
    assert image_counts["trustedCount"] == 1
    assert image_counts["trustReviewCount"] == 0

    changed_source = client.patch(
        f"/api/regions/{region['id']}",
        json={"sourceText": "修改原文", "expectedRevision": downstream["revision"]},
    )
    assert changed_source.status_code == 200, changed_source.text
    review = changed_source.json()
    assert review["trustDisposition"] == "review"
    assert review["trustReason"] == "trust-input-changed"

    reconfirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": review["revision"]},
    )
    assert reconfirmed.status_code == 200, reconfirmed.text
    assert reconfirmed.json()["trustDisposition"] == "trusted"

    ignored = client.patch(
        f"/api/regions/{region['id']}",
        json={"ignored": True, "expectedRevision": reconfirmed.json()["revision"]},
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["trustDisposition"] == "ignored"
    assert ignored.json()["trustReason"] == "human-ignored"

    unignored = client.patch(
        f"/api/regions/{region['id']}",
        json={"ignored": False, "expectedRevision": ignored.json()["revision"]},
    )
    assert unignored.status_code == 200, unignored.text
    assert unignored.json()["trustDisposition"] == "review"
    assert unignored.json()["trustReason"] == "trust-input-changed"


def test_reconfirming_translation_only_edit_preserves_current_visual_reviews(
    client: TestClient, app, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "translation-layout-reconfirm")
    image = upload_image(client, project["id"])
    region = _create_region(client, image["id"], 20, 30)
    confirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert confirmed.status_code == 200, confirmed.text

    inpaint_review = {
        "state": "accepted",
        "reviewedAt": "2026-08-20T00:00:00+00:00",
        "resultRevision": 1,
        "artifactChecksum": "a" * 64,
        "maskChecksum": "b" * 64,
    }
    typeset_review = {
        "state": "accepted",
        "reviewedAt": "2026-08-20T00:01:00+00:00",
        "resultRevision": 2,
        "artifactChecksum": "c" * 64,
    }
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        asset = session.get(ImageAsset, image["id"])
        assert asset is not None
        asset.status = {
            **asset.status,
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
            "stageReviews": {"inpaint": inpaint_review},
        }

    translated = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "translationText": "下游译文",
            "expectedRevision": confirmed.json()["revision"],
        },
    )
    assert translated.status_code == 200, translated.text
    assert translated.json()["confirmed"] is False
    after_translation = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert after_translation["status"]["inpaint"] == "done"
    assert after_translation["status"]["typeset"] == "pending"
    assert after_translation["stageReviews"]["inpaint"] == inpaint_review

    # The operator may generate and accept the current typeset result before
    # closing the separate page-review confirmation gate.
    with store.session() as session:
        asset = session.get(ImageAsset, image["id"])
        assert asset is not None
        asset.status = {
            **asset.status,
            "typeset": "done",
            "export": "done",
            "stageReviews": {
                "inpaint": inpaint_review,
                "typeset": typeset_review,
            },
        }

    reconfirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "confirmed": True,
            "expectedRevision": translated.json()["revision"],
        },
    )
    assert reconfirmed.status_code == 200, reconfirmed.text
    assert reconfirmed.json()["confirmed"] is True
    after_reconfirm = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert after_reconfirm["status"]["inpaint"] == "done"
    assert after_reconfirm["status"]["typeset"] == "done"
    assert after_reconfirm["status"]["export"] == "pending"
    assert after_reconfirm["stageReviews"]["inpaint"] == inpaint_review
    assert after_reconfirm["stageReviews"]["typeset"] == typeset_review


def test_initial_trust_confirmation_invalidates_visual_artifacts(
    client: TestClient, app, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "initial-trust-confirmation")
    image = upload_image(client, project["id"])
    region = _create_region(client, image["id"], 20, 30)
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        asset = session.get(ImageAsset, image["id"])
        assert asset is not None
        asset.status = {
            **asset.status,
            "translation": "done",
            "inpaint": "done",
            "typeset": "done",
            "export": "done",
            "stageReviews": {
                "inpaint": {
                    "state": "accepted",
                    "reviewedAt": "2026-08-20T00:00:00+00:00",
                    "resultRevision": 1,
                    "artifactChecksum": "a" * 64,
                    "maskChecksum": "b" * 64,
                },
                "typeset": {
                    "state": "accepted",
                    "reviewedAt": "2026-08-20T00:01:00+00:00",
                    "resultRevision": 2,
                    "artifactChecksum": "c" * 64,
                },
            },
        }

    confirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["trustDisposition"] == "trusted"
    current = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert current["status"]["translation"] == "pending"
    assert current["status"]["inpaint"] == "pending"
    assert current["status"]["typeset"] == "pending"
    assert current["status"]["export"] == "pending"
    assert current["stageReviews"] == {}


def test_policy_change_preserves_readable_detection_and_ocr_evidence(
    client: TestClient, app, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "policy-change-project")
    image = upload_image(client, project["id"])
    region = _create_region(client, image["id"], 20, 30)
    recognition = with_detection_evidence({}, 0.77, "generated-detector")
    recognition = with_ocr_evidence(
        recognition,
        0.82,
        "generated-ocr",
        attempts=[
            {
                "provider": "generated-ocr",
                "inputVariant": "original",
                "confidence": 0.82,
                "direction": "vertical",
            }
        ],
        selected_index=0,
    )
    recognition["trust"] = {
        "policyVersion": 0,
        "disposition": "trusted",
        "reason": "human-confirmed",
    }
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        persisted = session.get(TextRegion, region["id"])
        assert persisted is not None
        persisted.recognition = recognition
        persisted.confirmed = True

    current = client.get(f"/api/images/{image['id']}/regions").json()[0]
    assert current["confirmed"] is True
    assert current["detectorConfidence"] == 0.77
    assert current["ocrConfidence"] == 0.82
    assert current["recognition"]["ocr"]["attempts"][0]["inputVariant"] == "original"
    assert current["trustDisposition"] == "review"
    assert current["trustReason"] == "policy-version-changed"

    reconfirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": current["revision"]},
    )
    assert reconfirmed.status_code == 200, reconfirmed.text
    assert reconfirmed.json()["confirmed"] is True
    assert reconfirmed.json()["trustDisposition"] == "trusted"
    assert reconfirmed.json()["trustReason"] == "human-confirmed"


@pytest.mark.parametrize(
    ("disposition", "reason", "expected_reason"),
    (
        ("trusted", "automatic-proposal", "policy-version-changed"),
        ("ignored", "human-ignored", "trust-input-changed"),
    ),
)
def test_contradictory_trust_records_fail_closed_and_can_be_reconfirmed(
    client: TestClient,
    app,
    tmp_path: Path,
    disposition: str,
    reason: str,
    expected_reason: str,
) -> None:
    project = create_project(client, tmp_path / f"contradictory-{disposition}")
    image = upload_image(client, project["id"])
    region = _create_region(client, image["id"], 20, 30)
    recognition = with_detection_evidence({}, 0.99, "generated-detector")
    recognition["trust"] = {
        "policyVersion": 1,
        "disposition": disposition,
        "reason": reason,
    }
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        persisted = session.get(TextRegion, region["id"])
        assert persisted is not None
        persisted.recognition = recognition
        persisted.confirmed = True
        persisted.ignored = False

    current = client.get(f"/api/images/{image['id']}/regions").json()[0]
    assert current["confirmed"] is True
    assert current["ignored"] is False
    assert current["trustDisposition"] == "review"
    assert current["trustReason"] == expected_reason

    reconfirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": current["revision"]},
    )
    assert reconfirmed.status_code == 200, reconfirmed.text
    assert reconfirmed.json()["trustDisposition"] == "trusted"
    assert reconfirmed.json()["trustReason"] == "human-confirmed"


def test_region_must_remain_inside_image(client: TestClient, tmp_path: Path) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    response = client.post(
        f"/api/images/{image['id']}/regions",
        json={"x": 230, "y": 10, "width": 20, "height": 20},
    )
    assert response.status_code == 400
    invalid_type = client.post(
        f"/api/images/{image['id']}/regions",
        json={"x": 10, "y": 10, "width": 20, "height": 20, "type": "not-a-region-type"},
    )
    assert invalid_type.status_code == 422


def test_geometry_edit_discards_stale_detector_mask_polygon(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 12,
            "width": 40,
            "height": 50,
            "sourceText": "文字",
            "confirmed": True,
            "repair": {
                "detectorGenerated": True,
                "maskPolygon": [[10, 12], [50, 12], [50, 62], [10, 62]],
            },
        },
    )
    assert created.status_code == 201, created.text
    region = created.json()
    assert region["confirmed"] is True
    assert region["repair"]["detectorGenerated"] is True
    assert "maskPolygon" in region["repair"]
    assert region["repair"]["maskMode"] == "text"
    assert region["repair"]["textPolarity"] == "auto"
    assert region["repair"]["maskPadding"] == 4
    assert region["repair"]["dilation"] == 2
    assert region["repair"]["feather"] == 2

    moved = client.patch(
        f"/api/regions/{region['id']}",
        json={"x": 20, "width": 55, "expectedRevision": region["revision"]},
    )

    assert moved.status_code == 200, moved.text
    updated = moved.json()
    assert updated["x"] == 20
    assert updated["width"] == 55
    assert updated["confirmed"] is False
    assert updated["repair"]["detectorGenerated"] is True
    assert "maskPolygon" not in updated["repair"]


def test_geometry_edit_discards_stale_polygon_from_a_full_frontend_snapshot(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    created = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 12,
            "width": 40,
            "height": 50,
            "sourceText": "文字",
            "confirmed": True,
            "repair": {
                "detectorGenerated": True,
                "maskPolygon": [[10, 12], [50, 12], [50, 62], [10, 62]],
            },
        },
    )
    assert created.status_code == 201, created.text
    region = created.json()
    assert region["confirmed"] is True

    moved = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "x": 20,
            "y": region["y"],
            "width": region["width"],
            "height": region["height"],
            "rotation": region["rotation"],
            "sourceText": region["sourceText"],
            "translationText": region["translationText"],
            "type": region["type"],
            "direction": region["direction"],
            "order": region["order"],
            "confidence": region["confidence"],
            "ignored": region["ignored"],
            "confirmed": region["confirmed"],
            "style": region["style"],
            "repair": region["repair"],
            "expectedRevision": region["revision"],
        },
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["confirmed"] is False
    assert "maskPolygon" not in moved.json()["repair"]


def test_default_manga_reading_order_is_right_column_top_to_bottom(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    left_top = _create_region(client, image["id"], 20, 20)
    right_bottom = _create_region(client, image["id"], 160, 170)
    left_bottom = _create_region(client, image["id"], 20, 170)
    right_top = _create_region(client, image["id"], 160, 20)

    response = client.post(
        f"/api/images/{image['id']}/reading-order",
        json={"mode": "manga-vertical"},
    )
    assert response.status_code == 200
    ordered = response.json()
    assert [region["id"] for region in ordered] == [
        right_top["id"],
        right_bottom["id"],
        left_top["id"],
        left_bottom["id"],
    ]
    assert [region["order"] for region in ordered] == [0, 1, 2, 3]


def test_page_review_is_explicit_revisioned_and_reset_by_region_changes(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    initial = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert initial["regionCount"] == 0
    assert initial["status"]["reviewState"] == "pending"
    assert initial["status"]["reviewedAt"] == ""

    empty_as_reviewed = client.patch(
        f"/api/images/{image['id']}/review",
        json={"reviewState": "reviewed", "expectedRevision": initial["revision"]},
    )
    assert empty_as_reviewed.status_code == 400
    assert empty_as_reviewed.json()["detail"] == (
        "Cannot mark image as reviewed without at least one non-ignored region"
    )

    no_text = client.patch(
        f"/api/images/{image['id']}/review",
        json={"reviewState": "no-text-reviewed", "expectedRevision": initial["revision"]},
    )
    assert no_text.status_code == 200, no_text.text
    reviewed_image = no_text.json()
    assert reviewed_image["status"]["reviewState"] == "no-text-reviewed"
    assert reviewed_image["status"]["reviewedAt"]

    stale = client.patch(
        f"/api/images/{image['id']}/review",
        json={"reviewState": "reviewed", "expectedRevision": initial["revision"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["resource"] == f"image:{image['id']}"

    region = _create_region(client, image["id"], 20, 30)
    ignored_region = _create_region(client, image["id"], 100, 30)
    ignored_region = client.patch(
        f"/api/regions/{ignored_region['id']}",
        json={"ignored": True, "expectedRevision": ignored_region["revision"]},
    ).json()
    assert ignored_region["ignored"] is True
    after_create = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert after_create["status"]["reviewState"] == "pending"
    assert after_create["status"]["reviewedAt"] == ""

    text_bypass = client.patch(
        f"/api/images/{image['id']}/review",
        json={
            "reviewState": "no-text-reviewed",
            "expectedRevision": after_create["revision"],
        },
    )
    assert text_bypass.status_code == 400
    assert text_bypass.json()["detail"] == (
        "Cannot mark image as no-text-reviewed while non-ignored regions remain"
    )

    unconfirmed_bypass = client.patch(
        f"/api/images/{image['id']}/review",
        json={"reviewState": "reviewed", "expectedRevision": after_create["revision"]},
    )
    assert unconfirmed_bypass.status_code == 400
    assert unconfirmed_bypass.json()["detail"] == (
        "Cannot mark image as reviewed until every non-ignored region is confirmed "
        "and trusted (1 not ready)"
    )

    confirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={"confirmed": True, "expectedRevision": region["revision"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed"] is True
    after_confirm = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert after_confirm["revision"] > after_create["revision"]
    assert after_confirm["status"]["reviewState"] == "pending"

    reviewed = client.patch(
        f"/api/images/{image['id']}/review",
        json={"reviewState": "reviewed", "expectedRevision": after_confirm["revision"]},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"]["reviewState"] == "reviewed"
    assert reviewed.json()["confirmedCount"] == 1
    assert reviewed.json()["ignoredCount"] == 1

    changed = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "sourceText": "改写",
            "confirmed": True,
            "expectedRevision": confirmed.json()["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["confirmed"] is False

    after_change = client.get(f"/api/projects/{project['id']}/images").json()[0]
    stale_confirmation = client.patch(
        f"/api/images/{image['id']}/review",
        json={"reviewState": "reviewed", "expectedRevision": after_change["revision"]},
    )
    assert stale_confirmation.status_code == 400
    assert "every non-ignored region is confirmed" in stale_confirmation.json()["detail"]

    reconfirmed = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "translationText": "同请求确认",
            "confirmed": True,
            "expectedRevision": changed.json()["revision"],
        },
    )
    assert reconfirmed.status_code == 200, reconfirmed.text
    assert reconfirmed.json()["confirmed"] is True

    ignored = client.patch(
        f"/api/regions/{region['id']}",
        json={"ignored": True, "expectedRevision": reconfirmed.json()["revision"]},
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["ignored"] is True
    assert ignored.json()["confirmed"] is False

    all_ignored_as_reviewed = client.patch(
        f"/api/images/{image['id']}/review",
        json={
            "reviewState": "reviewed",
            "expectedRevision": client.get(f"/api/projects/{project['id']}/images").json()[0][
                "revision"
            ],
        },
    )
    assert all_ignored_as_reviewed.status_code == 400
    assert all_ignored_as_reviewed.json()["detail"] == (
        "Cannot mark image as reviewed without at least one non-ignored region"
    )

    all_ignored_as_no_text = client.patch(
        f"/api/images/{image['id']}/review",
        json={
            "reviewState": "no-text-reviewed",
            "expectedRevision": client.get(f"/api/projects/{project['id']}/images").json()[0][
                "revision"
            ],
        },
    )
    assert all_ignored_as_no_text.status_code == 200, all_ignored_as_no_text.text
    assert all_ignored_as_no_text.json()["status"]["reviewState"] == "no-text-reviewed"

    deleted = client.delete(
        f"/api/regions/{region['id']}",
        params={"expectedRevision": ignored.json()["revision"]},
    )
    assert deleted.status_code == 204
    after_delete = client.get(f"/api/projects/{project['id']}/images").json()[0]
    assert after_delete["status"]["reviewState"] == "pending"
    assert after_delete["status"]["reviewedAt"] == ""


def test_region_rejects_conflicting_flags_and_invalid_mask_edits(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "project")
    image = upload_image(client, project["id"])
    conflicting = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "ignored": True,
            "confirmed": True,
        },
    )
    assert conflicting.status_code == 422

    invalid_edits = (
        {"version": 2, "strokes": []},
        {"version": 1, "strokes": [{"mode": "paint", "radius": 2, "points": [[1, 1]]}]},
        {"version": 1, "strokes": [{"mode": "add", "radius": 0, "points": [[1, 1]]}]},
        {"version": 1, "strokes": [{"mode": "add", "radius": 2, "points": []}]},
    )
    for mask_edits in invalid_edits:
        response = client.post(
            f"/api/images/{image['id']}/regions",
            json={
                "x": 10,
                "y": 10,
                "width": 30,
                "height": 30,
                "repair": {"maskEdits": mask_edits},
            },
        )
        assert response.status_code == 422, response.text

    invalid_repairs = (
        {"maskMode": "polygon"},
        {"textPolarity": "mixed"},
        {"textPolarity": 1},
        {"maskPadding": -1},
        {"maskPadding": 513},
        {"dilation": 129},
        {"feather": 129},
        {"radius": 0},
        {"radius": 257},
        {"method": "magic"},
        {"fillColor": "not-a-color"},
        {
            "maskEdits": {
                "version": 1,
                "strokes": [{"mode": "add", "radius": 1, "points": [[1, 1]]}] * 257,
            }
        },
        {
            "maskEdits": {
                "version": 1,
                "strokes": [
                    {
                        "mode": "add",
                        "radius": 1,
                        "points": [[1, 1]] * 4097,
                    }
                ],
            }
        },
        {
            "maskEdits": {
                "version": 1,
                "strokes": [{"mode": "add", "radius": 513, "points": [[1, 1]]}],
            }
        },
        {"maskPolygon": [[1, 1], [2, 2]]},
        {"x": 999},
        {"polygon": [[1, 1], [2, 2], [3, 3]]},
    )
    for repair in invalid_repairs:
        response = client.post(
            f"/api/images/{image['id']}/regions",
            json={"x": 10, "y": 10, "width": 30, "height": 30, "repair": repair},
        )
        assert response.status_code == 422, (repair, response.text)

    out_of_bounds = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "repair": {
                "maskEdits": {
                    "version": 1,
                    "strokes": [{"mode": "add", "radius": 2, "points": [[image["width"] + 1, 2]]}],
                }
            },
        },
    )
    assert out_of_bounds.status_code == 400

    polygon_out_of_bounds = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "repair": {
                "maskPolygon": [[1, 1], [2, 2], [image["width"] + 1, 3]],
            },
        },
    )
    assert polygon_out_of_bounds.status_code == 400

    valid = client.post(
        f"/api/images/{image['id']}/regions",
        json={
            "x": 10,
            "y": 10,
            "width": 30,
            "height": 30,
            "confirmed": True,
            "repair": {
                "inpainterProvider": "lama",
                "method": "navier_stokes",
                "maskEdits": {
                    "version": 1,
                    "strokes": [{"mode": "add", "radius": 2, "points": [[1, 2], [3.5, 4.5]]}],
                },
            },
        },
    )
    assert valid.status_code == 201, valid.text
    valid_region = valid.json()
    assert valid_region["confirmed"] is True
    assert valid_region["repair"]["inpainterProvider"] == "lama-onnx"
    assert valid_region["repair"]["method"] == "navier-stokes"
    assert valid_region["repair"]["maskEdits"] == {
        "version": 1,
        "strokes": [{"mode": "add", "radius": 2.0, "points": [[1.0, 2.0], [3.5, 4.5]]}],
    }

    autosaved_repair = client.patch(
        f"/api/regions/{valid_region['id']}",
        json={
            "repair": {
                **valid_region["repair"],
                "maskEdits": {
                    "version": 1,
                    "strokes": [{"mode": "erase", "radius": 3, "points": [[15, 15]]}],
                },
            },
            "confirmed": True,
            "expectedRevision": valid_region["revision"],
        },
    )
    assert autosaved_repair.status_code == 200, autosaved_repair.text
    assert autosaved_repair.json()["confirmed"] is False
    assert autosaved_repair.json()["trustDisposition"] == "trusted"
    assert autosaved_repair.json()["trustReason"] == "human-confirmed"
