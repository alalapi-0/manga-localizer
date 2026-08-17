from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from manga_localizer.config import Settings
from manga_localizer.main import create_app
from manga_localizer.model_bundle import apply_model_bundle, file_sha256


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_manifest(models_dir: Path, models: list[dict]) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "manifest.json").write_text(
        json.dumps({"schemaVersion": 1, "downloadsAtStartup": False, "models": models}),
        encoding="utf-8",
    )


def test_apply_model_bundle_keeps_verified_file_and_rejects_checksum_mismatch(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models"
    good = b"verified-ppocr"
    bad = b"tampered-lama"
    (models).mkdir()
    (models / "ppocr.onnx").write_bytes(good)
    (models / "lama.onnx").write_bytes(bad)
    _write_manifest(
        models,
        [
            {
                "name": "ppocr",
                "kind": "file",
                "path": "ppocr.onnx",
                "sha256": _sha(good),
                "license": "Apache-2.0",
                "setting": "ppocr_detection_model",
            },
            {
                "name": "lama",
                "kind": "file",
                "path": "lama.onnx",
                "sha256": _sha(b"expected-lama"),
                "license": "Apache-2.0",
                "setting": "lama_inpainting_model",
            },
        ],
    )
    settings = Settings(data_dir=tmp_path / "catalog", model_bundle=models)
    applied, health = apply_model_bundle(settings)
    assert health is not None
    assert health["downloadsAtStartup"] is False
    assert health["models"]["ppocr"]["available"] is True
    assert health["models"]["lama"]["available"] is False
    assert health["models"]["lama"]["error"] == "checksum mismatch"
    assert applied.ppocr_detection_model == models / "ppocr.onnx"
    assert applied.lama_inpainting_model == models / ".unverified" / "lama.onnx"
    assert file_sha256(models / "ppocr.onnx") == _sha(good)


def test_create_app_reports_bundled_checksum_failure_as_unavailable(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "ppocr.onnx").write_bytes(b"wrong")
    _write_manifest(
        models,
        [
            {
                "name": "ppocr",
                "kind": "file",
                "path": "ppocr.onnx",
                "sha256": _sha(b"right"),
                "license": "Apache-2.0",
                "setting": "ppocr_detection_model",
            }
        ],
    )
    settings = Settings(
        data_dir=tmp_path / "catalog",
        model_bundle=models,
        worker_poll_seconds=0.01,
    )
    with TestClient(create_app(settings, start_worker=False)) as client:
        health = client.get("/api/health").json()
        assert health["bundledModels"]["downloadsAtStartup"] is False
        assert health["bundledModels"]["models"]["ppocr"]["available"] is False
        assert health["bundledModels"]["models"]["ppocr"]["error"] == "checksum mismatch"
        config = client.get("/api/config").json()
        assert config["providers"]["detection"]["ppocr-v3"]["available"] is False
