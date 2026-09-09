"""Verify existing repair heads with production validation and read-only stores.

This intentionally avoids catalog startup/migration and forbids creating any G0.
It verifies the selected heads, never claims whole-batch or generated-image quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def ro_connect(path):
    connection = sqlite3.connect(
        Path(path).resolve(strict=True).as_uri() + "?mode=ro",
        uri=True,
        timeout=10,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA query_only=ON")
    return connection


def canonical(path):
    path = path.absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("Selected recovery paths must not contain symlinks")
    return path.resolve(strict=True)


def selected_projects(store, expected_id):
    from manga_localizer.services.projects import ProjectNotFound, ProjectRegistry

    class SelectedProjects(ProjectRegistry):
        def __init__(self):
            # Deliberately avoid registry discovery/startup in legacy mode.
            pass

        def get(self, project_id):
            if project_id != expected_id:
                raise ProjectNotFound("Project outside selected recovery scope")
            return store

    return SelectedProjects()


def deny_creation(*args, **kwargs):
    raise RuntimeError("Existing-head proof forbids new repair generations")


def metadata_summary(data_root, review_root, project_databases, *, offset=0, limit=5):
    """Classify recorded repair metadata, never validate or activate a workflow."""
    import re
    from contextlib import ExitStack, closing

    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 10
    ):
        raise ValueError("offset must be nonnegative; limit must be 1..10")
    root = canonical(Path(data_root))

    def selected(path):
        result = canonical(Path(path))
        if not result.is_relative_to(root):
            raise ValueError("Database is outside the explicitly selected data root")
        return result

    def read(c, query, args=()):
        rows = c.execute(query, args).fetchmany(10001)
        if len(rows) > 10000:
            raise ValueError("Metadata row limit exceeded")
        return rows

    def connect(stack, path):
        c = stack.enter_context(closing(ro_connect(path)))
        calls = 0

        def progress():
            nonlocal calls
            calls += 1
            return int(calls > 5000)

        c.set_progress_handler(progress, 1000)
        c.execute("BEGIN")
        return c

    def opaque(value):
        return isinstance(value, str) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value
        )

    # Resolve every selected path before opening any source. Symlinks/out-of-root
    # inputs are configuration failures, not a reason to probe another location.
    review_db = selected(Path(review_root) / "final-review/final-review.sqlite3")
    paths = {key: selected(path) for key, path in project_databases.items()}
    with ExitStack() as stack:
        review = connect(stack, review_db)
        batches = read(
            review, "SELECT id,item_count,revision,format_version FROM batches"
        )
        items = read(
            review,
            "SELECT id,batch_id,source_project_id,source_image_id,revision,artifact_revision,verdict FROM items ORDER BY id",
        )
        if len(batches) != 1 or batches[0]["item_count"] != len(items):
            raise ValueError("Review batch identity/count mismatch")
        batch = batches[0]
        if batch["format_version"] not in (1, 2) or len(
            {r["id"] for r in items}
        ) != len(items):
            raise ValueError("Unsupported batch format or duplicate item identity")
        if any(r["batch_id"] != batch["id"] or not opaque(r["id"]) for r in items):
            raise ValueError("Invalid review item identity")
        issues = [r for r in items if r["verdict"] == "issues"]
        sources, unavailable = {}, []
        for source_id in sorted({r["source_project_id"] for r in issues}):
            try:
                c = connect(stack, paths[source_id])
                if [r[0] for r in read(c, "SELECT id FROM projects")] != [source_id]:
                    raise ValueError("Project identity mismatch")
                generations = {
                    r["id"]: dict(r)
                    for r in read(
                        c,
                        "SELECT id,project_id,source_project_id,source_image_id,state,next_sequence,parameter_set_id,parameter_set_hash FROM page_generations",
                    )
                }
                events = read(
                    c,
                    "SELECT generation_id,evidence FROM page_lineage_events WHERE gate='G0_identity'",
                )
                indexed = {}
                for event in events:
                    if len(event["evidence"]) > 65536:
                        raise ValueError("G0 metadata exceeds limit")
                    evidence = json.loads(event["evidence"])
                    if not isinstance(evidence, dict):
                        raise TypeError("Invalid G0 metadata")
                    item_id = evidence.get("finalReviewItemId")
                    if item_id is not None:
                        indexed.setdefault(item_id, []).append(
                            (generations.get(event["generation_id"]), evidence)
                        )
                sources[source_id] = indexed
            except (KeyError, OSError, sqlite3.Error, ValueError, TypeError):
                unavailable.append(source_id)
        details, counts = [], {}
        for item in issues:
            source_id = item["source_project_id"]
            result = {
                "item_id": item["id"],
                "source_project_id": source_id,
                "item_revision": item["revision"],
                "artifact_revision": item["artifact_revision"],
            }
            candidates = sources.get(source_id, {}).get(item["id"], [])
            classification = "no_recorded_repair"
            valid, active = True, []
            for generation, evidence in candidates:
                if (
                    generation is None
                    or generation["project_id"] != source_id
                    or generation["source_project_id"] != source_id
                    or generation["source_image_id"] != item["source_image_id"]
                    or generation["state"] not in ("active", "superseded")
                    or type(generation["next_sequence"]) is not int
                    or generation["next_sequence"] < 2
                    or not opaque(generation["id"])
                    or not opaque(generation["parameter_set_id"])
                    or not isinstance(generation["parameter_set_hash"], str)
                    or not re.fullmatch(
                        r"[a-f0-9]{64}", generation["parameter_set_hash"]
                    )
                    or type(evidence.get("finalReviewItemRevision")) is not int
                    or not 1 <= evidence["finalReviewItemRevision"] <= item["revision"]
                ):
                    valid = False
                    continue
                if generation["state"] == "active":
                    active.append((generation, evidence))
            if source_id in unavailable:
                classification = "source_unavailable"
            elif candidates:
                classification = "ambiguous_or_invalid_provenance"
                if valid and len(active) == 1:
                    classification = "existing_head_requires_gate_review"
                    generation, evidence = active[0]
                    result.update(
                        generation_id=generation["id"],
                        head_state=generation["state"],
                        next_sequence=generation["next_sequence"],
                        origin_item_revision=evidence["finalReviewItemRevision"],
                        parameter_set_id=generation["parameter_set_id"],
                        parameter_set_hash=generation["parameter_set_hash"],
                    )
            result["classification"] = classification
            counts[classification] = counts.get(classification, 0) + 1
            details.append(result)
        verdicts = {}
        for item in items:
            verdict = (
                item["verdict"]
                if item["verdict"] in ("approved", "issues", "pending")
                else "unknown"
            )
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        return {
            "mode": "read_only_metadata",
            "batch_id": batch["id"],
            "batch_revision": batch["revision"],
            "format_version": batch["format_version"],
            "total_items": len(items),
            "verdicts": verdicts,
            "issues_total": len(issues),
            "classified_total": sum(counts.values()),
            "classifications": counts,
            "source_unavailable": unavailable,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < len(details) else None,
            "items": details[offset : offset + limit],
            "metadata_digest": digest(details),
            "limits": "Metadata heads are not service-validated. All categories require page-specific restrictions and gate review before reopening; no enqueue, claim, generation, approval or artifact refresh occurred.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--project-database", action="append", default=[])
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.summary:
        if args.data_root is None:
            parser.error("--summary requires --data-root")
        selected = {}
        for entry in args.project_database:
            identity, separator, path = entry.partition("=")
            if not separator or not identity or identity in selected:
                parser.error("project database must be unique SOURCE_ID=PATH")
            selected[identity] = Path(path)
        result = metadata_summary(
            args.data_root,
            args.review_root,
            selected,
            offset=args.offset,
            limit=args.limit,
        )
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 8192:
            parser.error("summary exceeds 8192 bytes; reduce --limit")
        print(encoded)
        return
    if args.project_root is None or args.receipt is None:
        parser.error("legacy verification requires --project-root and --receipt")
    from manga_localizer.services import final_reviews as reviews_module
    from manga_localizer.services.projects import ProjectStore
    from sqlalchemy import create_engine

    project_root, review_root = (
        canonical(args.project_root),
        canonical(args.review_root),
    )
    project_manifest = canonical(project_root / "project/project.json")
    review_manifest = canonical(review_root / "final-review/manifest.json")
    project_db = canonical(project_root / "project/project.sqlite3")
    review_db = canonical(review_root / "final-review/final-review.sqlite3")
    for name in ("images", "thumbnails"):
        assert canonical(review_root / name).is_dir()
    manifest = json.loads(review_manifest.read_text())
    project_id = json.loads(project_manifest.read_text())["project"]["id"]
    assert manifest["kind"] == "manga-localizer-final-review" and manifest[
        "formatVersion"
    ] in (1, 2)
    receipt = json.loads(args.receipt.read_text())
    expected = receipt["observed_handoffs"]
    assert expected and len({c["item_id"] for c in expected}) == len(expected)
    engine = create_engine("sqlite://", creator=lambda: ro_connect(project_db))
    store = ProjectStore(project_root, engine)
    assert store.project().id == project_id
    reviews = reviews_module.FinalReviewStore(
        review_root, selected_projects(store, project_id), 384
    )
    f, p = ro_connect(review_db), ro_connect(project_db)

    def snapshot():
        return {
            "reviews": digest(
                [
                    tuple(r)
                    for r in f.execute(
                        "SELECT id,revision,verdict,artifact_revision,artifact_checksum,evidence_digest,strict_evidence FROM items ORDER BY id"
                    )
                ]
            ),
            "generations": digest(
                [
                    tuple(r)
                    for r in p.execute(
                        "SELECT id,run_id,next_sequence,state FROM page_generations ORDER BY id"
                    )
                ]
            ),
            "images": digest(
                [
                    tuple(r)
                    for r in p.execute("SELECT id,revision FROM images ORDER BY id")
                ]
            ),
            "lineage_event_count": p.execute(
                "SELECT count(*) FROM page_lineage_events"
            ).fetchone()[0],
            "counts": dict(
                f.execute("SELECT verdict,count(*) FROM items GROUP BY verdict")
            ),
        }

    before = snapshot()
    assert before == receipt["before"], (
        "Live facts changed; refresh the observed scope first"
    )
    batch = f.execute(
        "SELECT * FROM batches WHERE id=?", (manifest["batch"]["id"],)
    ).fetchone()
    assert batch and f.execute("SELECT count(*) FROM batches").fetchone()[0] == 1
    assert (
        batch["item_count"]
        == manifest["batch"]["itemCount"]
        == f.execute("SELECT count(*) FROM items").fetchone()[0]
    )
    assert batch["format_version"] == manifest["formatVersion"]
    cases = []
    with (
        patch.object(reviews_module, "_connect", ro_connect),
        patch.object(
            reviews_module, "create_final_review_repair_generation", deny_creation
        ),
    ):
        for case in expected:
            h = case["handoff"]
            row = f.execute(
                "SELECT * FROM items WHERE id=?", (case["item_id"],)
            ).fetchone()
            assert row["batch_id"] == batch["id"] and row["verdict"] == "issues"
            assert (
                row["revision"] == h["finalReviewItemRevision"]
                and batch["revision"] == h["batchRevision"]
            )
            assert row["source_project_id"] == project_id == h["repairProjectId"]
            assert row["artifact_revision"] == h["artifactRevision"]
            if manifest["formatVersion"] == 2:
                assert row["strict_evidence"] == 1
                revision = f.execute(
                    "SELECT * FROM artifact_revisions WHERE item_id=? AND artifact_revision=?",
                    (row["id"], row["artifact_revision"]),
                ).fetchone()
                evidence = json.loads(row["evidence_json"])
                assert revision and evidence == json.loads(revision["evidence_json"])
                assert (
                    digest(evidence)
                    == row["evidence_digest"]
                    == revision["evidence_digest"]
                )
                assert all(
                    v["artifactRevision"] == row["artifact_revision"]
                    for v in evidence.values()
                )
            results = []
            for _ in range(2):
                result = reviews.repair(
                    row["id"],
                    expected_revision=row["revision"],
                    expected_batch_revision=batch["revision"],
                    actor={
                        "actorKind": "codex",
                        "actorId": "root",
                        "taskId": receipt["task_id"],
                        "operationSource": "script",
                    },
                    parameter_set_id=h["parameterSetId"],
                    parameter_set_hash=h["parameterSetHash"],
                    retry_from_generation_id=None,
                )
                assert result == h and result["idempotent"] is True
                results.append(result)
            cases.append(
                {
                    "item_id": row["id"],
                    "handoff": results[0],
                    "duplicate_reopen_identical": results[0] == results[1],
                }
            )
    after = snapshot()
    assert after == before
    f.close()
    p.close()
    engine.dispose()
    receipt.update(
        cases=cases,
        status="passed",
        after=after,
        unchanged=True,
        observed_at=datetime.now(timezone.utc).isoformat(),
        backend_code={
            "repository": "alalapi-0/manga-localizer",
            "relative_path": str(
                Path(reviews_module.__file__)
                .resolve()
                .relative_to(Path(__file__).resolve().parents[1])
            ),
            "role": "verified governance worktree production service",
        },
        scope="Production repair service for selected live-data heads; read-only SQL and deny-creation guard; no full catalog/open migration, worker, or network listener",
        quality_status="Existing repair handoffs reopened; no new generated images or approvals. Original quality blockers remain.",
    )
    receipt["backend_service_sha256"] = hashlib.sha256(
        Path(reviews_module.__file__).read_bytes()
    ).hexdigest()
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verified_heads": len(cases),
                "duplicate_reopens": len(cases),
                "unchanged": True,
            }
        )
    )


if __name__ == "__main__":
    main()
