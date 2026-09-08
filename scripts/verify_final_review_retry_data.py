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

from manga_localizer.services import final_reviews as reviews_module
from manga_localizer.services.projects import (
    ProjectNotFound,
    ProjectRegistry,
    ProjectStore,
)
from sqlalchemy import create_engine


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


class SelectedProjects(ProjectRegistry):
    def __init__(self, store, expected_id):
        self.selected_store = store
        self.expected_id = expected_id

    def get(self, project_id):
        if project_id != self.expected_id:
            raise ProjectNotFound("Project outside selected recovery scope")
        return self.selected_store


def deny_creation(*args, **kwargs):
    raise RuntimeError("Existing-head proof forbids new repair generations")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
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
        review_root, SelectedProjects(store, project_id), 384
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
