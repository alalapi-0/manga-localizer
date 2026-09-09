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
            INSERT INTO batches VALUES('batch',3,5,1);
            CREATE TABLE items(id,batch_id,source_project_id,source_image_id,
                               revision,artifact_revision,verdict);
            INSERT INTO items VALUES('a','batch','source','image-a',2,1,'issues'),
                ('b','batch','source','image-b',2,1,'issues'),
                ('c','batch','source','image-c',1,1,'approved');""",
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
