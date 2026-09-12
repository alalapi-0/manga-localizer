from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from manga_localizer.database import Base, ImageAsset, PageGeneration, Project
from manga_localizer.security import UnsafePathError
from manga_localizer.services.projects import ProjectError, ProjectStore


@dataclass(frozen=True)
class ControlledRecoveryBinding:
    project_id: str
    project_manifest_path: Path
    project_database_path: Path
    media_root: Path
    expected_schema_digest: str
    expected_startup_mutations: tuple[str, ...]
    image_id: str
    generation_id: str
    run_id: str
    final_review_manifest_path: Path
    final_review_database_path: Path
    final_review_batch_id: str
    final_review_item_id: str
    executable_job_kinds: tuple[str, ...] = ("ocr", "mask", "typeset")


@dataclass(frozen=True)
class ControlledRecoveryPreflight:
    project_root: Path
    schema_digest: str
    startup_mutations: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "projectRoot": str(self.project_root),
            "schemaDigest": self.schema_digest,
            "startupMutations": list(self.startup_mutations),
        }


def _existing_regular_file(path: Path, label: str) -> Path:
    entry = path.expanduser()
    if not entry.is_absolute():
        raise ProjectError(f"{label} must be an absolute path")
    if any(component.is_symlink() for component in (entry, *entry.parents)):
        raise UnsafePathError(f"{label} must not be a symlink")
    resolved = entry.resolve(strict=True)
    if not resolved.is_file():
        raise ProjectError(f"{label} is not a regular file")
    return resolved


def _existing_directory(path: Path, label: str) -> Path:
    entry = path.expanduser()
    if not entry.is_absolute():
        raise ProjectError(f"{label} must be an absolute path")
    if any(component.is_symlink() for component in (entry, *entry.parents)):
        raise UnsafePathError(f"{label} must not be a symlink")
    resolved = entry.resolve(strict=True)
    if not resolved.is_dir():
        raise ProjectError(f"{label} is not a directory")
    return resolved


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def schema_digest(database_path: Path) -> str:
    with _read_only_connection(database_path) as connection:
        rows = connection.execute(
            """SELECT type, name, tbl_name, sql
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type, name"""
        ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preflight_controlled_recovery(
    binding: ControlledRecoveryBinding,
) -> ControlledRecoveryPreflight:
    manifest = _existing_regular_file(binding.project_manifest_path, "Project manifest")
    database = _existing_regular_file(binding.project_database_path, "Project database")
    media_root = _existing_directory(binding.media_root, "Project media root")
    if manifest.name != "project.json" or manifest.parent.name != "project":
        raise ProjectError("Expected a portable project/project.json manifest")
    project_root = manifest.parent.parent.resolve()
    if database != project_root / "project" / "project.sqlite3":
        raise ProjectError("Controlled project database does not match the bound manifest")
    if media_root != project_root:
        raise ProjectError("Controlled media root does not match the bound project root")
    payload = json.loads(manifest.read_text("utf-8"))
    if payload.get("project", {}).get("id") != binding.project_id:
        raise ProjectError("Controlled project manifest id does not match the binding")

    review_manifest = _existing_regular_file(
        binding.final_review_manifest_path, "Final-review manifest"
    )
    review_database = _existing_regular_file(
        binding.final_review_database_path, "Final-review database"
    )
    if review_manifest.name != "manifest.json" or review_manifest.parent.name != "final-review":
        raise ProjectError("Expected a final-review/manifest.json file")
    if review_database != review_manifest.parent / "final-review.sqlite3":
        raise ProjectError("Final-review database does not match the bound manifest")
    review_payload = json.loads(review_manifest.read_text("utf-8"))
    if review_payload.get("batch", {}).get("id") != binding.final_review_batch_id:
        raise ProjectError("Final-review manifest batch does not match the binding")

    if binding.expected_startup_mutations:
        raise ProjectError("Controlled recovery startup mutation plan must be empty")
    actual_digest = schema_digest(database)
    if actual_digest != binding.expected_schema_digest:
        raise ProjectError("Project schema differs from the reviewed controlled-recovery preimage")

    with _read_only_connection(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {name for object_type, name, _table in schema_rows if object_type == "table"}
        triggers = {name for object_type, name, _table in schema_rows if object_type == "trigger"}
        missing_tables = set(Base.metadata.tables) - tables
        missing_columns: dict[str, list[str]] = {}
        for table_name, table in Base.metadata.tables.items():
            if table_name not in tables:
                continue
            existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')}
            missing = sorted(column.name for column in table.columns if column.name not in existing)
            if missing:
                missing_columns[table_name] = missing
        required_triggers = {
            "revisions_g0_no_update",
            "revisions_g0_no_delete",
            "page_lineage_events_no_update",
            "page_lineage_events_no_delete",
            "region_ocr_attempts_validate_insert",
            "region_ocr_attempts_append_only_update",
            "region_ocr_attempts_append_only_delete",
            "text_regions_g4_validate_insert",
            "text_regions_g4_validate_update",
            "text_regions_g5_validate_insert",
            "text_regions_g5_validate_update",
            "text_regions_g6_validate_insert",
            "text_regions_g6_validate_update",
            "page_mask_artifacts_no_update",
            "page_mask_artifacts_no_delete",
            "page_mask_reviews_no_update",
            "page_mask_reviews_no_delete",
            "page_cloud_full_page_candidates_no_update",
            "page_cloud_full_page_candidates_no_delete",
            "page_cloud_full_page_reviews_no_update",
            "page_cloud_full_page_reviews_no_delete",
            "region_translation_candidates_no_update",
            "region_translation_candidates_no_delete",
            "region_translation_reviews_no_update",
            "region_translation_reviews_no_delete",
            "page_translation_reviews_no_update",
            "page_translation_reviews_no_delete",
            "page_typeset_candidates_no_update",
            "page_typeset_candidates_no_delete",
            "page_typeset_reviews_no_update",
            "page_typeset_reviews_no_delete",
        }
        missing_triggers = required_triggers - triggers
        if integrity != ("ok",) or foreign_key_errors:
            raise ProjectError("Controlled project database integrity preflight failed")
        if missing_tables or missing_columns or missing_triggers:
            raise ProjectError("Controlled project schema is incomplete or unprotected")
        project = connection.execute("SELECT id, schema_version FROM projects").fetchall()
        image = connection.execute(
            "SELECT project_id FROM images WHERE id = ?", (binding.image_id,)
        ).fetchone()
        generation = connection.execute(
            "SELECT image_id, run_id, state FROM page_generations WHERE id = ?",
            (binding.generation_id,),
        ).fetchone()
        image_generations = connection.execute(
            "SELECT id FROM page_generations WHERE image_id = ?", (binding.image_id,)
        ).fetchall()
        g0_rows = connection.execute(
            """SELECT evidence FROM page_lineage_events
               WHERE generation_id = ? AND gate = 'G0_identity'""",
            (binding.generation_id,),
        ).fetchall()
    if project != [(binding.project_id, 2)]:
        raise ProjectError("Controlled database must contain exactly the bound schema-v2 project")
    if image != (binding.project_id,):
        raise ProjectError("Controlled image does not belong to the bound project")
    if generation != (binding.image_id, binding.run_id, "active"):
        raise ProjectError("Controlled generation does not match the bound image and run")
    if image_generations != [(binding.generation_id,)]:
        raise ProjectError("Controlled image must belong only to the bound generation")
    if len(g0_rows) != 1:
        raise ProjectError("Controlled generation must have exactly one G0 identity event")
    try:
        g0_evidence = json.loads(g0_rows[0][0])
    except (TypeError, json.JSONDecodeError) as error:
        raise ProjectError("Controlled generation G0 identity is invalid") from error
    if g0_evidence.get("finalReviewItemId") != binding.final_review_item_id:
        raise ProjectError("Controlled generation is not bound to the final-review item")

    with _read_only_connection(review_database) as connection:
        review_integrity = connection.execute("PRAGMA integrity_check").fetchone()
        review_foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        review_schema = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        review_tables = {name for object_type, name in review_schema if object_type == "table"}
        review_triggers = {name for object_type, name in review_schema if object_type == "trigger"}
        required_review_columns = {
            "batches": {"id", "format_version", "item_count", "revision"},
            "items": {
                "id",
                "batch_id",
                "source_project_id",
                "source_image_id",
                "verdict",
                "revision",
                "artifact_revision",
                "evidence_json",
                "evidence_digest",
                "strict_evidence",
                "repair_handoff_json",
            },
            "revisions": {"id", "batch_id", "item_id", "item_revision"},
            "artifact_revisions": {
                "item_id",
                "artifact_revision",
                "evidence_json",
                "evidence_digest",
            },
        }
        missing_review_columns = {
            table: sorted(
                columns - {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            )
            for table, columns in required_review_columns.items()
            if table in review_tables
        }
        missing_review_columns = {
            table: columns for table, columns in missing_review_columns.items() if columns
        }
        required_review_triggers = {
            "artifact_revisions_no_update",
            "artifact_revisions_no_delete",
            "final_review_revisions_no_update",
            "final_review_revisions_no_delete",
        }
        if review_integrity != ("ok",) or review_foreign_key_errors:
            raise ProjectError("Controlled final-review database integrity preflight failed")
        if (
            set(required_review_columns) - review_tables
            or missing_review_columns
            or required_review_triggers - review_triggers
        ):
            raise ProjectError("Controlled final-review schema is incomplete or unprotected")
        batch = connection.execute("SELECT id FROM batches").fetchall()
        item = connection.execute(
            """SELECT batch_id, source_project_id, verdict, revision,
                      issue_codes, feedback
               FROM items WHERE id = ?""",
            (binding.final_review_item_id,),
        ).fetchone()
    if batch != [(binding.final_review_batch_id,)]:
        raise ProjectError("Controlled final-review database must contain exactly the bound batch")
    if item is None or item[:3] != (
        binding.final_review_batch_id,
        binding.project_id,
        "issues",
    ):
        raise ProjectError("Controlled final-review item does not match the bound page")
    try:
        feedback_checksum = hashlib.sha256(
            json.dumps(
                {
                    "issueCodes": sorted(json.loads(item[4])),
                    "feedback": item[5],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, json.JSONDecodeError) as error:
        raise ProjectError("Controlled final-review issue evidence is invalid") from error
    if (
        g0_evidence.get("finalReviewItemRevision") != item[3]
        or g0_evidence.get("feedbackChecksum") != feedback_checksum
    ):
        raise ProjectError("Controlled final-review issue evidence does not match G0")
    if set(binding.executable_job_kinds) - {"ocr", "mask", "typeset"}:
        raise ProjectError("Controlled recovery contains an unsupported executable job kind")
    return ControlledRecoveryPreflight(project_root, actual_digest, ())


def open_controlled_project(binding: ControlledRecoveryBinding) -> ProjectStore:
    preflight = preflight_controlled_recovery(binding)
    database_uri = f"sqlite:///{binding.project_database_path.resolve()}"
    engine: Engine = create_engine(
        database_uri,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    store = ProjectStore(preflight.project_root, engine)
    with store.session() as session:
        project = session.get(Project, binding.project_id)
        image = session.get(ImageAsset, binding.image_id)
        generation = session.get(PageGeneration, binding.generation_id)
        if (
            project is None
            or project.schema_version != 2
            or image is None
            or image.project_id != project.id
            or generation is None
            or generation.image_id != image.id
            or generation.run_id != binding.run_id
        ):
            raise ProjectError("Controlled recovery binding changed after preflight")
    return store
