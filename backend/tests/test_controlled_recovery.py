from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from manga_localizer.database import Job, JobItem, JobStatus, PageGeneration
from manga_localizer.main import create_app, create_controlled_recovery_app
from manga_localizer.queue import JobConflict
from manga_localizer.services.controlled_recovery import (
    ControlledRecoveryBinding,
    schema_digest,
)
from manga_localizer.services.page_lineage import PageLineageConflict
from manga_localizer.services.projects import ProjectError

from .conftest import create_project, upload_image
from .test_final_reviews import _ACTOR, _complete_repair_g10, _strict_batch


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_schema(path: Path) -> list[tuple[str, str, str, str | None]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """SELECT type, name, tbl_name, sql
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type, name"""
        ).fetchall()


def _binding(
    project_root: Path,
    batch: dict,
    item: dict,
    generation: PageGeneration,
    repair_image_id: str,
):
    database = project_root / "project/project.sqlite3"
    review_root = Path(batch["rootPath"])
    return ControlledRecoveryBinding(
        project_id=item["sourceProjectId"],
        project_manifest_path=project_root / "project/project.json",
        project_database_path=database,
        media_root=project_root,
        expected_schema_digest=schema_digest(database),
        expected_startup_mutations=(),
        image_id=repair_image_id,
        generation_id=generation.id,
        run_id=generation.run_id,
        final_review_manifest_path=review_root / "final-review/manifest.json",
        final_review_database_path=review_root / "final-review/final-review.sqlite3",
        final_review_batch_id=batch["id"],
        final_review_item_id=item["id"],
    )


@pytest.fixture
def controlled_fixture(settings, tmp_path: Path):
    normal = create_app(settings, start_worker=False)
    with TestClient(normal) as client:
        batch = _strict_batch(normal, client, tmp_path / "bound")
        item = batch["items"][0]
        target_store = normal.state.registry.get(item["sourceProjectId"])
        saved = client.patch(
            f"/api/final-review-items/{item['id']}",
            json={
                "verdict": "issues",
                "issueCodes": ["translation"],
                "feedback": "controlled recovery fixture",
                "expectedRevision": item["revision"],
                "expectedBatchRevision": batch["revision"],
                "actor": _ACTOR,
            },
        ).json()
        repaired = client.post(
            f"/api/final-review-items/{item['id']}/repair",
            json={
                "expectedRevision": saved["item"]["revision"],
                "expectedBatchRevision": saved["batchRevision"],
                "actor": _ACTOR,
            },
        )
        assert repaired.status_code == 201, repaired.text
        handoff = repaired.json()
        _complete_repair_g10(normal, client, tmp_path / "bound-repair", handoff)
        with target_store.session() as session:
            generation = session.get(PageGeneration, handoff["pageGenerationId"])
            assert generation is not None
        other = create_project(client, tmp_path / "other", name="other")
        other_image = upload_image(client, other["id"], relative_path="other.png")
        binding = _binding(target_store.root, batch, item, generation, handoff["repairImageId"])
    return binding, other, other_image


def test_controlled_startup_is_catalog_worker_and_other_project_isolated(
    settings, controlled_fixture
) -> None:
    binding, other, _other_image = controlled_fixture
    catalog = settings.catalog_path
    catalog_before = _sha256(catalog)
    other_manifest = Path(other["rootPath"]) / "project/project.json"
    other_before = _sha256(other_manifest)

    app = create_controlled_recovery_app(settings, binding)
    with TestClient(app) as client:
        assert app.state.queue.running is False
        assert [store.root for store in app.state.registry.stores()] == [binding.media_root]
        preflight = client.get("/api/controlled-recovery/preflight")
        assert preflight.status_code == 200
        assert preflight.json()["schemaDigest"] == binding.expected_schema_digest
        assert preflight.json()["startupMutations"] == []
        assert client.get(f"/api/projects/{binding.project_id}").status_code == 200
        assert client.get(f"/api/images/{binding.image_id}/generated/quality").status_code == 200
        assert client.get(f"/api/projects/{other['id']}").status_code == 403
        assert client.get("/api/projects").status_code == 403
        assert (
            client.post(
                "/api/projects/open", json={"manifestPath": str(other_manifest)}
            ).status_code
            == 403
        )

    assert _sha256(catalog) == catalog_before
    assert _sha256(other_manifest) == other_before


def test_controlled_factory_fails_before_writes_for_schema_or_plan_mismatch(
    settings, controlled_fixture
) -> None:
    binding, _other, _other_image = controlled_fixture
    catalog_before = _sha256(settings.catalog_path)
    manifest_before = _sha256(binding.project_manifest_path)

    with pytest.raises(ProjectError, match="schema differs"):
        create_controlled_recovery_app(
            settings,
            ControlledRecoveryBinding(**{**binding.__dict__, "expected_schema_digest": "0" * 64}),
        )
    with pytest.raises(ProjectError, match="mutation plan must be empty"):
        create_controlled_recovery_app(
            settings,
            ControlledRecoveryBinding(
                **{**binding.__dict__, "expected_startup_mutations": ("normalize",)}
            ),
        )

    assert _sha256(settings.catalog_path) == catalog_before
    assert _sha256(binding.project_manifest_path) == manifest_before


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("project-trigger", "project schema is incomplete or unprotected"),
        ("review-trigger", "final-review schema is incomplete or unprotected"),
        ("target-verdict", "final-review item does not match"),
        ("stale-issue", "issue evidence does not match G0"),
    ],
)
def test_controlled_preflight_rejects_tampered_protection_and_item_state_without_writes(
    settings,
    controlled_fixture,
    tamper: str,
    error: str,
) -> None:
    binding, _other, _other_image = controlled_fixture
    project_database = binding.project_database_path
    review_database = binding.final_review_database_path
    if tamper == "project-trigger":
        with sqlite3.connect(project_database) as connection:
            connection.execute("DROP TRIGGER page_mask_reviews_no_update")
        # The caller knows the altered schema exactly. Protection validation is
        # independent, so supplying its new digest must still fail closed.
        binding = replace(binding, expected_schema_digest=schema_digest(project_database))
    elif tamper == "review-trigger":
        with sqlite3.connect(review_database) as connection:
            connection.execute("DROP TRIGGER final_review_revisions_no_delete")
    elif tamper == "target-verdict":
        with sqlite3.connect(review_database) as connection:
            connection.execute(
                "UPDATE items SET verdict = 'pending' WHERE id = ?",
                (binding.final_review_item_id,),
            )
    else:
        with sqlite3.connect(review_database) as connection:
            connection.execute(
                """UPDATE items
                   SET feedback = 'changed after G0', revision = revision + 1
                   WHERE id = ?""",
                (binding.final_review_item_id,),
            )

    protected_files = (
        settings.catalog_path,
        binding.project_manifest_path,
        binding.final_review_manifest_path,
    )
    file_preimages = {path: path.read_bytes() for path in protected_files}
    project_schema = _sqlite_schema(project_database)
    review_schema = _sqlite_schema(review_database)
    with sqlite3.connect(review_database) as connection:
        item_preimage = connection.execute(
            "SELECT verdict, revision, feedback FROM items WHERE id = ?",
            (binding.final_review_item_id,),
        ).fetchone()

    with pytest.raises(ProjectError, match=error):
        create_controlled_recovery_app(settings, binding)

    assert {path: path.read_bytes() for path in protected_files} == file_preimages
    assert _sqlite_schema(project_database) == project_schema
    assert _sqlite_schema(review_database) == review_schema
    with sqlite3.connect(review_database) as connection:
        assert (
            connection.execute(
                "SELECT verdict, revision, feedback FROM items WHERE id = ?",
                (binding.final_review_item_id,),
            ).fetchone()
            == item_preimage
        )


def test_controlled_requests_reject_other_page_and_generation_before_job_write(
    settings, controlled_fixture
) -> None:
    binding, _other, other_image = controlled_fixture
    app = create_controlled_recovery_app(settings, binding)
    with TestClient(app) as client:
        store = app.state.registry.get(binding.project_id)
        with store.session() as session:
            jobs_before = session.query(Job).count()
        wrong_page = client.post(
            f"/api/projects/{binding.project_id}/ocr",
            json={
                "imageIds": [other_image["id"]],
                "regionIds": [],
                "options": {},
                "lineage": {
                    "runId": binding.run_id,
                    "actor": {
                        "actorKind": "codex",
                        "taskId": "controlled-test",
                        "operationSource": "api",
                    },
                    "pages": [
                        {
                            "imageId": other_image["id"],
                            "pageGenerationId": binding.generation_id,
                            "expectedSequence": 1,
                        }
                    ],
                },
            },
        )
        assert wrong_page.status_code == 403
        wrong_generation = client.patch(
            f"/api/images/{binding.image_id}/page-gates/ocr",
            json={
                "decision": "accept",
                "observedOcrChecksum": "0" * 64,
                "expectedRevision": 1,
                "lineage": {
                    "runId": binding.run_id,
                    "pageGenerationId": "00000000-0000-0000-0000-000000000000",
                    "expectedSequence": 1,
                    "actor": {
                        "actorKind": "codex",
                        "taskId": "controlled-test",
                        "operationSource": "api",
                    },
                },
            },
        )
        assert wrong_generation.status_code == 403
        with store.session() as session:
            assert session.query(Job).count() == jobs_before


def test_controlled_mode_allows_exact_refresh_but_denies_restart_approval_and_export(
    settings, controlled_fixture
) -> None:
    binding, _other, _other_image = controlled_fixture
    app = create_controlled_recovery_app(settings, binding)
    actor = {
        "actorKind": "codex",
        "taskId": "controlled-test",
        "operationSource": "api",
    }
    with TestClient(app) as client:
        review_store = app.state.final_reviews.find_item(binding.final_review_item_id)
        item_before = review_store.item(binding.final_review_item_id)
        batch_before = review_store.batch()
        refreshed = client.post(
            f"/api/final-review-items/{binding.final_review_item_id}/refresh",
            json={
                "expectedRevision": item_before["revision"],
                "expectedBatchRevision": batch_before["revision"],
                "actor": actor,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["item"]["id"] == binding.final_review_item_id
        assert refreshed.json()["item"]["verdict"] == "pending"
        assert (
            client.post(f"/api/images/{binding.image_id}/page-generations", json={}).status_code
            == 403
        )
        assert (
            client.patch(
                f"/api/final-review-items/{binding.final_review_item_id}", json={}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/final-review-batches/{binding.final_review_batch_id}/export", json={}
            ).status_code
            == 403
        )


@pytest.mark.anyio
async def test_one_off_executor_claims_only_the_exact_bound_job(
    settings, controlled_fixture
) -> None:
    binding, _other, _other_image = controlled_fixture
    app = create_controlled_recovery_app(settings, binding)
    with TestClient(app):
        store = app.state.registry.get(binding.project_id)
        lineage = {
            "runId": binding.run_id,
            "pages": [
                {
                    "imageId": binding.image_id,
                    "pageGenerationId": binding.generation_id,
                    "expectedSequence": 1,
                }
            ],
        }
        with store.session() as session:
            exact = Job(
                project_id=binding.project_id,
                kind="ocr",
                status=JobStatus.QUEUED.value,
                options={},
                lineage_context=lineage,
                total=1,
            )
            unrelated = Job(
                project_id=binding.project_id,
                kind="ocr",
                status=JobStatus.QUEUED.value,
                options={},
                lineage_context=lineage,
                total=1,
            )
            session.add_all([exact, unrelated])
            session.flush()
            exact_item = JobItem(job_id=exact.id, image_id=binding.image_id, position=0)
            unrelated_item = JobItem(job_id=unrelated.id, image_id=binding.image_id, position=0)
            session.add_all([exact_item, unrelated_item])
            session.flush()
            exact_id, exact_item_id, unrelated_id = exact.id, exact_item.id, unrelated.id

        entered = asyncio.Event()
        release = asyncio.Event()

        async def finish_exact(_store, job_id: str, item_id: str) -> None:
            assert job_id == exact_id
            assert item_id == exact_item_id
            entered.set()
            await release.wait()
            with store.session() as session:
                item = session.get(JobItem, item_id)
                assert item is not None
                item.status = JobStatus.COMPLETED.value
                item.progress = 1.0

        with pytest.raises(PageLineageConflict):
            await app.state.queue.execute_controlled_job(
                store,
                exact_id,
                project_id=binding.project_id,
                image_id=binding.image_id,
                generation_id=binding.generation_id,
                run_id=binding.run_id,
                allowed_kinds=binding.executable_job_kinds,
            )
        with store.session() as session:
            assert session.get(Job, exact_id).status == JobStatus.QUEUED.value
            assert session.get(JobItem, exact_item_id).status == JobStatus.QUEUED.value

        with (
            patch("manga_localizer.queue.require_job_lineage_for_execution"),
            patch.object(
                app.state.queue, "_execute_item", AsyncMock(side_effect=finish_exact)
            ) as execute_item,
        ):
            first = asyncio.create_task(
                app.state.queue.execute_controlled_job(
                    store,
                    exact_id,
                    project_id=binding.project_id,
                    image_id=binding.image_id,
                    generation_id=binding.generation_id,
                    run_id=binding.run_id,
                    allowed_kinds=binding.executable_job_kinds,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            with pytest.raises(JobConflict, match="outside"):
                await app.state.queue.execute_controlled_job(
                    store,
                    exact_id,
                    project_id=binding.project_id,
                    image_id=binding.image_id,
                    generation_id=binding.generation_id,
                    run_id=binding.run_id,
                    allowed_kinds=binding.executable_job_kinds,
                )
            assert execute_item.await_count == 1
            release.set()
            result = await first
        assert result.status == JobStatus.COMPLETED.value
        with store.session() as session:
            assert session.get(Job, unrelated_id).status == JobStatus.QUEUED.value
        with pytest.raises(JobConflict, match=r"outside|queued"):
            await app.state.queue.execute_controlled_job(
                store,
                exact_id,
                project_id=binding.project_id,
                image_id=binding.image_id,
                generation_id=binding.generation_id,
                run_id=binding.run_id,
                allowed_kinds=binding.executable_job_kinds,
            )
