from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

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
