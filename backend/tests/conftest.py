from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

# The production setting is intentionally required. Test collection imports the
# module-level ASGI app, so give that unused instance an explicit ephemeral route.
_TEST_DATA_DIRECTORY = tempfile.TemporaryDirectory(prefix="manga-localizer-tests-")
os.environ.setdefault("MANGA_LOCALIZER_DATA_DIR", _TEST_DATA_DIRECTORY.name)

from manga_localizer.config import Settings  # noqa: E402
from manga_localizer.main import create_app  # noqa: E402


def png_bytes(
    size: tuple[int, int] = (240, 320),
    *,
    color: str = "white",
    rectangle: tuple[int, int, int, int] | None = None,
) -> bytes:
    image = Image.new("RGB", size, color)
    if rectangle:
        ImageDraw.Draw(image).rectangle(rectangle, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "catalog",
        worker_poll_seconds=0.01,
        thumbnail_size=96,
        max_upload_bytes=2 * 1024 * 1024,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings, start_worker=False)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def create_project(client: TestClient, root: Path, name: str = "漫画项目") -> dict[str, Any]:
    response = client.post(
        "/api/projects",
        json={"name": name, "outputPath": str(root)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload_image(
    client: TestClient,
    project_id: str,
    *,
    relative_path: str = "第一章/ページ一.png",
    data: bytes | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/images/upload",
        files=[("files", ("page.png", data or png_bytes(), "image/png"))],
        data={"relativePaths": f'["{relative_path}"]'},
    )
    assert response.status_code == 201, response.text
    return response.json()[0]
