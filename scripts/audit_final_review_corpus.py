"""Build a private, read-only Round 0 audit of a final-review corpus.

The source SQLite databases are opened with URI ``mode=ro`` and immutable
semantics.  The generated ledgers deliberately exclude OCR/translation text,
review feedback, absolute machine paths, job options, and revision payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from collections import Counter
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

ARTIFACT_SPECS = (
    ("source", "source", None),
    ("preprocessed", "generated/preprocessed", ".png"),
    ("mask", "generated/masks", ".png"),
    ("inpainted", "generated/inpainted", ".png"),
    ("typeset", "generated/typeset", ".png"),
    ("original-text", "original-text", ".json"),
    ("translated-text", "translated-text", ".json"),
)
VISUAL_STAGES = ("preprocess", "inpaint", "typeset")
GATE_NAMES = (
    "G0_identity",
    "G1_baselineUpscale",
    "G2_reconstruction",
    "G3_textPresence",
    "G4_regions",
    "G5_background",
    "G6_ocr",
    "G7_mask",
    "G8_cleanPlate",
    "G9_translation",
    "G10_typeset",
    "G11_finalReview",
)


class AuditError(RuntimeError):
    pass


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _relative_path(value: str) -> Path:
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise AuditError(f"unsafe relative path in database: {value!r}")
    return Path(*pure.parts)


def _ro_connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise AuditError(f"SQLite database is missing: {path.name}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _integrity(connection: sqlite3.Connection, label: str) -> str:
    rows = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if rows != ["ok"]:
        raise AuditError(f"{label} integrity_check failed: {rows!r}")
    return "ok"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _actor_anchor_evidence(
    source: sqlite3.Connection, final_review: sqlite3.Connection
) -> list[dict[str, Any]]:
    anchor_names = {
        "actor",
        "actor_id",
        "actor_kind",
        "client",
        "client_id",
        "task_id",
        "thread_id",
        "session_id",
    }
    findings: list[dict[str, str]] = []
    checked_tables: list[str] = []
    for database_name, connection in (
        ("source", source),
        ("final-review", final_review),
    ):
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            checked_tables.append(f"{database_name}:{table}")
            columns = connection.execute(
                f"PRAGMA table_info({json.dumps(table)})"
            ).fetchall()
            for column in columns:
                if str(column[1]).lower() in anchor_names:
                    findings.append(
                        {
                            "database": database_name,
                            "table": table,
                            "column": str(column[1]),
                        }
                    )
    result = (
        "no actor/client/task/thread/session anchor exists in source or final-review records"
        if not findings
        else "potential actor/session columns exist but no page/checksum/session correlation was proven"
    )
    return [
        {
            "kind": "database-schema-observation",
            "result": result,
            "checkedTables": checked_tables,
            "anchorColumns": findings,
        }
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _artifact_record(kind: str, path: Path, display_path: str) -> dict[str, Any]:
    exists = path.is_file()
    return {
        "kind": kind,
        "relativePath": display_path,
        "exists": exists,
        "checksum": _sha256(path) if exists else None,
        "missing_artifact": not exists,
    }


def _manifest_project_id(workspace: Path) -> str:
    path = workspace / "project/project.json"
    if not path.is_file():
        raise AuditError("source project manifest is missing")
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError("source project manifest is unreadable") from error
    project = payload.get("project")
    value = project.get("id") if isinstance(project, dict) else payload.get("id")
    if not isinstance(value, str) or not value:
        raise AuditError("source project manifest has no project id")
    return value


def _parse_sources(values: list[str], repo_root: Path) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        project_id, separator, raw = value.partition("=")
        if not separator or not project_id or not raw:
            raise AuditError("--source-project must be PROJECT_ID=WORKSPACE")
        workspace = Path(raw).expanduser()
        if not workspace.is_absolute():
            workspace = repo_root / workspace
        workspace = workspace.resolve()
        if project_id in sources:
            raise AuditError(f"duplicate source project id: {project_id}")
        if _manifest_project_id(workspace) != project_id:
            raise AuditError(f"source project manifest id mismatch: {project_id}")
        sources[project_id] = workspace
    if not sources:
        raise AuditError("at least one --source-project is required")
    return sources


def _safe_status_summary(status: dict[str, Any]) -> dict[str, Any]:
    providers = {}
    for key in (
        "preprocessingProvider",
        "detectorProvider",
        "ocrProvider",
        "translatorProvider",
        "inpaintingProvider",
        "typesettingProvider",
    ):
        value = status.get(key)
        if isinstance(value, str) and value:
            providers[key] = value
    stage_states = {
        key: status[key]
        for key in (
            "preprocess",
            "detection",
            "ocr",
            "translation",
            "inpaint",
            "typeset",
            "export",
            "reviewState",
        )
        if isinstance(status.get(key), str)
    }
    reviews = {}
    raw_reviews = status.get("stageReviews")
    if isinstance(raw_reviews, dict):
        for stage in VISUAL_STAGES:
            raw = raw_reviews.get(stage)
            if not isinstance(raw, dict):
                continue
            reviews[stage] = {
                key: raw.get(key)
                for key in (
                    "state",
                    "reviewedAt",
                    "resultRevision",
                    "artifactChecksum",
                    "maskChecksum",
                    "provenanceDigest",
                )
                if isinstance(raw.get(key), (str, int))
                and not isinstance(raw.get(key), bool)
            }
    return {
        "stageStates": stage_states,
        "providers": providers,
        "stageReviews": reviews,
    }


def _db_evidence(connection: sqlite3.Connection, image_id: str) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    if _table_exists(connection, "jobs") and _table_exists(connection, "job_items"):
        rows = connection.execute(
            """
            SELECT DISTINCT j.id, j.kind, j.status, j.progress, j.total, j.completed,
                            j.created_at, j.updated_at
            FROM jobs j JOIN job_items ji ON ji.job_id = j.id
            WHERE ji.image_id = ? OR ji.region_id IN
                  (SELECT id FROM text_regions WHERE image_id = ?)
            ORDER BY j.created_at, j.id
            """,
            (image_id, image_id),
        ).fetchall()
        jobs = [dict(row) for row in rows]
    revisions: list[dict[str, Any]] = []
    if _table_exists(connection, "revisions"):
        rows = connection.execute(
            """
            SELECT id, entity_type, entity_id, operation, project_revision, created_at
            FROM revisions WHERE entity_id = ? OR entity_id IN
                 (SELECT id FROM text_regions WHERE image_id = ?)
            ORDER BY created_at, id
            """,
            (image_id, image_id),
        ).fetchall()
        revisions = [dict(row) for row in rows]
    providers: list[dict[str, Any]] = []
    if _table_exists(connection, "text_regions"):
        rows = connection.execute(
            """
            SELECT COALESCE(ocr_provider, '') AS ocr_provider,
                   COALESCE(translation_provider, '') AS translation_provider,
                   COUNT(*) AS region_count
            FROM text_regions WHERE image_id = ?
            GROUP BY ocr_provider, translation_provider
            ORDER BY ocr_provider, translation_provider
            """,
            (image_id,),
        ).fetchall()
        providers = [dict(row) for row in rows]
    return {
        "jobs": jobs,
        "revisions": revisions,
        "regionProviderCounts": providers,
        "privacy": {
            "ocrTextIncluded": False,
            "translatedTextIncluded": False,
            "jobOptionsIncluded": False,
            "revisionPayloadsIncluded": False,
        },
    }


def _source_final(
    final_variant: str,
    status_summary: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> tuple[str | None, bool, list[str]]:
    stage = final_variant
    artifact_kind = "typeset" if stage == "typeset" else "preprocessed"
    review = status_summary["stageReviews"].get(stage)
    reasons: list[str] = []
    if status_summary["stageStates"].get(stage) != "done":
        reasons.append("source-stage-not-done")
    if not review or review.get("state") != "accepted":
        reasons.append("source-stage-not-currently-accepted")
    record = artifacts[artifact_kind]
    if not record["exists"]:
        reasons.append("source-final-artifact-missing")
    actual = record["checksum"]
    expected = review.get("artifactChecksum") if review else None
    if expected and actual != expected:
        reasons.append("source-final-review-checksum-mismatch")
    if (
        final_variant == "preprocess"
        and status_summary["stageStates"].get("reviewState") != "no-text-reviewed"
    ):
        reasons.append("source-no-text-review-not-current")
    stale = bool(reasons)
    return (actual if not stale else None), stale, reasons


def _gate_payloads(
    *,
    approved: bool,
    source_checksum: str,
    frozen_checksum: str,
) -> dict[str, dict[str, Any]]:
    identity_evidence = [
        {"kind": "immutable-source-checksum", "checksum": source_checksum},
        {"kind": "frozen-final-checksum", "checksum": frozen_checksum},
        {"kind": "source-identity-database-match"},
    ]
    gates: dict[str, dict[str, Any]] = {
        "G0_identity": {
            "state": "accepted",
            "inputChecksum": source_checksum,
            "outputChecksum": frozen_checksum,
            "evidence": identity_evidence,
        }
    }
    if approved:
        for name in GATE_NAMES[1:]:
            gates[name] = {
                "state": "not-applicable",
                "reason": "originally approved page is read-only and is not reprocessed",
            }
        return gates
    gates.update(
        {
            "G1_baselineUpscale": {
                "state": "pending",
                "inputChecksum": source_checksum,
                "outputChecksum": "",
                "decision": "",
            },
            "G2_reconstruction": {
                "state": "pending",
                "decision": "",
                "reason": "",
                "candidateChecksums": [],
            },
            "G3_textPresence": {
                "state": "pending",
                "decision": "uncertain",
                "evidence": [],
            },
            "G4_regions": {"state": "pending", "regionDecisions": []},
            "G5_background": {"state": "pending", "regionClasses": []},
            "G6_ocr": {"state": "pending", "trustedRegionIds": [], "qcFlags": []},
            "G7_mask": {
                "state": "pending",
                "maskChecksum": "",
                "coverageReviewed": False,
            },
            "G8_cleanPlate": {
                "state": "pending",
                "route": "",
                "outputChecksum": "",
                "outsideMaskChangeCount": None,
            },
            "G9_translation": {
                "state": "pending",
                "confirmedRegionIds": [],
                "qcFlags": [],
            },
            "G10_typeset": {
                "state": "pending",
                "routeCounts": {},
                "overflowCount": None,
                "outputChecksum": "",
            },
            "G11_finalReview": {
                "state": "pending",
                "refreshedRevision": None,
                "userVerdict": "pending",
                "exportPath": None,
            },
        }
    )
    return gates


def _prepare_output(path: Path) -> Path:
    if path.exists():
        raise AuditError("--output must be a brand-new directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.mkdir()
    for relative in ("audit", "reports", "config"):
        (temporary / relative).mkdir()
    return temporary


def audit(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).expanduser().resolve()
    review_root = Path(args.final_review_root).expanduser()
    if not review_root.is_absolute():
        review_root = repo_root / review_root
    review_root = review_root.resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = repo_root / output
    output = output.resolve()
    if output.exists():
        raise AuditError("--output must be a brand-new directory")
    if (
        output == review_root
        or output.is_relative_to(review_root)
        or review_root.is_relative_to(output)
    ):
        raise AuditError("--output must not overlap final-review storage")
    sources = _parse_sources(args.source_project, repo_root)

    manifest_path = review_root / "final-review/manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError("final-review manifest is missing or unreadable") from error
    if manifest.get("kind") != "manga-localizer-final-review":
        raise AuditError("unexpected final-review manifest kind")
    manifest_batch = manifest.get("batch")
    if not isinstance(manifest_batch, dict):
        raise AuditError("final-review manifest has no batch")
    input_snapshots: dict[str, Any] = {
        "finalReview": {
            "manifestSha256": _sha256(manifest_path),
            "databaseSha256": _sha256(
                review_root / "final-review/final-review.sqlite3"
            ),
        },
        "sourceProjects": {
            project_id: {
                "manifestSha256": _sha256(workspace / "project/project.json"),
                "databaseSha256": _sha256(workspace / "project/project.sqlite3"),
            }
            for project_id, workspace in sorted(sources.items())
        },
    }

    database_path = review_root / "final-review/final-review.sqlite3"
    project_connections: dict[str, sqlite3.Connection] = {}
    with _ro_connect(database_path) as final_db:
        final_integrity = _integrity(final_db, "final review")
        batches = final_db.execute("SELECT * FROM batches ORDER BY id").fetchall()
        if len(batches) != 1:
            raise AuditError("final-review database must contain exactly one batch")
        batch = batches[0]
        audit_timestamp = args.audit_timestamp or str(batch["updated_at"])
        items = final_db.execute("SELECT * FROM items ORDER BY position").fetchall()
        history_count = final_db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        if manifest_batch.get("id") != batch["id"]:
            raise AuditError("manifest and database batch ids differ")
        counts = {
            "manifest": manifest_batch.get("itemCount"),
            "batch": batch["item_count"],
            "items": len(items),
        }
        if len(set(counts.values())) != 1:
            raise AuditError(f"manifest/batch/item count mismatch: {counts}")
        expected_positions = list(range(1, len(items) + 1))
        if [row["position"] for row in items] != expected_positions:
            raise AuditError(
                "final-review positions are not a contiguous 1-based sequence"
            )

        project_integrity: dict[str, str] = {}
        provenance: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        verdict_counts: Counter[str] = Counter()
        variant_counts: Counter[str] = Counter()
        stale_count = 0
        try:
            for project_id, workspace in sources.items():
                connection = _ro_connect(workspace / "project/project.sqlite3")
                project_connections[project_id] = connection
                project_integrity[project_id] = _integrity(
                    connection, f"source project {project_id}"
                )

            for item in items:
                project_id = item["source_project_id"]
                if project_id not in sources:
                    raise AuditError(f"no --source-project mapping for {project_id}")
                workspace = sources[project_id]
                source_db = project_connections[project_id]
                image = source_db.execute(
                    "SELECT * FROM images WHERE id = ?", (item["source_image_id"],)
                ).fetchone()
                if image is None:
                    raise AuditError(
                        f"source image identity is missing for final item {item['id']}"
                    )
                if (
                    image["project_id"] != project_id
                    or image["relative_path"] != item["source_relative_path"]
                ):
                    raise AuditError(
                        f"source identity mismatch for final item {item['id']}"
                    )
                relative = _relative_path(item["source_relative_path"])
                artifact_rows: list[dict[str, Any]] = []
                for kind, directory, suffix in ARTIFACT_SPECS:
                    target_relative = (
                        relative if suffix is None else relative.with_suffix(suffix)
                    )
                    path = workspace / directory / target_relative
                    artifact_rows.append(
                        _artifact_record(
                            kind, path, f"{directory}/{target_relative.as_posix()}"
                        )
                    )
                frozen_relative = _relative_path(item["snapshot_path"])
                thumbnail_relative = _relative_path(item["thumbnail_path"])
                artifact_rows.append(
                    _artifact_record(
                        "frozen",
                        review_root / frozen_relative,
                        frozen_relative.as_posix(),
                    )
                )
                artifact_rows.append(
                    _artifact_record(
                        "thumbnail",
                        review_root / thumbnail_relative,
                        thumbnail_relative.as_posix(),
                    )
                )
                artifacts = {record["kind"]: record for record in artifact_rows}

                source_checksum = artifacts["source"]["checksum"]
                if source_checksum is None:
                    raise AuditError(
                        f"immutable source is missing for final item {item['id']}"
                    )
                if source_checksum != image["checksum"]:
                    raise AuditError(
                        f"immutable source checksum mismatch for final item {item['id']}"
                    )
                if artifacts["frozen"]["checksum"] != item["artifact_checksum"]:
                    raise AuditError(
                        f"frozen artifact checksum mismatch for final item {item['id']}"
                    )
                if artifacts["thumbnail"]["checksum"] != item["thumbnail_checksum"]:
                    raise AuditError(
                        f"thumbnail checksum mismatch for final item {item['id']}"
                    )

                status = (
                    _json(image["status"], {})
                    if isinstance(image["status"], str)
                    else (image["status"] or {})
                )
                if not isinstance(status, dict):
                    status = {}
                status_summary = _safe_status_summary(status)
                source_final_checksum, stale, stale_reasons = _source_final(
                    item["final_variant"],
                    status_summary,
                    artifacts,
                )
                stale_count += int(stale)
                verdict = item["verdict"]
                verdict_counts[verdict] += 1
                variant_counts[item["final_variant"]] += 1
                approved = verdict == "approved"
                issue_codes = _json(item["issue_codes"], [])
                if not isinstance(issue_codes, list):
                    raise AuditError(f"invalid issue_codes for final item {item['id']}")
                generation_id = (
                    None
                    if approved
                    else str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"manga-localizer:{args.run_id}:{project_id}:{item['source_image_id']}:{source_checksum}",
                        )
                    )
                )
                attribution_evidence = _actor_anchor_evidence(source_db, final_db)
                provenance.append(
                    {
                        "position": item["position"],
                        "finalItemId": item["id"],
                        "sourceProjectId": project_id,
                        "sourceImageId": item["source_image_id"],
                        "sourceRelativePath": item["source_relative_path"],
                        "immutableSourceChecksum": source_checksum,
                        "frozenFinalChecksum": item["artifact_checksum"],
                        "sourceFinalChecksum": source_final_checksum,
                        "sourceFinalStale": stale,
                        "sourceFinalStaleReasons": stale_reasons,
                        "artifacts": artifact_rows,
                        "stageEvidence": status_summary,
                        "databaseEvidence": _db_evidence(
                            source_db, item["source_image_id"]
                        ),
                        "finalReviewHistory": {
                            "revisionCount": final_db.execute(
                                "SELECT COUNT(*) FROM revisions WHERE item_id = ?",
                                (item["id"],),
                            ).fetchone()[0],
                            "operations": [
                                row[0]
                                for row in final_db.execute(
                                    "SELECT operation FROM revisions WHERE item_id = ? ORDER BY created_at, rowid",
                                    (item["id"],),
                                ).fetchall()
                            ],
                            "payloadsIncluded": False,
                        },
                        "actorAttribution": "unknown",
                        "attributionEvidence": attribution_evidence,
                    }
                )
                pages.append(
                    {
                        "position": item["position"],
                        "finalItemId": item["id"],
                        "sourceProjectId": project_id,
                        "sourceImageId": item["source_image_id"],
                        "sourceRelativePath": item["source_relative_path"],
                        "immutableSourceChecksum": source_checksum,
                        "frozenFinalChecksum": item["artifact_checksum"],
                        "sourceFinalChecksum": source_final_checksum,
                        "sourceFinalStale": stale,
                        "originalVerdict": verdict,
                        "originalIssueCodes": sorted(str(code) for code in issue_codes),
                        "originalFeedbackPresent": bool(item["feedback"]),
                        "actorAttribution": "unknown",
                        "attributionEvidence": attribution_evidence,
                        "reworkRequired": not approved,
                        "runId": None if approved else args.run_id,
                        "pageGenerationId": generation_id,
                        "restartFromSource": not approved,
                        "parameterSetId": args.parameter_set_id,
                        "gates": _gate_payloads(
                            approved=approved,
                            source_checksum=source_checksum,
                            frozen_checksum=item["artifact_checksum"],
                        ),
                        "firstFailedGate": None,
                        "firstFailedGateReason": "pending per-page visual diagnosis; issue codes are not root-cause evidence"
                        if not approved
                        else "not applicable to read-only approved page",
                        "derivedFailures": [],
                        "nextAction": "perform G1 visual baseline from immutable source"
                        if not approved
                        else "preserve read-only; include in approved-only export validation",
                        "updatedAt": audit_timestamp,
                    }
                )
        finally:
            for connection in project_connections.values():
                connection.close()

    input_snapshots["finalReview"]["integrityCheck"] = final_integrity
    for project_id, result in project_integrity.items():
        input_snapshots["sourceProjects"][project_id]["integrityCheck"] = result
    parameters = {
        "formatVersion": 1,
        "runId": args.run_id,
        "threadId": args.thread_id,
        "parameterSetId": args.parameter_set_id,
        "auditTimestamp": audit_timestamp,
        "sqliteOpenMode": "mode=ro&immutable=1",
        "finalReviewBatchId": batch["id"],
        "sourceProjectIds": sorted(sources),
        "inputSnapshots": input_snapshots,
        "privacyPolicy": {
            "includeOcrText": False,
            "includeTranslatedText": False,
            "includeFeedbackText": False,
            "includeAbsolutePaths": False,
            "includeSecretsOrJobOptions": False,
        },
    }
    summary = {
        "formatVersion": 1,
        "runId": args.run_id,
        "batchId": batch["id"],
        "batchRevision": batch["revision"],
        "historyCount": history_count,
        "itemCount": len(items),
        "verdictCounts": {
            key: verdict_counts.get(key, 0) for key in ("approved", "issues", "pending")
        },
        "finalVariantCounts": dict(sorted(variant_counts.items())),
        "sourceFinalStaleCount": stale_count,
        "integrityChecks": {
            "finalReview": final_integrity,
            "sourceProjects": project_integrity,
        },
        "inputSnapshots": input_snapshots,
        "actorAttributionCounts": {"unknown": len(items)},
        "missingArtifactCounts": dict(
            sorted(
                Counter(
                    record["kind"]
                    for page in provenance
                    for record in page["artifacts"]
                    if record["missing_artifact"]
                ).items()
            )
        ),
    }
    temporary = _prepare_output(output)
    try:
        _write_jsonl(temporary / "audit/provenance-ledger.jsonl", provenance)
        _write_jsonl(temporary / "audit/page-ledger.jsonl", pages)
        _write_json(temporary / "reports/summary.json", summary)
        _write_json(temporary / "config/resolved-parameters.json", parameters)
        (temporary / "config/resolved-parameters.md").write_text(
            "# Round 0 resolved parameters\n\n"
            f"- Run ID: `{args.run_id}`\n"
            f"- Thread ID: `{args.thread_id}`\n"
            f"- Parameter set ID: `{args.parameter_set_id}`\n"
            "- SQLite mode: `mode=ro&immutable=1`\n"
            "- Logical snapshots: SHA-256 and SQLite integrity results are recorded in the JSON parameter file.\n"
            "- Privacy: no OCR text, translated text, feedback text, absolute machine paths, secrets, or job option payloads.\n",
            encoding="utf-8",
        )
        missing_counts = summary["missingArtifactCounts"]
        missing_total = sum(missing_counts.values())
        (temporary / "reports/progress.md").write_text(
            "# Final corpus rebuild progress\n\n"
            f"Round 0 audit completed for {len(items)} pages. "
            f"Verdicts: {verdict_counts.get('approved', 0)} approved, "
            f"{verdict_counts.get('issues', 0)} issues, {verdict_counts.get('pending', 0)} pending.\n\n"
            f"Batch revision: {batch['revision']}; history rows: {history_count}; "
            f"G0 identity accepted: {len(pages)}.\n\n"
            f"Current source final is stale for {stale_count} page(s). "
            f"Missing artifacts: {missing_total} ({json.dumps(missing_counts, sort_keys=True)}). "
            "Issue codes were retained as user labels and were not converted into first-failed-gate diagnoses.\n\n"
            "Approved-only export: pending; this read-only audit does not perform exports.\n\n"
            "Next action: validate and run the approved-only export, then visually diagnose issue pages from their immutable source in page-ledger order.\n",
            encoding="utf-8",
        )
        if output.exists():
            raise AuditError("--output appeared during audit; refusing to overwrite it")
        temporary.replace(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--final-review-root", required=True)
    parser.add_argument(
        "--source-project", action="append", default=[], metavar="PROJECT_ID=WORKSPACE"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--parameter-set-id", required=True)
    parser.add_argument(
        "--audit-timestamp",
        default=None,
        help="explicit ledger timestamp; defaults to the stable batch updated_at value",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        audit(_parser().parse_args(argv))
    except (AuditError, OSError, sqlite3.Error) as error:
        print(f"audit failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
