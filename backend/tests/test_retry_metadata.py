"""Standalone synthetic metadata fixtures; no app or provider required."""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify_final_review_retry_data.py"
spec = importlib.util.spec_from_file_location("retry_metadata", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RetryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.review = self.root / "review"
        (self.review / "final-review").mkdir(parents=True)
        self.db = self.review / "final-review/final-review.sqlite3"
        self.project = self.root / "project.sqlite3"
        self.sql(
            self.db,
            """CREATE TABLE batches(id,item_count,revision,format_version);
            INSERT INTO batches VALUES('batch',3,6,2);
            CREATE TABLE items(id,batch_id,source_project_id,source_image_id,
                               revision,artifact_revision,verdict,strict_evidence);
            INSERT INTO items VALUES('a','batch','source','image-a',4,2,'issues',1),
                ('b','batch','source','image-b',2,1,'issues',1),
                ('c','batch','source','image-c',2,1,'approved',1);
            CREATE TABLE revisions(id,batch_id,item_id,operation,before_json,
                                   after_json,item_revision,created_at);
            INSERT INTO revisions VALUES
                ('ra-create','batch','a','create','{}','{}',1,'2026-01-01T00:00:00Z'),
                ('ra-review-1','batch','a','review','{}','{}',2,'2026-01-01T00:01:00Z'),
                ('ra-refresh','batch','a','refresh','{}','{}',3,'2026-01-01T00:02:00Z'),
                ('ra-review-2','batch','a','review','{}','{}',4,'2026-01-01T00:03:00Z'),
                ('rb-create','batch','b','create','{}','{}',1,'2026-01-01T00:00:00Z'),
                ('rb-review','batch','b','review','{}','{}',2,'2026-01-01T00:01:00Z'),
                ('rc-create','batch','c','create','{}','{}',1,'2026-01-01T00:00:00Z'),
                ('rc-review','batch','c','review','{}','{}',2,'2026-01-01T00:01:00Z');
            CREATE TABLE artifact_revisions(item_id,artifact_revision,
                                            PRIMARY KEY(item_id,artifact_revision));
            INSERT INTO artifact_revisions VALUES
                ('a',1),('a',2),('b',1),('c',1);""",
        )
        self.sql(
            self.project,
            """CREATE TABLE projects(id);
            INSERT INTO projects VALUES('source');
            CREATE TABLE page_generations(id,project_id,source_project_id,source_image_id,
                                         state,next_sequence,parameter_set_id,parameter_set_hash);
            CREATE TABLE page_lineage_events(generation_id,gate,evidence);""",
        )
        c = sqlite3.connect(self.project)
        try:
            c.execute(
                "INSERT INTO page_generations VALUES(?,?,?,?,?,?,?,?)",
                ("gen-a", "source", "source", "image-a", "active", 3, "recipe", "a" * 64),
            )
            c.execute(
                "INSERT INTO page_lineage_events VALUES(?,?,?)",
                (
                    "gen-a",
                    "G0_identity",
                    json.dumps({"finalReviewItemId": "a", "finalReviewItemRevision": 2}),
                ),
            )
            c.commit()
        finally:
            c.close()

    def tearDown(self):
        self.temp.cleanup()

    def sql(self, db, text):
        c = sqlite3.connect(db)
        try:
            c.executescript(text)
            c.commit()
        finally:
            c.close()

    def collect(self, **kwargs):
        return module.metadata_summary(self.root, self.review, {"source": self.project}, **kwargs)

    def snapshot(self, **kwargs):
        return module.review_snapshot_summary(self.root, self.review, **kwargs)

    def test_bounded_json_counts_the_output_newline(self):
        value = {"value": "boundary"}
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.assertRaises(ValueError):
            module.bounded_json(value, max_output_bytes=len(encoded.encode()))
        self.assertEqual(
            module.bounded_json(value, max_output_bytes=len(encoded.encode()) + 1),
            encoded,
        )

    def test_full_denominator_paginated_and_read_only(self):
        before = [p.read_bytes() for p in (self.db, self.project)]
        result = self.collect(limit=1)
        self.assertEqual(result["total_items"], 3)
        self.assertEqual(result["issues_total"], 2)
        self.assertEqual(result["classified_total"], 2)
        self.assertEqual(
            result["classifications"],
            {"existing_head_requires_gate_review": 1, "no_recorded_repair": 1},
        )
        self.assertEqual(result["items"][0]["generation_id"], "gen-a")
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(
            result["metadata_digest"], self.collect(offset=1, limit=1)["metadata_digest"]
        )
        self.assertEqual(before, [p.read_bytes() for p in (self.db, self.project)])

    def test_review_snapshot_separates_history_stage_counts_and_candidate(self):
        before = self.db.read_bytes()
        result = self.snapshot(limit=1)
        item = result["items"][0]
        self.assertEqual(result["mode"], "read_only_review_snapshot")
        self.assertEqual(result["schema_version"], "manga-review-snapshot.v1")
        self.assertEqual(
            item["review_history_refs"], ["ra-create", "ra-review-1", "ra-refresh", "ra-review-2"]
        )
        self.assertEqual(
            (item["review_stage"], item["review_state"]), ("first_review", "pending_rework")
        )
        self.assertEqual(item["submission_attempt"], 2)
        self.assertEqual(item["rework_count"], 1)
        self.assertEqual(item["candidate_revision"], 2)
        self.assertEqual(item["gaps"], [])
        self.assertEqual(
            result["snapshot_digest"], self.snapshot(offset=1, limit=1)["snapshot_digest"]
        )
        approved = self.snapshot(offset=2)["items"][0]
        self.assertIsNone(approved["review_stage"])
        self.assertEqual(approved["review_state"], "awaiting_acceptance")
        self.assertEqual(before, self.db.read_bytes())

    def test_review_snapshot_keeps_migrated_legacy_refs_and_known_counts(self):
        self.sql(
            self.db,
            """UPDATE batches SET format_version=1;
            DELETE FROM revisions
            WHERE item_id='a' AND id IN ('ra-create','ra-review-1');""",
        )
        item = self.snapshot(limit=1)["items"][0]
        self.assertEqual(item["review_history_refs"], ["ra-refresh", "ra-review-2"])
        self.assertEqual(
            (item["review_stage"], item["review_state"]),
            ("first_review", "pending_rework"),
        )
        self.assertEqual(item["submission_attempt"], 2)
        self.assertEqual(item["rework_count"], 1)
        self.assertEqual(item["candidate_revision"], 2)
        self.assertEqual(item["gaps"], ["review_history_incomplete"])

    def test_review_snapshot_detects_an_intermediate_history_revision_gap(self):
        self.sql(self.db, "DELETE FROM revisions WHERE id='ra-review-1'")
        item = self.snapshot(limit=1)["items"][0]
        self.assertEqual(item["review_history_refs"], ["ra-create", "ra-refresh", "ra-review-2"])
        self.assertIn("review_history_incomplete", item["gaps"])

    def test_review_snapshot_preserves_explicit_legacy_and_history_gaps(self):
        self.sql(
            self.db,
            """UPDATE items SET strict_evidence=0 WHERE id='c';
            DELETE FROM artifact_revisions WHERE item_id='a' AND artifact_revision=2;""",
        )
        result = self.snapshot()
        a, _, c = result["items"]
        self.assertEqual(a["candidate_revision"], 2)
        self.assertIsNone(a["submission_attempt"])
        self.assertIn("submission_attempt_unknown", a["gaps"])
        self.assertIsNone(c["review_stage"])
        self.assertIsNone(c["review_state"])
        self.assertIn("review_stage_legacy_unknown", c["gaps"])
        self.assertEqual(result["gap_counts"]["submission_attempt_unknown"], 1)

    def test_duplicate_g0_is_not_valid_head(self):
        self.sql(self.project, "INSERT INTO page_lineage_events SELECT * FROM page_lineage_events")
        self.assertEqual(
            self.collect()["items"][0]["classification"], "ambiguous_or_invalid_provenance"
        )

    def test_failed_source_keeps_coverage(self):
        self.sql(self.project, "UPDATE projects SET id='wrong'")
        result = self.collect()
        self.assertEqual(result["classifications"], {"source_unavailable": 2})
        self.assertEqual(result["total_items"], 3)

    def test_symlink_and_offset_rejected(self):
        link = self.root / "link.sqlite3"
        link.symlink_to(self.project)
        with self.assertRaises(ValueError):
            module.metadata_summary(self.root, self.review, {"source": link})
        with self.assertRaises(ValueError):
            self.collect(offset=-1)

    def test_invalid_batch_does_not_claim_coverage(self):
        self.sql(self.db, "UPDATE items SET batch_id='wrong' WHERE id='a'")
        with self.assertRaises(ValueError):
            self.collect()


if __name__ == "__main__":
    unittest.main()
