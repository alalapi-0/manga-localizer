from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from manga_localizer.config import Settings
from manga_localizer.database import (
    ImageAsset,
    ImportBoundary,
    PageCleanPlateReview,
    PageCloudFullPageCandidate,
    PageCloudFullPageReview,
    PageGeneration,
    PageLineageEvent,
    PageMaskArtifact,
    PageMaskReview,
    PageTypesetReview,
)
from manga_localizer.security import UnsafePathError, atomic_write_bytes, safe_relative_path
from manga_localizer.services.clean_plates import require_current_clean_plate_acceptance
from manga_localizer.services.images import (
    require_current_accepted_stage_review,
    stage_reviews,
)
from manga_localizer.services.page_lineage import (
    create_final_review_repair_generation,
    final_review_repair_attempt_context,
    find_final_review_repair_generation,
    require_current_no_text_quality_plate,
    require_current_page_generation_identity,
    require_current_quality_plate,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectNotFound,
    ProjectRegistry,
    ProjectStore,
)
from manga_localizer.services.typesets import require_current_typeset_acceptance

ISSUE_CODES = frozenset(
    {
        "typesetting",
        "translation",
        "mask",
        "ai_inpaint",
        "missing_text",
        "preprocess",
        "other",
    }
)
VERDICTS = frozenset({"pending", "approved", "issues"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PARAMETER_SET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NO_STORE_HEADERS = {"Cache-Control": "private, no-store"}
_ARTIFACT_KINDS = ("original", "quality", "mask", "clean", "final")


class FinalReviewNotFound(ProjectNotFound):
    pass


class FinalReviewConflict(ProjectError):
    def __init__(
        self,
        message: str,
        *,
        expected_revision: int,
        actual_revision: int,
        item: dict[str, Any],
    ):
        super().__init__(message)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.item = item


class FinalReviewBatchConflict(ProjectError):
    def __init__(
        self,
        message: str,
        *,
        batch_id: str,
        expected_revision: int,
        actual_revision: int,
    ):
        super().__init__(message)
        self.batch_id = batch_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-.")
    return slug[:80] or "final-review"


def _sha256(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as error:
        raise ProjectError("A final-review artifact could not be read") from error


def _resolution(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            return opened.size
    except (OSError, ValueError) as error:
        raise ProjectError("A final-review artifact could not be decoded") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _actor_payload(actor: dict[str, Any]) -> dict[str, str | None]:
    kind = actor.get("actorKind")
    source = actor.get("operationSource")
    if kind not in {"codex", "cursor", "human", "system"} or source not in {
        "ui",
        "api",
        "script",
    }:
        raise ProjectError("Final-review actor provenance is invalid")
    result: dict[str, str | None] = {
        "actorKind": kind,
        "actorId": None,
        "taskId": None,
        "threadId": None,
        "sessionId": None,
        "operationSource": source,
    }
    for key in ("actorId", "taskId", "threadId", "sessionId"):
        value = actor.get(key)
        if value is not None:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or re.search(r"[/\\\x00\r\n]", value)
            ):
                raise ProjectError("Final-review actor provenance is invalid")
            result[key] = value
    if not any(result[key] for key in ("actorId", "taskId", "threadId", "sessionId")):
        raise ProjectError("Final-review actor provenance requires an identity anchor")
    return result


def _repair_handoff(row: sqlite3.Row) -> dict[str, Any] | None:
    if "repair_handoff_json" not in row.keys() or not row["repair_handoff_json"]:
        return None
    try:
        handoff = json.loads(row["repair_handoff_json"])
    except (json.JSONDecodeError, TypeError) as error:
        raise ProjectError("Final-review repair handoff metadata is invalid") from error
    if type(handoff) is not dict or set(handoff) != {
        "pageGenerationId",
        "repairImageId",
        "finalReviewItemRevision",
        "feedbackChecksum",
        "sourceRelativePath",
        "parameterSetId",
        "parameterSetHash",
    }:
        raise ProjectError("Final-review repair handoff metadata is invalid")
    for key in ("pageGenerationId", "repairImageId"):
        value = handoff[key]
        if (
            type(value) is not str
            or not value
            or len(value) > 128
            or re.search(r"[/\\\x00\r\n]", value)
        ):
            raise ProjectError("Final-review repair handoff metadata is invalid")
    if (
        type(handoff["finalReviewItemRevision"]) is not int
        or handoff["finalReviewItemRevision"] < 1
        or type(handoff["feedbackChecksum"]) is not str
        or not _SHA256_RE.fullmatch(handoff["feedbackChecksum"])
        or type(handoff["sourceRelativePath"]) is not str
        or type(handoff["parameterSetId"]) is not str
        or not _PARAMETER_SET_ID_RE.fullmatch(handoff["parameterSetId"])
        or type(handoff["parameterSetHash"]) is not str
        or not _SHA256_RE.fullmatch(handoff["parameterSetHash"])
    ):
        raise ProjectError("Final-review repair handoff metadata is invalid")
    safe_relative_path(handoff["sourceRelativePath"])
    return handoff


def _repair_source_relative_path(row: sqlite3.Row) -> str | None:
    handoff = _repair_handoff(row)
    if handoff is not None:
        return str(handoff["sourceRelativePath"])
    if "strict_evidence" in row.keys() and bool(row["strict_evidence"]):
        return str(row["source_relative_path"])
    return None


def _reject_symlink_components(path: Path) -> None:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise UnsafePathError("The selected path must be absolute")
    cursor = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            raise UnsafePathError("The selected path must not contain symlinks")


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _source_boundaries(projects: ProjectRegistry) -> list[Path]:
    boundaries: list[Path] = []
    for store in projects.stores():
        boundaries.append(store.root.resolve())
        with store.session() as session:
            for raw in session.scalars(select(ImportBoundary.path)).all():
                try:
                    boundaries.append(Path(raw).expanduser().resolve(strict=True))
                except (OSError, RuntimeError):
                    continue
    return boundaries


def _validate_external_target(
    raw: Path,
    *,
    projects: ProjectRegistry,
    review_roots: list[Path],
    allow_data_reviews_parent: Path | None = None,
) -> Path:
    _reject_symlink_components(raw)
    target = raw.expanduser().resolve(strict=False)
    protected = _source_boundaries(projects) + [root.resolve() for root in review_roots]
    if allow_data_reviews_parent is not None:
        allowed = allow_data_reviews_parent.resolve()
        protected = [root for root in protected if root != allowed]
    if any(_overlaps(target, boundary) for boundary in protected):
        raise UnsafePathError("The selected path overlaps project, import, or review storage")
    return target


def _ensure_new_target(target: Path) -> None:
    if target.exists():
        raise ProjectError("The selected output path already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.is_dir():
        raise ProjectError("The selected output parent is not a directory")


def _snapshot_thumbnail(source: Path, target: Path, size: int) -> None:
    try:
        with Image.open(source) as opened:
            opened.verify()
        with Image.open(source) as opened:
            thumbnail = opened.convert("RGB")
            thumbnail.thumbnail((size, size), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            thumbnail.save(output, format="JPEG", quality=84, optimize=True)
    except (OSError, ValueError) as error:
        raise ProjectError("A selected final image could not be decoded") from error
    atomic_write_bytes(target, output.getvalue())


def _freeze_evidence_bundle(
    root: Path,
    item_id: str,
    artifact_revision: int,
    descriptors: dict[str, dict[str, Any]],
    thumbnail_size: int,
) -> tuple[dict[str, dict[str, Any]], str, str]:
    relative_root = Path("images") / item_id / f"r{artifact_revision:06d}"
    destination_root = root / relative_root
    if destination_root.exists():
        raise ProjectError("Final-review artifact revision already exists")
    temporary = destination_root.with_name(f".{destination_root.name}.{uuid.uuid4().hex}.tmp")
    sanitized: dict[str, dict[str, Any]] = {}
    try:
        temporary.mkdir(parents=True)
        for kind in _ARTIFACT_KINDS:
            descriptor = dict(descriptors[kind])
            source_value = descriptor.pop("path", None)
            if descriptor["availability"] == "available":
                source = Path(str(source_value)).resolve(strict=True)
                suffix = source.suffix.lower() if kind == "original" else ".png"
                if suffix not in {
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                    ".tif",
                    ".tiff",
                    ".bmp",
                    ".gif",
                }:
                    suffix = ".png"
                relative = relative_root / f"{kind}{suffix}"
                target = temporary / f"{kind}{suffix}"
                shutil.copyfile(source, target)
                if _sha256(target) != descriptor["checksum"] or _resolution(target) != (
                    descriptor["grid"]["width"],
                    descriptor["grid"]["height"],
                ):
                    raise ProjectError("A frozen final-review evidence copy is inconsistent")
                descriptor["relativePath"] = relative.as_posix()
            else:
                descriptor["relativePath"] = None
            sanitized[kind] = descriptor
        final_source = Path(str(descriptors["final"]["path"])).resolve(strict=True)
        thumbnail_relative = relative_root / "thumbnail.jpg"
        _snapshot_thumbnail(final_source, temporary / "thumbnail.jpg", thumbnail_size)
        thumbnail_checksum = _sha256(temporary / "thumbnail.jpg")
        os.replace(temporary, destination_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(destination_root, ignore_errors=True)
        raise
    return sanitized, thumbnail_relative.as_posix(), thumbnail_checksum


def _legacy_frozen_evidence(root: Path, row: sqlite3.Row) -> dict[str, dict[str, Any]]:
    final_path = (root / safe_relative_path(row["snapshot_path"])).resolve(strict=True)
    width, height = _resolution(final_path)
    grid = {"width": width, "height": height}
    return {
        kind: {
            "kind": kind,
            "availability": "available" if kind == "final" else "unavailable",
            "artifactRevision": 1,
            "generationId": None,
            "producerId": None,
            "producerRevisionId": None,
            "terminalId": None,
            "terminalChecksum": None,
            "terminalRevisionId": None,
            "checksum": row["artifact_checksum"] if kind == "final" else None,
            "grid": grid if kind == "final" else None,
            "resolutionDigest": _digest(grid) if kind == "final" else None,
            "relativePath": row["snapshot_path"] if kind == "final" else None,
        }
        for kind in _ARTIFACT_KINDS
    }


def _artifact_for_image(
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
) -> tuple[Path, str, str]:
    lineaged_quality = require_current_no_text_quality_plate(store, session, image)
    if lineaged_quality is not None:
        if lineaged_quality["targetKind"] != "preprocessed":
            raise ProjectError("This quality-plate variant is not supported by final review yet")
        return lineaged_quality["path"], "preprocess", lineaged_quality["checksum"]
    relative = safe_relative_path(image.relative_path).with_suffix(".png")
    if image.status.get("typeset") == "done":
        try:
            checksums = require_current_accepted_stage_review(store, image, "typeset")
        except ProjectError:
            pass
        else:
            path = store.root / "generated" / "typeset" / relative
            return path, "typeset", checksums["artifactChecksum"]
    if image.status.get("reviewState") != "no-text-reviewed":
        raise ProjectError(
            "Every final-review item must have an accepted typeset result or an accepted "
            "no-text preprocess result"
        )
    checksums = require_current_accepted_stage_review(store, image, "preprocess")
    path = store.root / "generated" / "preprocessed" / relative
    return path, "preprocess", checksums["artifactChecksum"]


def _evidence_descriptor(
    kind: str,
    *,
    path: Path | None,
    availability: str,
    generation_id: str | None,
    producer_id: str | None,
    terminal_id: str | None,
    artifact_revision: int,
    terminal_checksum: str | None = None,
    producer_revision_id: str | None = None,
    terminal_revision_id: str | None = None,
) -> dict[str, Any]:
    if availability != "available":
        return {
            "kind": kind,
            "availability": availability,
            "artifactRevision": artifact_revision,
            "generationId": generation_id,
            "producerId": producer_id,
            "terminalId": terminal_id,
            "terminalChecksum": terminal_checksum,
            "producerRevisionId": producer_revision_id,
            "terminalRevisionId": terminal_revision_id,
            "checksum": None,
            "grid": None,
            "resolutionDigest": None,
            "path": None,
        }
    if path is None:
        raise ProjectError("Available final-review evidence has no artifact")
    width, height = _resolution(path)
    checksum = _sha256(path)
    grid = {"width": width, "height": height}
    return {
        "kind": kind,
        "availability": "available",
        "artifactRevision": artifact_revision,
        "generationId": generation_id,
        "producerId": producer_id,
        "terminalId": terminal_id,
        "terminalChecksum": terminal_checksum,
        "producerRevisionId": producer_revision_id,
        "terminalRevisionId": terminal_revision_id,
        "checksum": checksum,
        "grid": grid,
        "resolutionDigest": _digest(grid),
        # Source-local paths are used only during the copy transaction and are
        # removed before the descriptor enters the review database.
        "path": str(path),
    }


def _strict_artifacts_for_image(
    projects: ProjectRegistry,
    store: ProjectStore,
    session: Session,
    image: ImageAsset,
    *,
    artifact_revision: int,
) -> tuple[str, dict[str, dict[str, Any]]]:
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.project_id == image.project_id,
            PageGeneration.image_id == image.id,
            PageGeneration.state == "active",
        )
    )
    if generation is None:
        raise ProjectError("Strict final review requires an active page generation")
    identity_event = require_current_page_generation_identity(
        projects, store, session, image, generation
    )
    source_path = (store.root / safe_relative_path(image.source_path)).resolve(strict=True)
    if (
        not source_path.is_relative_to(store.source_root.resolve())
        or _sha256(source_path) != image.checksum
    ):
        raise ProjectError("Strict final-review immutable source identity is inconsistent")
    quality = require_current_quality_plate(store, session, image, generation)
    quality_event = session.scalar(
        select(PageLineageEvent).where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.sequence == quality["eventSequence"],
        )
    )
    if (
        quality_event is None
        or quality_event.gate != "G2_reconstruction"
        or quality_event.operation != "reconstruction-decision"
        or quality_event.state != "accepted"
        or quality_event.revision_id is None
        or quality_event.output_checksum != quality["checksum"]
        or (quality_event.evidence or {}).get("targetKind") != quality["targetKind"]
    ):
        raise ProjectError("Strict final-review quality event is unavailable")
    quality_producer_event = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.gate == "G1_baselineUpscale",
            PageLineageEvent.operation == "preprocess-artifact-produced",
            PageLineageEvent.output_checksum == quality["checksum"],
            PageLineageEvent.sequence < quality_event.sequence,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if (
        quality_producer_event is None
        or quality_producer_event.state != "pending"
        or quality_producer_event.input_checksum != generation.source_checksum
        or quality_producer_event.parent_checksum != generation.source_checksum
        or quality_producer_event.job_id is None
        or quality_producer_event.job_item_id is None
        or quality_producer_event.revision_id is not None
        or (quality_producer_event.evidence or {}).get("targetKind") != "image"
    ):
        raise ProjectError("Strict final-review quality publication event is unavailable")
    quality_completion_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.operation == "preprocess-job-completed",
                PageLineageEvent.job_id == quality_producer_event.job_id,
                PageLineageEvent.job_item_id == quality_producer_event.job_item_id,
                PageLineageEvent.output_checksum == quality["checksum"],
                PageLineageEvent.sequence > quality_producer_event.sequence,
                PageLineageEvent.sequence < quality_event.sequence,
            )
        ).all()
    )
    if len(quality_completion_events) != 1:
        raise ProjectError("Strict final-review quality completion event is unavailable")
    text_presence = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.gate == "G3_textPresence",
            PageLineageEvent.state == "accepted",
        )
        .order_by(PageLineageEvent.sequence.desc())
    )
    no_text = (
        require_current_no_text_quality_plate(store, session, image)
        if text_presence is not None and text_presence.decision == "no-text"
        else None
    )
    original = _evidence_descriptor(
        "original",
        path=source_path,
        availability="available",
        generation_id=generation.id,
        producer_id=identity_event.id,
        producer_revision_id=identity_event.revision_id,
        terminal_id=identity_event.id,
        terminal_checksum=identity_event.output_checksum,
        terminal_revision_id=identity_event.revision_id,
        artifact_revision=artifact_revision,
    )
    quality_descriptor = _evidence_descriptor(
        "quality",
        path=quality["path"],
        availability="available",
        generation_id=generation.id,
        producer_id=quality_producer_event.id,
        producer_revision_id=quality_producer_event.revision_id,
        terminal_id=quality_event.id,
        terminal_checksum=quality_event.output_checksum,
        terminal_revision_id=quality_event.revision_id,
        artifact_revision=artifact_revision,
    )
    if no_text is not None:
        no_text_event = session.scalar(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G3_textPresence",
                PageLineageEvent.decision == "no-text",
            )
            .order_by(PageLineageEvent.sequence.desc())
        )
        if (
            no_text_event is None
            or no_text_event.operation != "text-presence-decision"
            or no_text_event.state != "accepted"
            or no_text_event.revision_id is None
            or no_text_event.input_checksum != quality["checksum"]
            or no_text_event.output_checksum != quality["checksum"]
            or no_text_event.parent_checksum != quality["checksum"]
        ):
            raise ProjectError("Strict final-review no-text terminal is unavailable")
        na = {
            kind: _evidence_descriptor(
                kind,
                path=None,
                availability="not-applicable",
                generation_id=generation.id,
                producer_id=None,
                terminal_id=no_text_event.id,
                terminal_checksum=no_text_event.output_checksum,
                terminal_revision_id=no_text_event.revision_id,
                artifact_revision=artifact_revision,
            )
            for kind in ("mask", "clean")
        }
        final = _evidence_descriptor(
            "final",
            path=quality["path"],
            availability="available",
            generation_id=generation.id,
            producer_id=quality_producer_event.id,
            producer_revision_id=quality_producer_event.revision_id,
            terminal_id=no_text_event.id,
            terminal_checksum=no_text_event.output_checksum,
            terminal_revision_id=no_text_event.revision_id,
            artifact_revision=artifact_revision,
        )
        return "preprocess", {
            "original": original,
            "quality": quality_descriptor,
            **na,
            "final": final,
        }
    g8_terminal, clean_path, clean_candidate = require_current_clean_plate_acceptance(
        store, session, image, generation
    )
    cloud_route = isinstance(clean_candidate, PageCloudFullPageCandidate)
    review_model = PageCloudFullPageReview if cloud_route else PageCleanPlateReview
    clean_review = session.scalar(
        select(review_model)
        .where(review_model.generation_id == generation.id)
        .order_by(review_model.sequence.desc())
    )
    if clean_review is None:
        raise ProjectError("Strict final-review clean terminal is unavailable")
    clean_terminal_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == ("G8_cloudFullPage" if cloud_route else "G8_cleanPlate"),
                PageLineageEvent.operation
                == ("cloud-full-page-stage-review" if cloud_route else "clean-plate-stage-review"),
                PageLineageEvent.state == clean_review.state,
            )
        ).all()
    )
    clean_terminal_matches = [
        event
        for event in clean_terminal_events
        if (event.evidence or {}).get("candidateId") == clean_review.candidate_id
        and (event.evidence or {}).get("candidateChecksum") == clean_review.candidate_checksum
    ]
    clean_terminal_event = clean_terminal_matches[0] if len(clean_terminal_matches) == 1 else None
    if (
        clean_terminal_event is None
        or clean_terminal_event.state != clean_review.state
        or clean_terminal_event.revision_id is None
        or clean_terminal_event.output_checksum is None
        or clean_terminal_event.output_checksum != g8_terminal
        or (clean_terminal_event.evidence or {}).get("candidateId") != clean_review.candidate_id
        or (clean_terminal_event.evidence or {}).get("candidateChecksum")
        != clean_review.candidate_checksum
    ):
        raise ProjectError("Strict final-review clean terminal event is unavailable")
    mask_review = session.scalar(
        select(PageMaskReview)
        .where(PageMaskReview.generation_id == generation.id)
        .order_by(PageMaskReview.sequence.desc())
    )
    if mask_review is None:
        raise ProjectError("Strict final-review mask terminal event is unavailable")
    mask_terminal_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G7_mask",
                PageLineageEvent.operation == "mask-stage-review",
                PageLineageEvent.state == mask_review.state,
            )
        ).all()
    )
    mask_terminal_matches = [
        event
        for event in mask_terminal_events
        if (event.evidence or {}).get("artifactId") == mask_review.artifact_id
        and (event.evidence or {}).get("maskChecksum") == mask_review.mask_checksum
    ]
    mask_terminal_event = mask_terminal_matches[0] if len(mask_terminal_matches) == 1 else None
    if (
        mask_terminal_event is None
        or mask_terminal_event.state != mask_review.state
        or mask_terminal_event.revision_id is None
        or mask_terminal_event.output_checksum is None
        or mask_terminal_event.output_checksum != clean_terminal_event.parent_checksum
        or (clean_terminal_event.evidence or {}).get("g7Checksum")
        != mask_terminal_event.output_checksum
        or (mask_terminal_event.evidence or {}).get("artifactId") != mask_review.artifact_id
        or (mask_terminal_event.evidence or {}).get("maskChecksum") != mask_review.mask_checksum
    ):
        raise ProjectError("Strict final-review mask terminal event is unavailable")
    g10_terminal, final_path, candidate = require_current_typeset_acceptance(
        store, session, image, generation
    )
    typeset_review = session.scalar(
        select(PageTypesetReview).where(
            PageTypesetReview.generation_id == generation.id,
            PageTypesetReview.candidate_id == candidate.id,
        )
    )
    if typeset_review is None:
        raise ProjectError("Strict final-review typeset terminal is unavailable")
    typeset_terminal_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G10_typeset",
                PageLineageEvent.operation == "typeset-candidate-reviewed",
                PageLineageEvent.sequence == typeset_review.sequence,
                PageLineageEvent.revision_id == typeset_review.revision_id,
            )
        ).all()
    )
    if len(typeset_terminal_events) != 1:
        raise ProjectError("Strict final-review typeset terminal event is unavailable")
    typeset_terminal_event = typeset_terminal_events[0]
    typeset_terminal_evidence = typeset_terminal_event.evidence or {}
    if (
        typeset_review.state != "accepted"
        or typeset_terminal_event.state != "accepted"
        or typeset_terminal_event.decision != "candidate-accepted"
        or typeset_terminal_event.output_checksum != g10_terminal
        or typeset_terminal_event.output_checksum != typeset_review.terminal_checksum
        or typeset_terminal_evidence.get("candidateId") != candidate.id
        or typeset_terminal_evidence.get("candidateChecksum") != candidate.candidate_checksum
    ):
        raise ProjectError("Strict final-review typeset terminal event is unavailable")
    typeset_producer_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G10_typeset",
                PageLineageEvent.operation == "typeset-candidate-produced",
                PageLineageEvent.job_id == candidate.job_id,
                PageLineageEvent.job_item_id == candidate.job_item_id,
            )
        ).all()
    )
    if len(typeset_producer_events) != 1:
        raise ProjectError("Strict final-review typeset publication event is unavailable")
    typeset_producer_event = typeset_producer_events[0]
    typeset_producer_evidence = typeset_producer_event.evidence or {}
    if (
        typeset_producer_event.state != "pending"
        or typeset_producer_event.revision_id != candidate.revision_id
        or not isinstance(typeset_producer_event.output_checksum, str)
        or not _SHA256_RE.fullmatch(typeset_producer_event.output_checksum)
        or typeset_producer_event.sequence >= typeset_terminal_event.sequence
        or typeset_producer_event.output_checksum != typeset_terminal_event.input_checksum
        or typeset_producer_evidence.get("candidateId") != candidate.id
        or typeset_producer_evidence.get("candidateChecksum") != candidate.candidate_checksum
    ):
        raise ProjectError("Strict final-review typeset publication event is unavailable")
    if clean_candidate is None:
        return "typeset", {
            "original": original,
            "quality": quality_descriptor,
            "mask": _evidence_descriptor(
                "mask",
                path=None,
                availability="not-applicable",
                generation_id=generation.id,
                producer_id=None,
                terminal_id=mask_terminal_event.id,
                terminal_checksum=mask_terminal_event.output_checksum,
                producer_revision_id=None,
                terminal_revision_id=mask_terminal_event.revision_id,
                artifact_revision=artifact_revision,
            ),
            "clean": _evidence_descriptor(
                "clean",
                path=None,
                availability="not-applicable",
                generation_id=generation.id,
                producer_id=None,
                terminal_id=clean_terminal_event.id,
                terminal_checksum=clean_terminal_event.output_checksum,
                producer_revision_id=None,
                terminal_revision_id=clean_terminal_event.revision_id,
                artifact_revision=artifact_revision,
            ),
            "final": _evidence_descriptor(
                "final",
                path=final_path,
                availability="available",
                generation_id=generation.id,
                producer_id=typeset_producer_event.id,
                terminal_id=typeset_terminal_event.id,
                terminal_checksum=typeset_terminal_event.output_checksum,
                producer_revision_id=typeset_producer_event.revision_id,
                terminal_revision_id=typeset_terminal_event.revision_id,
                artifact_revision=artifact_revision,
            ),
        }
    mask = session.get(PageMaskArtifact, clean_candidate.mask_artifact_id)
    if mask is None or mask.generation_id != generation.id:
        raise ProjectError("Strict final-review mask evidence is unavailable")
    mask_path = (store.root / safe_relative_path(mask.relative_path)).resolve(strict=True)
    mask_producer_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G7_mask",
                PageLineageEvent.operation == "mask-artifact-produced",
                PageLineageEvent.job_id == mask.job_id,
                PageLineageEvent.job_item_id == mask.job_item_id,
            )
        ).all()
    )
    mask_producer_event = mask_producer_events[0] if len(mask_producer_events) == 1 else None
    if (
        mask_review.state != "accepted"
        or mask_review.artifact_id != mask.id
        or mask_review.mask_checksum != mask.mask_checksum
        or mask_terminal_event.state != "accepted"
        or mask_producer_event is None
        or mask_producer_event.revision_id is None
        or not isinstance(mask_producer_event.output_checksum, str)
        or not _SHA256_RE.fullmatch(mask_producer_event.output_checksum)
        or mask_producer_event.sequence >= mask_terminal_event.sequence
        or mask_producer_event.output_checksum != mask_terminal_event.input_checksum
        or (mask_producer_event.evidence or {}).get("artifactId") != mask.id
        or (mask_producer_event.evidence or {}).get("maskChecksum") != mask.mask_checksum
    ):
        raise ProjectError("Strict final-review mask publication event is unavailable")
    clean_producer_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == ("G8_cloudFullPage" if cloud_route else "G8_cleanPlate"),
                PageLineageEvent.operation
                == (
                    "cloud-full-page-candidate-produced"
                    if cloud_route
                    else "clean-plate-candidate-produced"
                ),
                PageLineageEvent.job_id == clean_candidate.job_id,
                PageLineageEvent.job_item_id == clean_candidate.job_item_id,
            )
        ).all()
    )
    if len(clean_producer_events) != 1:
        raise ProjectError("Strict final-review clean publication event is unavailable")
    clean_producer_event = clean_producer_events[0]
    clean_producer_evidence = clean_producer_event.evidence or {}
    clean_producer_checksum_key = "normalizedChecksum" if cloud_route else "candidateChecksum"
    if (
        clean_producer_event.state != "pending"
        or clean_producer_event.revision_id is None
        or not isinstance(clean_producer_event.output_checksum, str)
        or not _SHA256_RE.fullmatch(clean_producer_event.output_checksum)
        or clean_producer_event.sequence >= clean_terminal_event.sequence
        or clean_producer_event.output_checksum != clean_terminal_event.input_checksum
        or clean_producer_evidence.get("candidateId") != clean_candidate.id
        or clean_producer_evidence.get(clean_producer_checksum_key)
        != clean_candidate.candidate_checksum
    ):
        raise ProjectError("Strict final-review clean publication event is unavailable")
    descriptors = {
        "original": original,
        "quality": quality_descriptor,
        "mask": _evidence_descriptor(
            "mask",
            path=mask_path,
            availability="available",
            generation_id=generation.id,
            producer_id=mask_producer_event.id,
            producer_revision_id=mask_producer_event.revision_id,
            terminal_id=mask_terminal_event.id,
            terminal_checksum=mask_terminal_event.output_checksum,
            terminal_revision_id=mask_terminal_event.revision_id,
            artifact_revision=artifact_revision,
        ),
        "clean": _evidence_descriptor(
            "clean",
            path=clean_path,
            availability="available",
            generation_id=generation.id,
            producer_id=clean_producer_event.id,
            terminal_id=clean_terminal_event.id,
            terminal_checksum=clean_terminal_event.output_checksum,
            producer_revision_id=clean_producer_event.revision_id,
            terminal_revision_id=clean_terminal_event.revision_id,
            artifact_revision=artifact_revision,
        ),
        "final": _evidence_descriptor(
            "final",
            path=final_path,
            availability="available",
            generation_id=generation.id,
            producer_id=typeset_producer_event.id,
            terminal_id=typeset_terminal_event.id,
            terminal_checksum=typeset_terminal_event.output_checksum,
            producer_revision_id=typeset_producer_event.revision_id,
            terminal_revision_id=typeset_terminal_event.revision_id,
            artifact_revision=artifact_revision,
        ),
    }
    return "typeset", descriptors


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _create_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
                ,format_version INTEGER NOT NULL DEFAULT 2
            );
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                source_project_id TEXT NOT NULL,
                source_image_id TEXT NOT NULL,
                source_project_name TEXT NOT NULL,
                source_relative_path TEXT NOT NULL,
                final_variant TEXT NOT NULL CHECK(final_variant IN ('typeset', 'preprocess')),
                artifact_checksum TEXT NOT NULL,
                thumbnail_checksum TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                thumbnail_path TEXT NOT NULL,
                verdict TEXT NOT NULL DEFAULT 'pending',
                issue_codes TEXT NOT NULL DEFAULT '[]',
                feedback TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                artifact_revision INTEGER NOT NULL DEFAULT 1,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                strict_evidence INTEGER NOT NULL DEFAULT 1,
                evidence_digest TEXT,
                repair_handoff_json TEXT,
                UNIQUE(batch_id, position),
                UNIQUE(batch_id, source_project_id, source_image_id)
            );
            CREATE TABLE revisions (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                item_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX revisions_item_idx ON revisions(item_id, created_at);
            CREATE TABLE artifact_revisions (
                item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                artifact_revision INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                thumbnail_path TEXT NOT NULL,
                thumbnail_checksum TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(item_id, artifact_revision)
            );
            CREATE TRIGGER artifact_revisions_no_update BEFORE UPDATE ON artifact_revisions
            BEGIN SELECT RAISE(ABORT, 'artifact revisions are append-only'); END;
            CREATE TRIGGER artifact_revisions_no_delete BEFORE DELETE ON artifact_revisions
            BEGIN SELECT RAISE(ABORT, 'artifact revisions are append-only'); END;
            CREATE TRIGGER final_review_revisions_no_update BEFORE UPDATE ON revisions
            BEGIN SELECT RAISE(ABORT, 'final review history is append-only'); END;
            CREATE TRIGGER final_review_revisions_no_delete BEFORE DELETE ON revisions
            BEGIN SELECT RAISE(ABORT, 'final review history is append-only'); END;
            """
        )


def _migrate_database(path: Path, *, connection: sqlite3.Connection | None = None) -> None:
    """Add v2 columns without rewriting any legacy review bytes or decisions."""
    owned = connection is None
    connection = connection or _connect(path)
    try:
        batch_columns = {row["name"] for row in connection.execute("PRAGMA table_info(batches)")}
        item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
        if "format_version" not in batch_columns:
            connection.execute(
                "ALTER TABLE batches ADD COLUMN format_version INTEGER NOT NULL DEFAULT 1"
            )
        additions = {
            "artifact_revision": "INTEGER NOT NULL DEFAULT 1",
            "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "strict_evidence": "INTEGER NOT NULL DEFAULT 0",
            "evidence_digest": "TEXT",
            "repair_handoff_json": "TEXT",
        }
        for name, definition in additions.items():
            if name not in item_columns:
                connection.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS artifact_revisions (
                item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                artifact_revision INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_digest TEXT,
                thumbnail_path TEXT NOT NULL,
                thumbnail_checksum TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(item_id, artifact_revision)
            )
            """
        )
        artifact_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(artifact_revisions)")
        }
        if "thumbnail_path" not in artifact_columns:
            connection.execute("ALTER TABLE artifact_revisions ADD COLUMN thumbnail_path TEXT")
        if "thumbnail_checksum" not in artifact_columns:
            connection.execute("ALTER TABLE artifact_revisions ADD COLUMN thumbnail_checksum TEXT")
        if "evidence_digest" not in artifact_columns:
            connection.execute("ALTER TABLE artifact_revisions ADD COLUMN evidence_digest TEXT")
        for statement in (
            """CREATE TRIGGER IF NOT EXISTS artifact_revisions_no_update
            BEFORE UPDATE ON artifact_revisions
            BEGIN SELECT RAISE(ABORT, 'artifact revisions are append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS artifact_revisions_no_delete
            BEFORE DELETE ON artifact_revisions
            BEGIN SELECT RAISE(ABORT, 'artifact revisions are append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS final_review_revisions_no_update
            BEFORE UPDATE ON revisions
            BEGIN SELECT RAISE(ABORT, 'final review history is append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS final_review_revisions_no_delete
            BEFORE DELETE ON revisions
            BEGIN SELECT RAISE(ABORT, 'final review history is append-only'); END""",
        ):
            connection.execute(statement)
    finally:
        if owned:
            connection.close()


@dataclass
class FinalReviewStore:
    root: Path
    projects: ProjectRegistry
    thumbnail_size: int

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.lock = threading.RLock()

    @property
    def database_path(self) -> Path:
        return self.root / "final-review" / "final-review.sqlite3"

    @property
    def manifest_path(self) -> Path:
        return self.root / "final-review" / "manifest.json"

    def _item_row(self, item_id: str) -> sqlite3.Row:
        with _connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise FinalReviewNotFound(f"Final-review item {item_id} was not found")
        return row

    def batch(self, *, include_items: bool = False) -> dict[str, Any]:
        with self.lock, _connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM batches LIMIT 1").fetchone()
            if row is None:
                raise FinalReviewNotFound("Final-review batch database is empty")
            counts = {
                verdict: connection.execute(
                    "SELECT COUNT(*) FROM items WHERE verdict = ?", (verdict,)
                ).fetchone()[0]
                for verdict in ("pending", "approved", "issues")
            }
            if sum(counts.values()) != row["item_count"]:
                raise ProjectError("Final-review batch item count is inconsistent")
            result = {
                "id": row["id"],
                "name": row["name"],
                "rootPath": str(self.root),
                "manifestPath": str(self.manifest_path),
                "itemCount": row["item_count"],
                "counts": counts,
                "revision": row["revision"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "formatVersion": row["format_version"] if "format_version" in row.keys() else 1,
            }
            if include_items:
                rows = connection.execute("SELECT * FROM items ORDER BY position").fetchall()
                result["items"] = [self._public_item(item) for item in rows]
            return result

    def items(self) -> list[dict[str, Any]]:
        with self.lock, _connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM items ORDER BY position").fetchall()
        return [self._public_item(row) for row in rows]

    def item(self, item_id: str) -> dict[str, Any]:
        return self._public_item(self._item_row(item_id))

    def _current_stale(self, row: sqlite3.Row) -> bool:
        try:
            store = self.projects.get(row["source_project_id"])
            with store.session() as session:
                strict = bool(row["strict_evidence"]) if "strict_evidence" in row.keys() else False
                if strict:
                    frozen = json.loads(row["evidence_json"])["final"]
                    issue_repair_candidate = row["verdict"] == "issues"
                    if issue_repair_candidate:
                        feedback_checksum = _digest(
                            {
                                "issueCodes": sorted(json.loads(row["issue_codes"])),
                                "feedback": row["feedback"],
                            }
                        )
                        repair = find_final_review_repair_generation(
                            store,
                            session,
                            source_project_id=row["source_project_id"],
                            source_image_id=row["source_image_id"],
                            source_relative_path=_repair_source_relative_path(row),
                            final_review_item_id=row["id"],
                            final_review_item_revision=row["revision"],
                            feedback_checksum=feedback_checksum,
                        )
                        if repair is None:
                            return False
                        image, generation = repair
                    else:
                        handoff = _repair_handoff(row)
                        if handoff is not None:
                            repair = find_final_review_repair_generation(
                                store,
                                session,
                                source_project_id=row["source_project_id"],
                                source_image_id=row["source_image_id"],
                                source_relative_path=handoff["sourceRelativePath"],
                                final_review_item_id=row["id"],
                                final_review_item_revision=handoff["finalReviewItemRevision"],
                                feedback_checksum=handoff["feedbackChecksum"],
                                parameter_set_id=handoff["parameterSetId"],
                                parameter_set_hash=handoff["parameterSetHash"],
                            )
                            if repair is None:
                                return True
                            image, generation = repair
                            if (
                                generation.id != handoff["pageGenerationId"]
                                or image.id != handoff["repairImageId"]
                            ):
                                return True
                        else:
                            generation_id = frozen.get("generationId")
                            generation = session.get(PageGeneration, generation_id)
                            image = (
                                session.get(ImageAsset, generation.image_id)
                                if generation is not None
                                else None
                            )
                    if image is None:
                        return True
                    if issue_repair_candidate:
                        ready = False
                        try:
                            require_current_typeset_acceptance(store, session, image, generation)
                            ready = True
                        except ProjectError:
                            try:
                                ready = (
                                    require_current_no_text_quality_plate(store, session, image)
                                    is not None
                                )
                            except ProjectError:
                                ready = False
                        if not ready:
                            return False
                    variant, evidence = _strict_artifacts_for_image(
                        self.projects,
                        store,
                        session,
                        image,
                        artifact_revision=row["artifact_revision"],
                    )
                    return (
                        variant != row["final_variant"]
                        or evidence["final"]["checksum"] != row["artifact_checksum"]
                        or evidence["final"]["generationId"] != frozen.get("generationId")
                    )
                image = session.get(ImageAsset, row["source_image_id"])
                if image is None:
                    return True
                lineaged_quality = require_current_no_text_quality_plate(store, session, image)
                if lineaged_quality is not None:
                    if lineaged_quality["targetKind"] != "preprocessed":
                        return True
                    current_variant = "preprocess"
                    current_checksum = lineaged_quality["checksum"]
                else:
                    reviews = stage_reviews(image)
                    typeset_review = reviews.get("typeset")
                    if (
                        image.status.get("typeset") == "done"
                        and typeset_review is not None
                        and typeset_review.get("state") == "accepted"
                    ):
                        current_variant = "typeset"
                        current_checksum = typeset_review.get("artifactChecksum")
                    else:
                        preprocess_review = reviews.get("preprocess")
                        if (
                            image.status.get("reviewState") != "no-text-reviewed"
                            or image.status.get("preprocess") != "done"
                            or preprocess_review is None
                            or preprocess_review.get("state") != "accepted"
                        ):
                            return True
                        current_variant = "preprocess"
                        current_checksum = preprocess_review.get("artifactChecksum")
                return (
                    current_variant != row["final_variant"]
                    or current_checksum != row["artifact_checksum"]
                )
        except ProjectError:
            return True

    def _public_item(self, row: sqlite3.Row) -> dict[str, Any]:
        with _connect(self.database_path) as connection:
            batch_row = connection.execute(
                "SELECT * FROM batches WHERE id = ?", (row["batch_id"],)
            ).fetchone()
        format_version = (
            batch_row["format_version"]
            if batch_row is not None and "format_version" in batch_row.keys()
            else 1
        )
        artifact_revision = row["artifact_revision"] if "artifact_revision" in row.keys() else 1
        strict = bool(row["strict_evidence"]) if "strict_evidence" in row.keys() else False
        if strict:
            evidence = json.loads(row["evidence_json"])
            if "evidence_digest" not in row.keys() or row["evidence_digest"] != _digest(evidence):
                raise ProjectError("Final-review evidence metadata digest is inconsistent")
        else:
            evidence = {
                kind: {
                    "kind": kind,
                    "availability": "unavailable" if kind != "final" else "available",
                    "artifactRevision": artifact_revision,
                    "generationId": None,
                    "producerId": None,
                    "producerRevisionId": None,
                    "terminalId": None,
                    "terminalChecksum": None,
                    "terminalRevisionId": None,
                    "checksum": row["artifact_checksum"] if kind == "final" else None,
                    "grid": None,
                    "resolutionDigest": None,
                    "relativePath": row["snapshot_path"] if kind == "final" else None,
                }
                for kind in _ARTIFACT_KINDS
            }
        for descriptor in evidence.values():
            if descriptor["availability"] == "available":
                descriptor["url"] = (
                    f"/api/final-review-items/{row['id']}/artifacts/{descriptor['kind']}"
                    f"?artifactRevision={artifact_revision}"
                )
            else:
                descriptor["url"] = None
        return {
            "id": row["id"],
            "batchId": row["batch_id"],
            "formatVersion": 2 if strict else format_version,
            "position": row["position"],
            "sourceProjectId": row["source_project_id"],
            "sourceImageId": row["source_image_id"],
            "sourceProjectName": row["source_project_name"],
            "sourceRelativePath": row["source_relative_path"],
            "finalVariant": row["final_variant"],
            "artifactChecksum": row["artifact_checksum"],
            "thumbnailChecksum": row["thumbnail_checksum"],
            "currentArtifactStale": self._current_stale(row),
            "verdict": row["verdict"],
            "issueCodes": json.loads(row["issue_codes"]),
            "feedback": row["feedback"],
            "reviewedAt": row["reviewed_at"],
            "revision": row["revision"],
            "artifactRevision": artifact_revision,
            "strictEvidence": strict,
            "evidence": evidence,
            "evidenceDigest": (row["evidence_digest"] if "evidence_digest" in row.keys() else None),
            "contentUrl": (
                f"/api/final-review-items/{row['id']}/content?artifactRevision={artifact_revision}"
            ),
            "thumbnailUrl": (
                f"/api/final-review-items/{row['id']}/thumbnail"
                f"?artifactRevision={artifact_revision}"
            ),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def artifact_path(
        self,
        item_id: str,
        *,
        thumbnail: bool = False,
        kind: str = "final",
        artifact_revision: int | None = None,
    ) -> Path:
        row = self._item_row(item_id)
        current_revision = row["artifact_revision"] if "artifact_revision" in row.keys() else 1
        requested_revision = current_revision if artifact_revision is None else artifact_revision
        if requested_revision < 1:
            raise FinalReviewNotFound("Final-review artifact revision is invalid")
        if thumbnail:
            if requested_revision == current_revision:
                relative_value = row["thumbnail_path"]
                expected_checksum = row["thumbnail_checksum"]
            else:
                with _connect(self.database_path) as connection:
                    revision_row = connection.execute(
                        """SELECT thumbnail_path, thumbnail_checksum
                        FROM artifact_revisions
                        WHERE item_id = ? AND artifact_revision = ?""",
                        (item_id, requested_revision),
                    ).fetchone()
                if (
                    revision_row is None
                    or not revision_row["thumbnail_path"]
                    or not revision_row["thumbnail_checksum"]
                ):
                    raise FinalReviewNotFound("Final-review thumbnail revision was not found")
                relative_value = revision_row["thumbnail_path"]
                expected_checksum = revision_row["thumbnail_checksum"]
        elif kind not in _ARTIFACT_KINDS:
            raise FinalReviewNotFound("Unknown final-review evidence kind")
        elif "strict_evidence" not in row.keys() or not row["strict_evidence"]:
            if kind != "final" or requested_revision != current_revision:
                raise FinalReviewNotFound("Legacy final-review evidence is unavailable")
            relative_value = row["snapshot_path"]
            expected_checksum = row["artifact_checksum"]
        else:
            with _connect(self.database_path) as connection:
                revision_row = connection.execute(
                    """SELECT evidence_json, evidence_digest FROM artifact_revisions
                    WHERE item_id = ? AND artifact_revision = ?""",
                    (item_id, requested_revision),
                ).fetchone()
            if revision_row is None:
                raise FinalReviewNotFound("Final-review artifact revision was not found")
            revision_evidence = json.loads(revision_row["evidence_json"])
            if revision_row["evidence_digest"] != _digest(revision_evidence):
                raise ProjectError("Final-review artifact metadata digest is inconsistent")
            descriptor = revision_evidence[kind]
            if descriptor["availability"] != "available":
                raise FinalReviewNotFound("Final-review evidence is not available")
            relative_value = descriptor["relativePath"]
            expected_checksum = descriptor["checksum"]
        relative = safe_relative_path(relative_value)
        path = (self.root / relative).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FinalReviewNotFound("Final-review snapshot is unavailable")
        if _sha256(path) != expected_checksum:
            raise ProjectError("Final-review snapshot checksum does not match its record")
        return path

    def update_item(
        self,
        item_id: str,
        *,
        verdict: str,
        issue_codes: list[str],
        feedback: str,
        expected_revision: int,
        expected_batch_revision: int | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise ProjectError("Unknown final-review verdict")
        normalized_codes = sorted(set(issue_codes))
        if any(code not in ISSUE_CODES for code in normalized_codes):
            raise ProjectError("Unknown final-review issue code")
        feedback = feedback.strip()
        if len(feedback) > 10_000:
            raise ProjectError("Final-review feedback is too long")
        if verdict == "issues":
            if not normalized_codes:
                raise ProjectError("An issues verdict requires at least one issue code")
            if "other" in normalized_codes and not feedback:
                raise ProjectError("The other issue code requires feedback")
        elif verdict == "pending":
            normalized_codes = []
            feedback = ""
        else:
            normalized_codes = []
        now = _utcnow()
        with self.lock, _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                if row is None:
                    raise FinalReviewNotFound(f"Final-review item {item_id} was not found")
                if row["revision"] != expected_revision:
                    connection.rollback()
                    current = self.item(item_id)
                    raise FinalReviewConflict(
                        "The final-review item changed; reload it before saving",
                        expected_revision=expected_revision,
                        actual_revision=row["revision"],
                        item=current,
                    )
                batch_row = connection.execute(
                    "SELECT * FROM batches WHERE id = ?", (row["batch_id"],)
                ).fetchone()
                if batch_row is None:
                    raise ProjectError("Final-review batch is unavailable")
                strict = bool(row["strict_evidence"]) if "strict_evidence" in row.keys() else False
                normalized_actor = (
                    _actor_payload(actor or {})
                    if strict
                    else (_actor_payload(actor) if actor is not None else None)
                )
                if verdict == "approved" and row["verdict"] != "approved":
                    if normalized_actor is None or normalized_actor["actorKind"] != "human":
                        raise ProjectError("Final-review approval requires a human actor")
                if strict and expected_batch_revision is None:
                    raise ProjectError("Strict final-review save requires expected batch revision")
                if (
                    expected_batch_revision is not None
                    and batch_row["revision"] != expected_batch_revision
                ):
                    raise FinalReviewBatchConflict(
                        "The final-review batch changed; reload it before saving",
                        batch_id=row["batch_id"],
                        expected_revision=expected_batch_revision,
                        actual_revision=batch_row["revision"],
                    )
                if (
                    not strict
                    and row["verdict"] in {"approved", "issues"}
                    and (
                        row["verdict"] != verdict
                        or sorted(json.loads(row["issue_codes"])) != normalized_codes
                        or row["feedback"] != feedback
                    )
                ):
                    raise ProjectError(
                        "Legacy reviewed items are immutable; repair and strict refresh first"
                    )
                if (
                    row["verdict"] == verdict
                    and sorted(json.loads(row["issue_codes"])) == normalized_codes
                    and row["feedback"] == feedback
                ):
                    connection.rollback()
                    item = self._public_item(row)
                    if strict:
                        return {
                            "item": item,
                            "batchRevision": batch_row["revision"],
                            "historyCreated": False,
                        }
                    return item
                before = self._history_payload(row)
                reviewed_at = None if verdict == "pending" else now
                cursor = connection.execute(
                    """
                    UPDATE items
                    SET verdict = ?, issue_codes = ?, feedback = ?, reviewed_at = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (
                        verdict,
                        json.dumps(normalized_codes, separators=(",", ":")),
                        feedback,
                        reviewed_at,
                        now,
                        item_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProjectError("Final-review item update did not complete")
                updated = connection.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                assert updated is not None
                connection.execute(
                    """
                    INSERT INTO revisions
                    (id, batch_id, item_id, operation, before_json, after_json,
                     item_revision, created_at)
                    VALUES (?, ?, ?, 'review', ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        updated["batch_id"],
                        item_id,
                        json.dumps(before, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(
                            {
                                **self._history_payload(updated),
                                **({"actor": normalized_actor} if normalized_actor else {}),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        updated["revision"],
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE batches SET revision = revision + 1, updated_at = ?",
                    (now,),
                )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        item = self.item(item_id)
        if strict:
            return {
                "item": item,
                "batchRevision": self.batch()["revision"],
                "historyCreated": True,
            }
        return item

    @staticmethod
    def _history_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "verdict": row["verdict"],
            "issueCodes": json.loads(row["issue_codes"]),
            "feedback": row["feedback"],
            "reviewedAt": row["reviewed_at"],
            "finalVariant": row["final_variant"],
            "artifactChecksum": row["artifact_checksum"],
            "thumbnailChecksum": row["thumbnail_checksum"],
            "artifactRevision": (
                row["artifact_revision"] if "artifact_revision" in row.keys() else 1
            ),
            "evidenceDigest": (row["evidence_digest"] if "evidence_digest" in row.keys() else None),
            "revision": row["revision"],
        }

    def revisions(self, item_id: str) -> list[dict[str, Any]]:
        self._item_row(item_id)
        with self.lock, _connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM revisions WHERE item_id = ? ORDER BY created_at, rowid",
                (item_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "itemId": row["item_id"],
                "operation": row["operation"],
                "before": json.loads(row["before_json"]),
                "after": json.loads(row["after_json"]),
                "itemRevision": row["item_revision"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def repair(
        self,
        item_id: str,
        *,
        expected_revision: int,
        expected_batch_revision: int,
        actor: dict[str, Any],
        parameter_set_id: str = "final-review-repair-v1",
        parameter_set_hash: str | None = None,
        retry_from_generation_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_actor = _actor_payload(actor)
        parameter_set_hash = parameter_set_hash or _digest(
            {"parameterSetId": parameter_set_id, "contractVersion": "G0-repair-v1"}
        )
        if not _SHA256_RE.fullmatch(parameter_set_hash):
            raise ProjectError("Final-review repair parameter hash is invalid")
        with self.lock, _connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise FinalReviewNotFound(f"Final-review item {item_id} was not found")
            batch = connection.execute(
                "SELECT revision FROM batches WHERE id = ?", (row["batch_id"],)
            ).fetchone()
            if row["revision"] != expected_revision:
                raise FinalReviewConflict(
                    "The final-review item changed; reload it before repair",
                    expected_revision=expected_revision,
                    actual_revision=row["revision"],
                    item=self._public_item(row),
                )
            if batch is None or batch["revision"] != expected_batch_revision:
                raise FinalReviewBatchConflict(
                    "The final-review batch changed; reload it before repair",
                    batch_id=row["batch_id"],
                    expected_revision=expected_batch_revision,
                    actual_revision=batch["revision"] if batch is not None else 0,
                )
            if row["verdict"] != "issues":
                raise ProjectError("Only an issues verdict can start a repair generation")
            feedback_checksum = _digest(
                {
                    "issueCodes": sorted(json.loads(row["issue_codes"])),
                    "feedback": row["feedback"],
                }
            )
            source_project_id = row["source_project_id"]
            source_image_id = row["source_image_id"]
            run_id = f"final-review-{item_id[:8]}-r{expected_revision}"

            project_store = self.projects.get(source_project_id)
            with project_store.lock, project_store.session() as session:
                existing = find_final_review_repair_generation(
                    project_store,
                    session,
                    source_project_id=source_project_id,
                    source_image_id=source_image_id,
                    source_relative_path=_repair_source_relative_path(row),
                    final_review_item_id=item_id,
                    final_review_item_revision=expected_revision,
                    feedback_checksum=feedback_checksum,
                    parameter_set_id=parameter_set_id,
                    parameter_set_hash=parameter_set_hash,
                )
                if existing is not None:
                    target, generation = existing
                    attempt, retry_parent = final_review_repair_attempt_context(session, generation)
                    self._assert_repair_snapshot(
                        item_id,
                        row["batch_id"],
                        expected_revision,
                        expected_batch_revision,
                        feedback_checksum,
                    )
                    if retry_from_generation_id is None:
                        return self._repair_result(
                            row,
                            batch["revision"],
                            target,
                            generation,
                            repair_attempt=attempt,
                            retry_from_generation_id=retry_parent,
                            idempotent=True,
                        )
                    if generation.id != retry_from_generation_id:
                        if retry_parent == retry_from_generation_id:
                            return self._repair_result(
                                row,
                                batch["revision"],
                                target,
                                generation,
                                repair_attempt=attempt,
                                retry_from_generation_id=retry_parent,
                                idempotent=True,
                            )
                        raise ProjectError(
                            "The requested repair generation is no longer the retry chain head"
                        )
                    next_attempt = attempt + 1
                elif retry_from_generation_id is not None:
                    raise ProjectError("The requested repair generation does not exist")
                else:
                    next_attempt = 1

            generation_id = str(uuid.uuid4())
            if next_attempt > 1:
                run_id = f"{run_id}-a{next_attempt}"
            target, generation = create_final_review_repair_generation(
                project_store,
                source_image_id,
                final_review_item_id=item_id,
                final_review_item_revision=expected_revision,
                feedback_checksum=feedback_checksum,
                run_id=run_id,
                page_generation_id=generation_id,
                parameter_set_id=parameter_set_id,
                parameter_set_hash=parameter_set_hash,
                actor=normalized_actor,
                repair_attempt=next_attempt,
                retry_from_generation_id=retry_from_generation_id,
            )
            self._assert_repair_snapshot(
                item_id,
                row["batch_id"],
                expected_revision,
                expected_batch_revision,
                feedback_checksum,
            )
            return self._repair_result(
                row,
                batch["revision"],
                target,
                generation,
                repair_attempt=next_attempt,
                retry_from_generation_id=retry_from_generation_id,
                idempotent=False,
            )

    def _assert_repair_snapshot(
        self,
        item_id: str,
        batch_id: str,
        expected_revision: int,
        expected_batch_revision: int,
        feedback_checksum: str,
    ) -> None:
        with _connect(self.database_path) as recheck:
            current = recheck.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            current_batch = recheck.execute(
                "SELECT revision FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        if current_batch is None or current_batch["revision"] != expected_batch_revision:
            raise FinalReviewBatchConflict(
                "Final-review decision changed after repair G0; reload before navigation",
                batch_id=batch_id,
                expected_revision=expected_batch_revision,
                actual_revision=(current_batch["revision"] if current_batch is not None else 0),
            )
        if (
            current is None
            or current["revision"] != expected_revision
            or current["verdict"] != "issues"
            or _digest(
                {
                    "issueCodes": sorted(json.loads(current["issue_codes"])),
                    "feedback": current["feedback"],
                }
            )
            != feedback_checksum
        ):
            actual_revision = current["revision"] if current is not None else 0
            raise FinalReviewConflict(
                "Final-review decision changed after repair G0; reload before navigation",
                expected_revision=expected_revision,
                actual_revision=actual_revision,
                item=self._public_item(current) if current is not None else {},
            )

    @staticmethod
    def _repair_result(
        row: sqlite3.Row,
        batch_revision: int,
        target: ImageAsset,
        generation: PageGeneration,
        *,
        repair_attempt: int,
        retry_from_generation_id: str | None,
        idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "itemId": row["id"],
            "finalReviewItemRevision": row["revision"],
            "artifactRevision": (
                row["artifact_revision"] if "artifact_revision" in row.keys() else 1
            ),
            "batchRevision": batch_revision,
            "sourceProjectId": row["source_project_id"],
            "sourceImageId": row["source_image_id"],
            "repairProjectId": target.project_id,
            "repairImageId": target.id,
            "runId": generation.run_id,
            "pageGenerationId": generation.id,
            "nextSequence": generation.next_sequence,
            "parameterSetId": generation.parameter_set_id,
            "parameterSetHash": generation.parameter_set_hash,
            "repairAttempt": repair_attempt,
            "retryFromGenerationId": retry_from_generation_id,
            "idempotent": idempotent,
        }

    def refresh(
        self,
        item_id: str,
        *,
        expected_revision: int,
        expected_batch_revision: int | None,
        actor: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with ExitStack() as refresh_locks:
            refresh_locks.enter_context(self.lock)
            # Read-only legacy open never migrates.  An explicit, CAS-valid refresh may.
            with _connect(self.database_path) as connection:
                row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                if row is None:
                    raise FinalReviewNotFound(f"Final-review item {item_id} was not found")
                batch = connection.execute(
                    "SELECT * FROM batches WHERE id = ?", (row["batch_id"],)
                ).fetchone()
                normalized_actor = _actor_payload(actor or {})
                if row["revision"] != expected_revision:
                    raise FinalReviewConflict(
                        "The final-review item changed; reload it before refreshing",
                        expected_revision=expected_revision,
                        actual_revision=row["revision"],
                        item=self._public_item(row),
                    )
                if expected_batch_revision is None or batch is None:
                    raise ProjectError(
                        "The final-review batch changed; reload it before refreshing"
                    )
                if batch["revision"] != expected_batch_revision:
                    raise FinalReviewBatchConflict(
                        "The final-review batch changed; reload it before refreshing",
                        batch_id=row["batch_id"],
                        expected_revision=expected_batch_revision,
                        actual_revision=batch["revision"],
                    )
                was_strict = (
                    bool(row["strict_evidence"]) if "strict_evidence" in row.keys() else False
                )
                if not was_strict and row["verdict"] != "issues":
                    raise ProjectError("Only legacy issues may enter strict refresh")
                source_project_id = row["source_project_id"]
                source_image_id = row["source_image_id"]
                feedback_checksum = _digest(
                    {
                        "issueCodes": sorted(json.loads(row["issue_codes"])),
                        "feedback": row["feedback"],
                    }
                )

            project_store = self.projects.get(source_project_id)
            # Keep the source lineage and bytes stable through evidence freezing and
            # the final-review CAS commit.  The global order is final review -> project.
            refresh_locks.enter_context(project_store.lock)
            with project_store.session() as session:
                repair = find_final_review_repair_generation(
                    project_store,
                    session,
                    source_project_id=source_project_id,
                    source_image_id=source_image_id,
                    source_relative_path=_repair_source_relative_path(row),
                    final_review_item_id=item_id,
                    final_review_item_revision=expected_revision,
                    feedback_checksum=feedback_checksum,
                )
                if repair is None:
                    raise ProjectError(
                        "Strict refresh requires the exact final-review repair handoff"
                    )
                image, generation = repair
                repair_handoff_json = json.dumps(
                    {
                        "pageGenerationId": generation.id,
                        "repairImageId": image.id,
                        "finalReviewItemRevision": expected_revision,
                        "feedbackChecksum": feedback_checksum,
                        "sourceRelativePath": generation.source_relative_path,
                        "parameterSetId": generation.parameter_set_id,
                        "parameterSetHash": generation.parameter_set_hash,
                    },
                    separators=(",", ":"),
                )
                artifact_revision = (
                    row["artifact_revision"] if "artifact_revision" in row.keys() else 1
                ) + 1
                variant, descriptors = _strict_artifacts_for_image(
                    self.projects,
                    project_store,
                    session,
                    image,
                    artifact_revision=artifact_revision,
                )
                if (
                    was_strict
                    and descriptors["final"]["checksum"] == row["artifact_checksum"]
                    and descriptors["final"]["generationId"]
                    == json.loads(row["evidence_json"])["final"].get("generationId")
                ):
                    raise ProjectError("The source project has no newer accepted strict final")

            needs_schema_migration = "artifact_revision" not in row.keys()
            legacy_evidence = _legacy_frozen_evidence(self.root, row) if not was_strict else None
            frozen: dict[str, dict[str, Any]] | None = None
            revision_root = self.root / "images" / item_id / f"r{artifact_revision:06d}"
            try:
                frozen, thumbnail_relative, thumbnail_checksum = _freeze_evidence_bundle(
                    self.root, item_id, artifact_revision, descriptors, self.thumbnail_size
                )
                evidence_json = json.dumps(frozen, ensure_ascii=False, separators=(",", ":"))
                evidence_digest = _digest(frozen)
                final = frozen["final"]
                now = _utcnow()
                with _connect(self.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if needs_schema_migration:
                        _migrate_database(self.database_path, connection=connection)
                    current = connection.execute(
                        "SELECT * FROM items WHERE id = ?", (item_id,)
                    ).fetchone()
                    current_batch = connection.execute(
                        "SELECT revision FROM batches WHERE id = ?", (row["batch_id"],)
                    ).fetchone()
                    if current is None or current["revision"] != expected_revision:
                        raise ProjectError("Final-review item changed during refresh")
                    if (
                        current_batch is None
                        or current_batch["revision"] != expected_batch_revision
                    ):
                        raise FinalReviewBatchConflict(
                            "Final-review batch changed during refresh",
                            batch_id=row["batch_id"],
                            expected_revision=expected_batch_revision,
                            actual_revision=(
                                current_batch["revision"] if current_batch is not None else 0
                            ),
                        )
                    before = self._history_payload(current)
                    if legacy_evidence is not None:
                        legacy_json = json.dumps(
                            legacy_evidence, ensure_ascii=False, separators=(",", ":")
                        )
                        connection.execute(
                            """INSERT INTO artifact_revisions
                            (item_id, artifact_revision, evidence_json, evidence_digest,
                             thumbnail_path, thumbnail_checksum, created_at)
                            VALUES (?, 1, ?, ?, ?, ?, ?)""",
                            (
                                item_id,
                                legacy_json,
                                _digest(legacy_evidence),
                                current["thumbnail_path"],
                                current["thumbnail_checksum"],
                                now,
                            ),
                        )
                    connection.execute(
                        """INSERT INTO artifact_revisions
                        (item_id, artifact_revision, evidence_json, evidence_digest,
                         thumbnail_path, thumbnail_checksum, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            item_id,
                            artifact_revision,
                            evidence_json,
                            evidence_digest,
                            thumbnail_relative,
                            thumbnail_checksum,
                            now,
                        ),
                    )
                    cursor = connection.execute(
                        """UPDATE items SET final_variant = ?, artifact_checksum = ?,
                        thumbnail_checksum = ?, snapshot_path = ?, thumbnail_path = ?,
                        verdict = 'pending', issue_codes = '[]', feedback = '', reviewed_at = NULL,
                        revision = revision + 1, artifact_revision = ?, evidence_json = ?,
                        evidence_digest = ?, strict_evidence = 1, repair_handoff_json = ?,
                        updated_at = ?
                        WHERE id = ? AND revision = ?""",
                        (
                            variant,
                            final["checksum"],
                            thumbnail_checksum,
                            final["relativePath"],
                            thumbnail_relative,
                            artifact_revision,
                            evidence_json,
                            evidence_digest,
                            repair_handoff_json,
                            now,
                            item_id,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProjectError("Final-review refresh CAS did not complete")
                    updated = connection.execute(
                        "SELECT * FROM items WHERE id = ?", (item_id,)
                    ).fetchone()
                    assert updated is not None
                    connection.execute(
                        """INSERT INTO revisions
                        (id, batch_id, item_id, operation, before_json, after_json,
                         item_revision, created_at) VALUES (?, ?, ?, 'refresh', ?, ?, ?, ?)""",
                        (
                            str(uuid.uuid4()),
                            updated["batch_id"],
                            item_id,
                            json.dumps(before, ensure_ascii=False, separators=(",", ":")),
                            json.dumps(
                                {**self._history_payload(updated), "actor": normalized_actor},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            updated["revision"],
                            now,
                        ),
                    )
                    connection.execute(
                        "UPDATE batches SET revision = revision + 1, updated_at = ? WHERE id = ?",
                        (now, updated["batch_id"]),
                    )
                    connection.commit()
            except Exception:
                shutil.rmtree(revision_root, ignore_errors=True)
                raise
        return {
            "item": self.item(item_id),
            "batchRevision": self.batch()["revision"],
            "historyCreated": True,
        }


class FinalReviewRegistry:
    def __init__(self, settings: Settings, projects: ProjectRegistry):
        self.settings = settings
        self.projects = projects
        self.root = (settings.data_dir / "final-reviews").resolve()
        self.catalog_path = self.root / "catalog.json"
        self._stores: dict[str, FinalReviewStore] = {}
        self._lock = threading.RLock()

    def _save_catalog(self) -> None:
        payload = [
            {"batchId": batch_id, "manifestPath": str(store.manifest_path)}
            for batch_id, store in sorted(self._stores.items())
        ]
        atomic_write_bytes(
            self.catalog_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )

    def load_catalog(self) -> None:
        _validate_external_target(
            self.root,
            projects=self.projects,
            review_roots=[],
        )
        try:
            payload = json.loads(self.catalog_path.read_text("utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = []
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict) or not isinstance(entry.get("manifestPath"), str):
                    continue
                try:
                    self.open(Path(entry["manifestPath"]), remember=False)
                except (ProjectError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
                    continue
        self._save_catalog()

    def create(
        self,
        *,
        name: str,
        output_path: Path | None,
        source_project_ids: list[str],
        expected_item_count: int,
    ) -> dict[str, Any]:
        if not source_project_ids or len(set(source_project_ids)) != len(source_project_ids):
            raise ProjectError("Source project ids must be a non-empty unique list")
        if expected_item_count < 1:
            raise ProjectError("Expected item count must be positive")
        review_roots = [store.root for store in self._stores.values()]
        if output_path is None:
            target = self.root / _slug(name)
            suffix = 2
            while target.exists():
                target = self.root / f"{_slug(name)}-{suffix}"
                suffix += 1
            target = _validate_external_target(
                target,
                projects=self.projects,
                review_roots=[*review_roots, self.root],
                allow_data_reviews_parent=self.root,
            )
        else:
            target = _validate_external_target(
                output_path,
                projects=self.projects,
                review_roots=[*review_roots, self.root],
            )
        _ensure_new_target(target)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        batch_id = str(uuid.uuid4())
        now = _utcnow()
        selected_stores = {
            project_id: self.projects.get(project_id) for project_id in source_project_ids
        }
        all_open_stores = {
            str(store.root): store for store in [*self.projects.stores(), *selected_stores.values()]
        }
        source_locks = ExitStack()
        try:
            for store in sorted(all_open_stores.values(), key=lambda item: str(item.root)):
                source_locks.enter_context(store.lock)
            _ensure_new_target(target)
            temporary.mkdir()
            (temporary / "images").mkdir()
            (temporary / "thumbnails").mkdir()
            database_path = temporary / "final-review" / "final-review.sqlite3"
            _create_database(database_path)
            position = 1
            with _connect(database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO batches
                    (id, name, root_path, item_count, revision, created_at, updated_at,
                     format_version) VALUES (?, ?, ?, ?, 1, ?, ?, 2)""",
                    (batch_id, name, str(target), expected_item_count, now, now),
                )
                for project_id in source_project_ids:
                    store = selected_stores[project_id]
                    project = store.project()
                    with store.session() as session:
                        images = list(
                            session.scalars(
                                select(ImageAsset)
                                .where(ImageAsset.project_id == project_id)
                                .order_by(ImageAsset.relative_path, ImageAsset.created_at)
                            ).all()
                        )
                        for image in images:
                            item_id = str(uuid.uuid4())
                            variant, evidence = _strict_artifacts_for_image(
                                self.projects, store, session, image, artifact_revision=1
                            )
                            source = Path(str(evidence["final"]["path"]))
                            checksum = evidence["final"]["checksum"]
                            if _sha256(source) != checksum:
                                raise ProjectError(
                                    "A selected final artifact checksum is inconsistent"
                                )
                            frozen, thumbnail_relative, thumbnail_checksum = (
                                _freeze_evidence_bundle(
                                    temporary,
                                    item_id,
                                    1,
                                    evidence,
                                    self.settings.thumbnail_size,
                                )
                            )
                            snapshot_relative = Path(frozen["final"]["relativePath"])
                            evidence_json = json.dumps(
                                frozen, ensure_ascii=False, separators=(",", ":")
                            )
                            evidence_digest = _digest(frozen)
                            strict_evidence = 1
                            connection.execute(
                                """
                                INSERT INTO items
                                (id, batch_id, position, source_project_id, source_image_id,
                                 source_project_name, source_relative_path, final_variant,
                                 artifact_checksum, thumbnail_checksum, snapshot_path,
                                 thumbnail_path, verdict, issue_codes, feedback, reviewed_at,
                                 revision, created_at, updated_at, artifact_revision,
                                 evidence_json, strict_evidence, evidence_digest)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '[]',
                                        '', NULL, 1, ?, ?, 1, ?, ?, ?)
                                """,
                                (
                                    item_id,
                                    batch_id,
                                    position,
                                    project_id,
                                    image.id,
                                    project.name,
                                    image.relative_path,
                                    variant,
                                    checksum,
                                    thumbnail_checksum,
                                    snapshot_relative.as_posix(),
                                    thumbnail_relative,
                                    now,
                                    now,
                                    evidence_json,
                                    strict_evidence,
                                    evidence_digest,
                                ),
                            )
                            connection.execute(
                                """INSERT INTO artifact_revisions
                                (item_id, artifact_revision, evidence_json, evidence_digest,
                                 thumbnail_path, thumbnail_checksum, created_at)
                                VALUES (?, 1, ?, ?, ?, ?, ?)""",
                                (
                                    item_id,
                                    evidence_json,
                                    evidence_digest,
                                    thumbnail_relative,
                                    thumbnail_checksum,
                                    now,
                                ),
                            )
                            creation = {
                                "verdict": "pending",
                                "issueCodes": [],
                                "feedback": "",
                                "reviewedAt": None,
                                "finalVariant": variant,
                                "artifactChecksum": checksum,
                                "thumbnailChecksum": thumbnail_checksum,
                                "artifactRevision": 1,
                                "evidenceDigest": evidence_digest,
                                "revision": 1,
                            }
                            connection.execute(
                                """
                                INSERT INTO revisions
                                (id, batch_id, item_id, operation, before_json, after_json,
                                 item_revision, created_at)
                                VALUES (?, ?, ?, 'create', '{}', ?, 1, ?)
                                """,
                                (
                                    str(uuid.uuid4()),
                                    batch_id,
                                    item_id,
                                    json.dumps(
                                        creation,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                    now,
                                ),
                            )
                            position += 1
                selected_count = position - 1
                if selected_count != expected_item_count:
                    raise ProjectError(
                        f"Expected {expected_item_count} final-review items but selected "
                        f"{selected_count}"
                    )
                connection.commit()
            manifest = {
                "formatVersion": 2,
                "kind": "manga-localizer-final-review",
                "batch": {
                    "id": batch_id,
                    "name": name,
                    "itemCount": selected_count,
                    "createdAt": now,
                },
            }
            atomic_write_bytes(
                temporary / "final-review" / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            _ensure_new_target(target)
            temporary.replace(target)
        except Exception:
            source_locks.close()
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        try:
            store = FinalReviewStore(target, self.projects, self.settings.thumbnail_size)
            with self._lock:
                self._stores[batch_id] = store
                try:
                    self._save_catalog()
                except Exception:
                    self._stores.pop(batch_id, None)
                    shutil.rmtree(target, ignore_errors=True)
                    raise
            return store.batch(include_items=True)
        finally:
            source_locks.close()

    def open(self, manifest_path: Path, *, remember: bool = True) -> dict[str, Any]:
        _reject_symlink_components(manifest_path)
        manifest = manifest_path.expanduser().resolve(strict=True)
        if manifest.name != "manifest.json" or manifest.parent.name != "final-review":
            raise ProjectError("Expected a final-review/manifest.json file")
        payload = json.loads(manifest.read_text("utf-8"))
        if payload.get("kind") != "manga-localizer-final-review":
            raise ProjectError("The selected manifest is not a final-review batch")
        batch_id = payload.get("batch", {}).get("id")
        manifest_version = payload.get("formatVersion")
        if manifest_version not in {1, 2}:
            raise ProjectError("The final-review manifest format version is unsupported")
        if not isinstance(batch_id, str):
            raise ProjectError("The final-review manifest has no valid batch id")
        root = manifest.parent.parent.resolve()
        with self._lock:
            other_review_roots = [
                store.root for store in self._stores.values() if store.root != root
            ]
        allow_data_reviews_parent = self.root if root.is_relative_to(self.root) else None
        _validate_external_target(
            root,
            projects=self.projects,
            review_roots=[*other_review_roots, self.root],
            allow_data_reviews_parent=allow_data_reviews_parent,
        )
        for directory_name in ("images", "thumbnails"):
            directory = root / directory_name
            if directory.is_symlink() or not directory.is_dir():
                raise ProjectError("Final-review snapshot directories are unavailable")
        database = root / "final-review" / "final-review.sqlite3"
        if database.is_symlink() or not database.is_file():
            raise ProjectError("The final-review database is unavailable")
        with _connect(database) as connection:
            item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(items)").fetchall()
            }
            batch_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(batches)").fetchall()
            }
            database_batch = connection.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            database_version = (
                database_batch["format_version"]
                if database_batch is not None and "format_version" in batch_columns
                else 1
            )
            if database_version != manifest_version:
                raise ProjectError("Final-review manifest and database formats do not match")
            if manifest_version == 2:
                required = {
                    "artifact_revision",
                    "evidence_json",
                    "evidence_digest",
                    "strict_evidence",
                }
                if not required <= item_columns:
                    raise ProjectError("Strict final-review database schema is incomplete")
                strict_rows = connection.execute(
                    """SELECT i.id, i.artifact_revision, i.strict_evidence,
                              i.evidence_json, i.evidence_digest,
                              ar.evidence_json AS revision_evidence_json,
                              ar.evidence_digest AS revision_evidence_digest
                       FROM items AS i
                       LEFT JOIN artifact_revisions AS ar
                         ON ar.item_id = i.id
                        AND ar.artifact_revision = i.artifact_revision"""
                ).fetchall()
                item_count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                if len(strict_rows) != item_count:
                    raise ProjectError("Strict final-review evidence is incomplete")
                for strict_row in strict_rows:
                    if (
                        strict_row["strict_evidence"] != 1
                        or strict_row["revision_evidence_json"] is None
                        or strict_row["evidence_digest"] is None
                        or strict_row["revision_evidence_digest"] is None
                    ):
                        raise ProjectError("Strict final-review evidence is incomplete")
                    item_evidence = json.loads(strict_row["evidence_json"])
                    revision_evidence = json.loads(strict_row["revision_evidence_json"])
                    digest = _digest(item_evidence)
                    if (
                        item_evidence != revision_evidence
                        or digest != strict_row["evidence_digest"]
                        or digest != strict_row["revision_evidence_digest"]
                        or any(
                            descriptor.get("artifactRevision") != strict_row["artifact_revision"]
                            for descriptor in item_evidence.values()
                        )
                    ):
                        raise ProjectError("Strict final-review evidence digest is inconsistent")
        if "thumbnail_checksum" not in item_columns:
            raise ProjectError("The final-review database schema is unsupported")
        store = FinalReviewStore(root, self.projects, self.settings.thumbnail_size)
        batch = store.batch(include_items=True)
        if batch["id"] != batch_id:
            raise ProjectError("The final-review manifest and database ids do not match")
        if payload.get("batch", {}).get("itemCount") != batch["itemCount"]:
            raise ProjectError("The final-review manifest and database counts do not match")
        with self._lock:
            self._stores[batch_id] = store
            if remember:
                self._save_catalog()
        return batch

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            stores = list(self._stores.values())
        return sorted(
            (store.batch() for store in stores), key=lambda item: item["updatedAt"], reverse=True
        )

    def get(self, batch_id: str) -> FinalReviewStore:
        with self._lock:
            store = self._stores.get(batch_id)
        if store is None:
            raise FinalReviewNotFound(f"Final-review batch {batch_id} is not open")
        return store

    def find_item(self, item_id: str) -> FinalReviewStore:
        with self._lock:
            stores = list(self._stores.values())
        for store in stores:
            try:
                store._item_row(item_id)
            except FinalReviewNotFound:
                continue
            return store
        raise FinalReviewNotFound(f"Final-review item {item_id} was not found")

    def export(
        self,
        batch_id: str,
        output_path: Path,
        *,
        conflict: str,
        preserve_tree: bool,
        expected_batch_revision: int | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        store = self.get(batch_id)
        target = _validate_external_target(
            output_path,
            projects=self.projects,
            review_roots=[
                *(review.root for review in self._stores.values()),
                self.root,
            ],
        )
        _ensure_new_target(target)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        with store.lock, ExitStack() as source_locks:
            batch_snapshot = store.batch()
            _actor_payload(actor or {})
            if expected_batch_revision is None:
                raise ProjectError("Final-review export requires expected batch revision")
            if expected_batch_revision != batch_snapshot["revision"]:
                raise FinalReviewBatchConflict(
                    "The final-review batch changed; reload it before exporting",
                    batch_id=batch_id,
                    expected_revision=expected_batch_revision,
                    actual_revision=batch_snapshot["revision"],
                )
            if conflict != "rename":
                raise ProjectError("Final-review terminal export requires safe collision renaming")
            counts = batch_snapshot["counts"]
            if (
                batch_snapshot["itemCount"] <= 0
                or counts["approved"] != batch_snapshot["itemCount"]
                or counts["pending"] != 0
                or counts["issues"] != 0
            ):
                raise ProjectError("Final-review export requires every item to be approved")
            with _connect(store.database_path) as connection:
                source_project_ids = [
                    row["source_project_id"]
                    for row in connection.execute(
                        "SELECT DISTINCT source_project_id FROM items ORDER BY source_project_id"
                    ).fetchall()
                ]
            selected_source_stores = [
                self.projects.get(source_project_id) for source_project_id in source_project_ids
            ]
            all_open_stores = {
                str(project_store.root): project_store
                for project_store in [*self.projects.stores(), *selected_source_stores]
            }
            for project_store in sorted(all_open_stores.values(), key=lambda item: str(item.root)):
                source_locks.enter_context(project_store.lock)
            approved = store.items()
            if any(item["currentArtifactStale"] for item in approved):
                raise ProjectError(
                    "Final-review export requires every approved artifact to be current"
                )
            exported: list[dict[str, Any]] = []
            skipped_collisions = 0
            published = False
            try:
                temporary.mkdir()
                for item in approved:
                    source = store.artifact_path(
                        item["id"], artifact_revision=item["artifactRevision"]
                    )
                    project_namespace = (
                        f"{_slug(item['sourceProjectName'])}-{item['sourceProjectId'][:8]}"
                    )
                    relative = safe_relative_path(item["sourceRelativePath"]).with_suffix(".png")
                    export_relative = relative if preserve_tree else Path(relative.name)
                    destination = temporary / project_namespace / export_relative
                    if destination.exists():
                        counter = 2
                        while destination.exists():
                            destination = destination.with_name(
                                f"{export_relative.stem}-{counter}{export_relative.suffix}"
                            )
                            counter += 1
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    if _sha256(destination) != item["artifactChecksum"]:
                        raise ProjectError("An approved export copy failed checksum verification")
                    recorded_relative = destination.relative_to(temporary)
                    exported.append(
                        {
                            "sourceProjectId": item["sourceProjectId"],
                            "sourceImageId": item["sourceImageId"],
                            "relativePath": recorded_relative.as_posix(),
                            "artifactChecksum": item["artifactChecksum"],
                            "finalVariant": item["finalVariant"],
                            "artifactRevision": item["artifactRevision"],
                        }
                    )
                manifest = {
                    "formatVersion": 2,
                    "kind": "manga-localizer-approved-final-review-export",
                    "batchId": batch_id,
                    "batchRevision": batch_snapshot["revision"],
                    "exportedAt": _utcnow(),
                    "approvedCount": len(exported),
                    "items": exported,
                }
                atomic_write_bytes(
                    temporary / "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
                current_items = store.items()
                snapshot_identity = {
                    item["id"]: (
                        item["revision"],
                        item["artifactRevision"],
                        item["artifactChecksum"],
                        item["verdict"],
                    )
                    for item in approved
                }
                if len(current_items) != batch_snapshot["itemCount"] or any(
                    item["currentArtifactStale"]
                    or item["verdict"] != "approved"
                    or snapshot_identity.get(item["id"])
                    != (
                        item["revision"],
                        item["artifactRevision"],
                        item["artifactChecksum"],
                        item["verdict"],
                    )
                    for item in current_items
                ):
                    raise ProjectError("Final-review approved artifacts changed during export")
                with _connect(store.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current_revision = connection.execute(
                        "SELECT revision FROM batches WHERE id = ?", (batch_id,)
                    ).fetchone()
                    if (
                        current_revision is None
                        or current_revision["revision"] != batch_snapshot["revision"]
                    ):
                        raise FinalReviewBatchConflict(
                            "Final-review decisions changed during export; start the export again",
                            batch_id=batch_id,
                            expected_revision=batch_snapshot["revision"],
                            actual_revision=(
                                current_revision["revision"] if current_revision is not None else 0
                            ),
                        )
                    for item in approved:
                        store.artifact_path(item["id"], artifact_revision=item["artifactRevision"])
                        if item["strictEvidence"]:
                            for kind, descriptor in item["evidence"].items():
                                if descriptor["availability"] != "available":
                                    continue
                                path = store.artifact_path(
                                    item["id"],
                                    kind=kind,
                                    artifact_revision=item["artifactRevision"],
                                )
                                width, height = _resolution(path)
                                grid = {"width": width, "height": height}
                                if (
                                    grid != descriptor["grid"]
                                    or _digest(grid) != descriptor["resolutionDigest"]
                                ):
                                    raise ProjectError(
                                        "Frozen final-review evidence grid is inconsistent"
                                    )
                    _ensure_new_target(target)
                    temporary.replace(target)
                    published = True
                    connection.commit()
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                if published:
                    shutil.rmtree(target, ignore_errors=True)
                raise
        return {
            "batchId": batch_id,
            "outputPath": str(target),
            "exportedCount": len(exported),
            "skippedPendingCount": counts["pending"],
            "skippedIssuesCount": counts["issues"],
            "skippedCollisionCount": skipped_collisions,
            "manifestPath": str(target / "manifest.json"),
        }


FINAL_REVIEW_NO_STORE_HEADERS = _NO_STORE_HEADERS
