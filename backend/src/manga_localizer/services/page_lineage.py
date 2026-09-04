from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TypedDict

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from manga_localizer.database import (
    G0_REVISION_NO_DELETE_TRIGGER_SQL,
    G0_REVISION_NO_UPDATE_TRIGGER_SQL,
    PAGE_LINEAGE_EVENTS_NO_DELETE_TRIGGER_SQL,
    PAGE_LINEAGE_EVENTS_NO_UPDATE_TRIGGER_SQL,
    ImageAsset,
    Job,
    JobItem,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    RegionOCRAttempt,
    Revision,
    TextRegion,
)
from manga_localizer.security import atomic_write_bytes, resolve_write_target, safe_relative_path
from manga_localizer.services.projects import (
    ProjectError,
    ProjectNotFound,
    ProjectRegistry,
    ProjectStore,
    RevisionConflict,
    add_revision,
    region_payload,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_RE = re.compile(r"^[^/\\\x00\r\n]{1,128}$")
_PARAMETER_SET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTOR_KINDS = {"codex", "cursor", "human", "system"}
_OPERATION_SOURCES = {"ui", "api", "script"}
_PIPELINE_STAGES = (
    "preprocess",
    "detection",
    "ocr",
    "translation",
    "inpaint",
    "typeset",
    "export",
)
_PROVIDER_KEYS = {
    "preprocess": ("provider", "preprocessorProvider"),
    "detect": ("provider", "detectorProvider"),
    "ocr": ("provider", "ocrProvider"),
    "translate": ("provider", "translatorProvider"),
    "inpaint": ("provider", "inpainterProvider"),
    "typeset": ("provider", "typesetterProvider"),
    "render": ("typesetterProvider", "provider"),
}
_PROJECT_PROVIDER_KEYS = {
    "preprocess": "preprocessorProvider",
    "detect": "detectorProvider",
    "ocr": "ocrProvider",
    "translate": "translatorProvider",
    "inpaint": "inpainterProvider",
    "typeset": "typesetterProvider",
    "render": "typesetterProvider",
}
_DETECT_PROVIDER_CANONICAL = {
    "tesseract": "tesseract",
    "ppocr": "ppocr-v3",
    "ppocr-v3": "ppocr-v3",
    "paddleocr-detection": "ppocr-v3",
    "ppocr-v3+tesseract": "ppocr-v3+tesseract",
    "union": "ppocr-v3+tesseract",
    "ppocr-tesseract": "ppocr-v3+tesseract",
}
_JOB_GATES = {
    "preprocess": "G1_baselineUpscale",
    "detect": "G4_regions",
    "ocr": "G6_ocr",
    "mask": "G7_mask",
    "inpaint": "G8_cleanPlate",
    "translate": "G9_translation",
    "typeset": "G10_typeset",
    "render": "G10_typeset",
}
_JOB_STAGES = {
    "preprocess": "preprocess",
    "detect": "detection",
    "ocr": "ocr",
    "mask": "mask",
    "inpaint": "inpaint",
    "translate": "translation",
    "typeset": "typeset",
    "render": "typeset",
    "export": "export",
}
_SUPPORTED_LINEAGE_JOB_KINDS = {
    "preprocess",
    "detect",
    "ocr",
    "mask",
    "inpaint",
    "translate",
    "typeset",
}
_RECONSTRUCTION_REASONS = {
    "no": {"baseline-preserves-original-structure"},
    "yes": {
        "fine-lines-remain-insufficient",
        "screentone-remains-insufficient",
        "illustration-detail-remains-insufficient",
        "structure-remains-uncertain",
    },
}
_TEXT_PRESENCE_REASONS = {
    "yes": "processable-text-visible",
    "no": "no-processable-text-visible",
    "uncertain": "visual-evidence-uncertain",
}
_TEXT_PRESENCE_EVIDENCE = {
    "original-and-quality-compared",
    "dialogue-visible",
    "narration-visible",
    "title-visible",
    "sfx-visible",
    "art-lettering-visible",
    "environment-text-visible",
    "no-processable-text-visible",
    "conflicting-signals",
    "detector-support",
    "ocr-support",
}
_VISUAL_TEXT_EVIDENCE = {
    "dialogue-visible",
    "narration-visible",
    "title-visible",
    "sfx-visible",
    "art-lettering-visible",
    "environment-text-visible",
}
_G4_CONTENT_DISPOSITIONS = {
    "translate",
    "ignore",
    "keep-art",
    "redraw-art",
    "false-positive",
}
_G4_MUTATION_OPERATIONS = {
    "regions-created",
    "regions-updated",
    "regions-deleted",
    "regions-reordered",
}
_BACKGROUND_RATIONALE_ANCHORS = {
    "white-solid": "uniform-near-white",
    "black-solid": "uniform-near-black",
    "other-solid": "uniform-other-color",
    "simple-gradient": "smooth-gradient-continuity",
    "screentone": "periodic-screentone",
    "complex-lineart": "structural-lines-cross-region",
    "illustration/character": "character-or-illustration-detail",
}
_BACKGROUND_RATIONALE_CODES = set(_BACKGROUND_RATIONALE_ANCHORS.values()) | {"mixed-visual-signals"}
_G5_MUTATION_OPERATIONS = {"background-classification-reviewed"}
_G6_MUTATION_OPERATIONS = {"ocr-attempts-produced", "ocr-source-reviewed"}
OCR_QC_CHECKS = {
    "original-and-quality-compared",
    "source-text-characters-checked",
    "punctuation-checked",
    "direction-checked",
    "reading-order-checked",
    "empty-or-garbled-checked",
    "duplicate-fragment-checked",
    "template-contamination-checked",
    "page-text-consistency-checked",
}
OCR_QC_FLAGS = {
    "none",
    "original-quality-disagree",
    "low-japanese-character-ratio",
    "ocr-empty-attempt",
    "ocr-garbled-attempt",
    "duplicate-fragment",
    "template-contamination",
    "manual-correction",
}
_OCR_SOURCE_MODES = {"original-attempt", "quality-attempt", "manual-correction"}
_OCR_TEMPLATE_RE = re.compile(
    r"(?:联系我们|联系人|客服|免责声明|作为.{0,12}(?:AI|人工智能)|语言模型)",
    re.IGNORECASE,
)
_TEXT_DOWNSTREAM_STAGES = {"detection", "ocr", "translation", "inpaint", "typeset"}
_TEXT_DOWNSTREAM_JOB_KINDS = {"detect", "ocr", "translate", "inpaint", "typeset", "render"}
_PUBLIC_EVIDENCE_KEYS = {
    "eventType",
    "imageRevision",
    "qualityState",
    "maskChecksum",
    "maskArtifactId",
    "provenanceDigest",
    "restartFromSource",
    "finalReviewItemId",
    "finalReviewItemRevision",
    "feedbackChecksum",
    "repairIdentityVersion",
    "repairAttempt",
    "retryFromGenerationId",
    "predecessorGenerationId",
    "successorGenerationId",
    "successorAttempt",
    "predecessorStateChecksum",
    "targetImageId",
    "targetKind",
    "reconstructionRequired",
    "regionCount",
    "regionOperation",
    "recovered",
    "recoveryKind",
    "revisionIds",
    "revisionWitnessChecksum",
    "targetRegionId",
    "selectedAttemptId",
    "eligibleRegionCount",
    "classifiedRegionCount",
    "attemptedRegionCount",
    "reviewedRegionCount",
    "ocrAttemptCount",
    "candidateCount",
    "aiCandidateCount",
    "renderRegionCount",
    "candidateId",
    "candidateChecksum",
    "revisionNumber",
    "recipeRegionCount",
    "recipeChecksum",
    "qualityChecksum",
    "g7Checksum",
    "backgroundChecksum",
    "artifactId",
    "width",
    "height",
    "renderScale",
    "nonzeroPixelCount",
    "bbox",
    "rubyRegionCount",
    "rubyRegionIdsByPrimary",
    "coverageChecks",
    "collateralChecks",
    "provider",
    "modelVersion",
    "parameterHash",
    "g9TerminalChecksum",
    "cleanPlateChecksum",
    "routeChecksum",
    "styleChecksum",
    "layoutChecksum",
    "overflowRegionIds",
    "anomalies",
    "outsideMaskChangeCount",
    "routeManifest",
    "originKind",
    "providerIds",
    "modelVersions",
    "enabled",
    "checks",
    "textPresence",
    "visualComparison",
}


class PageLineageConflict(ProjectError):
    def __init__(
        self,
        message: str,
        *,
        resource: str,
        reason: str,
        expected_sequence: int | None = None,
        actual_sequence: int | None = None,
    ):
        super().__init__(message)
        self.resource = resource
        self.reason = reason
        self.expected_sequence = expected_sequence
        self.actual_sequence = actual_sequence


class JobMutationBinding(TypedDict):
    generationId: str
    sourceChecksum: str
    jobId: str
    jobItemId: str
    actor: dict[str, str | None]
    startedAt: datetime | None


class QualityPlateBinding(TypedDict):
    path: Path
    checksum: str
    targetKind: str
    eventSequence: int


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as error:
        raise ProjectError("Immutable source image could not be read") from error


def _immutable_image_path(store: ProjectStore, image: ImageAsset) -> Path:
    relative = safe_relative_path(image.source_path)
    if not relative.parts or relative.parts[0] != "source":
        raise ProjectError("Image source path is outside immutable source storage")
    source = resolve_write_target(store.root, relative)
    if not source.is_file():
        raise ProjectError("Immutable source image is missing")
    return source


def _decoded_image_resolution(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            return opened.size
    except (OSError, ValueError) as error:
        raise ProjectError("Immutable source image could not be decoded") from error


def _json_values_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool/int/float equality coercions."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _json_values_equal(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _json_values_equal(actual_value, expected_value)
            for actual_value, expected_value in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalized_trigger_sql(value: str) -> str:
    normalized = " ".join(value.strip().rstrip(";").split())
    return normalized.replace("CREATE TRIGGER IF NOT EXISTS", "CREATE TRIGGER", 1)


def _require_g0_identity_guards(session: Session) -> None:
    rows = session.connection().exec_driver_sql(
        """SELECT name, sql FROM sqlite_master
        WHERE type = 'trigger'
          AND name IN (
              'revisions_g0_no_update',
              'revisions_g0_no_delete',
              'page_lineage_events_no_update',
              'page_lineage_events_no_delete'
          )"""
    )
    actual = {
        str(row[0]): _normalized_trigger_sql(str(row[1])) for row in rows if row[1] is not None
    }
    expected = {
        "revisions_g0_no_update": _normalized_trigger_sql(G0_REVISION_NO_UPDATE_TRIGGER_SQL),
        "revisions_g0_no_delete": _normalized_trigger_sql(G0_REVISION_NO_DELETE_TRIGGER_SQL),
        "page_lineage_events_no_update": _normalized_trigger_sql(
            PAGE_LINEAGE_EVENTS_NO_UPDATE_TRIGGER_SQL
        ),
        "page_lineage_events_no_delete": _normalized_trigger_sql(
            PAGE_LINEAGE_EVENTS_NO_DELETE_TRIGGER_SQL
        ),
    }
    if actual != expected:
        raise ProjectError("G0 identity evidence is not protected as append-only")


def _safe_actor(actor: dict[str, Any]) -> dict[str, str | None]:
    actor_kind = actor.get("actorKind")
    operation_source = actor.get("operationSource")
    if actor_kind not in _ACTOR_KINDS or operation_source not in _OPERATION_SOURCES:
        raise PageLineageConflict(
            "Lineage actor metadata is invalid",
            resource="lineage-actor",
            reason="invalid-actor",
        )
    result: dict[str, str | None] = {
        "actorKind": str(actor_kind),
        "actorId": None,
        "taskId": None,
        "threadId": None,
        "sessionId": None,
        "operationSource": str(operation_source),
    }
    for key in ("actorId", "taskId", "threadId", "sessionId"):
        value = actor.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
            raise PageLineageConflict(
                "Lineage actor identity must be an opaque single-line value",
                resource="lineage-actor",
                reason="invalid-actor-id",
            )
        result[key] = value
    if not any(result[key] for key in ("actorId", "taskId", "threadId", "sessionId")):
        raise PageLineageConflict(
            "Lineage actor requires an identity anchor",
            resource="lineage-actor",
            reason="missing-actor-id",
        )
    return result


def _actor_columns(actor: dict[str, str | None]) -> dict[str, str | None]:
    return {
        "actor_kind": actor["actorKind"],
        "actor_id": actor["actorId"],
        "task_id": actor["taskId"],
        "thread_id": actor["threadId"],
        "session_id": actor["sessionId"],
        "operation_source": actor["operationSource"],
    }


def _public_actor(record: PageGeneration | PageLineageEvent) -> dict[str, str | None]:
    return _safe_actor(
        {
            "actorKind": record.actor_kind,
            "actorId": record.actor_id,
            "taskId": record.task_id,
            "threadId": record.thread_id,
            "sessionId": record.session_id,
            "operationSource": record.operation_source,
        }
    )


def _freshness_mismatches(store: ProjectStore, image: ImageAsset, session: Session) -> list[str]:
    mismatches: list[str] = []
    status = image.status or {}
    if image.revision != 1:
        mismatches.append("image-revision")
    for stage in _PIPELINE_STAGES:
        if status.get(stage, "pending") != "pending":
            mismatches.append(f"stage:{stage}")
    if status.get("reviewState", "pending") != "pending" or status.get("reviewedAt"):
        mismatches.append("page-review")
    if status.get("stageReviews"):
        mismatches.append("stage-reviews")
    if any(
        status.get(key)
        for key in (
            "renderInputVariant",
            "renderScale",
            "renderedSize",
            "inpaintCandidate",
            "inpaintCandidates",
            "typesetOverflowCount",
            "typesetOverflowRegionIds",
        )
    ):
        mismatches.append("derived-status")
    if image.inpaint_provenance is not None:
        mismatches.append("inpaint-provenance")
    if image.inpaint_classical_approval is not None:
        mismatches.append("classical-approval")
    if image.inpaint_ai_candidate_reviews is not None:
        mismatches.append("candidate-reviews")
    if image.processing_errors:
        mismatches.append("processing-errors")
    if session.scalar(select(TextRegion.id).where(TextRegion.image_id == image.id).limit(1)):
        mismatches.append("regions")

    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    for directory in ("preprocessed", "masks", "inpainted", "typeset"):
        artifact = resolve_write_target(
            store.root,
            Path("generated") / directory / relative,
            protected_roots=(store.source_root,),
        )
        if artifact.exists():
            mismatches.append(f"artifact:{directory}")
    candidate_root = resolve_write_target(
        store.root,
        Path("generated") / "inpaint-candidates" / relative.with_suffix(""),
        protected_roots=(store.source_root,),
    )
    if candidate_root.exists():
        mismatches.append("artifact:inpaint-candidates")
    return mismatches


def _append_event(
    session: Session,
    generation: PageGeneration,
    *,
    operation: str,
    state: str,
    actor: dict[str, str | None],
    gate: str | None = None,
    input_checksum: str | None = None,
    output_checksum: str | None = None,
    parent_checksum: str | None = None,
    stage: str | None = None,
    provider: str | None = None,
    model_version: str | None = None,
    parameter_hash: str | None = None,
    job_id: str | None = None,
    job_item_id: str | None = None,
    revision_id: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
    git_commit: str | None = None,
    evidence: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    expected_sequence: int | None = None,
) -> PageLineageEvent:
    sequence_update = update(PageGeneration).where(PageGeneration.id == generation.id)
    if expected_sequence is not None:
        sequence_update = sequence_update.where(PageGeneration.next_sequence == expected_sequence)
    next_sequence = session.scalar(
        sequence_update.values(next_sequence=PageGeneration.next_sequence + 1).returning(
            PageGeneration.next_sequence
        )
    )
    if not isinstance(next_sequence, int):
        actual_sequence = session.scalar(
            select(PageGeneration.next_sequence).where(PageGeneration.id == generation.id)
        )
        if expected_sequence is not None and isinstance(actual_sequence, int):
            raise PageLineageConflict(
                "Page lineage changed before the mutation evidence was appended",
                resource=f"page-generation:{generation.id}",
                reason="sequence-conflict",
                expected_sequence=expected_sequence,
                actual_sequence=actual_sequence,
            )
        raise PageLineageConflict(
            "Page generation disappeared while evidence was being appended",
            resource=f"page-generation:{generation.id}",
            reason="generation-missing",
        )
    event = PageLineageEvent(
        generation_id=generation.id,
        sequence=next_sequence - 1,
        operation=operation,
        gate=gate,
        state=state,
        **_actor_columns(actor),
        input_checksum=input_checksum,
        output_checksum=output_checksum,
        parent_checksum=parent_checksum,
        stage=stage,
        provider=provider,
        model_version=model_version,
        parameter_hash=parameter_hash,
        job_id=job_id,
        job_item_id=job_item_id,
        revision_id=revision_id,
        decision=decision,
        reason=reason,
        git_commit=git_commit,
        evidence=evidence or {},
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(event)
    session.flush()
    return event


def _page_generation_revision_after(generation: PageGeneration) -> dict[str, Any]:
    return {
        "runId": generation.run_id,
        "pageGenerationId": generation.id,
        "parameterSetId": generation.parameter_set_id,
        "parameterSetHash": generation.parameter_set_hash,
        "restartFromSource": True,
        "sourceProjectId": generation.source_project_id,
        "sourceImageId": generation.source_image_id,
        "sourceChecksum": generation.source_checksum,
        "state": "active",
    }


def _page_generation_g0_evidence(generation: PageGeneration, image: ImageAsset) -> dict[str, Any]:
    return {
        "eventType": "generation-created",
        "restartFromSource": True,
        "sourceRelativePath": generation.source_relative_path,
        "targetImageId": image.id,
        "targetRelativePath": image.relative_path,
    }


def create_page_generation(
    registry: ProjectRegistry,
    store: ProjectStore,
    image_id: str,
    *,
    run_id: str,
    page_generation_id: str,
    parameter_set_id: str,
    parameter_set_hash: str,
    restart_from_source: bool,
    source_project_id: str,
    source_image_id: str,
    expected_source_checksum: str,
    expected_revision: int,
    actor: dict[str, Any],
) -> PageGeneration:
    resource = f"image:{image_id}"
    normalized_actor = _safe_actor(actor)
    if not restart_from_source:
        raise PageLineageConflict(
            "A rework page generation must restart from immutable source",
            resource=resource,
            reason="restart-required",
        )
    if (
        not _PARAMETER_SET_ID_RE.fullmatch(run_id)
        or not _PARAMETER_SET_ID_RE.fullmatch(parameter_set_id)
        or not _SHA256_RE.fullmatch(parameter_set_hash)
        or not _SHA256_RE.fullmatch(expected_source_checksum)
    ):
        raise PageLineageConflict(
            "Page generation checksum metadata is invalid",
            resource=resource,
            reason="invalid-checksum",
        )

    # CAS is checked before source I/O, then checked again in the write
    # transaction so a concurrent page mutation cannot slip into the lineage.
    with store.session() as session:
        _require_g0_identity_guards(session)
        target_preflight = session.get(ImageAsset, image_id)
        if target_preflight is None:
            raise ProjectError("Page generation image was not found")
        if target_preflight.revision != expected_revision:
            raise RevisionConflict(
                "Image changed before the page generation was created",
                expected_revision=expected_revision,
                actual_revision=target_preflight.revision,
                resource=resource,
            )

    for open_store in registry.stores():
        with open_store.session() as session:
            if session.get(PageGeneration, page_generation_id) is not None:
                raise PageLineageConflict(
                    "Page generation id is already in use",
                    resource=f"page-generation:{page_generation_id}",
                    reason="generation-id-conflict",
                )

    source_store, source_image = registry.find_image(source_image_id)
    if source_image.project_id != source_project_id:
        raise PageLineageConflict(
            "Source image does not belong to the declared source project",
            resource=resource,
            reason="source-identity-mismatch",
        )
    source_actual_checksum = _sha256_file(_immutable_image_path(source_store, source_image))
    if (
        source_image.checksum != expected_source_checksum
        or source_actual_checksum != expected_source_checksum
    ):
        raise PageLineageConflict(
            "Immutable source checksum does not match the declared identity",
            resource=resource,
            reason="source-checksum-mismatch",
        )

    with store.session() as session:
        _require_g0_identity_guards(session)
        project = store.project(session)
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Page generation image was not found")
        if image.revision != expected_revision:
            raise RevisionConflict(
                "Image changed before the page generation was created",
                expected_revision=expected_revision,
                actual_revision=image.revision,
                resource=resource,
            )
        if session.scalar(
            select(PageGeneration.id).where(
                PageGeneration.image_id == image.id,
                PageGeneration.state == "active",
            )
        ):
            raise PageLineageConflict(
                "Image already has an active page generation",
                resource=resource,
                reason="active-generation-exists",
            )
        mismatches = _freshness_mismatches(store, image, session)
        if mismatches:
            raise PageLineageConflict(
                "Target page is not a fresh immutable-source workspace",
                resource=resource,
                reason="target-not-fresh:" + ",".join(sorted(mismatches)),
            )
        target_actual_checksum = _sha256_file(_immutable_image_path(store, image))
        if image.relative_path != source_image.relative_path:
            raise PageLineageConflict(
                "Target page relative path does not match the declared source identity",
                resource=resource,
                reason="target-relative-path-mismatch",
            )
        if image.checksum != expected_source_checksum or target_actual_checksum != image.checksum:
            raise PageLineageConflict(
                "Target page is not an exact copy of the immutable source",
                resource=resource,
                reason="target-checksum-mismatch",
            )

        now = datetime.now(UTC)
        generation = PageGeneration(
            id=page_generation_id,
            run_id=run_id,
            project_id=project.id,
            image_id=image.id,
            restart_from_source=True,
            parameter_set_id=parameter_set_id,
            parameter_set_hash=parameter_set_hash,
            source_project_id=source_project_id,
            source_image_id=source_image_id,
            source_checksum=expected_source_checksum,
            source_relative_path=source_image.relative_path,
            state="active",
            next_sequence=1,
            **_actor_columns(normalized_actor),
            created_at=now,
        )
        session.add(generation)
        image.revision += 1
        revision = add_revision(
            session,
            project,
            entity_type="page-generation",
            entity_id=generation.id,
            operation="create",
            before=None,
            after=_page_generation_revision_after(generation),
        )
        session.flush()
        _append_event(
            session,
            generation,
            operation="generation-created",
            gate="G0_identity",
            state="accepted",
            actor=normalized_actor,
            input_checksum=expected_source_checksum,
            output_checksum=target_actual_checksum,
            parameter_hash=parameter_set_hash,
            revision_id=revision.id,
            decision="restart-from-immutable-source",
            reason="source-and-target-checksums-match",
            evidence=_page_generation_g0_evidence(generation, image),
            started_at=now,
            finished_at=now,
        )
        session.flush()
    store.write_snapshot()
    with store.session() as session:
        created = session.get(PageGeneration, page_generation_id)
        if created is None:
            raise ProjectError("Page generation could not be reloaded")
        return created


def _require_generic_page_generation_identity(
    registry: ProjectRegistry,
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
    g0: PageLineageEvent,
) -> PageLineageEvent:
    project = store.project(session)
    if (
        generation.project_id != project.id
        or generation.image_id != image.id
        or image.project_id != project.id
        or generation.restart_from_source is not True
        or generation.state != "active"
        or generation.closed_at is not None
        or generation.created_at is None
        or not _PARAMETER_SET_ID_RE.fullmatch(generation.run_id)
        or not _PARAMETER_SET_ID_RE.fullmatch(generation.parameter_set_id)
        or not _SHA256_RE.fullmatch(generation.parameter_set_hash)
        or not _SHA256_RE.fullmatch(generation.source_checksum)
    ):
        raise ProjectError("Page generation identity provenance is inconsistent")

    source_store, source = registry.find_image(generation.source_image_id)
    source_path = _immutable_image_path(source_store, source)
    target_path = _immutable_image_path(store, image)
    source_actual_checksum = _sha256_file(source_path)
    target_actual_checksum = _sha256_file(target_path)
    if (
        source.project_id != generation.source_project_id
        or source.relative_path != generation.source_relative_path
        or source.checksum != generation.source_checksum
        or source_actual_checksum != generation.source_checksum
        or image.relative_path != source.relative_path
        or image.checksum != generation.source_checksum
        or target_actual_checksum != generation.source_checksum
    ):
        raise ProjectError("Page generation immutable source binding is inconsistent")

    try:
        actor_matches = _public_actor(generation) == _public_actor(g0)
    except ProjectError as error:
        raise ProjectError("Page generation identity provenance is inconsistent") from error
    if (
        g0.sequence != 1
        or g0.operation != "generation-created"
        or g0.state != "accepted"
        or g0.input_checksum != generation.source_checksum
        or g0.output_checksum != target_actual_checksum
        or g0.parent_checksum is not None
        or g0.stage is not None
        or g0.provider is not None
        or g0.model_version is not None
        or g0.parameter_hash != generation.parameter_set_hash
        or g0.job_id is not None
        or g0.job_item_id is not None
        or g0.revision_id is None
        or g0.decision != "restart-from-immutable-source"
        or g0.reason != "source-and-target-checksums-match"
        or g0.git_commit is not None
        or not _json_values_equal(g0.evidence, _page_generation_g0_evidence(generation, image))
        or g0.started_at is None
        or g0.finished_at is None
        or g0.started_at != generation.created_at
        or g0.finished_at != generation.created_at
        or g0.created_at is None
        or not actor_matches
    ):
        raise ProjectError("Page generation identity provenance is inconsistent")

    revisions = list(
        session.scalars(
            select(Revision).where(
                Revision.project_id == project.id,
                Revision.entity_type == "page-generation",
                Revision.entity_id == generation.id,
                Revision.operation == "create",
            )
        ).all()
    )
    if (
        len(revisions) != 1
        or revisions[0].id != g0.revision_id
        or revisions[0].before is not None
        or not _json_values_equal(revisions[0].after, _page_generation_revision_after(generation))
        or revisions[0].project_revision < 1
        or revisions[0].created_at is None
    ):
        raise ProjectError("Page generation creation revision is inconsistent")
    return g0


def require_current_page_generation_identity(
    registry: ProjectRegistry,
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
) -> PageLineageEvent:
    _require_g0_identity_guards(session)
    events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(PageLineageEvent.generation_id == generation.id)
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    if generation.next_sequence < 2 or [event.sequence for event in events] != list(
        range(1, generation.next_sequence)
    ):
        raise ProjectError("Page generation event sequence is inconsistent")
    g0_events = [event for event in events if event.gate == "G0_identity"]
    if len(g0_events) != 1:
        raise ProjectError("Page generation G0 identity is not unique")
    g0 = g0_events[0]
    evidence = g0.evidence if isinstance(g0.evidence, dict) else {}
    repair_markers = {
        "finalReviewItemId",
        "finalReviewItemRevision",
        "feedbackChecksum",
    }
    if (
        image.source_kind == "final-review-repair"
        or g0.reason == "final-review-issue-repair"
        or bool(repair_markers & evidence.keys())
    ):
        item_id = evidence.get("finalReviewItemId")
        item_revision = evidence.get("finalReviewItemRevision")
        feedback_checksum = evidence.get("feedbackChecksum")
        if (
            type(item_id) is not str
            or not _OPAQUE_ID_RE.fullmatch(item_id)
            or type(item_revision) is not int
            or item_revision < 1
            or type(feedback_checksum) is not str
            or not _SHA256_RE.fullmatch(feedback_checksum)
        ):
            raise ProjectError("Final-review repair G0 identity is invalid")
        repair = find_final_review_repair_generation(
            store,
            session,
            source_project_id=generation.source_project_id,
            source_image_id=generation.source_image_id,
            source_relative_path=generation.source_relative_path,
            final_review_item_id=item_id,
            final_review_item_revision=item_revision,
            feedback_checksum=feedback_checksum,
            parameter_set_id=generation.parameter_set_id,
            parameter_set_hash=generation.parameter_set_hash,
        )
        if (
            repair is None
            or repair[0].id != image.id
            or repair[1].id != generation.id
            or g0.generation_id != generation.id
        ):
            raise ProjectError("Final-review repair G0 identity is inconsistent")
        return g0
    return _require_generic_page_generation_identity(
        registry, store, session, image, generation, g0
    )


def _final_review_repair_relative_path(
    source: ImageAsset,
    final_review_item_id: str,
    final_review_item_revision: int,
    repair_attempt: int = 1,
) -> Path:
    root = (
        Path("final-review-repairs") / final_review_item_id / f"r{final_review_item_revision:06d}"
    )
    if repair_attempt > 1:
        root /= f"a{repair_attempt:06d}"
    return root / safe_relative_path(source.relative_path).name


def _final_review_repair_attempt_fields(
    repair_attempt: int,
    retry_from_generation_id: str | None,
) -> dict[str, Any]:
    if repair_attempt == 1 and retry_from_generation_id is None:
        return {}
    if (
        type(repair_attempt) is not int
        or repair_attempt < 2
        or not isinstance(retry_from_generation_id, str)
        or not _OPAQUE_ID_RE.fullmatch(retry_from_generation_id)
    ):
        raise ProjectError("Final-review repair retry identity is invalid")
    return {
        "repairIdentityVersion": 2,
        "repairAttempt": repair_attempt,
        "retryFromGenerationId": retry_from_generation_id,
    }


def _final_review_repair_attempt_from_evidence(
    evidence: dict[str, Any],
) -> tuple[int, str | None]:
    retry_keys = {
        "repairIdentityVersion",
        "repairAttempt",
        "retryFromGenerationId",
    }
    present = retry_keys & evidence.keys()
    if not present:
        return 1, None
    if present != retry_keys:
        raise ProjectError("Final-review repair retry identity is incomplete")
    attempt = evidence["repairAttempt"]
    retry_from = evidence["retryFromGenerationId"]
    if (
        evidence["repairIdentityVersion"] != 2
        or type(attempt) is not int
        or attempt < 2
        or not isinstance(retry_from, str)
        or not _OPAQUE_ID_RE.fullmatch(retry_from)
    ):
        raise ProjectError("Final-review repair retry identity is invalid")
    return attempt, retry_from


def final_review_repair_attempt_context(
    session: Session,
    generation: PageGeneration,
) -> tuple[int, str | None]:
    rows = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G0_identity",
            )
        ).all()
    )
    if len(rows) != 1 or not isinstance(rows[0].evidence, dict):
        raise ProjectError("Final-review repair G0 identity is not unique")
    return _final_review_repair_attempt_from_evidence(rows[0].evidence)


def _final_review_repair_supersession_evidence(
    predecessor: PageGeneration,
    *,
    successor_generation_id: str,
    successor_attempt: int,
    prior_event: PageLineageEvent,
    transition_sequence: int,
) -> dict[str, Any]:
    predecessor_state_checksum = _digest(
        {
            "generationId": predecessor.id,
            "nextSequence": transition_sequence,
            "lastEventId": prior_event.id,
            "lastEventSequence": prior_event.sequence,
            "lastEventOperation": prior_event.operation,
            "lastEventState": prior_event.state,
            "lastEventInputChecksum": prior_event.input_checksum,
            "lastEventOutputChecksum": prior_event.output_checksum,
        }
    )
    return {
        "eventType": "generation-superseded",
        "predecessorGenerationId": predecessor.id,
        "successorGenerationId": successor_generation_id,
        "successorAttempt": successor_attempt,
        "predecessorStateChecksum": predecessor_state_checksum,
    }


def _final_review_repair_supersession_revision_after(
    *,
    successor_generation_id: str,
    closed_at: datetime,
) -> dict[str, Any]:
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=UTC)
    return {
        "state": "superseded",
        "closedAt": closed_at.isoformat(),
        "successorGenerationId": successor_generation_id,
    }


def _final_review_repair_revision_after(
    *,
    generation: PageGeneration,
    target: ImageAsset,
    source: ImageAsset,
    final_review_item_id: str,
    final_review_item_revision: int,
    feedback_checksum: str,
    repair_attempt: int = 1,
    retry_from_generation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "runId": generation.run_id,
        "pageGenerationId": generation.id,
        "targetImageId": target.id,
        "restartFromSource": True,
        "parameterSetId": generation.parameter_set_id,
        "parameterSetHash": generation.parameter_set_hash,
        "sourceProjectId": source.project_id,
        "sourceImageId": source.id,
        "sourceChecksum": source.checksum,
        "finalReviewItemId": final_review_item_id,
        "finalReviewItemRevision": final_review_item_revision,
        "feedbackChecksum": feedback_checksum,
        **_final_review_repair_attempt_fields(
            repair_attempt,
            retry_from_generation_id,
        ),
    }


def _final_review_repair_g0_evidence(
    *,
    generation: PageGeneration,
    target: ImageAsset,
    source: ImageAsset,
    final_review_item_id: str,
    final_review_item_revision: int,
    feedback_checksum: str,
    repair_attempt: int = 1,
    retry_from_generation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "eventType": "generation-created",
        "restartFromSource": True,
        "parameterSetId": generation.parameter_set_id,
        "parameterSetHash": generation.parameter_set_hash,
        "sourceRelativePath": source.relative_path,
        "targetImageId": target.id,
        "targetRelativePath": target.relative_path,
        "finalReviewItemId": final_review_item_id,
        "finalReviewItemRevision": final_review_item_revision,
        "feedbackChecksum": feedback_checksum,
        **_final_review_repair_attempt_fields(
            repair_attempt,
            retry_from_generation_id,
        ),
    }


def find_final_review_repair_generation(
    store: ProjectStore,
    session: Session,
    *,
    source_project_id: str,
    source_image_id: str,
    source_relative_path: str | None,
    final_review_item_id: str,
    final_review_item_revision: int,
    feedback_checksum: str,
    parameter_set_id: str | None = None,
    parameter_set_hash: str | None = None,
) -> tuple[ImageAsset, PageGeneration] | None:
    """Return the fully bound repair chain head, or ``None`` when none exists.

    Identity is discovered from every G0 event in the project rather than from
    mutable generation source columns.  Any duplicate identity, malformed
    creation record, or source/target drift fails closed before idempotent reuse.
    """
    _require_g0_identity_guards(session)
    if (parameter_set_id is None) != (parameter_set_hash is None):
        raise ProjectError("Repair parameter identity must be supplied as a complete pair")
    if not _SHA256_RE.fullmatch(feedback_checksum):
        raise ProjectError("Final-review repair feedback checksum is invalid")

    project = store.project(session)
    source = session.get(ImageAsset, source_image_id)
    if (
        project.id != source_project_id
        or source is None
        or source.project_id != source_project_id
        or (source_relative_path is not None and source.relative_path != source_relative_path)
        or not _SHA256_RE.fullmatch(source.checksum)
        or source.width <= 0
        or source.height <= 0
    ):
        raise ProjectError("Final-review repair immutable source binding is inconsistent")
    source_path = _immutable_image_path(store, source)
    if _sha256_file(source_path) != source.checksum or _decoded_image_resolution(source_path) != (
        source.width,
        source.height,
    ):
        raise ProjectError("Final-review repair immutable source binding is inconsistent")

    identity = {
        "finalReviewItemId": final_review_item_id,
        "finalReviewItemRevision": final_review_item_revision,
        "feedbackChecksum": feedback_checksum,
    }
    candidates: list[PageLineageEvent] = []
    for event in session.scalars(
        select(PageLineageEvent).where(PageLineageEvent.gate == "G0_identity")
    ):
        evidence = event.evidence
        if isinstance(evidence, dict) and all(
            key in evidence and _json_values_equal(evidence[key], value)
            for key, value in identity.items()
        ):
            candidates.append(event)
    if not candidates:
        return None

    chain: list[tuple[int, str | None, PageLineageEvent, PageGeneration]] = []
    for candidate in candidates:
        evidence = candidate.evidence
        if not isinstance(evidence, dict):
            raise ProjectError("Existing repair handoff provenance is inconsistent")
        attempt, retry_from = _final_review_repair_attempt_from_evidence(evidence)
        candidate_generation = session.get(PageGeneration, candidate.generation_id)
        if candidate_generation is None:
            raise ProjectError("Existing repair handoff provenance is inconsistent")
        chain.append((attempt, retry_from, candidate, candidate_generation))
    chain.sort(key=lambda entry: entry[0])
    if [entry[0] for entry in chain] != list(range(1, len(chain) + 1)):
        raise ProjectError("Final-review repair retry chain is ambiguous")
    for index, (attempt, retry_from, _candidate, candidate_generation) in enumerate(chain):
        predecessor_id = chain[index - 1][3].id if index else None
        is_head = index == len(chain) - 1
        expected_run_id = f"final-review-{final_review_item_id[:8]}-r{final_review_item_revision}"
        if attempt > 1:
            expected_run_id = f"{expected_run_id}-a{attempt}"
        if (
            retry_from != predecessor_id
            or candidate_generation.run_id != expected_run_id
            or candidate_generation.project_id != source_project_id
            or candidate_generation.source_project_id != source_project_id
            or candidate_generation.source_image_id != source.id
            or candidate_generation.source_checksum != source.checksum
            or candidate_generation.source_relative_path != source.relative_path
            or candidate_generation.restart_from_source is not True
            or candidate_generation.created_at is None
            or candidate_generation.next_sequence < 2
            or not _PARAMETER_SET_ID_RE.fullmatch(candidate_generation.parameter_set_id)
            or not _SHA256_RE.fullmatch(candidate_generation.parameter_set_hash)
            or (
                is_head
                and (
                    candidate_generation.state != "active"
                    or candidate_generation.closed_at is not None
                )
            )
            or (
                not is_head
                and (
                    candidate_generation.state != "superseded"
                    or candidate_generation.closed_at is None
                )
            )
        ):
            raise ProjectError("Existing repair handoff provenance is inconsistent")
        if not is_head:
            historical_events = list(
                session.scalars(
                    select(PageLineageEvent)
                    .where(PageLineageEvent.generation_id == candidate_generation.id)
                    .order_by(PageLineageEvent.sequence)
                ).all()
            )
            if [event.sequence for event in historical_events] != list(
                range(1, candidate_generation.next_sequence)
            ) or len(historical_events) < 2:
                raise ProjectError("Existing repair handoff provenance is inconsistent")
            transition = historical_events[-1]
            prior_event = historical_events[-2]
            successor = chain[index + 1][3]
            expected_evidence = _final_review_repair_supersession_evidence(
                candidate_generation,
                successor_generation_id=successor.id,
                successor_attempt=attempt + 1,
                prior_event=prior_event,
                transition_sequence=transition.sequence,
            )
            transition_checksum = _digest(expected_evidence)
            transition_revision = session.get(Revision, transition.revision_id)
            if (
                transition.operation != "generation-superseded"
                or transition.gate != "G0_retry"
                or transition.state != "accepted"
                or transition.input_checksum != expected_evidence["predecessorStateChecksum"]
                or transition.output_checksum != transition_checksum
                or transition.parent_checksum != expected_evidence["predecessorStateChecksum"]
                or transition.stage is not None
                or transition.provider is not None
                or transition.model_version is not None
                or transition.parameter_hash != transition_checksum
                or transition.job_id is not None
                or transition.job_item_id is not None
                or transition_revision is None
                or transition.decision != "retry-from-immutable-source"
                or transition.reason != "superseded-by-final-review-retry"
                or transition.git_commit is not None
                or not _json_values_equal(transition.evidence, expected_evidence)
                or _public_actor(transition) != _public_actor(chain[index + 1][2])
                or transition.started_at is None
                or transition.finished_at != transition.started_at
                or transition_revision.project_id != source_project_id
                or transition_revision.entity_type != "page-generation"
                or transition_revision.entity_id != candidate_generation.id
                or transition_revision.operation != "supersede-final-review-repair"
                or not _json_values_equal(
                    transition_revision.before,
                    {"state": "active", "closedAt": None},
                )
                or not _json_values_equal(
                    transition_revision.after,
                    _final_review_repair_supersession_revision_after(
                        successor_generation_id=successor.id,
                        closed_at=candidate_generation.closed_at,
                    ),
                )
            ):
                raise ProjectError("Final-review repair retry transition is inconsistent")

    attempt, retry_from_generation_id, g0, generation = chain[-1]
    if parameter_set_id is not None and (
        generation.parameter_set_id != parameter_set_id
        or generation.parameter_set_hash != parameter_set_hash
    ):
        raise ProjectError("Existing repair handoff parameter set does not match this request")

    # Each immutable attempt binds its own parameter identity. A changed recipe
    # may start a successor; it must never rewrite or weaken historical evidence.
    for attempt, retry_from_generation_id, g0, generation in chain:
        expected_run_id = f"final-review-{final_review_item_id[:8]}-r{final_review_item_revision}"
        if attempt > 1:
            expected_run_id = f"{expected_run_id}-a{attempt}"
        if (
            generation.run_id != expected_run_id
            or generation.project_id != source_project_id
            or generation.source_project_id != source_project_id
            or generation.source_image_id != source.id
            or generation.source_checksum != source.checksum
            or generation.source_relative_path != source.relative_path
            or generation.restart_from_source is not True
            or generation.created_at is None
            or generation.next_sequence < 2
            or not _PARAMETER_SET_ID_RE.fullmatch(generation.parameter_set_id)
            or not _SHA256_RE.fullmatch(generation.parameter_set_hash)
        ):
            raise ProjectError("Existing repair handoff provenance is inconsistent")

        events = list(
            session.scalars(
                select(PageLineageEvent)
                .where(PageLineageEvent.generation_id == generation.id)
                .order_by(PageLineageEvent.sequence)
            ).all()
        )
        if [event.sequence for event in events] != list(range(1, generation.next_sequence)):
            raise ProjectError("Existing repair handoff provenance is inconsistent")
        generation_g0_events = [event for event in events if event.gate == "G0_identity"]
        if len(generation_g0_events) != 1 or generation_g0_events[0].id != g0.id:
            raise ProjectError("Existing repair handoff provenance is inconsistent")

        target = session.get(ImageAsset, generation.image_id)
        if target is None or target.id == source.id:
            raise ProjectError("Existing repair handoff provenance is inconsistent")
        expected_relative = _final_review_repair_relative_path(
            source,
            final_review_item_id,
            final_review_item_revision,
            attempt,
        )
        expected_source_path = (Path("source") / expected_relative).as_posix()
        if (
            target.project_id != source_project_id
            or target.name != expected_relative.name
            or target.relative_path != expected_relative.as_posix()
            or target.source_path != expected_source_path
            or target.source_kind != "final-review-repair"
            or target.input_path is not None
            or target.width != source.width
            or target.height != source.height
            or target.media_type != source.media_type
            or target.checksum != source.checksum
        ):
            raise ProjectError("Existing repair handoff provenance is inconsistent")
        target_path = _immutable_image_path(store, target)
        expected_target_path = resolve_write_target(store.source_root, expected_relative)
        try:
            physically_distinct = target_path != source_path and not target_path.samefile(
                source_path
            )
        except OSError as error:
            raise ProjectError("Existing repair handoff target could not be inspected") from error
        if (
            target_path != expected_target_path
            or not physically_distinct
            or _sha256_file(target_path) != target.checksum
            or _decoded_image_resolution(target_path) != (target.width, target.height)
        ):
            raise ProjectError("Existing repair handoff provenance is inconsistent")

        try:
            actor_matches = _public_actor(generation) == _public_actor(g0)
        except ProjectError as error:
            raise ProjectError("Existing repair handoff provenance is inconsistent") from error
        expected_evidence = _final_review_repair_g0_evidence(
            generation=generation,
            target=target,
            source=source,
            final_review_item_id=final_review_item_id,
            final_review_item_revision=final_review_item_revision,
            feedback_checksum=feedback_checksum,
            repair_attempt=attempt,
            retry_from_generation_id=retry_from_generation_id,
        )
        if (
            g0.sequence != 1
            or g0.operation != "generation-created"
            or g0.state != "accepted"
            or g0.input_checksum != source.checksum
            or g0.output_checksum != source.checksum
            or g0.parent_checksum is not None
            or g0.stage is not None
            or g0.provider is not None
            or g0.model_version is not None
            or g0.parameter_hash != generation.parameter_set_hash
            or g0.job_id is not None
            or g0.job_item_id is not None
            or g0.revision_id is None
            or g0.decision != "restart-from-immutable-source"
            or g0.reason != "final-review-issue-repair"
            or g0.git_commit is not None
            or not _json_values_equal(g0.evidence, expected_evidence)
            or g0.started_at is None
            or g0.finished_at is None
            or g0.started_at != g0.finished_at
            or g0.created_at is None
            or not actor_matches
        ):
            raise ProjectError("Existing repair handoff provenance is inconsistent")

        creation_revisions = list(
            session.scalars(
                select(Revision).where(
                    Revision.project_id == source_project_id,
                    Revision.entity_type == "page-generation",
                    Revision.entity_id == generation.id,
                    Revision.operation == "create-final-review-repair",
                )
            ).all()
        )
        expected_revision_after = _final_review_repair_revision_after(
            generation=generation,
            target=target,
            source=source,
            final_review_item_id=final_review_item_id,
            final_review_item_revision=final_review_item_revision,
            feedback_checksum=feedback_checksum,
            repair_attempt=attempt,
            retry_from_generation_id=retry_from_generation_id,
        )
        if (
            len(creation_revisions) != 1
            or creation_revisions[0].id != g0.revision_id
            or creation_revisions[0].before is not None
            or not _json_values_equal(creation_revisions[0].after, expected_revision_after)
            or creation_revisions[0].project_revision < 1
            or creation_revisions[0].created_at is None
        ):
            raise ProjectError("Existing repair handoff provenance is inconsistent")
    return target, generation


def create_final_review_repair_generation(
    store: ProjectStore,
    source_image_id: str,
    *,
    final_review_item_id: str,
    final_review_item_revision: int,
    feedback_checksum: str,
    run_id: str,
    page_generation_id: str,
    parameter_set_id: str,
    parameter_set_hash: str,
    actor: dict[str, Any],
    repair_attempt: int = 1,
    retry_from_generation_id: str | None = None,
) -> tuple[ImageAsset, PageGeneration]:
    """Create a fresh, isolated repair target and its G0 in one project transaction.

    The reviewed source page remains untouched.  A retry supersedes only the prior
    isolated repair generation, while every prior event, revision, and file remains
    append-only.  Each target is copied again from the immutable original source.
    """
    normalized_actor = _safe_actor(actor)
    _final_review_repair_attempt_fields(repair_attempt, retry_from_generation_id)
    if (
        not _PARAMETER_SET_ID_RE.fullmatch(parameter_set_id)
        or not _SHA256_RE.fullmatch(parameter_set_hash)
        or not _SHA256_RE.fullmatch(feedback_checksum)
    ):
        raise PageLineageConflict(
            "Final-review repair checksum metadata is invalid",
            resource=f"final-review-item:{final_review_item_id}",
            reason="invalid-checksum",
        )
    with store.lock, store.session() as session:
        _require_g0_identity_guards(session)
        project = store.project(session)
        source = session.get(ImageAsset, source_image_id)
        if source is None:
            raise ProjectError("Final-review repair source image was not found")
        source_path = _immutable_image_path(store, source)
        actual_checksum = _sha256_file(source_path)
        if actual_checksum != source.checksum:
            raise PageLineageConflict(
                "Immutable repair source changed",
                resource=f"image:{source.id}",
                reason="source-checksum-mismatch",
            )
        predecessor: PageGeneration | None = None
        if retry_from_generation_id is not None:
            existing = find_final_review_repair_generation(
                store,
                session,
                source_project_id=project.id,
                source_image_id=source.id,
                source_relative_path=source.relative_path,
                final_review_item_id=final_review_item_id,
                final_review_item_revision=final_review_item_revision,
                feedback_checksum=feedback_checksum,
            )
            if existing is None or existing[1].id != retry_from_generation_id:
                raise PageLineageConflict(
                    "The requested repair generation is no longer the retry chain head",
                    resource=f"final-review-item:{final_review_item_id}",
                    reason="repair-retry-stale",
                )
            predecessor = existing[1]
            predecessor_attempt, _retry_parent = final_review_repair_attempt_context(
                session, predecessor
            )
            if repair_attempt != predecessor_attempt + 1:
                raise PageLineageConflict(
                    "The final-review repair attempt is not the next retry",
                    resource=f"page-generation:{predecessor.id}",
                    reason="repair-retry-sequence-invalid",
                )
            for active_job in session.scalars(
                select(Job).where(Job.status.in_(("queued", "running", "paused")))
            ):
                context = active_job.lineage_context
                pages = context.get("pages") if isinstance(context, dict) else None
                if isinstance(pages, list) and any(
                    isinstance(page, dict) and page.get("pageGenerationId") == predecessor.id
                    for page in pages
                ):
                    raise PageLineageConflict(
                        "A repair generation with an active job cannot be retried",
                        resource=f"page-generation:{predecessor.id}",
                        reason="repair-retry-job-active",
                    )
        relative = _final_review_repair_relative_path(
            source,
            final_review_item_id,
            final_review_item_revision,
            repair_attempt,
        )
        target = resolve_write_target(store.source_root, relative)
        if target.exists():
            raise PageLineageConflict(
                "Final-review repair target already exists without a committed handoff",
                resource=f"final-review-item:{final_review_item_id}",
                reason="repair-target-conflict",
            )
        atomic_write_bytes(target, source_path.read_bytes())
        try:
            now = datetime.now(UTC)
            if predecessor is not None:
                predecessor_events = list(
                    session.scalars(
                        select(PageLineageEvent)
                        .where(PageLineageEvent.generation_id == predecessor.id)
                        .order_by(PageLineageEvent.sequence)
                    ).all()
                )
                if (
                    not predecessor_events
                    or predecessor_events[-1].sequence != predecessor.next_sequence - 1
                ):
                    raise ProjectError("Final-review repair predecessor sequence is invalid")
                transition_evidence = _final_review_repair_supersession_evidence(
                    predecessor,
                    successor_generation_id=page_generation_id,
                    successor_attempt=repair_attempt,
                    prior_event=predecessor_events[-1],
                    transition_sequence=predecessor.next_sequence,
                )
                transition_checksum = _digest(transition_evidence)
                transition_revision = add_revision(
                    session,
                    project,
                    entity_type="page-generation",
                    entity_id=predecessor.id,
                    operation="supersede-final-review-repair",
                    before={"state": "active", "closedAt": None},
                    after=_final_review_repair_supersession_revision_after(
                        successor_generation_id=page_generation_id,
                        closed_at=now,
                    ),
                )
                session.flush()
                _append_event(
                    session,
                    predecessor,
                    operation="generation-superseded",
                    gate="G0_retry",
                    state="accepted",
                    actor=normalized_actor,
                    input_checksum=transition_evidence["predecessorStateChecksum"],
                    output_checksum=transition_checksum,
                    parent_checksum=transition_evidence["predecessorStateChecksum"],
                    parameter_hash=transition_checksum,
                    revision_id=transition_revision.id,
                    decision="retry-from-immutable-source",
                    reason="superseded-by-final-review-retry",
                    evidence=transition_evidence,
                    started_at=now,
                    finished_at=now,
                    expected_sequence=predecessor.next_sequence,
                )
                predecessor.state = "superseded"
                predecessor.closed_at = now
            image = ImageAsset(
                project_id=project.id,
                name=relative.name,
                relative_path=relative.as_posix(),
                source_path=(Path("source") / relative).as_posix(),
                source_kind="final-review-repair",
                input_path=None,
                width=source.width,
                height=source.height,
                media_type=source.media_type,
                checksum=source.checksum,
                status={
                    "preprocess": "pending",
                    "detection": "pending",
                    "ocr": "pending",
                    "translation": "pending",
                    "inpaint": "pending",
                    "typeset": "pending",
                    "export": "pending",
                    "reviewState": "pending",
                    "reviewedAt": "",
                    "preprocessingProvider": "",
                    "detectorProvider": "",
                    "ocrProvider": "",
                    "translatorProvider": "",
                    "inpaintingProvider": "",
                    "typesettingProvider": "",
                },
            )
            session.add(image)
            session.flush()
            generation = PageGeneration(
                id=page_generation_id,
                run_id=run_id,
                project_id=project.id,
                image_id=image.id,
                restart_from_source=True,
                parameter_set_id=parameter_set_id,
                parameter_set_hash=parameter_set_hash,
                source_project_id=project.id,
                source_image_id=source.id,
                source_checksum=source.checksum,
                source_relative_path=source.relative_path,
                state="active",
                next_sequence=1,
                **_actor_columns(normalized_actor),
            )
            session.add(generation)
            revision = add_revision(
                session,
                project,
                entity_type="page-generation",
                entity_id=generation.id,
                operation="create-final-review-repair",
                before=None,
                after=_final_review_repair_revision_after(
                    generation=generation,
                    target=image,
                    source=source,
                    final_review_item_id=final_review_item_id,
                    final_review_item_revision=final_review_item_revision,
                    feedback_checksum=feedback_checksum,
                    repair_attempt=repair_attempt,
                    retry_from_generation_id=retry_from_generation_id,
                ),
            )
            session.flush()
            _append_event(
                session,
                generation,
                operation="generation-created",
                gate="G0_identity",
                state="accepted",
                actor=normalized_actor,
                input_checksum=source.checksum,
                output_checksum=actual_checksum,
                parameter_hash=parameter_set_hash,
                revision_id=revision.id,
                decision="restart-from-immutable-source",
                reason="final-review-issue-repair",
                evidence=_final_review_repair_g0_evidence(
                    generation=generation,
                    target=image,
                    source=source,
                    final_review_item_id=final_review_item_id,
                    final_review_item_revision=final_review_item_revision,
                    feedback_checksum=feedback_checksum,
                    repair_attempt=repair_attempt,
                    retry_from_generation_id=retry_from_generation_id,
                ),
                started_at=now,
                finished_at=now,
            )
            session.flush()
            image_id = image.id
        except Exception:
            target.unlink(missing_ok=True)
            raise
    store.write_snapshot()
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        generation = session.get(PageGeneration, page_generation_id)
        if image is None or generation is None:
            raise ProjectError("Final-review repair generation could not be reloaded")
        return image, generation


def list_page_generations(store: ProjectStore, image_id: str) -> list[PageGeneration]:
    with store.session() as session:
        if session.get(ImageAsset, image_id) is None:
            raise ProjectError("Page generation image was not found")
        return list(
            session.scalars(
                select(PageGeneration)
                .where(PageGeneration.image_id == image_id)
                .order_by(PageGeneration.created_at, PageGeneration.id)
            ).all()
        )


def list_page_lineage_events(store: ProjectStore, generation_id: str) -> list[PageLineageEvent]:
    with store.session() as session:
        if session.get(PageGeneration, generation_id) is None:
            raise ProjectError("Page generation was not found")
        return list(
            session.scalars(
                select(PageLineageEvent)
                .where(PageLineageEvent.generation_id == generation_id)
                .order_by(PageLineageEvent.sequence)
            ).all()
        )


def public_page_generation(generation: PageGeneration) -> dict[str, Any]:
    return {
        "id": generation.id,
        "runId": generation.run_id,
        "projectId": generation.project_id,
        "imageId": generation.image_id,
        "restartFromSource": generation.restart_from_source,
        "parameterSetId": generation.parameter_set_id,
        "parameterSetHash": generation.parameter_set_hash,
        "sourceProjectId": generation.source_project_id,
        "sourceImageId": generation.source_image_id,
        "sourceChecksum": generation.source_checksum,
        "state": generation.state,
        "nextSequence": generation.next_sequence,
        "actor": _public_actor(generation),
        "createdAt": generation.created_at,
        "closedAt": generation.closed_at,
    }


_OMIT_PUBLIC_EVIDENCE = object()
_PUBLIC_MASK_CHECKS = {
    "coverageChecks": {
        "body-glyphs-covered",
        "punctuation-covered",
        "strokes-and-shadows-covered",
        "ruby-covered",
        "antialias-edges-covered",
    },
    "collateralChecks": {
        "bubble-borders-protected",
        "characters-protected",
        "speed-lines-protected",
        "screentone-protected",
        "nearby-art-protected",
    },
}
_PUBLIC_CLEAN_PLATE_CHECKS = {
    "outside-mask-unchanged",
    "source-text-unreadable",
    "no-white-or-gray-hole",
    "no-blur-band",
    "no-repeated-texture",
    "background-continuous",
    "structure-preserved",
}
_PUBLIC_CLEAN_PLATE_CATEGORIES = {
    "white-solid",
    "black-solid",
    "other-solid",
    "simple-gradient",
    "screentone",
    "complex-lineart",
    "illustration/character",
}
_PUBLIC_CLEAN_PLATE_ROUTES = {
    "deterministic-solid",
    "controlled-gradient",
    "screentone-preserving",
    "ai-inpaint-redraw",
    "classical-fallback",
    "layered-structure",
}
_PUBLIC_CLEAN_PLATE_ORIGINS = {"deterministic", "ai", "classical", "mixed"}
_PUBLIC_TYPESET_CHECKS = {
    "original-clean-final-compared",
    "translation-complete",
    "hierarchy-reading-order-preserved",
    "key-art-unobstructed",
    "typography-source-matched",
    "bubble-contained",
    "art-lettering-composition-matched",
    "overflow-free",
}


def _public_evidence_value(key: str, value: Any) -> Any:
    if isinstance(value, str) or type(value) in {bool, int} or value is None:
        return value
    if key == "renderScale" and isinstance(value, float) and math.isfinite(value):
        return value
    if (
        key == "bbox"
        and isinstance(value, dict)
        and set(value)
        == {
            "x",
            "y",
            "width",
            "height",
        }
    ):
        if (
            all(type(value[name]) is int for name in value)
            and value["x"] >= 0
            and value["y"] >= 0
            and value["width"] > 0
            and value["height"] > 0
        ):
            return dict(value)
    if key == "rubyRegionIdsByPrimary" and isinstance(value, dict) and len(value) <= 4096:
        if all(
            isinstance(parent, str)
            and _OPAQUE_ID_RE.fullmatch(parent)
            and isinstance(children, list)
            and len(children) <= 4096
            and children == sorted(set(children))
            and all(isinstance(child, str) and _OPAQUE_ID_RE.fullmatch(child) for child in children)
            for parent, children in value.items()
        ):
            return {parent: list(children) for parent, children in sorted(value.items())}
    if key in {"coverageChecks", "collateralChecks"} and isinstance(value, list):
        expected = _PUBLIC_MASK_CHECKS[key]
        if (
            len(value) == 5
            and all(
                isinstance(entry, dict)
                and set(entry) == {"check", "passed"}
                and entry["check"] in expected
                and type(entry["passed"]) is bool
                for entry in value
            )
            and {entry["check"] for entry in value} == expected
        ):
            return [{"check": entry["check"], "passed": entry["passed"]} for entry in value]
    if key == "routeManifest" and isinstance(value, list) and len(value) <= 4096:
        base_keys = {
            "regionId",
            "backgroundCategory",
            "route",
            "originKind",
            "provider",
            "modelVersion",
            "parameterHash",
        }

        def valid_lineage_input(item: object) -> bool:
            if not isinstance(item, dict) or set(item) != {
                "referenceId",
                "referenceImageId",
                "referenceCandidateId",
                "snapshotId",
                "artifactChecksum",
                "sourceManifestDigest",
                "legacyManifestDigest",
                "sourceChecksum",
                "maskChecksum",
                "width",
                "height",
                "ancestry",
            }:
                return False
            ancestry = item.get("ancestry")
            if not isinstance(ancestry, dict) or set(ancestry) not in (
                {"referenceGenerationId", "originKind", "providerIds"},
                {"referenceGenerationId", "originKind", "providerIds", "lineage"},
            ):
                return False
            providers = ancestry.get("providerIds")
            lineage = ancestry.get("lineage")
            lineage_valid = lineage is None or (
                isinstance(lineage, dict)
                and set(lineage)
                == {"version", "transformId", "transformVersion", "baseId", "baseChecksum"}
                and lineage.get("version") == 1
                and type(lineage.get("transformVersion")) is int
                and isinstance(lineage.get("transformId"), str)
                and _OPAQUE_ID_RE.fullmatch(lineage["transformId"])
                and isinstance(lineage.get("baseId"), str)
                and _OPAQUE_ID_RE.fullmatch(lineage["baseId"])
                and isinstance(lineage.get("baseChecksum"), str)
                and _SHA256_RE.fullmatch(lineage["baseChecksum"])
            )
            return bool(
                all(
                    isinstance(item.get(identity), str) and _OPAQUE_ID_RE.fullmatch(item[identity])
                    for identity in (
                        "referenceId",
                        "referenceImageId",
                        "referenceCandidateId",
                    )
                )
                and all(
                    isinstance(item.get(checksum), str) and _SHA256_RE.fullmatch(item[checksum])
                    for checksum in (
                        "snapshotId",
                        "artifactChecksum",
                        "sourceManifestDigest",
                        "legacyManifestDigest",
                        "sourceChecksum",
                        "maskChecksum",
                    )
                )
                and type(item.get("width")) is int
                and item["width"] > 0
                and type(item.get("height")) is int
                and item["height"] > 0
                and isinstance(ancestry.get("referenceGenerationId"), str)
                and _OPAQUE_ID_RE.fullmatch(ancestry["referenceGenerationId"])
                and ancestry.get("originKind")
                in {"direct-ai", "ai-derived", "classical", "deterministic-postprocess", "mixed"}
                and isinstance(providers, list)
                and all(
                    isinstance(provider, str) and _OPAQUE_ID_RE.fullmatch(provider)
                    for provider in providers
                )
                and providers == sorted(set(providers))
                and lineage_valid
            )

        def valid_route_entry(entry: object) -> bool:
            if not isinstance(entry, dict):
                return False
            layered = entry.get("route") == "layered-structure"
            expected_keys = base_keys | ({"lineageInputs"} if layered else set())
            lineage_inputs = entry.get("lineageInputs")
            return bool(
                set(entry) == expected_keys
                and isinstance(entry.get("regionId"), str)
                and _OPAQUE_ID_RE.fullmatch(entry["regionId"])
                and entry.get("backgroundCategory") in _PUBLIC_CLEAN_PLATE_CATEGORIES
                and entry.get("route") in _PUBLIC_CLEAN_PLATE_ROUTES
                and entry.get("originKind") in _PUBLIC_CLEAN_PLATE_ORIGINS
                and (layered or entry["originKind"] in {"deterministic", "ai", "classical"})
                and isinstance(entry.get("provider"), str)
                and _OPAQUE_ID_RE.fullmatch(entry["provider"])
                and isinstance(entry.get("modelVersion"), str)
                and _OPAQUE_ID_RE.fullmatch(entry["modelVersion"])
                and isinstance(entry.get("parameterHash"), str)
                and _SHA256_RE.fullmatch(entry["parameterHash"])
                and (
                    not layered
                    or (
                        entry["originKind"] in {"classical", "mixed"}
                        and entry["provider"] == "opencv"
                        and entry["modelVersion"] == "layered-structure-guide-v1"
                        and isinstance(lineage_inputs, list)
                        and 1 <= len(lineage_inputs) <= 16
                        and all(valid_lineage_input(item) for item in lineage_inputs)
                    )
                )
            )

        if all(valid_route_entry(entry) for entry in value):
            return [dict(entry) for entry in value]
    if key in {"providerIds", "modelVersions"} and isinstance(value, list):
        if (
            len(value) <= 4096
            and value == sorted(set(value))
            and all(isinstance(entry, str) and _OPAQUE_ID_RE.fullmatch(entry) for entry in value)
        ):
            return list(value)
    if key == "revisionIds" and isinstance(value, list):
        if (
            len(value) <= 4096
            and len(set(value)) == len(value)
            and all(isinstance(entry, str) and _OPAQUE_ID_RE.fullmatch(entry) for entry in value)
        ):
            return list(value)
    if key == "checks" and isinstance(value, list):
        observed = {
            entry.get("check")
            for entry in value
            if isinstance(entry, dict) and set(entry) == {"check", "passed"}
        }
        allowed = (_PUBLIC_CLEAN_PLATE_CHECKS, _PUBLIC_TYPESET_CHECKS) if value else (set(),)
        if all(
            isinstance(entry, dict)
            and set(entry) == {"check", "passed"}
            and isinstance(entry["check"], str)
            and type(entry["passed"]) is bool
            for entry in value
        ) and any(observed == expected and len(value) == len(expected) for expected in allowed):
            return [{"check": entry["check"], "passed": entry["passed"]} for entry in value]
    if key in {"overflowRegionIds", "anomalies"} and isinstance(value, list):
        if (
            len(value) <= 4096
            and len(set(value)) == len(value)
            and all(
                isinstance(entry, str) and 0 < len(entry) <= 128 and "\x00" not in entry
                for entry in value
            )
        ):
            return list(value)
    return _OMIT_PUBLIC_EVIDENCE


def public_page_lineage_event(event: PageLineageEvent) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, value in (event.evidence or {}).items():
        if key not in _PUBLIC_EVIDENCE_KEYS:
            continue
        public_value = _public_evidence_value(key, value)
        if public_value is not _OMIT_PUBLIC_EVIDENCE:
            evidence[key] = public_value
    return {
        "id": event.id,
        "generationId": event.generation_id,
        "sequence": event.sequence,
        "operation": event.operation,
        "gate": event.gate,
        "state": event.state,
        "actor": _public_actor(event),
        "inputChecksum": event.input_checksum,
        "outputChecksum": event.output_checksum,
        "parentChecksum": event.parent_checksum,
        "stage": event.stage,
        "provider": event.provider,
        "modelVersion": event.model_version,
        "parameterHash": event.parameter_hash,
        "jobId": event.job_id,
        "jobItemId": event.job_item_id,
        "revisionId": event.revision_id,
        "decision": event.decision,
        "reason": event.reason,
        "gitCommit": event.git_commit,
        "evidence": evidence,
        "startedAt": event.started_at,
        "finishedAt": event.finished_at,
        "createdAt": event.created_at,
    }


def _lineage_page_map(lineage: dict[str, Any]) -> dict[str, str]:
    pages = lineage.get("pages")
    if not isinstance(pages, list) or not pages:
        raise PageLineageConflict(
            "Job lineage page bindings are missing",
            resource="job-lineage",
            reason="invalid-page-bindings",
        )
    result: dict[str, str] = {}
    generation_ids: set[str] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise PageLineageConflict(
                "Job lineage page binding is invalid",
                resource="job-lineage",
                reason="invalid-page-bindings",
            )
        image_id = page.get("imageId")
        generation_id = page.get("pageGenerationId")
        if (
            not isinstance(image_id, str)
            or not isinstance(generation_id, str)
            or image_id in result
            or generation_id in generation_ids
        ):
            raise PageLineageConflict(
                "Job lineage page bindings must be unique",
                resource="job-lineage",
                reason="invalid-page-bindings",
            )
        result[image_id] = generation_id
        generation_ids.add(generation_id)
    return result


def _lineage_page_sequence_map(lineage: dict[str, Any]) -> dict[str, int]:
    pages = lineage.get("pages")
    if not isinstance(pages, list) or not pages:
        raise PageLineageConflict(
            "Job lineage page bindings are missing",
            resource="job-lineage",
            reason="invalid-page-bindings",
        )
    result: dict[str, int] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise PageLineageConflict(
                "Job lineage page binding is invalid",
                resource="job-lineage",
                reason="invalid-page-bindings",
            )
        image_id = page.get("imageId")
        expected_sequence = page.get("expectedSequence")
        if (
            not isinstance(image_id, str)
            or image_id in result
            or not isinstance(expected_sequence, int)
            or isinstance(expected_sequence, bool)
            or expected_sequence < 1
        ):
            raise PageLineageConflict(
                "Job lineage page sequence bindings are invalid",
                resource="job-lineage",
                reason="invalid-sequence",
            )
        result[image_id] = expected_sequence
    return result


def _validate_job_context(
    store: ProjectStore,
    session: Session,
    *,
    project_id: str,
    target_image_ids: set[str],
    lineage: dict[str, Any] | None,
    enforce_sequence: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, PageGeneration]]:
    generations = list(
        session.scalars(
            select(PageGeneration).where(
                PageGeneration.project_id == project_id,
                PageGeneration.image_id.in_(target_image_ids),
            )
        ).all()
    )
    active = {
        generation.image_id: generation
        for generation in generations
        if generation.state == "active"
    }
    historical_only = {generation.image_id for generation in generations} - set(active)
    if lineage is None:
        if active:
            raise PageLineageConflict(
                "Active page generations require an explicit job lineage context",
                resource="job-lineage",
                reason="lineage-required",
            )
        if historical_only:
            raise PageLineageConflict(
                "Pages with historical lineage require a new active generation",
                resource="job-lineage",
                reason="page-generation-not-active",
            )
        return None, {}
    if not active:
        raise PageLineageConflict(
            "A lineage-bound job requires active page generations",
            resource="job-lineage",
            reason="active-generation-missing",
        )
    if set(active) != target_image_ids:
        raise PageLineageConflict(
            "Legacy and active-generation pages cannot be mixed in one job",
            resource="job-lineage",
            reason="mixed-generation-targets",
        )

    run_id = lineage.get("runId")
    if not isinstance(run_id, str) or not _OPAQUE_ID_RE.fullmatch(run_id):
        raise PageLineageConflict(
            "Job lineage run id is invalid",
            resource="job-lineage",
            reason="run-mismatch",
        )
    actor = _safe_actor(lineage.get("actor") if isinstance(lineage.get("actor"), dict) else {})
    page_map = _lineage_page_map(lineage)
    page_sequence_map = _lineage_page_sequence_map(lineage) if enforce_sequence else {}
    if set(page_map) != target_image_ids:
        raise PageLineageConflict(
            "Job lineage bindings do not exactly match the job targets",
            resource="job-lineage",
            reason="target-mismatch",
        )
    if enforce_sequence and set(page_sequence_map) != target_image_ids:
        raise PageLineageConflict(
            "Job lineage sequence bindings do not exactly match the job targets",
            resource="job-lineage",
            reason="target-mismatch",
        )
    for image_id, generation in active.items():
        if generation.run_id != run_id or generation.id != page_map[image_id]:
            raise PageLineageConflict(
                "Job lineage does not match the active page generation",
                resource=f"image:{image_id}",
                reason="generation-mismatch",
            )
        if enforce_sequence and generation.next_sequence != page_sequence_map[image_id]:
            raise PageLineageConflict(
                "Page lineage changed after the job was prepared",
                resource=f"page-generation:{generation.id}",
                reason="sequence-conflict",
                expected_sequence=page_sequence_map[image_id],
                actual_sequence=generation.next_sequence,
            )
        image = session.get(ImageAsset, image_id)
        if image is None or image.checksum != generation.source_checksum:
            raise PageLineageConflict(
                "Page source identity changed after generation creation",
                resource=f"image:{image_id}",
                reason="source-identity-changed",
            )
        if _sha256_file(_immutable_image_path(store, image)) != generation.source_checksum:
            raise PageLineageConflict(
                "Immutable page source changed after generation creation",
                resource=f"image:{image_id}",
                reason="source-checksum-changed",
            )
    normalized = {
        "version": 1,
        "runId": run_id,
        "actor": actor,
        "pages": [
            {
                "imageId": image_id,
                "pageGenerationId": page_map[image_id],
                **({"expectedSequence": page_sequence_map[image_id]} if enforce_sequence else {}),
            }
            for image_id in sorted(page_map)
        ],
    }
    return normalized, active


def normalize_job_lineage(
    store: ProjectStore,
    session: Session,
    *,
    project_id: str,
    target_image_ids: set[str],
    lineage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, PageGeneration]]:
    return _validate_job_context(
        store,
        session,
        project_id=project_id,
        target_image_ids=target_image_ids,
        lineage=lineage,
        enforce_sequence=True,
    )


def require_image_mutation_lineage(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    lineage: dict[str, Any] | None,
) -> tuple[PageGeneration, dict[str, str | None], int] | None:
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.project_id == image.project_id,
            PageGeneration.image_id == image.id,
            PageGeneration.state == "active",
        )
    )
    if generation is None:
        historical_generation = session.scalar(
            select(PageGeneration.id)
            .where(
                PageGeneration.project_id == image.project_id,
                PageGeneration.image_id == image.id,
            )
            .limit(1)
        )
        if historical_generation is not None:
            raise PageLineageConflict(
                "Page lineage exists but has no active generation",
                resource=f"image:{image.id}",
                reason="page-generation-not-active",
            )
        if lineage is not None:
            raise PageLineageConflict(
                "Mutation lineage was supplied for a page without an active generation",
                resource=f"image:{image.id}",
                reason="active-generation-missing",
            )
        return None
    if lineage is None:
        raise PageLineageConflict(
            "Active page generation mutations require explicit lineage evidence",
            resource=f"image:{image.id}",
            reason="lineage-required",
        )
    run_id = lineage.get("runId")
    generation_id = lineage.get("pageGenerationId")
    expected_sequence = lineage.get("expectedSequence")
    if run_id != generation.run_id or generation_id != generation.id:
        raise PageLineageConflict(
            "Mutation lineage does not match the active page generation",
            resource=f"image:{image.id}",
            reason="generation-mismatch",
        )
    if not isinstance(expected_sequence, int) or isinstance(expected_sequence, bool):
        raise PageLineageConflict(
            "Mutation lineage expected sequence is invalid",
            resource=f"page-generation:{generation.id}",
            reason="invalid-sequence",
        )
    if generation.next_sequence != expected_sequence:
        raise PageLineageConflict(
            "Page lineage changed after the mutation was prepared",
            resource=f"page-generation:{generation.id}",
            reason="sequence-conflict",
            expected_sequence=expected_sequence,
            actual_sequence=generation.next_sequence,
        )
    actor = _safe_actor(lineage.get("actor") if isinstance(lineage.get("actor"), dict) else {})
    if image.checksum != generation.source_checksum:
        raise PageLineageConflict(
            "Page source identity changed after generation creation",
            resource=f"image:{image.id}",
            reason="source-identity-changed",
        )
    if _sha256_file(_immutable_image_path(store, image)) != generation.source_checksum:
        raise PageLineageConflict(
            "Immutable page source changed after generation creation",
            resource=f"image:{image.id}",
            reason="source-checksum-changed",
        )
    return generation, actor, expected_sequence


def _latest_gate_event(
    session: Session,
    generation_id: str,
    gate: str,
) -> PageLineageEvent | None:
    return session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation_id,
            PageLineageEvent.gate == gate,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )


def _generated_page_artifact_path(
    store: ProjectStore,
    image: ImageAsset,
    target_kind: str,
) -> Path:
    directory = {
        "preprocessed": "preprocessed",
        "reconstruction": "quality",
    }.get(target_kind)
    if directory is None:
        raise PageLineageConflict(
            "Accepted quality-plate evidence has an unsupported target kind",
            resource=f"image:{image.id}",
            reason="quality-target-unsupported",
        )
    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    return resolve_write_target(
        store.root,
        Path("generated") / directory / relative,
        protected_roots=(store.source_root,),
    )


def _generated_checksum(path: Path, *, image_id: str) -> str:
    if not path.is_file():
        raise PageLineageConflict(
            "The checksum-bound quality artifact is missing",
            resource=f"image:{image_id}",
            reason="quality-artifact-missing",
        )
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as error:
        raise PageLineageConflict(
            "The checksum-bound quality artifact could not be read",
            resource=f"image:{image_id}",
            reason="quality-artifact-unreadable",
        ) from error


def _current_accepted_preprocess(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, PageLineageEvent]:
    status = image.status or {}
    reviews = status.get("stageReviews")
    review = reviews.get("preprocess") if isinstance(reviews, dict) else None
    latest = _latest_gate_event(session, generation.id, "G1_baselineUpscale")
    if (
        status.get("preprocess") != "done"
        or not isinstance(review, dict)
        or review.get("state") != "accepted"
        or latest is None
        or latest.operation != "preprocess-stage-review"
        or latest.state != "accepted"
    ):
        raise PageLineageConflict(
            "G1 baseline preprocessing is not currently accepted",
            resource=f"page-generation:{generation.id}",
            reason="g1-not-accepted",
        )
    artifact = _generated_page_artifact_path(store, image, "preprocessed")
    checksum = _generated_checksum(artifact, image_id=image.id)
    if (
        review.get("artifactChecksum") != checksum
        or latest.output_checksum != checksum
        or latest.parent_checksum != generation.source_checksum
    ):
        raise PageLineageConflict(
            "G1 accepted evidence does not match the current baseline artifact",
            resource=f"page-generation:{generation.id}",
            reason="g1-checksum-mismatch",
        )
    produced = _require_completed_production_event(
        session,
        generation=generation,
        stage="preprocess",
        output_checksum=checksum,
    )
    if latest.sequence <= produced.sequence:
        raise PageLineageConflict(
            "G1 acceptance does not follow the current completed production event",
            resource=f"page-generation:{generation.id}",
            reason="g1-evidence-out-of-order",
        )
    return checksum, latest


def require_current_quality_plate(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
) -> QualityPlateBinding:
    baseline_checksum, baseline_event = _current_accepted_preprocess(
        store,
        session,
        image,
        generation,
    )
    event = _latest_gate_event(session, generation.id, "G2_reconstruction")
    if event is None or event.state != "accepted":
        raise PageLineageConflict(
            "G2 reconstruction decision is not currently accepted",
            resource=f"page-generation:{generation.id}",
            reason="g2-not-accepted",
        )
    target_kind = (event.evidence or {}).get("targetKind")
    if not isinstance(target_kind, str):
        raise PageLineageConflict(
            "G2 accepted evidence has no quality-plate target",
            resource=f"page-generation:{generation.id}",
            reason="quality-target-missing",
        )
    if (
        event.sequence <= baseline_event.sequence
        or event.input_checksum != baseline_checksum
        or event.parent_checksum != baseline_checksum
        or not isinstance(event.output_checksum, str)
        or not _SHA256_RE.fullmatch(event.output_checksum)
    ):
        raise PageLineageConflict(
            "G2 accepted evidence is stale or does not descend from G1",
            resource=f"page-generation:{generation.id}",
            reason="g2-lineage-mismatch",
        )
    if target_kind == "reconstruction":
        from manga_localizer.services.reconstructions import accepted_quality

        return accepted_quality(store, session, image, generation, event)
    if (
        target_kind != "preprocessed"
        or event.operation != "reconstruction-decision"
        or event.decision != "further-reconstruction-no"
    ):
        raise PageLineageConflict(
            "G2 baseline acceptance requires an explicit no-reconstruction decision",
            resource=f"page-generation:{generation.id}",
            reason="g2-lineage-mismatch",
        )
    artifact = _generated_page_artifact_path(store, image, target_kind)
    actual_checksum = _generated_checksum(artifact, image_id=image.id)
    if actual_checksum != event.output_checksum or (
        target_kind == "preprocessed" and actual_checksum != baseline_checksum
    ):
        raise PageLineageConflict(
            "The current quality plate does not match accepted G2 evidence",
            resource=f"page-generation:{generation.id}",
            reason="g2-checksum-mismatch",
        )
    return {
        "path": artifact,
        "checksum": actual_checksum,
        "targetKind": target_kind,
        "eventSequence": event.sequence,
    }


def record_reconstruction_decision(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    observed_quality_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    if decision not in _RECONSTRUCTION_REASONS or reason not in _RECONSTRUCTION_REASONS[decision]:
        raise ProjectError("Reconstruction decision and reason are inconsistent")
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before the reconstruction decision",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            binding = require_image_mutation_lineage(store, session, image, lineage)
            if binding is None:
                raise PageLineageConflict(
                    "G2 decisions require an active page generation",
                    resource=f"image:{image.id}",
                    reason="active-generation-missing",
                )
            generation, actor, expected_sequence = binding
            baseline_checksum, _baseline_event = _current_accepted_preprocess(
                store,
                session,
                image,
                generation,
            )
            if observed_quality_checksum != baseline_checksum:
                raise PageLineageConflict(
                    "The observed baseline no longer matches the accepted G1 artifact",
                    resource=f"page-generation:{generation.id}",
                    reason="observed-quality-checksum-mismatch",
                )

            project = store.project(session)
            status = dict(image.status or {})
            before = {
                "gate": "G2_reconstruction",
                "latestEventSequence": (
                    latest.sequence
                    if (latest := _latest_gate_event(session, generation.id, "G2_reconstruction"))
                    else None
                ),
                "reviewState": status.get("reviewState", "pending"),
                "reviewedAt": status.get("reviewedAt") or "",
            }
            # Every new G2 decision supersedes any older G3 page decision, even
            # when the accepted baseline bytes themselves did not change.
            status["reviewState"] = "pending"
            status["reviewedAt"] = ""
            status["export"] = "pending"
            image.status = status
            image.revision += 1
            now = datetime.now(UTC)
            accepted = decision == "no"
            state = "accepted" if accepted else "blocked"
            revision = add_revision(
                session,
                project,
                entity_type="page-gate",
                entity_id=generation.id,
                operation="reconstruction",
                before=before,
                after={
                    "gate": "G2_reconstruction",
                    "decision": decision,
                    "reason": reason,
                    "state": state,
                    "qualityChecksum": baseline_checksum if accepted else None,
                    "reviewState": "pending",
                },
            )
            session.flush()
            event = _append_event(
                session,
                generation,
                operation="reconstruction-decision",
                gate="G2_reconstruction",
                state=state,
                actor=actor,
                input_checksum=baseline_checksum,
                output_checksum=baseline_checksum if accepted else None,
                parent_checksum=baseline_checksum,
                stage="quality",
                parameter_hash=generation.parameter_set_hash,
                revision_id=revision.id,
                decision=f"further-reconstruction-{decision}",
                reason=reason,
                evidence={
                    "eventType": "reconstruction-decision",
                    "imageRevision": image.revision,
                    "qualityState": state,
                    "reconstructionRequired": decision == "yes",
                    **({"targetKind": "preprocessed"} if accepted else {}),
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def _no_text_residuals(store: ProjectStore, session: Session, image: ImageAsset) -> list[str]:
    residuals: list[str] = []
    if session.scalar(select(TextRegion.id).where(TextRegion.image_id == image.id).limit(1)):
        residuals.append("regions")
    status = image.status or {}
    residuals.extend(
        f"stage:{stage}"
        for stage in sorted(_TEXT_DOWNSTREAM_STAGES)
        if status.get(stage, "pending") != "pending"
    )
    reviews = status.get("stageReviews")
    if isinstance(reviews, dict):
        residuals.extend(f"review:{stage}" for stage in ("inpaint", "typeset") if stage in reviews)
    if any(
        status.get(key)
        for key in (
            "renderInputVariant",
            "renderScale",
            "renderedSize",
            "inpaintCandidate",
            "inpaintCandidates",
            "typesetOverflowCount",
            "typesetOverflowRegionIds",
        )
    ):
        residuals.append("derived-status")
    if image.inpaint_provenance is not None:
        residuals.append("inpaint-provenance")
    if image.inpaint_classical_approval is not None:
        residuals.append("classical-approval")
    if image.inpaint_ai_candidate_reviews is not None:
        residuals.append("candidate-reviews")
    if any(
        isinstance(error, dict) and error.get("stage") in _TEXT_DOWNSTREAM_JOB_KINDS
        for error in (image.processing_errors or [])
    ):
        residuals.append("processing-errors")

    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    for directory in ("masks", "inpainted", "typeset"):
        artifact = resolve_write_target(
            store.root,
            Path("generated") / directory / relative,
            protected_roots=(store.source_root,),
        )
        if artifact.exists():
            residuals.append(f"artifact:{directory}")
    candidate_root = resolve_write_target(
        store.root,
        Path("generated") / "inpaint-candidates" / relative.with_suffix(""),
        protected_roots=(store.source_root,),
    )
    if candidate_root.exists():
        residuals.append("artifact:inpaint-candidates")
    active_job = session.scalar(
        select(Job.id)
        .join(JobItem, JobItem.job_id == Job.id)
        .where(
            JobItem.image_id == image.id,
            Job.kind.in_(_TEXT_DOWNSTREAM_JOB_KINDS),
            Job.status.in_(("queued", "running", "paused")),
        )
        .limit(1)
    )
    if active_job is not None:
        residuals.append("active-job")
    return sorted(set(residuals))


def require_current_no_text_quality_plate(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
) -> QualityPlateBinding | None:
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.project_id == image.project_id,
            PageGeneration.image_id == image.id,
            PageGeneration.state == "active",
        )
    )
    if generation is None:
        return None
    quality = require_current_quality_plate(store, session, image, generation)
    event = _latest_gate_event(session, generation.id, "G3_textPresence")
    if (
        event is None
        or event.state != "accepted"
        or event.decision != "no-text"
        or event.sequence <= quality["eventSequence"]
        or event.input_checksum != quality["checksum"]
        or event.output_checksum != quality["checksum"]
        or event.parent_checksum != quality["checksum"]
        or (image.status or {}).get("reviewState") != "no-text-reviewed"
    ):
        raise PageLineageConflict(
            "G3 no-text evidence is not current for the accepted quality plate",
            resource=f"page-generation:{generation.id}",
            reason="g3-no-text-not-current",
        )
    residuals = _no_text_residuals(store, session, image)
    if residuals:
        raise PageLineageConflict(
            "A no-text page still has downstream text-processing state",
            resource=f"image:{image.id}",
            reason="no-text-residuals:" + ",".join(residuals),
        )
    return quality


def require_current_text_present_quality_plate(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[QualityPlateBinding, PageLineageEvent]:
    """Return the exact G2 plate only when the current G3 decision is text-present."""
    quality = require_current_quality_plate(store, session, image, generation)
    event = _latest_gate_event(session, generation.id, "G3_textPresence")
    if (
        event is None
        or event.operation != "text-presence-decision"
        or event.state != "accepted"
        or event.decision != "text-present"
        or event.sequence <= quality["eventSequence"]
        or event.input_checksum != quality["checksum"]
        or event.output_checksum != quality["checksum"]
        or event.parent_checksum != quality["checksum"]
    ):
        raise PageLineageConflict(
            "G3 text-present evidence is not current for the accepted quality plate",
            resource=f"page-generation:{generation.id}",
            reason="g3-text-present-not-current",
        )
    return quality, event


def _g4_region_payload_checksum(rows: list[dict[str, Any]]) -> str:
    payload = sorted(
        [
            {
                "id": row["id"],
                "geometry": [
                    float(row["x"]),
                    float(row["y"]),
                    float(row["width"]),
                    float(row["height"]),
                    float(row["rotation"]),
                ],
                "type": row["type"],
                "direction": row["direction"],
                "order": int(row["order"]),
                "paragraphGroupId": row.get("paragraphGroupId"),
                "rubyParentId": row.get("rubyParentId"),
                "contentDisposition": row.get("contentDisposition"),
                "detectorJobItemId": row.get("detectorJobItemId"),
                "detectorCandidateIndex": (
                    int(row["detectorCandidateIndex"])
                    if row.get("detectorCandidateIndex") is not None
                    else None
                ),
            }
            for row in rows
        ],
        key=lambda row: (row["order"], row["id"]),
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def g4_region_state_checksum(session: Session, image_id: str) -> str:
    """Hash only G4 semantics so later OCR/translation work cannot stale G4."""
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image_id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    return _g4_region_payload_checksum(
        [
            {
                "id": row.id,
                "x": row.x,
                "y": row.y,
                "width": row.width,
                "height": row.height,
                "rotation": row.rotation,
                "type": row.region_type,
                "direction": row.direction,
                "order": row.reading_order,
                "paragraphGroupId": row.paragraph_group_id,
                "rubyParentId": row.ruby_parent_id,
                "contentDisposition": row.content_disposition,
                "detectorJobItemId": row.detector_job_item_id,
                "detectorCandidateIndex": row.detector_candidate_index,
            }
            for row in rows
        ]
    )


def background_classification_required(region: TextRegion) -> bool:
    """Return whether G5 must classify this independently reviewed G4 region."""
    return region.region_type != "ruby" and region.content_disposition in {
        "translate",
        "redraw-art",
    }


def _g5_state_payload(rows: list[TextRegion], *, blank: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "eligibility": (
                "required" if background_classification_required(row) else "not-applicable"
            ),
            "category": None if blank else row.background_category,
            "confidence": None if blank else row.background_confidence,
            "rationaleCodes": None if blank else row.background_rationale_codes,
            "reviewer": None if blank else row.background_reviewer,
            "generationId": None if blank else row.background_generation_id,
        }
        for row in rows
    ]


def _g5_checksum_for_rows(rows: list[TextRegion], *, blank: bool = False) -> str:
    encoded = json.dumps(
        _g5_state_payload(rows, blank=blank),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def g5_background_state_checksum(session: Session, image_id: str) -> str:
    """Hash G5 eligibility and its complete reviewer-owned evidence bundle."""
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image_id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    return _g5_checksum_for_rows(rows)


def _g5_validation_issues(
    rows: list[TextRegion],
    *,
    generation_id: str,
    require_complete: bool,
) -> tuple[list[str], int, int]:
    issues: set[str] = set()
    eligible_count = 0
    classified_count = 0
    for row in rows:
        bundle = (
            row.background_category,
            row.background_confidence,
            row.background_rationale_codes,
            row.background_reviewer,
            row.background_generation_id,
        )
        if not background_classification_required(row):
            if any(value is not None for value in bundle):
                issues.add("ineligible-region-has-background-classification")
            continue
        eligible_count += 1
        if all(value is None for value in bundle):
            if require_complete:
                issues.add("background-classification-missing")
            continue
        if any(value is None for value in bundle):
            issues.add("background-classification-incomplete")
            continue
        classified_count += 1
        category = row.background_category
        confidence = row.background_confidence
        rationales = row.background_rationale_codes
        reviewer = row.background_reviewer
        if category not in _BACKGROUND_RATIONALE_ANCHORS:
            issues.add("background-category-invalid")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or float(confidence) < 0
            or float(confidence) > 1
        ):
            issues.add("background-confidence-invalid")
        if (
            not isinstance(rationales, list)
            or not rationales
            or any(not isinstance(code, str) for code in rationales)
            or len(set(rationales)) != len(rationales)
            or not set(rationales).issubset(_BACKGROUND_RATIONALE_CODES)
            or (
                category in _BACKGROUND_RATIONALE_ANCHORS
                and _BACKGROUND_RATIONALE_ANCHORS[category] not in rationales
            )
        ):
            issues.add("background-rationale-invalid")
        if not isinstance(reviewer, dict):
            issues.add("background-reviewer-invalid")
        else:
            try:
                if reviewer != _safe_actor(reviewer):
                    issues.add("background-reviewer-invalid")
            except (AttributeError, PageLineageConflict):
                issues.add("background-reviewer-invalid")
        if row.background_generation_id != generation_id:
            issues.add("background-generation-stale")
    return sorted(issues), eligible_count, classified_count


def ocr_source_review_required(region: TextRegion) -> bool:
    """Return whether G4 requires trusted source text before localization.

    ``redraw-art`` is a localization decision, not a preservation decision: its
    source text still has to traverse G6 and G9 before the dedicated G10 art
    lettering route can consume it. Ruby remains attached visual evidence and
    never becomes an independent OCR/translation target.
    """
    return region.region_type != "ruby" and region.content_disposition in {
        "translate",
        "redraw-art",
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_ocr_attempt(attempt: RegionOCRAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "regionId": attempt.region_id,
        "generationId": attempt.generation_id,
        "jobId": attempt.job_id,
        "jobItemId": attempt.job_item_id,
        "inputVariant": attempt.input_variant,
        "parentChecksum": attempt.parent_checksum,
        "cropChecksum": attempt.crop_checksum,
        "cropBox": attempt.crop_box,
        "provider": attempt.provider,
        "modelVersion": attempt.model_version,
        "parameterHash": attempt.parameter_hash,
        "language": attempt.language,
        "direction": attempt.direction,
        "text": attempt.text,
        "textChecksum": attempt.text_checksum,
        "confidence": attempt.confidence,
        "createdAt": attempt.created_at,
    }


def _g6_attempt_payload(attempt: RegionOCRAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "regionId": attempt.region_id,
        "generationId": attempt.generation_id,
        "jobId": attempt.job_id,
        "jobItemId": attempt.job_item_id,
        "inputVariant": attempt.input_variant,
        "parentChecksum": attempt.parent_checksum,
        "cropChecksum": attempt.crop_checksum,
        "cropBox": attempt.crop_box,
        "provider": attempt.provider,
        "modelVersion": attempt.model_version,
        "parameterHash": attempt.parameter_hash,
        "language": attempt.language,
        "direction": attempt.direction,
        "text": attempt.text,
        "textChecksum": attempt.text_checksum,
        "confidence": attempt.confidence,
    }


def _g6_rows_and_attempts(
    session: Session,
    image_id: str,
    generation_id: str,
) -> tuple[list[TextRegion], list[RegionOCRAttempt]]:
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image_id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    attempts = list(
        session.scalars(
            select(RegionOCRAttempt)
            .where(
                RegionOCRAttempt.image_id == image_id,
                RegionOCRAttempt.generation_id == generation_id,
            )
            .order_by(
                RegionOCRAttempt.region_id,
                RegionOCRAttempt.job_item_id,
                RegionOCRAttempt.input_variant,
                RegionOCRAttempt.id,
            )
        ).all()
    )
    return rows, attempts


def _g6_state_payload(
    rows: list[TextRegion],
    attempts: list[RegionOCRAttempt],
    *,
    blank: bool = False,
) -> list[dict[str, Any]]:
    attempts_by_region: dict[str, list[RegionOCRAttempt]] = {}
    for attempt in attempts:
        attempts_by_region.setdefault(attempt.region_id, []).append(attempt)
    return [
        {
            "id": row.id,
            "eligibility": "required" if ocr_source_review_required(row) else "not-applicable",
            "attempts": (
                []
                if blank
                else [
                    _g6_attempt_payload(attempt) for attempt in attempts_by_region.get(row.id, [])
                ]
            ),
            "sourceText": None if blank or row.ocr_review is None else row.source_text,
            "review": None if blank else row.ocr_review,
            "reviewer": None if blank else row.ocr_reviewer,
            "generationId": None if blank else row.ocr_generation_id,
        }
        for row in rows
    ]


def _g6_checksum_for_rows(
    rows: list[TextRegion],
    attempts: list[RegionOCRAttempt],
    *,
    blank: bool = False,
) -> str:
    encoded = json.dumps(
        _g6_state_payload(rows, attempts, blank=blank),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def g6_ocr_state_checksum(session: Session, image_id: str, generation_id: str) -> str:
    """Hash G6 eligibility, immutable attempts, trusted source, QC, and reviewer."""
    rows, attempts = _g6_rows_and_attempts(session, image_id, generation_id)
    return _g6_checksum_for_rows(rows, attempts)


def _text_is_garbled(value: str) -> bool:
    return "\ufffd" in value or any(
        ord(character) < 32 and character not in {"\n", "\t"} for character in value
    )


def _has_repeated_fragment(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return bool(re.search(r"(.{2,8})\1\1", compact))


def _japanese_character_ratio(value: str) -> float:
    meaningful = [character for character in value if character.isalnum()]
    if not meaningful:
        return 0.0
    japanese = sum(
        "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff"
        for character in meaningful
    )
    return japanese / len(meaningful)


def derive_ocr_qc_flags(
    source_text: str,
    *,
    source_mode: str,
    selected_attempt: RegionOCRAttempt,
    attempts: list[RegionOCRAttempt],
) -> list[str]:
    paired = [
        attempt for attempt in attempts if attempt.job_item_id == selected_attempt.job_item_id
    ]
    texts = [attempt.text.strip() for attempt in paired]
    flags: set[str] = set()
    variants = {attempt.input_variant: attempt.text.strip() for attempt in paired}
    if set(variants) == {"original", "quality"} and variants["original"] != variants["quality"]:
        flags.add("original-quality-disagree")
    if any(not text for text in texts):
        flags.add("ocr-empty-attempt")
    if any(_text_is_garbled(text) for text in texts):
        flags.add("ocr-garbled-attempt")
    if any(_has_repeated_fragment(text) for text in texts):
        flags.add("duplicate-fragment")
    if any(_OCR_TEMPLATE_RE.search(text) for text in texts):
        flags.add("template-contamination")
    if _japanese_character_ratio(source_text) < 0.25:
        flags.add("low-japanese-character-ratio")
    if source_mode == "manual-correction":
        flags.add("manual-correction")
    return sorted(flags) if flags else ["none"]


def _g6_validation_issues(
    rows: list[TextRegion],
    attempts: list[RegionOCRAttempt],
    *,
    generation_id: str,
    original_checksum: str,
    quality_checksum: str,
    completed_job_item_ids: set[str],
    require_complete: bool,
) -> tuple[list[str], int, int, int]:
    issues: set[str] = set()
    rows_by_id = {row.id: row for row in rows}
    attempts_by_region: dict[str, list[RegionOCRAttempt]] = {}
    attempts_by_id: dict[str, RegionOCRAttempt] = {}
    for attempt in attempts:
        attempts_by_region.setdefault(attempt.region_id, []).append(attempt)
        attempts_by_id[attempt.id] = attempt
        row = rows_by_id.get(attempt.region_id)
        expected_parent = (
            original_checksum if attempt.input_variant == "original" else quality_checksum
        )
        box = attempt.crop_box
        if row is None or not ocr_source_review_required(row):
            issues.add("ineligible-region-has-ocr-attempt")
        if attempt.image_id != (row.image_id if row is not None else None):
            issues.add("ocr-attempt-image-mismatch")
        if attempt.generation_id != generation_id:
            issues.add("ocr-attempt-generation-stale")
        if attempt.input_variant not in {"original", "quality"}:
            issues.add("ocr-attempt-variant-invalid")
        if attempt.parent_checksum != expected_parent:
            issues.add("ocr-attempt-parent-stale")
        if (
            not isinstance(box, dict)
            or set(box) != {"x", "y", "width", "height"}
            or any(
                not isinstance(box.get(key), int) or isinstance(box.get(key), bool)
                for key in ("x", "y", "width", "height")
            )
        ):
            issues.add("ocr-attempt-crop-invalid")
        elif box["x"] < 0 or box["y"] < 0 or box["width"] < 1 or box["height"] < 1:
            issues.add("ocr-attempt-crop-invalid")
        if (
            not isinstance(attempt.crop_checksum, str)
            or not _SHA256_RE.fullmatch(attempt.crop_checksum)
            or not isinstance(attempt.parameter_hash, str)
            or not _SHA256_RE.fullmatch(attempt.parameter_hash)
            or attempt.text_checksum != _sha256_text(attempt.text)
        ):
            issues.add("ocr-attempt-checksum-invalid")
        if (
            not isinstance(attempt.provider, str)
            or not _OPAQUE_ID_RE.fullmatch(attempt.provider)
            or attempt.direction not in {"horizontal", "vertical"}
            or (row is not None and attempt.direction != row.direction)
            or isinstance(attempt.confidence, bool)
            or (
                attempt.confidence is not None
                and (
                    not math.isfinite(float(attempt.confidence))
                    or float(attempt.confidence) < 0
                    or float(attempt.confidence) > 1
                )
            )
        ):
            issues.add("ocr-attempt-evidence-invalid")

    eligible_count = 0
    attempted_count = 0
    reviewed_count = 0
    for row in rows:
        bundle = (row.ocr_review, row.ocr_reviewer, row.ocr_generation_id)
        region_attempts = attempts_by_region.get(row.id, [])
        if not ocr_source_review_required(row):
            if any(value is not None for value in bundle):
                issues.add("ineligible-region-has-ocr-review")
            continue
        eligible_count += 1
        variants = {attempt.input_variant for attempt in region_attempts}
        if {"original", "quality"}.issubset(variants):
            attempted_count += 1
        elif require_complete:
            issues.add("ocr-dual-attempts-missing")
        if all(value is None for value in bundle):
            if require_complete:
                issues.add("ocr-source-review-missing")
            continue
        if any(value is None for value in bundle):
            issues.add("ocr-source-review-incomplete")
            continue
        review = row.ocr_review
        reviewer = row.ocr_reviewer
        if not isinstance(review, dict):
            issues.add("ocr-source-review-invalid")
            continue
        source_mode = review.get("sourceMode")
        selected_attempt_id = review.get("selectedAttemptId")
        checks = review.get("qcChecks")
        flags = review.get("qcFlags")
        selected = attempts_by_id.get(selected_attempt_id)
        if (
            source_mode not in _OCR_SOURCE_MODES
            or not isinstance(selected_attempt_id, str)
            or selected is None
            or selected.region_id != row.id
            or selected.job_item_id not in completed_job_item_ids
            or not isinstance(checks, list)
            or len(checks) != len(set(checks))
            or set(checks) != OCR_QC_CHECKS
            or not isinstance(flags, list)
            or not flags
            or any(not isinstance(flag, str) for flag in flags)
            or len(flags) != len(set(flags))
            or not set(flags).issubset(OCR_QC_FLAGS)
            or ("none" in flags and len(flags) != 1)
        ):
            issues.add("ocr-source-review-invalid")
            continue
        if source_mode == "original-attempt" and selected.input_variant != "original":
            issues.add("ocr-selected-attempt-mode-mismatch")
        if source_mode == "quality-attempt" and selected.input_variant != "quality":
            issues.add("ocr-selected-attempt-mode-mismatch")
        if source_mode != "manual-correction" and row.source_text != selected.text.strip():
            issues.add("ocr-source-text-attempt-mismatch")
        if (
            not row.source_text.strip()
            or _text_is_garbled(row.source_text)
            or _OCR_TEMPLATE_RE.search(row.source_text)
            or review.get("sourceTextChecksum") != _sha256_text(row.source_text)
            or flags
            != derive_ocr_qc_flags(
                row.source_text,
                source_mode=str(source_mode),
                selected_attempt=selected,
                attempts=region_attempts,
            )
        ):
            issues.add("ocr-source-text-invalid")
        if row.ocr_generation_id != generation_id:
            issues.add("ocr-review-generation-stale")
        if not isinstance(reviewer, dict):
            issues.add("ocr-reviewer-invalid")
        else:
            try:
                if reviewer != _safe_actor(reviewer):
                    issues.add("ocr-reviewer-invalid")
            except (AttributeError, PageLineageConflict):
                issues.add("ocr-reviewer-invalid")
        reviewed_count += 1
    return sorted(issues), eligible_count, attempted_count, reviewed_count


def record_text_presence_decision(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    evidence: list[str],
    observed_original_checksum: str,
    observed_quality_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    evidence_set = set(evidence)
    if (
        decision not in _TEXT_PRESENCE_REASONS
        or reason != _TEXT_PRESENCE_REASONS[decision]
        or len(evidence_set) != len(evidence)
        or not evidence_set.issubset(_TEXT_PRESENCE_EVIDENCE)
        or "original-and-quality-compared" not in evidence_set
        or (decision == "yes" and not evidence_set.intersection(_VISUAL_TEXT_EVIDENCE))
        or (decision == "no" and "no-processable-text-visible" not in evidence_set)
        or (decision == "uncertain" and "conflicting-signals" not in evidence_set)
    ):
        raise ProjectError("Text-presence decision evidence is incomplete or inconsistent")

    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before the text-presence decision",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            binding = require_image_mutation_lineage(store, session, image, lineage)
            if binding is None:
                raise PageLineageConflict(
                    "G3 decisions require an active page generation",
                    resource=f"image:{image.id}",
                    reason="active-generation-missing",
                )
            generation, actor, expected_sequence = binding
            quality = require_current_quality_plate(store, session, image, generation)
            if observed_original_checksum != generation.source_checksum:
                raise PageLineageConflict(
                    "The observed original does not match the immutable source",
                    resource=f"page-generation:{generation.id}",
                    reason="observed-original-checksum-mismatch",
                )
            if observed_quality_checksum != quality["checksum"]:
                raise PageLineageConflict(
                    "The observed quality plate no longer matches accepted G2 evidence",
                    resource=f"page-generation:{generation.id}",
                    reason="observed-quality-checksum-mismatch",
                )
            if decision == "no":
                residuals = _no_text_residuals(store, session, image)
                if residuals:
                    raise PageLineageConflict(
                        "A no-text page still has downstream text-processing state",
                        resource=f"image:{image.id}",
                        reason="no-text-residuals:" + ",".join(residuals),
                    )

            project = store.project(session)
            status = dict(image.status or {})
            before = {
                "gate": "G3_textPresence",
                "reviewState": status.get("reviewState", "pending"),
                "reviewedAt": status.get("reviewedAt") or "",
            }
            now = datetime.now(UTC)
            if decision == "no":
                status["reviewState"] = "no-text-reviewed"
                status["reviewedAt"] = now.isoformat()
            else:
                status["reviewState"] = "pending"
                status["reviewedAt"] = ""
            status["export"] = "pending"
            image.status = status
            image.revision += 1
            state = "pending" if decision == "uncertain" else "accepted"
            revision = add_revision(
                session,
                project,
                entity_type="page-gate",
                entity_id=generation.id,
                operation="text-presence",
                before=before,
                after={
                    "gate": "G3_textPresence",
                    "decision": decision,
                    "reason": reason,
                    "state": state,
                    "qualityChecksum": quality["checksum"],
                    "reviewState": status["reviewState"],
                },
            )
            session.flush()
            event = _append_event(
                session,
                generation,
                operation="text-presence-decision",
                gate="G3_textPresence",
                state=state,
                actor=actor,
                input_checksum=quality["checksum"],
                output_checksum=quality["checksum"],
                parent_checksum=quality["checksum"],
                stage="text-presence",
                parameter_hash=generation.parameter_set_hash,
                revision_id=revision.id,
                decision={"yes": "text-present", "no": "no-text", "uncertain": "uncertain"}[
                    decision
                ],
                reason=reason,
                evidence={
                    "eventType": "text-presence-decision",
                    "imageRevision": image.revision,
                    "qualityState": state,
                    "targetKind": quality["targetKind"],
                    "textPresence": decision,
                    "visualComparison": True,
                    "evidenceCodes": sorted(evidence_set),
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def require_no_active_generations_for_project_settings(session: Session, project_id: str) -> None:
    generation = session.scalar(
        select(PageGeneration.id).where(PageGeneration.project_id == project_id).limit(1)
    )
    if generation is not None:
        raise PageLineageConflict(
            "Project processing settings cannot change while page lineage exists",
            resource=f"project:{project_id}",
            reason="active-generation-settings-lock",
        )


def require_no_page_generations_for_project_ingest(session: Session, project_id: str) -> None:
    generation = session.scalar(
        select(PageGeneration.id).where(PageGeneration.project_id == project_id).limit(1)
    )
    if generation is not None:
        raise PageLineageConflict(
            "Images cannot be added after page lineage has started",
            resource=f"project:{project_id}",
            reason="page-lineage-image-ingest-lock",
        )


def record_stage_review_event(
    session: Session,
    *,
    binding: tuple[PageGeneration, dict[str, str | None], int] | None,
    stage: str,
    state: str,
    checksums: dict[str, str],
    revision_id: str,
    reviewed_at: datetime,
) -> None:
    if binding is None:
        return
    generation, actor, expected_sequence = binding
    if state != "pending":
        _require_completed_production_event(
            session,
            generation=generation,
            stage=stage,
            output_checksum=checksums.get("artifactChecksum"),
        )
    parent_checksum = _latest_accepted_checksum(session, generation.id)
    evidence: dict[str, Any] = {
        "eventType": "stage-review",
        "qualityState": state,
    }
    if checksums.get("maskChecksum"):
        evidence["maskChecksum"] = checksums["maskChecksum"]
    if checksums.get("provenanceDigest"):
        evidence["provenanceDigest"] = checksums["provenanceDigest"]
    _append_event(
        session,
        generation,
        operation=f"{stage}-stage-review",
        gate={
            "preprocess": "G1_baselineUpscale",
            "inpaint": "G8_cleanPlate",
            "typeset": "G10_typeset",
        }[stage],
        state=state,
        actor=actor,
        input_checksum=parent_checksum,
        output_checksum=checksums.get("artifactChecksum"),
        parent_checksum=parent_checksum,
        stage=stage,
        parameter_hash=generation.parameter_set_hash,
        revision_id=revision_id,
        decision=f"{stage}-{state}",
        reason="checksum-observed" if state != "pending" else "review-reset",
        evidence=evidence,
        started_at=reviewed_at,
        finished_at=reviewed_at,
        expected_sequence=expected_sequence,
    )


def require_supported_lineage_job_kind(kind: str) -> None:
    if kind not in _SUPPORTED_LINEAGE_JOB_KINDS:
        raise PageLineageConflict(
            "This operation is blocked until its lineage gate is implemented",
            resource=f"job-kind:{kind}",
            reason="lineage-stage-not-supported",
        )


def job_mutation_binding(
    job: Job,
    item: JobItem,
    generations: dict[str, PageGeneration],
) -> JobMutationBinding | None:
    if job.lineage_context is None or item.image_id is None:
        return None
    generation = generations.get(item.image_id)
    if generation is None:
        raise PageLineageConflict(
            "Job item has no matching active page generation",
            resource=f"job-item:{item.id}",
            reason="generation-mismatch",
        )
    actor = _safe_actor(job.lineage_context["actor"])
    return {
        "generationId": generation.id,
        "sourceChecksum": generation.source_checksum,
        "jobId": job.id,
        "jobItemId": item.id,
        "actor": actor,
        "startedAt": item.started_at,
    }


def record_detect_regions_produced(
    store: ProjectStore,
    session: Session,
    *,
    binding: JobMutationBinding | None,
    input_checksum: str,
    output_checksum: str,
    provider: str | None,
    image_revision: int,
    region_count: int,
) -> None:
    """Append G4 production evidence in the transaction publishing detector rows."""
    if binding is None:
        return
    if not _SHA256_RE.fullmatch(input_checksum) or not _SHA256_RE.fullmatch(output_checksum):
        raise PageLineageConflict(
            "Produced region checksum evidence is invalid",
            resource=f"job-item:{binding['jobItemId']}",
            reason="invalid-checksum",
        )
    generation = session.get(PageGeneration, binding["generationId"])
    job = session.get(Job, binding["jobId"])
    item = session.get(JobItem, binding["jobItemId"])
    image = session.get(ImageAsset, generation.image_id) if generation is not None else None
    try:
        persisted_page_map = (
            _lineage_page_map(job.lineage_context)
            if job is not None and isinstance(job.lineage_context, dict)
            else {}
        )
    except PageLineageConflict:
        persisted_page_map = {}
    if (
        generation is None
        or generation.state != "active"
        or job is None
        or job.kind != "detect"
        or item is None
        or item.status != "running"
        or image is None
        or job.lineage_context is None
        or item.image_id != generation.image_id
        or job.lineage_context.get("runId") != generation.run_id
        or persisted_page_map.get(generation.image_id) != generation.id
        or binding["sourceChecksum"] != generation.source_checksum
    ):
        raise PageLineageConflict(
            "Detector job lineage changed before region publication",
            resource=f"job-item:{binding['jobItemId']}",
            reason="generation-mismatch",
        )
    if (
        image.checksum != generation.source_checksum
        or _sha256_file(_immutable_image_path(store, image)) != generation.source_checksum
    ):
        raise PageLineageConflict(
            "Immutable page source changed before region publication",
            resource=f"image:{generation.image_id}",
            reason="source-checksum-changed",
        )
    quality, g3_event = require_current_text_present_quality_plate(
        store,
        session,
        image,
        generation,
    )
    _require_g5_not_started(session, generation)
    if input_checksum != quality["checksum"]:
        raise PageLineageConflict(
            "Detector input no longer matches the accepted quality plate",
            resource=f"job-item:{item.id}",
            reason="detect-input-checksum-mismatch",
        )
    enqueued = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "detect-job-enqueued",
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    safe_provider = (
        provider
        if isinstance(provider, str) and len(provider) <= 80 and _OPAQUE_ID_RE.fullmatch(provider)
        else None
    )
    if (
        enqueued is None
        or enqueued.sequence <= g3_event.sequence
        or enqueued.input_checksum != quality["checksum"]
        or enqueued.parent_checksum != quality["checksum"]
    ):
        raise PageLineageConflict(
            "Detector enqueue evidence is missing or stale",
            resource=f"job-item:{item.id}",
            reason="detect-enqueue-evidence-missing",
        )
    if safe_provider is None or enqueued.provider != safe_provider:
        raise PageLineageConflict(
            "Runtime detector provider does not match enqueue evidence",
            resource=f"job-item:{item.id}",
            reason="detect-provider-mismatch",
        )
    if g4_region_state_checksum(session, image.id) != output_checksum:
        raise PageLineageConflict(
            "Published detector rows do not match their G4 checksum",
            resource=f"job-item:{item.id}",
            reason="detect-output-checksum-mismatch",
        )
    _append_event(
        session,
        generation,
        operation="detect-regions-produced",
        gate="G4_regions",
        state="pending",
        actor=binding["actor"],
        input_checksum=quality["checksum"],
        output_checksum=output_checksum,
        parent_checksum=quality["checksum"],
        stage="detection",
        provider=safe_provider,
        parameter_hash=generation.parameter_set_hash,
        job_id=job.id,
        job_item_id=item.id,
        reason="review-required",
        evidence={
            "eventType": "regions-produced",
            "qualityState": "pending-review",
            "imageRevision": image_revision,
            "targetKind": "region-set",
            "regionCount": region_count,
        },
        started_at=binding["startedAt"],
        finished_at=datetime.now(UTC),
    )


def record_job_artifact_produced(
    store: ProjectStore,
    session: Session,
    *,
    binding: JobMutationBinding | None,
    stage: str,
    input_checksum: str,
    output_checksum: str,
    provider: str | None,
    image_revision: int,
) -> None:
    if binding is None:
        return
    if not _SHA256_RE.fullmatch(input_checksum) or not _SHA256_RE.fullmatch(output_checksum):
        raise PageLineageConflict(
            "Produced artifact checksum evidence is invalid",
            resource=f"job-item:{binding['jobItemId']}",
            reason="invalid-checksum",
        )
    generation = session.get(PageGeneration, binding["generationId"])
    if generation is None or generation.state != "active":
        raise PageLineageConflict(
            "Page generation changed before artifact publication",
            resource=f"page-generation:{binding['generationId']}",
            reason="active-generation-missing",
        )
    job = session.get(Job, binding["jobId"])
    item = session.get(JobItem, binding["jobItemId"])
    image = session.get(ImageAsset, generation.image_id)
    try:
        persisted_page_map = (
            _lineage_page_map(job.lineage_context)
            if job is not None and isinstance(job.lineage_context, dict)
            else {}
        )
    except PageLineageConflict:
        persisted_page_map = {}
    if (
        job is None
        or item is None
        or image is None
        or job.lineage_context is None
        or item.image_id != generation.image_id
        or job.lineage_context.get("runId") != generation.run_id
        or persisted_page_map.get(generation.image_id) != generation.id
    ):
        raise PageLineageConflict(
            "Job lineage changed before artifact publication",
            resource=f"job-item:{binding['jobItemId']}",
            reason="generation-mismatch",
        )
    if (
        image.checksum != generation.source_checksum
        or input_checksum != generation.source_checksum
        or _sha256_file(_immutable_image_path(store, image)) != generation.source_checksum
    ):
        raise PageLineageConflict(
            "Immutable page source changed before artifact publication",
            resource=f"image:{generation.image_id}",
            reason="source-checksum-changed",
        )
    safe_provider = (
        provider
        if isinstance(provider, str) and len(provider) <= 80 and _OPAQUE_ID_RE.fullmatch(provider)
        else None
    )
    _append_event(
        session,
        generation,
        operation=f"{stage}-artifact-produced",
        gate={
            "preprocess": "G1_baselineUpscale",
            "inpaint": "G8_cleanPlate",
            "typeset": "G10_typeset",
        }[stage],
        state="pending",
        actor=binding["actor"],
        input_checksum=input_checksum,
        output_checksum=output_checksum,
        parent_checksum=input_checksum,
        stage=stage,
        provider=safe_provider,
        parameter_hash=generation.parameter_set_hash,
        job_id=job.id,
        job_item_id=item.id,
        reason="review-required",
        evidence={
            "eventType": "artifact-produced",
            "qualityState": "pending-review",
            "imageRevision": image_revision,
            "targetKind": "image",
        },
        started_at=binding["startedAt"],
        finished_at=datetime.now(UTC),
    )


def _require_completed_production_event(
    session: Session,
    *,
    generation: PageGeneration,
    stage: str,
    output_checksum: str | None,
) -> PageLineageEvent:
    if output_checksum is None:
        raise PageLineageConflict(
            "Stage review has no current artifact checksum",
            resource=f"page-generation:{generation.id}",
            reason="production-evidence-missing",
        )
    produced = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.operation == f"{stage}-artifact-produced",
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if produced is None or produced.output_checksum != output_checksum:
        raise PageLineageConflict(
            "Current artifact has no matching atomic production evidence",
            resource=f"page-generation:{generation.id}",
            reason="production-evidence-missing",
        )
    completion_operations = {
        "preprocess": {"preprocess-job-completed"},
        "inpaint": {"inpaint-job-completed"},
        "typeset": {"typeset-job-completed", "render-job-completed"},
    }[stage]
    completed = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == produced.job_item_id,
            PageLineageEvent.operation.in_(completion_operations),
            PageLineageEvent.sequence > produced.sequence,
            PageLineageEvent.output_checksum == output_checksum,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    item = session.get(JobItem, produced.job_item_id) if produced.job_item_id else None
    if completed is None or item is None or item.status != "completed":
        raise PageLineageConflict(
            "Producing job item has not committed completion evidence",
            resource=f"job-item:{produced.job_item_id or 'unknown'}",
            reason="producer-not-completed",
        )
    return produced


def _require_current_g4_draft(
    session: Session,
    *,
    generation: PageGeneration,
    quality: QualityPlateBinding,
    g3_event: PageLineageEvent,
) -> tuple[str, PageLineageEvent, PageLineageEvent]:
    """Verify the latest detector publication/completion and its mutation chain."""
    enqueued = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.operation == "detect-job-enqueued",
            PageLineageEvent.sequence > g3_event.sequence,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if enqueued is None or enqueued.job_item_id is None:
        raise PageLineageConflict(
            "G4 has no detector job bound to the current text-present decision",
            resource=f"page-generation:{generation.id}",
            reason="g4-production-missing",
        )
    produced = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == enqueued.job_item_id,
            PageLineageEvent.operation == "detect-regions-produced",
            PageLineageEvent.sequence > enqueued.sequence,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if (
        produced is None
        or produced.input_checksum != quality["checksum"]
        or produced.parent_checksum != quality["checksum"]
        or enqueued.provider is None
        or produced.provider != enqueued.provider
        or not isinstance(produced.output_checksum, str)
        or not _SHA256_RE.fullmatch(produced.output_checksum)
    ):
        raise PageLineageConflict(
            "G4 detector publication is missing or stale",
            resource=f"job-item:{enqueued.job_item_id}",
            reason="g4-production-missing",
        )
    completed = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == produced.job_item_id,
            PageLineageEvent.operation == "detect-job-completed",
            PageLineageEvent.sequence > produced.sequence,
            PageLineageEvent.output_checksum == produced.output_checksum,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    item = session.get(JobItem, produced.job_item_id) if produced.job_item_id else None
    if (
        completed is None
        or completed.provider != produced.provider
        or item is None
        or item.status != "completed"
    ):
        raise PageLineageConflict(
            "The detector item has not committed matching completion evidence",
            resource=f"job-item:{produced.job_item_id or 'unknown'}",
            reason="producer-not-completed",
        )
    expected_checksum = produced.output_checksum
    semantic_events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G4_regions",
                PageLineageEvent.sequence > completed.sequence,
                PageLineageEvent.operation.in_(
                    tuple(_G4_MUTATION_OPERATIONS | {"regions-stage-review"})
                ),
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    for event in semantic_events:
        if event.parent_checksum != quality["checksum"]:
            raise PageLineageConflict(
                "G4 draft evidence does not descend from the current quality plate",
                resource=f"page-generation:{generation.id}",
                reason="g4-lineage-mismatch",
            )
        if event.operation in _G4_MUTATION_OPERATIONS:
            if (
                event.input_checksum != expected_checksum
                or not isinstance(event.output_checksum, str)
                or not _SHA256_RE.fullmatch(event.output_checksum)
            ):
                raise PageLineageConflict(
                    "G4 mutation evidence is not a continuous checksum chain",
                    resource=f"page-generation:{generation.id}",
                    reason="g4-lineage-mismatch",
                )
            expected_checksum = event.output_checksum
        elif event.output_checksum != expected_checksum:
            raise PageLineageConflict(
                "G4 acceptance evidence does not match the current draft",
                resource=f"page-generation:{generation.id}",
                reason="g4-lineage-mismatch",
            )
    latest_g4 = _latest_gate_event(session, generation.id, "G4_regions")
    if (
        latest_g4 is None
        or latest_g4.sequence < completed.sequence
        or latest_g4.output_checksum != expected_checksum
    ):
        raise PageLineageConflict(
            "G4 draft evidence is no longer current",
            resource=f"page-generation:{generation.id}",
            reason="g4-evidence-not-current",
        )
    return expected_checksum, produced, completed


def _require_g5_not_started(session: Session, generation: PageGeneration) -> None:
    started = session.scalar(
        select(PageLineageEvent.id)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.gate == "G5_background",
        )
        .limit(1)
    )
    if started is not None:
        raise PageLineageConflict(
            "G4 is locked because background review has started",
            resource=f"page-generation:{generation.id}",
            reason="g5-started-g4-locked",
        )


def _require_current_g4_acceptance(
    store: ProjectStore,
    session: Session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, PageLineageEvent]:
    quality, g3_event = require_current_text_present_quality_plate(
        store,
        session,
        image,
        generation,
    )
    draft_checksum, _produced, _completed = _require_current_g4_draft(
        session,
        generation=generation,
        quality=quality,
        g3_event=g3_event,
    )
    accepted = _latest_gate_event(session, generation.id, "G4_regions")
    actual_checksum = g4_region_state_checksum(session, image.id)
    if (
        accepted is None
        or accepted.operation != "regions-stage-review"
        or accepted.state != "accepted"
        or accepted.decision != "regions-accepted"
        or accepted.reason != "all-region-decisions-reviewed"
        or accepted.output_checksum != actual_checksum
        or accepted.parent_checksum != quality["checksum"]
        or draft_checksum != actual_checksum
        or (image.status or {}).get("detection") != "done"
    ):
        raise PageLineageConflict(
            "G5 requires the current G4 region set to be explicitly accepted",
            resource=f"page-generation:{generation.id}",
            reason="g4-regions-not-currently-accepted",
        )
    return actual_checksum, accepted


def _require_current_g5_draft(
    session: Session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    g4_checksum: str,
    g4_accepted: PageLineageEvent,
    verify_stored: bool = True,
) -> tuple[str, PageLineageEvent | None]:
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image.id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    expected_checksum = _g5_checksum_for_rows(rows, blank=True)
    events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G5_background",
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    terminal: PageLineageEvent | None = None
    latest_reviewer_by_region: dict[str, dict[str, str | None]] = {}
    for event in events:
        if event.sequence <= g4_accepted.sequence or event.parent_checksum != g4_checksum:
            raise PageLineageConflict(
                "G5 evidence does not descend from the current accepted G4 region set",
                resource=f"page-generation:{generation.id}",
                reason="g5-lineage-mismatch",
            )
        if terminal is not None:
            raise PageLineageConflict(
                "G5 evidence changed after its terminal review",
                resource=f"page-generation:{generation.id}",
                reason="g5-accepted-state-mutated",
            )
        if event.operation in _G5_MUTATION_OPERATIONS:
            target_region_id = (event.evidence or {}).get("targetRegionId")
            if (
                event.state != "pending"
                or event.input_checksum != expected_checksum
                or not isinstance(event.output_checksum, str)
                or not _SHA256_RE.fullmatch(event.output_checksum)
                or not isinstance(target_region_id, str)
            ):
                raise PageLineageConflict(
                    "G5 classification evidence is not a continuous checksum chain",
                    resource=f"page-generation:{generation.id}",
                    reason="g5-lineage-mismatch",
                )
            latest_reviewer_by_region[target_region_id] = _public_actor(event)
            expected_checksum = event.output_checksum
            continue
        if event.operation == "background-stage-review":
            if (
                event.state not in {"accepted", "not-applicable"}
                or event.input_checksum != expected_checksum
                or event.output_checksum != expected_checksum
            ):
                raise PageLineageConflict(
                    "G5 terminal review does not match its classification draft",
                    resource=f"page-generation:{generation.id}",
                    reason="g5-lineage-mismatch",
                )
            terminal = event
            continue
        raise PageLineageConflict(
            "G5 contains unsupported evidence",
            resource=f"page-generation:{generation.id}",
            reason="g5-lineage-mismatch",
        )
    actual_checksum = _g5_checksum_for_rows(rows)
    if verify_stored and actual_checksum != expected_checksum:
        raise PageLineageConflict(
            "Stored background classifications do not match their G5 evidence",
            resource=f"page-generation:{generation.id}",
            reason="g5-evidence-not-current",
        )
    if verify_stored:
        rows_by_id = {row.id: row for row in rows}
        if set(latest_reviewer_by_region) - set(rows_by_id):
            raise PageLineageConflict(
                "G5 classification evidence targets a missing region",
                resource=f"page-generation:{generation.id}",
                reason="g5-lineage-mismatch",
            )
        for row in rows:
            if row.background_category is None:
                continue
            if latest_reviewer_by_region.get(row.id) != row.background_reviewer:
                raise PageLineageConflict(
                    "Stored G5 reviewer does not match classification event evidence",
                    resource=f"region:{row.id}",
                    reason="g5-reviewer-evidence-mismatch",
                )
    return expected_checksum, terminal


def record_background_classification_mutation(
    store: ProjectStore,
    session: Session,
    *,
    image: ImageAsset,
    region: TextRegion,
    binding: tuple[PageGeneration, dict[str, str | None], int],
    before_checksum: str,
    after_checksum: str,
    revision_id: str,
) -> PageLineageEvent:
    generation, actor, expected_sequence = binding
    g4_checksum, g4_accepted = _require_current_g4_acceptance(
        store,
        session,
        image=image,
        generation=generation,
    )
    current_checksum, terminal = _require_current_g5_draft(
        session,
        image=image,
        generation=generation,
        g4_checksum=g4_checksum,
        g4_accepted=g4_accepted,
        verify_stored=False,
    )
    if terminal is not None:
        raise PageLineageConflict(
            "Accepted G5 background classifications are immutable",
            resource=f"page-generation:{generation.id}",
            reason="g5-backgrounds-accepted",
        )
    if (
        current_checksum != before_checksum
        or g5_background_state_checksum(session, image.id) != after_checksum
    ):
        raise PageLineageConflict(
            "Background mutation checksums do not match the current G5 draft",
            resource=f"region:{region.id}",
            reason="g5-mutation-checksum-mismatch",
        )
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image.id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    issues, eligible_count, classified_count = _g5_validation_issues(
        rows,
        generation_id=generation.id,
        require_complete=False,
    )
    if issues:
        raise PageLineageConflict(
            "G5 background draft is inconsistent",
            resource=f"image:{image.id}",
            reason="g5-backgrounds-invalid:" + ",".join(issues),
        )
    return _append_event(
        session,
        generation,
        operation="background-classification-reviewed",
        gate="G5_background",
        state="pending",
        actor=actor,
        input_checksum=before_checksum,
        output_checksum=after_checksum,
        parent_checksum=g4_checksum,
        stage="background",
        parameter_hash=generation.parameter_set_hash,
        revision_id=revision_id,
        decision="background-classification-recorded",
        reason=str(region.background_category),
        evidence={
            "eventType": "background-classification-reviewed",
            "qualityState": "pending-review",
            "imageRevision": image.revision,
            "targetKind": "region",
            "targetRegionId": region.id,
            "regionCount": len(rows),
            "eligibleRegionCount": eligible_count,
            "classifiedRegionCount": classified_count,
        },
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        expected_sequence=expected_sequence,
    )


def require_current_background_classifications(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, PageLineageEvent]:
    """Return current accepted/not-applicable G5 evidence for future G6+ gates."""
    g4_checksum, g4_accepted = _require_current_g4_acceptance(
        store,
        session,
        image=image,
        generation=generation,
    )
    checksum, terminal = _require_current_g5_draft(
        session,
        image=image,
        generation=generation,
        g4_checksum=g4_checksum,
        g4_accepted=g4_accepted,
    )
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image.id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    issues, eligible_count, classified_count = _g5_validation_issues(
        rows,
        generation_id=generation.id,
        require_complete=True,
    )
    expected_state = "accepted" if eligible_count else "not-applicable"
    expected_decision = "backgrounds-accepted" if eligible_count else "background-not-applicable"
    expected_reason = (
        "all-eligible-backgrounds-reviewed" if eligible_count else "no-eligible-regions"
    )
    if (
        issues
        or classified_count != eligible_count
        or terminal is None
        or terminal.state != expected_state
        or terminal.decision != expected_decision
        or terminal.reason != expected_reason
        or terminal.output_checksum != checksum
    ):
        raise PageLineageConflict(
            "G5 background classifications are not current and explicitly accepted",
            resource=f"page-generation:{generation.id}",
            reason=(
                "g5-backgrounds-invalid:" + ",".join(issues)
                if issues
                else "g5-backgrounds-not-currently-accepted"
            ),
        )
    return checksum, terminal


def _require_current_g6_draft(
    session: Session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    g5_checksum: str,
    g5_accepted: PageLineageEvent,
    verify_stored: bool = True,
) -> tuple[str, PageLineageEvent | None, set[str], set[str]]:
    rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
    expected_checksum = _g6_checksum_for_rows(rows, attempts, blank=True)
    events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G6_ocr",
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    enqueued_by_item: dict[str, PageLineageEvent] = {}
    produced_by_item: dict[str, PageLineageEvent] = {}
    completed_job_item_ids: set[str] = set()
    failed_job_item_ids: set[str] = set()
    latest_review_by_region: dict[str, tuple[dict[str, str | None], str | None]] = {}
    terminal: PageLineageEvent | None = None
    for event in events:
        if event.sequence <= g5_accepted.sequence or event.parent_checksum != g5_checksum:
            raise PageLineageConflict(
                "G6 evidence does not descend from the current accepted G5 state",
                resource=f"page-generation:{generation.id}",
                reason="g6-lineage-mismatch",
            )
        if terminal is not None:
            raise PageLineageConflict(
                "G6 evidence changed after its terminal review",
                resource=f"page-generation:{generation.id}",
                reason="g6-accepted-state-mutated",
            )
        item_id = event.job_item_id
        if event.operation == "ocr-job-enqueued":
            if (
                event.state != "pending"
                or not isinstance(item_id, str)
                or item_id in enqueued_by_item
                or any(
                    prior_item_id not in completed_job_item_ids
                    and prior_item_id not in failed_job_item_ids
                    for prior_item_id in enqueued_by_item
                )
                or event.input_checksum != expected_checksum
                or event.output_checksum != expected_checksum
                or event.provider is None
            ):
                raise PageLineageConflict(
                    "G6 enqueue evidence is not bound to the current draft",
                    resource=f"page-generation:{generation.id}",
                    reason="g6-lineage-mismatch",
                )
            enqueued_by_item[item_id] = event
            continue
        if event.operation == "ocr-attempts-produced":
            enqueued = enqueued_by_item.get(item_id or "")
            if (
                event.state != "pending"
                or enqueued is None
                or item_id in produced_by_item
                or item_id in completed_job_item_ids
                or item_id in failed_job_item_ids
                or event.sequence <= enqueued.sequence
                or event.input_checksum != expected_checksum
                or event.input_checksum != enqueued.input_checksum
                or not isinstance(event.output_checksum, str)
                or not _SHA256_RE.fullmatch(event.output_checksum)
                or event.provider != enqueued.provider
            ):
                raise PageLineageConflict(
                    "G6 OCR publication is not a continuous checksum mutation",
                    resource=f"job-item:{item_id or 'unknown'}",
                    reason="g6-lineage-mismatch",
                )
            produced_by_item[item_id] = event
            expected_checksum = event.output_checksum
            continue
        if event.operation == "ocr-job-completed":
            enqueued = enqueued_by_item.get(item_id or "")
            produced = produced_by_item.get(item_id or "")
            item = session.get(JobItem, item_id) if item_id else None
            if (
                event.state != "pending"
                or enqueued is None
                or produced is None
                or item_id in completed_job_item_ids
                or item_id in failed_job_item_ids
                or event.sequence <= produced.sequence
                or event.input_checksum != enqueued.input_checksum
                or event.output_checksum != produced.output_checksum
                or event.output_checksum != expected_checksum
                or event.provider != produced.provider
                or item is None
                or item.status != "completed"
            ):
                raise PageLineageConflict(
                    "G6 OCR completion evidence is missing or stale",
                    resource=f"job-item:{item_id or 'unknown'}",
                    reason="g6-producer-not-completed",
                )
            completed_job_item_ids.add(item_id)
            continue
        if event.operation == "ocr-job-failed":
            enqueued = enqueued_by_item.get(item_id or "")
            item = session.get(JobItem, item_id) if item_id else None
            if (
                event.state != "blocked"
                or enqueued is None
                or item_id in produced_by_item
                or item_id in completed_job_item_ids
                or item_id in failed_job_item_ids
                or event.sequence <= enqueued.sequence
                or event.input_checksum != enqueued.input_checksum
                or event.output_checksum is not None
                or item is None
                or item.status != "failed"
            ):
                raise PageLineageConflict(
                    "G6 failed-job evidence is inconsistent",
                    resource=f"job-item:{item_id or 'unknown'}",
                    reason="g6-lineage-mismatch",
                )
            failed_job_item_ids.add(item_id)
            continue
        if event.operation == "ocr-source-reviewed":
            target_region_id = (event.evidence or {}).get("targetRegionId")
            selected_attempt_id = (event.evidence or {}).get("selectedAttemptId")
            if (
                event.state != "pending"
                or event.input_checksum != expected_checksum
                or not isinstance(event.output_checksum, str)
                or not _SHA256_RE.fullmatch(event.output_checksum)
                or not isinstance(target_region_id, str)
                or not isinstance(selected_attempt_id, str)
                or any(
                    enqueued_item_id not in completed_job_item_ids
                    and enqueued_item_id not in failed_job_item_ids
                    for enqueued_item_id in enqueued_by_item
                )
            ):
                raise PageLineageConflict(
                    "G6 source review is not a continuous checksum mutation",
                    resource=f"page-generation:{generation.id}",
                    reason="g6-lineage-mismatch",
                )
            latest_review_by_region[target_region_id] = (
                _public_actor(event),
                selected_attempt_id,
            )
            expected_checksum = event.output_checksum
            continue
        if event.operation == "ocr-stage-review":
            if (
                event.state not in {"accepted", "not-applicable"}
                or event.input_checksum != expected_checksum
                or event.output_checksum != expected_checksum
                or any(
                    enqueued_item_id not in completed_job_item_ids
                    and enqueued_item_id not in failed_job_item_ids
                    for enqueued_item_id in enqueued_by_item
                )
            ):
                raise PageLineageConflict(
                    "G6 terminal review does not match the current OCR draft",
                    resource=f"page-generation:{generation.id}",
                    reason="g6-lineage-mismatch",
                )
            terminal = event
            continue
        raise PageLineageConflict(
            "G6 contains unsupported evidence",
            resource=f"page-generation:{generation.id}",
            reason="g6-lineage-mismatch",
        )

    if verify_stored and _g6_checksum_for_rows(rows, attempts) != expected_checksum:
        raise PageLineageConflict(
            "Stored OCR attempts or reviews do not match their G6 evidence",
            resource=f"page-generation:{generation.id}",
            reason="g6-evidence-not-current",
        )
    if verify_stored:
        attempt_item_ids = {attempt.job_item_id for attempt in attempts}
        if attempt_item_ids != set(produced_by_item):
            raise PageLineageConflict(
                "Stored OCR attempts do not match publication events",
                resource=f"page-generation:{generation.id}",
                reason="g6-attempt-publication-mismatch",
            )
        attempts_per_item: dict[str, int] = {}
        for attempt in attempts:
            attempts_per_item[attempt.job_item_id] = (
                attempts_per_item.get(attempt.job_item_id, 0) + 1
            )
        for item_id, produced in produced_by_item.items():
            if (produced.evidence or {}).get("ocrAttemptCount") != attempts_per_item.get(
                item_id, 0
            ):
                raise PageLineageConflict(
                    "G6 attempt count does not match publication evidence",
                    resource=f"job-item:{item_id}",
                    reason="g6-attempt-publication-mismatch",
                )
        rows_by_id = {row.id: row for row in rows}
        if set(latest_review_by_region) - set(rows_by_id):
            raise PageLineageConflict(
                "G6 source review targets a missing region",
                resource=f"page-generation:{generation.id}",
                reason="g6-lineage-mismatch",
            )
        for row in rows:
            if row.ocr_review is None:
                continue
            evidence = latest_review_by_region.get(row.id)
            selected_attempt_id = row.ocr_review.get("selectedAttemptId")
            if evidence != (row.ocr_reviewer, selected_attempt_id):
                raise PageLineageConflict(
                    "Stored G6 reviewer does not match source-review event evidence",
                    resource=f"region:{row.id}",
                    reason="g6-reviewer-evidence-mismatch",
                )
    unfinished_job_item_ids = set(enqueued_by_item) - (completed_job_item_ids | failed_job_item_ids)
    return expected_checksum, terminal, completed_job_item_ids, unfinished_job_item_ids


def require_current_ocr_trust(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, PageLineageEvent]:
    """Return the current explicit G6 trust gate for future G7+ work."""
    quality, _g3_event = require_current_text_present_quality_plate(
        store, session, image, generation
    )
    g5_checksum, g5_accepted = require_current_background_classifications(
        store, session, image, generation
    )
    checksum, terminal, completed, unfinished = _require_current_g6_draft(
        session,
        image=image,
        generation=generation,
        g5_checksum=g5_checksum,
        g5_accepted=g5_accepted,
    )
    rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
    issues, eligible_count, attempted_count, reviewed_count = _g6_validation_issues(
        rows,
        attempts,
        generation_id=generation.id,
        original_checksum=generation.source_checksum,
        quality_checksum=quality["checksum"],
        completed_job_item_ids=completed,
        require_complete=True,
    )
    expected_state = "accepted" if eligible_count else "not-applicable"
    expected_decision = "ocr-trust-accepted" if eligible_count else "ocr-not-applicable"
    expected_reason = (
        "all-translatable-source-text-reviewed" if eligible_count else "no-translatable-regions"
    )
    if (
        issues
        or unfinished
        or attempted_count != eligible_count
        or reviewed_count != eligible_count
        or terminal is None
        or terminal.state != expected_state
        or terminal.decision != expected_decision
        or terminal.reason != expected_reason
        or terminal.output_checksum != checksum
    ):
        raise PageLineageConflict(
            "G6 OCR source text is not current and explicitly trusted",
            resource=f"page-generation:{generation.id}",
            reason=(
                "g6-ocr-invalid:" + ",".join(issues) if issues else "g6-ocr-not-currently-accepted"
            ),
        )
    return checksum, terminal


def record_ocr_attempts_produced(
    store: ProjectStore,
    session: Session,
    *,
    binding: JobMutationBinding | None,
    input_checksum: str,
    output_checksum: str,
    provider: str,
    model_version: str | None,
    parameter_hash: str,
    image_revision: int,
    region_count: int,
    attempt_count: int,
    revision_id: str,
) -> None:
    """Append G6 publication evidence in the transaction inserting attempts."""
    if binding is None:
        return
    if (
        not _SHA256_RE.fullmatch(input_checksum)
        or not _SHA256_RE.fullmatch(output_checksum)
        or not _SHA256_RE.fullmatch(parameter_hash)
    ):
        raise PageLineageConflict(
            "Produced OCR checksum evidence is invalid",
            resource=f"job-item:{binding['jobItemId']}",
            reason="invalid-checksum",
        )
    generation = session.get(PageGeneration, binding["generationId"])
    job = session.get(Job, binding["jobId"])
    item = session.get(JobItem, binding["jobItemId"])
    image = session.get(ImageAsset, generation.image_id) if generation is not None else None
    try:
        persisted_page_map = (
            _lineage_page_map(job.lineage_context)
            if job is not None and isinstance(job.lineage_context, dict)
            else {}
        )
    except PageLineageConflict:
        persisted_page_map = {}
    if (
        generation is None
        or generation.state != "active"
        or job is None
        or job.kind != "ocr"
        or item is None
        or item.status != "running"
        or item.region_id is not None
        or image is None
        or job.lineage_context is None
        or item.image_id != generation.image_id
        or job.lineage_context.get("runId") != generation.run_id
        or persisted_page_map.get(generation.image_id) != generation.id
        or binding["sourceChecksum"] != generation.source_checksum
    ):
        raise PageLineageConflict(
            "OCR job lineage changed before attempt publication",
            resource=f"job-item:{binding['jobItemId']}",
            reason="generation-mismatch",
        )
    if (
        image.checksum != generation.source_checksum
        or _sha256_file(_immutable_image_path(store, image)) != generation.source_checksum
    ):
        raise PageLineageConflict(
            "Immutable page source changed before OCR publication",
            resource=f"image:{generation.image_id}",
            reason="source-checksum-changed",
        )
    quality, _g3_event = require_current_text_present_quality_plate(
        store, session, image, generation
    )
    g5_checksum, g5_accepted = require_current_background_classifications(
        store, session, image, generation
    )
    current_checksum, terminal, completed, _unfinished = _require_current_g6_draft(
        session,
        image=image,
        generation=generation,
        g5_checksum=g5_checksum,
        g5_accepted=g5_accepted,
        verify_stored=False,
    )
    if terminal is not None:
        raise PageLineageConflict(
            "Accepted G6 OCR evidence is immutable",
            resource=f"page-generation:{generation.id}",
            reason="g6-ocr-accepted",
        )
    enqueued = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "ocr-job-enqueued",
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if (
        current_checksum != input_checksum
        or enqueued is None
        or enqueued.input_checksum != input_checksum
        or enqueued.output_checksum != input_checksum
        or enqueued.parent_checksum != g5_checksum
        or enqueued.provider != provider
        or item.id in completed
        or g6_ocr_state_checksum(session, image.id, generation.id) != output_checksum
    ):
        raise PageLineageConflict(
            "OCR publication no longer matches its enqueue or G6 draft",
            resource=f"job-item:{item.id}",
            reason="g6-ocr-input-changed",
        )
    rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
    eligible = [row for row in rows if ocr_source_review_required(row)]
    item_attempts = [attempt for attempt in attempts if attempt.job_item_id == item.id]
    per_region_variants = {
        row.id: {attempt.input_variant for attempt in item_attempts if attempt.region_id == row.id}
        for row in eligible
    }
    if (
        region_count != len(eligible)
        or not eligible
        or attempt_count != len(item_attempts)
        or attempt_count != len(eligible) * 2
        or any(variants != {"original", "quality"} for variants in per_region_variants.values())
        or {attempt.region_id for attempt in item_attempts} != {row.id for row in eligible}
    ):
        raise PageLineageConflict(
            "Strict OCR publication must contain both crops for every eligible region",
            resource=f"job-item:{item.id}",
            reason="g6-dual-attempts-incomplete",
        )
    issues, _eligible_count, _attempted_count, _reviewed_count = _g6_validation_issues(
        rows,
        attempts,
        generation_id=generation.id,
        original_checksum=generation.source_checksum,
        quality_checksum=quality["checksum"],
        completed_job_item_ids=completed,
        require_complete=False,
    )
    if issues:
        raise PageLineageConflict(
            "Published OCR attempt evidence is inconsistent",
            resource=f"job-item:{item.id}",
            reason="g6-ocr-invalid:" + ",".join(issues),
        )
    _append_event(
        session,
        generation,
        operation="ocr-attempts-produced",
        gate="G6_ocr",
        state="pending",
        actor=binding["actor"],
        input_checksum=input_checksum,
        output_checksum=output_checksum,
        parent_checksum=g5_checksum,
        stage="ocr",
        provider=provider,
        model_version=model_version,
        parameter_hash=parameter_hash,
        job_id=job.id,
        job_item_id=item.id,
        revision_id=revision_id,
        reason="source-review-required",
        evidence={
            "eventType": "ocr-attempts-produced",
            "qualityState": "pending-review",
            "imageRevision": image_revision,
            "targetKind": "region-set",
            "regionCount": len(rows),
            "eligibleRegionCount": len(eligible),
            "attemptedRegionCount": len(eligible),
            "ocrAttemptCount": attempt_count,
        },
        started_at=binding["startedAt"],
        finished_at=datetime.now(UTC),
    )


def record_ocr_source_review_mutation(
    store: ProjectStore,
    session: Session,
    *,
    image: ImageAsset,
    region: TextRegion,
    selected_attempt_id: str,
    binding: tuple[PageGeneration, dict[str, str | None], int],
    before_checksum: str,
    after_checksum: str,
    revision_id: str,
) -> PageLineageEvent:
    generation, actor, expected_sequence = binding
    quality, _g3_event = require_current_text_present_quality_plate(
        store, session, image, generation
    )
    g5_checksum, g5_accepted = require_current_background_classifications(
        store, session, image, generation
    )
    current_checksum, terminal, completed, unfinished = _require_current_g6_draft(
        session,
        image=image,
        generation=generation,
        g5_checksum=g5_checksum,
        g5_accepted=g5_accepted,
        verify_stored=False,
    )
    if terminal is not None:
        raise PageLineageConflict(
            "Accepted G6 OCR evidence is immutable",
            resource=f"page-generation:{generation.id}",
            reason="g6-ocr-accepted",
        )
    if unfinished:
        raise PageLineageConflict(
            "OCR source review must wait for the active strict OCR job",
            resource=f"page-generation:{generation.id}",
            reason="g6-ocr-job-active",
        )
    if (
        current_checksum != before_checksum
        or g6_ocr_state_checksum(session, image.id, generation.id) != after_checksum
    ):
        raise PageLineageConflict(
            "OCR review checksums do not match the current G6 draft",
            resource=f"region:{region.id}",
            reason="g6-review-checksum-mismatch",
        )
    rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
    issues, eligible_count, attempted_count, reviewed_count = _g6_validation_issues(
        rows,
        attempts,
        generation_id=generation.id,
        original_checksum=generation.source_checksum,
        quality_checksum=quality["checksum"],
        completed_job_item_ids=completed,
        require_complete=False,
    )
    if issues:
        raise PageLineageConflict(
            "G6 OCR review draft is inconsistent",
            resource=f"image:{image.id}",
            reason="g6-ocr-invalid:" + ",".join(issues),
        )
    return _append_event(
        session,
        generation,
        operation="ocr-source-reviewed",
        gate="G6_ocr",
        state="pending",
        actor=actor,
        input_checksum=before_checksum,
        output_checksum=after_checksum,
        parent_checksum=g5_checksum,
        stage="ocr",
        provider=region.ocr_provider,
        parameter_hash=generation.parameter_set_hash,
        revision_id=revision_id,
        decision="source-text-trusted",
        reason=str((region.ocr_review or {}).get("sourceMode")),
        evidence={
            "eventType": "ocr-source-reviewed",
            "qualityState": "pending-review",
            "imageRevision": image.revision,
            "targetKind": "region",
            "targetRegionId": region.id,
            "selectedAttemptId": selected_attempt_id,
            "regionCount": len(rows),
            "eligibleRegionCount": eligible_count,
            "attemptedRegionCount": attempted_count,
            "reviewedRegionCount": reviewed_count,
        },
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        expected_sequence=expected_sequence,
    )


def record_g4_region_mutation(
    store: ProjectStore,
    session: Session,
    *,
    image: ImageAsset,
    binding: tuple[PageGeneration, dict[str, str | None], int] | None,
    operation: str,
    before_checksum: str,
    after_checksum: str,
    revision_id: str,
    region_count: int,
) -> PageLineageEvent | None:
    if binding is None:
        return None
    if operation not in _G4_MUTATION_OPERATIONS:
        raise ProjectError("Unsupported G4 region mutation operation")
    generation, actor, expected_sequence = binding
    _require_g5_not_started(session, generation)
    quality, g3_event = require_current_text_present_quality_plate(
        store,
        session,
        image,
        generation,
    )
    current_checksum, _produced, _completed = _require_current_g4_draft(
        session,
        generation=generation,
        quality=quality,
        g3_event=g3_event,
    )
    if current_checksum != before_checksum or g4_region_state_checksum(session, image.id) != (
        after_checksum
    ):
        raise PageLineageConflict(
            "Region mutation checksums do not match the current G4 draft",
            resource=f"image:{image.id}",
            reason="g4-mutation-checksum-mismatch",
        )
    return _append_event(
        session,
        generation,
        operation=operation,
        gate="G4_regions",
        state="pending",
        actor=actor,
        input_checksum=before_checksum,
        output_checksum=after_checksum,
        parent_checksum=quality["checksum"],
        stage="detection",
        parameter_hash=generation.parameter_set_hash,
        revision_id=revision_id,
        decision="region-draft-mutated",
        reason=operation,
        evidence={
            "eventType": "regions-mutated",
            "qualityState": "pending-review",
            "imageRevision": image.revision,
            "targetKind": "region-set",
            "regionOperation": operation,
            "regionCount": region_count,
        },
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        expected_sequence=expected_sequence,
    )


def reconcile_committed_g4_reorder(
    store: ProjectStore,
    session: Session,
    *,
    image: ImageAsset,
    binding: tuple[PageGeneration, dict[str, str | None], int],
    requested_region_ids: list[str],
) -> PageLineageEvent | None:
    """Append evidence for the one known pre-fix committed-reorder residue.

    The recovery is deliberately narrow: it accepts only an otherwise current G4
    draft followed by a contiguous project-revision suffix made exclusively of
    reorder revisions for this image. Replaying that suffix backwards must produce
    the exact checksum held by the lineage ledger.
    """

    generation, actor, expected_sequence = binding
    _require_g5_not_started(session, generation)
    quality, g3_event = require_current_text_present_quality_plate(
        store,
        session,
        image,
        generation,
    )
    ledger_checksum, _produced, _completed = _require_current_g4_draft(
        session,
        generation=generation,
        quality=quality,
        g3_event=g3_event,
    )
    current_checksum = g4_region_state_checksum(session, image.id)
    if current_checksum == ledger_checksum:
        return None

    resource = f"image:{image.id}"

    def conflict(reason: str) -> NoReturn:
        raise PageLineageConflict(
            "Committed G4 reorder residue could not be reconciled",
            resource=resource,
            reason=f"g4-reorder-recovery-{reason}",
        )

    latest = _latest_gate_event(session, generation.id, "G4_regions")
    if (
        latest is None
        or latest.sequence != expected_sequence - 1
        or latest.state != "pending"
        or latest.operation not in _G4_MUTATION_OPERATIONS
        or latest.output_checksum != ledger_checksum
        or latest.parent_checksum != quality["checksum"]
        or latest.revision_id is None
    ):
        conflict("anchor-invalid")
    anchor = session.get(Revision, latest.revision_id)
    project = store.project(session)
    if anchor is None or anchor.project_id != project.id:
        conflict("anchor-invalid")

    rows = list(session.scalars(select(TextRegion).where(TextRegion.image_id == image.id)).all())
    current_payloads = {row.id: region_payload(row) for row in rows}
    current_order = [row.id for row in sorted(rows, key=lambda row: (row.reading_order, row.id))]
    if requested_region_ids != current_order:
        conflict("request-mismatch")

    revisions = list(
        session.scalars(
            select(Revision)
            .where(
                Revision.project_id == project.id,
                Revision.project_revision > anchor.project_revision,
            )
            .order_by(Revision.project_revision)
        ).all()
    )
    expected_project_revisions = list(
        range(anchor.project_revision + 1, anchor.project_revision + 1 + len(revisions))
    )
    if (
        not revisions
        or [revision.project_revision for revision in revisions] != expected_project_revisions
        or project.revision != revisions[-1].project_revision
    ):
        conflict("revision-suffix-invalid")

    virtual_payloads = {region_id: dict(payload) for region_id, payload in current_payloads.items()}
    for revision in reversed(revisions):
        before = revision.before
        after = revision.after
        if (
            revision.operation != "reorder"
            or revision.entity_type != "region"
            or revision.entity_id not in virtual_payloads
            or not isinstance(before, dict)
            or not isinstance(after, dict)
            or before.keys() != after.keys()
            or before.get("id") != revision.entity_id
            or after.get("id") != revision.entity_id
            or before.get("imageId") != image.id
            or after.get("imageId") != image.id
            or virtual_payloads[revision.entity_id] != after
        ):
            conflict("revision-suffix-invalid")
        changed_keys = {key for key in before if before.get(key) != after.get(key)}
        if changed_keys != {"order", "revision"}:
            conflict("revision-delta-invalid")
        before_revision = before.get("revision")
        after_revision = after.get("revision")
        if (
            not isinstance(before.get("order"), int)
            or isinstance(before.get("order"), bool)
            or not isinstance(after.get("order"), int)
            or isinstance(after.get("order"), bool)
            or not isinstance(before_revision, int)
            or isinstance(before_revision, bool)
            or after_revision != before_revision + 1
        ):
            conflict("revision-delta-invalid")
        virtual_payloads[revision.entity_id] = dict(before)

    try:
        replayed_checksum = _g4_region_payload_checksum(list(virtual_payloads.values()))
    except (KeyError, TypeError, ValueError, OverflowError):
        conflict("revision-suffix-invalid")
    if replayed_checksum != ledger_checksum:
        conflict("checksum-mismatch")

    prior_image_revision = latest.evidence.get("imageRevision")
    if (
        not isinstance(prior_image_revision, int)
        or isinstance(prior_image_revision, bool)
        or image.revision != prior_image_revision + 1
    ):
        conflict("image-revision-invalid")

    witness_payload = [
        {
            "id": revision.id,
            "projectRevision": revision.project_revision,
            "entityId": revision.entity_id,
            "before": revision.before,
            "after": revision.after,
        }
        for revision in revisions
    ]
    witness_checksum = hashlib.sha256(
        json.dumps(
            witness_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now = datetime.now(UTC)
    return _append_event(
        session,
        generation,
        operation="regions-reordered",
        gate="G4_regions",
        state="pending",
        actor=actor,
        input_checksum=ledger_checksum,
        output_checksum=current_checksum,
        parent_checksum=quality["checksum"],
        stage="detection",
        parameter_hash=generation.parameter_set_hash,
        revision_id=revisions[-1].id,
        decision="region-draft-reconciled",
        reason="committed-reorder-missing-event",
        evidence={
            "eventType": "regions-mutated",
            "qualityState": "pending-review",
            "imageRevision": image.revision,
            "targetKind": "region-set",
            "regionOperation": "regions-reordered",
            "regionCount": len(rows),
            "recovered": True,
            "recoveryKind": "committed-reorder-missing-event",
            "revisionIds": [revision.id for revision in revisions],
            "revisionWitnessChecksum": witness_checksum,
        },
        started_at=now,
        finished_at=now,
        expected_sequence=expected_sequence,
    )


def _g4_validation_issues(image: ImageAsset, rows: list[TextRegion]) -> list[str]:
    if not rows:
        return ["regions-empty"]
    issues: set[str] = set()
    if sorted(row.reading_order for row in rows) != list(range(len(rows))):
        issues.add("reading-order-invalid")
    if not any(row.content_disposition != "false-positive" for row in rows):
        issues.add("processable-region-missing")
    by_id = {row.id: row for row in rows}
    for row in rows:
        if row.content_disposition not in _G4_CONTENT_DISPOSITIONS:
            issues.add("disposition-missing")
        if not all(
            math.isfinite(float(value))
            for value in (row.x, row.y, row.width, row.height, row.rotation)
        ) or (
            row.width <= 0
            or row.height <= 0
            or row.x < 0
            or row.y < 0
            or row.x + row.width > image.width + 0.001
            or row.y + row.height > image.height + 0.001
        ):
            issues.add("geometry-invalid")
        detector_pair = (row.detector_job_item_id, row.detector_candidate_index)
        if (detector_pair[0] is None) != (detector_pair[1] is None) or (
            detector_pair[1] is not None and detector_pair[1] < 0
        ):
            issues.add("detector-identity-invalid")
        if row.content_disposition == "false-positive":
            if row.ruby_parent_id is not None:
                issues.add("ruby-parent-on-false-positive")
            continue
        if row.direction not in {"horizontal", "vertical"}:
            issues.add("direction-unresolved")
        if row.region_type == "unknown":
            issues.add("type-unresolved")
        if not row.paragraph_group_id:
            issues.add("paragraph-group-missing")
        if row.region_type == "sound_effect" and row.content_disposition not in {
            "ignore",
            "keep-art",
            "redraw-art",
        }:
            issues.add("sound-effect-disposition-invalid")
        if row.region_type != "ruby":
            if row.ruby_parent_id is not None:
                issues.add("ruby-parent-on-non-ruby")
            continue
        # Ruby never becomes an independent OCR/translation target.  Most ruby
        # is removed with its translated parent (``ignore``), while
        # ``keep-art`` records the less common editorial decision to retain the
        # original annotation pixels.  The latter still keeps the semantic
        # parent link and paragraph grouping required for G4 auditability.
        if row.content_disposition not in {"ignore", "keep-art"}:
            issues.add("ruby-disposition-invalid")
        parent = by_id.get(row.ruby_parent_id or "")
        if row.ruby_parent_id == row.id:
            issues.add("ruby-parent-self")
        elif parent is None:
            issues.add("ruby-parent-missing")
        elif parent.region_type == "ruby":
            issues.add("ruby-parent-ruby")
        elif parent.content_disposition == "false-positive":
            issues.add("ruby-parent-false-positive")
        elif not row.paragraph_group_id or row.paragraph_group_id != parent.paragraph_group_id:
            issues.add("ruby-group-mismatch")
    return sorted(issues)


def record_regions_gate_acceptance(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    observed_region_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    if decision != "accept" or reason != "all-region-decisions-reviewed":
        raise ProjectError("G4 region decision and reason are inconsistent")
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before the G4 region decision",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            binding = require_image_mutation_lineage(store, session, image, lineage)
            if binding is None:
                raise PageLineageConflict(
                    "G4 decisions require an active page generation",
                    resource=f"image:{image.id}",
                    reason="active-generation-missing",
                )
            generation, actor, expected_sequence = binding
            _require_g5_not_started(session, generation)
            quality, g3_event = require_current_text_present_quality_plate(
                store,
                session,
                image,
                generation,
            )
            draft_checksum, _produced, _completed = _require_current_g4_draft(
                session,
                generation=generation,
                quality=quality,
                g3_event=g3_event,
            )
            actual_checksum = g4_region_state_checksum(session, image.id)
            if observed_region_checksum != actual_checksum or draft_checksum != actual_checksum:
                raise PageLineageConflict(
                    "Observed regions no longer match the current G4 draft",
                    resource=f"page-generation:{generation.id}",
                    reason="observed-region-checksum-mismatch",
                )
            rows = list(
                session.scalars(
                    select(TextRegion)
                    .where(TextRegion.image_id == image.id)
                    .order_by(TextRegion.reading_order, TextRegion.id)
                ).all()
            )
            issues = _g4_validation_issues(image, rows)
            if issues:
                raise PageLineageConflict(
                    "G4 region semantics are incomplete or inconsistent",
                    resource=f"image:{image.id}",
                    reason="g4-regions-invalid:" + ",".join(issues),
                )
            if (image.status or {}).get("detection") != "done":
                raise PageLineageConflict(
                    "Detector publication is not current on the image",
                    resource=f"image:{image.id}",
                    reason="g4-detection-status-not-current",
                )

            project = store.project(session)
            status = dict(image.status or {})
            before = {
                "gate": "G4_regions",
                "regionChecksum": draft_checksum,
                "regionCount": len(rows),
                "reviewState": status.get("reviewState", "pending"),
            }
            status["reviewState"] = "pending"
            status["reviewedAt"] = ""
            status["export"] = "pending"
            image.status = status
            image.revision += 1
            now = datetime.now(UTC)
            revision = add_revision(
                session,
                project,
                entity_type="page-gate",
                entity_id=generation.id,
                operation="regions",
                before=before,
                after={
                    "gate": "G4_regions",
                    "state": "accepted",
                    "regionChecksum": actual_checksum,
                    "regionCount": len(rows),
                },
            )
            session.flush()
            event = _append_event(
                session,
                generation,
                operation="regions-stage-review",
                gate="G4_regions",
                state="accepted",
                actor=actor,
                input_checksum=quality["checksum"],
                output_checksum=actual_checksum,
                parent_checksum=quality["checksum"],
                stage="detection",
                parameter_hash=generation.parameter_set_hash,
                revision_id=revision.id,
                decision="regions-accepted",
                reason=reason,
                evidence={
                    "eventType": "regions-stage-review",
                    "qualityState": "accepted",
                    "imageRevision": image.revision,
                    "targetKind": "region-set",
                    "regionCount": len(rows),
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def background_gate_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    """Return authoritative G5 checksums and eligibility without mutating evidence."""
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = session.scalar(
            select(PageGeneration).where(
                PageGeneration.project_id == image.project_id,
                PageGeneration.image_id == image.id,
                PageGeneration.state == "active",
            )
        )
        if generation is None:
            historical = session.scalar(
                select(PageGeneration.id)
                .where(
                    PageGeneration.project_id == image.project_id,
                    PageGeneration.image_id == image.id,
                )
                .limit(1)
            )
            raise PageLineageConflict(
                "G5 requires an active page generation",
                resource=f"image:{image.id}",
                reason=(
                    "page-generation-not-active"
                    if historical is not None
                    else "active-generation-missing"
                ),
            )
        g4_checksum, g4_accepted = _require_current_g4_acceptance(
            store,
            session,
            image=image,
            generation=generation,
        )
        checksum, terminal = _require_current_g5_draft(
            session,
            image=image,
            generation=generation,
            g4_checksum=g4_checksum,
            g4_accepted=g4_accepted,
        )
        rows = list(
            session.scalars(
                select(TextRegion)
                .where(TextRegion.image_id == image.id)
                .order_by(TextRegion.reading_order, TextRegion.id)
            ).all()
        )
        issues, _eligible_count, _classified_count = _g5_validation_issues(
            rows,
            generation_id=generation.id,
            require_complete=False,
        )
        if issues:
            raise PageLineageConflict(
                "G5 background draft is inconsistent",
                resource=f"image:{image.id}",
                reason="g5-backgrounds-invalid:" + ",".join(issues),
            )
        if terminal is not None:
            checksum, terminal = require_current_background_classifications(
                store,
                session,
                image,
                generation,
            )
        eligible = [row.id for row in rows if background_classification_required(row)]
        classified = [
            row.id
            for row in rows
            if background_classification_required(row) and row.background_category is not None
        ]
        return {
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "nextSequence": generation.next_sequence,
            "g4Checksum": g4_checksum,
            "backgroundChecksum": checksum,
            "state": terminal.state if terminal is not None else "pending",
            "eligibleRegionIds": eligible,
            "classifiedRegionIds": classified,
        }


def record_background_gate_acceptance(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    observed_background_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    if decision != "accept" or reason not in {
        "all-eligible-backgrounds-reviewed",
        "no-eligible-regions",
    }:
        raise ProjectError("G5 background decision and reason are inconsistent")
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before the G5 background decision",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            binding = require_image_mutation_lineage(store, session, image, lineage)
            if binding is None:
                raise PageLineageConflict(
                    "G5 decisions require an active page generation",
                    resource=f"image:{image.id}",
                    reason="active-generation-missing",
                )
            generation, actor, expected_sequence = binding
            g4_checksum, g4_accepted = _require_current_g4_acceptance(
                store,
                session,
                image=image,
                generation=generation,
            )
            actual_checksum, terminal = _require_current_g5_draft(
                session,
                image=image,
                generation=generation,
                g4_checksum=g4_checksum,
                g4_accepted=g4_accepted,
            )
            if terminal is not None:
                raise PageLineageConflict(
                    "G5 background review already has a terminal decision",
                    resource=f"page-generation:{generation.id}",
                    reason="g5-backgrounds-accepted",
                )
            if observed_background_checksum != actual_checksum:
                raise PageLineageConflict(
                    "Observed backgrounds no longer match the current G5 draft",
                    resource=f"page-generation:{generation.id}",
                    reason="observed-background-checksum-mismatch",
                )
            rows = list(
                session.scalars(
                    select(TextRegion)
                    .where(TextRegion.image_id == image.id)
                    .order_by(TextRegion.reading_order, TextRegion.id)
                ).all()
            )
            issues, eligible_count, classified_count = _g5_validation_issues(
                rows,
                generation_id=generation.id,
                require_complete=True,
            )
            expected_reason = (
                "all-eligible-backgrounds-reviewed" if eligible_count else "no-eligible-regions"
            )
            if reason != expected_reason:
                raise PageLineageConflict(
                    "G5 reason does not match page eligibility",
                    resource=f"image:{image.id}",
                    reason="g5-background-reason-mismatch",
                )
            if issues or classified_count != eligible_count:
                raise PageLineageConflict(
                    "G5 background classifications are incomplete or inconsistent",
                    resource=f"image:{image.id}",
                    reason=(
                        "g5-backgrounds-invalid:" + ",".join(issues)
                        if issues
                        else "g5-backgrounds-incomplete"
                    ),
                )

            project = store.project(session)
            status = dict(image.status or {})
            before = {
                "gate": "G5_background",
                "backgroundChecksum": actual_checksum,
                "eligibleRegionCount": eligible_count,
                "classifiedRegionCount": classified_count,
                "reviewState": status.get("reviewState", "pending"),
            }
            status["reviewState"] = "pending"
            status["reviewedAt"] = ""
            status["export"] = "pending"
            image.status = status
            image.revision += 1
            now = datetime.now(UTC)
            event_state = "accepted" if eligible_count else "not-applicable"
            event_decision = (
                "backgrounds-accepted" if eligible_count else "background-not-applicable"
            )
            revision = add_revision(
                session,
                project,
                entity_type="page-gate",
                entity_id=generation.id,
                operation="background",
                before=before,
                after={
                    "gate": "G5_background",
                    "state": event_state,
                    "backgroundChecksum": actual_checksum,
                    "eligibleRegionCount": eligible_count,
                    "classifiedRegionCount": classified_count,
                },
            )
            session.flush()
            event = _append_event(
                session,
                generation,
                operation="background-stage-review",
                gate="G5_background",
                state=event_state,
                actor=actor,
                input_checksum=actual_checksum,
                output_checksum=actual_checksum,
                parent_checksum=g4_checksum,
                stage="background",
                parameter_hash=generation.parameter_set_hash,
                revision_id=revision.id,
                decision=event_decision,
                reason=reason,
                evidence={
                    "eventType": "background-stage-review",
                    "qualityState": event_state,
                    "imageRevision": image.revision,
                    "targetKind": "region-set",
                    "regionCount": len(rows),
                    "eligibleRegionCount": eligible_count,
                    "classifiedRegionCount": classified_count,
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def ocr_gate_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    """Return authoritative G6 attempts, review progress, and current checksum."""
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = session.scalar(
            select(PageGeneration).where(
                PageGeneration.project_id == image.project_id,
                PageGeneration.image_id == image.id,
                PageGeneration.state == "active",
            )
        )
        if generation is None:
            historical = session.scalar(
                select(PageGeneration.id)
                .where(
                    PageGeneration.project_id == image.project_id,
                    PageGeneration.image_id == image.id,
                )
                .limit(1)
            )
            raise PageLineageConflict(
                "G6 requires an active page generation",
                resource=f"image:{image.id}",
                reason=(
                    "page-generation-not-active"
                    if historical is not None
                    else "active-generation-missing"
                ),
            )
        quality, _g3_event = require_current_text_present_quality_plate(
            store, session, image, generation
        )
        g5_checksum, g5_accepted = require_current_background_classifications(
            store, session, image, generation
        )
        checksum, terminal, completed, _unfinished = _require_current_g6_draft(
            session,
            image=image,
            generation=generation,
            g5_checksum=g5_checksum,
            g5_accepted=g5_accepted,
        )
        rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
        issues, _eligible_count, _attempted_count, _reviewed_count = _g6_validation_issues(
            rows,
            attempts,
            generation_id=generation.id,
            original_checksum=generation.source_checksum,
            quality_checksum=quality["checksum"],
            completed_job_item_ids=completed,
            require_complete=False,
        )
        if issues:
            raise PageLineageConflict(
                "G6 OCR draft is inconsistent",
                resource=f"image:{image.id}",
                reason="g6-ocr-invalid:" + ",".join(issues),
            )
        if terminal is not None:
            checksum, terminal = require_current_ocr_trust(store, session, image, generation)
        attempts_by_region: dict[str, set[str]] = {}
        for attempt in attempts:
            attempts_by_region.setdefault(attempt.region_id, set()).add(attempt.input_variant)
        eligible = [row.id for row in rows if ocr_source_review_required(row)]
        attempted = [
            row.id
            for row in rows
            if ocr_source_review_required(row)
            and attempts_by_region.get(row.id, set()) >= {"original", "quality"}
        ]
        reviewed = [
            row.id for row in rows if ocr_source_review_required(row) and row.ocr_review is not None
        ]
        return {
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "nextSequence": generation.next_sequence,
            "g5Checksum": g5_checksum,
            "ocrChecksum": checksum,
            "state": terminal.state if terminal is not None else "pending",
            "eligibleRegionIds": eligible,
            "attemptedRegionIds": attempted,
            "reviewedRegionIds": reviewed,
            "attempts": [public_ocr_attempt(attempt) for attempt in attempts],
        }


def record_ocr_gate_acceptance(
    store: ProjectStore,
    image_id: str,
    *,
    decision: str,
    reason: str,
    observed_ocr_checksum: str,
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    if decision != "accept" or reason not in {
        "all-translatable-source-text-reviewed",
        "no-translatable-regions",
    }:
        raise ProjectError("G6 OCR decision and reason are inconsistent")
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before the G6 OCR decision",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            binding = require_image_mutation_lineage(store, session, image, lineage)
            if binding is None:
                raise PageLineageConflict(
                    "G6 decisions require an active page generation",
                    resource=f"image:{image.id}",
                    reason="active-generation-missing",
                )
            generation, actor, expected_sequence = binding
            quality, _g3_event = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            g5_checksum, g5_accepted = require_current_background_classifications(
                store, session, image, generation
            )
            actual_checksum, terminal, completed, unfinished = _require_current_g6_draft(
                session,
                image=image,
                generation=generation,
                g5_checksum=g5_checksum,
                g5_accepted=g5_accepted,
            )
            if terminal is not None:
                raise PageLineageConflict(
                    "G6 OCR review already has a terminal decision",
                    resource=f"page-generation:{generation.id}",
                    reason="g6-ocr-accepted",
                )
            if unfinished:
                raise PageLineageConflict(
                    "G6 acceptance must wait for the active strict OCR job",
                    resource=f"page-generation:{generation.id}",
                    reason="g6-ocr-job-active",
                )
            if observed_ocr_checksum != actual_checksum:
                raise PageLineageConflict(
                    "Observed OCR evidence no longer matches the current G6 draft",
                    resource=f"page-generation:{generation.id}",
                    reason="observed-ocr-checksum-mismatch",
                )
            rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
            issues, eligible_count, attempted_count, reviewed_count = _g6_validation_issues(
                rows,
                attempts,
                generation_id=generation.id,
                original_checksum=generation.source_checksum,
                quality_checksum=quality["checksum"],
                completed_job_item_ids=completed,
                require_complete=True,
            )
            expected_reason = (
                "all-translatable-source-text-reviewed"
                if eligible_count
                else "no-translatable-regions"
            )
            if reason != expected_reason:
                raise PageLineageConflict(
                    "G6 reason does not match page eligibility",
                    resource=f"image:{image.id}",
                    reason="g6-ocr-reason-mismatch",
                )
            if issues or attempted_count != eligible_count or reviewed_count != eligible_count:
                raise PageLineageConflict(
                    "G6 OCR evidence is incomplete or inconsistent",
                    resource=f"image:{image.id}",
                    reason=(
                        "g6-ocr-invalid:" + ",".join(issues) if issues else "g6-ocr-incomplete"
                    ),
                )

            project = store.project(session)
            status = dict(image.status or {})
            before = {
                "gate": "G6_ocr",
                "ocrChecksum": actual_checksum,
                "eligibleRegionCount": eligible_count,
                "attemptedRegionCount": attempted_count,
                "reviewedRegionCount": reviewed_count,
                "reviewState": status.get("reviewState", "pending"),
            }
            status["reviewState"] = "pending"
            status["reviewedAt"] = ""
            status["export"] = "pending"
            image.status = status
            image.revision += 1
            now = datetime.now(UTC)
            event_state = "accepted" if eligible_count else "not-applicable"
            event_decision = "ocr-trust-accepted" if eligible_count else "ocr-not-applicable"
            revision = add_revision(
                session,
                project,
                entity_type="page-gate",
                entity_id=generation.id,
                operation="ocr",
                before=before,
                after={
                    "gate": "G6_ocr",
                    "state": event_state,
                    "ocrChecksum": actual_checksum,
                    "eligibleRegionCount": eligible_count,
                    "attemptedRegionCount": attempted_count,
                    "reviewedRegionCount": reviewed_count,
                },
            )
            session.flush()
            event = _append_event(
                session,
                generation,
                operation="ocr-stage-review",
                gate="G6_ocr",
                state=event_state,
                actor=actor,
                input_checksum=actual_checksum,
                output_checksum=actual_checksum,
                parent_checksum=g5_checksum,
                stage="ocr",
                parameter_hash=generation.parameter_set_hash,
                revision_id=revision.id,
                decision=event_decision,
                reason=reason,
                evidence={
                    "eventType": "ocr-stage-review",
                    "qualityState": event_state,
                    "imageRevision": image.revision,
                    "targetKind": "region-set",
                    "regionCount": len(rows),
                    "eligibleRegionCount": eligible_count,
                    "attemptedRegionCount": attempted_count,
                    "reviewedRegionCount": reviewed_count,
                    "ocrAttemptCount": len(attempts),
                },
                started_at=now,
                finished_at=now,
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def _safe_provider(kind: str, options: dict[str, Any], settings: dict[str, Any]) -> str | None:
    value: object = None
    for key in _PROVIDER_KEYS.get(kind, ()):
        if options.get(key):
            value = options[key]
            break
    if value is None:
        project_key = _PROJECT_PROVIDER_KEYS.get(kind)
        value = settings.get(project_key) if project_key else None
    if isinstance(value, str) and _OPAQUE_ID_RE.fullmatch(value) and len(value) <= 80:
        return _DETECT_PROVIDER_CANONICAL.get(value, value) if kind == "detect" else value
    return None


def _latest_accepted_checksum(session: Session, generation_id: str) -> str | None:
    return session.scalar(
        select(PageLineageEvent.output_checksum)
        .where(
            PageLineageEvent.generation_id == generation_id,
            PageLineageEvent.state == "accepted",
            PageLineageEvent.output_checksum.is_not(None),
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )


def record_job_enqueued_events(
    store: ProjectStore,
    session: Session,
    *,
    job: Job,
    items: list[JobItem],
    generations: dict[str, PageGeneration],
    project_settings: dict[str, Any],
) -> None:
    if job.lineage_context is None:
        return
    actor = _safe_actor(job.lineage_context["actor"])
    expected_sequences = _lineage_page_sequence_map(job.lineage_context)
    emitted_per_image: dict[str, int] = {}
    provider = _safe_provider(job.kind, dict(job.options), project_settings)
    for item in items:
        if item.image_id is None:
            continue
        generation = generations[item.image_id]
        event_input_checksum: str | None
        event_output_checksum: str | None = None
        evidence_counts: dict[str, Any] = {}
        event_model_version: str | None = None
        event_parameter_hash = generation.parameter_set_hash
        if job.kind == "detect":
            image = session.get(ImageAsset, item.image_id)
            if image is None:
                raise PageLineageConflict(
                    "Detector target image disappeared before enqueue",
                    resource=f"image:{item.image_id}",
                    reason="generation-mismatch",
                )
            quality, _g3_event = require_current_text_present_quality_plate(
                store,
                session,
                image,
                generation,
            )
            _require_g5_not_started(session, generation)
            parent_checksum = quality["checksum"]
            event_input_checksum = parent_checksum
        elif job.kind == "ocr":
            image = session.get(ImageAsset, item.image_id)
            if image is None or item.region_id is not None:
                raise PageLineageConflict(
                    "Strict G6 OCR requires one whole-page job item",
                    resource=f"job-item:{item.id}",
                    reason="g6-whole-page-required",
                )
            _quality, _g3_event = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            parent_checksum, g5_accepted = require_current_background_classifications(
                store, session, image, generation
            )
            draft_checksum, terminal, completed, unfinished = _require_current_g6_draft(
                session,
                image=image,
                generation=generation,
                g5_checksum=parent_checksum,
                g5_accepted=g5_accepted,
            )
            if terminal is not None:
                raise PageLineageConflict(
                    "Accepted G6 OCR evidence is immutable",
                    resource=f"page-generation:{generation.id}",
                    reason="g6-ocr-accepted",
                )
            if unfinished:
                raise PageLineageConflict(
                    "Another strict OCR job is already active for this page",
                    resource=f"image:{image.id}",
                    reason="g6-ocr-job-active",
                )
            rows, attempts = _g6_rows_and_attempts(session, image.id, generation.id)
            issues, eligible_count, _attempted_count, _reviewed_count = _g6_validation_issues(
                rows,
                attempts,
                generation_id=generation.id,
                original_checksum=generation.source_checksum,
                quality_checksum=_quality["checksum"],
                completed_job_item_ids=completed,
                require_complete=False,
            )
            if issues:
                raise PageLineageConflict(
                    "G6 OCR draft is inconsistent before enqueue",
                    resource=f"image:{image.id}",
                    reason="g6-ocr-invalid:" + ",".join(issues),
                )
            if not eligible_count:
                raise PageLineageConflict(
                    "A page without translatable regions must use the G6 not-applicable gate",
                    resource=f"image:{image.id}",
                    reason="g6-no-translatable-regions",
                )
            active_item = session.scalar(
                select(JobItem.id)
                .join(Job, Job.id == JobItem.job_id)
                .where(
                    Job.id != job.id,
                    Job.kind == "ocr",
                    Job.lineage_context.is_not(None),
                    JobItem.image_id == image.id,
                    JobItem.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            if active_item is not None:
                raise PageLineageConflict(
                    "Another strict OCR job is already active for this page",
                    resource=f"image:{image.id}",
                    reason="g6-ocr-job-active",
                )
            if provider is None:
                raise PageLineageConflict(
                    "Strict OCR requires a canonical local provider",
                    resource=f"job:{job.id}",
                    reason="g6-ocr-provider-invalid",
                )
            event_input_checksum = draft_checksum
            event_output_checksum = draft_checksum
            evidence_counts = {"eligibleRegionCount": eligible_count}
        elif job.kind == "mask":
            from manga_localizer.services.masks import (
                current_mask_state_checksum,
                eligible_mask_regions,
                mask_job_items_for_generation,
            )

            image = session.get(ImageAsset, item.image_id)
            if image is None or item.region_id is not None:
                raise PageLineageConflict(
                    "Strict G7 mask requires one whole-page job item",
                    resource=f"job-item:{item.id}",
                    reason="g7-whole-page-required",
                )
            parent_checksum, _terminal = require_current_ocr_trust(
                store, session, image, generation
            )
            quality, _quality_event = require_current_text_present_quality_plate(
                store, session, image, generation
            )
            eligible = eligible_mask_regions(session, image.id)
            if not eligible:
                raise PageLineageConflict(
                    "A zero-eligible page must use the G7 not-applicable decision",
                    resource=f"image:{image.id}",
                    reason="g7-mask-not-applicable",
                )
            from manga_localizer.database import PageMaskDraft, PageMaskReview

            terminal = session.scalar(
                select(PageMaskReview)
                .where(PageMaskReview.generation_id == generation.id)
                .order_by(PageMaskReview.sequence.desc())
                .limit(1)
            )
            if terminal is not None and terminal.state in {"accepted", "not-applicable"}:
                raise PageLineageConflict(
                    "Accepted G7 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g7-mask-accepted",
                )
            draft = session.get(PageMaskDraft, generation.id)
            if draft is None or draft.parent_checksum != parent_checksum:
                raise PageLineageConflict(
                    "Current G7 draft is missing",
                    resource=f"image:{image.id}",
                    reason="g7-mask-draft-missing",
                )
            if mask_job_items_for_generation(
                session,
                generation,
                statuses=("queued", "running"),
                exclude_job_id=job.id,
            ):
                raise PageLineageConflict(
                    "Another strict mask job is already active for this page",
                    resource=f"image:{image.id}",
                    reason="g7-mask-job-active",
                )
            current_state = current_mask_state_checksum(
                session,
                image=image,
                generation=generation,
                g6_checksum=parent_checksum,
                quality_checksum=quality["checksum"],
                replay_ignore_job_item_id=item.id,
            )
            event_input_checksum = current_state
            event_output_checksum = current_state
            from manga_localizer.services.masks import ruby_mapping

            mapping = ruby_mapping(session, image.id, eligible)
            provider = "deterministic-mask"
            event_model_version = "create-mask-v1"
            event_parameter_hash = draft.state_checksum
            evidence_counts = {
                "eligibleRegionCount": len(eligible),
                "rubyRegionCount": sum(map(len, mapping.values())),
                "rubyRegionIdsByPrimary": mapping,
                "recipeChecksum": draft.state_checksum,
                "qualityChecksum": quality["checksum"],
            }
        elif job.kind == "inpaint":
            from manga_localizer.services.clean_plates import prepare_clean_plate_enqueue

            image = session.get(ImageAsset, item.image_id)
            if image is None or item.region_id is not None:
                raise PageLineageConflict(
                    "Strict G8 inpaint requires one whole-page job item",
                    resource=f"job-item:{item.id}",
                    reason="g8-whole-page-required",
                )
            prepared = prepare_clean_plate_enqueue(
                store,
                session,
                image=image,
                generation=generation,
                job=job,
                item=item,
            )
            parent_checksum = prepared["g7Checksum"]
            event_input_checksum = prepared["stateChecksum"]
            event_output_checksum = prepared["stateChecksum"]
            provider = prepared["provider"]
            event_model_version = prepared["modelVersion"]
            event_parameter_hash = prepared["parameterHash"]
            evidence_counts = {
                "g7Checksum": prepared["g7Checksum"],
                "backgroundChecksum": prepared["backgroundChecksum"],
                "qualityChecksum": prepared["qualityChecksum"],
                "maskArtifactId": prepared["maskArtifactId"],
                "maskChecksum": prepared["maskChecksum"],
                "routeManifest": prepared["routeManifest"],
                "routeChecksum": prepared["routeChecksum"],
                **(
                    {"layeredStructureSnapshots": prepared["layeredStructureSnapshots"]}
                    if "layeredStructureSnapshots" in prepared
                    else {}
                ),
            }
        elif job.kind == "translate":
            from manga_localizer.services.translations import prepare_translation_enqueue

            image = session.get(ImageAsset, item.image_id)
            if image is None or item.region_id is not None:
                raise PageLineageConflict(
                    "Strict G9 translate requires one whole-page job item",
                    resource=f"job-item:{item.id}",
                    reason="g9-whole-page-required",
                )
            prepared = prepare_translation_enqueue(
                store, session, image=image, generation=generation, job=job, item=item
            )
            parent_checksum = prepared["g8Checksum"]
            event_input_checksum = prepared["stateChecksum"]
            event_output_checksum = prepared["stateChecksum"]
            provider = prepared["provider"]
            event_model_version = prepared["modelVersion"]
            event_parameter_hash = prepared["parameterHash"]
            evidence_counts = {"eligibleRegionCount": prepared["eligibleRegionCount"]}
        elif job.kind == "typeset":
            from manga_localizer.services.typesets import prepare_typeset_enqueue

            image = session.get(ImageAsset, item.image_id)
            if image is None or item.region_id is not None:
                raise PageLineageConflict(
                    "Strict G10 typeset requires one whole-page job item",
                    resource=f"job-item:{item.id}",
                    reason="g10-whole-page-required",
                )
            prepared = prepare_typeset_enqueue(
                store, session, image=image, generation=generation, job=job, item=item
            )
            parent_checksum = prepared["g9TerminalChecksum"]
            event_input_checksum = prepared["stateChecksum"]
            event_output_checksum = prepared["stateChecksum"]
            provider = prepared["provider"]
            event_model_version = prepared["modelVersion"]
            event_parameter_hash = prepared["parameterHash"]
            evidence_counts = {
                "regionCount": prepared["regionCount"],
                "renderRegionCount": prepared["renderRegionCount"],
                "g9TerminalChecksum": prepared["g9TerminalChecksum"],
                "cleanPlateChecksum": prepared["cleanPlateChecksum"],
                "routeChecksum": prepared["routeChecksum"],
                "styleChecksum": prepared["styleChecksum"],
            }
        else:
            parent_checksum = _latest_accepted_checksum(session, generation.id)
            event_input_checksum = parent_checksum
        expected_sequence = expected_sequences[item.image_id] + emitted_per_image.get(
            item.image_id, 0
        )
        _append_event(
            session,
            generation,
            operation=f"{job.kind}-job-enqueued",
            gate=_JOB_GATES.get(job.kind),
            state="pending",
            actor=actor,
            input_checksum=event_input_checksum,
            output_checksum=event_output_checksum,
            parent_checksum=parent_checksum,
            stage=_JOB_STAGES.get(job.kind),
            provider=provider,
            model_version=event_model_version,
            parameter_hash=event_parameter_hash,
            job_id=job.id,
            job_item_id=item.id,
            reason="job-enqueued",
            evidence={
                "eventType": "job-enqueued",
                "qualityState": "pending-review",
                "targetKind": (
                    "region-set"
                    if job.kind in {"detect", "ocr"}
                    else "region"
                    if item.region_id
                    else "image"
                ),
                **evidence_counts,
            },
            started_at=job.created_at,
            expected_sequence=expected_sequence,
        )
        emitted_per_image[item.image_id] = emitted_per_image.get(item.image_id, 0) + 1


def require_job_lineage_for_execution(
    store: ProjectStore, session: Session, job: Job
) -> dict[str, PageGeneration]:
    if job.lineage_context is not None:
        require_supported_lineage_job_kind(job.kind)
    target_image_ids = {item.image_id for item in job.items if item.image_id is not None}
    _normalized, generations = _validate_job_context(
        store,
        session,
        project_id=job.project_id,
        target_image_ids=target_image_ids,
        lineage=job.lineage_context,
    )
    return generations


def _region_state_checksum(session: Session, image_id: str) -> str:
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image_id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    payload = [
        {
            "id": row.id,
            "geometry": [row.x, row.y, row.width, row.height, row.rotation],
            "sourceText": row.source_text,
            "translationText": row.translation_text,
            "type": row.region_type,
            "direction": row.direction,
            "order": row.reading_order,
            "ignored": row.ignored,
            "confirmed": row.confirmed,
            "recognition": row.recognition,
            "repair": row.repair,
            "style": row.style,
            "revision": row.revision,
        }
        for row in rows
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_output_checksum(
    store: ProjectStore, session: Session, job: Job, item: JobItem
) -> str | None:
    if item.image_id is None:
        return None
    image = session.get(ImageAsset, item.image_id)
    if image is None:
        return None
    if job.kind == "detect":
        return g4_region_state_checksum(session, image.id)
    if job.kind == "ocr" and isinstance(job.lineage_context, dict):
        generation_id = _lineage_page_map(job.lineage_context).get(image.id)
        return (
            g6_ocr_state_checksum(session, image.id, generation_id)
            if generation_id is not None
            else None
        )
    if job.kind == "mask" and isinstance(job.lineage_context, dict):
        return session.scalar(
            select(PageLineageEvent.output_checksum).where(
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "mask-artifact-produced",
            )
        )
    if job.kind == "inpaint" and isinstance(job.lineage_context, dict):
        return session.scalar(
            select(PageLineageEvent.output_checksum).where(
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "clean-plate-candidate-produced",
            )
        )
    if job.kind == "translate" and isinstance(job.lineage_context, dict):
        return session.scalar(
            select(PageLineageEvent.output_checksum).where(
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "translation-candidates-produced",
            )
        )
    if job.kind == "typeset" and isinstance(job.lineage_context, dict):
        return session.scalar(
            select(PageLineageEvent.output_checksum).where(
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "typeset-candidate-produced",
            )
        )
    if job.kind in {"ocr", "translate"}:
        return _region_state_checksum(session, image.id)
    directory = {
        "preprocess": "preprocessed",
        "inpaint": "inpainted",
        "typeset": "typeset",
        "render": "typeset",
    }.get(job.kind)
    if directory is None:
        return None
    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    artifact = resolve_write_target(
        store.root,
        Path("generated") / directory / relative,
        protected_roots=(store.source_root,),
    )
    return _sha256_file(artifact) if artifact.is_file() else None


def record_job_item_finished(
    store: ProjectStore,
    session: Session,
    *,
    job: Job,
    item: JobItem,
    output: dict[str, Any] | None,
    error: Exception | None,
) -> None:
    lineage = job.lineage_context
    if not isinstance(lineage, dict) or item.image_id is None:
        return
    try:
        actor = _safe_actor(lineage.get("actor") if isinstance(lineage.get("actor"), dict) else {})
        generation_id = _lineage_page_map(lineage).get(item.image_id)
    except PageLineageConflict:
        return
    if generation_id is None:
        return
    generation = session.get(PageGeneration, generation_id)
    if generation is None or generation.image_id != item.image_id:
        return
    enqueued = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == f"{job.kind}-job-enqueued",
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    input_checksum = enqueued.input_checksum if enqueued is not None else None
    provider_value = (output or {}).get("provider")
    provider = (
        provider_value
        if isinstance(provider_value, str)
        and len(provider_value) <= 80
        and _OPAQUE_ID_RE.fullmatch(provider_value)
        else (enqueued.provider if enqueued is not None else None)
    )
    succeeded = error is None
    mask_completion_evidence: dict[str, Any] = {}
    output_checksum = _job_output_checksum(store, session, job, item) if succeeded else None
    if job.kind == "ocr":
        produced = session.scalar(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "ocr-attempts-produced",
            )
            .order_by(PageLineageEvent.sequence.desc())
            .limit(1)
        )
        if succeeded and (
            produced is None
            or output_checksum is None
            or produced.output_checksum != output_checksum
        ):
            raise PageLineageConflict(
                "Strict OCR cannot complete without matching attempt publication evidence",
                resource=f"job-item:{item.id}",
                reason="g6-publication-missing",
            )
        if not succeeded and produced is not None:
            raise PageLineageConflict(
                "A published OCR attempt set must recover to completion, not failure",
                resource=f"job-item:{item.id}",
                reason="g6-published-job-failed",
            )
    if job.kind == "mask":
        produced = session.scalar(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "mask-artifact-produced",
            )
            .order_by(PageLineageEvent.sequence.desc())
            .limit(1)
        )
        if succeeded and (
            produced is None
            or output_checksum is None
            or produced.output_checksum != output_checksum
        ):
            raise PageLineageConflict(
                "Strict mask cannot complete without matching artifact publication evidence",
                resource=f"job-item:{item.id}",
                reason="g7-publication-missing",
            )
        artifact = session.scalar(
            select(PageMaskArtifact).where(PageMaskArtifact.job_item_id == item.id)
        )
        if succeeded and (
            artifact is None
            or produced is None
            or produced.provider != artifact.provider
            or produced.model_version != artifact.model_version
            or produced.parameter_hash != artifact.parameter_hash
            or (produced.evidence or {}).get("artifactId") != artifact.id
            or (produced.evidence or {}).get("maskChecksum") != artifact.mask_checksum
            or (produced.evidence or {}).get("recipeChecksum") != artifact.draft_checksum
            or (produced.evidence or {}).get("qualityChecksum") != artifact.quality_checksum
            or (produced.evidence or {}).get("width") != artifact.width
            or (produced.evidence or {}).get("height") != artifact.height
            or (produced.evidence or {}).get("renderScale") != artifact.render_scale
            or (produced.evidence or {}).get("nonzeroPixelCount") != artifact.nonzero_pixels
            or (produced.evidence or {}).get("bbox") != artifact.bbox
        ):
            raise PageLineageConflict(
                "Published mask event does not match the immutable artifact row",
                resource=f"job-item:{item.id}",
                reason="g7-publication-mismatch",
            )
        if succeeded and artifact is not None:
            mask_completion_evidence = {
                key: (produced.evidence or {}).get(key)
                for key in (
                    "artifactId",
                    "maskChecksum",
                    "recipeChecksum",
                    "qualityChecksum",
                    "width",
                    "height",
                    "renderScale",
                    "nonzeroPixelCount",
                    "bbox",
                    "eligibleRegionCount",
                    "rubyRegionCount",
                    "rubyRegionIdsByPrimary",
                    "provider",
                    "modelVersion",
                    "parameterHash",
                )
            }
        if not succeeded and produced is not None:
            raise PageLineageConflict(
                "A published mask artifact must recover to completion, not failure",
                resource=f"job-item:{item.id}",
                reason="g7-published-job-failed",
            )
        if not succeeded and enqueued is not None:
            mask_completion_evidence = {
                key: (enqueued.evidence or {}).get(key)
                for key in (
                    "recipeChecksum",
                    "qualityChecksum",
                    "eligibleRegionCount",
                    "rubyRegionCount",
                    "rubyRegionIdsByPrimary",
                )
            }
            mask_completion_evidence.update(
                {
                    "provider": enqueued.provider,
                    "modelVersion": enqueued.model_version,
                    "parameterHash": enqueued.parameter_hash,
                }
            )
    if job.kind == "inpaint":
        from manga_localizer.services.clean_plates import clean_plate_completion_evidence

        completion = clean_plate_completion_evidence(
            store,
            session,
            job=job,
            item=item,
            succeeded=succeeded,
        )
        output_checksum = completion["outputChecksum"]
        mask_completion_evidence = completion["evidence"]
    if job.kind == "translate":
        from manga_localizer.services.translations import translation_completion_evidence

        completion = translation_completion_evidence(
            session, job=job, item=item, succeeded=succeeded
        )
        output_checksum = completion["outputChecksum"]
        mask_completion_evidence = completion["evidence"]
    if job.kind == "typeset":
        from manga_localizer.services.typesets import typeset_completion_evidence

        completion = typeset_completion_evidence(
            store, session, job=job, item=item, succeeded=succeeded
        )
        output_checksum = completion["outputChecksum"]
        mask_completion_evidence = completion["evidence"]
    output_model_version = (output or {}).get("modelVersion")
    model_version = (
        output_model_version
        if isinstance(output_model_version, str)
        and len(output_model_version) <= 128
        and _OPAQUE_ID_RE.fullmatch(output_model_version)
        else (enqueued.model_version if enqueued is not None else None)
    )
    output_parameter_hash = (output or {}).get("parameterHash")
    parameter_hash = (
        output_parameter_hash
        if isinstance(output_parameter_hash, str) and _SHA256_RE.fullmatch(output_parameter_hash)
        else (
            enqueued.parameter_hash
            if enqueued is not None and enqueued.parameter_hash is not None
            else generation.parameter_set_hash
        )
    )
    _append_event(
        session,
        generation,
        operation=f"{job.kind}-job-{'completed' if succeeded else 'failed'}",
        gate=_JOB_GATES.get(job.kind),
        state="pending" if succeeded else "blocked",
        actor=actor,
        input_checksum=input_checksum,
        output_checksum=output_checksum,
        parent_checksum=(enqueued.parent_checksum if enqueued is not None else input_checksum),
        stage=_JOB_STAGES.get(job.kind),
        provider=provider,
        model_version=model_version,
        parameter_hash=parameter_hash,
        job_id=job.id,
        job_item_id=item.id,
        decision=None,
        reason="review-required" if succeeded else "job-execution-failed",
        evidence={
            "eventType": "job-completed" if succeeded else "job-failed",
            "qualityState": "pending-review" if succeeded else "blocked",
            "targetKind": (
                "region-set" if job.kind == "detect" else "region" if item.region_id else "image"
            ),
            **(
                {
                    "ocrAttemptCount": int((output or {}).get("attemptCount", 0)),
                    "eligibleRegionCount": int((output or {}).get("count", 0)),
                }
                if job.kind == "ocr" and succeeded
                else {}
            ),
            **(
                mask_completion_evidence
                if job.kind in {"mask", "inpaint", "translate", "typeset"}
                else {}
            ),
        },
        started_at=item.started_at,
        finished_at=item.finished_at,
    )


def find_page_generation(
    registry: ProjectRegistry, generation_id: str
) -> tuple[ProjectStore, PageGeneration]:
    for store in registry.stores():
        with store.session() as session:
            generation = session.get(PageGeneration, generation_id)
            if generation is not None:
                return store, generation
    raise ProjectNotFound("Page generation was not found")
