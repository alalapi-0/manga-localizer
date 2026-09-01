from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_final_review_corpus.py"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_project(
    root: Path,
    *,
    project_id: str,
    image_id: str,
    relative_path: str,
    source: bytes,
    final: bytes,
    current_review: bool,
    include_mask: bool,
) -> None:
    (root / "project").mkdir(parents=True)
    (root / "project/project.json").write_text(
        json.dumps({"project": {"id": project_id, "name": project_id}}), "utf-8"
    )
    relative = Path(relative_path)
    source_path = root / "source" / relative
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source)
    for directory, suffix, content in (
        ("generated/preprocessed", ".png", b"preprocessed-" + source),
        ("generated/inpainted", ".png", b"inpainted-" + source),
        ("generated/typeset", ".png", final),
        ("original-text", ".json", "原文绝不能泄露".encode()),
        ("translated-text", ".json", "译文绝不能泄露".encode()),
    ):
        target = root / directory / relative.with_suffix(suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if include_mask:
        target = root / "generated/masks" / relative.with_suffix(".png")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"mask-" + source)

    review = {
        "state": "accepted",
        "reviewedAt": "2026-08-25T00:00:00+00:00",
        "resultRevision": 7,
        "artifactChecksum": _sha256(final),
    }
    status = {
        "typeset": "done" if current_review else "pending",
        "preprocess": "done",
        "reviewState": "text-reviewed",
        "ocrProvider": "safe-ocr-provider",
        "translatorProvider": "safe-translator-provider",
        "stageReviews": {"typeset": review} if current_review else {},
    }
    database = root / "project/project.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE images (
              id TEXT PRIMARY KEY, project_id TEXT, relative_path TEXT,
              checksum TEXT, status TEXT
            );
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY, kind TEXT, status TEXT, progress REAL,
              total INTEGER, completed INTEGER, options TEXT, error TEXT,
              created_at TEXT, updated_at TEXT
            );
            CREATE TABLE job_items (job_id TEXT, image_id TEXT, region_id TEXT);
            CREATE TABLE revisions (
              id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT,
              operation TEXT, project_revision INTEGER, before TEXT, after TEXT,
              created_at TEXT
            );
            CREATE TABLE text_regions (
              id TEXT PRIMARY KEY, image_id TEXT, source_text TEXT, translation_text TEXT,
              ocr_provider TEXT, translation_provider TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO images VALUES (?, ?, ?, ?, ?)",
            (image_id, project_id, relative_path, _sha256(source), json.dumps(status)),
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"job-{image_id}",
                "ocr",
                "completed",
                1.0,
                1,
                1,
                json.dumps({"api_key": "TOP-SECRET", "prompt": "原文绝不能泄露"}),
                None,
                "2026-08-25T00:00:00Z",
                "2026-08-25T00:01:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO job_items VALUES (?, ?, ?)", (f"job-{image_id}", image_id, None)
        )
        connection.execute(
            "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"revision-{image_id}",
                "image",
                image_id,
                "update",
                2,
                json.dumps({"sourceText": "原文绝不能泄露"}),
                json.dumps({"translationText": "译文绝不能泄露"}),
                "2026-08-25T00:02:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO text_regions VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"region-{image_id}",
                image_id,
                "原文绝不能泄露",
                "译文绝不能泄露",
                "safe-ocr-provider",
                "safe-translator-provider",
            ),
        )


def _write_review(
    root: Path,
    projects: list[tuple[str, str, str, bytes, str]],
) -> None:
    (root / "final-review").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "thumbnails").mkdir()
    batch_id = "batch-199"
    database = root / "final-review/final-review.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE batches (
              id TEXT PRIMARY KEY, name TEXT, root_path TEXT, item_count INTEGER,
              revision INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE items (
              id TEXT PRIMARY KEY, batch_id TEXT, position INTEGER,
              source_project_id TEXT, source_image_id TEXT, source_project_name TEXT,
              source_relative_path TEXT, final_variant TEXT, artifact_checksum TEXT,
              thumbnail_checksum TEXT, snapshot_path TEXT, thumbnail_path TEXT,
              verdict TEXT, issue_codes TEXT, feedback TEXT, reviewed_at TEXT,
              revision INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE revisions (
              id TEXT PRIMARY KEY, batch_id TEXT, item_id TEXT, operation TEXT,
              before_json TEXT, after_json TEXT, item_revision INTEGER, created_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch_id, "synthetic", "/private/absolute/path", 2, 204, "now", "now"),
        )
        for position, (project_id, image_id, relative, frozen, verdict) in enumerate(projects, 1):
            item_id = f"item-{position}"
            thumbnail = b"thumbnail-" + frozen
            snapshot_path = f"images/{item_id}.png"
            thumbnail_path = f"thumbnails/{item_id}.jpg"
            (root / snapshot_path).write_bytes(frozen)
            (root / thumbnail_path).write_bytes(thumbnail)
            issue_codes = [] if verdict == "approved" else ["mask", "translation"]
            feedback = "private reviewer feedback must not leak" if verdict == "issues" else ""
            connection.execute(
                """INSERT INTO items
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    batch_id,
                    position,
                    project_id,
                    image_id,
                    project_id,
                    relative,
                    "typeset",
                    _sha256(frozen),
                    _sha256(thumbnail),
                    snapshot_path,
                    thumbnail_path,
                    verdict,
                    json.dumps(issue_codes),
                    feedback,
                    "now",
                    2,
                    "now",
                    "now",
                ),
            )
            connection.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"fr-revision-{position}",
                    batch_id,
                    item_id,
                    "review",
                    json.dumps({"feedback": "private reviewer feedback must not leak"}),
                    json.dumps({"feedback": "private reviewer feedback must not leak"}),
                    2,
                    "now",
                ),
            )
    (root / "final-review/manifest.json").write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "kind": "manga-localizer-final-review",
                "batch": {"id": batch_id, "name": "synthetic", "itemCount": 2, "createdAt": "now"},
            }
        ),
        "utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, list[tuple[str, Path]], Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    project_a = repo / "workspace-a"
    project_b = repo / "workspace-b"
    frozen_a = b"approved-final"
    frozen_b = b"stale-issue-final"
    _write_project(
        project_a,
        project_id="project-a",
        image_id="image-a",
        relative_path="chapter/a.jpg",
        source=b"source-a",
        final=frozen_a,
        current_review=True,
        include_mask=True,
    )
    _write_project(
        project_b,
        project_id="project-b",
        image_id="image-b",
        relative_path="chapter/b.jpg",
        source=b"source-b",
        final=frozen_b,
        current_review=False,
        include_mask=False,
    )
    review = repo / "review"
    _write_review(
        review,
        [
            ("project-a", "image-a", "chapter/a.jpg", frozen_a, "approved"),
            ("project-b", "image-b", "chapter/b.jpg", frozen_b, "issues"),
        ],
    )
    return repo, [("project-a", project_a), ("project-b", project_b)], review


def _run(repo: Path, sources: list[tuple[str, Path]], review: Path, output: Path):
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(repo),
        "--final-review-root",
        str(review),
    ]
    for project_id, workspace in sources:
        command.extend(["--source-project", f"{project_id}={workspace}"])
    command.extend(
        [
            "--output",
            str(output),
            "--run-id",
            "round0-test",
            "--thread-id",
            "thread-test",
            "--parameter-set-id",
            "parameters-test",
            "--audit-timestamp",
            "2026-08-25T00:00:00+00:00",
        ]
    )
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_audit_builds_private_deterministic_round_zero_ledgers(tmp_path: Path) -> None:
    repo, sources, review = _fixture(tmp_path)
    first_output = repo / "audit-one"
    second_output = repo / "audit-two"
    first = _run(repo, sources, review, first_output)
    second = _run(repo, sources, review, second_output)
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)

    provenance = _read_jsonl(first_output / "audit/provenance-ledger.jsonl")
    pages = _read_jsonl(first_output / "audit/page-ledger.jsonl")
    pages_again = _read_jsonl(second_output / "audit/page-ledger.jsonl")
    assert [page["position"] for page in pages] == [1, 2]
    assert pages[0]["reworkRequired"] is False
    assert pages[0]["pageGenerationId"] is None
    assert pages[0]["gates"]["G1_baselineUpscale"]["state"] == "not-applicable"
    assert pages[1]["reworkRequired"] is True
    assert pages[1]["restartFromSource"] is True
    assert pages[1]["pageGenerationId"] == pages_again[1]["pageGenerationId"]
    assert pages[1]["firstFailedGate"] is None
    assert pages[1]["gates"]["G0_identity"]["state"] == "accepted"
    assert provenance[0]["sourceFinalStale"] is False
    assert provenance[1]["sourceFinalStale"] is True
    assert provenance[1]["actorAttribution"] == "unknown"
    missing = {row["kind"] for row in provenance[1]["artifacts"] if row["missing_artifact"]}
    assert missing == {"mask"}

    summary = json.loads((first_output / "reports/summary.json").read_text("utf-8"))
    assert summary["verdictCounts"] == {"approved": 1, "issues": 1, "pending": 0}
    assert summary["sourceFinalStaleCount"] == 1
    assert summary["historyCount"] == 2
    assert summary["missingArtifactCounts"] == {"mask": 1}
    assert summary["integrityChecks"]["finalReview"] == "ok"
    assert set(summary["inputSnapshots"]["sourceProjects"]) == {"project-a", "project-b"}
    assert all(
        len(snapshot["databaseSha256"]) == 64
        and len(snapshot["manifestSha256"]) == 64
        and snapshot["integrityCheck"] == "ok"
        for snapshot in summary["inputSnapshots"]["sourceProjects"].values()
    )
    progress = (first_output / "reports/progress.md").read_text("utf-8")
    assert "1 approved, 1 issues, 0 pending" in progress
    assert "Batch revision: 204; history rows: 2; G0 identity accepted: 2" in progress
    assert "Approved-only export: pending" in progress

    complete_output = "\n".join(
        path.read_text("utf-8") for path in first_output.rglob("*") if path.is_file()
    )
    assert "原文绝不能泄露" not in complete_output
    assert "译文绝不能泄露" not in complete_output
    assert "TOP-SECRET" not in complete_output
    assert "private reviewer feedback must not leak" not in complete_output
    assert str(tmp_path) not in complete_output


def test_audit_fails_closed_on_checksum_mismatch_and_existing_output(tmp_path: Path) -> None:
    repo, sources, review = _fixture(tmp_path)
    existing = repo / "already-exists"
    existing.mkdir()
    refused = _run(repo, sources, review, existing)
    assert refused.returncode == 2
    assert "brand-new directory" in refused.stderr
    assert list(existing.iterdir()) == []

    frozen = review / "images/item-2.png"
    frozen.write_bytes(frozen.read_bytes() + b"corrupt")
    failed_output = repo / "failed-audit"
    failed = _run(repo, sources, review, failed_output)
    assert failed.returncode == 2
    assert "frozen artifact checksum mismatch" in failed.stderr
    assert not failed_output.exists()
    assert not list(repo.glob(f".{failed_output.name}.*.tmp"))
