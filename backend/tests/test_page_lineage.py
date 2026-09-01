from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

import manga_localizer.queue as queue_module
import manga_localizer.services.regions as regions_module
from manga_localizer.config import Settings
from manga_localizer.database import (
    ImageAsset,
    ImportBoundary,
    Job,
    JobItem,
    PageCleanPlateCandidate,
    PageCleanPlateReview,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    PageMaskDraft,
    PageMaskReview,
    PageTranslationReview,
    Project,
    RegionOCRAttempt,
    RegionTranslationCandidate,
    RegionTranslationReview,
    Revision,
    TextRegion,
    create_project_engine,
)
from manga_localizer.imaging.lineart_inpaint import (
    CANDIDATE_LAMA_FULL_CONTEXT,
    CANDIDATE_PRIMARY,
)
from manga_localizer.main import create_app
from manga_localizer.providers.ocr import OCRRegion
from manga_localizer.schemas import CleanPlateLayeredRouteEntry
from manga_localizer.security import resolve_write_target, safe_relative_path
from manga_localizer.services import clean_plates as clean_plate_service
from manga_localizer.services import masks as mask_service
from manga_localizer.services import page_lineage
from manga_localizer.services.clean_plates import require_current_clean_plate_acceptance
from manga_localizer.services.images import make_inpaint_provenance
from manga_localizer.services.inpaint_candidates import (
    candidate_image_path,
    inpaint_candidate_manifest_digest,
    load_layered_structure_snapshots,
    snapshot_layered_structure_references,
    write_page_inpaint_candidates,
)
from manga_localizer.services.masks import require_current_mask_acceptance
from manga_localizer.services.page_lineage import (
    g4_region_state_checksum,
    public_page_lineage_event,
    require_current_background_classifications,
    require_current_ocr_trust,
)
from manga_localizer.services.projects import ProjectError, add_revision

from .conftest import create_project, png_bytes, upload_image

_PARAMETER_HASH = "a" * 64
_RUN_ID = "rebuild-r1-test-run"
_ACTOR = {
    "actorKind": "codex",
    "taskId": "lineage-test-task",
    "threadId": "lineage-test-thread",
    "sessionId": "lineage-test-session",
    "operationSource": "api",
}
_OCR_QC_CHECKS = [
    "original-and-quality-compared",
    "source-text-characters-checked",
    "punctuation-checked",
    "direction-checked",
    "reading-order-checked",
    "empty-or-garbled-checked",
    "duplicate-fragment-checked",
    "template-contamination-checked",
    "page-text-consistency-checked",
]


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _generation_body(
    *,
    source_project_id: str,
    source_image_id: str,
    checksum: str,
    generation_id: str | None = None,
    expected_revision: int = 1,
) -> dict[str, object]:
    return {
        "runId": _RUN_ID,
        "pageGenerationId": generation_id or str(uuid.uuid4()),
        "parameterSetId": "rebuild-r1-test-parameters-v1",
        "parameterSetHash": _PARAMETER_HASH,
        "restartFromSource": True,
        "sourceProjectId": source_project_id,
        "sourceImageId": source_image_id,
        "expectedSourceChecksum": checksum,
        "expectedRevision": expected_revision,
        "actor": _ACTOR,
    }


def _lineage_context(
    image_id: str, generation_id: str, expected_sequence: int = 2
) -> dict[str, object]:
    return {
        "runId": _RUN_ID,
        "actor": _ACTOR,
        "pages": [
            {
                "imageId": image_id,
                "pageGenerationId": generation_id,
                "expectedSequence": expected_sequence,
            }
        ],
    }


def _current_lineage_context(
    client: TestClient, image_id: str, generation_id: str
) -> dict[str, object]:
    generations = client.get(f"/api/images/{image_id}/page-generations")
    assert generations.status_code == 200, generations.text
    current = next(
        generation for generation in generations.json() if generation["id"] == generation_id
    )
    return _lineage_context(image_id, generation_id, current["nextSequence"])


def _project_image(client: TestClient, project_id: str, image_id: str) -> dict[str, object]:
    return next(
        row
        for row in client.get(f"/api/projects/{project_id}/images").json()
        if row["id"] == image_id
    )


def _mutation_lineage(generation_id: str, expected_sequence: int) -> dict[str, object]:
    return {
        "runId": _RUN_ID,
        "pageGenerationId": generation_id,
        "expectedSequence": expected_sequence,
        "actor": _ACTOR,
    }


def _source_and_target(client: TestClient, tmp_path: Path):
    source_data = png_bytes(color="white", rectangle=(20, 20, 100, 80))
    source_project = create_project(client, tmp_path / "source", "source")
    source_image = upload_image(
        client,
        source_project["id"],
        relative_path="chapter/page-001.png",
        data=source_data,
    )
    target_project = create_project(client, tmp_path / "target", "target")
    target_image = upload_image(
        client,
        target_project["id"],
        relative_path="chapter/page-001.png",
        data=source_data,
    )
    return source_data, source_project, source_image, target_project, target_image


def _accept_g1_preprocess(
    client: TestClient,
    app,
    *,
    target_project: dict[str, object],
    target_image: dict[str, object],
    generation_id: str,
) -> tuple[str, dict[str, object]]:
    project_id = str(target_project["id"])
    image_id = str(target_image["id"])
    queued = client.post(
        f"/api/projects/{project_id}/preprocess",
        json={
            "imageIds": [image_id],
            "options": {"profile": "off"},
            "lineage": _lineage_context(image_id, generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    store = app.state.registry.get(project_id)
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    completed = app.state.queue.get_job(store, queued.json()["id"])
    assert completed.status == "completed"

    quality_bytes = client.get(f"/api/images/{image_id}/generated/preprocessed").content
    quality_checksum = _checksum(quality_bytes)
    current_image = _project_image(client, project_id, image_id)
    generation = client.get(f"/api/images/{image_id}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{image_id}/stage-reviews/preprocess",
        json={
            "state": "accepted",
            "expectedRevision": current_image["revision"],
            "observedArtifactChecksum": quality_checksum,
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    return quality_checksum, accepted.json()


def _accept_g2_without_reconstruction(
    client: TestClient,
    *,
    image_id: str,
    generation_id: str,
    quality_checksum: str,
    image_revision: int,
    expected_sequence: int,
) -> dict[str, object]:
    accepted = client.patch(
        f"/api/images/{image_id}/page-gates/reconstruction",
        json={
            "decision": "no",
            "reason": "baseline-preserves-original-structure",
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": image_revision,
            "lineage": _mutation_lineage(generation_id, expected_sequence),
        },
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _accept_g3_text_present(
    client: TestClient,
    *,
    source_checksum: str,
    image_id: str,
    generation_id: str,
    quality_checksum: str,
    image_revision: int,
    expected_sequence: int,
) -> dict[str, object]:
    accepted = client.patch(
        f"/api/images/{image_id}/page-gates/text-presence",
        json={
            "decision": "yes",
            "reason": "processable-text-visible",
            "evidence": ["original-and-quality-compared", "dialogue-visible"],
            "observedOriginalChecksum": source_checksum,
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": image_revision,
            "lineage": _mutation_lineage(generation_id, expected_sequence),
        },
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


class _LineageDetector:
    name = "tesseract"

    def __init__(self, detections: list[OCRRegion] | None = None) -> None:
        self.detections = (
            detections
            if detections is not None
            else [OCRRegion(20, 30, 80, 40, "候補", 0.9, "vertical")]
        )
        self.calls = 0

    def detect_text_regions(self, _image: Path, **_options: object) -> list[OCRRegion]:
        self.calls += 1
        return list(self.detections)


class _StrictLineageOCR:
    name = "tesseract"

    def __init__(self) -> None:
        self.calls: list[tuple[Path, dict[str, int], str, str | None]] = []

    def get_capabilities(self) -> dict[str, object]:
        return {"version": "strict-test-v1"}

    def recognize_region(
        self,
        image: Path,
        region: dict[str, int],
        *,
        direction: str,
        language: str | None,
    ) -> OCRRegion:
        self.calls.append((image, dict(region), direction, language))
        is_quality = "generated/preprocessed" in image.as_posix()
        return OCRRegion(
            region["x"],
            region["y"],
            region["width"],
            region["height"],
            "品質本文" if is_quality else "原文本文",
            0.75 if is_quality else 0.0,
            direction,
        )


def _prepare_g3_yes_page(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    if prepared is None:
        data, source_project, source_image, target_project, target_image = _source_and_target(
            client, tmp_path
        )
    else:
        data = prepared["data"]
        source_project = prepared["sourceProject"]
        source_image = prepared["sourceImage"]
        target_project = prepared["targetProject"]
        target_image = prepared["targetImage"]
        assert isinstance(data, bytes)
        assert isinstance(source_project, dict) and isinstance(source_image, dict)
        assert isinstance(target_project, dict) and isinstance(target_image, dict)
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    quality_checksum, accepted_g1 = _accept_g1_preprocess(
        client,
        app,
        target_project=target_project,
        target_image=target_image,
        generation_id=generation_id,
    )
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted_g2 = _accept_g2_without_reconstruction(
        client,
        image_id=target_image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=accepted_g1["revision"],
        expected_sequence=generation["nextSequence"],
    )
    accepted_g3 = _accept_g3_text_present(
        client,
        source_checksum=_checksum(data),
        image_id=target_image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=accepted_g2["imageRevision"],
        expected_sequence=accepted_g2["nextSequence"],
    )
    return {
        "data": data,
        "sourceProject": source_project,
        "sourceImage": source_image,
        "targetProject": target_project,
        "targetImage": target_image,
        "generationId": generation_id,
        "qualityChecksum": quality_checksum,
        "acceptedG3": accepted_g3,
        "store": app.state.registry.get(target_project["id"]),
    }


def _prepare_g4_accepted_page(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    disposition: str = "translate",
    region_type: str = "dialogue",
    extra_dispositions: tuple[str, ...] = (),
    include_ruby: bool = False,
    accept_g4: bool = True,
    rotation: float = 0,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared = prepared or _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _LineageDetector()
    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))

    region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    current_image = _project_image(client, target_project["id"], target_image["id"])
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    decided = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "type": region_type,
            "rotation": rotation,
            "direction": "vertical",
            "paragraphGroupId": "paragraph-1",
            "contentDisposition": disposition,
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert decided.status_code == 200, decided.text
    region = decided.json()
    next_order = 1
    for extra_disposition in extra_dispositions:
        current_image = _project_image(client, target_project["id"], target_image["id"])
        generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
        created = client.post(
            f"/api/images/{target_image['id']}/regions",
            json={
                "x": 5 + (next_order % 4) * 30,
                "y": 5 + (next_order // 4) * 35,
                "width": 20,
                "height": 20,
                "type": "dialogue",
                "direction": "vertical",
                "order": next_order,
                "paragraphGroupId": f"paragraph-{next_order + 1}",
                "contentDisposition": extra_disposition,
                "expectedImageRevision": current_image["revision"],
                "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
            },
        )
        assert created.status_code == 201, created.text
        next_order += 1
    if include_ruby:
        current_image = _project_image(client, target_project["id"], target_image["id"])
        generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
        created_ruby = client.post(
            f"/api/images/{target_image['id']}/regions",
            json={
                "x": 80,
                "y": 25,
                "width": 20,
                "height": 15,
                "type": "ruby",
                "direction": "vertical",
                "order": next_order,
                "paragraphGroupId": "paragraph-1",
                "rubyParentId": region["id"],
                "contentDisposition": "ignore",
                "expectedImageRevision": current_image["revision"],
                "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
            },
        )
        assert created_ruby.status_code == 201, created_ruby.text
    mutation_event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
    if not accept_g4:
        regions = client.get(f"/api/images/{target_image['id']}/regions").json()
        return prepared | {
            "region": next(item for item in regions if item["id"] == region["id"]),
            "regions": regions,
            "g4MutationEvent": mutation_event,
            "acceptedG4": None,
        }
    current_image = _project_image(client, target_project["id"], target_image["id"])
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": mutation_event["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    return prepared | {
        "region": next(item for item in regions if item["id"] == region["id"]),
        "regions": regions,
        "acceptedG4": accepted.json(),
    }


def _prepare_g5_accepted_page(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    disposition: str = "translate",
    region_type: str = "dialogue",
    extra_dispositions: tuple[str, ...] = (),
    include_ruby: bool = False,
    background_category: str = "white-solid",
    rotation: float = 0,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        disposition=disposition,
        region_type=region_type,
        extra_dispositions=extra_dispositions,
        include_ruby=include_ruby,
        rotation=rotation,
        prepared=prepared,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    for region in prepared["regions"]:
        if region["type"] == "ruby" or region["contentDisposition"] not in {
            "translate",
            "redraw-art",
        }:
            continue
        current_image = _project_image(client, target_project["id"], target_image["id"])
        generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
        classified = client.patch(
            f"/api/regions/{region['id']}/background-classification",
            json={
                "category": background_category,
                "confidence": 0,
                "rationaleCodes": [
                    {
                        "white-solid": "uniform-near-white",
                        "black-solid": "uniform-near-black",
                        "other-solid": "uniform-other-color",
                        "simple-gradient": "smooth-gradient-continuity",
                        "screentone": "periodic-screentone",
                        "complex-lineart": "structural-lines-cross-region",
                        "illustration/character": "character-or-illustration-detail",
                    }[background_category]
                ],
                "expectedRevision": region["revision"],
                "expectedImageRevision": current_image["revision"],
                "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
            },
        )
        assert classified.status_code == 200, classified.text
        context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
        for index, current in enumerate(prepared["regions"]):
            if current["id"] == region["id"]:
                prepared["regions"][index] = classified.json()
                break
    current_image = _project_image(client, target_project["id"], target_image["id"])
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": (
                "all-eligible-backgrounds-reviewed"
                if context["eligibleRegionIds"]
                else "no-eligible-regions"
            ),
            "observedBackgroundChecksum": context["backgroundChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    return prepared | {
        "regions": regions,
        "region": next(row for row in regions if row["id"] == prepared["region"]["id"]),
        "acceptedG5": accepted.json(),
    }


def _prepare_g6_accepted_page(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    disposition: str = "translate",
    region_type: str = "dialogue",
    extra_dispositions: tuple[str, ...] = (),
    include_ruby: bool = False,
    background_category: str = "white-solid",
    ocr_provider=None,
    rotation: float = 0,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared = _prepare_g5_accepted_page(
        client,
        app,
        tmp_path,
        disposition=disposition,
        region_type=region_type,
        extra_dispositions=extra_dispositions,
        include_ruby=include_ruby,
        background_category=background_category,
        rotation=rotation,
        prepared=prepared,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    if context["eligibleRegionIds"]:
        app.state.providers.ocr = ocr_provider or _StrictLineageOCR()
        queued = client.post(
            f"/api/projects/{target_project['id']}/ocr",
            json={
                "imageIds": [target_image["id"]],
                "options": {"provider": "tesseract", "language": "ja"},
                "lineage": _current_lineage_context(client, target_image["id"], generation_id),
            },
        )
        assert queued.status_code == 202, queued.text
        claimed = app.state.queue._claim_next()
        assert claimed == (store, queued.json()["id"])
        asyncio.run(app.state.queue._execute(*claimed))
        completed_job = app.state.queue.get_job(store, queued.json()["id"])
        assert completed_job.status == "completed", [item.error for item in completed_job.items]
        context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
        for region_id in context["eligibleRegionIds"]:
            region = next(
                row
                for row in client.get(f"/api/images/{target_image['id']}/regions").json()
                if row["id"] == region_id
            )
            attempt = next(
                row
                for row in context["attempts"]
                if row["regionId"] == region_id and row["inputVariant"] == "quality"
            )
            current_image = _project_image(client, target_project["id"], target_image["id"])
            generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
            reviewed = client.patch(
                f"/api/regions/{region_id}/ocr-source-review",
                json={
                    "sourceText": attempt["text"],
                    "sourceMode": "quality-attempt",
                    "selectedAttemptId": attempt["id"],
                    "qcChecks": _OCR_QC_CHECKS,
                    "expectedRevision": region["revision"],
                    "expectedImageRevision": current_image["revision"],
                    "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
                },
            )
            assert reviewed.status_code == 200, reviewed.text
        context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    current_image = _project_image(client, target_project["id"], target_image["id"])
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/ocr",
        json={
            "decision": "accept",
            "reason": (
                "all-translatable-source-text-reviewed"
                if context["eligibleRegionIds"]
                else "no-translatable-regions"
            ),
            "observedOcrChecksum": context["ocrChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    return prepared | {"acceptedG6": accepted.json()}


def _counts(store) -> tuple[int, int, int, int, int]:
    with store.session() as session:
        project = store.project(session)
        image = session.scalar(select(ImageAsset))
        assert image is not None
        return (
            project.revision,
            image.revision,
            session.scalar(select(func.count()).select_from(Revision)) or 0,
            session.scalar(select(func.count()).select_from(PageGeneration)) or 0,
            session.scalar(select(func.count()).select_from(PageLineageEvent)) or 0,
        )


def test_g0_generation_checksum_conflict_is_zero_write_and_success_is_append_only(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    store = app.state.registry.get(target_project["id"])
    before = _counts(store)
    generation_id = str(uuid.uuid4())
    bad = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum="b" * 64,
            generation_id=generation_id,
        ),
    )
    assert bad.status_code == 409, bad.text
    assert bad.json()["detail"]["reason"] == "source-checksum-mismatch"
    assert _counts(store) == before

    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    generation = created.json()
    assert generation == {
        "id": generation_id,
        "runId": _RUN_ID,
        "projectId": target_project["id"],
        "imageId": target_image["id"],
        "restartFromSource": True,
        "parameterSetId": "rebuild-r1-test-parameters-v1",
        "parameterSetHash": _PARAMETER_HASH,
        "sourceProjectId": source_project["id"],
        "sourceImageId": source_image["id"],
        "sourceChecksum": _checksum(data),
        "state": "active",
        "nextSequence": 2,
        "actor": _ACTOR | {"actorId": None},
        "createdAt": generation["createdAt"],
        "closedAt": None,
    }
    events_response = client.get(f"/api/page-generations/{generation_id}/events")
    assert events_response.status_code == 200, events_response.text
    events = events_response.json()
    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["gate"] == "G0_identity"
    assert events[0]["state"] == "accepted"
    assert events[0]["inputChecksum"] == _checksum(data)
    assert events[0]["outputChecksum"] == _checksum(data)
    assert events[0]["revisionId"]
    assert events[0]["evidence"] == {
        "eventType": "generation-created",
        "restartFromSource": True,
        "targetImageId": target_image["id"],
    }
    encoded = json.dumps({"generation": generation, "events": events}, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "sourceText" not in encoded
    assert "translationText" not in encoded
    assert "feedback" not in encoded

    with sqlite3.connect(store.database_path) as database:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute(
                "UPDATE page_lineage_events SET state = 'rejected' WHERE generation_id = ?",
                (generation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute(
                "DELETE FROM page_lineage_events WHERE generation_id = ?",
                (generation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="G0 lineage revisions are append-only"):
            database.execute(
                "UPDATE revisions SET operation = 'tampered' WHERE id = ?",
                (events[0]["revisionId"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="G0 lineage revisions are append-only"):
            database.execute(
                "DELETE FROM revisions WHERE id = ?",
                (events[0]["revisionId"],),
            )
    assert len(client.get(f"/api/page-generations/{generation_id}/events").json()) == 1

    before_untracked_mutations = _counts(store)
    region = client.post(
        f"/api/images/{target_image['id']}/regions",
        json={"x": 10, "y": 10, "width": 30, "height": 30},
    )
    assert region.status_code == 409, region.text
    assert region.json()["detail"]["reason"] == "lineage-required"
    review = client.patch(
        f"/api/images/{target_image['id']}/review",
        json={"reviewState": "no-text-reviewed", "expectedRevision": 2},
    )
    assert review.status_code == 409, review.text
    assert review.json()["detail"]["reason"] == "lineage-required"
    current_project = client.get(f"/api/projects/{target_project['id']}").json()
    settings_change = client.patch(
        f"/api/projects/{target_project['id']}",
        json={
            "settings": {"sourceLanguage": "en"},
            "expectedRevision": current_project["revision"],
        },
    )
    assert settings_change.status_code == 409, settings_change.text
    assert settings_change.json()["detail"]["reason"] == ("active-generation-settings-lock")
    assert _counts(store) == before_untracked_mutations


def test_g0_generation_creation_rejects_missing_identity_guard_without_writes(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    store = app.state.registry.get(target_project["id"])
    before = _counts(store)
    with sqlite3.connect(store.database_path) as database:
        database.execute("DROP TRIGGER page_lineage_events_no_update")

    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=str(uuid.uuid4()),
        ),
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"] == "G0 identity evidence is not protected as append-only"
    assert _counts(store) == before


def test_historical_generation_without_an_active_successor_is_not_legacy(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    store = app.state.registry.get(target_project["id"])
    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        assert generation is not None
        generation.state = "superseded"
        generation.closed_at = generation.created_at

    before = _counts(store)
    region = client.post(
        f"/api/images/{target_image['id']}/regions",
        json={"x": 10, "y": 10, "width": 30, "height": 30},
    )
    assert region.status_code == 409, region.text
    assert region.json()["detail"]["reason"] == "page-generation-not-active"

    job = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={"imageIds": [target_image["id"]], "options": {"profile": "off"}},
    )
    assert job.status_code == 409, job.text
    assert job.json()["detail"]["reason"] == "page-generation-not-active"

    current_project = client.get(f"/api/projects/{target_project['id']}").json()
    settings = client.patch(
        f"/api/projects/{target_project['id']}",
        json={
            "settings": {"sourceLanguage": "en"},
            "expectedRevision": current_project["revision"],
        },
    )
    assert settings.status_code == 409, settings.text
    assert settings.json()["detail"]["reason"] == "active-generation-settings-lock"
    assert _counts(store) == before


def test_active_generation_requires_exact_job_binding_and_records_pending_evidence(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    store = app.state.registry.get(target_project["id"])

    missing = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={"imageIds": [target_image["id"]], "options": {"profile": "off"}},
    )
    assert missing.status_code == 409, missing.text
    assert missing.json()["detail"]["reason"] == "lineage-required"
    wrong = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={
            "imageIds": [target_image["id"]],
            "options": {"profile": "off"},
            "lineage": _lineage_context(target_image["id"], str(uuid.uuid4())),
        },
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["detail"]["reason"] == "generation-mismatch"
    gated_detect = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert gated_detect.status_code == 409, gated_detect.text
    assert gated_detect.json()["detail"]["reason"] == "g1-not-accepted"
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(PageLineageEvent)) == 1

    queued = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={
            "imageIds": [target_image["id"]],
            "options": {"profile": "off"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    encoded_job = json.dumps(job, ensure_ascii=False)
    assert "lineage" not in encoded_job.lower()
    assert str(tmp_path) not in encoded_job
    with store.session() as session:
        persisted = session.get(Job, job["id"])
        assert persisted is not None
        assert persisted.lineage_context == {
            "version": 1,
            "runId": _RUN_ID,
            "actor": _ACTOR | {"actorId": None},
            "pages": [
                {
                    "imageId": target_image["id"],
                    "pageGenerationId": generation_id,
                    "expectedSequence": 2,
                }
            ],
        }
    enqueued_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["sequence"] for event in enqueued_events] == [1, 2]
    assert enqueued_events[1]["operation"] == "preprocess-job-enqueued"
    assert enqueued_events[1]["gate"] == "G1_baselineUpscale"
    assert enqueued_events[1]["state"] == "pending"
    assert enqueued_events[1]["inputChecksum"] == _checksum(data)
    assert enqueued_events[1]["outputChecksum"] is None
    blocked_pause = client.post(f"/api/jobs/{job['id']}/pause")
    assert blocked_pause.status_code == 409, blocked_pause.text
    assert blocked_pause.json()["detail"]["reason"] == "lineage-action-not-supported"

    claimed = app.state.queue._claim_next()
    assert claimed is not None
    claimed_store, claimed_job_id = claimed
    assert claimed_store is store
    assert claimed_job_id == job["id"]
    item_id = job["items"][0]["id"]
    assert app.state.queue._begin_item(store, job["id"], item_id)
    output = app.state.queue._process_item(store, job["id"], item_id)
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[2]["operation"] == "preprocess-artifact-produced"
    assert events[2]["state"] == "pending"
    assert events[2]["reason"] == "review-required"
    assert events[2]["inputChecksum"] == _checksum(data)
    assert len(events[2]["outputChecksum"]) == 64

    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    observed = client.get(f"/api/images/{target_image['id']}/generated/preprocessed").content
    review_body = {
        "state": "accepted",
        "expectedRevision": current_image["revision"],
        "observedArtifactChecksum": _checksum(observed),
    }
    missing_review_lineage = client.patch(
        f"/api/images/{target_image['id']}/stage-reviews/preprocess",
        json=review_body,
    )
    assert missing_review_lineage.status_code == 409, missing_review_lineage.text
    assert missing_review_lineage.json()["detail"]["reason"] == "lineage-required"
    premature_review = client.patch(
        f"/api/images/{target_image['id']}/stage-reviews/preprocess",
        json=review_body
        | {
            "lineage": {
                "runId": _RUN_ID,
                "pageGenerationId": generation_id,
                "expectedSequence": 4,
                "actor": _ACTOR,
            }
        },
    )
    assert premature_review.status_code == 409, premature_review.text
    assert premature_review.json()["detail"]["reason"] == "producer-not-completed"
    assert [
        event["sequence"]
        for event in client.get(f"/api/page-generations/{generation_id}/events").json()
    ] == [1, 2, 3]

    app.state.queue._finish_item(store, job["id"], item_id, output=output)
    completed = app.state.queue.get_job(store, job["id"])
    assert completed.status == "completed"
    completed_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["sequence"] for event in completed_events] == [1, 2, 3, 4]
    assert completed_events[-1]["operation"] == "preprocess-job-completed"
    assert completed_events[-1]["state"] == "pending"
    assert completed_events[-1]["outputChecksum"] == _checksum(observed)

    stale_review = client.patch(
        f"/api/images/{target_image['id']}/stage-reviews/preprocess",
        json=review_body
        | {
            "lineage": {
                "runId": _RUN_ID,
                "pageGenerationId": generation_id,
                "expectedSequence": 4,
                "actor": _ACTOR,
            }
        },
    )
    assert stale_review.status_code == 409, stale_review.text
    stale_detail = stale_review.json()["detail"]
    assert stale_detail["reason"] == "sequence-conflict"
    assert stale_detail["expectedSequence"] == 4
    assert stale_detail["actualSequence"] == 5
    accepted = client.patch(
        f"/api/images/{target_image['id']}/stage-reviews/preprocess",
        json=review_body
        | {
            "lineage": {
                "runId": _RUN_ID,
                "pageGenerationId": generation_id,
                "expectedSequence": 5,
                "actor": _ACTOR,
            }
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["stageReviews"]["preprocess"]["state"] == "accepted"
    reviewed_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["sequence"] for event in reviewed_events] == [1, 2, 3, 4, 5]
    assert reviewed_events[-1]["operation"] == "preprocess-stage-review"
    assert reviewed_events[-1]["gate"] == "G1_baselineUpscale"
    assert reviewed_events[-1]["state"] == "accepted"
    assert reviewed_events[-1]["inputChecksum"] == _checksum(data)
    assert reviewed_events[-1]["outputChecksum"] == _checksum(observed)
    assert reviewed_events[-1]["revisionId"]


@pytest.mark.parametrize(
    ("setup_action", "blocked_action", "expected_status"),
    (("pause", "resume", "paused"), ("cancel", "retry", "cancelled")),
)
def test_legacy_job_restart_actions_recheck_active_and_historical_page_lineage(
    client: TestClient,
    app,
    tmp_path: Path,
    setup_action: str,
    blocked_action: str,
    expected_status: str,
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    queued = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={"imageIds": [target_image["id"]], "options": {"profile": "off"}},
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    prepared = client.post(f"/api/jobs/{job_id}/{setup_action}")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == expected_status

    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    store = app.state.registry.get(target_project["id"])

    def snapshot() -> tuple[object, ...]:
        with store.session() as session:
            job = session.get(Job, job_id)
            project = session.get(Project, target_project["id"])
            assert job is not None and project is not None
            return (
                job.status,
                job.progress,
                job.completed,
                job.error,
                job.updated_at,
                json.dumps(job.options, sort_keys=True),
                tuple(
                    (
                        item.id,
                        item.status,
                        item.progress,
                        item.error,
                        json.dumps(item.output, sort_keys=True),
                        item.started_at,
                        item.finished_at,
                    )
                    for item in job.items
                ),
                project.revision,
                session.scalar(select(func.count()).select_from(Revision)),
                session.scalar(select(func.count()).select_from(PageLineageEvent)),
            )

    active_before = snapshot()
    active = client.post(f"/api/jobs/{job_id}/{blocked_action}")
    assert active.status_code == 409, active.text
    assert active.json()["detail"]["reason"] == "lineage-required"
    assert snapshot() == active_before

    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        assert generation is not None
        generation.state = "superseded"
    historical_before = snapshot()
    historical = client.post(f"/api/jobs/{job_id}/{blocked_action}")
    assert historical.status_code == 409, historical.text
    assert historical.json()["detail"]["reason"] == "page-generation-not-active"
    assert snapshot() == historical_before


def test_image_ingest_is_zero_write_when_project_has_active_or_historical_lineage(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    store = app.state.registry.get(target_project["id"])
    incoming = tmp_path / "incoming-page.png"
    incoming.write_bytes(png_bytes(color="lavender"))

    def snapshot() -> tuple[object, ...]:
        source_files = tuple(
            sorted(
                (
                    path.relative_to(store.source_root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in store.source_root.rglob("*")
                if path.is_file()
            )
        )
        with store.session() as session:
            project = session.get(Project, target_project["id"])
            assert project is not None
            return (
                project.input_root,
                project.revision,
                session.scalar(select(func.count()).select_from(ImageAsset)),
                session.scalar(select(func.count()).select_from(ImportBoundary)),
                session.scalar(select(func.count()).select_from(Revision)),
                session.scalar(select(func.count()).select_from(PageLineageEvent)),
                source_files,
            )

    for suffix in ("active", "historical"):
        before_upload = snapshot()
        upload = client.post(
            f"/api/projects/{target_project['id']}/images/upload",
            files=[("files", (f"{suffix}.png", png_bytes(color="gray"), "image/png"))],
        )
        assert upload.status_code == 409, upload.text
        assert upload.json()["detail"]["reason"] == "page-lineage-image-ingest-lock"
        assert snapshot() == before_upload

        before_local = snapshot()
        local = client.post(
            f"/api/projects/{target_project['id']}/images/import-local",
            json={"paths": [str(incoming)]},
        )
        assert local.status_code == 409, local.text
        assert local.json()["detail"]["reason"] == "page-lineage-image-ingest-lock"
        assert snapshot() == before_local

        if suffix == "active":
            with store.session() as session:
                generation = session.get(PageGeneration, generation_id)
                assert generation is not None
                generation.state = "superseded"


def test_g2_reconstruction_gate_requires_current_g1_and_latest_decision_wins(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    store = app.state.registry.get(target_project["id"])

    before_g1 = _counts(store)
    premature = client.patch(
        f"/api/images/{target_image['id']}/page-gates/reconstruction",
        json={
            "decision": "no",
            "reason": "baseline-preserves-original-structure",
            "observedQualityChecksum": _checksum(data),
            "expectedRevision": 2,
            "lineage": _mutation_lineage(generation_id, 2),
        },
    )
    assert premature.status_code == 409, premature.text
    assert premature.json()["detail"]["reason"] == "g1-not-accepted"
    assert _counts(store) == before_g1

    quality_checksum, accepted_g1 = _accept_g1_preprocess(
        client,
        app,
        target_project=target_project,
        target_image=target_image,
        generation_id=generation_id,
    )
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_bad_checksum = _counts(store)
    mismatch = client.patch(
        f"/api/images/{target_image['id']}/page-gates/reconstruction",
        json={
            "decision": "no",
            "reason": "baseline-preserves-original-structure",
            "observedQualityChecksum": "b" * 64,
            "expectedRevision": accepted_g1["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["detail"]["reason"] == "observed-quality-checksum-mismatch"
    assert _counts(store) == before_bad_checksum

    accepted = _accept_g2_without_reconstruction(
        client,
        image_id=target_image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=accepted_g1["revision"],
        expected_sequence=generation["nextSequence"],
    )
    assert accepted["imageRevision"] == accepted_g1["revision"] + 1
    assert accepted["nextSequence"] == generation["nextSequence"] + 1
    assert accepted["event"]["gate"] == "G2_reconstruction"
    assert accepted["event"]["state"] == "accepted"
    assert accepted["event"]["inputChecksum"] == quality_checksum
    assert accepted["event"]["outputChecksum"] == quality_checksum
    assert accepted["event"]["evidence"] == {
        "eventType": "reconstruction-decision",
        "imageRevision": accepted["imageRevision"],
        "qualityState": "accepted",
        "targetKind": "preprocessed",
        "reconstructionRequired": False,
    }

    before_stale_sequence = _counts(store)
    stale = client.patch(
        f"/api/images/{target_image['id']}/page-gates/reconstruction",
        json={
            "decision": "yes",
            "reason": "fine-lines-remain-insufficient",
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": accepted["imageRevision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["reason"] == "sequence-conflict"
    assert _counts(store) == before_stale_sequence

    blocked = client.patch(
        f"/api/images/{target_image['id']}/page-gates/reconstruction",
        json={
            "decision": "yes",
            "reason": "fine-lines-remain-insufficient",
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": accepted["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted["nextSequence"]),
        },
    )
    assert blocked.status_code == 200, blocked.text
    blocked_result = blocked.json()
    assert blocked_result["event"]["state"] == "blocked"
    assert blocked_result["event"]["decision"] == "further-reconstruction-yes"
    assert blocked_result["event"]["outputChecksum"] is None

    before_g3 = _counts(store)
    blocked_g3 = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "yes",
            "reason": "processable-text-visible",
            "evidence": ["original-and-quality-compared", "dialogue-visible"],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": blocked_result["imageRevision"],
            "lineage": _mutation_lineage(generation_id, blocked_result["nextSequence"]),
        },
    )
    assert blocked_g3.status_code == 409, blocked_g3.text
    assert blocked_g3.json()["detail"]["reason"] == "g2-not-accepted"
    assert _counts(store) == before_g3


def test_g3_text_presence_is_visual_checksum_bound_and_no_text_is_residual_free(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    quality_checksum, accepted_g1 = _accept_g1_preprocess(
        client,
        app,
        target_project=target_project,
        target_image=target_image,
        generation_id=generation_id,
    )
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted_g2 = _accept_g2_without_reconstruction(
        client,
        image_id=target_image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=accepted_g1["revision"],
        expected_sequence=generation["nextSequence"],
    )
    store = app.state.registry.get(target_project["id"])

    before_wrong_original = _counts(store)
    wrong_original = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "no",
            "reason": "no-processable-text-visible",
            "evidence": [
                "original-and-quality-compared",
                "no-processable-text-visible",
            ],
            "observedOriginalChecksum": "c" * 64,
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": accepted_g2["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted_g2["nextSequence"]),
        },
    )
    assert wrong_original.status_code == 409, wrong_original.text
    assert wrong_original.json()["detail"]["reason"] == ("observed-original-checksum-mismatch")
    assert _counts(store) == before_wrong_original

    with store.session() as session:
        session.add(
            TextRegion(
                image_id=target_image["id"],
                x=10,
                y=10,
                width=30,
                height=20,
                reading_order=0,
            )
        )
    before_residual = _counts(store)
    residual = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "no",
            "reason": "no-processable-text-visible",
            "evidence": [
                "original-and-quality-compared",
                "no-processable-text-visible",
            ],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": accepted_g2["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted_g2["nextSequence"]),
        },
    )
    assert residual.status_code == 409, residual.text
    assert residual.json()["detail"]["reason"] == "no-text-residuals:regions"
    assert _counts(store) == before_residual
    with store.session() as session:
        region = session.scalar(select(TextRegion).where(TextRegion.image_id == target_image["id"]))
        assert region is not None
        session.delete(region)

    uncertain = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "uncertain",
            "reason": "visual-evidence-uncertain",
            "evidence": ["original-and-quality-compared", "conflicting-signals"],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": accepted_g2["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted_g2["nextSequence"]),
        },
    )
    assert uncertain.status_code == 200, uncertain.text
    uncertain_result = uncertain.json()
    assert uncertain_result["event"]["state"] == "pending"
    assert uncertain_result["event"]["evidence"]["textPresence"] == "uncertain"

    present = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "yes",
            "reason": "processable-text-visible",
            "evidence": ["original-and-quality-compared", "dialogue-visible"],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": uncertain_result["imageRevision"],
            "lineage": _mutation_lineage(generation_id, uncertain_result["nextSequence"]),
        },
    )
    assert present.status_code == 200, present.text
    present_result = present.json()
    assert present_result["event"]["state"] == "accepted"
    assert present_result["event"]["decision"] == "text-present"

    no_text = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "no",
            "reason": "no-processable-text-visible",
            "evidence": [
                "original-and-quality-compared",
                "no-processable-text-visible",
            ],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": present_result["imageRevision"],
            "lineage": _mutation_lineage(generation_id, present_result["nextSequence"]),
        },
    )
    assert no_text.status_code == 200, no_text.text
    no_text_result = no_text.json()
    assert no_text_result["event"]["state"] == "accepted"
    assert no_text_result["event"]["decision"] == "no-text"
    assert no_text_result["event"]["outputChecksum"] == quality_checksum
    assert no_text_result["event"]["evidence"] == {
        "eventType": "text-presence-decision",
        "imageRevision": no_text_result["imageRevision"],
        "qualityState": "accepted",
        "targetKind": "preprocessed",
        "textPresence": "no",
        "visualComparison": True,
    }
    current = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    assert current["status"]["reviewState"] == "no-text-reviewed"
    assert all(
        current["status"][stage] == "pending"
        for stage in ("detection", "ocr", "translation", "inpaint", "typeset", "export")
    )
    assert current["regionCount"] == 0

    review_root = tmp_path / "lineaged-no-text-review"
    review_batch = client.post(
        "/api/final-review-batches",
        json={
            "name": "lineaged no-text",
            "outputPath": str(review_root),
            "sourceProjectIds": [target_project["id"]],
            "expectedItemCount": 1,
        },
    )
    assert review_batch.status_code == 201, review_batch.text
    batch = review_batch.json()
    final_item = batch["items"][0]
    assert final_item["formatVersion"] == 2
    assert final_item["strictEvidence"] is True
    assert final_item["currentArtifactStale"] is False
    assert final_item["evidence"]["mask"]["availability"] == "not-applicable"
    assert final_item["evidence"]["clean"]["availability"] == "not-applicable"
    assert final_item["evidence"]["final"]["checksum"] == quality_checksum
    assert final_item["evidence"]["final"]["terminalRevisionId"]

    blocked_g2 = client.patch(
        f"/api/images/{target_image['id']}/page-gates/reconstruction",
        json={
            "decision": "yes",
            "reason": "fine-lines-remain-insufficient",
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": no_text_result["imageRevision"],
            "lineage": _mutation_lineage(generation_id, no_text_result["nextSequence"]),
        },
    )
    assert blocked_g2.status_code == 200, blocked_g2.text
    blocked_g2_result = blocked_g2.json()
    assert blocked_g2_result["event"]["state"] == "blocked"
    after_block = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    assert after_block["status"]["reviewState"] == "pending"
    assert after_block["status"]["reviewedAt"] == ""

    stale_batch = client.get(f"/api/final-review-batches/{batch['id']}")
    assert stale_batch.status_code == 200, stale_batch.text
    assert stale_batch.json()["items"][0]["currentArtifactStale"] is True
    bypass = client.post(
        "/api/final-review-batches",
        json={
            "name": "blocked lineaged no-text",
            "outputPath": str(tmp_path / "blocked-lineaged-review"),
            "sourceProjectIds": [target_project["id"]],
            "expectedItemCount": 1,
        },
    )
    assert bypass.status_code == 409, bypass.text
    assert bypass.json()["detail"]["reason"] == "g2-not-accepted"


def test_detect_enqueue_requires_current_g3_yes_and_is_zero_write_when_blocked(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    quality_checksum, accepted_g1 = _accept_g1_preprocess(
        client,
        app,
        target_project=target_project,
        target_image=target_image,
        generation_id=generation_id,
    )
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted_g2 = _accept_g2_without_reconstruction(
        client,
        image_id=target_image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=accepted_g1["revision"],
        expected_sequence=generation["nextSequence"],
    )
    store = app.state.registry.get(target_project["id"])

    def state() -> tuple[int, int, int, int]:
        with store.session() as session:
            image = session.get(ImageAsset, target_image["id"])
            assert image is not None
            return (
                image.revision,
                session.scalar(select(func.count()).select_from(Job)) or 0,
                session.scalar(select(func.count()).select_from(TextRegion)) or 0,
                session.scalar(select(func.count()).select_from(PageLineageEvent)) or 0,
            )

    def enqueue() -> object:
        return client.post(
            f"/api/projects/{target_project['id']}/detect",
            json={
                "imageIds": [target_image["id"]],
                "lineage": _current_lineage_context(client, target_image["id"], generation_id),
            },
        )

    before_missing = state()
    missing = enqueue()
    assert missing.status_code == 409, missing.text
    assert missing.json()["detail"]["reason"] == "g3-text-present-not-current"
    assert state() == before_missing

    uncertain = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "uncertain",
            "reason": "visual-evidence-uncertain",
            "evidence": ["original-and-quality-compared", "conflicting-signals"],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": accepted_g2["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted_g2["nextSequence"]),
        },
    )
    assert uncertain.status_code == 200, uncertain.text
    before_uncertain = state()
    blocked_uncertain = enqueue()
    assert blocked_uncertain.status_code == 409, blocked_uncertain.text
    assert blocked_uncertain.json()["detail"]["reason"] == "g3-text-present-not-current"
    assert state() == before_uncertain

    no_text = client.patch(
        f"/api/images/{target_image['id']}/page-gates/text-presence",
        json={
            "decision": "no",
            "reason": "no-processable-text-visible",
            "evidence": [
                "original-and-quality-compared",
                "no-processable-text-visible",
            ],
            "observedOriginalChecksum": _checksum(data),
            "observedQualityChecksum": quality_checksum,
            "expectedRevision": uncertain.json()["imageRevision"],
            "lineage": _mutation_lineage(
                generation_id,
                uncertain.json()["nextSequence"],
            ),
        },
    )
    assert no_text.status_code == 200, no_text.text
    before_no = state()
    blocked_no = enqueue()
    assert blocked_no.status_code == 409, blocked_no.text
    assert blocked_no.json()["detail"]["reason"] == "g3-text-present-not-current"
    assert state() == before_no

    present = _accept_g3_text_present(
        client,
        source_checksum=_checksum(data),
        image_id=target_image["id"],
        generation_id=generation_id,
        quality_checksum=quality_checksum,
        image_revision=no_text.json()["imageRevision"],
        expected_sequence=no_text.json()["nextSequence"],
    )
    queued = enqueue()
    assert queued.status_code == 202, queued.text
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert events[-1]["operation"] == "detect-job-enqueued"
    assert events[-1]["inputChecksum"] == quality_checksum
    assert events[-1]["parentChecksum"] == quality_checksum
    assert events[-1]["sequence"] == present["nextSequence"]


def test_detect_enqueue_rejects_a_replayed_sequence_without_any_partial_write(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)

    lineage = _current_lineage_context(client, target_image["id"], generation_id)
    pages = lineage["pages"]
    assert isinstance(pages, list) and isinstance(pages[0], dict)
    expected_sequence = pages[0]["expectedSequence"]
    assert isinstance(expected_sequence, int)
    payload = {"imageIds": [target_image["id"]], "lineage": lineage}
    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json=payload,
    )
    assert queued.status_code == 202, queued.text

    def snapshot() -> tuple[object, ...]:
        with store.session() as session:
            generation = session.get(PageGeneration, generation_id)
            image = session.get(ImageAsset, target_image["id"])
            project = session.get(Project, target_project["id"])
            assert generation is not None and image is not None and project is not None
            return (
                generation.next_sequence,
                image.revision,
                project.revision,
                session.scalar(select(func.count()).select_from(Job)),
                session.scalar(select(func.count()).select_from(JobItem)),
                session.scalar(select(func.count()).select_from(PageLineageEvent)),
                session.scalar(select(func.count()).select_from(Revision)),
            )

    after_first = snapshot()
    replayed = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json=payload,
    )
    assert replayed.status_code == 409, replayed.text
    assert replayed.json()["detail"] == {
        "message": "Page lineage changed after the job was prepared",
        "resource": f"page-generation:{generation_id}",
        "reason": "sequence-conflict",
        "expectedSequence": expected_sequence,
        "actualSequence": expected_sequence + 1,
    }
    assert snapshot() == after_first


def test_detect_runtime_provider_must_match_enqueued_provider(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    detector = _LineageDetector()
    detector.name = "different-provider"
    app.state.providers.ocr = detector
    before_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]

    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))

    failed = app.state.queue.get_job(store, queued.json()["id"])
    assert failed.status == "failed"
    assert failed.items[0].status == "failed"
    assert "provider does not match" in (failed.items[0].error or "")
    assert client.get(f"/api/images/{target_image['id']}/regions").json() == []
    after_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    assert after_image["revision"] == before_image["revision"]
    assert after_image["status"] == before_image["status"]
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event for event in events if event["operation"] == "detect-regions-produced"] == []
    assert events[-1]["operation"] == "detect-job-failed"
    assert events[-1]["provider"] == "tesseract"


def test_detect_publication_completion_region_mutation_and_g4_acceptance_are_bound(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _LineageDetector()

    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    claimed = app.state.queue._claim_next()
    assert claimed == (store, job["id"])
    item_id = job["items"][0]["id"]
    assert app.state.queue._begin_item(store, job["id"], item_id)
    output = app.state.queue._process_item(store, job["id"], item_id)

    regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    assert len(regions) == 1
    region = regions[0]
    assert region["detectorJobItemId"] == item_id
    assert region["detectorCandidateIndex"] == 0
    assert region["paragraphGroupId"] is None
    assert region["rubyParentId"] is None
    assert region["contentDisposition"] is None
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    produced = events[-1]
    assert produced["operation"] == "detect-regions-produced"
    assert produced["state"] == "pending"
    assert produced["jobItemId"] == item_id
    assert produced["inputChecksum"] == prepared["qualityChecksum"]
    assert produced["parentChecksum"] == prepared["qualityChecksum"]
    assert produced["evidence"]["regionCount"] == 1
    with store.session() as session:
        assert produced["outputChecksum"] == g4_region_state_checksum(session, target_image["id"])

    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    premature = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": produced["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert premature.status_code == 409, premature.text
    assert premature.json()["detail"]["reason"] == "producer-not-completed"

    app.state.queue._finish_item(store, job["id"], item_id, output=output)
    completed = app.state.queue.get_job(store, job["id"])
    assert completed.status == "completed"
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert events[-1]["operation"] == "detect-job-completed"
    assert events[-1]["outputChecksum"] == produced["outputChecksum"]
    assert events[-1]["provider"] == produced["provider"] == "tesseract"
    assert events[-1]["evidence"]["targetKind"] == "region-set"

    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_missing_image_revision = _counts(store)
    missing_image_revision = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "contentDisposition": "translate",
            "expectedRevision": region["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert missing_image_revision.status_code == 409, missing_image_revision.text
    assert missing_image_revision.json()["detail"]["reason"] == "image-revision-required"
    assert _counts(store) == before_missing_image_revision

    stale_sequence = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "contentDisposition": "translate",
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"] - 1),
        },
    )
    assert stale_sequence.status_code == 409, stale_sequence.text
    assert stale_sequence.json()["detail"]["reason"] == "sequence-conflict"
    assert _counts(store) == before_missing_image_revision

    blocked_create = client.post(
        f"/api/images/{target_image['id']}/regions",
        json={
            "x": 5,
            "y": 5,
            "width": 20,
            "height": 20,
            "type": "dialogue",
            "direction": "vertical",
            "order": 1,
            "paragraphGroupId": "paragraph-blocked",
            "contentDisposition": "translate",
            "style": {"fontSize": 99},
            "repair": {"maskPadding": 99},
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert blocked_create.status_code == 409, blocked_create.text
    assert blocked_create.json()["detail"]["reason"] == "g4-field-not-supported"
    assert _counts(store) == before_missing_image_revision
    assert len(client.get(f"/api/images/{target_image['id']}/regions").json()) == 1

    blocked_delete = client.request(
        "DELETE",
        f"/api/regions/{region['id']}",
        json={
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert blocked_delete.status_code == 409, blocked_delete.text
    assert blocked_delete.json()["detail"]["reason"] == ("detector-candidate-disposition-required")
    assert _counts(store) == before_missing_image_revision
    assert len(client.get(f"/api/images/{target_image['id']}/regions").json()) == 1

    decided = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "x": 25,
            "type": "dialogue",
            "direction": "horizontal",
            "order": 0,
            "paragraphGroupId": "paragraph-1",
            "contentDisposition": "translate",
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert decided.status_code == 200, decided.text
    decided_region = decided.json()
    assert decided_region["x"] == 25
    assert decided_region["direction"] == "horizontal"
    assert decided_region["contentDisposition"] == "translate"
    assert decided_region["paragraphGroupId"] == "paragraph-1"
    mutation_event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
    assert mutation_event["operation"] == "regions-updated"
    assert mutation_event["state"] == "pending"
    assert mutation_event["inputChecksum"] == produced["outputChecksum"]

    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": mutation_event["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["event"]["operation"] == "regions-stage-review"
    assert result["event"]["gate"] == "G4_regions"
    assert result["event"]["state"] == "accepted"
    assert result["event"]["inputChecksum"] == prepared["qualityChecksum"]
    assert result["event"]["outputChecksum"] == mutation_event["outputChecksum"]
    assert result["event"]["evidence"] == {
        "eventType": "regions-stage-review",
        "imageRevision": result["imageRevision"],
        "qualityState": "accepted",
        "regionCount": 1,
        "targetKind": "region-set",
    }

    before_blocked = _counts(store)
    blocked_later_stage = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "sourceText": "未到 G6",
            "expectedRevision": decided_region["revision"],
            "expectedImageRevision": result["imageRevision"],
            "lineage": _mutation_lineage(generation_id, result["nextSequence"]),
        },
    )
    assert blocked_later_stage.status_code == 409, blocked_later_stage.text
    assert blocked_later_stage.json()["detail"]["reason"] == "g4-field-not-supported"
    assert _counts(store) == before_blocked


def test_detect_crash_recovery_republishes_latest_item_without_duplicate_candidates(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    detector = _LineageDetector()
    app.state.providers.ocr = detector
    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    item_id = job["items"][0]["id"]
    claimed = app.state.queue._claim_next()
    assert claimed == (store, job["id"])
    assert app.state.queue._begin_item(store, job["id"], item_id)
    app.state.queue._process_item(store, job["id"], item_id)
    first_regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    assert len(first_regions) == 1
    first_id = first_regions[0]["id"]
    assert store.recover_jobs() == 1

    reclaimed = app.state.queue._claim_next()
    assert reclaimed == (store, job["id"])
    asyncio.run(app.state.queue._execute(*reclaimed))
    completed = app.state.queue.get_job(store, job["id"])
    assert completed.status == "completed"
    assert detector.calls == 2
    final_regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    assert len(final_regions) == 1
    assert final_regions[0]["id"] != first_id
    assert final_regions[0]["detectorJobItemId"] == item_id
    assert final_regions[0]["detectorCandidateIndex"] == 0
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(TextRegion)) == 1

    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    produced = [event for event in events if event["operation"] == "detect-regions-produced"]
    completed_events = [event for event in events if event["operation"] == "detect-job-completed"]
    assert len(produced) == 2
    assert len(completed_events) == 1
    assert completed_events[0]["sequence"] > produced[-1]["sequence"]
    assert completed_events[0]["jobItemId"] == produced[-1]["jobItemId"] == item_id
    assert completed_events[0]["outputChecksum"] == produced[-1]["outputChecksum"]


def test_detect_region_publication_rolls_back_when_lineage_append_fails(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _LineageDetector()
    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    item_id = job["items"][0]["id"]
    claimed = app.state.queue._claim_next()
    assert claimed == (store, job["id"])
    assert app.state.queue._begin_item(store, job["id"], item_id)
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        assert image is not None
        before = (
            image.revision,
            dict(image.status),
            session.scalar(select(func.count()).select_from(TextRegion)),
            session.scalar(select(func.count()).select_from(Revision)),
            session.scalar(select(func.count()).select_from(PageLineageEvent)),
        )

    original_append = page_lineage._append_event

    def fail_detect_append(*args, **kwargs):
        if kwargs.get("operation") == "detect-regions-produced":
            raise RuntimeError("injected lineage append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(page_lineage, "_append_event", fail_detect_append)
    with pytest.raises(RuntimeError, match="injected lineage append failure"):
        app.state.queue._process_item(store, job["id"], item_id)

    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        assert image is not None
        after = (
            image.revision,
            dict(image.status),
            session.scalar(select(func.count()).select_from(TextRegion)),
            session.scalar(select(func.count()).select_from(Revision)),
            session.scalar(select(func.count()).select_from(PageLineageEvent)),
        )
    assert after == before


def test_g4_acceptance_rolls_back_when_lineage_append_fails(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _LineageDetector()
    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))

    region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    decided = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "type": "dialogue",
            "direction": "vertical",
            "paragraphGroupId": "paragraph-1",
            "contentDisposition": "translate",
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert decided.status_code == 200, decided.text
    mutation_event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before = _counts(store)

    original_append = page_lineage._append_event

    def fail_acceptance_append(*args, **kwargs):
        if kwargs.get("operation") == "regions-stage-review":
            raise RuntimeError("injected G4 acceptance append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(page_lineage, "_append_event", fail_acceptance_append)
    with pytest.raises(RuntimeError, match="injected G4 acceptance append failure"):
        client.patch(
            f"/api/images/{target_image['id']}/page-gates/regions",
            json={
                "decision": "accept",
                "reason": "all-region-decisions-reviewed",
                "observedRegionChecksum": mutation_event["outputChecksum"],
                "expectedRevision": current_image["revision"],
                "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
            },
        )
    assert _counts(store) == before
    assert all(
        event["operation"] != "regions-stage-review"
        for event in client.get(f"/api/page-generations/{generation_id}/events").json()
    )


def test_strict_g4_reorder_appends_a_continuous_region_event(
    client: TestClient,
    app,
    tmp_path: Path,
) -> None:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("false-positive",),
        accept_g4=False,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)

    before_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    before_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_image = _project_image(client, target_project["id"], target_image["id"])
    before_regions = sorted(prepared["regions"], key=lambda region: region["order"])
    requested_ids = [region["id"] for region in reversed(before_regions)]

    response = client.post(
        f"/api/images/{target_image['id']}/regions/reorder",
        json={
            "regionIds": requested_ids,
            "expectedImageRevision": before_image["revision"],
            "lineage": _mutation_lineage(
                generation_id,
                before_generation["nextSequence"],
            ),
        },
    )
    assert response.status_code == 200, response.text
    assert [region["id"] for region in response.json()] == requested_ids
    assert [region["order"] for region in response.json()] == [0, 1]

    after_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    after_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    assert len(after_events) == len(before_events) + 1
    assert after_generation["nextSequence"] == before_generation["nextSequence"] + 1
    event = after_events[-1]
    assert event["operation"] == "regions-reordered"
    assert event["sequence"] == before_generation["nextSequence"]
    assert event["inputChecksum"] == before_events[-1]["outputChecksum"]
    assert event["evidence"]["regionCount"] == 2
    assert event["revisionId"]
    with store.session() as session:
        assert event["outputChecksum"] == g4_region_state_checksum(
            session,
            target_image["id"],
        )
        revision = session.get(Revision, event["revisionId"])
        assert revision is not None
        assert revision.operation == "reorder"
        assert revision.entity_id == requested_ids[-1]


def test_strict_g4_reorder_rolls_back_when_lineage_append_fails(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("false-positive",),
        accept_g4=False,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)

    before_regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    before_image = _project_image(client, target_project["id"], target_image["id"])
    before_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    before_counts = _counts(store)
    requested_ids = [
        region["id"]
        for region in reversed(sorted(before_regions, key=lambda region: region["order"]))
    ]
    original_append = page_lineage._append_event

    def fail_reorder_append(*args, **kwargs):
        if kwargs.get("operation") == "regions-reordered":
            raise RuntimeError("injected G4 reorder append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(page_lineage, "_append_event", fail_reorder_append)
    with pytest.raises(RuntimeError, match="injected G4 reorder append failure"):
        client.post(
            f"/api/images/{target_image['id']}/regions/reorder",
            json={
                "regionIds": requested_ids,
                "expectedImageRevision": before_image["revision"],
                "lineage": _mutation_lineage(
                    generation_id,
                    before_generation["nextSequence"],
                ),
            },
        )

    assert client.get(f"/api/images/{target_image['id']}/regions").json() == before_regions
    assert _project_image(client, target_project["id"], target_image["id"]) == before_image
    assert client.get(f"/api/page-generations/{generation_id}/events").json() == before_events
    assert client.get(f"/api/images/{target_image['id']}/page-generations").json()[0] == (
        before_generation
    )
    assert _counts(store) == before_counts


def test_strict_g4_reorder_recovers_the_known_committed_revision_residue(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("false-positive",),
        accept_g4=False,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    before_regions = sorted(prepared["regions"], key=lambda region: region["order"])
    requested_ids = [region["id"] for region in reversed(before_regions)]
    before_image = _project_image(client, target_project["id"], target_image["id"])
    before_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_events = client.get(f"/api/page-generations/{generation_id}/events").json()

    with monkeypatch.context() as residue_patch:
        residue_patch.setattr(
            regions_module,
            "record_g4_region_mutation",
            lambda *args, **kwargs: None,
        )
        committed_without_event = client.post(
            f"/api/images/{target_image['id']}/regions/reorder",
            json={
                "regionIds": requested_ids,
                "expectedImageRevision": before_image["revision"],
                "lineage": _mutation_lineage(
                    generation_id,
                    before_generation["nextSequence"],
                ),
            },
        )
    assert committed_without_event.status_code == 200, committed_without_event.text
    residue_image = _project_image(client, target_project["id"], target_image["id"])
    residue_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    assert residue_image["revision"] == before_image["revision"] + 1
    assert residue_generation["nextSequence"] == before_generation["nextSequence"]
    assert client.get(f"/api/page-generations/{generation_id}/events").json() == before_events

    recovered = client.post(
        f"/api/images/{target_image['id']}/regions/reorder",
        json={
            "regionIds": requested_ids,
            "expectedImageRevision": residue_image["revision"],
            "lineage": _mutation_lineage(
                generation_id,
                residue_generation["nextSequence"],
            ),
        },
    )
    assert recovered.status_code == 200, recovered.text
    recovered_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    recovery_event = recovered_events[-1]
    assert len(recovered_events) == len(before_events) + 1
    assert recovery_event["operation"] == "regions-reordered"
    assert recovery_event["inputChecksum"] == before_events[-1]["outputChecksum"]
    assert recovery_event["evidence"]["recovered"] is True
    assert recovery_event["evidence"]["recoveryKind"] == ("committed-reorder-missing-event")
    assert len(recovery_event["evidence"]["revisionIds"]) == 2
    assert len(recovery_event["evidence"]["revisionWitnessChecksum"]) == 64

    recovered_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[
        0
    ]
    repeated = client.post(
        f"/api/images/{target_image['id']}/regions/reorder",
        json={
            "regionIds": requested_ids,
            "expectedImageRevision": residue_image["revision"],
            "lineage": _mutation_lineage(
                generation_id,
                recovered_generation["nextSequence"],
            ),
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert client.get(f"/api/page-generations/{generation_id}/events").json() == (recovered_events)

    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": recovery_event["outputChecksum"],
            "expectedRevision": residue_image["revision"],
            "lineage": _mutation_lineage(
                generation_id,
                recovered_generation["nextSequence"],
            ),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["event"]["state"] == "accepted"


def test_strict_g4_reorder_recovery_rejects_an_unrelated_revision_suffix(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("false-positive",),
        accept_g4=False,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    requested_ids = [
        region["id"]
        for region in reversed(sorted(prepared["regions"], key=lambda region: region["order"]))
    ]
    before_image = _project_image(client, target_project["id"], target_image["id"])
    before_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    with monkeypatch.context() as residue_patch:
        residue_patch.setattr(
            regions_module,
            "record_g4_region_mutation",
            lambda *args, **kwargs: None,
        )
        committed_without_event = client.post(
            f"/api/images/{target_image['id']}/regions/reorder",
            json={
                "regionIds": requested_ids,
                "expectedImageRevision": before_image["revision"],
                "lineage": _mutation_lineage(
                    generation_id,
                    before_generation["nextSequence"],
                ),
            },
        )
    assert committed_without_event.status_code == 200, committed_without_event.text

    with store.session() as session:
        project = store.project(session)
        add_revision(
            session,
            project,
            entity_type="project",
            entity_id=project.id,
            operation="update",
            before={"name": project.name},
            after={"name": project.name},
        )
    residue_image = _project_image(client, target_project["id"], target_image["id"])
    residue_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    blocked = client.post(
        f"/api/images/{target_image['id']}/regions/reorder",
        json={
            "regionIds": requested_ids,
            "expectedImageRevision": residue_image["revision"],
            "lineage": _mutation_lineage(
                generation_id,
                residue_generation["nextSequence"],
            ),
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == ("g4-reorder-recovery-revision-suffix-invalid")
    assert client.get(f"/api/page-generations/{generation_id}/events").json() == before_events


def test_empty_detect_requires_manual_g4_regions_and_never_changes_g3_to_no_text(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _LineageDetector([])
    queued = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert client.get(f"/api/images/{target_image['id']}/regions").json() == []
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert events[-1]["operation"] == "detect-job-completed"
    empty_checksum = events[-1]["outputChecksum"]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    assert current_image["status"]["reviewState"] == "pending"
    assert current_image["status"]["detection"] == "done"
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    rejected_empty = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": empty_checksum,
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert rejected_empty.status_code == 409, rejected_empty.text
    assert rejected_empty.json()["detail"]["reason"] == "g4-regions-invalid:regions-empty"

    created = client.post(
        f"/api/images/{target_image['id']}/regions",
        json={
            "x": 20,
            "y": 30,
            "width": 80,
            "height": 40,
            "type": "dialogue",
            "direction": "vertical",
            "order": 0,
            "paragraphGroupId": "manual-paragraph-1",
            "contentDisposition": "translate",
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert created.status_code == 201, created.text
    mutation_event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
    assert mutation_event["operation"] == "regions-created"
    assert mutation_event["inputChecksum"] == empty_checksum
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": mutation_event["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    lineage = client.get(f"/api/page-generations/{generation_id}/events").json()
    g3_events = [event for event in lineage if event["operation"] == "text-presence-decision"]
    assert g3_events[-1]["decision"] == "text-present"


def test_false_positive_survives_redetection_but_cannot_be_the_only_g4_region(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g3_yes_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _LineageDetector()

    first = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert first.status_code == 202, first.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, first.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))

    region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    original_region_id = region["id"]
    original_item_id = region["detectorJobItemId"]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    decided = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "contentDisposition": "false-positive",
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["type"] == "unknown"
    assert decided.json()["paragraphGroupId"] is None

    second = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert second.status_code == 202, second.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, second.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))

    regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    assert len(regions) == 1
    assert regions[0]["id"] == original_region_id
    assert regions[0]["detectorJobItemId"] == original_item_id
    assert regions[0]["contentDisposition"] == "false-positive"

    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    produced = [event for event in events if event["operation"] == "detect-regions-produced"][-1]
    rejected = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": produced["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["reason"] == ("g4-regions-invalid:processable-region-missing")

    created = client.post(
        f"/api/images/{target_image['id']}/regions",
        json={
            "x": 100,
            "y": 100,
            "width": 30,
            "height": 30,
            "type": "dialogue",
            "direction": "vertical",
            "order": 1,
            "paragraphGroupId": "manual-paragraph-1",
            "contentDisposition": "translate",
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert created.status_code == 201, created.text
    mutation_event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": mutation_event["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_g4_checksum_excludes_later_text_and_render_fields_but_tracks_semantics(
    client: TestClient, app, tmp_path: Path
) -> None:
    project = create_project(client, tmp_path / "checksum-project", "checksum")
    image = upload_image(client, project["id"])
    store = app.state.registry.get(project["id"])
    with store.session() as session:
        row = TextRegion(
            image_id=image["id"],
            x=10,
            y=20,
            width=80,
            height=40,
            region_type="dialogue",
            direction="vertical",
            reading_order=0,
            paragraph_group_id="paragraph-1",
            content_disposition="translate",
            revision=1,
        )
        session.add(row)
        session.flush()
        baseline = g4_region_state_checksum(session, image["id"])
        row.source_text = "原文"
        row.translation_text = "译文"
        row.confidence = 0.75
        row.recognition = {"private": "evidence"}
        row.style = {"fontSize": 24}
        row.repair = {"maskPadding": 8}
        row.revision += 1
        session.flush()
        assert g4_region_state_checksum(session, image["id"]) == baseline
        row.direction = "horizontal"
        session.flush()
        assert g4_region_state_checksum(session, image["id"]) != baseline


def test_g4_validation_rejects_ruby_parented_to_a_false_positive() -> None:
    image = ImageAsset(id="image-g4-validation", width=240, height=320)
    rows = [
        TextRegion(
            id="false-positive-parent",
            image_id=image.id,
            x=10,
            y=10,
            width=60,
            height=40,
            rotation=0,
            region_type="dialogue",
            direction="vertical",
            reading_order=0,
            paragraph_group_id="paragraph-noise",
            content_disposition="false-positive",
        ),
        TextRegion(
            id="invalid-ruby",
            image_id=image.id,
            x=70,
            y=10,
            width=20,
            height=20,
            rotation=0,
            region_type="ruby",
            direction="vertical",
            reading_order=1,
            paragraph_group_id="paragraph-noise",
            ruby_parent_id="false-positive-parent",
            content_disposition="ignore",
        ),
        TextRegion(
            id="real-text",
            image_id=image.id,
            x=100,
            y=10,
            width=60,
            height=40,
            rotation=0,
            region_type="dialogue",
            direction="vertical",
            reading_order=2,
            paragraph_group_id="paragraph-real",
            content_disposition="translate",
        ),
    ]
    assert page_lineage._g4_validation_issues(image, rows) == ["ruby-parent-false-positive"]


def test_worker_rechecks_generation_before_mutation_and_fails_closed(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    queued = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={
            "imageIds": [target_image["id"]],
            "options": {"profile": "off"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    store = app.state.registry.get(target_project["id"])
    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        assert generation is not None
        generation.state = "superseded"
        generation.closed_at = generation.created_at

    claimed = app.state.queue._claim_next()
    assert claimed is not None
    asyncio.run(app.state.queue._execute(*claimed))
    failed = app.state.queue.get_job(store, queued.json()["id"])
    assert failed.status == "failed"
    assert failed.items[0].status == "failed"
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        assert image is not None
        assert image.revision == 2
        assert image.status["preprocess"] == "pending"
        assert image.processing_errors == []
    assert not (
        Path(target_project["rootPath"]) / "generated/preprocessed/chapter/page-001.png"
    ).exists()
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[-1]["operation"] == "preprocess-job-failed"
    assert events[-1]["state"] == "blocked"
    assert events[-1]["outputChecksum"] is None


def test_recovery_keeps_lineage_and_does_not_duplicate_enqueue_evidence(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    generation_id = str(uuid.uuid4())
    created = client.post(
        f"/api/images/{target_image['id']}/page-generations",
        json=_generation_body(
            source_project_id=source_project["id"],
            source_image_id=source_image["id"],
            checksum=_checksum(data),
            generation_id=generation_id,
        ),
    )
    assert created.status_code == 201, created.text
    queued = client.post(
        f"/api/projects/{target_project['id']}/preprocess",
        json={
            "imageIds": [target_image["id"]],
            "options": {"profile": "off"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    store = app.state.registry.get(target_project["id"])
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    item_id = queued.json()["items"][0]["id"]
    assert app.state.queue._begin_item(store, queued.json()["id"], item_id)
    app.state.queue._process_item(store, queued.json()["id"], item_id)
    before_recovery = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["operation"] for event in before_recovery] == [
        "generation-created",
        "preprocess-job-enqueued",
        "preprocess-artifact-produced",
    ]

    assert store.recover_jobs() == 1
    recovered = app.state.queue.get_job(store, queued.json()["id"])
    assert recovered.status == "queued"
    assert recovered.items[0].status == "queued"
    with store.session() as session:
        persisted = session.get(Job, queued.json()["id"])
        assert persisted is not None
        assert persisted.lineage_context == {
            "version": 1,
            "runId": _RUN_ID,
            "actor": _ACTOR | {"actorId": None},
            "pages": [
                {
                    "imageId": target_image["id"],
                    "pageGenerationId": generation_id,
                    "expectedSequence": 2,
                }
            ],
        }
    assert client.get(f"/api/page-generations/{generation_id}/events").json() == before_recovery

    reclaimed = app.state.queue._claim_next()
    assert reclaimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*reclaimed))
    completed = app.state.queue.get_job(store, queued.json()["id"])
    assert completed.status == "completed"
    final_events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["operation"] for event in final_events] == [
        "generation-created",
        "preprocess-job-enqueued",
        "preprocess-artifact-produced",
        "preprocess-artifact-produced",
        "preprocess-job-completed",
    ]


def test_g5_background_classification_chain_acceptance_and_locks(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g4_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_project, dict)
    assert isinstance(target_image, dict)
    assert isinstance(region, dict)

    context_response = client.get(f"/api/images/{target_image['id']}/page-gates/background")
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert context["state"] == "pending"
    assert context["eligibleRegionIds"] == [region["id"]]
    assert context["classifiedRegionIds"] == []

    cases = [
        ("white-solid", "uniform-near-white", 1.0),
        ("black-solid", "uniform-near-black", 0.8),
        ("other-solid", "uniform-other-color", 0.6),
        ("simple-gradient", "smooth-gradient-continuity", 0.4),
        ("screentone", "periodic-screentone", 0.2),
        ("complex-lineart", "structural-lines-cross-region", 0.01),
        ("illustration/character", "character-or-illustration-detail", 0.0),
    ]
    for category, rationale, confidence in cases:
        current_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
        before_checksum = context["backgroundChecksum"]
        classified = client.patch(
            f"/api/regions/{region['id']}/background-classification",
            json={
                "category": category,
                "confidence": confidence,
                "rationaleCodes": [rationale, "mixed-visual-signals"],
                "expectedRevision": current_region["revision"],
                "expectedImageRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
        assert classified.status_code == 200, classified.text
        classified_region = classified.json()
        assert classified_region["backgroundCategory"] == category
        assert classified_region["backgroundConfidence"] == confidence
        assert classified_region["backgroundRationaleCodes"] == sorted(
            [rationale, "mixed-visual-signals"]
        )
        assert classified_region["backgroundReviewer"] == _ACTOR | {"actorId": None}
        assert classified_region["backgroundGenerationId"] == generation_id
        event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
        assert event["operation"] == "background-classification-reviewed"
        assert event["gate"] == "G5_background"
        assert event["state"] == "pending"
        assert event["inputChecksum"] == before_checksum
        assert event["parentChecksum"] == context["g4Checksum"]
        assert event["actor"] == classified_region["backgroundReviewer"]
        assert event["evidence"]["targetRegionId"] == region["id"]
        assert event["evidence"]["eligibleRegionCount"] == 1
        assert event["evidence"]["classifiedRegionCount"] == 1
        context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
        assert context["classifiedRegionIds"] == [region["id"]]
        assert context["backgroundChecksum"] == event["outputChecksum"]

    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": "all-eligible-backgrounds-reviewed",
            "observedBackgroundChecksum": context["backgroundChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    accepted_result = accepted.json()
    assert accepted_result["event"]["operation"] == "background-stage-review"
    assert accepted_result["event"]["gate"] == "G5_background"
    assert accepted_result["event"]["state"] == "accepted"
    assert accepted_result["event"]["decision"] == "backgrounds-accepted"
    assert accepted_result["event"]["inputChecksum"] == context["backgroundChecksum"]
    assert accepted_result["event"]["outputChecksum"] == context["backgroundChecksum"]
    assert accepted_result["event"]["parentChecksum"] == context["g4Checksum"]
    accepted_context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    assert accepted_context["state"] == "accepted"
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image is not None and generation is not None
        checksum, terminal = require_current_background_classifications(
            store, session, image, generation
        )
        assert checksum == context["backgroundChecksum"]
        assert terminal.id == accepted_result["event"]["id"]

    before_locked = _counts(store)
    current_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    reclassified = client.patch(
        f"/api/regions/{region['id']}/background-classification",
        json={
            "category": "white-solid",
            "confidence": 0.5,
            "rationaleCodes": ["uniform-near-white"],
            "expectedRevision": current_region["revision"],
            "expectedImageRevision": accepted_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted_context["nextSequence"]),
        },
    )
    assert reclassified.status_code == 409, reclassified.text
    assert reclassified.json()["detail"]["reason"] == "g5-backgrounds-accepted"
    assert _counts(store) == before_locked

    g4_mutation = client.patch(
        f"/api/regions/{region['id']}",
        json={
            "x": current_region["x"] + 1,
            "expectedRevision": current_region["revision"],
            "expectedImageRevision": accepted_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, accepted_context["nextSequence"]),
        },
    )
    assert g4_mutation.status_code == 409, g4_mutation.text
    assert g4_mutation.json()["detail"]["reason"] == "g5-started-g4-locked"
    assert _counts(store) == before_locked

    detect = client.post(
        f"/api/projects/{target_project['id']}/detect",
        json={
            "imageIds": [target_image["id"]],
            "lineage": _lineage_context(
                target_image["id"], generation_id, accepted_context["nextSequence"]
            ),
        },
    )
    assert detect.status_code == 409, detect.text
    assert detect.json()["detail"]["reason"] == "g5-started-g4-locked"
    assert _counts(store) == before_locked


def test_g5_rejects_invalid_evidence_and_stale_cas_without_writes(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g4_accepted_page(client, app, tmp_path)
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_image, dict) and isinstance(region, dict)
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    valid = {
        "category": "white-solid",
        "confidence": 0.5,
        "rationaleCodes": ["uniform-near-white"],
        "expectedRevision": region["revision"],
        "expectedImageRevision": context["imageRevision"],
        "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
    }
    baseline_counts = _counts(store)
    baseline_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    invalid_payloads = [
        valid | {"confidence": True},
        valid | {"confidence": -0.01},
        valid | {"confidence": 1.01},
        valid | {"rationaleCodes": []},
        valid | {"rationaleCodes": ["uniform-near-white", "uniform-near-white"]},
        valid | {"rationaleCodes": ["uniform-near-black"]},
        valid | {"rationaleCodes": ["not-controlled"]},
        valid | {"reviewer": _ACTOR},
        valid | {"backgroundGenerationId": generation_id},
    ]
    for payload in invalid_payloads:
        response = client.patch(
            f"/api/regions/{region['id']}/background-classification",
            json=payload,
        )
        assert response.status_code == 422, response.text
        assert _counts(store) == baseline_counts
        assert client.get(f"/api/images/{target_image['id']}/regions").json()[0] == (
            baseline_region
        )

    non_finite_payload = valid | {"confidence": float("nan")}
    non_finite = client.patch(
        f"/api/regions/{region['id']}/background-classification",
        content=json.dumps(non_finite_payload),
        headers={"content-type": "application/json"},
    )
    assert non_finite.status_code == 422, non_finite.text
    assert _counts(store) == baseline_counts

    stale_payloads = [
        valid | {"expectedRevision": region["revision"] - 1},
        valid | {"expectedImageRevision": context["imageRevision"] - 1},
        valid
        | {
            "lineage": _mutation_lineage(
                generation_id,
                context["nextSequence"] - 1,
            )
        },
    ]
    expected_reasons = [None, None, "sequence-conflict"]
    for payload, expected_reason in zip(stale_payloads, expected_reasons, strict=True):
        response = client.patch(
            f"/api/regions/{region['id']}/background-classification",
            json=payload,
        )
        assert response.status_code == 409, response.text
        if expected_reason is not None:
            assert response.json()["detail"]["reason"] == expected_reason
        assert _counts(store) == baseline_counts
        assert client.get(f"/api/images/{target_image['id']}/regions").json()[0] == (
            baseline_region
        )


def test_g5_requires_every_eligible_region_before_acceptance(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("redraw-art",),
    )
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    regions = prepared["regions"]
    store = prepared["store"]
    assert isinstance(target_image, dict) and isinstance(regions, list)
    assert len(regions) == 2
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    assert set(context["eligibleRegionIds"]) == {row["id"] for row in regions}

    first = regions[0]
    classified = client.patch(
        f"/api/regions/{first['id']}/background-classification",
        json={
            "category": "simple-gradient",
            "confidence": 0.1,
            "rationaleCodes": ["smooth-gradient-continuity"],
            "expectedRevision": first["revision"],
            "expectedImageRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert classified.status_code == 200, classified.text
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    before_incomplete = _counts(store)
    incomplete = client.patch(
        f"/api/images/{target_image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": "all-eligible-backgrounds-reviewed",
            "observedBackgroundChecksum": context["backgroundChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert incomplete.status_code == 409, incomplete.text
    assert "background-classification-missing" in incomplete.json()["detail"]["reason"]
    assert _counts(store) == before_incomplete

    current_regions = client.get(f"/api/images/{target_image['id']}/regions").json()
    second = next(row for row in current_regions if row["id"] != first["id"])
    classified_second = client.patch(
        f"/api/regions/{second['id']}/background-classification",
        json={
            "category": "complex-lineart",
            "confidence": 0.0,
            "rationaleCodes": ["structural-lines-cross-region"],
            "expectedRevision": second["revision"],
            "expectedImageRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert classified_second.status_code == 200, classified_second.text
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": "all-eligible-backgrounds-reviewed",
            "observedBackgroundChecksum": context["backgroundChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_g5_zero_eligible_regions_require_explicit_not_applicable_review(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g4_accepted_page(
        client,
        app,
        tmp_path,
        disposition="keep-art",
        extra_dispositions=("ignore",),
        include_ruby=True,
    )
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    regions = prepared["regions"]
    store = prepared["store"]
    assert isinstance(target_image, dict) and isinstance(regions, list)
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    assert context["eligibleRegionIds"] == []
    assert context["classifiedRegionIds"] == []

    baseline = _counts(store)
    for row in regions:
        rejected = client.patch(
            f"/api/regions/{row['id']}/background-classification",
            json={
                "category": "white-solid",
                "confidence": 0,
                "rationaleCodes": ["uniform-near-white"],
                "expectedRevision": row["revision"],
                "expectedImageRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["reason"] == "g5-region-not-eligible"
        assert _counts(store) == baseline

    wrong_reason = client.patch(
        f"/api/images/{target_image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": "all-eligible-backgrounds-reviewed",
            "observedBackgroundChecksum": context["backgroundChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert wrong_reason.status_code == 409, wrong_reason.text
    assert wrong_reason.json()["detail"]["reason"] == "g5-background-reason-mismatch"
    assert _counts(store) == baseline

    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/background",
        json={
            "decision": "accept",
            "reason": "no-eligible-regions",
            "observedBackgroundChecksum": context["backgroundChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["event"]["state"] == "not-applicable"
    assert result["event"]["decision"] == "background-not-applicable"
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image is not None and generation is not None
        checksum, terminal = require_current_background_classifications(
            store, session, image, generation
        )
        assert checksum == context["backgroundChecksum"]
        assert terminal.state == "not-applicable"


def test_g5_requires_current_g4_acceptance_and_rolls_back_failed_event_appends(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _prepare_g4_accepted_page(client, app, tmp_path, accept_g4=False)
    target_project = pending["targetProject"]
    target_image = pending["targetImage"]
    generation_id = str(pending["generationId"])
    region = pending["region"]
    store = pending["store"]
    assert isinstance(target_project, dict)
    assert isinstance(target_image, dict) and isinstance(region, dict)
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    before_pending = _counts(store)
    rejected = client.patch(
        f"/api/regions/{region['id']}/background-classification",
        json={
            "category": "screentone",
            "confidence": 0.5,
            "rationaleCodes": ["periodic-screentone"],
            "expectedRevision": region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["reason"] == "g4-regions-not-currently-accepted"
    assert _counts(store) == before_pending
    assert client.get(f"/api/images/{target_image['id']}/page-gates/background").status_code == 409

    accepted_g4 = client.patch(
        f"/api/images/{target_image['id']}/page-gates/regions",
        json={
            "decision": "accept",
            "reason": "all-region-decisions-reviewed",
            "observedRegionChecksum": pending["g4MutationEvent"]["outputChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted_g4.status_code == 200, accepted_g4.text
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    before_mutation_failure = _counts(store)
    original_append = page_lineage._append_event

    def fail_g5_mutation(*args, **kwargs):
        if kwargs.get("operation") == "background-classification-reviewed":
            raise RuntimeError("injected G5 mutation append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(page_lineage, "_append_event", fail_g5_mutation)
    with pytest.raises(RuntimeError, match="injected G5 mutation append failure"):
        client.patch(
            f"/api/regions/{region['id']}/background-classification",
            json={
                "category": "screentone",
                "confidence": 0.5,
                "rationaleCodes": ["periodic-screentone"],
                "expectedRevision": region["revision"],
                "expectedImageRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
    assert _counts(store) == before_mutation_failure
    rolled_back_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    assert rolled_back_region == region
    assert client.get(f"/api/images/{target_image['id']}/page-gates/background").json() == context

    monkeypatch.setattr(page_lineage, "_append_event", original_append)
    classified = client.patch(
        f"/api/regions/{region['id']}/background-classification",
        json={
            "category": "screentone",
            "confidence": 0.5,
            "rationaleCodes": ["periodic-screentone"],
            "expectedRevision": region["revision"],
            "expectedImageRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert classified.status_code == 200, classified.text
    context = client.get(f"/api/images/{target_image['id']}/page-gates/background").json()
    before_accept_failure = _counts(store)

    def fail_g5_acceptance(*args, **kwargs):
        if kwargs.get("operation") == "background-stage-review":
            raise RuntimeError("injected G5 acceptance append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(page_lineage, "_append_event", fail_g5_acceptance)
    with pytest.raises(RuntimeError, match="injected G5 acceptance append failure"):
        client.patch(
            f"/api/images/{target_image['id']}/page-gates/background",
            json={
                "decision": "accept",
                "reason": "all-eligible-backgrounds-reviewed",
                "observedBackgroundChecksum": context["backgroundChecksum"],
                "expectedRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
    assert _counts(store) == before_accept_failure
    assert client.get(f"/api/images/{target_image['id']}/page-gates/background").json() == context


def test_g5_database_guards_invalid_bundles_and_service_detects_raw_tamper(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g4_accepted_page(client, app, tmp_path)
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_image, dict) and isinstance(region, dict)
    reviewer = json.dumps(_ACTOR | {"actorId": None})
    with sqlite3.connect(store.database_path) as database:
        invalid_rows = [
            ("white-solid", 0.5, None),
            ("white-solid", 2.0, json.dumps(["uniform-near-white"])),
            ("white-solid", 0.5, json.dumps([])),
            ("white-solid", 0.5, json.dumps(["uniform-near-black"])),
            (
                "white-solid",
                0.5,
                json.dumps(["uniform-near-white", "uniform-near-white"]),
            ),
        ]
        for category, confidence, rationale_codes in invalid_rows:
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(
                    """
                    UPDATE text_regions
                    SET background_category = ?, background_confidence = ?,
                        background_rationale_codes = ?, background_reviewer = ?,
                        background_generation_id = ?
                    WHERE id = ?
                    """,
                    (
                        category,
                        confidence,
                        rationale_codes,
                        reviewer,
                        generation_id,
                        region["id"],
                    ),
                )
            database.rollback()
        database.execute(
            """
            UPDATE text_regions
            SET background_category = ?, background_confidence = ?,
                background_rationale_codes = ?, background_reviewer = ?,
                background_generation_id = ?
            WHERE id = ?
            """,
            (
                "white-solid",
                0.5,
                json.dumps(["uniform-near-white"]),
                reviewer,
                generation_id,
                region["id"],
            ),
        )
        database.commit()

    tampered = client.get(f"/api/images/{target_image['id']}/page-gates/background")
    assert tampered.status_code == 409, tampered.text
    assert tampered.json()["detail"]["reason"] == "g5-evidence-not-current"
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image is not None and generation is not None
        with pytest.raises(page_lineage.PageLineageConflict) as error:
            require_current_background_classifications(store, session, image, generation)
        assert error.value.reason == "g5-evidence-not-current"


def test_g6_dual_crop_attempts_explicit_source_review_and_acceptance(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path, include_ruby=True)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_project, dict)
    assert isinstance(target_image, dict)
    assert isinstance(region, dict)

    provider = _StrictLineageOCR()
    app.state.providers.ocr = provider
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {
                "provider": "tesseract",
                "language": "ja",
                "modelVersion": "forged-client-label",
            },
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    duplicate = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["reason"] == "g6-ocr-job-active"

    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "completed"
    assert len(provider.calls) == 2

    context_response = client.get(f"/api/images/{target_image['id']}/page-gates/ocr")
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert context["state"] == "pending"
    assert context["eligibleRegionIds"] == [region["id"]]
    assert context["attemptedRegionIds"] == [region["id"]]
    assert context["reviewedRegionIds"] == []
    attempts = [attempt for attempt in context["attempts"] if attempt["regionId"] == region["id"]]
    assert {attempt["inputVariant"] for attempt in attempts} == {"original", "quality"}
    assert {attempt["confidence"] for attempt in attempts} == {0.0, 0.75}
    assert all(len(attempt["cropChecksum"]) == 64 for attempt in attempts)
    assert all(attempt["modelVersion"] == "strict-test-v1" for attempt in attempts)
    assert all(attempt["direction"] == "vertical" for attempt in attempts)
    assert all(attempt["language"] == "jpn_vert" for attempt in attempts)
    current_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    assert current_region["ocrReview"] is None
    assert current_region["ocrReviewer"] is None
    assert current_region["ocrGenerationId"] is None

    quality_attempt = next(attempt for attempt in attempts if attempt["inputVariant"] == "quality")
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    reviewed = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json={
            "sourceText": quality_attempt["text"],
            "sourceMode": "quality-attempt",
            "selectedAttemptId": quality_attempt["id"],
            "qcChecks": _OCR_QC_CHECKS,
            "expectedRevision": current_region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    reviewed_region = reviewed.json()
    assert reviewed_region["sourceText"] == "品質本文"
    assert reviewed_region["ocrReview"] == {
        "sourceMode": "quality-attempt",
        "selectedAttemptId": quality_attempt["id"],
        "sourceTextChecksum": _checksum("品質本文".encode()),
        "qcChecks": sorted(_OCR_QC_CHECKS),
        "qcFlags": ["original-quality-disagree"],
    }
    assert reviewed_region["ocrReviewer"] == _ACTOR | {"actorId": None}
    assert reviewed_region["ocrGenerationId"] == generation_id
    review_event = client.get(f"/api/page-generations/{generation_id}/events").json()[-1]
    assert review_event["operation"] == "ocr-source-reviewed"
    assert review_event["evidence"]["selectedAttemptId"] == quality_attempt["id"]

    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    assert context["reviewedRegionIds"] == [region["id"]]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/ocr",
        json={
            "decision": "accept",
            "reason": "all-translatable-source-text-reviewed",
            "observedOcrChecksum": context["ocrChecksum"],
            "expectedRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["event"]["state"] == "accepted"
    assert accepted.json()["event"]["decision"] == "ocr-trust-accepted"
    assert client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()["state"] == (
        "accepted"
    )
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        generation_row = session.get(PageGeneration, generation_id)
        assert image is not None and generation_row is not None
        checksum, terminal = require_current_ocr_trust(store, session, image, generation_row)
        assert checksum == context["ocrChecksum"]
        assert terminal.state == "accepted"

    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    immutable = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json={
            "sourceText": quality_attempt["text"],
            "sourceMode": "quality-attempt",
            "selectedAttemptId": quality_attempt["id"],
            "qcChecks": _OCR_QC_CHECKS,
            "expectedRevision": reviewed_region["revision"],
            "expectedImageRevision": accepted.json()["imageRevision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert immutable.status_code == 409, immutable.text
    assert immutable.json()["detail"]["reason"] == "g6-ocr-accepted"
    with sqlite3.connect(store.database_path) as database:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute(
                "UPDATE region_ocr_attempts SET text = ? WHERE id = ?",
                ("tampered", quality_attempt["id"]),
            )
        database.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute(
                "DELETE FROM region_ocr_attempts WHERE id = ?",
                (quality_attempt["id"],),
            )


def test_g6_active_retry_blocks_source_review_and_gate_without_writes(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_project, dict)
    assert isinstance(target_image, dict) and isinstance(region, dict)
    provider = _StrictLineageOCR()
    app.state.providers.ocr = provider

    first = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert first.status_code == 202, first.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, first.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    first_context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    quality = next(
        attempt for attempt in first_context["attempts"] if attempt["inputVariant"] == "quality"
    )
    current_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    initial_review = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json={
            "sourceText": quality["text"],
            "sourceMode": "quality-attempt",
            "selectedAttemptId": quality["id"],
            "qcChecks": _OCR_QC_CHECKS,
            "expectedRevision": current_region["revision"],
            "expectedImageRevision": current_image["revision"],
            "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
        },
    )
    assert initial_review.status_code == 200, initial_review.text

    retry = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert retry.status_code == 202, retry.text

    def assert_active_job_blocks_review_and_gate() -> None:
        context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
        current = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
        image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
        current_generation = client.get(
            f"/api/images/{target_image['id']}/page-generations"
        ).json()[0]
        baseline_counts = _counts(store)
        blocked_review = client.patch(
            f"/api/regions/{region['id']}/ocr-source-review",
            json={
                "sourceText": quality["text"],
                "sourceMode": "quality-attempt",
                "selectedAttemptId": quality["id"],
                "qcChecks": _OCR_QC_CHECKS,
                "expectedRevision": current["revision"],
                "expectedImageRevision": image["revision"],
                "lineage": _mutation_lineage(generation_id, current_generation["nextSequence"]),
            },
        )
        assert blocked_review.status_code == 409, blocked_review.text
        assert blocked_review.json()["detail"]["reason"] == "g6-ocr-job-active"
        assert _counts(store) == baseline_counts
        assert client.get(f"/api/images/{target_image['id']}/regions").json()[0] == current

        blocked_gate = client.patch(
            f"/api/images/{target_image['id']}/page-gates/ocr",
            json={
                "decision": "accept",
                "reason": "all-translatable-source-text-reviewed",
                "observedOcrChecksum": context["ocrChecksum"],
                "expectedRevision": image["revision"],
                "lineage": _mutation_lineage(generation_id, current_generation["nextSequence"]),
            },
        )
        assert blocked_gate.status_code == 409, blocked_gate.text
        assert blocked_gate.json()["detail"]["reason"] == "g6-ocr-job-active"
        assert _counts(store) == baseline_counts

    # A queued item is active even before provider execution begins.
    assert_active_job_blocks_review_and_gate()

    claimed = app.state.queue._claim_next()
    assert claimed == (store, retry.json()["id"])
    with store.session() as session:
        retry_item_id = session.scalar(
            select(JobItem.id).where(JobItem.job_id == retry.json()["id"])
        )
    assert isinstance(retry_item_id, str)
    assert app.state.queue._begin_item(store, retry.json()["id"], retry_item_id)
    output = app.state.queue._process_item(store, retry.json()["id"], retry_item_id)
    assert output["attemptCount"] == 2

    # Published attempts do not make a still-running item terminal.
    assert_active_job_blocks_review_and_gate()


def test_g6_rejects_forged_review_invalid_text_and_stale_cas_without_writes(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_project, dict)
    assert isinstance(target_image, dict) and isinstance(region, dict)
    app.state.providers.ocr = _StrictLineageOCR()
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    quality = next(
        attempt for attempt in context["attempts"] if attempt["inputVariant"] == "quality"
    )
    current_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    valid = {
        "sourceText": quality["text"],
        "sourceMode": "quality-attempt",
        "selectedAttemptId": quality["id"],
        "qcChecks": _OCR_QC_CHECKS,
        "expectedRevision": current_region["revision"],
        "expectedImageRevision": current_image["revision"],
        "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
    }
    baseline_counts = _counts(store)
    baseline_context = context
    forged = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json=valid | {"ocrReviewer": _ACTOR},
    )
    assert forged.status_code == 422, forged.text
    mismatch = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json=valid | {"sourceText": "別の本文"},
    )
    assert mismatch.status_code == 400, mismatch.text
    assert "must match" in mismatch.json()["detail"]
    template = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json=valid
        | {
            "sourceText": "联系我们",
            "sourceMode": "manual-correction",
        },
    )
    assert template.status_code == 409, template.text
    assert "ocr-source-text-invalid" in template.json()["detail"]["reason"]
    stale = client.patch(
        f"/api/regions/{region['id']}/ocr-source-review",
        json=valid | {"expectedImageRevision": current_image["revision"] - 1},
    )
    assert stale.status_code == 409, stale.text
    assert _counts(store) == baseline_counts
    assert client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json() == (
        baseline_context
    )
    assert client.get(f"/api/images/{target_image['id']}/regions").json()[0] == current_region


def test_g6_redraw_art_sound_effect_requires_ocr_and_trusted_source_review(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(
        client,
        app,
        tmp_path,
        disposition="redraw-art",
        region_type="sound_effect",
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    assert context["eligibleRegionIds"] == [prepared["region"]["id"]]
    assert context["attempts"] == []
    baseline = _counts(store)
    forbidden_na = client.patch(
        f"/api/images/{target_image['id']}/page-gates/ocr",
        json={
            "decision": "accept",
            "reason": "no-translatable-regions",
            "observedOcrChecksum": context["ocrChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert forbidden_na.status_code == 409, forbidden_na.text
    assert forbidden_na.json()["detail"]["reason"] == "g6-ocr-reason-mismatch"
    assert _counts(store) == baseline

    app.state.providers.ocr = _StrictLineageOCR()
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    redraw_job = app.state.queue.get_job(store, queued.json()["id"])
    assert redraw_job.status == "completed", [item.error for item in redraw_job.items]
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    assert {attempt["regionId"] for attempt in context["attempts"]} == {prepared["region"]["id"]}
    assert {attempt["inputVariant"] for attempt in context["attempts"]} == {
        "original",
        "quality",
    }
    before_unreviewed = _counts(store)
    unreviewed = client.patch(
        f"/api/images/{target_image['id']}/page-gates/ocr",
        json={
            "decision": "accept",
            "reason": "all-translatable-source-text-reviewed",
            "observedOcrChecksum": context["ocrChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert unreviewed.status_code == 409, unreviewed.text
    assert "ocr-source-review-missing" in unreviewed.json()["detail"]["reason"]
    assert _counts(store) == before_unreviewed


def test_g6_keep_art_ignore_and_ruby_remain_not_applicable(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(
        client,
        app,
        tmp_path,
        disposition="keep-art",
        extra_dispositions=("ignore",),
        include_ruby=True,
    )
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    assert context["eligibleRegionIds"] == []
    assert context["attempts"] == []
    baseline = _counts(store)
    forbidden_job = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert forbidden_job.status_code == 409, forbidden_job.text
    assert forbidden_job.json()["detail"]["reason"] == "g6-no-translatable-regions"
    assert _counts(store) == baseline
    accepted = client.patch(
        f"/api/images/{target_image['id']}/page-gates/ocr",
        json={
            "decision": "accept",
            "reason": "no-translatable-regions",
            "observedOcrChecksum": context["ocrChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["event"]["state"] == "not-applicable"
    assert accepted.json()["event"]["decision"] == "ocr-not-applicable"
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        generation_row = session.get(PageGeneration, generation_id)
        assert image is not None and generation_row is not None
        checksum, terminal = require_current_ocr_trust(store, session, image, generation_row)
        assert checksum == context["ocrChecksum"]
        assert terminal.state == "not-applicable"


def test_g6_recovers_published_attempts_without_running_provider_twice(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    provider = _StrictLineageOCR()
    app.state.providers.ocr = provider
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    with store.session() as session:
        item_id = session.scalar(select(JobItem.id).where(JobItem.job_id == queued.json()["id"]))
    assert isinstance(item_id, str)
    assert app.state.queue._begin_item(store, queued.json()["id"], item_id)
    output = app.state.queue._process_item(store, queued.json()["id"], item_id)
    assert output["attemptCount"] == 2
    assert len(provider.calls) == 2
    with store.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RegionOCRAttempt)
                .where(RegionOCRAttempt.job_item_id == item_id)
            )
            == 2
        )
    assert store.recover_jobs() == 1
    reclaimed = app.state.queue._claim_next()
    assert reclaimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*reclaimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "completed"
    assert len(provider.calls) == 2
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["operation"] for event in events[-3:]] == [
        "ocr-job-enqueued",
        "ocr-attempts-produced",
        "ocr-job-completed",
    ]


def test_g6_recovery_requeues_failed_parent_with_running_published_item(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    provider = _StrictLineageOCR()
    app.state.providers.ocr = provider
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    with store.session() as session:
        item_id = session.scalar(select(JobItem.id).where(JobItem.job_id == queued.json()["id"]))
    assert isinstance(item_id, str)
    assert app.state.queue._begin_item(store, queued.json()["id"], item_id)
    output = app.state.queue._process_item(store, queued.json()["id"], item_id)
    assert output["attemptCount"] == 2
    assert len(provider.calls) == 2

    original_record_finished = queue_module.record_job_item_finished

    def fail_completion_event(*_args, **_kwargs) -> None:
        raise RuntimeError("injected OCR completion event failure")

    monkeypatch.setattr(queue_module, "record_job_item_finished", fail_completion_event)
    with pytest.raises(RuntimeError, match="injected OCR completion event failure"):
        app.state.queue._finish_item(
            store,
            queued.json()["id"],
            item_id,
            output=output,
        )
    app.state.queue._fail_job(store, queued.json()["id"], "Unexpected worker failure")
    with store.session() as session:
        job = session.get(Job, queued.json()["id"])
        item = session.get(JobItem, item_id)
        assert job is not None and item is not None
        assert job.status == "failed"
        assert item.status == "running"

    monkeypatch.setattr(queue_module, "record_job_item_finished", original_record_finished)
    assert store.recover_jobs() == 1
    with store.session() as session:
        job = session.get(Job, queued.json()["id"])
        item = session.get(JobItem, item_id)
        assert job is not None and item is not None
        assert job.status == "queued"
        assert job.error is None
        assert item.status == "queued"
        assert item.error is None

    reclaimed = app.state.queue._claim_next()
    assert reclaimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*reclaimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "completed"
    assert len(provider.calls) == 2
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["operation"] for event in events[-3:]] == [
        "ocr-job-enqueued",
        "ocr-attempts-produced",
        "ocr-job-completed",
    ]


def test_g6_failed_provider_is_zero_attempt_and_new_strict_job_retries(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)

    class FailingOCR(_StrictLineageOCR):
        def recognize_region(self, *args, **kwargs) -> OCRRegion:
            self.calls.append((args[0], dict(args[1]), kwargs["direction"], kwargs.get("language")))
            raise RuntimeError("injected OCR provider failure")

    failing = FailingOCR()
    app.state.providers.ocr = failing
    first = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert first.status_code == 202, first.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, first.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, first.json()["id"]).status == "failed"
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr")
    assert context.status_code == 200, context.text
    assert context.json()["attempts"] == []
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["operation"] for event in events[-2:]] == [
        "ocr-job-enqueued",
        "ocr-job-failed",
    ]

    recovered = _StrictLineageOCR()
    app.state.providers.ocr = recovered
    retry = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert retry.status_code == 202, retry.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, retry.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, retry.json()["id"]).status == "completed"
    assert len(recovered.calls) == 2
    retried_context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    assert len(retried_context["attempts"]) == 2


def test_g6_source_review_and_gate_roll_back_when_event_append_fails(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    region = prepared["region"]
    store = prepared["store"]
    assert isinstance(target_project, dict)
    assert isinstance(target_image, dict) and isinstance(region, dict)
    app.state.providers.ocr = _StrictLineageOCR()
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract", "language": "ja"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    quality = next(
        attempt for attempt in context["attempts"] if attempt["inputVariant"] == "quality"
    )
    current_region = client.get(f"/api/images/{target_image['id']}/regions").json()[0]
    current_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]
    review_body = {
        "sourceText": quality["text"],
        "sourceMode": "quality-attempt",
        "selectedAttemptId": quality["id"],
        "qcChecks": _OCR_QC_CHECKS,
        "expectedRevision": current_region["revision"],
        "expectedImageRevision": current_image["revision"],
        "lineage": _mutation_lineage(generation_id, generation["nextSequence"]),
    }
    original_append = page_lineage._append_event

    def fail_review(*args, **kwargs):
        if kwargs.get("operation") == "ocr-source-reviewed":
            raise RuntimeError("injected OCR review append failure")
        return original_append(*args, **kwargs)

    baseline_counts = _counts(store)
    monkeypatch.setattr(page_lineage, "_append_event", fail_review)
    with pytest.raises(RuntimeError, match="injected OCR review append failure"):
        client.patch(f"/api/regions/{region['id']}/ocr-source-review", json=review_body)
    assert _counts(store) == baseline_counts
    assert client.get(f"/api/images/{target_image['id']}/regions").json()[0] == current_region
    assert client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json() == context

    monkeypatch.setattr(page_lineage, "_append_event", original_append)
    reviewed = client.patch(f"/api/regions/{region['id']}/ocr-source-review", json=review_body)
    assert reviewed.status_code == 200, reviewed.text
    reviewed_context = client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json()
    reviewed_image = client.get(f"/api/projects/{target_project['id']}/images").json()[0]
    reviewed_generation = client.get(f"/api/images/{target_image['id']}/page-generations").json()[0]

    def fail_gate(*args, **kwargs):
        if kwargs.get("operation") == "ocr-stage-review":
            raise RuntimeError("injected OCR gate append failure")
        return original_append(*args, **kwargs)

    baseline_counts = _counts(store)
    monkeypatch.setattr(page_lineage, "_append_event", fail_gate)
    with pytest.raises(RuntimeError, match="injected OCR gate append failure"):
        client.patch(
            f"/api/images/{target_image['id']}/page-gates/ocr",
            json={
                "decision": "accept",
                "reason": "all-translatable-source-text-reviewed",
                "observedOcrChecksum": reviewed_context["ocrChecksum"],
                "expectedRevision": reviewed_image["revision"],
                "lineage": _mutation_lineage(generation_id, reviewed_generation["nextSequence"]),
            },
        )
    assert _counts(store) == baseline_counts
    assert client.get(f"/api/images/{target_image['id']}/page-gates/ocr").json() == (
        reviewed_context
    )


def test_g6_attempt_publication_rolls_back_when_lineage_append_fails(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g5_accepted_page(client, app, tmp_path)
    target_project = prepared["targetProject"]
    target_image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(target_project, dict) and isinstance(target_image, dict)
    app.state.providers.ocr = _StrictLineageOCR()
    queued = client.post(
        f"/api/projects/{target_project['id']}/ocr",
        json={
            "imageIds": [target_image["id"]],
            "options": {"provider": "tesseract"},
            "lineage": _current_lineage_context(client, target_image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    with store.session() as session:
        item_id = session.scalar(select(JobItem.id).where(JobItem.job_id == queued.json()["id"]))
        before_attempts = session.scalar(select(func.count()).select_from(RegionOCRAttempt))
        image = session.get(ImageAsset, target_image["id"])
        project = store.project(session)
        assert image is not None
        before = (image.revision, project.revision)
    assert isinstance(item_id, str)
    assert app.state.queue._begin_item(store, queued.json()["id"], item_id)
    original_append = page_lineage._append_event

    def fail_publication(*args, **kwargs):
        if kwargs.get("operation") == "ocr-attempts-produced":
            raise RuntimeError("injected OCR publication append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(page_lineage, "_append_event", fail_publication)
    with pytest.raises(RuntimeError, match="injected OCR publication append failure"):
        app.state.queue._process_item(store, queued.json()["id"], item_id)
    with store.session() as session:
        image = session.get(ImageAsset, target_image["id"])
        project = store.project(session)
        assert image is not None
        assert session.scalar(select(func.count()).select_from(RegionOCRAttempt)) == before_attempts
        assert (image.revision, project.revision) == before
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert events[-1]["operation"] == "ocr-job-enqueued"


def test_open_additively_migrates_legacy_database_without_fabricating_lineage(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "legacy-project"
    first_app = create_app(settings, start_worker=False)
    with TestClient(first_app) as first:
        project = create_project(first, root, "legacy")
        image = upload_image(first, project["id"], data=png_bytes(color="gray"))
        queued = first.post(
            f"/api/projects/{project['id']}/preprocess",
            json={"imageIds": [image["id"]], "options": {"profile": "off"}},
        )
        assert queued.status_code == 202, queued.text
        store = first_app.state.registry.get(project["id"])
        with store.session() as session:
            project_row = session.get(Project, project["id"])
            image_row = session.get(ImageAsset, image["id"])
            assert project_row is not None and image_row is not None
            preimage = {
                "projectRevision": project_row.revision,
                "imageRevision": image_row.revision,
                "revisionCount": session.scalar(select(func.count()).select_from(Revision)),
                "jobCount": session.scalar(select(func.count()).select_from(Job)),
            }
        database_path = store.database_path
    first_app.state.registry.get(project["id"]).engine.dispose()

    with sqlite3.connect(database_path) as database:
        for trigger in (
            "page_mask_artifacts_no_update",
            "page_mask_artifacts_no_delete",
            "page_mask_reviews_no_update",
            "page_mask_reviews_no_delete",
            "page_mask_artifacts_validate_insert",
            "page_mask_reviews_validate_insert",
            "page_clean_plate_candidates_no_update",
            "page_clean_plate_candidates_no_delete",
            "page_clean_plate_reviews_no_update",
            "page_clean_plate_reviews_no_delete",
            "page_clean_plate_candidates_validate_insert",
            "page_clean_plate_reviews_validate_insert",
            "page_cloud_full_page_candidates_no_update",
            "page_cloud_full_page_candidates_no_delete",
            "page_cloud_full_page_reviews_no_update",
            "page_cloud_full_page_reviews_no_delete",
            "page_cloud_full_page_candidates_validate_insert",
            "page_cloud_full_page_reviews_validate_insert",
            "revisions_g8_no_update",
            "revisions_g8_no_delete",
            "revisions_g8_cloud_no_update",
            "revisions_g8_cloud_no_delete",
            "region_translation_candidates_no_update",
            "region_translation_candidates_no_delete",
            "region_translation_reviews_no_update",
            "region_translation_reviews_no_delete",
            "page_translation_reviews_no_update",
            "page_translation_reviews_no_delete",
            "region_translation_candidates_validate_insert",
            "region_translation_reviews_validate_insert",
            "page_translation_reviews_validate_insert",
            "revisions_g9_no_update",
            "revisions_g9_no_delete",
            "page_typeset_candidates_no_update",
            "page_typeset_candidates_no_delete",
            "page_typeset_reviews_no_update",
            "page_typeset_reviews_no_delete",
            "page_typeset_candidates_validate_insert",
            "page_typeset_reviews_validate_insert",
            "page_typeset_reviews_validate_known_defects",
            "revisions_g10_no_update",
            "revisions_g10_no_delete",
            "revisions_g0_no_update",
            "revisions_g0_no_delete",
        ):
            database.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        database.execute("DROP TABLE page_typeset_reviews")
        database.execute("DROP TABLE page_typeset_candidates")
        database.execute("DROP TABLE page_clean_plate_reviews")
        database.execute("DROP TABLE page_clean_plate_candidates")
        database.execute("DROP TABLE page_cloud_full_page_reviews")
        database.execute("DROP TABLE page_cloud_full_page_candidates")
        database.execute("DROP TABLE page_mask_reviews")
        database.execute("DROP TABLE page_mask_artifacts")
        database.execute("DROP TABLE page_mask_drafts")
        database.execute("DROP TABLE page_translation_reviews")
        database.execute("DROP TABLE region_translation_reviews")
        database.execute("DROP TABLE region_translation_candidates")
        database.execute("DROP TRIGGER text_regions_g6_validate_insert")
        database.execute("DROP TRIGGER text_regions_g6_validate_update")
        database.execute("DROP TRIGGER region_ocr_attempts_validate_insert")
        database.execute("DROP TRIGGER region_ocr_attempts_append_only_update")
        database.execute("DROP TRIGGER region_ocr_attempts_append_only_delete")
        database.execute("DROP TABLE region_ocr_attempts")
        database.execute("DROP TABLE page_lineage_events")
        database.execute("DROP TABLE page_generations")
        database.execute("ALTER TABLE jobs DROP COLUMN lineage_context")

    fresh_settings = settings.model_copy(update={"data_dir": tmp_path / "fresh-catalog"})
    for index in range(2):
        reopened_app = create_app(fresh_settings, start_worker=False)
        with TestClient(reopened_app) as reopened:
            opened = reopened.post(
                "/api/projects/open",
                json={"manifestPath": str(root / "project/project.json")},
            )
            assert opened.status_code == 200, opened.text
            reopened_store = reopened_app.state.registry.get(project["id"])
            with reopened_store.session() as session:
                project_row = session.get(Project, project["id"])
                image_row = session.get(ImageAsset, image["id"])
                job_row = session.get(Job, queued.json()["id"])
                assert project_row is not None and image_row is not None and job_row is not None
                assert project_row.revision == preimage["projectRevision"]
                assert image_row.revision == preimage["imageRevision"]
                assert (
                    session.scalar(select(func.count()).select_from(Revision))
                    == preimage["revisionCount"]
                )
                assert session.scalar(select(func.count()).select_from(Job)) == preimage["jobCount"]
                assert session.scalar(select(func.count()).select_from(PageGeneration)) == 0
                assert session.scalar(select(func.count()).select_from(PageLineageEvent)) == 0
                assert session.scalar(select(func.count()).select_from(RegionOCRAttempt)) == 0
                assert job_row.lineage_context is None
        reopened_app.state.registry.get(project["id"]).engine.dispose()
        if index == 0:
            with sqlite3.connect(database_path) as database:
                tables = {
                    row[0]
                    for row in database.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                assert {
                    "page_generations",
                    "page_lineage_events",
                    "region_ocr_attempts",
                    "page_typeset_candidates",
                    "page_typeset_reviews",
                } <= tables
                job_columns = {row[1] for row in database.execute("PRAGMA table_info(jobs)")}
                assert "lineage_context" in job_columns
                region_columns = {
                    row[1] for row in database.execute("PRAGMA table_info(text_regions)")
                }
                assert {
                    "background_category",
                    "background_confidence",
                    "background_rationale_codes",
                    "background_reviewer",
                    "background_generation_id",
                    "ocr_review",
                    "ocr_reviewer",
                    "ocr_generation_id",
                } <= region_columns
                g5_triggers = {
                    row[0]
                    for row in database.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'trigger' AND name LIKE 'text_regions_g5_validate_%'
                        """
                    )
                }
                assert g5_triggers == {
                    "text_regions_g5_validate_insert",
                    "text_regions_g5_validate_update",
                }
                g6_triggers = {
                    row[0]
                    for row in database.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'trigger' AND name LIKE 'text_regions_g6_validate_%'
                        """
                    )
                }
                assert g6_triggers == {
                    "text_regions_g6_validate_insert",
                    "text_regions_g6_validate_update",
                }
                attempt_triggers = {
                    row[0]
                    for row in database.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'trigger'
                          AND name LIKE 'region_ocr_attempts_append_only_%'
                        """
                    )
                }
                assert attempt_triggers == {
                    "region_ocr_attempts_append_only_update",
                    "region_ocr_attempts_append_only_delete",
                }


def test_open_replaces_translate_only_ocr_attempt_trigger(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "legacy-translate-only-ocr-trigger"
    first_app = create_app(settings, start_worker=False)
    with TestClient(first_app) as first:
        project = create_project(first, root, "legacy OCR trigger")
        image = upload_image(first, project["id"], data=png_bytes(color="gray"))
        database_path = first_app.state.registry.get(project["id"]).database_path
    first_app.state.registry.get(project["id"]).engine.dispose()

    with sqlite3.connect(database_path) as database:
        current_sql = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            ("region_ocr_attempts_validate_insert",),
        ).fetchone()[0]
        assert "region.region_disposition IN ('translate', 'redraw-art')" in current_sql
        legacy_sql = current_sql.replace(
            "region.region_disposition IN ('translate', 'redraw-art')",
            "region.region_disposition = 'translate'",
        )
        database.execute("DROP TRIGGER region_ocr_attempts_validate_insert")
        database.execute(legacy_sql)
        persisted_sql = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            ("region_ocr_attempts_validate_insert",),
        ).fetchone()[0]
        assert "redraw-art" not in persisted_sql

    fresh_settings = settings.model_copy(update={"data_dir": tmp_path / "fresh-trigger-catalog"})
    reopened_app = create_app(fresh_settings, start_worker=False)
    with TestClient(reopened_app) as reopened:
        opened = reopened.post(
            "/api/projects/open",
            json={"manifestPath": str(root / "project/project.json")},
        )
        assert opened.status_code == 200, opened.text
        assert reopened.get(f"/api/projects/{project['id']}/images").json()[0]["id"] == image["id"]
        reopened_database_path = reopened_app.state.registry.get(project["id"]).database_path
        with sqlite3.connect(reopened_database_path) as database:
            migrated_sql = database.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                ("region_ocr_attempts_validate_insert",),
            ).fetchone()[0]
        assert "region.region_disposition IN ('translate', 'redraw-art')" in migrated_sql
    reopened_app.state.registry.get(project["id"]).engine.dispose()


_MASK_COVERAGE = [
    {"check": check, "passed": True}
    for check in (
        "body-glyphs-covered",
        "punctuation-covered",
        "strokes-and-shadows-covered",
        "ruby-covered",
        "antialias-edges-covered",
    )
]
_MASK_COLLATERAL = [
    {"check": check, "passed": True}
    for check in (
        "bubble-borders-protected",
        "characters-protected",
        "speed-lines-protected",
        "screentone-protected",
        "nearby-art-protected",
    )
]


def _mask_recipe(region_id: str, *, manual: bool = False) -> dict[str, object]:
    return {
        "regionId": region_id,
        "maskMode": "manual" if manual else "region",
        "polygon": None,
        "padding": 2,
        "dilation": 1,
        "feather": 1,
        "polarity": "auto",
        "maskEdits": {
            "version": 1,
            "strokes": (
                [{"mode": "add", "radius": 3, "points": [[25, 25], [60, 60]]}] if manual else []
            ),
        },
    }


def test_g7_draft_checksum_uses_cross_runtime_float64_tokens() -> None:
    recipe = [
        {
            "regionId": "region-1",
            "maskMode": "manual",
            "polygon": [[0.0, 1.5], [2.0, 3.25], [4.0, 5.0]],
            "padding": 4,
            "dilation": 2,
            "feather": 1,
            "polarity": "auto",
            "maskEdits": {
                "version": 1,
                "strokes": [
                    {
                        "mode": "add",
                        "radius": 1.0,
                        "points": [[0.0, 1.5], [2.0, 3.25]],
                    }
                ],
            },
        }
    ]

    assert (
        mask_service._draft_checksum("a" * 64, "b" * 64, {"region-1": []}, recipe)
        == "96fad7d6a7dca0386b8544d04c53c377575c89f4b946ecf8f839f82e4eb700b0"
    )


def test_g7_primary_recipe_includes_ruby_and_accepts_exact_artifact(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path, include_ruby=True)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(project, dict) and isinstance(image, dict)
    context_response = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert context["draft"]["revision"] == 0
    assert len(context["eligibleRegionIds"]) == 1
    primary_id = context["eligibleRegionIds"][0]
    ruby_ids = context["rubyRegionIdsByPrimary"][primary_id]
    assert len(ruby_ids) == 1
    with store.session() as session:
        assert session.get(PageMaskDraft, generation_id) is None
    saved = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": [_mask_recipe(primary_id, manual=True)],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert saved.status_code == 200, saved.text
    queued = client.post(
        f"/api/projects/{project['id']}/mask",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "completed"
    context_response = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    artifact = context["artifacts"][0]
    assert artifact["nonzeroPixelCount"] > 0
    assert artifact["provider"] == "deterministic-mask"
    served = client.get(
        f"/api/images/{image['id']}/page-gates/mask/artifacts/{artifact['artifactId']}"
    )
    assert served.status_code == 200
    assert served.headers["cache-control"] == "private, no-store"
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "accept",
            "reason": "complete-and-no-collateral",
            "selectedArtifactId": artifact["artifactId"],
            "observedMaskChecksum": artifact["maskChecksum"],
            "coverageChecks": _MASK_COVERAGE,
            "collateralChecks": _MASK_COLLATERAL,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    event = accepted.json()["event"]
    assert event["evidence"]["rubyRegionIdsByPrimary"] == {primary_id: ruby_ids}
    final_response = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert final_response.status_code == 200, final_response.text
    final_context = final_response.json()
    assert event["outputChecksum"] == final_context["maskStateChecksum"]
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        checksum, selected = require_current_mask_acceptance(store, session, image_row, generation)
        assert checksum == final_context["maskStateChecksum"]
        assert selected is not None and selected.id == artifact["artifactId"]
        selected_path = store.root / selected.relative_path
        generation.next_sequence += 1
        session.add(
            PageLineageEvent(
                generation_id=generation.id,
                sequence=generation.next_sequence - 1,
                operation="inpaint-job-enqueued",
                gate="G8_cleanPlate",
                state="pending",
                actor_kind="codex",
                task_id="lineage-test-task",
                operation_source="api",
                input_checksum=checksum,
                output_checksum=checksum,
                parent_checksum=checksum,
                stage="inpaint",
                reason="job-enqueued",
                evidence={},
            )
        )
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        downstream_checksum, downstream_selected = require_current_mask_acceptance(
            store, session, image_row, generation
        )
        assert downstream_checksum == final_context["maskStateChecksum"]
        assert downstream_selected is not None
        assert downstream_selected.id == artifact["artifactId"]
    selected_path.write_bytes(b"not-the-accepted-png")
    changed = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["reason"] == "g7-mask-artifact-checksum-mismatch"


def test_g7_redraw_art_after_g6_na_and_polygon_fraud_is_zero_write(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path, disposition="redraw-art")
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert len(context["eligibleRegionIds"]) == 1
    before = _counts(store)
    recipe = _mask_recipe(context["eligibleRegionIds"][0])
    recipe["maskMode"] = "text"
    recipe["polygon"] = [[-1, 1], [2, 2], [3, 3]]
    invalid = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": [recipe],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert invalid.status_code in {400, 409, 422}, invalid.text
    assert _counts(store) == before
    with store.session() as session:
        assert session.get(PageMaskDraft, generation_id) is None
        assert session.scalar(select(func.count()).select_from(PageMaskArtifact)) == 0
        assert session.scalar(select(func.count()).select_from(PageMaskReview)) == 0


def _save_g7_default_draft(client: TestClient, prepared: dict[str, object]) -> dict[str, object]:
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    generation_id = str(prepared["generationId"])
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    response = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": [_mask_recipe(region_id) for region_id in context["eligibleRegionIds"]],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _enqueue_g7(client: TestClient, prepared: dict[str, object]):
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    response = client.post(
        f"/api/projects/{project['id']}/mask",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    return response


def _prepare_g7_accepted_page(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    disposition: str = "translate",
    region_type: str = "dialogue",
    background_category: str = "white-solid",
    extra_dispositions: tuple[str, ...] = (),
    ocr_provider=None,
    rotation: float = 0,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared = _prepare_g6_accepted_page(
        client,
        app,
        tmp_path,
        disposition=disposition,
        region_type=region_type,
        background_category=background_category,
        extra_dispositions=extra_dispositions,
        ocr_provider=ocr_provider,
        rotation=rotation,
        prepared=prepared,
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    artifact = context["artifacts"][-1]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "accept",
            "reason": "complete-and-no-collateral",
            "selectedArtifactId": artifact["artifactId"],
            "observedMaskChecksum": artifact["maskChecksum"],
            "coverageChecks": _MASK_COVERAGE,
            "collateralChecks": _MASK_COLLATERAL,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    return prepared | {
        "maskArtifact": artifact,
        "acceptedG7": accepted.json(),
    }


_CLEAN_PLATE_CHECKS = [
    {"check": check, "passed": True}
    for check in (
        "outside-mask-unchanged",
        "source-text-unreadable",
        "no-white-or-gray-hole",
        "no-blur-band",
        "no-repeated-texture",
        "background-continuous",
        "structure-preserved",
    )
]

_TRANSLATION_CHECKS = [
    {"check": check, "passed": True}
    for check in (
        "target-chinese-checked",
        "forbidden-template-checked",
        "nonempty-checked",
        "source-copy-checked",
        "japanese-residual-checked",
        "generic-duplicate-checked",
        "source-consistency-checked",
        "context-consistency-checked",
        "tone-and-type-checked",
        "source-noise-checked",
    )
]


def _prepare_g8_accepted_page(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    disposition: str = "translate",
    region_type: str = "dialogue",
    extra_dispositions: tuple[str, ...] = (),
    ocr_provider=None,
    rotation: float = 0,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared = _prepare_g7_accepted_page(
        client,
        app,
        tmp_path,
        disposition=disposition,
        region_type=region_type,
        extra_dispositions=extra_dispositions,
        ocr_provider=ocr_provider,
        rotation=rotation,
        prepared=prepared,
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    candidate = context["candidates"][-1]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "accept",
            "reason": "clean-plate-complete",
            "candidateId": candidate["candidateId"],
            "observedCandidateChecksum": candidate["candidateChecksum"],
            "observedWidth": candidate["width"],
            "observedHeight": candidate["height"],
            "checks": _CLEAN_PLATE_CHECKS,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    return prepared | {"acceptedG8": accepted.json()}


def _create_g9_candidate(
    client: TestClient,
    *,
    image_id: str,
    generation_id: str,
    context: dict[str, object],
    region: dict[str, object],
    translation_text: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/images/{image_id}/page-gates/translation/candidates",
        json={
            "regionId": region["regionId"],
            "translationText": translation_text,
            "originKind": "agent",
            "observedG8Checksum": context["g8Checksum"],
            "observedSourceTextChecksum": region["sourceTextChecksum"],
            "observedContextChecksum": region["contextChecksum"],
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, int(context["nextSequence"])),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_g9_whole_page_candidates_review_projection_and_terminal(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    initial = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert initial.status_code == 200, initial.text
    assert initial.json()["translationStateChecksum"] == initial.json()["g8Checksum"]
    region_job = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "regionIds": [initial.json()["eligibleRegions"][0]["regionId"]],
            "options": {"provider": "mock"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert region_job.status_code == 409
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "mock"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "completed"
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    assert len(context["candidates"]) == len(context["eligibleRegions"]) == 1
    candidate = context["candidates"][0]
    assert candidate["originKind"] == "model"
    assert candidate["targetLanguage"] == "zh-CN"
    assert candidate["computedQcFlags"] == ["none"]
    reviewed = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{candidate['candidateId']}",
        json={
            "decision": "accept",
            "reason": "translation-reviewed",
            "observedCandidateChecksum": candidate["candidateChecksum"],
            "observedSourceTextChecksum": candidate["sourceTextChecksum"],
            "observedContextChecksum": candidate["contextChecksum"],
            "observedG8Checksum": context["g8Checksum"],
            "checks": _TRANSLATION_CHECKS,
            "qcFlags": ["none"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    with store.session() as session:
        region = session.get(TextRegion, candidate["regionId"])
        assert region is not None
        assert region.translation_text == candidate["translationText"]
        assert region.translation_provider == "mock"
    terminal = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "accept",
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert terminal.status_code == 200, terminal.text
    final_context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    assert final_context["state"] == "accepted"
    assert final_context["terminalChecksum"] == terminal.json()["event"]["outputChecksum"]
    before = _counts(store)
    blocked_revision = client.post(
        f"/api/images/{image['id']}/page-gates/translation/candidates",
        json={
            "regionId": candidate["regionId"],
            "translationText": "终态后禁止修改",
            "originKind": "agent",
            "observedG8Checksum": final_context["g8Checksum"],
            "observedSourceTextChecksum": candidate["sourceTextChecksum"],
            "observedContextChecksum": candidate["contextChecksum"],
            "observedTranslationStateChecksum": final_context["translationStateChecksum"],
            "expectedRevision": final_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, final_context["nextSequence"]),
        },
    )
    assert blocked_revision.status_code == 409
    assert blocked_revision.json()["detail"]["reason"] == "g9-translation-accepted"
    assert _counts(store) == before


def test_g9_redraw_art_only_sound_effect_is_eligible_and_cannot_be_not_applicable(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(
        client,
        app,
        tmp_path,
        disposition="redraw-art",
        region_type="sound_effect",
    )
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    assert [row["regionId"] for row in context["eligibleRegions"]] == [prepared["region"]["id"]]
    assert context["eligibleRegions"][0]["regionType"] == "sound_effect"

    forbidden_na = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "not-applicable",
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert forbidden_na.status_code == 409, forbidden_na.text
    assert forbidden_na.json()["detail"]["reason"] == "g9-na-invalid"
    replay = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert replay.status_code == 200, replay.text
    assert replay.json() == context


def test_g9_mixed_translate_and_redraw_art_requires_both_reviews_and_replays_exactly(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("redraw-art",),
    )
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    regions = prepared["regions"]
    assert isinstance(regions, list)
    expected_ids = {
        row["id"]
        for row in regions
        if row["contentDisposition"] in {"translate", "redraw-art"} and row["type"] != "ruby"
    }
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    assert {row["regionId"] for row in context["eligibleRegions"]} == expected_ids

    translations = ("第一句合规译文", "第二句合规译文")
    for region, translation_text in zip(context["eligibleRegions"], translations, strict=True):
        context = _create_g9_candidate(
            client,
            image_id=str(image["id"]),
            generation_id=generation_id,
            context=context,
            region=region,
            translation_text=translation_text,
        )
    candidates = list(context["candidates"])
    assert {candidate["regionId"] for candidate in candidates} == expected_ids

    first = candidates[0]
    first_review = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{first['candidateId']}",
        json={
            "decision": "accept",
            "reason": "translation-reviewed",
            "observedCandidateChecksum": first["candidateChecksum"],
            "observedSourceTextChecksum": first["sourceTextChecksum"],
            "observedContextChecksum": first["contextChecksum"],
            "observedG8Checksum": context["g8Checksum"],
            "checks": _TRANSLATION_CHECKS,
            "qcFlags": ["none"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert first_review.status_code == 200, first_review.text
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    incomplete = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "accept",
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert incomplete.status_code == 409, incomplete.text
    assert incomplete.json()["detail"]["reason"] == "g9-unreviewed-candidates"

    second = candidates[1]
    second_review = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{second['candidateId']}",
        json={
            "decision": "accept",
            "reason": "translation-reviewed",
            "observedCandidateChecksum": second["candidateChecksum"],
            "observedSourceTextChecksum": second["sourceTextChecksum"],
            "observedContextChecksum": second["contextChecksum"],
            "observedG8Checksum": context["g8Checksum"],
            "checks": _TRANSLATION_CHECKS,
            "qcFlags": ["none"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert second_review.status_code == 200, second_review.text
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    terminal = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "accept",
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert terminal.status_code == 200, terminal.text
    replay = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert replay.status_code == 200, replay.text
    assert replay.json()["state"] == "accepted"
    assert replay.json()["reviewedRegionCount"] == 2


def test_g9_remote_without_authorization_never_invokes_provider(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    calls = 0
    original = app.state.providers.translation

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.providers, "translation", counted)
    blocked = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "openai-compatible"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g9-remote-not-authorized"
    assert calls == 0


def test_g9_provider_interleave_fails_cas_without_candidate_publication(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    region_id = context["eligibleRegions"][0]["regionId"]
    provider_calls = 0

    class InterleavingProvider:
        name = "mock"

        def translate_text(self, text, context=(), **options):
            nonlocal provider_calls
            provider_calls += 1
            with store.session() as session:
                region = session.get(TextRegion, region_id)
                assert region is not None
                region.revision += 1
            return "你好"

    monkeypatch.setattr(
        app.state.providers,
        "translation",
        lambda provider_name, options: InterleavingProvider(),
    )
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "mock"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert provider_calls == 1
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "failed"
    with store.session() as session:
        assert session.scalar(select(func.count(RegionTranslationCandidate.id))) == 0
        assert session.scalar(select(func.count(RegionTranslationReview.id))) == 0
        assert session.scalar(select(func.count(PageTranslationReview.id))) == 0
        assert (
            session.scalar(
                select(func.count(PageLineageEvent.id)).where(
                    PageLineageEvent.generation_id == generation_id,
                    PageLineageEvent.operation == "translation-candidates-produced",
                )
            )
            == 0
        )


def test_g9_publication_crash_window_recovers_exactly_once(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "mock"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    claimed = app.state.queue._claim_next()
    assert claimed == (store, job_id)
    original_finish = app.state.queue._finish_item
    monkeypatch.setattr(app.state.queue, "_finish_item", lambda *args, **kwargs: None)
    asyncio.run(app.state.queue._execute(*claimed))
    with store.session() as session:
        item = session.scalar(select(JobItem).where(JobItem.job_id == job_id))
        assert item is not None and item.status == "running"
        item_id = item.id
        assert session.scalar(select(func.count(RegionTranslationCandidate.id))) == 1
        assert (
            session.scalar(
                select(func.count(PageLineageEvent.id)).where(
                    PageLineageEvent.operation == "translate-job-completed"
                )
            )
            == 0
        )
    recovered_output = app.state.queue._process_item(store, job_id, item_id)
    original_finish(store, job_id, item_id, output=recovered_output)
    repeated_output = app.state.queue._process_item(store, job_id, item_id)
    assert repeated_output == recovered_output
    with store.session() as session:
        assert session.scalar(select(func.count(RegionTranslationCandidate.id))) == 1
        assert (
            session.scalar(
                select(func.count(PageLineageEvent.id)).where(
                    PageLineageEvent.operation == "translation-candidates-produced"
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(PageLineageEvent.id)).where(
                    PageLineageEvent.operation == "translate-job-completed"
                )
            )
            == 1
        )


def test_g9_nonadjacent_duplicate_acceptance_has_reject_and_revise_path(
    client: TestClient, app, tmp_path: Path
) -> None:
    class DistinctOCR(_StrictLineageOCR):
        def recognize_region(self, image, region, *, direction, language):
            result = super().recognize_region(image, region, direction=direction, language=language)
            return OCRRegion(
                result.x,
                result.y,
                result.width,
                result.height,
                f"{result.text}{region['x']}",
                result.confidence,
                result.direction,
            )

    prepared = _prepare_g8_accepted_page(
        client,
        app,
        tmp_path,
        extra_dispositions=("translate", "translate"),
        ocr_provider=DistinctOCR(),
    )
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    translations = ["共同译文", "中间译文", "共同译文"]
    for index, translation in enumerate(translations):
        context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
        region = context["eligibleRegions"][index]
        created = client.post(
            f"/api/images/{image['id']}/page-gates/translation/candidates",
            json={
                "regionId": region["regionId"],
                "translationText": translation,
                "originKind": "agent",
                "observedG8Checksum": context["g8Checksum"],
                "observedSourceTextChecksum": region["sourceTextChecksum"],
                "observedContextChecksum": region["contextChecksum"],
                "observedTranslationStateChecksum": context["translationStateChecksum"],
                "expectedRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
        assert created.status_code == 200, created.text
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    first = context["candidates"][0]
    review_body = {
        "reason": "translation-reviewed",
        "observedCandidateChecksum": first["candidateChecksum"],
        "observedSourceTextChecksum": first["sourceTextChecksum"],
        "observedContextChecksum": first["contextChecksum"],
        "observedG8Checksum": context["g8Checksum"],
        "checks": _TRANSLATION_CHECKS,
        "qcFlags": ["none"],
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
    }
    blocked = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{first['candidateId']}",
        json=review_body | {"decision": "accept"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g9-generic-duplicate"
    rejected_checks = [dict(entry) for entry in _TRANSLATION_CHECKS]
    next(entry for entry in rejected_checks if entry["check"] == "generic-duplicate-checked")[
        "passed"
    ] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{first['candidateId']}",
        json=review_body
        | {
            "decision": "reject",
            "reason": "generic-duplicate",
            "checks": rejected_checks,
            "qcFlags": ["generic-duplicate"],
        },
    )
    assert rejected.status_code == 200, rejected.text
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    region = next(row for row in context["eligibleRegions"] if row["regionId"] == first["regionId"])
    revised = client.post(
        f"/api/images/{image['id']}/page-gates/translation/candidates",
        json={
            "regionId": region["regionId"],
            "translationText": "修订后的独特译文",
            "originKind": "agent",
            "observedG8Checksum": context["g8Checksum"],
            "observedSourceTextChecksum": region["sourceTextChecksum"],
            "observedContextChecksum": region["contextChecksum"],
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["candidates"][-1]["supersedesCandidateId"] == first["candidateId"]


def test_cloud_typeset_fk_upgrade_preserves_existing_row_and_is_idempotent(tmp_path):
    database_path = tmp_path / "legacy-typeset.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.execute(
            """
            CREATE TABLE page_typeset_candidates (
                id VARCHAR(36) PRIMARY KEY,
                generation_id VARCHAR(36) NOT NULL,
                image_id VARCHAR(36) NOT NULL,
                job_id VARCHAR(36) NOT NULL,
                job_item_id VARCHAR(36) NOT NULL,
                sequence INTEGER NOT NULL,
                parent_checksum VARCHAR(64) NOT NULL,
                g9_terminal_checksum VARCHAR(64) NOT NULL,
                translation_state_checksum VARCHAR(64) NOT NULL,
                clean_plate_candidate_id VARCHAR(36),
                clean_plate_checksum VARCHAR(64) NOT NULL,
                region_manifest JSON NOT NULL,
                route_manifest JSON NOT NULL,
                route_checksum VARCHAR(64) NOT NULL,
                style_manifest JSON NOT NULL,
                style_checksum VARCHAR(64) NOT NULL,
                layout_manifest JSON NOT NULL,
                layout_checksum VARCHAR(64) NOT NULL,
                provider VARCHAR(80) NOT NULL,
                model_version VARCHAR(128) NOT NULL,
                parameter_hash VARCHAR(64) NOT NULL,
                candidate_checksum VARCHAR(64) NOT NULL,
                relative_path TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                render_scale FLOAT NOT NULL,
                overflow_region_ids JSON NOT NULL,
                anomalies JSON NOT NULL,
                revision_id VARCHAR(36) NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        values = (
            "legacy-row",
            "generation",
            "image",
            "job",
            "item",
            1,
            *(["a" * 64] * 3),
            None,
            "a" * 64,
            "[]",
            "[]",
            "a" * 64,
            "[]",
            "a" * 64,
            "[]",
            "a" * 64,
            "legacy",
            "legacy-v1",
            "a" * 64,
            "a" * 64,
            "generated/legacy.png",
            100,
            200,
            1.0,
            "[]",
            "[]",
            "revision",
            "2026-01-01 00:00:00",
        )
        database.execute(
            f"INSERT INTO page_typeset_candidates VALUES ({','.join('?' for _ in values)})",
            values,
        )

    for _ in range(2):
        engine = create_project_engine(database_path)
        engine.dispose()
    with sqlite3.connect(database_path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(page_typeset_candidates)")}
        foreign_keys = list(database.execute("PRAGMA foreign_key_list(page_typeset_candidates)"))
        row = database.execute(
            "SELECT id, cloud_full_page_candidate_id FROM page_typeset_candidates"
        ).fetchone()
    assert "cloud_full_page_candidate_id" in columns
    assert any(
        foreign_key[2] == "page_cloud_full_page_candidates"
        and foreign_key[3] == "cloud_full_page_candidate_id"
        and foreign_key[6] == "RESTRICT"
        for foreign_key in foreign_keys
    )
    assert row == ("legacy-row", None)


def test_g9_forbidden_template_hard_fails_and_replay_rejects_sequence_tamper(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    eligible = context["eligibleRegions"][0]
    revision_body = {
        "regionId": eligible["regionId"],
        "translationText": "联系我们",
        "originKind": "agent",
        "observedG8Checksum": context["g8Checksum"],
        "observedSourceTextChecksum": eligible["sourceTextChecksum"],
        "observedContextChecksum": eligible["contextChecksum"],
        "observedTranslationStateChecksum": context["translationStateChecksum"],
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
    }
    before_stale = _counts(store)
    stale = client.post(
        f"/api/images/{image['id']}/page-gates/translation/candidates",
        json=revision_body | {"expectedRevision": context["imageRevision"] - 1},
    )
    assert stale.status_code == 409, stale.text
    assert _counts(store) == before_stale
    revised = client.post(
        f"/api/images/{image['id']}/page-gates/translation/candidates",
        json=revision_body,
    )
    assert revised.status_code == 200, revised.text
    context = revised.json()
    candidate = context["candidates"][0]
    assert candidate["computedQcFlags"] == ["forbidden-template"]
    hard_fail = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{candidate['candidateId']}",
        json={
            "decision": "accept",
            "reason": "translation-reviewed",
            "observedCandidateChecksum": candidate["candidateChecksum"],
            "observedSourceTextChecksum": candidate["sourceTextChecksum"],
            "observedContextChecksum": candidate["contextChecksum"],
            "observedG8Checksum": context["g8Checksum"],
            "checks": _TRANSLATION_CHECKS,
            "qcFlags": ["none"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert hard_fail.status_code == 409
    assert hard_fail.json()["detail"]["reason"] == "g9-hard-qc-failed"
    with sqlite3.connect(store.database_path) as database:
        database.execute("DROP TRIGGER region_translation_candidates_no_update")
        database.execute(
            "UPDATE region_translation_candidates SET sequence = sequence + 1 WHERE id = ?",
            (candidate["candidateId"],),
        )
        database.commit()
    tampered = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert tampered.status_code == 409
    assert tampered.json()["detail"]["reason"] == "g9-replay-invalid"


def test_g9_translate_freezes_canonical_provider_metadata_and_blocks_dictionary_jobs(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)

    blocked = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "dictionary", "dictionary": {"候補": "候选"}},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g9-dictionary-job-blocked"

    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {
                "provider": "local-nmt",
                "modelVersion": "forged-client-version",
            },
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    with store.session() as session:
        job = session.get(Job, queued.json()["id"])
        assert job is not None
        assert job.options["provider"] == "argos-ja-zh"
        assert job.options["modelVersion"] == "argos-ja-zh-local-v1"
        assert job.options["modelVersion"] != "forged-client-version"


@pytest.mark.parametrize("tamper_kind", ["candidate-matrix", "invalid-actor", "terminal-matrix"])
def test_g9_exact_event_replay_rejects_isolated_raw_sql_tamper(
    client: TestClient, app, tmp_path: Path, tamper_kind: str
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    created = _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=context,
        region=context["eligibleRegions"][0],
        translation_text="合规译文",
    )
    candidate = created["candidates"][0]
    if tamper_kind == "terminal-matrix":
        reviewed = client.patch(
            f"/api/images/{image['id']}/page-gates/translation/candidates/"
            f"{candidate['candidateId']}",
            json={
                "decision": "accept",
                "reason": "translation-reviewed",
                "observedCandidateChecksum": candidate["candidateChecksum"],
                "observedSourceTextChecksum": candidate["sourceTextChecksum"],
                "observedContextChecksum": candidate["contextChecksum"],
                "observedG8Checksum": created["g8Checksum"],
                "checks": _TRANSLATION_CHECKS,
                "qcFlags": ["none"],
                "expectedRevision": created["imageRevision"],
                "lineage": _mutation_lineage(generation_id, created["nextSequence"]),
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        terminal_context_response = client.get(f"/api/images/{image['id']}/page-gates/translation")
        assert terminal_context_response.status_code == 200, terminal_context_response.text
        terminal_context = terminal_context_response.json()
        terminal = client.patch(
            f"/api/images/{image['id']}/page-gates/translation",
            json={
                "decision": "accept",
                "observedTranslationStateChecksum": terminal_context["translationStateChecksum"],
                "expectedRevision": terminal_context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, terminal_context["nextSequence"]),
            },
        )
        assert terminal.status_code == 200, terminal.text
    with sqlite3.connect(store.database_path) as database:
        database.execute("DROP TRIGGER page_lineage_events_no_update")
        operation = (
            "translation-stage-review"
            if tamper_kind == "terminal-matrix"
            else "translation-candidate-revised"
        )
        if tamper_kind == "invalid-actor":
            database.execute(
                """
                UPDATE page_lineage_events
                SET actor_kind = ?, operation_source = ?, task_id = NULL,
                    thread_id = NULL, session_id = NULL
                WHERE generation_id = ? AND operation = ?
                """,
                ("evil", "evil", generation_id, operation),
            )
        else:
            database.execute(
                """
                UPDATE page_lineage_events SET decision = ?, evidence = ?
                WHERE generation_id = ? AND operation = ?
                """,
                ("forged-decision", json.dumps({"forged": True}), generation_id, operation),
            )
        database.commit()
    replay = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g9-replay-invalid"


@pytest.mark.parametrize("tamper_kind", ["context-policy", "linked-revision"])
def test_g9_replay_rejects_candidate_contract_or_revision_tamper(
    client: TestClient, app, tmp_path: Path, tamper_kind: str
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    created = _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=context,
        region=context["eligibleRegions"][0],
        translation_text="上下文绑定译文",
    )
    candidate = created["candidates"][0]
    with sqlite3.connect(store.database_path) as database:
        if tamper_kind == "context-policy":
            database.execute("DROP TRIGGER region_translation_candidates_no_update")
            database.execute(
                "UPDATE region_translation_candidates SET context_policy = ? WHERE id = ?",
                (json.dumps({"targetLanguage": "zh-CN", "forged": True}), candidate["candidateId"]),
            )
        else:
            database.execute("DROP TRIGGER revisions_g9_no_update")
            database.execute(
                """
                UPDATE revisions SET after = ?
                WHERE id = (SELECT revision_id FROM region_translation_candidates WHERE id = ?)
                """,
                (
                    json.dumps({"candidateId": candidate["candidateId"], "forged": True}),
                    candidate["candidateId"],
                ),
            )
        database.commit()
    replay = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g9-replay-invalid"


def test_g9_rejection_reason_must_match_defect_without_persisting_bad_review(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    created = _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=context,
        region=context["eligibleRegions"][0],
        translation_text="联系我们",
    )
    candidate = created["candidates"][0]
    failed_checks = [dict(entry) for entry in _TRANSLATION_CHECKS]
    next(entry for entry in failed_checks if entry["check"] == "forbidden-template-checked")[
        "passed"
    ] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/translation/candidates/{candidate['candidateId']}",
        json={
            "decision": "reject",
            "reason": "non-chinese-output",
            "observedCandidateChecksum": candidate["candidateChecksum"],
            "observedSourceTextChecksum": candidate["sourceTextChecksum"],
            "observedContextChecksum": candidate["contextChecksum"],
            "observedG8Checksum": created["g8Checksum"],
            "checks": failed_checks,
            "qcFlags": ["forbidden-template"],
            "expectedRevision": created["imageRevision"],
            "lineage": _mutation_lineage(generation_id, created["nextSequence"]),
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["reason"] == "g9-rejection-invalid"
    with store.session() as session:
        assert session.scalar(select(func.count(RegionTranslationReview.id))) == 0
    readable = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert readable.status_code == 200, readable.text
    assert readable.json()["candidates"][0]["review"] is None


def test_g9_chinese_ratio_ignores_punctuation_but_rejects_punctuation_only(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path, extra_dispositions=("translate",))
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    first = _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=context,
        region=context["eligibleRegions"][0],
        translation_text="好？！……",  # noqa: RUF001 - intentional CJK punctuation fixture
    )
    assert first["candidates"][0]["computedQcFlags"] == ["none"]
    second = _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=first,
        region=first["eligibleRegions"][1],
        translation_text="？！……",  # noqa: RUF001 - intentional CJK punctuation fixture
    )
    assert second["candidates"][-1]["computedQcFlags"] == ["non-chinese-output"]


def test_g9_replay_rejects_unaccepted_compatibility_projection_tamper(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    created = _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=context,
        region=context["eligibleRegions"][0],
        translation_text="尚未接受的译文",
    )
    candidate = created["candidates"][0]
    with sqlite3.connect(store.database_path) as database:
        database.execute(
            """
            UPDATE text_regions
            SET translation_text = ?, translation_provider = ?
            WHERE id = ?
            """,
            (candidate["translationText"], candidate["provider"], candidate["regionId"]),
        )
        database.commit()
    replay = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g9-replay-invalid"


def test_g9_replay_rejects_downstream_event_before_translation_terminal(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    _create_g9_candidate(
        client,
        image_id=str(image["id"]),
        generation_id=generation_id,
        context=context,
        region=context["eligibleRegions"][0],
        translation_text="等待审阅的译文",
    )
    generation = client.get(f"/api/images/{image['id']}/page-generations").json()[0]
    with sqlite3.connect(store.database_path) as database:
        database.execute(
            """
            INSERT INTO page_lineage_events (
                id, generation_id, sequence, operation, gate, state, actor_kind,
                task_id, thread_id, session_id, operation_source, parent_checksum,
                stage, reason, evidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid.uuid4()),
                generation_id,
                generation["nextSequence"],
                "typesetting-started",
                "G10_typesetting",
                "pending",
                _ACTOR["actorKind"],
                _ACTOR["taskId"],
                _ACTOR["threadId"],
                _ACTOR["sessionId"],
                _ACTOR["operationSource"],
                context["g8Checksum"],
                "typesetting",
                "downstream-started",
                json.dumps({}),
            ),
        )
        database.execute(
            "UPDATE page_generations SET next_sequence = next_sequence + 1 WHERE id = ?",
            (generation_id,),
        )
        database.commit()
    replay = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g9-replay-invalid"


def test_g9_provider_runtime_identity_mismatch_fails_before_translation_call(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "mock"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    calls = 0

    class MismatchedProvider:
        name = "argos-ja-zh"

        def translate_text(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return "不应调用"

    monkeypatch.setattr(
        app.state.providers,
        "translation",
        lambda _provider_name, _options: MismatchedProvider(),
    )
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "failed"
    assert calls == 0
    with store.session() as session:
        assert session.scalar(select(func.count(RegionTranslationCandidate.id))) == 0


def test_g9_manual_revision_is_zero_write_while_automatic_job_is_active(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    initial = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    queued = client.post(
        f"/api/projects/{project['id']}/translate",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "mock"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    after_enqueue = _counts(store)
    blocked = client.post(
        f"/api/images/{image['id']}/page-gates/translation/candidates",
        json={
            "regionId": initial["eligibleRegions"][0]["regionId"],
            "translationText": "不得插队的人工译文",
            "originKind": "agent",
            "observedG8Checksum": initial["g8Checksum"],
            "observedSourceTextChecksum": initial["eligibleRegions"][0]["sourceTextChecksum"],
            "observedContextChecksum": initial["eligibleRegions"][0]["contextChecksum"],
            "observedTranslationStateChecksum": initial["translationStateChecksum"],
            "expectedRevision": initial["imageRevision"],
            "lineage": _mutation_lineage(generation_id, initial["nextSequence"] + 1),
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g9-translation-job-active"
    assert _counts(store) == after_enqueue
    readable = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert readable.status_code == 200, readable.text
    assert readable.json()["candidates"] == []
    with store.session() as session:
        assert session.scalar(select(func.count(RegionTranslationCandidate.id))) == 0
        assert session.scalar(select(func.count(RegionTranslationReview.id))) == 0


def test_g8_outside_mask_count_is_rgba_exact() -> None:
    source = Image.new("RGBA", (3, 3), (10, 20, 30, 40))
    mask = Image.new("L", source.size, 0)
    rgb_changed = source.copy()
    rgb_changed.putpixel((0, 0), (11, 20, 30, 40))
    assert clean_plate_service._outside_mask_change_count(source, rgb_changed, mask) == 1
    alpha_changed = source.copy()
    alpha_changed.putpixel((0, 0), (10, 20, 30, 41))
    assert clean_plate_service._outside_mask_change_count(source, alpha_changed, mask) == 1
    mask.putpixel((0, 0), 1)
    inside_changed = source.copy()
    inside_changed.putpixel((0, 0), (99, 99, 99, 99))
    assert clean_plate_service._outside_mask_change_count(source, inside_changed, mask) == 0


def test_g8_duplicate_workers_cannot_overwrite_published_candidate(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    item_id = queued.json()["items"][0]["id"]
    assert app.state.queue._claim_next() == (store, job_id)
    assert app.state.queue._begin_item(store, job_id, item_id)
    counter = 0
    nested_outputs: list[dict[str, object]] = []
    original_render = clean_plate_service._render_candidate

    def racing_render(**kwargs):
        nonlocal counter
        variant = counter
        counter += 1
        payload, width, height, outside, anomalies = original_render(**kwargs)
        if variant == 0:
            nested_outputs.append(app.state.queue._process_item(store, job_id, item_id))
        with Image.open(io.BytesIO(payload)) as candidate_image:
            candidate = candidate_image.convert("RGB")
        with Image.open(io.BytesIO(kwargs["mask_bytes"])) as mask_image:
            mask = np.asarray(mask_image.convert("L"), dtype=np.uint8)
        target_y, target_x = np.argwhere(mask > 0)[0]
        candidate.putpixel(
            (int(target_x), int(target_y)),
            (80 + variant, 80 + variant, 80 + variant),
        )
        output = io.BytesIO()
        candidate.save(output, format="PNG", optimize=False)
        return output.getvalue(), width, height, outside, anomalies

    monkeypatch.setattr(clean_plate_service, "_render_candidate", racing_render)
    outer_output = app.state.queue._process_item(store, job_id, item_id)
    outputs = [*nested_outputs, outer_output]
    assert len({output["candidateChecksum"] for output in outputs}) == 1
    with store.session() as session:
        rows = list(session.scalars(select(PageCleanPlateCandidate)).all())
        assert len(rows) == 1
        row = rows[0]
        candidate_path = store.root / row.relative_path
        assert _checksum(candidate_path.read_bytes()) == row.candidate_checksum
    app.state.queue._finish_item(store, job_id, item_id, output=outputs[0])
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate")
    assert context.status_code == 200, context.text
    assert context.json()["candidates"][0]["completed"] is True


def test_g8_candidate_validator_recomputes_outside_mask_pixels(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    with store.session() as session:
        row = session.scalar(select(PageCleanPlateCandidate))
        assert row is not None
        target = store.root / row.relative_path
        mask = session.get(PageMaskArtifact, row.mask_artifact_id)
        assert mask is not None
        with Image.open(store.root / mask.relative_path) as opened:
            mask_grid = np.asarray(opened.convert("L"), dtype=np.uint8)
        outside_y, outside_x = np.argwhere(mask_grid == 0)[0]
        with Image.open(target) as opened:
            tampered = opened.convert("RGBA")
        old = tampered.getpixel((int(outside_x), int(outside_y)))
        tampered.putpixel(
            (int(outside_x), int(outside_y)),
            ((old[0] + 1) % 256, old[1], old[2], (old[3] + 1) % 256),
        )
        tampered.save(target, format="PNG", optimize=False)
        forged = SimpleNamespace(
            id=row.id,
            generation_id=row.generation_id,
            image_id=row.image_id,
            mask_artifact_id=row.mask_artifact_id,
            mask_checksum=row.mask_checksum,
            quality_checksum=row.quality_checksum,
            relative_path=row.relative_path,
            outside_mask_change_count=0,
            candidate_checksum=_checksum(target.read_bytes()),
            width=row.width,
            height=row.height,
        )
        with pytest.raises(page_lineage.PageLineageConflict, match="outside its accepted mask"):
            clean_plate_service._validate_candidate_file(store, forged, session=session)


def test_g8_deterministic_candidate_is_immutable_reviewed_and_consumable(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g7_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    completed = app.state.queue.get_job(store, queued.json()["id"])
    assert completed.status == "completed"

    response = client.get(f"/api/images/{image['id']}/page-gates/clean-plate")
    assert response.status_code == 200, response.text
    context = response.json()
    assert context["state"] == "pending"
    assert context["g7Checksum"] == prepared["acceptedG7"]["event"]["outputChecksum"]
    assert context["fallbackEnabled"] is False
    assert context["fallbackAllowed"] is False
    candidate = context["candidates"][0]
    assert candidate["completed"] is True
    assert candidate["outsideMaskChangeCount"] == 0
    assert candidate["originKind"] == "deterministic"
    assert candidate["routeManifest"][0]["route"] == "deterministic-solid"
    assert candidate["routeManifest"][0]["modelVersion"] == "boundary-median-solid-v1"
    served = client.get(
        f"/api/images/{image['id']}/page-gates/clean-plate/candidates/{candidate['candidateId']}"
    )
    assert served.status_code == 200, served.text
    assert served.headers["cache-control"] == "private, no-store"
    assert _checksum(served.content) == candidate["candidateChecksum"]

    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "accept",
            "reason": "clean-plate-complete",
            "candidateId": candidate["candidateId"],
            "observedCandidateChecksum": candidate["candidateChecksum"],
            "observedWidth": candidate["width"],
            "observedHeight": candidate["height"],
            "checks": _CLEAN_PLATE_CHECKS,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    final_context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert final_context["state"] == "accepted"
    assert final_context["acceptedCandidateId"] == candidate["candidateId"]
    assert accepted.json()["event"]["outputChecksum"] == final_context["cleanPlateStateChecksum"]
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        state, path, selected = require_current_clean_plate_acceptance(
            store, session, image_row, generation
        )
        assert state == final_context["cleanPlateStateChecksum"]
        assert _checksum(path.read_bytes()) == candidate["candidateChecksum"]
        assert selected is not None and selected.id == candidate["candidateId"]
        assert session.scalar(select(func.count()).select_from(PageCleanPlateCandidate)) == 1
        assert session.scalar(select(func.count()).select_from(PageCleanPlateReview)) == 1
        before_counts = {
            "events": session.scalar(select(func.count()).select_from(PageLineageEvent)),
            "revisions": session.scalar(select(func.count()).select_from(Revision)),
        }

    blocked_legacy_review = client.patch(
        f"/api/images/{image['id']}/stage-reviews/inpaint",
        json={
            "state": "pending",
            "expectedRevision": final_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, final_context["nextSequence"]),
        },
    )
    assert blocked_legacy_review.status_code == 409, blocked_legacy_review.text
    assert blocked_legacy_review.json()["detail"]["reason"] == ("g8-legacy-stage-review-blocked")
    after_block = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert after_block == final_context
    with store.session() as session:
        assert {
            "events": session.scalar(select(func.count()).select_from(PageLineageEvent)),
            "revisions": session.scalar(select(func.count()).select_from(Revision)),
        } == before_counts


def test_g8_zero_eligible_page_requires_artifact_free_not_applicable(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(
        client,
        app,
        tmp_path,
        disposition="keep-art",
        extra_dispositions=("ignore",),
        include_ruby=True,
    )
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    mask_context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert mask_context["eligibleRegionIds"] == []
    g7 = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "not-applicable",
            "reason": "no-eligible-regions",
            "selectedArtifactId": None,
            "observedMaskChecksum": None,
            "coverageChecks": [],
            "collateralChecks": [],
            "expectedRevision": mask_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, mask_context["nextSequence"]),
        },
    )
    assert g7.status_code == 200, g7.text
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert context["maskArtifactId"] is None
    assert context["candidates"] == []
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "not-applicable",
            "reason": "no-clean-plate-required",
            "candidateId": None,
            "observedCandidateChecksum": None,
            "observedWidth": None,
            "observedHeight": None,
            "checks": [],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    final_context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert final_context["state"] == "not-applicable"
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        state, path, candidate = require_current_clean_plate_acceptance(
            store, session, image_row, generation
        )
        assert state == final_context["cleanPlateStateChecksum"]
        assert _checksum(path.read_bytes()) == prepared["qualityChecksum"]
        assert candidate is None
    g9_context_response = client.get(f"/api/images/{image['id']}/page-gates/translation")
    assert g9_context_response.status_code == 200, g9_context_response.text
    g9_context = g9_context_response.json()
    assert g9_context["eligibleRegions"] == []
    assert g9_context["candidates"] == []
    assert g9_context["translationStateChecksum"] == g9_context["g8Checksum"]
    g9_terminal = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "not-applicable",
            "observedTranslationStateChecksum": g9_context["translationStateChecksum"],
            "expectedRevision": g9_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, g9_context["nextSequence"]),
        },
    )
    assert g9_terminal.status_code == 200, g9_terminal.text
    g9_final = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    assert g9_final["state"] == "not-applicable"
    assert g9_final["terminalChecksum"] == g9_terminal.json()["event"]["outputChecksum"]


class _G8FakeLama:
    name = "lama-onnx"

    def __init__(self) -> None:
        self.calls = 0

    def inpaint(self, image, mask, **_options):
        self.calls += 1
        source = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
        target = np.asarray(Image.fromarray(mask).convert("L"), dtype=np.uint8) > 0
        source[target] = [210, 210, 210]
        return Image.fromarray(source, mode="RGB")


class _G8RecordingLama(_G8FakeLama):
    def __init__(self) -> None:
        super().__init__()
        self.masks: list[np.ndarray] = []

    def inpaint(self, image, mask, **options):
        self.masks.append(np.asarray(Image.fromarray(mask).convert("L"), dtype=np.uint8).copy())
        return super().inpaint(image, mask, **options)


def _g8_render_fixture(
    mask: np.ndarray,
    *,
    strategy: str | None,
    second_parameter_hash: str = "a" * 64,
) -> tuple[_G8RecordingLama, tuple[bytes, int, int, int, list[str]]]:
    source = Image.new("RGB", (mask.shape[1], mask.shape[0]), (32, 32, 32))
    rows = [
        SimpleNamespace(id="region-a", x=1, y=1, width=3, height=4),
        SimpleNamespace(id="region-b", x=4, y=1, width=3, height=4),
    ]
    manifest = [
        {
            "regionId": "region-a",
            "backgroundCategory": "complex-lineart",
            "route": "ai-inpaint-redraw",
            "originKind": "ai",
            "provider": "lama-onnx",
            "modelVersion": "lama-onnx-local-v1",
            "parameterHash": "a" * 64,
        },
        {
            "regionId": "region-b",
            "backgroundCategory": "complex-lineart",
            "route": "ai-inpaint-redraw",
            "originKind": "ai",
            "provider": "lama-onnx",
            "modelVersion": "lama-onnx-local-v1",
            "parameterHash": second_parameter_hash,
        },
    ]
    fake = _G8RecordingLama()
    result = clean_plate_service._render_candidate(
        quality_bytes=clean_plate_service._png_bytes(source),
        mask_bytes=clean_plate_service._png_bytes(Image.fromarray(mask, mode="L")),
        rows=rows,
        manifest=manifest,
        normalized={
            "contextPadding": 0,
            "inferencePadding": 0,
            "radius": 1.0,
            "ownerMaskStrategy": strategy,
        },
        scale=1,
        inpainter=lambda _name: fake,
    )
    return fake, result


def test_g8_connected_owner_masks_with_one_execution_contract_render_once() -> None:
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[2:5, 2:7] = 255
    fake, (_payload, width, height, outside, anomalies) = _g8_render_fixture(
        mask,
        strategy="connected-contract-union-v1",
    )
    assert fake.calls == 1
    assert len(fake.masks) == 1 and np.array_equal(fake.masks[0], mask)
    assert (width, height, outside, anomalies) == (8, 8, 0, [])


def test_g8_union_strategy_keeps_disconnected_or_distinct_contract_masks_separate() -> None:
    disconnected = np.zeros((8, 8), dtype=np.uint8)
    disconnected[2:4, 1:3] = 255
    disconnected[2:4, 5:7] = 255
    fake, (_payload, _width, _height, outside, _anomalies) = _g8_render_fixture(
        disconnected,
        strategy="connected-contract-union-v1",
    )
    assert fake.calls == 2
    assert outside == 0

    connected = np.zeros((8, 8), dtype=np.uint8)
    connected[2:5, 2:7] = 255
    fake, (_payload, _width, _height, outside, _anomalies) = _g8_render_fixture(
        connected,
        strategy="connected-contract-union-v1",
        second_parameter_hash="b" * 64,
    )
    assert fake.calls == 2
    assert outside == 0


def test_g8_legacy_jobs_keep_sequential_owner_rendering() -> None:
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[2:5, 2:7] = 255
    fake, (_payload, _width, _height, outside, _anomalies) = _g8_render_fixture(
        mask,
        strategy=None,
    )
    assert fake.calls == 2
    assert outside == 0


@pytest.mark.parametrize(
    ("category", "expected_fill"),
    [("white-solid", np.array([255, 255, 255])), ("black-solid", np.array([0, 0, 0]))],
)
def test_g8_classified_solid_fill_uses_declared_color_only_inside_mask(
    category: str,
    expected_fill: np.ndarray,
) -> None:
    source = np.full((8, 8, 3), [31, 97, 163], dtype=np.uint8)
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[3, 3] = 255
    mask[4, 4] = 128
    rows = [SimpleNamespace(id="region-a", x=2, y=2, width=4, height=4)]
    manifest = [
        {
            "regionId": "region-a",
            "backgroundCategory": category,
            "route": "deterministic-solid",
            "originKind": "deterministic",
            "provider": "opencv",
            "modelVersion": "classified-solid-v1",
            "parameterHash": "a" * 64,
        }
    ]

    payload, width, height, outside, anomalies = clean_plate_service._render_candidate(
        quality_bytes=clean_plate_service._png_bytes(Image.fromarray(source, mode="RGB")),
        mask_bytes=clean_plate_service._png_bytes(Image.fromarray(mask, mode="L")),
        rows=rows,
        manifest=manifest,
        normalized={
            "contextPadding": 0,
            "inferencePadding": 0,
            "radius": 1.0,
            "ownerMaskStrategy": None,
            "solidFillStrategy": "classified-color",
        },
        scale=1,
        inpainter=lambda _name: pytest.fail("classified solid fill must not use an inpainter"),
    )
    rendered = np.asarray(Image.open(io.BytesIO(payload)).convert("RGB"), dtype=np.uint8)

    assert np.array_equal(rendered[3, 3], expected_fill)
    alpha = 128 / 255
    expected_blend = np.rint(source[4, 4] * (1.0 - alpha) + expected_fill * alpha).astype(np.uint8)
    assert np.array_equal(rendered[4, 4], expected_blend)
    assert np.array_equal(rendered[mask == 0], source[mask == 0])
    assert (width, height, outside, anomalies) == (8, 8, 0, [])


@pytest.mark.parametrize(
    ("category", "expected_fill"),
    [("white-solid", np.array([255, 255, 255])), ("black-solid", np.array([0, 0, 0]))],
)
def test_g8_classified_solid_opaque_mask_removes_feathered_source_pixels(
    category: str,
    expected_fill: np.ndarray,
) -> None:
    source = np.full((8, 8, 3), [31, 97, 163], dtype=np.uint8)
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[3, 3] = 255
    mask[4, 4] = 1
    rows = [SimpleNamespace(id="region-a", x=2, y=2, width=4, height=4)]
    manifest = [
        {
            "regionId": "region-a",
            "backgroundCategory": category,
            "route": "deterministic-solid",
            "originKind": "deterministic",
            "provider": "opencv",
            "modelVersion": "classified-solid-v2",
            "parameterHash": "a" * 64,
        }
    ]

    payload, width, height, outside, anomalies = clean_plate_service._render_candidate(
        quality_bytes=clean_plate_service._png_bytes(Image.fromarray(source, mode="RGB")),
        mask_bytes=clean_plate_service._png_bytes(Image.fromarray(mask, mode="L")),
        rows=rows,
        manifest=manifest,
        normalized={
            "contextPadding": 0,
            "inferencePadding": 0,
            "radius": 1.0,
            "ownerMaskStrategy": None,
            "solidFillStrategy": "classified-color-opaque-mask",
        },
        scale=1,
        inpainter=lambda _name: pytest.fail("classified solid fill must not use an inpainter"),
    )
    rendered = np.asarray(Image.open(io.BytesIO(payload)).convert("RGB"), dtype=np.uint8)

    assert np.array_equal(rendered[mask > 0], np.broadcast_to(expected_fill, (2, 3)))
    assert np.array_equal(rendered[mask == 0], source[mask == 0])
    assert (width, height, outside, anomalies) == (8, 8, 0, [])


def test_g8_rejects_unknown_solid_fill_strategy() -> None:
    with pytest.raises(ProjectError, match="solidFillStrategy is unsupported"):
        clean_plate_service._supported_options({"solidFillStrategy": "guess"})


def test_g8_classified_solid_route_has_distinct_audit_identity(
    client: TestClient,
    app,
    tmp_path: Path,
) -> None:
    prepared = _prepare_g7_accepted_page(
        client,
        app,
        tmp_path,
        background_category="white-solid",
    )
    store = prepared["store"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        _background, _quality, default_manifest, default_normalized = (
            clean_plate_service._route_manifest(
                store,
                session,
                image_row,
                generation,
                options={},
                fallback_enabled=False,
            )
        )
        _background, _quality, classified_manifest, classified_normalized = (
            clean_plate_service._route_manifest(
                store,
                session,
                image_row,
                generation,
                options={"solidFillStrategy": "classified-color"},
                fallback_enabled=False,
            )
        )
        _background, _quality, opaque_manifest, opaque_normalized = (
            clean_plate_service._route_manifest(
                store,
                session,
                image_row,
                generation,
                options={"solidFillStrategy": "classified-color-opaque-mask"},
                fallback_enabled=False,
            )
        )

    assert default_manifest[0]["modelVersion"] == "boundary-median-solid-v1"
    assert classified_manifest[0]["modelVersion"] == "classified-solid-v1"
    assert default_manifest[0]["parameterHash"] == clean_plate_service._digest(
        {"ringRadius": 16, "renderScaleAppliedAtRuntime": True}
    )
    assert classified_manifest[0]["parameterHash"] == clean_plate_service._digest(
        {
            "backgroundCategory": "white-solid",
            "fillRgb": [255, 255, 255],
            "solidFillStrategy": "classified-color",
        }
    )
    assert opaque_manifest[0]["modelVersion"] == "classified-solid-v2"
    assert opaque_manifest[0]["parameterHash"] == clean_plate_service._digest(
        {
            "backgroundCategory": "white-solid",
            "fillRgb": [255, 255, 255],
            "solidFillStrategy": "classified-color-opaque-mask",
            "maskApplication": "opaque-nonzero-support",
        }
    )
    assert default_normalized["routeChecksum"] != classified_normalized["routeChecksum"]
    assert classified_normalized["routeChecksum"] != opaque_normalized["routeChecksum"]


def test_g8_lama_alias_uses_canonical_real_registry_identity(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g7_accepted_page(
        client, app, tmp_path, background_category="complex-lineart"
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    fake = _G8FakeLama()
    monkeypatch.setattr(app.state.providers, "lama", fake)

    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {
                "provider": "lama",
                "ownerMaskStrategy": "legacy-sequential-v1",
            },
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    with store.session() as session:
        queued_row = session.get(Job, queued.json()["id"])
        assert queued_row is not None
        assert queued_row.options["ownerMaskStrategy"] == "connected-contract-union-v1"
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))

    assert fake.calls == 1
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    candidate = context["candidates"][0]
    assert candidate["providerIds"] == ["lama-onnx"]
    assert candidate["routeManifest"][0]["provider"] == "lama-onnx"
    assert candidate["routeManifest"][0]["parameterHash"] == clean_plate_service._digest(
        {
            "contextPadding": 64,
            "inferencePadding": 32,
            "ownerMaskStrategy": "connected-contract-union-v1",
            "renderScaleAppliedAtRuntime": True,
        }
    )
    assert candidate["completed"] is True


def test_g8_replay_rejects_fallback_enabled_before_ai_rejection(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g7_accepted_page(
        client, app, tmp_path, background_category="complex-lineart"
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    fake = _G8FakeLama()
    original_resolver = app.state.providers.inpainter
    app.state.providers.inpainter = lambda name: (
        fake if name == "lama-onnx" else original_resolver(name)
    )
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "lama"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        replay = clean_plate_service._g8_replay(store, session, image_row, generation)
        assert replay["candidates"] and replay["reviews"] == []
        reason = "all-ai-candidates-rejected"
        image_row.revision += 1
        revision = add_revision(
            session,
            store.project(session),
            entity_type="page-clean-plate-fallback",
            entity_id=generation.id,
            operation="enable",
            before={"enabled": False},
            after={"enabled": True},
        )
        session.flush()
        provisional = PageLineageEvent(
            generation_id=generation.id,
            sequence=generation.next_sequence,
            operation="clean-plate-fallback-enabled",
            state="pending",
            actor_kind="codex",
            task_id=_ACTOR["taskId"],
            thread_id=_ACTOR["threadId"],
            session_id=_ACTOR["sessionId"],
            operation_source="api",
            reason=reason,
            evidence={},
        )
        after_state = clean_plate_service._state_checksum(
            replay["g7Checksum"],
            replay["backgroundChecksum"],
            replay["qualityChecksum"],
            replay["maskArtifact"].id,
            replay["maskArtifact"].mask_checksum,
            replay["candidates"],
            replay["reviews"],
            [*replay["fallbackEvents"], provisional],
        )
        session.add(
            PageLineageEvent(
                generation_id=generation.id,
                sequence=generation.next_sequence,
                operation="clean-plate-fallback-enabled",
                gate="G8_cleanPlate",
                state="pending",
                actor_kind="codex",
                task_id=_ACTOR["taskId"],
                thread_id=_ACTOR["threadId"],
                session_id=_ACTOR["sessionId"],
                operation_source="api",
                input_checksum=replay["stateChecksum"],
                output_checksum=after_state,
                parent_checksum=replay["g7Checksum"],
                stage="inpaint",
                provider="operator",
                model_version="page-scoped-fallback-v1",
                parameter_hash=clean_plate_service._digest({"enabled": True, "reason": reason}),
                revision_id=revision.id,
                decision="classical-fallback-enabled",
                reason=reason,
                evidence={
                    "eventType": "clean-plate-fallback-enabled",
                    "qualityState": "pending-review",
                    "enabled": True,
                    "candidateCount": len(replay["candidates"]),
                    "aiCandidateCount": len(replay["candidates"]),
                    "imageRevision": image_row.revision,
                },
            )
        )
        generation.next_sequence += 1
    response = client.get(f"/api/images/{image['id']}/page-gates/clean-plate")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "g8-clean-plate-replay-invalid"


def test_g8_replay_binds_candidate_provenance_to_its_enqueue_route(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g7_accepted_page(
        client, app, tmp_path, background_category="complex-lineart"
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    fake = _G8FakeLama()
    original_resolver = app.state.providers.inpainter
    app.state.providers.inpainter = lambda name: (
        fake if name == "lama-onnx" else original_resolver(name)
    )
    queued = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "lama"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    with store.session() as session:
        connection = session.connection()
        for trigger in (
            "page_clean_plate_candidates_no_update",
            "page_lineage_events_no_update",
            "revisions_g8_no_update",
        ):
            connection.exec_driver_sql(f"DROP TRIGGER {trigger}")
        row = session.scalar(
            select(PageCleanPlateCandidate).where(
                PageCleanPlateCandidate.generation_id == generation_id
            )
        )
        produced = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.operation == "clean-plate-candidate-produced",
            )
        )
        completed = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.operation == "inpaint-job-completed",
            )
        )
        assert row is not None and produced is not None and completed is not None
        revision = session.get(Revision, produced.revision_id)
        assert revision is not None
        manifest = json.loads(json.dumps(row.route_manifest))
        manifest[0].update(
            {
                "route": "classical-fallback",
                "originKind": "classical",
                "provider": "opencv",
                "modelVersion": "telea-v1",
                "parameterHash": clean_plate_service._digest({"radius": 3.0}),
            }
        )
        route_checksum = clean_plate_service._digest(
            {"forgedAgainstEnqueue": True, "routes": manifest}
        )
        row.route_manifest = manifest
        row.route_checksum = route_checksum
        row.origin_kind = "classical"
        row.provider_ids = ["opencv"]
        row.model_versions = ["telea-v1"]
        row.parameter_hash = route_checksum
        revision.after = {
            "candidateChecksum": row.candidate_checksum,
            "maskChecksum": row.mask_checksum,
            "routeChecksum": route_checksum,
        }
        session.flush()
        state_checksum = clean_plate_service._state_checksum(
            row.parent_checksum,
            row.background_checksum,
            row.quality_checksum,
            row.mask_artifact_id,
            row.mask_checksum,
            [row],
            [],
            [],
        )
        produced.provider = "opencv"
        produced.parameter_hash = route_checksum
        produced.output_checksum = state_checksum
        produced.evidence = {
            **produced.evidence,
            "routeManifest": manifest,
            "routeChecksum": route_checksum,
            "originKind": "classical",
            "providerIds": ["opencv"],
            "modelVersions": ["telea-v1"],
            "parameterHash": route_checksum,
        }
        completed.provider = "opencv"
        completed.parameter_hash = route_checksum
        completed.output_checksum = state_checksum
        completed.evidence = {**completed.evidence, "routeChecksum": route_checksum}
    response = client.get(f"/api/images/{image['id']}/page-gates/clean-plate")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "g8-clean-plate-replay-invalid"


def test_g8_classical_fallback_is_page_scoped_and_requires_ai_rejection(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g7_accepted_page(
        client, app, tmp_path, background_category="complex-lineart"
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    fake = _G8FakeLama()
    original_resolver = app.state.providers.inpainter
    app.state.providers.inpainter = lambda name: (
        fake if name == "lama-onnx" else original_resolver(name)
    )
    before = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    blocked = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate/fallback",
        json={
            "enabled": True,
            "reason": "all-ai-candidates-rejected",
            "expectedRevision": before["imageRevision"],
            "lineage": _mutation_lineage(generation_id, before["nextSequence"]),
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "g8-ai-candidates-not-all-rejected"

    ai_job = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "lama"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert ai_job.status_code == 202, ai_job.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, ai_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    ai_candidate = context["candidates"][0]
    assert ai_candidate["originKind"] == "ai"
    rejected_checks = [dict(entry) for entry in _CLEAN_PLATE_CHECKS]
    next(entry for entry in rejected_checks if entry["check"] == "structure-preserved")[
        "passed"
    ] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "reject",
            "reason": "structure-damaged",
            "candidateId": ai_candidate["candidateId"],
            "observedCandidateChecksum": ai_candidate["candidateChecksum"],
            "observedWidth": ai_candidate["width"],
            "observedHeight": ai_candidate["height"],
            "checks": rejected_checks,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert context["fallbackAllowed"] is True
    enabled = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate/fallback",
        json={
            "enabled": True,
            "reason": "all-ai-candidates-rejected",
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["fallbackEnabled"] is True

    fallback_job = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {"classicalFallback": True},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert fallback_job.status_code == 202, fallback_job.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, fallback_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    classical = context["candidates"][-1]
    assert classical["originKind"] == "classical"
    assert classical["routeManifest"][0]["route"] == "classical-fallback"
    assert classical["anomalies"] == [
        f"classical-complex-fallback:{classical['routeManifest'][0]['regionId']}"
    ]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "accept",
            "reason": "clean-plate-complete",
            "candidateId": classical["candidateId"],
            "observedCandidateChecksum": classical["candidateChecksum"],
            "observedWidth": classical["width"],
            "observedHeight": classical["height"],
            "checks": _CLEAN_PLATE_CHECKS,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert fake.calls == 1


def test_g8_layered_acceptance_rechecks_current_fallback_without_failed_write(
    client: TestClient, app, tmp_path: Path
) -> None:
    data, source_project, source_image, target_project, target_image = _source_and_target(
        client, tmp_path
    )
    reference_image = upload_image(
        client,
        target_project["id"],
        relative_path="chapter/layered-reference.png",
        data=data,
    )
    prepared_g3 = _prepare_g3_yes_page(
        client,
        app,
        tmp_path,
        prepared={
            "data": data,
            "sourceProject": source_project,
            "sourceImage": source_image,
            "targetProject": target_project,
            "targetImage": target_image,
        },
    )
    prepared = _prepare_g7_accepted_page(
        client,
        app,
        tmp_path,
        background_category="complex-lineart",
        prepared=prepared_g3,
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)

    reference_generation_id = str(uuid.uuid4())
    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        reference_row = session.get(ImageAsset, reference_image["id"])
        accepted_mask = session.get(PageMaskArtifact, prepared["maskArtifact"]["artifactId"])
        assert generation is not None and reference_row is not None and accepted_mask is not None
        accepted_mask_path = resolve_write_target(
            store.root,
            Path(accepted_mask.relative_path),
            protected_roots=(store.source_root,),
        )
        session.add(
            PageGeneration(
                id=reference_generation_id,
                run_id="synthetic-layered-reference",
                project_id=generation.project_id,
                image_id=reference_row.id,
                restart_from_source=True,
                parameter_set_id="synthetic-layered-reference-v1",
                parameter_set_hash="b" * 64,
                source_project_id=generation.source_project_id,
                source_image_id=generation.source_image_id,
                source_checksum=generation.source_checksum,
                source_relative_path=generation.source_relative_path,
                state="superseded",
                next_sequence=2,
                actor_kind=generation.actor_kind,
                actor_id=generation.actor_id,
                task_id=generation.task_id,
                thread_id=generation.thread_id,
                session_id=generation.session_id,
                operation_source=generation.operation_source,
            )
        )
    reference_relative = safe_relative_path(reference_image["relativePath"]).with_suffix(".png")
    reference_mask_path = resolve_write_target(
        store.root,
        Path("generated") / "masks" / reference_relative,
        protected_roots=(store.source_root,),
    )
    reference_mask_path.parent.mkdir(parents=True, exist_ok=True)
    reference_mask_bytes = accepted_mask_path.read_bytes()
    reference_mask_path.write_bytes(reference_mask_bytes)
    reference_mask_checksum = _checksum(reference_mask_bytes)
    with Image.open(io.BytesIO(reference_mask_bytes)) as opened_mask:
        reference_grid = opened_mask.size
    reference_bytes = png_bytes(size=reference_grid, color=(121, 122, 123))
    reference_checksum = _checksum(reference_bytes)
    reference_record = {
        "id": CANDIDATE_PRIMARY,
        "label": "synthetic trusted layered reference",
        "artifactChecksum": reference_checksum,
        "originKind": "direct-ai",
        "providerIds": ["lama-onnx"],
        "changedPixelsOutsideMask": 0,
        "meanAbsDeltaInsideMask": 0.0,
        "chromaInsideMask": 0.0,
        "anomalies": [],
    }
    write_page_inpaint_candidates(
        store,
        reference_relative,
        selected_id=CANDIDATE_PRIMARY,
        generation_id=reference_generation_id,
        mask_checksum=reference_mask_checksum,
        encoded_files=[(CANDIDATE_PRIMARY, reference_bytes)],
        manifest_candidates=[reference_record],
    )
    reference_manifest_digest = inpaint_candidate_manifest_digest(
        generation_id=reference_generation_id,
        mask_checksum=reference_mask_checksum,
        candidates=[reference_record],
    )
    with store.session() as session:
        reference_row = session.get(ImageAsset, reference_image["id"])
        assert reference_row is not None
        reference_status = dict(reference_row.status or {})
        reference_status["inpaint"] = "done"
        reference_row.status = reference_status
        reference_row.inpaint_provenance = make_inpaint_provenance(
            artifact_checksum=reference_checksum,
            mask_checksum=reference_mask_checksum,
            candidate_id=CANDIDATE_PRIMARY,
            origin_kind="direct-ai",
            provider_ids=["lama-onnx"],
            generation_id=reference_generation_id,
            candidate_manifest_digest=reference_manifest_digest,
        )

    fake = _G8FakeLama()
    original_resolver = app.state.providers.inpainter
    app.state.providers.inpainter = lambda name: (
        fake if name == "lama-onnx" else original_resolver(name)
    )
    ai_job = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {"provider": "lama"},
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert ai_job.status_code == 202, ai_job.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, ai_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    ai_candidate = context["candidates"][0]
    rejected_checks = [dict(entry) for entry in _CLEAN_PLATE_CHECKS]
    next(entry for entry in rejected_checks if entry["check"] == "structure-preserved")[
        "passed"
    ] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "reject",
            "reason": "structure-damaged",
            "candidateId": ai_candidate["candidateId"],
            "observedCandidateChecksum": ai_candidate["candidateChecksum"],
            "observedWidth": ai_candidate["width"],
            "observedHeight": ai_candidate["height"],
            "checks": rejected_checks,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    enabled = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate/fallback",
        json={
            "enabled": True,
            "reason": "all-ai-candidates-rejected",
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert enabled.status_code == 200, enabled.text

    guide = {
        "version": 1,
        "domains": [
            {
                "id": "complete-support",
                "mode": "reference",
                "referenceId": "reference-a",
                "polygon": [
                    [0, 0],
                    [image["width"] - 1, 0],
                    [image["width"] - 1, image["height"] - 1],
                    [0, image["height"] - 1],
                ],
            }
        ],
        "strokes": [],
        "featherRadius": 0,
    }
    requested = [
        {
            "id": "reference-a",
            "imageId": reference_image["id"],
            "candidateId": CANDIDATE_PRIMARY,
            "expectedSourceChecksum": _checksum(data),
            "expectedArtifactChecksum": reference_checksum,
            "expectedManifestDigest": reference_manifest_digest,
            "expectedMaskChecksum": reference_mask_checksum,
        }
    ]
    layered_job = client.post(
        f"/api/projects/{project['id']}/inpaint",
        json={
            "imageIds": [image["id"]],
            "options": {
                "classicalFallback": True,
                "layeredStructureGuide": guide,
                "layeredStructureReferences": requested,
            },
            "lineage": _current_lineage_context(client, image["id"], generation_id),
        },
    )
    assert layered_job.status_code == 202, layered_job.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, layered_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    layered = context["candidates"][-1]
    assert all(route["route"] == "layered-structure" for route in layered["routeManifest"])

    disabled = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate/fallback",
        json={
            "enabled": False,
            "reason": "resume-ai-candidates",
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert disabled.status_code == 200, disabled.text
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert context["fallbackEnabled"] is False

    def mutation_state() -> tuple[object, ...]:
        with store.session() as session:
            image_row = session.get(ImageAsset, image["id"])
            generation = session.get(PageGeneration, generation_id)
            assert image_row is not None and generation is not None
            return (
                session.scalar(select(func.count()).select_from(PageCleanPlateReview)) or 0,
                session.scalar(select(func.count()).select_from(Revision)) or 0,
                session.scalar(select(func.count()).select_from(PageLineageEvent)) or 0,
                image_row.revision,
                generation.next_sequence,
                generation.state,
                json.dumps(image_row.status, sort_keys=True),
            )

    before_failed_review = mutation_state()
    before_failed_context = context
    failed = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "accept",
            "reason": "clean-plate-complete",
            "candidateId": layered["candidateId"],
            "observedCandidateChecksum": layered["candidateChecksum"],
            "observedWidth": layered["width"],
            "observedHeight": layered["height"],
            "checks": _CLEAN_PLATE_CHECKS,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert failed.status_code == 409, failed.text
    assert failed.json()["detail"]["reason"] == "g8-ai-candidates-not-all-rejected"
    assert mutation_state() == before_failed_review
    assert client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json() == (
        before_failed_context
    )

    enabled_again = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate/fallback",
        json={
            "enabled": True,
            "reason": "all-ai-candidates-rejected",
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert enabled_again.status_code == 200, enabled_again.text
    context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "accept",
            "reason": "clean-plate-complete",
            "candidateId": layered["candidateId"],
            "observedCandidateChecksum": layered["candidateChecksum"],
            "observedWidth": layered["width"],
            "observedHeight": layered["height"],
            "checks": _CLEAN_PLATE_CHECKS,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    final_context = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    assert final_context["state"] == "accepted"
    assert final_context["acceptedCandidateId"] == layered["candidateId"]
    assert fake.calls == 1


def test_g8_layered_structure_reference_snapshot_is_bound_write_once_and_replayable(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g7_accepted_page(
        client, app, tmp_path, background_category="complex-lineart"
    )
    store = prepared["store"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    old_generation_id = str(uuid.uuid4())
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        accepted_mask = session.get(PageMaskArtifact, prepared["maskArtifact"]["artifactId"])
        assert image_row is not None and generation is not None and accepted_mask is not None
        accepted_mask_path = resolve_write_target(
            store.root,
            Path(accepted_mask.relative_path),
            protected_roots=(store.source_root,),
        )
        source_checksum = generation.source_checksum
        session.add(
            PageGeneration(
                id=old_generation_id,
                run_id="historical-layered-reference",
                project_id=generation.project_id,
                image_id=image_row.id,
                restart_from_source=True,
                parameter_set_id="historical-layered-reference-v1",
                parameter_set_hash="b" * 64,
                source_project_id=generation.source_project_id,
                source_image_id=generation.source_image_id,
                source_checksum=generation.source_checksum,
                source_relative_path=generation.source_relative_path,
                state="superseded",
                next_sequence=2,
                actor_kind=generation.actor_kind,
                actor_id=generation.actor_id,
                task_id=generation.task_id,
                thread_id=generation.thread_id,
                session_id=generation.session_id,
                operation_source=generation.operation_source,
            )
        )
    relative = safe_relative_path(image["relativePath"]).with_suffix(".png")
    mask_path = resolve_write_target(
        store.root,
        Path("generated") / "masks" / relative,
        protected_roots=(store.source_root,),
    )
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    mask_path.write_bytes(accepted_mask_path.read_bytes())
    mask_bytes = mask_path.read_bytes()
    mask_checksum = _checksum(mask_bytes)
    with Image.open(io.BytesIO(mask_bytes)) as opened_mask:
        grid = opened_mask.size
    artifact_bytes = png_bytes(size=grid, color=(91, 92, 93))
    artifact_checksum = _checksum(artifact_bytes)
    selected_record = {
        "id": CANDIDATE_PRIMARY,
        "label": "selected historical AI reference",
        "artifactChecksum": artifact_checksum,
        "originKind": "direct-ai",
        "providerIds": ["lama-onnx"],
        "changedPixelsOutsideMask": 0,
        "meanAbsDeltaInsideMask": 0.0,
        "chromaInsideMask": 0.0,
        "anomalies": [],
    }
    reference_bytes = png_bytes(size=grid, color=(121, 122, 123))
    reference_checksum = _checksum(reference_bytes)
    reference_record = {
        **selected_record,
        "id": CANDIDATE_LAMA_FULL_CONTEXT,
        "label": "unselected historical AI reference",
        "artifactChecksum": reference_checksum,
    }
    records = [selected_record, reference_record]
    write_page_inpaint_candidates(
        store,
        relative,
        selected_id=CANDIDATE_PRIMARY,
        generation_id=old_generation_id,
        mask_checksum=mask_checksum,
        encoded_files=[
            (CANDIDATE_PRIMARY, artifact_bytes),
            (CANDIDATE_LAMA_FULL_CONTEXT, reference_bytes),
        ],
        manifest_candidates=records,
    )
    manifest_digest = inpaint_candidate_manifest_digest(
        generation_id=old_generation_id,
        mask_checksum=mask_checksum,
        candidates=records,
    )
    requested = [
        {
            "id": "reference-a",
            "imageId": image["id"],
            "candidateId": CANDIDATE_LAMA_FULL_CONTEXT,
            "expectedSourceChecksum": source_checksum,
            "expectedArtifactChecksum": reference_checksum,
            "expectedManifestDigest": manifest_digest,
            "expectedMaskChecksum": mask_checksum,
        }
    ]
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(ProjectError, match="not trusted current provenance"):
            snapshot_layered_structure_references(
                store,
                session,
                image=image_row,
                generation=generation,
                references=requested,
                expected_grid=grid,
            )
        status = dict(image_row.status)
        status["inpaint"] = "done"
        image_row.status = status
        image_row.inpaint_provenance = make_inpaint_provenance(
            artifact_checksum=artifact_checksum,
            mask_checksum=mask_checksum,
            candidate_id=CANDIDATE_PRIMARY,
            origin_kind="direct-ai",
            provider_ids=["lama-onnx"],
            generation_id=old_generation_id,
            candidate_manifest_digest=manifest_digest,
        )
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        snapshots = snapshot_layered_structure_references(
            store,
            session,
            image=image_row,
            generation=generation,
            references=requested,
            expected_grid=grid,
        )
        with pytest.raises(ProjectError, match="lineage or checksum changed"):
            snapshot_layered_structure_references(
                store,
                session,
                image=image_row,
                generation=generation,
                references=[
                    {
                        **requested[0],
                        "expectedArtifactChecksum": "0" * 64,
                    }
                ],
                expected_grid=grid,
            )
        with pytest.raises(ProjectError, match="same immutable source"):
            snapshot_layered_structure_references(
                store,
                session,
                image=image_row,
                generation=generation,
                references=[
                    {
                        **requested[0],
                        "expectedSourceChecksum": "0" * 64,
                    }
                ],
                expected_grid=grid,
            )
        with pytest.raises(ProjectError, match="grid or bytes changed"):
            snapshot_layered_structure_references(
                store,
                session,
                image=image_row,
                generation=generation,
                references=requested,
                expected_grid=(grid[0] + 1, grid[1]),
            )
    assert snapshots[0]["artifactChecksum"] == reference_checksum
    assert snapshots[0]["legacyManifestDigest"] == manifest_digest
    loaded = load_layered_structure_snapshots(store, generation_id, snapshots)
    assert loaded == {"reference-a": reference_bytes}
    with pytest.raises(ProjectError, match="content address"):
        load_layered_structure_snapshots(store, str(uuid.uuid4()), snapshots)
    with pytest.raises(ProjectError, match="content address"):
        load_layered_structure_snapshots(
            store,
            generation_id,
            [{**snapshots[0], "snapshotId": "0" * 64}],
        )

    snapshot_base = Path("generated") / "lineage-inputs" / "layered-structure-v1" / generation_id
    original_manifest_path = resolve_write_target(
        store.root,
        snapshot_base / snapshots[0]["snapshotId"] / "manifest.json",
        protected_roots=(store.source_root,),
    )
    original_manifest = json.loads(original_manifest_path.read_text("utf-8"))
    tampered_manifests = [
        {**original_manifest, "version": 2},
        {**original_manifest, "unexpected": True},
        {**original_manifest, "width": original_manifest["width"] + 1},
    ]
    for tampered_manifest in tampered_manifests:
        encoded_manifest = json.dumps(
            tampered_manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_checksum = _checksum(encoded_manifest)
        snapshot_id = _checksum(
            json.dumps(
                {
                    "generationId": generation_id,
                    "sourceManifestDigest": manifest_checksum,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        tampered_root = resolve_write_target(
            store.root,
            snapshot_base / snapshot_id,
            protected_roots=(store.source_root,),
        )
        tampered_root.mkdir(parents=True, exist_ok=True)
        (tampered_root / "artifact.png").write_bytes(reference_bytes)
        (tampered_root / "manifest.json").write_bytes(encoded_manifest)
        with pytest.raises(ProjectError, match="manifest changed"):
            load_layered_structure_snapshots(
                store,
                generation_id,
                [
                    {
                        **snapshots[0],
                        "snapshotId": snapshot_id,
                        "sourceManifestDigest": manifest_checksum,
                    }
                ],
            )
    wrong_grid_bytes = png_bytes(size=(grid[0] + 1, grid[1]), color=(1, 2, 3))
    wrong_grid_checksum = _checksum(wrong_grid_bytes)
    wrong_grid_manifest = {
        **original_manifest,
        "artifactChecksum": wrong_grid_checksum,
    }
    wrong_grid_manifest_bytes = json.dumps(
        wrong_grid_manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    wrong_grid_manifest_digest = _checksum(wrong_grid_manifest_bytes)
    wrong_grid_snapshot_id = _checksum(
        json.dumps(
            {
                "generationId": generation_id,
                "sourceManifestDigest": wrong_grid_manifest_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    wrong_grid_root = resolve_write_target(
        store.root,
        snapshot_base / wrong_grid_snapshot_id,
        protected_roots=(store.source_root,),
    )
    wrong_grid_root.mkdir(parents=True, exist_ok=True)
    (wrong_grid_root / "artifact.png").write_bytes(wrong_grid_bytes)
    (wrong_grid_root / "manifest.json").write_bytes(wrong_grid_manifest_bytes)
    with pytest.raises(ProjectError, match="artifact grid changed"):
        load_layered_structure_snapshots(
            store,
            generation_id,
            [
                {
                    **snapshots[0],
                    "snapshotId": wrong_grid_snapshot_id,
                    "sourceManifestDigest": wrong_grid_manifest_digest,
                    "artifactChecksum": wrong_grid_checksum,
                }
            ],
        )

    candidate_image_path(store, relative, CANDIDATE_LAMA_FULL_CONTEXT).unlink()
    assert load_layered_structure_snapshots(store, generation_id, snapshots) == loaded
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(ProjectError):
            snapshot_layered_structure_references(
                store,
                session,
                image=image_row,
                generation=generation,
                references=requested,
                expected_grid=grid,
            )
    guide = {
        "version": 1,
        "domains": [
            {
                "id": "complete-support",
                "mode": "reference",
                "referenceId": "reference-a",
                "polygon": [
                    [0, 0],
                    [image["width"] - 1, 0],
                    [image["width"] - 1, image["height"] - 1],
                    [0, image["height"] - 1],
                ],
            }
        ],
        "strokes": [],
        "featherRadius": 0,
    }
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(page_lineage.PageLineageConflict) as disabled:
            clean_plate_service._route_manifest(
                store,
                session,
                image_row,
                generation,
                options={
                    "classicalFallback": True,
                    "layeredStructureGuide": guide,
                    "layeredStructureReferences": requested,
                },
                fallback_enabled=False,
                accepted_g7_checksum=prepared["acceptedG7"]["event"]["outputChecksum"],
                bound_snapshots=snapshots,
            )
        assert disabled.value.reason == "g8-classical-fallback-disabled"
        with pytest.raises(ProjectError, match="unsupported options"):
            clean_plate_service._route_manifest(
                store,
                session,
                image_row,
                generation,
                options={
                    "classicalFallback": True,
                    "layeredStructureGuide": guide,
                    "layeredStructureReferences": requested,
                    "path": "forbidden.png",
                },
                fallback_enabled=True,
                accepted_g7_checksum=prepared["acceptedG7"]["event"]["outputChecksum"],
                bound_snapshots=snapshots,
            )
        _background, _quality, route_manifest, normalized = clean_plate_service._route_manifest(
            store,
            session,
            image_row,
            generation,
            options={
                "classicalFallback": True,
                "layeredStructureGuide": guide,
                "layeredStructureReferences": requested,
            },
            fallback_enabled=True,
            accepted_g7_checksum=prepared["acceptedG7"]["event"]["outputChecksum"],
            bound_snapshots=snapshots,
        )
        with pytest.raises(ProjectError, match="snapshot binding changed"):
            clean_plate_service._route_manifest(
                store,
                session,
                image_row,
                generation,
                options={
                    "classicalFallback": True,
                    "layeredStructureGuide": guide,
                    "layeredStructureReferences": [
                        {**requested[0], "expectedArtifactChecksum": "f" * 64}
                    ],
                },
                fallback_enabled=True,
                accepted_g7_checksum=prepared["acceptedG7"]["event"]["outputChecksum"],
                bound_snapshots=snapshots,
            )
    assert normalized["layeredStructureSnapshots"] == snapshots
    assert all(entry["route"] == "layered-structure" for entry in route_manifest)
    assert all(entry["originKind"] == "mixed" for entry in route_manifest)
    assert all(entry["provider"] == "opencv" for entry in route_manifest)
    assert all(entry["modelVersion"] == "layered-structure-guide-v1" for entry in route_manifest)
    assert all(entry["lineageInputs"] == snapshots for entry in route_manifest)
    assert "polygon" not in json.dumps(route_manifest)
    CleanPlateLayeredRouteEntry.model_validate(route_manifest[0])
    assert page_lineage._public_evidence_value("routeManifest", route_manifest) == route_manifest

    malformed_records = [
        {**item, "originKind": "classical", "providerIds": ["lama-onnx"]} for item in records
    ]
    write_page_inpaint_candidates(
        store,
        relative,
        selected_id=CANDIDATE_PRIMARY,
        generation_id=old_generation_id,
        mask_checksum=mask_checksum,
        encoded_files=[
            (CANDIDATE_PRIMARY, artifact_bytes),
            (CANDIDATE_LAMA_FULL_CONTEXT, reference_bytes),
        ],
        manifest_candidates=malformed_records,
    )
    malformed_manifest_digest = inpaint_candidate_manifest_digest(
        generation_id=old_generation_id,
        mask_checksum=mask_checksum,
        candidates=malformed_records,
    )
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        image_row.inpaint_provenance = make_inpaint_provenance(
            artifact_checksum=artifact_checksum,
            mask_checksum=mask_checksum,
            candidate_id=CANDIDATE_PRIMARY,
            origin_kind="classical",
            provider_ids=["lama-onnx"],
            generation_id=old_generation_id,
            candidate_manifest_digest=malformed_manifest_digest,
        )
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(ProjectError, match="classical reference provenance"):
            snapshot_layered_structure_references(
                store,
                session,
                image=image_row,
                generation=generation,
                references=[
                    {
                        **requested[0],
                        "expectedManifestDigest": malformed_manifest_digest,
                    }
                ],
                expected_grid=grid,
            )

    collision_path = resolve_write_target(
        store.root,
        Path("generated")
        / "lineage-inputs"
        / "layered-structure-v1"
        / generation_id
        / snapshots[0]["snapshotId"]
        / "artifact.png",
        protected_roots=(store.source_root,),
    )
    collision_path.write_bytes(b"changed")
    with pytest.raises(ProjectError, match="snapshot changed"):
        load_layered_structure_snapshots(store, generation_id, snapshots)


def _raw_append_second_g7_draft_event(
    store,
    *,
    image_id: str,
    generation_id: str,
    context: dict[str, object],
    tamper: str,
) -> None:
    """Append an otherwise exact second draft prefix with one corrupted matrix field."""
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        generation = session.get(PageGeneration, generation_id)
        draft = session.get(PageMaskDraft, generation_id)
        assert image is not None and generation is not None and draft is not None
        assert not session.scalars(
            select(PageMaskArtifact).where(PageMaskArtifact.generation_id == generation_id)
        ).first()
        draft.revision += 1
        image.revision += 1
        revision = add_revision(
            session,
            store.project(session),
            entity_type="page-mask-draft",
            entity_id=generation_id,
            operation="update",
            before={"checksum": draft.state_checksum},
            after={"checksum": draft.state_checksum},
        )
        session.flush()
        mapping = context["rubyRegionIdsByPrimary"]
        assert isinstance(mapping, dict)
        output_checksum = mask_service._state_checksum(
            str(context["g6Checksum"]),
            str(context["qualityChecksum"]),
            mapping,
            draft,
            [],
            [],
        )
        evidence = {
            "eventType": "mask-draft-updated",
            "qualityState": "pending-review",
            "eligibleRegionCount": len(context["eligibleRegionIds"]),
            "recipeRegionCount": len(draft.recipe),
            "recipeChecksum": draft.state_checksum,
            "qualityChecksum": context["qualityChecksum"],
            "rubyRegionCount": sum(map(len, mapping.values())),
            "rubyRegionIdsByPrimary": mapping,
            "imageRevision": image.revision,
        }
        values: dict[str, object] = {
            "generation_id": generation.id,
            "sequence": generation.next_sequence,
            "operation": "mask-draft-updated",
            "gate": "G7_mask",
            "state": "pending",
            "actor_kind": _ACTOR["actorKind"],
            "actor_id": None,
            "task_id": _ACTOR["taskId"],
            "thread_id": _ACTOR["threadId"],
            "session_id": _ACTOR["sessionId"],
            "operation_source": _ACTOR["operationSource"],
            "input_checksum": context["maskStateChecksum"],
            "output_checksum": output_checksum,
            "parent_checksum": context["g6Checksum"],
            "stage": "mask",
            "provider": "deterministic-mask",
            "model_version": "create-mask-v1",
            "parameter_hash": draft.state_checksum,
            "revision_id": revision.id,
            "decision": None,
            "reason": "mask-recipe-updated",
            "evidence": evidence,
        }
        if tamper == "gate":
            values["gate"] = "G6_ocr"
        elif tamper == "state":
            values["state"] = "blocked"
        elif tamper == "decision":
            values["decision"] = "mask-accepted"
        elif tamper == "reason":
            values["reason"] = "forged-reason"
        elif tamper == "stage":
            values["stage"] = "translation"
        elif tamper == "parent":
            values["parent_checksum"] = "b" * 64
        elif tamper == "identity":
            values["provider"] = "forged-mask"
        elif tamper == "input":
            values["input_checksum"] = "c" * 64
        elif tamper == "output":
            values["output_checksum"] = "d" * 64
        elif tamper == "evidence":
            evidence["qualityState"] = "accepted"
        else:  # pragma: no cover - helper contract
            raise AssertionError(f"Unknown G7 tamper: {tamper}")
        session.add(PageLineageEvent(**values))
        generation.next_sequence += 1


def _raw_append_g7_enqueue(
    store,
    *,
    project_id: str,
    image_id: str,
    generation_id: str,
    context: dict[str, object],
    tamper: str,
) -> None:
    lineage = _lineage_context(image_id, generation_id, int(context["nextSequence"]))
    if tamper == "duplicate-lineage":
        lineage["pages"].append(dict(lineage["pages"][0]))
    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        assert generation is not None
        job = Job(
            project_id=project_id,
            kind="mask",
            status="queued",
            total=1,
            options={},
            lineage_context=lineage,
        )
        item = JobItem(
            image_id=image_id,
            region_id=(str(uuid.uuid4()) if tamper == "region-item" else None),
            position=0,
            status="queued",
            progress=0,
            output={},
        )
        job.items.append(item)
        session.add(job)
        session.flush()
        mapping = context["rubyRegionIdsByPrimary"]
        assert isinstance(mapping, dict)
        session.add(
            PageLineageEvent(
                generation_id=generation_id,
                sequence=generation.next_sequence,
                operation="mask-job-enqueued",
                gate="G7_mask",
                state="pending",
                actor_kind=_ACTOR["actorKind"],
                actor_id=None,
                task_id=("forged-task" if tamper == "job-actor" else _ACTOR["taskId"]),
                thread_id=_ACTOR["threadId"],
                session_id=_ACTOR["sessionId"],
                operation_source=_ACTOR["operationSource"],
                input_checksum=context["maskStateChecksum"],
                output_checksum=context["maskStateChecksum"],
                parent_checksum=context["g6Checksum"],
                stage="mask",
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=context["draft"]["stateChecksum"],
                job_id=job.id,
                job_item_id=item.id,
                decision=None,
                reason="job-enqueued",
                evidence={
                    "eventType": "job-enqueued",
                    "qualityState": "pending-review",
                    "targetKind": "image",
                    "eligibleRegionCount": len(context["eligibleRegionIds"]),
                    "rubyRegionCount": sum(map(len, mapping.values())),
                    "rubyRegionIdsByPrimary": mapping,
                    "recipeChecksum": context["draft"]["stateChecksum"],
                    "qualityChecksum": context["qualityChecksum"],
                },
            )
        )
        generation.next_sequence += 1


def _raw_append_g7_artifact_review(
    store,
    *,
    image_id: str,
    generation_id: str,
    context: dict[str, object],
    artifact_id: str,
    state: str,
    coverage_checks: list[dict[str, object]],
    reviewer: dict[str, object],
    event_actor: dict[str, object] | None = None,
) -> None:
    event_actor = event_actor or _ACTOR
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        generation = session.get(PageGeneration, generation_id)
        draft = session.get(PageMaskDraft, generation_id)
        artifact = session.get(PageMaskArtifact, artifact_id)
        assert image is not None and generation is not None and draft is not None
        assert artifact is not None
        prior_reviews = list(
            session.scalars(
                select(PageMaskReview)
                .where(PageMaskReview.generation_id == generation_id)
                .order_by(PageMaskReview.sequence)
            ).all()
        )
        reason = "complete-and-no-collateral" if state == "accepted" else "coverage-incomplete"
        review = PageMaskReview(
            generation_id=generation_id,
            image_id=image_id,
            artifact_id=artifact.id,
            sequence=len(prior_reviews) + 1,
            state=state,
            reason=reason,
            mask_checksum=artifact.mask_checksum,
            coverage_checks=coverage_checks,
            collateral_checks=_MASK_COLLATERAL,
            reviewer=reviewer,
        )
        session.add(review)
        session.flush()
        artifacts = list(
            session.scalars(
                select(PageMaskArtifact)
                .where(PageMaskArtifact.generation_id == generation_id)
                .order_by(PageMaskArtifact.sequence)
            ).all()
        )
        mapping = context["rubyRegionIdsByPrimary"]
        assert isinstance(mapping, dict)
        after_state = mask_service._state_checksum(
            str(context["g6Checksum"]),
            str(context["qualityChecksum"]),
            mapping,
            draft,
            artifacts,
            [*prior_reviews, review],
        )
        image.revision += 1
        revision = add_revision(
            session,
            store.project(session),
            entity_type="page-mask-review",
            entity_id=generation_id,
            operation=state,
            before={},
            after={
                "state": state,
                "artifactId": artifact.id,
                "maskChecksum": artifact.mask_checksum,
            },
        )
        session.flush()
        session.add(
            PageLineageEvent(
                generation_id=generation_id,
                sequence=generation.next_sequence,
                operation="mask-stage-review",
                gate="G7_mask",
                state=state,
                actor_kind=str(event_actor["actorKind"]),
                actor_id=event_actor.get("actorId"),
                task_id=event_actor.get("taskId"),
                thread_id=event_actor.get("threadId"),
                session_id=event_actor.get("sessionId"),
                operation_source=str(event_actor["operationSource"]),
                input_checksum=context["maskStateChecksum"],
                output_checksum=after_state,
                parent_checksum=context["g6Checksum"],
                stage="mask",
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=draft.state_checksum,
                revision_id=revision.id,
                decision="mask-accepted" if state == "accepted" else "mask-rejected",
                reason=reason,
                evidence={
                    "eventType": "mask-stage-review",
                    "qualityState": state,
                    "artifactId": artifact.id,
                    "maskChecksum": artifact.mask_checksum,
                    "recipeChecksum": draft.state_checksum,
                    "qualityChecksum": context["qualityChecksum"],
                    "eligibleRegionCount": len(context["eligibleRegionIds"]),
                    "rubyRegionCount": sum(map(len, mapping.values())),
                    "rubyRegionIdsByPrimary": mapping,
                    "coverageChecks": coverage_checks,
                    "collateralChecks": _MASK_COLLATERAL,
                    "imageRevision": image.revision,
                },
            )
        )
        generation.next_sequence += 1


def test_g7_failed_job_is_exact_and_fresh_job_retries(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    first = _enqueue_g7(client, prepared)
    assert first.status_code == 202, first.text
    original_create_mask = mask_service.create_mask

    def fail_mask(*_args, **_kwargs):
        raise RuntimeError("injected deterministic mask failure")

    monkeypatch.setattr(mask_service, "create_mask", fail_mask)
    claimed = app.state.queue._claim_next()
    assert claimed == (store, first.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, first.json()["id"]).status == "failed"
    context = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert context.status_code == 200, context.text
    assert context.json()["artifacts"] == []
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    enqueue, failed = events[-2:]
    assert [enqueue["operation"], failed["operation"]] == [
        "mask-job-enqueued",
        "mask-job-failed",
    ]
    assert failed["inputChecksum"] == enqueue["inputChecksum"]
    assert failed["outputChecksum"] is None
    assert (failed["provider"], failed["modelVersion"], failed["parameterHash"]) == (
        enqueue["provider"],
        enqueue["modelVersion"],
        enqueue["parameterHash"],
    )
    monkeypatch.setattr(mask_service, "create_mask", original_create_mask)
    retry = _enqueue_g7(client, prepared)
    assert retry.status_code == 202, retry.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, retry.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert app.state.queue.get_job(store, retry.json()["id"]).status == "completed"


def test_g7_deterministic_file_and_published_row_recovery(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    with store.session() as session:
        item_id = session.scalar(select(JobItem.id).where(JobItem.job_id == queued.json()["id"]))
    assert isinstance(item_id, str)
    assert app.state.queue._begin_item(store, queued.json()["id"], item_id)
    original_append = mask_service._append_event

    def fail_publication(*args, **kwargs):
        if kwargs.get("operation") == "mask-artifact-produced":
            raise RuntimeError("injected G7 publication failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(mask_service, "_append_event", fail_publication)
    with pytest.raises(RuntimeError, match="injected G7 publication failure"):
        app.state.queue._process_item(store, queued.json()["id"], item_id)
    deterministic_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"g7-mask:{generation_id}:{item_id}"))
    residue = store.root / "generated" / "lineage-masks" / generation_id / f"{deterministic_id}.png"
    assert residue.is_file()
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(PageMaskArtifact)) == 0
    monkeypatch.setattr(mask_service, "_append_event", original_append)
    output = app.state.queue._process_item(store, queued.json()["id"], item_id)
    assert output["artifactId"] == deterministic_id
    assert list(residue.parent.glob("*.png")) == [residue]
    cancelled = client.post(f"/api/jobs/{queued.json()['id']}/cancel")
    assert cancelled.status_code == 409, cancelled.text

    def forbid_raster(*_args, **_kwargs):
        raise AssertionError("published artifact must recover without rerasterizing")

    monkeypatch.setattr(mask_service, "create_mask", forbid_raster)
    assert store.recover_jobs() == 1
    reclaimed = app.state.queue._claim_next()
    assert reclaimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*reclaimed))
    assert app.state.queue.get_job(store, queued.json()["id"]).status == "completed"
    events = client.get(f"/api/page-generations/{generation_id}/events").json()
    assert [event["operation"] for event in events[-3:]] == [
        "mask-job-enqueued",
        "mask-artifact-produced",
        "mask-job-completed",
    ]


def test_g7_raw_draft_tamper_and_append_only_artifact_fail_closed(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    artifact_id = context["artifacts"][0]["artifactId"]
    artifact = context["artifacts"][0]
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        revision = session.scalar(
            select(Revision)
            .where(
                Revision.entity_type == "page-mask-artifact",
                Revision.entity_id == artifact_id,
            )
            .limit(1)
        )
        assert image_row is not None and revision is not None
        assert context["imageRevision"] == image_row.revision
    with sqlite3.connect(store.database_path) as database:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute(
                "UPDATE page_mask_artifacts SET nonzero_pixels = nonzero_pixels + 1 WHERE id = ?",
                (artifact_id,),
            )
        orphan_item_id = str(uuid.uuid4())
        database.execute(
            """
            INSERT INTO job_items
                (id, job_id, image_id, region_id, position, status, progress, error, output)
            VALUES (?, ?, ?, NULL, 99, 'completed', 1, NULL, '{}')
            """,
            (orphan_item_id, artifact["jobId"], image["id"]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid mask raster facts"):
            database.execute(
                """
                INSERT INTO page_mask_artifacts
                    (id, generation_id, image_id, job_id, job_item_id, sequence,
                     parent_checksum, quality_checksum, draft_checksum, mask_checksum,
                     relative_path, width, height, render_scale, provider, model_version,
                     parameter_hash, nonzero_pixels, bbox, created_at)
                SELECT ?, generation_id, image_id, job_id, ?, sequence + 1,
                       parent_checksum, quality_checksum, draft_checksum, mask_checksum,
                       'generated/lineage-masks/not-canonical.png', width, height,
                       render_scale, provider, model_version, parameter_hash,
                       nonzero_pixels, bbox, created_at
                FROM page_mask_artifacts WHERE id = ?
                """,
                (str(uuid.uuid4()), orphan_item_id, artifact_id),
            )
        database.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="artifact identity mismatch"):
            database.execute(
                """
                INSERT INTO page_mask_reviews
                    (id, generation_id, image_id, artifact_id, sequence, state, reason,
                     mask_checksum, coverage_checks, collateral_checks, reviewer, created_at)
                VALUES (?, ?, ?, ?, 1, 'accepted', 'complete-and-no-collateral',
                        ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    image["id"],
                    artifact_id,
                    artifact["maskChecksum"],
                    json.dumps(_MASK_COVERAGE),
                    json.dumps(_MASK_COLLATERAL),
                    json.dumps(_ACTOR),
                ),
            )
        database.rollback()
        database.execute(
            "UPDATE page_mask_drafts SET recipe = ? WHERE generation_id = ?",
            ("[]", generation_id),
        )
        database.commit()
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] in {
        "g7-mask-draft-checksum-mismatch",
        "g7-mask-eligibility-mismatch",
        "g7-mask-replay-invalid",
    }


@pytest.mark.parametrize(
    "tamper",
    (
        "gate",
        "state",
        "decision",
        "reason",
        "stage",
        "parent",
        "identity",
        "input",
        "output",
        "evidence",
    ),
)
def test_g7_raw_event_matrix_tamper_is_rejected(
    client: TestClient, app, tmp_path: Path, tamper: str
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = _save_g7_default_draft(client, prepared)
    _raw_append_second_g7_draft_event(
        store,
        image_id=image["id"],
        generation_id=generation_id,
        context=context,
        tamper=tamper,
    )
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_draft_is_owned_by_its_generation_image(client: TestClient, app, tmp_path: Path) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    with store.session() as session:
        current = session.get(ImageAsset, image["id"])
        assert current is not None
        other = ImageAsset(
            project_id=current.project_id,
            name="other-page.png",
            relative_path="other-page.png",
            source_path=current.source_path,
            source_kind=current.source_kind,
            width=current.width,
            height=current.height,
            media_type=current.media_type,
            checksum=current.checksum,
        )
        session.add(other)
        session.flush()
        other_id = other.id
    _save_g7_default_draft(client, prepared)
    with sqlite3.connect(store.database_path) as database:
        database.execute(
            "UPDATE page_mask_drafts SET image_id = ? WHERE generation_id = ?",
            (other_id, generation_id),
        )
        database.commit()

    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] in {
        "g7-mask-draft-checksum-mismatch",
        "g7-mask-replay-invalid",
    }


def test_g7_rejects_non_g7_event_interleaved_after_terminal_g6(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        assert generation is not None
        session.add(
            PageLineageEvent(
                generation_id=generation.id,
                sequence=generation.next_sequence,
                operation="preprocess-job-enqueued",
                gate="G1_baselineUpscale",
                state="pending",
                actor_kind="system",
                actor_id="forged-interleave",
                operation_source="api",
                evidence={},
            )
        )
        generation.next_sequence += 1

    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


@pytest.mark.parametrize("tamper", ("job-actor", "duplicate-lineage", "region-item"))
def test_g7_raw_enqueue_identity_tamper_is_rejected(
    client: TestClient, app, tmp_path: Path, tamper: str
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    context = _save_g7_default_draft(client, prepared)
    _raw_append_g7_enqueue(
        store,
        project_id=project["id"],
        image_id=image["id"],
        generation_id=generation_id,
        context=context,
        tamper=tamper,
    )
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_raw_cancelled_current_item_is_rejected(client: TestClient, app, tmp_path: Path) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    with sqlite3.connect(store.database_path) as database:
        database.execute(
            "UPDATE job_items SET status = 'cancelled' WHERE job_id = ?",
            (queued.json()["id"],),
        )
        database.commit()
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_raw_na_with_nonzero_eligibility_is_rejected(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert context["eligibleRegionIds"]
    virtual_checksum = mask_service._draft_checksum(
        str(context["g6Checksum"]), str(context["qualityChecksum"]), {}, []
    )
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        review = PageMaskReview(
            generation_id=generation_id,
            image_id=image["id"],
            artifact_id=None,
            sequence=1,
            state="not-applicable",
            reason="no-eligible-regions",
            mask_checksum=None,
            coverage_checks=[],
            collateral_checks=[],
            reviewer={**_ACTOR, "actorId": None},
        )
        session.add(review)
        session.flush()
        virtual = PageMaskDraft(
            generation_id=generation_id,
            image_id=image["id"],
            parent_checksum=str(context["g6Checksum"]),
            quality_checksum=str(context["qualityChecksum"]),
            recipe=[],
            state_checksum=virtual_checksum,
            revision=0,
        )
        after_state = mask_service._state_checksum(
            str(context["g6Checksum"]),
            str(context["qualityChecksum"]),
            {},
            virtual,
            [],
            [review],
        )
        image_row.revision += 1
        revision = add_revision(
            session,
            store.project(session),
            entity_type="page-mask-review",
            entity_id=generation_id,
            operation="not-applicable",
            before={},
            after={"state": "not-applicable", "artifactId": None, "maskChecksum": None},
        )
        session.flush()
        session.add(
            PageLineageEvent(
                generation_id=generation_id,
                sequence=generation.next_sequence,
                operation="mask-stage-review",
                gate="G7_mask",
                state="not-applicable",
                actor_kind=_ACTOR["actorKind"],
                actor_id=None,
                task_id=_ACTOR["taskId"],
                thread_id=_ACTOR["threadId"],
                session_id=_ACTOR["sessionId"],
                operation_source=_ACTOR["operationSource"],
                input_checksum=context["g6Checksum"],
                output_checksum=after_state,
                parent_checksum=context["g6Checksum"],
                stage="mask",
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=virtual_checksum,
                revision_id=revision.id,
                decision="mask-not-applicable",
                reason="no-eligible-regions",
                evidence={
                    "eventType": "mask-stage-review",
                    "qualityState": "not-applicable",
                    "artifactId": None,
                    "maskChecksum": None,
                    "recipeChecksum": virtual_checksum,
                    "qualityChecksum": context["qualityChecksum"],
                    "eligibleRegionCount": 0,
                    "rubyRegionCount": 0,
                    "rubyRegionIdsByPrimary": {},
                    "coverageChecks": [],
                    "collateralChecks": [],
                    "imageRevision": image_row.revision,
                },
            )
        )
        generation.next_sequence += 1
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_na_rechecks_late_legacy_residual(client: TestClient, app, tmp_path: Path) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path, disposition="ignore")
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    store = prepared["store"]
    assert isinstance(project, dict) and isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert context["eligibleRegionIds"] == []
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "not-applicable",
            "reason": "no-eligible-regions",
            "selectedArtifactId": None,
            "observedMaskChecksum": None,
            "coverageChecks": [],
            "collateralChecks": [],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    terminal = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert terminal.status_code == 200 and terminal.json()["state"] == "not-applicable"
    legacy = store.root / "generated" / "masks" / Path(image["relativePath"]).with_suffix(".png")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(png_bytes(color="white"))
    stale = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert stale.status_code == 409, stale.text
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(page_lineage.PageLineageConflict, match="legacy mask"):
            require_current_mask_acceptance(store, session, image_row, generation)


def test_g7_public_structured_evidence_is_key_specific() -> None:
    event = PageLineageEvent(
        generation_id=str(uuid.uuid4()),
        sequence=1,
        operation="mask-stage-review",
        state="rejected",
        actor_kind="codex",
        task_id="lineage-test-task",
        operation_source="api",
        evidence={
            "bbox": {"x": 0, "y": 0, "width": 0, "height": 2},
            "coverageChecks": [
                {"check": f"private-{index}", "passed": False} for index in range(5)
            ],
            "collateralChecks": _MASK_COLLATERAL,
        },
    )
    public = public_page_lineage_event(event)["evidence"]
    assert "bbox" not in public
    assert "coverageChecks" not in public
    assert public["collateralChecks"] == _MASK_COLLATERAL


def test_g8_public_structured_evidence_preserves_safe_replay_fields() -> None:
    route_manifest = [
        {
            "regionId": str(uuid.uuid4()),
            "backgroundCategory": "illustration/character",
            "route": "ai-inpaint-redraw",
            "originKind": "ai",
            "provider": "lama-onnx",
            "modelVersion": "lama-onnx-local-v1",
            "parameterHash": "a" * 64,
        }
    ]
    checks = [
        {"check": check, "passed": check == "outside-mask-unchanged"}
        for check in (
            "outside-mask-unchanged",
            "source-text-unreadable",
            "no-white-or-gray-hole",
            "no-blur-band",
            "no-repeated-texture",
            "background-continuous",
            "structure-preserved",
        )
    ]
    event = PageLineageEvent(
        generation_id=str(uuid.uuid4()),
        sequence=1,
        operation="clean-plate-candidate-produced",
        state="pending",
        actor_kind="codex",
        task_id="lineage-test-task",
        operation_source="api",
        evidence={
            "g7Checksum": "b" * 64,
            "backgroundChecksum": "c" * 64,
            "maskArtifactId": str(uuid.uuid4()),
            "routeManifest": route_manifest,
            "originKind": "ai",
            "providerIds": ["lama-onnx"],
            "modelVersions": ["lama-onnx-local-v1"],
            "outsideMaskChangeCount": 0,
            "enabled": False,
            "aiCandidateCount": 1,
            "checks": checks,
            "privateText": "must-not-leak",
        },
    )
    public = public_page_lineage_event(event)["evidence"]
    assert public == {
        "g7Checksum": "b" * 64,
        "backgroundChecksum": "c" * 64,
        "maskArtifactId": event.evidence["maskArtifactId"],
        "routeManifest": route_manifest,
        "originKind": "ai",
        "providerIds": ["lama-onnx"],
        "modelVersions": ["lama-onnx-local-v1"],
        "outsideMaskChangeCount": 0,
        "enabled": False,
        "aiCandidateCount": 1,
        "checks": checks,
    }

    event.evidence["routeManifest"] = [{**route_manifest[0], "sourceText": "private"}]
    assert "routeManifest" not in public_page_lineage_event(event)["evidence"]


def test_g7_old_generation_active_item_is_ignored_but_current_one_locks(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    old_generation_id = str(uuid.uuid4())
    with store.session() as session:
        old_job = Job(
            project_id=project["id"],
            kind="mask",
            status="queued",
            total=1,
            lineage_context=_lineage_context(image["id"], old_generation_id),
        )
        old_job.items.append(
            JobItem(image_id=image["id"], region_id=None, position=0, status="queued")
        )
        session.add(old_job)
        session.flush()
        old_job_id = old_job.id
        old_item_id = old_job.items[0].id
    saved = _save_g7_default_draft(client, prepared)
    assert saved["generationId"] == generation_id
    current = _enqueue_g7(client, prepared)
    assert current.status_code == 202, current.text
    second = _enqueue_g7(client, prepared)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["reason"] == "g7-mask-job-active"
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    locked_draft = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": context["draft"]["regions"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert locked_draft.status_code == 409, locked_draft.text
    assert locked_draft.json()["detail"]["reason"] == "g7-mask-job-active"
    with store.session() as session:
        generation = session.get(PageGeneration, generation_id)
        assert generation is not None
        session.add(
            PageLineageEvent(
                generation_id=generation.id,
                sequence=generation.next_sequence,
                operation="mask-job-enqueued",
                gate="G7_mask",
                state="pending",
                actor_kind="codex",
                task_id="lineage-test-task",
                operation_source="api",
                input_checksum=context["maskStateChecksum"],
                output_checksum=context["maskStateChecksum"],
                parent_checksum=context["g6Checksum"],
                stage="mask",
                provider="deterministic-mask",
                model_version="create-mask-v1",
                parameter_hash=context["draft"]["stateChecksum"],
                job_id=old_job_id,
                job_item_id=old_item_id,
                reason="job-enqueued",
                evidence={
                    "eventType": "job-enqueued",
                    "qualityState": "pending-review",
                    "targetKind": "image",
                    "eligibleRegionCount": len(context["eligibleRegionIds"]),
                    "rubyRegionCount": sum(map(len, context["rubyRegionIdsByPrimary"].values())),
                    "rubyRegionIdsByPrimary": context["rubyRegionIdsByPrimary"],
                    "recipeChecksum": context["draft"]["stateChecksum"],
                    "qualityChecksum": context["qualityChecksum"],
                },
            )
        )
        generation.next_sequence += 1
    ghost = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert ghost.status_code == 409, ghost.text
    assert ghost.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_rejected_artifact_requires_later_draft_and_new_artifact(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    first_job = _enqueue_g7(client, prepared)
    claimed = app.state.queue._claim_next()
    assert claimed == (store, first_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    first_artifact = context["artifacts"][0]
    failed_coverage = [dict(entry) for entry in _MASK_COVERAGE]
    failed_coverage[0]["passed"] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "reject",
            "reason": "coverage-incomplete",
            "selectedArtifactId": first_artifact["artifactId"],
            "observedMaskChecksum": first_artifact["maskChecksum"],
            "coverageChecks": failed_coverage,
            "collateralChecks": _MASK_COLLATERAL,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    rejected_context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    unchanged = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "accept",
            "reason": "complete-and-no-collateral",
            "selectedArtifactId": first_artifact["artifactId"],
            "observedMaskChecksum": first_artifact["maskChecksum"],
            "coverageChecks": _MASK_COVERAGE,
            "collateralChecks": _MASK_COLLATERAL,
            "expectedRevision": rejected_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, rejected_context["nextSequence"]),
        },
    )
    assert unchanged.status_code == 409, unchanged.text
    revised = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": rejected_context["draft"]["regions"],
            "expectedRevision": rejected_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, rejected_context["nextSequence"]),
        },
    )
    assert revised.status_code == 200, revised.text
    second_job = _enqueue_g7(client, prepared)
    claimed = app.state.queue._claim_next()
    assert claimed == (store, second_job.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    second_context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert len(second_context["artifacts"]) == 2
    second_artifact = second_context["artifacts"][-1]
    assert second_artifact["artifactId"] != first_artifact["artifactId"]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "accept",
            "reason": "complete-and-no-collateral",
            "selectedArtifactId": second_artifact["artifactId"],
            "observedMaskChecksum": second_artifact["maskChecksum"],
            "coverageChecks": _MASK_COVERAGE,
            "collateralChecks": _MASK_COLLATERAL,
            "expectedRevision": second_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, second_context["nextSequence"]),
        },
    )
    assert accepted.status_code == 200, accepted.text
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        assert session.scalar(select(func.count()).select_from(PageMaskArtifact)) == 2
        assert session.scalar(select(func.count()).select_from(PageMaskReview)) == 2
        checksum, selected = require_current_mask_acceptance(store, session, image_row, generation)
        assert checksum == accepted.json()["event"]["outputChecksum"]
        assert selected is not None and selected.id == second_artifact["artifactId"]
        historical = session.get(PageMaskArtifact, first_artifact["artifactId"])
        assert historical is not None
        historical_path = store.root / historical.relative_path
    historical_path.write_bytes(b"corrupt-rejected-mask")
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(
            page_lineage.PageLineageConflict, match="Mask artifact is unavailable or changed"
        ):
            require_current_mask_acceptance(store, session, image_row, generation)


def test_g7_raw_accept_of_same_rejected_artifact_fails_replay_and_consumption(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    artifact = context["artifacts"][0]
    failed_coverage = [dict(entry) for entry in _MASK_COVERAGE]
    failed_coverage[0]["passed"] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "reject",
            "reason": "coverage-incomplete",
            "selectedArtifactId": artifact["artifactId"],
            "observedMaskChecksum": artifact["maskChecksum"],
            "coverageChecks": failed_coverage,
            "collateralChecks": _MASK_COLLATERAL,
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert rejected.status_code == 200, rejected.text
    rejected_context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    revised = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": rejected_context["draft"]["regions"],
            "expectedRevision": rejected_context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, rejected_context["nextSequence"]),
        },
    )
    assert revised.status_code == 200, revised.text
    revised_context = revised.json()
    _raw_append_g7_artifact_review(
        store,
        image_id=image["id"],
        generation_id=generation_id,
        context=revised_context,
        artifact_id=artifact["artifactId"],
        state="accepted",
        coverage_checks=_MASK_COVERAGE,
        reviewer={**_ACTOR, "actorId": None},
    )
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        with pytest.raises(page_lineage.PageLineageConflict, match="without revision/regeneration"):
            require_current_mask_acceptance(store, session, image_row, generation)


def test_g7_raw_review_actor_mismatch_is_rejected(client: TestClient, app, tmp_path: Path) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    artifact = context["artifacts"][0]
    failed_coverage = [dict(entry) for entry in _MASK_COVERAGE]
    failed_coverage[0]["passed"] = False
    _raw_append_g7_artifact_review(
        store,
        image_id=image["id"],
        generation_id=generation_id,
        context=context,
        artifact_id=artifact["artifactId"],
        state="rejected",
        coverage_checks=failed_coverage,
        reviewer={**_ACTOR, "actorId": None, "taskId": "forged-reviewer"},
    )
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_raw_matching_but_invalid_review_actor_is_rejected(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _save_g7_default_draft(client, prepared)
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    artifact = context["artifacts"][0]
    invalid_actor = {"actorKind": "evil", "operationSource": "evil"}
    _raw_append_g7_artifact_review(
        store,
        image_id=image["id"],
        generation_id=generation_id,
        context=context,
        artifact_id=artifact["artifactId"],
        state="accepted",
        coverage_checks=_MASK_COVERAGE,
        reviewer=invalid_actor,
        event_actor=invalid_actor,
    )
    replay = client.get(f"/api/images/{image['id']}/page-gates/mask")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["reason"] == "g7-mask-replay-invalid"


def test_g7_four_x_scales_manual_strokes_and_forces_linked_ruby_target(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path, include_ruby=True)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    initial = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    primary_id = initial["eligibleRegionIds"][0]
    ruby_id = initial["rubyRegionIdsByPrimary"][primary_id][0]
    fake_plate = tmp_path / "quality-4x.png"
    Image.new("RGB", (image["width"] * 4, image["height"] * 4), "white").save(fake_plate)
    fake_checksum = hashlib.sha256(fake_plate.read_bytes()).hexdigest()

    def trusted_g6(*_args, **_kwargs):
        return initial["g6Checksum"], None

    def quality_4x(*_args, **_kwargs):
        return {"checksum": fake_checksum, "path": fake_plate}, None

    monkeypatch.setattr(mask_service, "require_current_ocr_trust", trusted_g6)
    monkeypatch.setattr(mask_service, "require_current_text_present_quality_plate", quality_4x)
    monkeypatch.setattr(page_lineage, "require_current_ocr_trust", trusted_g6)
    monkeypatch.setattr(page_lineage, "require_current_text_present_quality_plate", quality_4x)
    # This raster-only regression deliberately substitutes a synthetic 4x plate after
    # G3 was accepted, so it cannot also exercise G7's real G3 checksum continuity.
    monkeypatch.setattr(mask_service, "_validate_g7_replay", lambda *_args, **_kwargs: None)
    captured: list[list[dict[str, object]]] = []

    def capture_mask(_path, regions, **_kwargs):
        captured.append(regions)
        result = np.zeros((image["height"] * 4, image["width"] * 4), dtype=np.uint8)
        result[4:8, 4:8] = 255
        return result

    monkeypatch.setattr(mask_service, "create_mask", capture_mask)
    recipe = _mask_recipe(primary_id, manual=True)
    saved = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": [recipe],
            "expectedRevision": initial["imageRevision"],
            "lineage": _mutation_lineage(generation_id, initial["nextSequence"]),
        },
    )
    assert saved.status_code == 200, saved.text
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert len(captured) == 1
    primary, ruby = captured[0]
    assert primary["maskMode"] == "manual"
    assert primary["maskEdits"]["strokes"][0]["radius"] == 12
    assert primary["maskEdits"]["strokes"][0]["points"][0] == [100, 100]
    assert ruby["maskMode"] == "region"
    regions = client.get(f"/api/images/{image['id']}/regions").json()
    ruby_row = next(row for row in regions if row["id"] == ruby_id)
    assert ruby["x"] == ruby_row["x"] * 4
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert context["artifacts"][0]["renderScale"] == 4


def test_g7_page_wide_stroke_order_can_erase_an_overlapping_region(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g6_accepted_page(client, app, tmp_path, extra_dispositions=("translate",))
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    first_id, second_id = context["eligibleRegionIds"]
    regions = client.get(f"/api/images/{image['id']}/regions").json()
    first_region = next(row for row in regions if row["id"] == first_id)
    erase_point = [
        first_region["x"] + first_region["width"] / 2,
        first_region["y"] + first_region["height"] / 2,
    ]
    first_recipe = _mask_recipe(first_id)
    first_recipe.update({"padding": 0, "dilation": 0, "feather": 0})
    second_recipe = _mask_recipe(second_id, manual=True)
    second_recipe.update(
        {
            "padding": 0,
            "dilation": 0,
            "feather": 0,
            "maskEdits": {
                "version": 1,
                "strokes": [{"mode": "erase", "radius": 5, "points": [erase_point]}],
            },
        }
    )
    saved = client.patch(
        f"/api/images/{image['id']}/page-gates/mask/draft",
        json={
            "regions": [first_recipe, second_recipe],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert saved.status_code == 200, saved.text
    queued = _enqueue_g7(client, prepared)
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    result = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    artifact = result["artifacts"][0]
    response = client.get(
        f"/api/images/{image['id']}/page-gates/mask/artifacts/{artifact['artifactId']}"
    )
    assert response.status_code == 200, response.text
    with Image.open(io.BytesIO(response.content)) as opened:
        pixels = np.asarray(opened.convert("L"))
    scale = artifact["renderScale"]
    center_x, center_y = (round(value * scale) for value in erase_point)
    protected_x = round((first_region["x"] + 4) * scale)
    protected_y = round((first_region["y"] + 4) * scale)
    assert pixels[center_y, center_x] == 0
    assert pixels[protected_y, protected_x] == 255
