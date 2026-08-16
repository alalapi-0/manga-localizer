from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from manga_localizer.config import Settings
from manga_localizer.main import create_app
from manga_localizer.workbench_static import cors_origins_for, resolve_frontend_dist, workbench_url


def test_resolve_frontend_dist_requires_index_html(tmp_path: Path) -> None:
    missing = tmp_path / "empty"
    missing.mkdir()
    settings = Settings(data_dir=tmp_path / "catalog", frontend_dist=missing)
    assert resolve_frontend_dist(settings) is None

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Manga Localizer</title>",
        encoding="utf-8",
    )
    found = resolve_frontend_dist(Settings(data_dir=tmp_path / "catalog", frontend_dist=dist))
    assert found == dist.resolve()


def test_api_serves_the_built_workbench_from_the_same_origin(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Manga Localizer</title>",
        encoding="utf-8",
    )
    settings = Settings(data_dir=tmp_path / "catalog", frontend_dist=dist, worker_poll_seconds=0.01)
    with TestClient(create_app(settings, start_worker=False)) as client:
        workbench = client.get("/")
        assert workbench.status_code == 200
        assert "Manga Localizer" in workbench.text
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["lanAccess"] is False
        assert health.json()["companionUrl"] is None


def test_lan_companion_health_exposes_the_phone_url(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Manga Localizer</title>",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "catalog",
        frontend_dist=dist,
        host="192.168.1.20",
        lan_access=True,
        worker_poll_seconds=0.01,
    )
    assert workbench_url(settings) == "http://192.168.1.20:8000"
    assert "http://192.168.1.20:8000" in cors_origins_for(settings)
    with TestClient(create_app(settings, start_worker=False)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["lanAccess"] is True
        assert health.json()["companionUrl"] == "http://192.168.1.20:8000"
        capabilities = client.get("/api/config").json()["capabilities"]
        assert capabilities["lanAccess"] is True
        assert capabilities["companionUrl"] == "http://192.168.1.20:8000"
