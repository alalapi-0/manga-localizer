"""Native G2 reconstruction with immutable artifacts and append-only provenance.

No model invocation occurs here. The executing Agent supplies a native result;
operator attestation is recorded honestly, never promoted to provider verification.
Existing lineage/job/revision tables are sufficient; no migration is required.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageGeneration,
    PageLineageEvent,
    Revision,
)
from manga_localizer.schemas import ReconstructionImportRequest
from manga_localizer.security import atomic_write_bytes, resolve_write_target
from manga_localizer.services import page_lineage as lineage_service
from manga_localizer.services.cloud_full_page_clean_plates import (
    FIT_COVER_CROP,
    _normalize,
    _png_bytes,
)
from manga_localizer.services.projects import ProjectError, RevisionConflict, add_revision

PROFILE = "native-reconstruction-v1"
LETTERING_LOCK_PROFILE = "g1-inside-lettering-mask-native-outside-v1"
MAX_RAW_BYTES = 40 * 1024 * 1024
MAX_METADATA_CHARS = 16 * 1024
CHECKS = (
    "clarity-improved",
    "identity-preserved",
    "expression-preserved",
    "composition-preserved",
    "text-and-sfx-preserved",
    "objects-preserved",
    "no-invented-detail",
    "no-artifacts",
)
GATE = "G2_reconstruction"
PRODUCED = "reconstruction-candidate-produced"
COMPLETED = "reconstruction-job-completed"
REVIEWED = "reconstruction-candidate-reviewed"
ENQUEUED = "reconstruction-job-enqueued"


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest(value: Any) -> str:
    return sha(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    )


def _conflict(message: str, reason: str = "g2-reconstruction-invalid"):
    raise lineage_service.PageLineageConflict(message, resource="G2-reconstruction", reason=reason)


def dump_import_request(metadata: dict[str, Any]) -> dict[str, Any]:
    dumped = ReconstructionImportRequest.model_validate(metadata).model_dump(
        mode="json", by_alias=True
    )
    if dumped.get("letteringLock") is not True:
        dumped.pop("letteringLock", None)
        dumped.pop("letteringMaskSha256", None)
    return dumped


def lock_lettering(native: bytes, baseline: bytes, mask: bytes) -> tuple[bytes, dict[str, Any]]:
    if not mask or len(mask) > MAX_RAW_BYTES:
        raise ProjectError("Lettering lock mask is outside the supported limits")
    try:
        native_image = Image.open(io.BytesIO(native))
        baseline_image = Image.open(io.BytesIO(baseline))
        mask_image = Image.open(io.BytesIO(mask))
        native_image.load()
        baseline_image.load()
        mask_image.load()
    except OSError as error:
        raise ProjectError("Lettering lock mask is not a supported raster") from error
    try:
        if getattr(mask_image, "n_frames", 1) != 1:
            raise ProjectError("Lettering lock mask must be a single raster")
        native_rgb = native_image.convert("RGB")
        baseline_rgb = baseline_image.convert("RGB")
        mask_l = mask_image.convert("L")
        if native_rgb.size != baseline_rgb.size or mask_l.size != native_rgb.size:
            raise ProjectError("Lettering lock inputs must share the reconstruction grid")
        binary = mask_l.point([0] + [255] * 255)
        if binary.getextrema() == (0, 0):
            raise ProjectError("Lettering lock mask is empty")
        locked = Image.composite(baseline_rgb, native_rgb, binary)
        payload = _png_bytes(locked)
        locked_count = int(sum(binary.histogram()[1:]))
    finally:
        native_image.close()
        baseline_image.close()
        mask_image.close()
    return payload, {
        "letteringLock": True,
        "letteringLockProfile": LETTERING_LOCK_PROFILE,
        "letteringMaskSha256": sha(mask),
        "nativeNormalizedSha256": sha(native),
        "letteringSource": "accepted-G1",
        "maskRule": "nonzero-g1-zero-native",
        "lockedPixelCount": locked_count,
    }


def normalize(raw: bytes, grid: tuple[int, int]) -> tuple[bytes, dict[str, Any]]:
    if not raw or len(raw) > MAX_RAW_BYTES or min(grid) < 1 or grid[0] * grid[1] > 32_000_000:
        raise ProjectError("Reconstruction raster is outside the supported limits")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if getattr(opened, "n_frames", 1) != 1:
                raise ProjectError("Reconstruction requires a single upright raster")
        normalized, manifest, _raw_grid, _media_type = _normalize(raw, grid, fit=FIT_COVER_CROP)
    except (OSError, ValueError) as error:
        raise ProjectError("Reconstruction input is not a supported raster") from error
    return normalized, {**manifest, "profile": "reconstruction-whole-frame-v1"}


def _materialize_normalized(
    raw: bytes,
    grid: tuple[int, int],
    baseline: bytes,
    request: dict[str, Any],
    lettering_mask: bytes | None,
) -> tuple[bytes, dict[str, Any]]:
    native, native_manifest = normalize(raw, grid)
    if request.get("letteringLock") is True:
        expected = request.get("letteringMaskSha256")
        if not lettering_mask or not isinstance(expected, str) or sha(lettering_mask) != expected:
            raise ProjectError(
                "Lettering lock mask is missing or does not match the request digest"
            )
        locked, lock_manifest = lock_lettering(native, baseline, lettering_mask)
        return locked, {**native_manifest, **lock_manifest}
    if lettering_mask is not None:
        raise ProjectError("Lettering mask was supplied without a lettering lock request")
    return native, native_manifest


def _path(store, generation_id: str, candidate_id: str, name: str) -> Path:
    return resolve_write_target(
        store.root,
        Path("generated") / "lineage-reconstructions" / generation_id / candidate_id / name,
        protected_roots=(store.source_root,),
    )


def _read(path: Path, checksum: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ProjectError("Reconstruction artifact is unavailable") from error
    if sha(payload) != checksum:
        _conflict("Reconstruction artifact checksum changed", "g2-artifact-tampered")
    return payload


def _active(session, image):
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.image_id == image.id, PageGeneration.state == "active"
        )
    )
    if generation is None:
        _conflict("Reconstruction requires an active generation", "active-generation-missing")
    return generation


def _inputs(store, session, image, generation):
    checksum, baseline_event = lineage_service._current_accepted_preprocess(
        store, session, image, generation
    )
    decision = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.gate == GATE,
            PageLineageEvent.operation == "reconstruction-decision",
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if (
        decision is None
        or decision.decision != "further-reconstruction-yes"
        or decision.state != "blocked"
        or decision.sequence <= baseline_event.sequence
        or decision.input_checksum != checksum
        or decision.parent_checksum != checksum
    ):
        _conflict("Current accepted G1 and explicit G2 yes are required", "g2-yes-required")
    source = lineage_service._immutable_image_path(store, image).read_bytes()
    if sha(source) != generation.source_checksum or image.checksum != generation.source_checksum:
        _conflict("Immutable reconstruction source changed", "source-checksum-changed")
    baseline = _read(
        lineage_service._generated_page_artifact_path(store, image, "preprocessed"), checksum
    )
    with Image.open(io.BytesIO(baseline)) as opened:
        grid = opened.size
    if not any(grid == (image.width * scale, image.height * scale) for scale in range(1, 5)):
        _conflict("Reconstruction baseline must use an integer isotropic source grid")
    return {
        "sourceChecksum": generation.source_checksum,
        "baselineChecksum": checksum,
        "baselineEventId": baseline_event.id,
        "decisionEventId": decision.id,
        "targetGrid": {"width": grid[0], "height": grid[1]},
    }, baseline


def _stable_request(metadata: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in metadata.items() if k not in {"lineage", "expectedRevision"}}
    binding = metadata["lineage"]
    return {
        **result,
        "lineage": {
            "runId": binding["runId"],
            "pageGenerationId": binding["pageGenerationId"],
            "actor": lineage_service._safe_actor(binding["actor"]),
        },
    }


def _candidate_id(generation_id, invocation_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"native-g2:{generation_id}:{invocation_id}"))


def _producer(session, generation_id, candidate_id):
    events = session.scalars(
        select(PageLineageEvent).where(
            PageLineageEvent.generation_id == generation_id,
            PageLineageEvent.operation == PRODUCED,
        )
    ).all()
    matches = [event for event in events if event.evidence.get("candidateId") == candidate_id]
    if len(matches) != 1:
        _conflict("Reconstruction candidate has no unique producer")
    return matches[0]


def _verify_candidate(store, session, image, generation, event):
    evidence = event.evidence
    request = evidence.get("request", {})
    cid = evidence.get("candidateId")
    required = {
        "candidateId",
        "request",
        "requestDigest",
        "rawChecksum",
        "normalizedChecksum",
        "baselineChecksum",
        "targetGrid",
        "normalization",
        "imageRevision",
    }
    if set(evidence) != required or not isinstance(request, dict):
        _conflict("Reconstruction producer metadata is incomplete")
    try:
        validated = dump_import_request(
            {
                **request,
                "expectedRevision": 0,
                "lineage": {**request["lineage"], "expectedSequence": 2},
            }
        )
    except (ValidationError, KeyError, TypeError) as error:
        raise ProjectError("Reconstruction provenance is invalid") from error
    if (
        _stable_request(validated) != request
        or digest(request) != evidence["requestDigest"]
        or request["sourceChecksum"] != generation.source_checksum
        or request["lineage"]["runId"] != generation.run_id
        or request["lineage"]["pageGenerationId"] != generation.id
        or cid != _candidate_id(generation.id, request["invocationId"])
    ):
        _conflict("Reconstruction producer identity changed")
    baseline_event = session.get(PageLineageEvent, request["baselineEventId"])
    decision = session.get(PageLineageEvent, request["decisionEventId"])
    if (
        baseline_event is None
        or decision is None
        or baseline_event.generation_id != generation.id
        or decision.generation_id != generation.id
        or baseline_event.operation != "preprocess-stage-review"
        or baseline_event.state != "accepted"
        or baseline_event.output_checksum != request["baselineChecksum"]
        or baseline_event.parent_checksum != generation.source_checksum
        or decision.operation != "reconstruction-decision"
        or decision.decision != "further-reconstruction-yes"
        or decision.state != "blocked"
        or decision.input_checksum != request["baselineChecksum"]
        or decision.parent_checksum != request["baselineChecksum"]
        or not baseline_event.sequence < decision.sequence < event.sequence - 1
    ):
        _conflict("Reconstruction input ancestry is invalid")
    job = session.get(Job, event.job_id)
    item = session.get(JobItem, event.job_item_id)
    revision = session.get(Revision, event.revision_id)
    expected_output = {
        k: evidence[k]
        for k in ("candidateId", "rawChecksum", "normalizedChecksum", "requestDigest")
    }
    if (
        job is None
        or item is None
        or revision is None
        or job.kind != "native-reconstruction"
        or job.project_id != image.project_id
        or job.status != "completed"
        or job.progress != 1.0
        or job.error is not None
        or item.status != "completed"
        or item.progress != 1.0
        or item.error is not None
        or item.position != 0
        or item.job_id != job.id
        or job.total != 1
        or job.completed != 1
        or item.image_id != image.id
        or item.region_id is not None
        or item.started_at is None
        or item.finished_at is None
        or item.output != expected_output
        or job.options != {"profile": PROFILE, "requestDigest": evidence["requestDigest"]}
        or job.lineage_context != request["lineage"]
        or revision.project_id != image.project_id
        or revision.operation != "create"
        or revision.before != {}
        or revision.entity_type != "page-reconstruction"
        or revision.entity_id != cid
        or revision.after != evidence
    ):
        _conflict("Reconstruction has no exact completed producer job/revision")
    if session.scalars(select(JobItem.id).where(JobItem.job_id == job.id)).all() != [item.id]:
        _conflict("Reconstruction producer job has unexpected items")
    chain = session.scalars(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_id == job.id,
        )
        .order_by(PageLineageEvent.sequence)
    ).all()
    if len(chain) != 3:
        _conflict("Reconstruction producer chain is incomplete")
    for offset, (linked, operation) in enumerate(
        zip(chain, (ENQUEUED, PRODUCED, COMPLETED), strict=True)
    ):
        expected_evidence = (
            evidence
            if offset == 1
            else {"candidateId": cid, "requestDigest": evidence["requestDigest"]}
        )
        if (
            linked.operation != operation
            or linked.gate != GATE
            or linked.state != "pending"
            or linked.sequence != event.sequence - 1 + offset
            or linked.job_item_id != item.id
            or linked.input_checksum != request["baselineChecksum"]
            or linked.parent_checksum != request["baselineChecksum"]
            or linked.output_checksum != (None if offset == 0 else evidence["normalizedChecksum"])
            or linked.parameter_hash != evidence["requestDigest"]
            or linked.provider != request["provider"]
            or linked.model_version != request["modelVersion"]
            or lineage_service._actor_columns(request["lineage"]["actor"])
            != {
                key: getattr(linked, key)
                for key in lineage_service._actor_columns(request["lineage"]["actor"])
            }
            or linked.evidence != expected_evidence
            or linked.revision_id != (revision.id if offset == 1 else None)
        ):
            _conflict("Reconstruction producer events are inconsistent")
    baseline = _read(_path(store, generation.id, cid, "baseline.png"), evidence["baselineChecksum"])
    if evidence["baselineChecksum"] != request["baselineChecksum"]:
        _conflict("Reconstruction baseline snapshot is inconsistent")
    with Image.open(io.BytesIO(baseline)) as opened:
        grid = opened.size
    if evidence["targetGrid"] != {"width": grid[0], "height": grid[1]}:
        _conflict("Reconstruction target grid is inconsistent")
    raw = _read(_path(store, generation.id, cid, "raw.bin"), evidence["rawChecksum"])
    mask_path = _path(store, generation.id, cid, "lettering-mask.png")
    if request.get("letteringLock") is True:
        lettering_mask = _read(mask_path, request["letteringMaskSha256"])
    else:
        if mask_path.exists():
            _conflict("Unlocked reconstruction has a lettering mask artifact")
        lettering_mask = None
    normalized, manifest = _materialize_normalized(raw, grid, baseline, request, lettering_mask)
    candidate_path = _path(store, generation.id, cid, "normalized.png")
    if (
        manifest != evidence["normalization"]
        or sha(normalized) != evidence["normalizedChecksum"]
        or _read(candidate_path, evidence["normalizedChecksum"]) != normalized
    ):
        _conflict("Reconstruction normalization cannot be replayed")
    return candidate_path


def _current_candidate(store, session, image, generation, producer, inputs):
    path = _verify_candidate(store, session, image, generation, producer)
    request = producer.evidence["request"]
    if any(
        request[key] != inputs[key]
        for key in ("sourceChecksum", "baselineChecksum", "baselineEventId", "decisionEventId")
    ):
        _conflict("Reconstruction candidate is stale", "g2-candidate-stale")
    return path


def _public_candidate(event, reviews):
    e = event.evidence
    review = next((r for r in reviews if r.evidence.get("candidateId") == e["candidateId"]), None)
    return {
        "candidateId": e["candidateId"],
        "rawChecksum": e["rawChecksum"],
        "checksum": e["normalizedChecksum"],
        "requestDigest": e["requestDigest"],
        "request": e["request"],
        "targetGrid": e["targetGrid"],
        "producerEventId": event.id,
        "state": review.state if review else "pending",
    }


def context(store, image_id: str):
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = _active(session, image)
        inputs, _baseline = _inputs(store, session, image, generation)
        producers = session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.operation == PRODUCED,
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
        reviews = session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.operation == REVIEWED,
            )
            .order_by(PageLineageEvent.sequence.desc())
        ).all()
        for producer in producers:
            _verify_candidate(store, session, image, generation, producer)
        return {
            "profile": PROFILE,
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "runId": generation.run_id,
            "nextSequence": generation.next_sequence,
            **inputs,
            "candidates": [_public_candidate(event, reviews) for event in producers],
        }


def input_path(store, image_id: str, role: str):
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Image was not found in this project")
        generation = _active(session, image)
        _inputs(store, session, image, generation)
        if role == "original":
            return lineage_service._immutable_image_path(store, image)
        if role == "baseline":
            return lineage_service._generated_page_artifact_path(store, image, "preprocessed")
        raise ProjectError("Unknown reconstruction input")


def ingest(
    store,
    image_id: str,
    *,
    raw: bytes,
    metadata: dict[str, Any],
    lettering_mask: bytes | None = None,
):
    try:
        metadata = dump_import_request(metadata)
    except ValidationError as error:
        raise ProjectError("Invalid reconstruction metadata") from error
    request = _stable_request(metadata)
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            generation = _active(session, image)
            inputs, baseline = _inputs(store, session, image, generation)
            if (
                request["lineage"]["runId"] != generation.run_id
                or request["lineage"]["pageGenerationId"] != generation.id
                or any(
                    request[key] != inputs[key]
                    for key in (
                        "sourceChecksum",
                        "baselineChecksum",
                        "baselineEventId",
                        "decisionEventId",
                    )
                )
            ):
                _conflict("Reconstruction input or generation is stale", "g2-candidate-stale")
            grid = (inputs["targetGrid"]["width"], inputs["targetGrid"]["height"])
            normalized, manifest = _materialize_normalized(
                raw, grid, baseline, request, lettering_mask
            )
            cid = _candidate_id(generation.id, request["invocationId"])
            existing = session.scalars(
                select(PageLineageEvent).where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.operation == PRODUCED,
                )
            ).all()
            for event in existing:
                if event.evidence.get("candidateId") == cid:
                    _current_candidate(store, session, image, generation, event, inputs)
                    if (
                        event.evidence["request"] != request
                        or event.evidence["rawChecksum"] != sha(raw)
                        or event.evidence["normalizedChecksum"] != sha(normalized)
                    ):
                        _conflict(
                            "Invocation replay changed bytes or parameters",
                            "g2-invocation-conflict",
                        )
                    reviews = session.scalars(
                        select(PageLineageEvent)
                        .where(
                            PageLineageEvent.generation_id == generation.id,
                            PageLineageEvent.operation == REVIEWED,
                        )
                        .order_by(PageLineageEvent.sequence.desc())
                    ).all()
                    return {
                        **_public_candidate(event, reviews),
                        "replayed": True,
                        "imageRevision": image.revision,
                        "nextSequence": generation.next_sequence,
                    }
            if image.revision != metadata["expectedRevision"]:
                raise RevisionConflict(
                    "Image changed before reconstruction import",
                    expected_revision=metadata["expectedRevision"],
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            binding = lineage_service.require_image_mutation_lineage(
                store, session, image, metadata["lineage"]
            )
            generation, actor, sequence = binding
            evidence = {
                "candidateId": cid,
                "request": request,
                "requestDigest": digest(request),
                "rawChecksum": sha(raw),
                "normalizedChecksum": sha(normalized),
                "baselineChecksum": sha(baseline),
                "targetGrid": inputs["targetGrid"],
                "normalization": manifest,
                "imageRevision": image.revision + 1,
            }
            payloads = {"baseline.png": baseline, "raw.bin": raw, "normalized.png": normalized}
            if lettering_mask is not None:
                payloads["lettering-mask.png"] = lettering_mask
            targets = {name: _path(store, generation.id, cid, name) for name in payloads}
            for name, target in targets.items():
                if target.exists() and target.read_bytes() != payloads[name]:
                    _conflict("Immutable reconstruction output already contains different bytes")
            now = datetime.now(UTC)
            jid = str(uuid.uuid5(uuid.UUID(cid), "job"))
            iid = str(uuid.uuid5(uuid.UUID(cid), "item"))
            job = Job(
                id=jid,
                project_id=image.project_id,
                kind="native-reconstruction",
                status="completed",
                progress=1.0,
                total=1,
                completed=1,
                options={"profile": PROFILE, "requestDigest": digest(request)},
                lineage_context=request["lineage"],
                created_at=now,
                updated_at=now,
            )
            item = JobItem(
                id=iid,
                job_id=jid,
                image_id=image.id,
                position=0,
                status="completed",
                progress=1.0,
                output={
                    k: evidence[k]
                    for k in ("candidateId", "rawChecksum", "normalizedChecksum", "requestDigest")
                },
                started_at=now,
                finished_at=now,
            )
            session.add_all([job, item])
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-reconstruction",
                entity_id=cid,
                operation="create",
                before={},
                after=evidence,
            )
            session.flush()
            for name, target in targets.items():
                if not target.exists():
                    atomic_write_bytes(target, payloads[name])
            produced = None
            for offset, operation in enumerate((ENQUEUED, PRODUCED, COMPLETED)):
                event = lineage_service._append_event(
                    session,
                    generation,
                    operation=operation,
                    state="pending",
                    actor=actor,
                    gate=GATE,
                    input_checksum=inputs["baselineChecksum"],
                    parent_checksum=inputs["baselineChecksum"],
                    output_checksum=None if offset == 0 else sha(normalized),
                    stage="quality",
                    provider=request["provider"],
                    model_version=request["modelVersion"],
                    parameter_hash=digest(request),
                    job_id=jid,
                    job_item_id=iid,
                    revision_id=revision.id if offset == 1 else None,
                    evidence=evidence
                    if offset == 1
                    else {"candidateId": cid, "requestDigest": digest(request)},
                    started_at=now,
                    finished_at=now,
                    expected_sequence=sequence + offset,
                )
                if offset == 1:
                    produced = event
            result = {
                **_public_candidate(produced, []),
                "replayed": False,
                "imageRevision": image.revision,
                "nextSequence": generation.next_sequence,
            }
        store.write_snapshot()
    return result


def _checks(checks, decision):
    if (
        [entry.get("check") for entry in checks] != list(CHECKS)
        or any(
            set(entry) != {"check", "passed"} or type(entry["passed"]) is not bool
            for entry in checks
        )
        or decision not in {"accept", "reject"}
        or (decision == "accept") != all(entry["passed"] for entry in checks)
    ):
        _conflict("Reconstruction review requires exact, truthful visual checks")


def review(
    store,
    image_id,
    *,
    candidate_id,
    observed_checksum,
    decision,
    checks,
    expected_revision,
    lineage,
):
    _checks(checks, decision)
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Image was not found in this project")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    "Image changed before reconstruction review",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            generation, actor, sequence = lineage_service.require_image_mutation_lineage(
                store, session, image, lineage
            )
            inputs, _baseline = _inputs(store, session, image, generation)
            producer = _producer(session, generation.id, candidate_id)
            _current_candidate(store, session, image, generation, producer, inputs)
            if producer.output_checksum != observed_checksum:
                _conflict("Reconstruction review checksum differs from the candidate")
            old_reviews = session.scalars(
                select(PageLineageEvent).where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.operation == REVIEWED,
                )
            ).all()
            if any(event.evidence.get("candidateId") == candidate_id for event in old_reviews):
                _conflict("Reconstruction candidate review is immutable")
            state = "accepted" if decision == "accept" else "rejected"
            evidence = {
                "candidateId": candidate_id,
                "producerEventId": producer.id,
                "targetKind": "reconstruction",
                "checks": checks,
                "requestDigest": producer.evidence["requestDigest"],
            }
            image.revision += 1
            status = dict(image.status or {})
            status.update(reviewState="pending", reviewedAt="", export="pending")
            image.status = status
            revision = add_revision(
                session,
                store.project(session),
                entity_type="page-reconstruction-review",
                entity_id=candidate_id,
                operation="review",
                before={},
                after=evidence | {"state": state},
            )
            session.flush()
            now = datetime.now(UTC)
            event = lineage_service._append_event(
                session,
                generation,
                operation=REVIEWED,
                gate=GATE,
                state=state,
                actor=actor,
                input_checksum=inputs["baselineChecksum"],
                parent_checksum=inputs["baselineChecksum"],
                output_checksum=observed_checksum,
                stage="quality",
                parameter_hash=producer.parameter_hash,
                revision_id=revision.id,
                decision=decision,
                reason="visual-checks-passed" if decision == "accept" else "visual-checks-failed",
                evidence=evidence,
                started_at=now,
                finished_at=now,
                expected_sequence=sequence,
            )
            result = {
                "candidateId": candidate_id,
                "state": state,
                "checksum": observed_checksum,
                "eventId": event.id,
                "imageRevision": image.revision,
                "nextSequence": generation.next_sequence,
            }
        store.write_snapshot()
    return result


def accepted_quality(store, session, image, generation, event):
    inputs, _baseline = _inputs(store, session, image, generation)
    e = event.evidence
    if (
        event.operation != REVIEWED
        or event.state != "accepted"
        or event.decision != "accept"
        or set(e) != {"candidateId", "producerEventId", "targetKind", "checks", "requestDigest"}
        or e["targetKind"] != "reconstruction"
    ):
        _conflict("Accepted reconstruction review is invalid")
    _checks(e["checks"], "accept")
    producer = _producer(session, generation.id, e["candidateId"])
    path = _current_candidate(store, session, image, generation, producer, inputs)
    revision = session.get(Revision, event.revision_id)
    if (
        producer.id != e["producerEventId"]
        or event.sequence <= producer.sequence + 1
        or event.output_checksum != producer.output_checksum
        or event.parameter_hash != producer.parameter_hash
        or e["requestDigest"] != producer.evidence["requestDigest"]
        or revision is None
        or revision.project_id != image.project_id
        or revision.operation != "review"
        or revision.before != {}
        or revision.entity_type != "page-reconstruction-review"
        or revision.entity_id != e["candidateId"]
        or revision.after != e | {"state": "accepted"}
    ):
        _conflict("Accepted reconstruction review does not bind its completed producer")
    return {
        "path": path,
        "checksum": event.output_checksum,
        "targetKind": "reconstruction",
        "eventSequence": event.sequence,
    }


def artifact_path(store, image_id, candidate_id, variant="normalized"):
    names = {
        "normalized": "normalized.png",
        "raw": "raw.bin",
        "baseline": "baseline.png",
        "lettering-mask": "lettering-mask.png",
    }
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None or variant not in names:
            raise ProjectError("Unknown reconstruction artifact")
        events = session.scalars(
            select(PageLineageEvent)
            .join(PageGeneration)
            .where(PageGeneration.image_id == image.id, PageLineageEvent.operation == PRODUCED)
        ).all()
        producer = next(
            (event for event in events if event.evidence.get("candidateId") == candidate_id), None
        )
        if producer is None:
            raise ProjectError("Unknown reconstruction candidate")
        if (
            variant == "lettering-mask"
            and producer.evidence.get("request", {}).get("letteringLock") is not True
        ):
            raise ProjectError("Unknown reconstruction artifact")
        generation = session.get(PageGeneration, producer.generation_id)
        _verify_candidate(store, session, image, generation, producer)
        return _path(store, generation.id, candidate_id, names[variant])
