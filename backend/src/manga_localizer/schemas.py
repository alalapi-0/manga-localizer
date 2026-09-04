from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from PIL import ImageColor
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class APIModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class ProjectCreate(APIModel):
    name: str = Field(min_length=1, max_length=255)
    output_path: Path | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Project name must contain visible characters")
        return value


class ProjectOpen(APIModel):
    manifest_path: Path


class FinalReviewBatchCreate(APIModel):
    name: str = Field(min_length=1, max_length=255)
    output_path: Path | None = None
    source_project_ids: list[str] = Field(min_length=1)
    expected_item_count: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def clean_final_review_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Final-review name must contain visible characters")
        return value


class FinalReviewBatchOpen(APIModel):
    manifest_path: Path


class FinalReviewItemPatch(APIModel):
    verdict: Literal["pending", "approved", "issues"]
    issue_codes: list[
        Literal[
            "typesetting",
            "translation",
            "mask",
            "ai_inpaint",
            "missing_text",
            "preprocess",
            "other",
        ]
    ] = Field(default_factory=list)
    feedback: str = Field(default="", max_length=10_000)
    expected_revision: int = Field(ge=1)
    expected_batch_revision: int | None = Field(default=None, ge=1)
    actor: dict[str, Any] | None = None


class FinalReviewItemRefresh(APIModel):
    expected_revision: int = Field(ge=1)
    expected_batch_revision: int = Field(ge=1)
    actor: dict[str, Any]


class FinalReviewItemRepair(APIModel):
    expected_revision: int = Field(ge=1)
    expected_batch_revision: int = Field(ge=1)
    actor: dict[str, Any]
    parameter_set_id: str = Field(
        default="final-review-repair-v1",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    parameter_set_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retry_from_generation_id: UUID | None = None


class FinalReviewBatchExport(APIModel):
    output_path: Path
    conflict: Literal["rename", "skip"] = "rename"
    preserve_tree: bool = True
    expected_batch_revision: int = Field(ge=1)
    actor: dict[str, Any]


class ProjectPatch(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class ProjectOut(APIModel):
    id: str
    name: str
    root_path: str
    output_root: str
    input_root: str | None
    schema_version: int
    settings: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class InpaintCandidateOut(APIModel):
    id: str
    label: str
    origin_kind: Literal[
        "direct-ai", "ai-derived", "classical", "deterministic-postprocess", "mixed"
    ]
    anomalies: list[str] = Field(default_factory=list)


class SelectInpaintCandidateRequest(APIModel):
    candidate_id: str
    expected_revision: int = Field(ge=0)


class InpaintClassicalFallbackRequest(APIModel):
    state: Literal["approved", "pending"]
    reason: Literal["ai-visible-artifacts"] | None = None
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_approval_fields(self) -> InpaintClassicalFallbackRequest:
        if self.state == "approved":
            if self.reason != "ai-visible-artifacts":
                raise ValueError("Classical fallback approval requires ai-visible-artifacts")
        elif self.reason is not None:
            raise ValueError("Pending classical fallback cannot include approval evidence")
        return self


class InpaintAICandidateReviewRequest(APIModel):
    state: Literal["rejected", "pending"]
    expected_revision: int = Field(ge=0)


class InpaintFallbackOut(APIModel):
    state: Literal["approved", "pending"]
    kind: Literal["classical-page-fallback"] | None = None
    reason: Literal["ai-visible-artifacts"] | None = None
    origin_kind: Literal["classical"] | None = None
    candidate_id: str | None = None
    rejected_ai_candidate_ids: list[str] = Field(default_factory=list)


class PreprocessSuggestionOut(APIModel):
    profile: Literal["off", "ocr-friendly", "balanced", "visual-quality"]
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ImageOut(APIModel):
    id: str
    project_id: str
    name: str
    relative_path: str
    source_kind: str
    width: int
    height: int
    media_type: str
    status: dict[str, str]
    preprocess_suggestion: PreprocessSuggestionOut
    stage_reviews: dict[Literal["preprocess", "inpaint", "typeset"], dict[str, str | int]] = Field(
        default_factory=dict
    )
    region_count: int
    confirmed_count: int
    trusted_count: int
    trust_review_count: int
    ignored_count: int
    processing_errors: list[dict[str, Any]]
    error: str | None
    revision: int
    preprocessing_provider: str | None
    detector_provider: str | None
    ocr_provider: str | None
    translator_provider: str | None
    inpainting_provider: str | None
    typesetting_provider: str | None
    render_input_variant: Literal["original", "preprocessed"] | None = None
    render_scale: list[float] | None = None
    rendered_size: list[int] | None = None
    inpaint_candidate: str | None = None
    inpaint_candidates: list[InpaintCandidateOut] = Field(default_factory=list)
    inpaint_candidate_generation_id: str | None = None
    inpaint_ai_rejected_candidate_ids: list[str] = Field(default_factory=list)
    inpaint_fallback: InpaintFallbackOut
    typeset_overflow_count: int = 0
    typeset_overflow_region_ids: list[str] = Field(default_factory=list)
    thumbnail_url: str
    content_url: str
    created_at: datetime
    updated_at: datetime


class LineageActor(APIModel):
    actor_kind: Literal["codex", "cursor", "human", "system"]
    actor_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation_source: Literal["ui", "api", "script"]

    @field_validator("actor_id", "task_id", "thread_id", "session_id")
    @classmethod
    def validate_opaque_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if cleaned != value or any(
            character in cleaned for character in ("/", "\\", "\x00", "\n", "\r")
        ):
            raise ValueError("Lineage identifiers must be opaque single-line values")
        return cleaned

    @model_validator(mode="after")
    def require_actor_anchor(self) -> LineageActor:
        if not any((self.actor_id, self.task_id, self.thread_id, self.session_id)):
            raise ValueError("Lineage actor requires at least one opaque identity anchor")
        return self


class MutationLineageContext(APIModel):
    run_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    page_generation_id: UUID
    expected_sequence: int = Field(ge=2)
    actor: LineageActor


class ImageReviewRequest(APIModel):
    review_state: Literal["pending", "reviewed", "no-text-reviewed"]
    expected_revision: int = Field(ge=0)


class StageReviewRequest(APIModel):
    state: Literal["pending", "accepted", "rejected"]
    expected_revision: int = Field(ge=0)
    observed_artifact_checksum: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    observed_mask_checksum: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    lineage: MutationLineageContext | None = None

    @model_validator(mode="after")
    def validate_observed_checksums(self) -> StageReviewRequest:
        if self.state == "pending":
            if (
                self.observed_artifact_checksum is not None
                or self.observed_mask_checksum is not None
            ):
                raise ValueError("Pending visual reviews cannot include observed checksums")
        elif self.observed_artifact_checksum is None:
            raise ValueError("Accepted and rejected reviews require an observed artifact checksum")
        return self


class LocalImportRequest(APIModel):
    paths: list[Path] = Field(min_length=1)


class RegionCreate(APIModel):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)
    rotation: float = Field(default=0.0, allow_inf_nan=False)
    source_text: str = ""
    translation_text: str = ""
    type: Literal[
        "dialogue",
        "narration",
        "sound_effect",
        "title",
        "ruby",
        "background",
        "unknown",
        "thought",
        "sign",
        "speech",
        "other",
    ] = "dialogue"
    direction: Literal["horizontal", "vertical", "auto"] = "vertical"
    order: int | None = Field(default=None, ge=0)
    paragraph_group_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    ruby_parent_id: UUID | None = None
    content_disposition: (
        Literal["translate", "ignore", "keep-art", "redraw-art", "false-positive"] | None
    ) = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    ignored: bool = False
    confirmed: bool = False
    style: dict[str, Any] = Field(default_factory=dict)
    repair: dict[str, Any] = Field(default_factory=dict)
    expected_image_revision: int | None = Field(default=None, ge=0)
    lineage: MutationLineageContext | None = None

    @field_validator("repair")
    @classmethod
    def validate_repair(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_repair(value)

    @model_validator(mode="after")
    def validate_review_flags(self) -> RegionCreate:
        if self.ignored and self.confirmed:
            raise ValueError("A region cannot be both ignored and confirmed")
        return self


class RegionPatch(APIModel):
    x: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    width: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    height: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    rotation: float | None = Field(default=None, allow_inf_nan=False)
    source_text: str | None = None
    translation_text: str | None = None
    type: (
        Literal[
            "dialogue",
            "narration",
            "sound_effect",
            "title",
            "ruby",
            "background",
            "unknown",
            "thought",
            "sign",
            "speech",
            "other",
        ]
        | None
    ) = None
    direction: Literal["horizontal", "vertical", "auto"] | None = None
    order: int | None = Field(default=None, ge=0)
    paragraph_group_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    ruby_parent_id: UUID | None = None
    content_disposition: (
        Literal["translate", "ignore", "keep-art", "redraw-art", "false-positive"] | None
    ) = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    ignored: bool | None = None
    confirmed: bool | None = None
    style: dict[str, Any] | None = None
    repair: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    expected_image_revision: int | None = Field(default=None, ge=0)
    lineage: MutationLineageContext | None = None

    @field_validator("repair")
    @classmethod
    def validate_repair(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_repair(value) if value is not None else None

    @model_validator(mode="after")
    def validate_review_flags(self) -> RegionPatch:
        if self.ignored is True and self.confirmed is True:
            raise ValueError("A region cannot be both ignored and confirmed")
        return self


class OCRReviewEvidenceOut(APIModel):
    source_mode: Literal["original-attempt", "quality-attempt", "manual-correction"]
    selected_attempt_id: str
    source_text_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    qc_checks: list[str]
    qc_flags: list[str]


class RegionOut(APIModel):
    id: str
    image_id: str
    x: float
    y: float
    width: float
    height: float
    rotation: float
    source_text: str
    translation_text: str
    type: str
    direction: str
    order: int
    paragraph_group_id: str | None
    ruby_parent_id: str | None
    content_disposition: (
        Literal["translate", "ignore", "keep-art", "redraw-art", "false-positive"] | None
    )
    detector_job_item_id: str | None
    detector_candidate_index: int | None
    background_category: (
        Literal[
            "white-solid",
            "black-solid",
            "other-solid",
            "simple-gradient",
            "screentone",
            "complex-lineart",
            "illustration/character",
        ]
        | None
    )
    background_confidence: float | None
    background_rationale_codes: list[str] | None
    background_reviewer: LineageActor | None
    background_generation_id: str | None
    ocr_review: OCRReviewEvidenceOut | None
    ocr_reviewer: LineageActor | None
    ocr_generation_id: str | None
    confidence: float | None
    recognition: dict[str, Any]
    detector_confidence: float | None
    ocr_confidence: float | None
    trust_disposition: Literal["review", "trusted", "ignored"]
    trust_reason: str
    trust_policy_version: int
    ignored: bool
    confirmed: bool
    style: dict[str, Any]
    repair: dict[str, Any]
    ocr_provider: str | None
    translation_provider: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ReadingOrderRequest(APIModel):
    region_ids: list[str] | None = None
    mode: Literal["manga-vertical", "horizontal-ltr"] = "manga-vertical"
    expected_image_revision: int | None = Field(default=None, ge=0)
    lineage: MutationLineageContext | None = None


class RegionDeleteRequest(APIModel):
    expected_revision: int | None = Field(default=None, ge=0)
    expected_image_revision: int | None = Field(default=None, ge=0)
    lineage: MutationLineageContext | None = None


class BackgroundClassificationRequest(APIModel):
    category: Literal[
        "white-solid",
        "black-solid",
        "other-solid",
        "simple-gradient",
        "screentone",
        "complex-lineart",
        "illustration/character",
    ]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    rationale_codes: list[
        Literal[
            "uniform-near-white",
            "uniform-near-black",
            "uniform-other-color",
            "smooth-gradient-continuity",
            "periodic-screentone",
            "structural-lines-cross-region",
            "character-or-illustration-detail",
            "mixed-visual-signals",
        ]
    ] = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    expected_image_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @field_validator("confidence", mode="before")
    @classmethod
    def reject_boolean_confidence(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Background confidence must be a finite number")
        # Python's JSON parser accepts non-standard NaN/Infinity tokens. Feed
        # Pydantic a JSON-safe sentinel so FastAPI can serialize the resulting
        # 422 response instead of echoing a non-finite float into JSON.
        if isinstance(value, float) and not math.isfinite(value):
            return "non-finite-number"
        return value

    @model_validator(mode="after")
    def validate_rationale(self) -> BackgroundClassificationRequest:
        if len(set(self.rationale_codes)) != len(self.rationale_codes):
            raise ValueError("Background rationale codes must not contain duplicates")
        anchor = {
            "white-solid": "uniform-near-white",
            "black-solid": "uniform-near-black",
            "other-solid": "uniform-other-color",
            "simple-gradient": "smooth-gradient-continuity",
            "screentone": "periodic-screentone",
            "complex-lineart": "structural-lines-cross-region",
            "illustration/character": "character-or-illustration-detail",
        }[self.category]
        if anchor not in self.rationale_codes:
            raise ValueError("Background rationale does not support the selected category")
        return self


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


class OCRSourceReviewRequest(APIModel):
    source_text: str = Field(min_length=1, max_length=10_000)
    source_mode: Literal["original-attempt", "quality-attempt", "manual-correction"]
    selected_attempt_id: UUID
    qc_checks: list[
        Literal[
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
    ] = Field(min_length=9, max_length=9)
    expected_revision: int = Field(ge=0)
    expected_image_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @field_validator("source_text")
    @classmethod
    def validate_source_text(cls, value: str) -> str:
        cleaned = value.strip()
        if (
            not cleaned
            or "\ufffd" in cleaned
            or any(ord(character) < 32 and character not in {"\n", "\t"} for character in cleaned)
        ):
            raise ValueError("Trusted OCR source text must contain valid visible characters")
        return cleaned

    @model_validator(mode="after")
    def validate_qc_checks(self) -> OCRSourceReviewRequest:
        if set(self.qc_checks) != OCR_QC_CHECKS or len(set(self.qc_checks)) != len(self.qc_checks):
            raise ValueError("Every required OCR QC check must be acknowledged exactly once")
        return self


class OCRAttemptOut(APIModel):
    id: str
    region_id: str
    generation_id: str
    job_id: str
    job_item_id: str
    input_variant: Literal["original", "quality"]
    parent_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    crop_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    crop_box: dict[str, int]
    provider: str
    model_version: str | None
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str | None
    direction: Literal["horizontal", "vertical"]
    text: str
    text_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    created_at: datetime


class PageGenerationCreate(APIModel):
    run_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    page_generation_id: UUID
    parameter_set_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    parameter_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    restart_from_source: Literal[True]
    source_project_id: UUID
    source_image_id: UUID
    expected_source_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    actor: LineageActor


class PageGenerationOut(APIModel):
    id: str
    run_id: str
    project_id: str
    image_id: str
    restart_from_source: bool
    parameter_set_id: str
    parameter_set_hash: str
    source_project_id: str
    source_image_id: str
    source_checksum: str
    state: Literal["active", "completed", "superseded"]
    next_sequence: int
    actor: LineageActor
    created_at: datetime
    closed_at: datetime | None


class PageLineageEventOut(APIModel):
    id: str
    generation_id: str
    sequence: int
    operation: str
    gate: str | None
    state: Literal["pending", "accepted", "rejected", "blocked", "not-applicable"]
    actor: LineageActor
    input_checksum: str | None
    output_checksum: str | None
    parent_checksum: str | None
    stage: str | None
    provider: str | None
    model_version: str | None
    parameter_hash: str | None
    job_id: str | None
    job_item_id: str | None
    revision_id: str | None
    decision: str | None
    reason: str | None
    git_commit: str | None
    evidence: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class ReconstructionGateRequest(APIModel):
    decision: Literal["no", "yes"]
    reason: Literal[
        "baseline-preserves-original-structure",
        "fine-lines-remain-insufficient",
        "screentone-remains-insufficient",
        "illustration-detail-remains-insufficient",
        "structure-remains-uncertain",
    ]
    observed_quality_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @model_validator(mode="after")
    def validate_decision_reason(self) -> ReconstructionGateRequest:
        baseline_reason = "baseline-preserves-original-structure"
        if self.decision == "no" and self.reason != baseline_reason:
            raise ValueError("A no-reconstruction decision must confirm preserved structure")
        if self.decision == "yes" and self.reason == baseline_reason:
            raise ValueError("A reconstruction-required decision must identify a remaining defect")
        return self


class ReconstructionImportRequest(APIModel):
    profile: Literal["native-reconstruction-v1"]
    runtime: Literal["codex", "cursor"]
    tool: Literal["image_gen", "GenerateImage"]
    provider: Literal["unreported"]
    model_version: Literal["native-image-model-unreported", "auto-native-image-model-unreported"]
    claim_status: Literal["operator-attested-client-supplied-unverified"]
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_event_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    decision_event_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_revision: int = Field(ge=0, strict=True)
    lineage: MutationLineageContext
    lettering_lock: Literal[True] | None = None
    lettering_mask_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def native_identity(self) -> ReconstructionImportRequest:
        expected = {
            "codex": ("image_gen", "native-image-model-unreported"),
            "cursor": ("GenerateImage", "auto-native-image-model-unreported"),
        }[self.runtime]
        if (self.tool, self.model_version) != expected:
            raise ValueError("Native reconstruction identity is inconsistent")
        if (self.lettering_lock is True) != (self.lettering_mask_sha256 is not None):
            raise ValueError("Lettering lock requires a mask digest")
        return self


class ReconstructionCheckResult(APIModel):
    check: Literal[
        "clarity-improved",
        "identity-preserved",
        "expression-preserved",
        "composition-preserved",
        "text-and-sfx-preserved",
        "objects-preserved",
        "no-invented-detail",
        "no-artifacts",
    ]
    passed: bool = Field(strict=True)


class ReconstructionReviewRequest(APIModel):
    candidate_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    observed_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["accept", "reject"]
    checks: list[ReconstructionCheckResult] = Field(min_length=8, max_length=8)
    expected_revision: int = Field(ge=0, strict=True)
    lineage: MutationLineageContext


class TextPresenceGateRequest(APIModel):
    decision: Literal["yes", "no", "uncertain"]
    reason: Literal[
        "processable-text-visible",
        "no-processable-text-visible",
        "visual-evidence-uncertain",
    ]
    evidence: list[
        Literal[
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
        ]
    ] = Field(min_length=1)
    observed_original_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_quality_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @model_validator(mode="after")
    def validate_visual_decision(self) -> TextPresenceGateRequest:
        evidence = set(self.evidence)
        if len(evidence) != len(self.evidence):
            raise ValueError("Text-presence evidence must not contain duplicates")
        if "original-and-quality-compared" not in evidence:
            raise ValueError("Text presence requires an original/quality visual comparison")
        visual_text_evidence = {
            "dialogue-visible",
            "narration-visible",
            "title-visible",
            "sfx-visible",
            "art-lettering-visible",
            "environment-text-visible",
        }
        expected_reason = {
            "yes": "processable-text-visible",
            "no": "no-processable-text-visible",
            "uncertain": "visual-evidence-uncertain",
        }[self.decision]
        if self.reason != expected_reason:
            raise ValueError("Text-presence reason does not match the decision")
        if self.decision == "yes" and not evidence.intersection(visual_text_evidence):
            raise ValueError("A text-present decision requires visible text evidence")
        if self.decision == "no" and "no-processable-text-visible" not in evidence:
            raise ValueError("A no-text decision requires explicit visual no-text evidence")
        if self.decision == "uncertain" and "conflicting-signals" not in evidence:
            raise ValueError("An uncertain decision requires conflicting visual evidence")
        return self


class RegionsGateRequest(APIModel):
    decision: Literal["accept"]
    reason: Literal["all-region-decisions-reviewed"]
    observed_region_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class BackgroundGateRequest(APIModel):
    decision: Literal["accept"]
    reason: Literal["all-eligible-backgrounds-reviewed", "no-eligible-regions"]
    observed_background_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class BackgroundGateContextOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    g4_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    background_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "accepted", "not-applicable"]
    eligible_region_ids: list[str]
    classified_region_ids: list[str]


class OCRGateRequest(APIModel):
    decision: Literal["accept"]
    reason: Literal["all-translatable-source-text-reviewed", "no-translatable-regions"]
    observed_ocr_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class OCRGateContextOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    g5_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    ocr_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "accepted", "not-applicable"]
    eligible_region_ids: list[str]
    attempted_region_ids: list[str]
    reviewed_region_ids: list[str]
    attempts: list[OCRAttemptOut]


class MaskStroke(APIModel):
    mode: Literal["add", "erase"]
    radius: float = Field(gt=0, le=512, allow_inf_nan=False)
    points: list[tuple[float, float]] = Field(min_length=1, max_length=4096)


class MaskEdits(APIModel):
    version: Literal[1]
    strokes: list[MaskStroke] = Field(max_length=256)


class MaskRegionRecipe(APIModel):
    region_id: str = Field(min_length=1, max_length=36)
    mask_mode: Literal["region", "text", "manual"]
    polygon: list[tuple[float, float]] | None = Field(default=None, min_length=3, max_length=4096)
    padding: int = Field(ge=0, le=512)
    dilation: int = Field(ge=0, le=128)
    feather: int = Field(ge=0, le=128)
    polarity: Literal["auto", "dark", "light"]
    mask_edits: MaskEdits

    @model_validator(mode="after")
    def validate_mask_recipe(self) -> MaskRegionRecipe:
        if self.mask_mode == "manual" and not self.mask_edits.strokes:
            raise ValueError("Manual mask mode requires at least one brush or erase stroke")
        return self


class MaskDraftRequest(APIModel):
    regions: list[MaskRegionRecipe] = Field(max_length=4096)
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @model_validator(mode="after")
    def unique_regions(self) -> MaskDraftRequest:
        ids = [region.region_id for region in self.regions]
        if len(ids) != len(set(ids)):
            raise ValueError("Mask draft regions must not repeat region ids")
        return self


class MaskCheckResult(APIModel):
    check: str = Field(min_length=1, max_length=80)
    passed: bool


class MaskGateRequest(APIModel):
    decision: Literal["accept", "reject", "not-applicable"]
    reason: Literal[
        "complete-and-no-collateral",
        "coverage-incomplete",
        "collateral-damage",
        "coverage-and-collateral-failed",
        "no-eligible-regions",
    ]
    selected_artifact_id: str | None = None
    observed_mask_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    coverage_checks: list[MaskCheckResult] = Field(max_length=5)
    collateral_checks: list[MaskCheckResult] = Field(max_length=5)
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class MaskDraftOut(APIModel):
    revision: int
    state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    regions: list[MaskRegionRecipe]


class MaskArtifactOut(APIModel):
    artifact_id: str
    sequence: int
    job_id: str
    job_item_id: str
    parent_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    mask_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    render_scale: float = Field(gt=0)
    provider: str
    model_version: str
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonzero_pixel_count: int = Field(gt=0)
    bbox: dict[str, int]
    created_at: datetime


class MaskReviewOut(APIModel):
    id: str
    state: Literal["accepted", "rejected", "not-applicable"]
    reason: str
    artifact_id: str | None
    mask_checksum: str | None
    coverage_checks: list[MaskCheckResult]
    collateral_checks: list[MaskCheckResult]
    reviewer: LineageActor
    created_at: datetime


class MaskGateContextOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    g6_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    mask_state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "accepted", "rejected", "not-applicable"]
    eligible_region_ids: list[str]
    ruby_region_ids_by_primary: dict[str, list[str]]
    draft: MaskDraftOut
    artifacts: list[MaskArtifactOut]
    selected_artifact_id: str | None
    review: MaskReviewOut | None


class CleanPlateRouteEntry(APIModel):
    region_id: str
    background_category: Literal[
        "white-solid",
        "black-solid",
        "other-solid",
        "simple-gradient",
        "screentone",
        "complex-lineart",
        "illustration/character",
    ]
    route: Literal[
        "deterministic-solid",
        "controlled-gradient",
        "screentone-preserving",
        "ai-inpaint-redraw",
        "classical-fallback",
    ]
    origin_kind: Literal["deterministic", "ai", "classical"]
    provider: str
    model_version: str
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CleanPlateLineageAncestry(APIModel):
    reference_generation_id: str
    origin_kind: Literal[
        "direct-ai",
        "ai-derived",
        "classical",
        "deterministic-postprocess",
        "mixed",
    ]
    provider_ids: list[str]
    lineage: dict[str, Any] | None = None


class CleanPlateLayeredLineageInput(APIModel):
    reference_id: str
    reference_image_id: str
    reference_candidate_id: str
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    legacy_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    mask_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    ancestry: CleanPlateLineageAncestry


class CleanPlateLayeredRouteEntry(APIModel):
    region_id: str
    background_category: Literal[
        "white-solid",
        "black-solid",
        "other-solid",
        "simple-gradient",
        "screentone",
        "complex-lineart",
        "illustration/character",
    ]
    route: Literal["layered-structure"]
    origin_kind: Literal["classical", "mixed"]
    provider: Literal["opencv"]
    model_version: Literal["layered-structure-guide-v1"]
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_inputs: list[CleanPlateLayeredLineageInput] = Field(min_length=1, max_length=16)


class CleanPlateRouteSummary(APIModel):
    region_id: str
    background_category: str
    default_route: str


class CleanPlateCheckResult(APIModel):
    check: str = Field(min_length=1, max_length=80)
    passed: bool


class CleanPlateCandidateReviewOut(APIModel):
    id: str
    state: Literal["accepted", "rejected"]
    reason: str
    checks: list[CleanPlateCheckResult]
    reviewer: LineageActor
    created_at: datetime


class CleanPlateCandidateOut(APIModel):
    candidate_id: str
    sequence: int = Field(ge=1)
    job_id: str
    job_item_id: str
    parent_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    background_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    mask_artifact_id: str
    mask_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_manifest: list[CleanPlateRouteEntry | CleanPlateLayeredRouteEntry]
    route_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin_kind: Literal["deterministic", "ai", "classical", "mixed"]
    provider_ids: list[str]
    model_versions: list[str]
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    render_scale: float = Field(gt=0)
    outside_mask_change_count: int = Field(ge=0)
    anomalies: list[str]
    completed: bool
    review: CleanPlateCandidateReviewOut | None
    created_at: datetime


class CleanPlateGateContextOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    g7_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    background_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    mask_artifact_id: str | None
    mask_checksum: str | None
    clean_plate_state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "accepted", "rejected", "not-applicable"]
    routes: list[CleanPlateRouteSummary]
    candidates: list[CleanPlateCandidateOut]
    accepted_candidate_id: str | None
    fallback_enabled: bool
    fallback_allowed: bool


class CleanPlateFallbackRequest(APIModel):
    enabled: bool
    reason: Literal["all-ai-candidates-rejected", "resume-ai-candidates"]
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class CleanPlateGateRequest(APIModel):
    decision: Literal["accept", "reject", "not-applicable"]
    reason: Literal[
        "clean-plate-complete",
        "residual-text-readable",
        "hole-or-block",
        "blur-band",
        "repeated-texture",
        "background-discontinuous",
        "structure-damaged",
        "outside-mask-changed",
        "multiple-visual-failures",
        "no-clean-plate-required",
    ]
    candidate_id: str | None = None
    observed_candidate_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_width: int | None = Field(default=None, gt=0)
    observed_height: int | None = Field(default=None, gt=0)
    checks: list[CleanPlateCheckResult] = Field(max_length=7)
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class CloudFullPageCheckResult(APIModel):
    check: Literal[
        "full-page-fidelity",
        "no-new-text",
        "no-new-objects",
        "unrelated-content-preserved",
        "target-source-text-unreadable",
        "no-white-or-gray-hole",
        "no-blur-band",
        "no-repeated-texture",
        "background-continuous",
        "structure-preserved",
    ]
    passed: bool


class CloudFullPageReviewRequest(APIModel):
    candidate_id: str
    observed_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=120)
    checks: list[CloudFullPageCheckResult] = Field(min_length=10, max_length=10)
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class TranslationCheckResult(APIModel):
    check: Literal[
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
    ]
    passed: bool


class TranslationCandidateRevisionRequest(APIModel):
    region_id: str
    translation_text: str = Field(max_length=20_000)
    origin_kind: Literal["manual", "agent", "dictionary"]
    observed_g8_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_source_text_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_context_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_translation_state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class TranslationCandidateReviewRequest(APIModel):
    decision: Literal["accept", "reject"]
    reason: Literal[
        "translation-reviewed",
        "empty-output",
        "non-chinese-output",
        "forbidden-template",
        "source-copy",
        "japanese-residual",
        "generic-duplicate",
        "source-inconsistent",
        "context-inconsistent",
        "source-noise-hallucination",
        "multiple-qc-failures",
    ]
    observed_candidate_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_source_text_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_context_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_g8_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: list[TranslationCheckResult] = Field(min_length=10, max_length=10)
    qc_flags: list[
        Literal[
            "none",
            "empty-output",
            "non-chinese-output",
            "forbidden-template",
            "source-copy",
            "japanese-residual",
            "generic-duplicate",
            "source-inconsistent",
            "context-inconsistent",
            "source-noise-hallucination",
        ]
    ] = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @model_validator(mode="after")
    def exact_translation_qc(self) -> TranslationCandidateReviewRequest:
        names = [entry.check for entry in self.checks]
        if len(set(names)) != 10:
            raise ValueError("Every translation QC check is required exactly once")
        if len(set(self.qc_flags)) != len(self.qc_flags):
            raise ValueError("Translation QC flags must not repeat")
        if "none" in self.qc_flags and len(self.qc_flags) != 1:
            raise ValueError("The none QC flag must be exclusive")
        return self


class TranslationGateRequest(APIModel):
    decision: Literal["accept", "not-applicable"]
    observed_translation_state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext


class TranslationGateContextOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    g8_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    clean_plate_candidate_id: str | None
    clean_plate_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_language: Literal["zh-CN", "zh", "zh-Hans"]
    translation_state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "accepted", "not-applicable"]
    terminal_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    eligible_regions: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    accepted_candidate_ids_by_region: dict[str, str]
    reviewed_region_count: int = Field(ge=0)


class TypesetVisualCheckResult(APIModel):
    check: Literal[
        "original-clean-final-compared",
        "translation-complete",
        "hierarchy-reading-order-preserved",
        "key-art-unobstructed",
        "typography-source-matched",
        "bubble-contained",
        "art-lettering-composition-matched",
        "overflow-free",
    ]
    passed: bool


class TypesetCandidateReviewRequest(APIModel):
    decision: Literal["accept", "reject"]
    reason: Literal[
        "typeset-reviewed",
        "original-clean-final-compared",
        "translation-complete",
        "hierarchy-reading-order-preserved",
        "key-art-unobstructed",
        "typography-source-matched",
        "bubble-contained",
        "art-lettering-composition-matched",
        "overflow-free",
        "multiple-visual-failures",
    ]
    observed_candidate_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_route_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_style_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_layout_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_translation_terminal_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_clean_plate_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_width: int = Field(ge=1)
    observed_height: int = Field(ge=1)
    observed_render_scale: float = Field(gt=0, le=4, allow_inf_nan=False)
    checks: list[TypesetVisualCheckResult] = Field(min_length=8, max_length=8)
    expected_revision: int = Field(ge=0)
    lineage: MutationLineageContext

    @model_validator(mode="after")
    def exact_typeset_checks(self) -> TypesetCandidateReviewRequest:
        names = [entry.check for entry in self.checks]
        if len(set(names)) != 8:
            raise ValueError("Every G10 visual check is required exactly once")
        failed = [entry.check for entry in self.checks if not entry.passed]
        if self.decision == "accept":
            if failed or self.reason != "typeset-reviewed":
                raise ValueError("G10 acceptance requires all checks and typeset-reviewed")
        elif not failed:
            raise ValueError("G10 rejection requires at least one failed check")
        elif self.reason == "multiple-visual-failures":
            if len(failed) < 2:
                raise ValueError("Multiple visual failures requires at least two failed checks")
        elif self.reason not in failed:
            raise ValueError("G10 rejection reason must identify a failed check")
        return self


class TypesetGateContextOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    g9_terminal_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_state_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    clean_plate_candidate_id: str | None
    clean_plate_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "accepted"]
    terminal_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidates: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    route_manifest: list[dict[str, Any]]
    route_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    style_defaults: dict[str, Any]
    available_fonts: list[dict[str, Any]]
    available_display_fonts: list[dict[str, Any]]
    art_lettering_capability: dict[str, Any]
    retry_region_styles: dict[str, dict[str, Any]]


class PageGateResultOut(APIModel):
    image_id: str
    image_revision: int
    generation_id: str
    next_sequence: int
    event: PageLineageEventOut


class JobLineagePage(APIModel):
    image_id: UUID
    page_generation_id: UUID
    expected_sequence: int = Field(ge=1)


class JobLineageContext(APIModel):
    run_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    actor: LineageActor
    pages: list[JobLineagePage] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_page_bindings(self) -> JobLineageContext:
        image_ids = [page.image_id for page in self.pages]
        generation_ids = [page.page_generation_id for page in self.pages]
        if len(set(image_ids)) != len(image_ids):
            raise ValueError("Lineage page bindings must not repeat image ids")
        if len(set(generation_ids)) != len(generation_ids):
            raise ValueError("Lineage page bindings must not repeat generation ids")
        return self


class JobRequest(APIModel):
    image_ids: list[str] = Field(default_factory=list)
    region_ids: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    lineage: JobLineageContext | None = None


class JobItemOut(APIModel):
    id: str
    image_id: str | None
    position: int
    status: str
    progress: float
    error: str | None
    output: dict[str, Any]


class JobOut(APIModel):
    id: str
    project_id: str
    kind: str
    status: str
    progress: float
    total: int
    completed: int
    error: str | None
    items: list[JobItemOut]
    created_at: datetime
    updated_at: datetime


class HealthOut(APIModel):
    status: Literal["ok", "degraded"]
    version: str
    database: str
    queue: str
    lan_access: bool = False
    companion_url: str | None = None
    bundled_models: dict[str, Any] | None = None


class ConfigOut(APIModel):
    providers: dict[str, Any]
    capabilities: dict[str, Any]


class OpenAISessionConfig(APIModel):
    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    model: str | None = None


def _validate_repair(value: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the versioned repair fields accepted from the editor."""
    allowed_fields = {
        "contextPadding",
        "detectedTextCandidate",
        "detectorGenerated",
        "dilation",
        "feather",
        "fillColor",
        "inpainterProvider",
        "maskEdits",
        "maskMode",
        "maskPadding",
        "maskPolygon",
        "method",
        "ocrAttemptCount",
        "ocrInputVariant",
        "padding",
        "radius",
        "textPolarity",
    }
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"Unknown repair fields: {', '.join(unknown_fields)}")
    tombstones = {field for field, field_value in value.items() if field_value is None}
    normalized = {
        field: field_value for field, field_value in value.items() if field_value is not None
    }
    integer_limits = {
        "padding": 512,
        "maskPadding": 512,
        "dilation": 128,
        "feather": 128,
        "contextPadding": 4096,
    }
    for field, maximum in integer_limits.items():
        if field not in normalized:
            continue
        field_value = normalized[field]
        if type(field_value) is not int or not 0 <= field_value <= maximum:
            raise ValueError(f"{field} must be an integer between 0 and {maximum}")
    if "radius" in normalized:
        radius = normalized["radius"]
        if (
            isinstance(radius, bool)
            or not isinstance(radius, (int, float))
            or not math.isfinite(float(radius))
            or not 0 < float(radius) <= 256
        ):
            raise ValueError("radius must be a finite number between 0 and 256")
        normalized["radius"] = float(radius)
    if "maskMode" in normalized:
        mask_mode = normalized["maskMode"]
        if not isinstance(mask_mode, str) or mask_mode not in {"region", "text", "manual"}:
            raise ValueError("maskMode must be region, text, or manual")
    if "textPolarity" in normalized:
        text_polarity = normalized["textPolarity"]
        if not isinstance(text_polarity, str) or text_polarity not in {
            "auto",
            "dark",
            "light",
        }:
            raise ValueError("textPolarity must be auto, dark, or light")
    if "method" in normalized:
        method = normalized["method"]
        aliases = {
            "telea": "telea",
            "ns": "navier-stokes",
            "navier-stokes": "navier-stokes",
            "navier_stokes": "navier-stokes",
            "solid": "solid",
            "screentone": "screentone",
        }
        if not isinstance(method, str) or method not in aliases:
            raise ValueError("method must be telea, navier-stokes, solid, or screentone")
        normalized["method"] = aliases[method]
    if "fillColor" in normalized:
        fill_color = normalized["fillColor"]
        if not isinstance(fill_color, str) or not fill_color or len(fill_color) > 64:
            raise ValueError("fillColor must be a valid color")
        try:
            channels = ImageColor.getrgb(fill_color)
        except ValueError as error:
            raise ValueError("fillColor must be a valid color") from error
        if len(channels) != 3:
            raise ValueError("fillColor must be an opaque RGB color")
    if "detectorGenerated" in normalized and type(normalized["detectorGenerated"]) is not bool:
        raise ValueError("detectorGenerated must be a boolean")
    if "maskPolygon" in normalized:
        polygon = normalized["maskPolygon"]
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= 4096:
            raise ValueError("maskPolygon must contain between 3 and 4096 points")
        normalized_polygon: list[list[float]] = []
        for point in polygon:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Each maskPolygon point must be a two-number list")
            if any(
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(float(coordinate))
                or float(coordinate) < 0
                for coordinate in point
            ):
                raise ValueError("maskPolygon points must use non-negative finite coordinates")
            normalized_polygon.append([float(point[0]), float(point[1])])
        normalized["maskPolygon"] = normalized_polygon
    if "inpainterProvider" in normalized:
        provider = normalized["inpainterProvider"]
        aliases = {
            "opencv": "opencv",
            "opencv-inpaint": "opencv",
            "lama": "lama-onnx",
            "lama-onnx": "lama-onnx",
        }
        if not isinstance(provider, str) or provider not in aliases:
            raise ValueError("inpainterProvider must be opencv or lama-onnx")
        normalized["inpainterProvider"] = aliases[provider]
    if "maskEdits" not in normalized:
        return {**normalized, **dict.fromkeys(tombstones)}
    edits = normalized["maskEdits"]
    if type(edits) is not dict or set(edits) != {"version", "strokes"}:
        raise ValueError("maskEdits must contain exactly version and strokes")
    if type(edits["version"]) is not int or edits["version"] != 1:
        raise ValueError("maskEdits version must be 1")
    strokes = edits["strokes"]
    if not isinstance(strokes, list):
        raise ValueError("maskEdits strokes must be a list")
    if len(strokes) > 256:
        raise ValueError("maskEdits must contain at most 256 strokes")
    normalized_strokes: list[dict[str, Any]] = []
    total_points = 0
    for stroke in strokes:
        if type(stroke) is not dict or set(stroke) != {"mode", "radius", "points"}:
            raise ValueError("Each mask edit stroke must contain exactly mode, radius, and points")
        mode = stroke["mode"]
        if mode not in {"add", "erase"}:
            raise ValueError("Mask edit stroke mode must be add or erase")
        radius = stroke["radius"]
        if (
            isinstance(radius, bool)
            or not isinstance(radius, (int, float))
            or not math.isfinite(float(radius))
            or float(radius) <= 0
            or float(radius) > 512
        ):
            raise ValueError("Mask edit stroke radius must be between 0 and 512")
        points = stroke["points"]
        if not isinstance(points, list) or not points:
            raise ValueError("Mask edit stroke points must be a non-empty list")
        if len(points) > 4096:
            raise ValueError("Each mask edit stroke must contain at most 4096 points")
        total_points += len(points)
        if total_points > 16384:
            raise ValueError("maskEdits must contain at most 16384 points")
        normalized_points: list[list[float]] = []
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Each mask edit point must be a two-number list")
            if any(
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(float(coordinate))
                or float(coordinate) < 0
                for coordinate in point
            ):
                raise ValueError("Mask edit points must use non-negative finite coordinates")
            normalized_points.append([float(point[0]), float(point[1])])
        normalized_strokes.append(
            {"mode": mode, "radius": float(radius), "points": normalized_points}
        )
    normalized["maskEdits"] = {"version": 1, "strokes": normalized_strokes}
    return {**normalized, **dict.fromkeys(tombstones)}
