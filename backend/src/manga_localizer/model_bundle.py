"""Verify checksum-bound optional models that were copied into an application bundle.

Ordinary application startup never downloads weights. A missing or mismatched
bundled file stays unavailable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from manga_localizer.config import Settings

MANIFEST_NAME = "manifest.json"
UNVERIFIED_DIR = ".unverified"
SCHEMA_VERSION = 1


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_ready(extract_path: Path) -> bool:
    return (
        extract_path.is_dir()
        and (extract_path / "metadata.json").is_file()
        and (extract_path / "sentencepiece.model").is_file()
        and (extract_path / "model" / "model.bin").is_file()
    )


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("Bundled model manifest is missing or unsupported")
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("Bundled model manifest does not list any models")
    return payload


def verify_entry(models_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    name = str(entry.get("name") or "model")
    kind = str(entry.get("kind") or "file")
    relative = str(entry.get("path") or "")
    expected = str(entry.get("sha256") or "")
    license_name = str(entry.get("license") or "")
    if not relative or len(expected) != 64:
        return {
            "name": name,
            "available": False,
            "error": "manifest entry is incomplete",
            "license": license_name,
        }
    if kind == "archive":
        archive_name = str(entry.get("archive") or "")
        archive = models_dir / archive_name if archive_name else None
        extract = models_dir / relative
        if archive is None or not archive.is_file():
            return {
                "name": name,
                "available": False,
                "error": "archive is missing",
                "license": license_name,
            }
        if file_sha256(archive) != expected:
            return {
                "name": name,
                "available": False,
                "error": "checksum mismatch",
                "license": license_name,
            }
        if not archive_ready(extract):
            return {
                "name": name,
                "available": False,
                "error": "extracted package is incomplete",
                "license": license_name,
            }
        return {"name": name, "available": True, "error": None, "license": license_name}
    target = models_dir / relative
    if not target.is_file():
        return {
            "name": name,
            "available": False,
            "error": "model file is missing",
            "license": license_name,
        }
    if file_sha256(target) != expected:
        return {
            "name": name,
            "available": False,
            "error": "checksum mismatch",
            "license": license_name,
        }
    return {"name": name, "available": True, "error": None, "license": license_name}


def _failed_path(models_dir: Path, entry: dict[str, Any]) -> Path:
    relative = Path(str(entry.get("path") or entry.get("name") or "model"))
    return models_dir / UNVERIFIED_DIR / relative.name


def apply_model_bundle(settings: Settings) -> tuple[Settings, dict[str, Any] | None]:
    if settings.model_bundle is None:
        return settings, None
    models_dir = settings.model_bundle.expanduser()
    manifest_path = models_dir / MANIFEST_NAME
    health: dict[str, Any] = {
        "present": True,
        "downloadsAtStartup": False,
        "error": None,
        "models": {},
    }
    try:
        payload = load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        health["error"] = str(error) or "Bundled model manifest is missing"
        return settings, health

    updates: dict[str, Path] = {}
    for raw in payload.get("models") or []:
        if not isinstance(raw, dict):
            continue
        result = verify_entry(models_dir, raw)
        health["models"][result["name"]] = {
            "available": result["available"],
            "error": result["error"],
            "license": result["license"],
        }
        setting = str(raw.get("setting") or "")
        if not setting:
            continue
        if result["available"]:
            updates[setting] = models_dir / str(raw["path"])
        else:
            updates[setting] = _failed_path(models_dir, raw)
    return settings.model_copy(update=updates), health
