from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from manga_localizer.database import ImageAsset, ImportBoundary, Job, JobStatus, TextRegion
from manga_localizer.logging_utils import redact, without_secrets
from manga_localizer.security import (
    UnsafePathError,
    atomic_copy_file,
    atomic_write_bytes,
    cleanup_stale_atomic_temps,
    portable_path_key,
    resolve_within,
    resolve_write_target,
    safe_relative_path,
)
from manga_localizer.services.images import stage_artifact_checksums, stage_reviews
from manga_localizer.services.projects import ProjectError, ProjectStore

_BUNDLE_TEMP_RE = re.compile(r"^\.project\.(?:json|sqlite3)\.[0-9a-f]{32}\.tmp$")
_BUNDLE_SQLITE_SIDECAR_RE = re.compile(
    r"^(\.project\.sqlite3\.[0-9a-f]{32}\.tmp)-(?:journal|wal|shm)$"
)
_BUNDLE_OWNER_RE = re.compile(r"^\.manga-localizer-bundle\.[0-9a-f]{32}\.owner$")


@dataclass(frozen=True)
class _InputProtection:
    files: frozenset[Path]
    directories: frozenset[Path]


def _database_project_id(database_path: Path) -> str:
    try:
        with sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True) as database:
            rows = database.execute("SELECT id FROM projects LIMIT 2").fetchall()
    except sqlite3.Error as error:
        raise ProjectError("Existing export project database is not readable") from error
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise ProjectError("Existing export project database has no unique project id")
    return rows[0][0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy_verified(
    source: Path,
    destination: Path,
    expected_checksum: str,
    *,
    label: str,
) -> None:
    """Copy one opened byte stream and publish it only when its digest is approved."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256()
    try:
        try:
            with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        except OSError as error:
            raise ProjectError(f"{label} could not be copied safely") from error
        if digest.hexdigest() != expected_checksum:
            raise ProjectError(f"{label} changed after review; review it again before exporting")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _portable_image_status(
    value: Any,
    available_stages: set[str],
    *,
    include_provider_keys: bool = True,
) -> dict[str, Any]:
    status = dict(value) if isinstance(value, dict) else {}
    provider_keys = {
        "preprocess": "preprocessingProvider",
        "inpaint": "inpaintingProvider",
        "typeset": "typesettingProvider",
    }
    for stage, provider_key in provider_keys.items():
        if stage in available_stages:
            continue
        status[stage] = "pending"
        if include_provider_keys:
            status[provider_key] = ""
        else:
            status.pop(provider_key, None)
    reviews = status.get("stageReviews")
    if isinstance(reviews, dict):
        retained: dict[str, dict[str, str | int]] = {}
        for stage in available_stages:
            record = reviews.get(stage)
            if not isinstance(record, dict):
                continue
            keys = ["state", "reviewedAt", "resultRevision", "artifactChecksum"]
            if stage == "inpaint":
                keys.append("maskChecksum")
            retained[stage] = {key: record[key] for key in keys if key in record}
        if retained:
            status["stageReviews"] = retained
        else:
            status.pop("stageReviews", None)
    if "inpaint" not in available_stages or "typeset" not in available_stages:
        status["export"] = "pending"
    return status


def _portable_assets(
    store: ProjectStore,
) -> tuple[
    list[tuple[Path, Path, str]],
    list[tuple[Path | None, Path, str | None]],
    dict[str, set[str]],
]:
    with store.session() as session:
        images = list(session.scalars(select(ImageAsset)).all())
    sources: list[tuple[Path, Path, str]] = []
    generated: list[tuple[Path | None, Path, str | None]] = []
    available_stages: dict[str, set[str]] = {}
    for image in images:
        source_relative = safe_relative_path(image.source_path)
        if not source_relative.parts or source_relative.parts[0] != "source":
            raise ProjectError("Portable image source is outside immutable source storage")
        source = resolve_write_target(store.root, source_relative)
        if not source.is_file() or _sha256(source) != image.checksum:
            raise ProjectError(
                f"Immutable source copy is missing or changed: {image.relative_path}"
            )
        sources.append(
            (
                source,
                source_relative,
                image.checksum,
            )
        )
        image_relative = safe_relative_path(image.relative_path).with_suffix(".png")
        reviews = stage_reviews(image)
        current: set[str] = set()
        for stage in ("preprocess", "inpaint", "typeset"):
            review = reviews.get(stage)
            if image.status.get(stage) != "done" or review is None:
                continue
            if review.get("state") != "accepted":
                continue
            try:
                actual = stage_artifact_checksums(store, image, stage)
            except ProjectError:
                continue
            if all(review.get(key) == checksum for key, checksum in actual.items()):
                current.add(stage)
        if "inpaint" not in current:
            current.discard("typeset")
        available_stages[image.id] = current
        variants = (
            ("preprocessed", "preprocess", "artifactChecksum"),
            ("inpainted", "inpaint", "artifactChecksum"),
            ("typeset", "typeset", "artifactChecksum"),
            ("masks", "inpaint", "maskChecksum"),
        )
        for variant, stage, checksum_key in variants:
            relative = Path("generated") / variant / image_relative
            source = resolve_write_target(
                store.root,
                relative,
                protected_roots=(store.source_root,),
            )
            included = stage in current and source.is_file()
            expected = reviews.get(stage, {}).get(checksum_key) if included else None
            generated.append(
                (
                    source if included else None,
                    relative,
                    str(expected) if included else None,
                )
            )
    return sources, generated, available_stages


def _verify_portable_assets(
    assets: tuple[
        list[tuple[Path, Path, str]],
        list[tuple[Path | None, Path, str | None]],
        dict[str, set[str]],
    ],
) -> None:
    sources, generated, _available_stages = assets
    for source, _relative, checksum in sources:
        if not source.is_file() or _sha256(source) != checksum:
            raise ProjectError("Immutable source changed during portable bundle finalization")
    for source, _relative, checksum in generated:
        if source is None:
            continue
        if checksum is None or not source.is_file() or _sha256(source) != checksum:
            raise ProjectError("Reviewed generated artifact changed during bundle finalization")


def _input_protection(store: ProjectStore) -> _InputProtection:
    with store.session() as session:
        recorded = session.scalars(
            select(ImageAsset.input_path).where(ImageAsset.input_path.is_not(None))
        ).all()
        boundaries = list(session.scalars(select(ImportBoundary)).all())
    files = {Path(path).expanduser().resolve() for path in recorded if path}
    directories: set[Path] = set()
    for boundary in boundaries:
        resolved = Path(boundary.path).expanduser().resolve()
        if boundary.kind == "directory":
            directories.add(resolved)
        else:
            files.add(resolved)
    return _InputProtection(frozenset(files), frozenset(directories))


def _assert_not_original(target: Path, protection: _InputProtection) -> None:
    resolved = target.resolve()
    if resolved in protection.files or any(
        resolved == directory or resolved.is_relative_to(directory)
        for directory in protection.directories
    ):
        raise UnsafePathError("Export target would overwrite an original imported file")


def _export_write_target(store: ProjectStore, export_root: Path, relative: str | Path) -> Path:
    return resolve_write_target(
        export_root,
        relative,
        protected_roots=(store.source_root, export_root / "source"),
    )


def _validate_export_artifact_targets(
    store: ProjectStore,
    export_root: Path,
    job_id: str,
    protection: _InputProtection,
) -> None:
    with store.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ProjectError("Export job was not found")
        image_ids = {item.image_id for item in job.items if item.image_id}
        images = list(session.scalars(select(ImageAsset).where(ImageAsset.id.in_(image_ids))).all())
        options = dict(job.options)
    preserve_tree = bool(options.get("preserveTree", True))
    export_format = str(options.get("format", "both"))
    image_variant = str(options.get("imageVariant", "typeset"))
    if export_format == "both":
        for bundle_target in ("project/project.json", "project/project.sqlite3"):
            _assert_not_original(
                _export_write_target(store, export_root, bundle_target),
                protection,
            )
    elif export_format == "json":
        _assert_not_original(
            _export_write_target(store, export_root, "export.json"),
            protection,
        )
    for image in images:
        source_relative = safe_relative_path(image.relative_path)
        relative = source_relative if preserve_tree else Path(source_relative.name)
        targets: list[Path] = []
        if export_format in {"images", "both"}:
            if image_variant in {"typeset", "both"}:
                targets.append(Path("translated") / relative.with_suffix(".png"))
            if image_variant in {"inpainted", "both"}:
                targets.append(Path("clean") / relative.with_suffix(".png"))
            targets.append(Path("masks") / relative.with_suffix(".png"))
        if export_format in {"json", "both"}:
            targets.extend(
                (
                    Path("original-text") / relative.with_suffix(".json"),
                    Path("translated-text") / relative.with_suffix(".json"),
                )
            )
        for relative_target in targets:
            _assert_not_original(
                _export_write_target(store, export_root, relative_target),
                protection,
            )


def _validate_portable_asset_targets(
    store: ProjectStore,
    export_root: Path,
    *,
    allow_refresh: bool,
    protection: _InputProtection,
    assets: tuple[
        list[tuple[Path, Path, str]],
        list[tuple[Path | None, Path, str | None]],
        dict[str, set[str]],
    ]
    | None = None,
) -> None:
    for directory_name in ("source", "generated"):
        directory = export_root / directory_name
        if directory.is_symlink():
            raise UnsafePathError(f"Export {directory_name} directory must not be a symlink")
        if directory.exists() and not directory.is_dir():
            raise ProjectError(f"Export {directory_name} path exists and is not a directory")
    sources, generated, _available_stages = assets or _portable_assets(store)
    for _source, relative, checksum in sources:
        entry = export_root.joinpath(relative)
        target = resolve_write_target(
            export_root,
            relative,
            protected_roots=(store.source_root,),
        )
        cleanup_stale_atomic_temps(target)
        _assert_not_original(target, protection)
        if entry.is_symlink():
            raise UnsafePathError("Portable source files must not be symlinks")
        if target.exists() and not target.is_file():
            raise ProjectError(f"Portable source conflict: {relative.as_posix()}")
        if target.is_file() and _sha256(target) != checksum and not allow_refresh:
            raise ProjectError(f"Portable source conflict: {relative.as_posix()}")
    for source, relative, expected_checksum in generated:
        entry = export_root.joinpath(relative)
        target = _export_write_target(store, export_root, relative)
        cleanup_stale_atomic_temps(target)
        _assert_not_original(target, protection)
        if entry.is_symlink():
            raise UnsafePathError("Portable generated files must not be symlinks")
        if target.exists() and not target.is_file():
            raise ProjectError(f"Portable generated path is not a file: {relative.as_posix()}")
        if source is None and target.exists() and not allow_refresh:
            raise ProjectError(f"Portable generated conflict: {relative.as_posix()}")
        if (
            source is not None
            and target.is_file()
            and not allow_refresh
            and _sha256(target) != expected_checksum
        ):
            raise ProjectError(f"Portable generated conflict: {relative.as_posix()}")


def _copy_portable_assets(
    store: ProjectStore,
    export_root: Path,
    protection: _InputProtection,
    assets: tuple[
        list[tuple[Path, Path, str]],
        list[tuple[Path | None, Path, str | None]],
        dict[str, set[str]],
    ],
) -> None:
    sources, generated, _available_stages = assets
    for source, relative, checksum in sources:
        target = resolve_write_target(
            export_root,
            relative,
            protected_roots=(store.source_root,),
        )
        _assert_not_original(target, protection)
        _atomic_copy_verified(
            source,
            target,
            checksum,
            label="Portable immutable source",
        )
    for source, relative, expected_checksum in generated:
        target = _export_write_target(store, export_root, relative)
        _assert_not_original(target, protection)
        if source is None:
            target.unlink(missing_ok=True)
        else:
            assert expected_checksum is not None
            _atomic_copy_verified(
                source,
                target,
                expected_checksum,
                label="Reviewed portable generated artifact",
            )


def _sanitize_portable_database(
    database_path: Path,
    *,
    finalized_job_id: str | None = None,
    available_stages: dict[str, set[str]] | None = None,
) -> None:
    """Remove machine-only paths while keeping the copied database directly reopenable."""

    def sanitize_json(encoded: str | None) -> str:
        try:
            value = json.loads(encoded) if encoded else None
        except (TypeError, json.JSONDecodeError):
            value = None
        return json.dumps(redact(without_secrets(value)), ensure_ascii=False)

    with sqlite3.connect(database_path) as database:
        # The live project uses WAL mode. A portable copy must be self-contained in the main
        # database file before it is atomically renamed into the export bundle.
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA secure_delete=ON")
        database.execute("UPDATE projects SET root_path = '.', input_root = NULL")
        database.execute("DELETE FROM import_boundaries")
        database.execute("UPDATE images SET input_path = NULL")
        for image_id, encoded_status in database.execute(
            "SELECT id, status FROM images"
        ).fetchall():
            try:
                status = json.loads(encoded_status) if encoded_status else {}
            except (TypeError, json.JSONDecodeError):
                status = {}
            portable_status = _portable_image_status(
                status,
                (available_stages or {}).get(image_id, set()),
            )
            database.execute(
                "UPDATE images SET status = ? WHERE id = ?",
                (json.dumps(portable_status, ensure_ascii=False), image_id),
            )
        for project_id, encoded_settings in database.execute(
            "SELECT id, settings FROM projects"
        ).fetchall():
            database.execute(
                "UPDATE projects SET settings = ? WHERE id = ?",
                (sanitize_json(encoded_settings), project_id),
            )
        for image_id, encoded_errors in database.execute(
            "SELECT id, processing_errors FROM images"
        ).fetchall():
            database.execute(
                "UPDATE images SET processing_errors = ? WHERE id = ?",
                (sanitize_json(encoded_errors), image_id),
            )
        for revision_id, before, after in database.execute(
            "SELECT id, before, after FROM revisions"
        ).fetchall():
            database.execute(
                "UPDATE revisions SET before = ?, after = ? WHERE id = ?",
                (
                    sanitize_json(before) if before is not None else None,
                    sanitize_json(after) if after is not None else None,
                    revision_id,
                ),
            )
        for job_id, encoded_options in database.execute("SELECT id, options FROM jobs").fetchall():
            try:
                options = json.loads(encoded_options) if encoded_options else {}
            except (TypeError, json.JSONDecodeError):
                options = {}
            if isinstance(options, dict):
                options = redact(without_secrets(options))
                options = {
                    key: value
                    for key, value in options.items()
                    if re.sub(r"[^a-z0-9]", "", str(key).lower()) != "outputpath"
                }
                if job_id == finalized_job_id:
                    options["bundleFinalized"] = True
            database.execute(
                "UPDATE jobs SET options = ?, status = CASE WHEN id = ? THEN 'completed' "
                "ELSE status END, error = CASE WHEN id = ? THEN NULL ELSE error END WHERE id = ?",
                (
                    json.dumps(options, ensure_ascii=False),
                    finalized_job_id,
                    finalized_job_id,
                    job_id,
                ),
            )
        for item_id, encoded_output in database.execute(
            "SELECT id, output FROM job_items"
        ).fetchall():
            database.execute(
                "UPDATE job_items SET output = ? WHERE id = ?",
                (sanitize_json(encoded_output), item_id),
            )
        for job_id, error in database.execute("SELECT id, error FROM jobs").fetchall():
            database.execute(
                "UPDATE jobs SET error = ? WHERE id = ?",
                (redact(error) if error else None, job_id),
            )
        for item_id, error in database.execute("SELECT id, error FROM job_items").fetchall():
            database.execute(
                "UPDATE job_items SET error = ? WHERE id = ?",
                (redact(error) if error else None, item_id),
            )
        # Rebuild the copied file so deleted local paths and replaced secret-bearing text cannot
        # survive in free pages of the portable SQLite artifact.
        database.commit()
        database.execute("VACUUM")


def _portable_manifest_bytes(
    store: ProjectStore,
    finalized_job_id: str | None,
    available_stages: dict[str, set[str]],
) -> bytes:
    payload = json.loads(store.manifest_path.read_text("utf-8"))
    for image in payload.get("images", []):
        if not isinstance(image, dict) or not isinstance(image.get("id"), str):
            continue
        stages = available_stages.get(image["id"], set())
        image["status"] = _portable_image_status(
            image.get("status"),
            stages,
            include_provider_keys=False,
        )
        providers = image.get("providers")
        if isinstance(providers, dict):
            if "preprocess" not in stages:
                providers["preprocessing"] = None
            if "inpaint" not in stages:
                providers["inpainting"] = None
            if "typeset" not in stages:
                providers["typesetting"] = None
    if finalized_job_id is not None:
        for job in payload.get("jobs", []):
            if isinstance(job, dict) and job.get("id") == finalized_job_id:
                job["status"] = JobStatus.COMPLETED.value
                job["error"] = None
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _bundle_owner_name(store: ProjectStore, job_id: str) -> str:
    identity = f"{store.project().id}:{job_id}".encode()
    return f".manga-localizer-bundle.{hashlib.sha256(identity).hexdigest()[:32]}.owner"


def _partial_bundle_owned_by(
    store: ProjectStore,
    project_entry: Path,
    recovery_job_id: str | None,
) -> bool:
    if recovery_job_id is None:
        return False
    owner = project_entry / _bundle_owner_name(store, recovery_job_id)
    return owner.is_file() and not owner.is_symlink()


def _clean_owned_partial_bundle(
    store: ProjectStore,
    project_entry: Path,
    recovery_job_id: str,
) -> None:
    owner_name = _bundle_owner_name(store, recovery_job_id)
    allowed_final = {"project.json", "project.sqlite3", owner_name}
    entries = list(project_entry.iterdir())
    entry_names = {entry.name for entry in entries}

    def is_owned_temporary(name: str) -> bool:
        if _BUNDLE_TEMP_RE.fullmatch(name):
            return True
        sidecar = _BUNDLE_SQLITE_SIDECAR_RE.fullmatch(name)
        return sidecar is not None and sidecar.group(1) in entry_names

    if any(
        entry.is_symlink()
        or (entry.name not in allowed_final and not is_owned_temporary(entry.name))
        for entry in entries
    ):
        raise ProjectError("Partial export project directory contains unrelated content")
    for entry in entries:
        if entry.name != owner_name:
            if not entry.is_file():
                raise ProjectError("Partial export project directory contains a non-file entry")
            entry.unlink()


def validate_project_bundle_target(
    store: ProjectStore,
    export_root: Path,
    *,
    recovery_job_id: str | None = None,
) -> bool:
    """Reject ambiguous, symlinked, or differently owned portable project targets."""
    export_root = export_root.resolve()
    if export_root == store.root.resolve():
        return True
    project_entry = export_root / "project"
    if project_entry.is_symlink():
        raise UnsafePathError("Export project directory must not be a symlink")
    if not project_entry.exists():
        return False
    if not project_entry.is_dir():
        raise ProjectError("Export project path exists and is not a directory")
    if not any(project_entry.iterdir()):
        return False
    manifest_entry = project_entry / "project.json"
    database_entry = project_entry / "project.sqlite3"
    if manifest_entry.is_symlink() or database_entry.is_symlink():
        raise UnsafePathError("Export project files must not be symlinks")
    if not manifest_entry.is_file() or not database_entry.is_file():
        if _partial_bundle_owned_by(store, project_entry, recovery_job_id):
            assert recovery_job_id is not None
            _clean_owned_partial_bundle(store, project_entry, recovery_job_id)
            return True
        raise ProjectError(
            "A non-empty export project directory must contain a complete project bundle"
        )
    manifest = resolve_within(export_root, "project/project.json", allow_missing=False)
    database = resolve_within(export_root, "project/project.sqlite3", allow_missing=False)
    try:
        payload = json.loads(manifest.read_text("utf-8"))
        manifest_id = payload.get("project", {}).get("id")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectError("Existing export project manifest is not readable") from error
    expected_id = store.project().id
    if manifest_id != expected_id or _database_project_id(database) != expected_id:
        raise ProjectError("Export target belongs to a different portable project")
    return True


def choose_export_root(
    store: ProjectStore,
    output_path: str | None,
    job_id: str,
    *,
    include_assets: bool = True,
) -> Path:
    root_entry = Path(output_path).expanduser() if output_path else store.root
    if root_entry.is_symlink():
        raise UnsafePathError("Export root must not be a symlink")
    root = root_entry.resolve()
    source_root = store.source_root.resolve()
    overlaps_source = (
        root == source_root
        or root.is_relative_to(source_root)
        or (source_root.is_relative_to(root) and root != store.root)
    )
    if overlaps_source:
        raise UnsafePathError("Export root must not overlap the immutable source tree")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "project").is_symlink():
        raise UnsafePathError("Export project directory must not be a symlink")
    protection = _input_protection(store)
    _validate_export_artifact_targets(store, root, job_id, protection)
    allow_refresh = validate_project_bundle_target(
        store,
        root,
        recovery_job_id=job_id,
    )
    if root != store.root.resolve() and include_assets:
        assets = _portable_assets(store)
        _validate_portable_asset_targets(
            store,
            root,
            allow_refresh=allow_refresh,
            protection=protection,
            assets=assets,
        )
    return root


def _artifact_path(root: Path, path: Path | None) -> str | None:
    return path.relative_to(root).as_posix() if path is not None else None


def ensure_project_bundle(
    store: ProjectStore,
    export_root: Path,
    *,
    finalized_job_id: str | None = None,
) -> None:
    """Place a sanitized, reopenable project snapshot and its local image assets."""
    if export_root.resolve() == store.root.resolve():
        store.write_snapshot()
        return
    allow_refresh = validate_project_bundle_target(
        store,
        export_root,
        recovery_job_id=finalized_job_id,
    )
    protection = _input_protection(store)
    assets = _portable_assets(store)
    _sources, _generated, available_stages = assets
    project_root = _export_write_target(store, export_root, "project")
    project_root.mkdir(parents=True, exist_ok=True)
    if finalized_job_id is not None:
        owner_name = _bundle_owner_name(store, finalized_job_id)
        owner_destination = _export_write_target(
            store,
            export_root,
            f"project/{owner_name}",
        )
        _assert_not_original(owner_destination, protection)
        if not owner_destination.exists():
            with owner_destination.open("xb"):
                pass
        for entry in project_root.iterdir():
            if (
                entry.name != owner_name
                and _BUNDLE_OWNER_RE.fullmatch(entry.name)
                and entry.is_file()
                and not entry.is_symlink()
            ):
                entry.unlink()
    _validate_portable_asset_targets(
        store,
        export_root,
        allow_refresh=allow_refresh,
        protection=protection,
        assets=assets,
    )
    _copy_portable_assets(store, export_root, protection, assets)
    _verify_portable_assets(assets)
    store.write_snapshot()
    manifest_destination = _export_write_target(store, export_root, "project/project.json")
    database_destination = _export_write_target(store, export_root, "project/project.sqlite3")
    _assert_not_original(manifest_destination, protection)
    _assert_not_original(database_destination, protection)
    nonce = uuid.uuid4().hex
    manifest_temporary = _export_write_target(
        store,
        export_root,
        f"project/.project.json.{nonce}.tmp",
    )
    database_temporary = _export_write_target(
        store,
        export_root,
        f"project/.project.sqlite3.{nonce}.tmp",
    )
    try:
        with manifest_temporary.open("xb") as manifest_handle:
            manifest_handle.write(
                _portable_manifest_bytes(store, finalized_job_id, available_stages)
            )
            manifest_handle.flush()
            os.fsync(manifest_handle.fileno())
        with (
            sqlite3.connect(store.database_path) as source_database,
            sqlite3.connect(database_temporary) as destination_database,
        ):
            source_database.backup(destination_database)
        _sanitize_portable_database(
            database_temporary,
            finalized_job_id=finalized_job_id,
            available_stages=available_stages,
        )
        database_temporary.replace(database_destination)
        manifest_temporary.replace(manifest_destination)
    finally:
        database_temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            database_temporary.with_name(database_temporary.name + suffix).unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)


def _portable_existing_path(path: Path) -> Path | None:
    if not path.parent.is_dir():
        return None
    desired_key = portable_path_key(path.name)
    matches: list[Path] = []
    for entry in path.parent.iterdir():
        try:
            if portable_path_key(entry.name) == desired_key:
                matches.append(entry)
        except UnsafePathError:
            continue
    if len(matches) > 1:
        raise ProjectError(f"Ambiguous cross-platform export conflict: {path.name}")
    if not matches:
        return None
    match = matches[0]
    if match.is_symlink() or not match.is_file():
        raise UnsafePathError("Export conflict target must be a regular file")
    return match


def _conflict_path(path: Path, conflict: str) -> tuple[Path | None, str]:
    if conflict not in {"rename", "overwrite", "skip"}:
        raise ProjectError("Export conflict strategy must be rename, overwrite, or skip")
    existing = _portable_existing_path(path)
    if existing is None:
        return path, "created"
    if conflict == "skip":
        return None, "skipped"
    if conflict == "overwrite":
        return existing, "overwritten"
    counter = 2
    candidate = path
    while _portable_existing_path(candidate) is not None:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        counter += 1
    return candidate, "renamed"


def _write_copy(
    source: Path,
    target: Path,
    conflict: str,
    *,
    expected_checksum: str | None = None,
) -> tuple[Path | None, str]:
    cleanup_stale_atomic_temps(target)
    destination, resolution = _conflict_path(target, conflict)
    if destination is None:
        return None, resolution
    cleanup_stale_atomic_temps(destination)
    if expected_checksum is None:
        atomic_copy_file(source, destination)
    else:
        _atomic_copy_verified(
            source,
            destination,
            expected_checksum,
            label="Reviewed generated artifact",
        )
    return destination, resolution


def _write_json(payload: dict[str, Any], target: Path, conflict: str) -> tuple[Path | None, str]:
    cleanup_stale_atomic_temps(target)
    destination, resolution = _conflict_path(target, conflict)
    if destination is None:
        return None, resolution
    cleanup_stale_atomic_temps(destination)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(destination, data)
    return destination, resolution


def validate_image_export_readiness(
    store: ProjectStore,
    image_id: str,
    *,
    export_format: str,
    image_variant: str,
) -> dict[str, dict[str, str | int]]:
    """Fail a generated-image export before any destination path is created."""
    if export_format not in {"images", "both"}:
        return {}
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError(f"Export image does not exist: {image_id}")
    source_relative = safe_relative_path(image.relative_path)
    required_reviews = (
        ["inpaint", "typeset"]
        if image_variant == "typeset"
        else ["inpaint"]
        if image_variant == "inpainted"
        else ["inpaint", "typeset"]
    )
    for stage in required_reviews:
        if image.status.get(stage) != "done":
            label = "Rendered" if stage == "typeset" else "Inpainted"
            raise ProjectError(
                f"{label} output is stale for {image.relative_path}; page was not exported"
            )
        directory = "typeset" if stage == "typeset" else "inpainted"
        generated = resolve_write_target(
            store.root,
            Path("generated") / directory / source_relative.with_suffix(".png"),
            protected_roots=(store.source_root,),
        )
        if not generated.is_file():
            label = "Typeset" if stage == "typeset" else "Inpainted"
            raise ProjectError(
                f"{label} output is missing for {image.relative_path}; page was not exported"
            )
    generated_mask = resolve_write_target(
        store.root,
        Path("generated") / "masks" / source_relative.with_suffix(".png"),
        protected_roots=(store.source_root,),
    )
    if not generated_mask.is_file():
        raise ProjectError(
            f"Render mask is missing for {image.relative_path}; page was not exported"
        )
    if image.status.get("reviewState", "pending") not in {
        "reviewed",
        "no-text-reviewed",
    }:
        raise ProjectError(
            f"Page review is pending for {image.relative_path}; page was not exported"
        )
    reviews = stage_reviews(image)
    missing_reviews = [
        stage
        for stage in required_reviews
        if not isinstance(reviews.get(stage), dict)
        or reviews[stage].get("state") != "accepted"
    ]
    if missing_reviews:
        labels = ", ".join(missing_reviews)
        raise ProjectError(
            f"Stage review must be accepted for {labels} before exporting "
            f"{image.relative_path}; page was not exported"
        )
    stale_reviews = [
        stage
        for stage in required_reviews
        if any(
            reviews[stage].get(key) != value
            for key, value in stage_artifact_checksums(store, image, stage).items()
        )
    ]
    if stale_reviews:
        labels = ", ".join(stale_reviews)
        raise ProjectError(
            f"Stage review no longer matches the generated artifact for {labels}; "
            f"review {image.relative_path} again before exporting"
        )
    return reviews


def write_json_export_summary(
    store: ProjectStore,
    export_root: Path,
    job_id: str,
) -> dict[str, str | None]:
    """Write the path-only aggregate index for a metadata-only export."""
    with store.session() as session:
        job = session.get(Job, job_id)
        if job is None or job.kind != "export":
            raise ProjectError("JSON export job was not found")
        project = store.project(session)
        options = dict(job.options)
        items = sorted(job.items, key=lambda item: item.position)

        def item_artifact(output: dict[str, Any], key: str) -> str | None:
            entry = output.get(key)
            artifact = entry.get("artifact") if isinstance(entry, dict) else None
            if artifact is None:
                return None
            if not isinstance(artifact, str):
                raise ProjectError("JSON export item contains an invalid artifact path")
            return safe_relative_path(artifact).as_posix()

        images = []
        for item in items:
            output = dict(item.output or {})
            relative_path = output.get("relativePath")
            export_relative_path = output.get("exportRelativePath")
            if not isinstance(relative_path, str) or not isinstance(export_relative_path, str):
                raise ProjectError("JSON export item is missing relative path metadata")
            images.append(
                {
                    "imageId": item.image_id,
                    "relativePath": safe_relative_path(relative_path).as_posix(),
                    "exportRelativePath": safe_relative_path(export_relative_path).as_posix(),
                    "originalText": item_artifact(output, "originalText"),
                    "translatedText": item_artifact(output, "translatedText"),
                }
            )
        payload = {
            "formatVersion": 1,
            "kind": "manga-localizer-json-export",
            "project": {
                "id": project.id,
                "name": project.name,
                "schemaVersion": project.schema_version,
            },
            "export": {
                "jobId": job.id,
                "format": "json",
                "preserveTree": options.get("preserveTree", True),
            },
            "images": images,
        }
        conflict = str(options.get("conflict", "rename"))
    target = _export_write_target(store, export_root, "export.json")
    path, resolution = _write_json(payload, target, conflict)
    return {
        "artifact": _artifact_path(export_root, path),
        "conflict": resolution,
    }


def export_image(
    store: ProjectStore,
    image_id: str,
    *,
    export_root: Path,
    export_format: str,
    conflict: str,
    preserve_tree: bool = True,
    image_variant: str = "typeset",
) -> dict[str, Any]:
    if export_format not in {"images", "json", "both"}:
        raise ProjectError("Export format must be images, json, or both")
    if image_variant not in {"typeset", "inpainted", "both"}:
        raise ProjectError("Export imageVariant must be typeset, inpainted, or both")
    reviews = validate_image_export_readiness(
        store,
        image_id,
        export_format=export_format,
        image_variant=image_variant,
    )
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError(f"Export image does not exist: {image_id}")
        regions = list(
            session.scalars(
                select(TextRegion)
                .where(TextRegion.image_id == image_id)
                .order_by(TextRegion.reading_order)
            ).all()
        )
    source_relative = safe_relative_path(image.relative_path)
    relative = source_relative if preserve_tree else Path(source_relative.name)
    output: dict[str, Any] = {
        "imageId": image.id,
        "imageRevision": image.revision,
        "relativePath": source_relative.as_posix(),
        "exportRelativePath": relative.as_posix(),
        "imageVariant": image_variant,
    }
    if export_format in {"images", "both"}:
        image_sources: list[tuple[str, str, Path]] = []
        if image_variant in {"typeset", "both"}:
            if image.status.get("typeset") != "done" or image.status.get("inpaint") != "done":
                raise ProjectError(
                    f"Rendered output is stale for {image.relative_path}; page was not exported"
                )
            image_sources.append(
                (
                    "translatedImage",
                    "translated",
                    resolve_write_target(
                        store.root,
                        Path("generated") / "typeset" / source_relative.with_suffix(".png"),
                        protected_roots=(store.source_root,),
                    ),
                )
            )
        if image_variant in {"inpainted", "both"}:
            if image.status.get("inpaint") != "done":
                raise ProjectError(
                    f"Inpainted output is stale for {image.relative_path}; page was not exported"
                )
            image_sources.append(
                (
                    "cleanImage",
                    "clean",
                    resolve_write_target(
                        store.root,
                        Path("generated") / "inpainted" / source_relative.with_suffix(".png"),
                        protected_roots=(store.source_root,),
                    ),
                )
            )
        generated_mask = resolve_write_target(
            store.root,
            Path("generated") / "masks" / source_relative.with_suffix(".png"),
            protected_roots=(store.source_root,),
        )
        for output_key, directory, generated in image_sources:
            image_target = _export_write_target(
                store,
                export_root,
                Path(directory) / relative.with_suffix(".png"),
            )
            review_stage = "typeset" if output_key == "translatedImage" else "inpaint"
            path, resolution = _write_copy(
                generated,
                image_target,
                conflict,
                expected_checksum=str(reviews[review_stage]["artifactChecksum"]),
            )
            output[output_key] = {
                "artifact": _artifact_path(export_root, path),
                "conflict": resolution,
            }
        mask_target = _export_write_target(
            store,
            export_root,
            Path("masks") / relative.with_suffix(".png"),
        )
        mask_path, mask_resolution = _write_copy(
            generated_mask,
            mask_target,
            conflict,
            expected_checksum=str(reviews["inpaint"]["maskChecksum"]),
        )
        output["mask"] = {
            "artifact": _artifact_path(export_root, mask_path),
            "conflict": mask_resolution,
        }
    if export_format in {"json", "both"}:
        image_payload = {
            "formatVersion": 1,
            "image": {
                "id": image.id,
                "relativePath": image.relative_path,
                "width": image.width,
                "height": image.height,
                "checksum": image.checksum,
            },
        }
        original_payload = {
            **image_payload,
            "regions": [
                {
                    "id": region.id,
                    "order": region.reading_order,
                    "sourceText": region.source_text,
                    "confidence": region.confidence,
                    "type": region.region_type,
                    "direction": region.direction,
                    "ignored": region.ignored,
                    "geometry": {
                        "x": region.x,
                        "y": region.y,
                        "width": region.width,
                        "height": region.height,
                        "rotation": region.rotation,
                    },
                }
                for region in regions
            ],
        }
        translated_payload = {
            **image_payload,
            "regions": [
                {
                    "id": region.id,
                    "order": region.reading_order,
                    "translationText": region.translation_text,
                    "confirmed": region.confirmed,
                    "ignored": region.ignored,
                    "style": region.style,
                }
                for region in regions
            ],
        }
        for name, directory, payload in (
            ("originalText", "original-text", original_payload),
            ("translatedText", "translated-text", translated_payload),
        ):
            target = _export_write_target(
                store,
                export_root,
                Path(directory) / relative.with_suffix(".json"),
            )
            path, resolution = _write_json(payload, target, conflict)
            output[name] = {
                "artifact": _artifact_path(export_root, path),
                "conflict": resolution,
            }
    if export_format == "both":
        output["project"] = {
            "manifest": "project/project.json",
            "database": "project/project.sqlite3",
        }
    return output
