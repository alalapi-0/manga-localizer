from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


G0_REVISION_NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS revisions_g0_no_update
BEFORE UPDATE ON revisions
WHEN EXISTS (
    SELECT 1 FROM page_lineage_events AS event
    WHERE event.revision_id = OLD.id
      AND event.gate = 'G0_identity'
)
BEGIN
    SELECT RAISE(ABORT, 'G0 lineage revisions are append-only');
END
"""

G0_REVISION_NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS revisions_g0_no_delete
BEFORE DELETE ON revisions
WHEN EXISTS (
    SELECT 1 FROM page_lineage_events AS event
    WHERE event.revision_id = OLD.id
      AND event.gate = 'G0_identity'
)
BEGIN
    SELECT RAISE(ABORT, 'G0 lineage revisions are append-only');
END
"""

PAGE_LINEAGE_EVENTS_NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS page_lineage_events_no_update
BEFORE UPDATE ON page_lineage_events
BEGIN
    SELECT RAISE(ABORT, 'page lineage events are append-only');
END
"""

PAGE_LINEAGE_EVENTS_NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS page_lineage_events_no_delete
BEFORE DELETE ON page_lineage_events
BEGIN
    SELECT RAISE(ABORT, 'page lineage events are append-only');
END
"""


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    root_path: Mapped[str] = mapped_column(Text)
    input_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=2)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    images: Mapped[list[ImageAsset]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ImageAsset.relative_path"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ImageAsset(Base):
    __tablename__ = "images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    relative_path: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(40), default="browser-upload")
    input_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(100), default="image/png")
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=lambda: {
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
    # Internal generation evidence for the current clean plate. This is kept
    # separate from ``status`` because status is a public presentation/cache
    # payload and must never authorize the strict AI-clean-plate gate.
    inpaint_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Private, page-scoped evidence for an explicitly reviewed classical fallback.
    # Public APIs expose only a narrow projection; the complete integrity anchors
    # remain in the project database.
    inpaint_classical_approval: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Private, generation-scoped operator decisions for AI candidates. Candidate
    # identity is always derived server-side from the selected provenance.
    inpaint_ai_candidate_reviews: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    processing_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="images")
    regions: Mapped[list[TextRegion]] = relationship(
        back_populates="image", cascade="all, delete-orphan", order_by="TextRegion.reading_order"
    )


class ImportBoundary(Base):
    """Machine-local read-only boundary selected through trusted-path import."""

    __tablename__ = "import_boundaries"
    __table_args__ = (UniqueConstraint("project_id", "path", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TextRegion(Base):
    __tablename__ = "text_regions"
    __table_args__ = (
        CheckConstraint(
            "region_disposition IS NULL OR region_disposition IN "
            "('translate', 'ignore', 'keep-art', 'redraw-art', 'false-positive')",
            name="ck_text_region_disposition",
        ),
        CheckConstraint(
            "detector_candidate_index IS NULL OR detector_candidate_index >= 0",
            name="ck_text_region_detector_candidate_index",
        ),
        CheckConstraint(
            "(detector_job_item_id IS NULL AND detector_candidate_index IS NULL) OR "
            "(detector_job_item_id IS NOT NULL AND detector_candidate_index IS NOT NULL)",
            name="ck_text_region_detector_identity_pair",
        ),
        CheckConstraint(
            "background_category IS NULL OR background_category IN "
            "('white-solid', 'black-solid', 'other-solid', 'simple-gradient', "
            "'screentone', 'complex-lineart', 'illustration/character')",
            name="ck_text_region_background_category",
        ),
        CheckConstraint(
            "background_confidence IS NULL OR "
            "(background_confidence >= 0 AND background_confidence <= 1)",
            name="ck_text_region_background_confidence",
        ),
        CheckConstraint(
            "(background_category IS NULL AND background_confidence IS NULL AND "
            "background_rationale_codes IS NULL AND background_reviewer IS NULL AND "
            "background_generation_id IS NULL) OR "
            "(background_category IS NOT NULL AND background_confidence IS NOT NULL AND "
            "background_rationale_codes IS NOT NULL AND background_reviewer IS NOT NULL AND "
            "background_generation_id IS NOT NULL)",
            name="ck_text_region_background_bundle",
        ),
        CheckConstraint(
            "(ocr_review IS NULL AND ocr_reviewer IS NULL AND ocr_generation_id IS NULL) OR "
            "(ocr_review IS NOT NULL AND ocr_reviewer IS NOT NULL AND "
            "ocr_generation_id IS NOT NULL)",
            name="ck_text_region_ocr_review_bundle",
        ),
        Index(
            "uq_text_region_detector_candidate",
            "detector_job_item_id",
            "detector_candidate_index",
            unique=True,
            sqlite_where=text("detector_job_item_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"), index=True)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    rotation: Mapped[float] = mapped_column(Float, default=0.0)
    source_text: Mapped[str] = mapped_column(Text, default="")
    translation_text: Mapped[str] = mapped_column(Text, default="")
    region_type: Mapped[str] = mapped_column(String(50), default="dialogue")
    direction: Mapped[str] = mapped_column(String(20), default="vertical")
    reading_order: Mapped[int] = mapped_column(Integer, default=0)
    # G4 semantic structure remains NULL for legacy rows.  A current page
    # generation must explicitly populate and accept these values before OCR.
    paragraph_group_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ruby_parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("text_regions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    content_disposition: Mapped[str | None] = mapped_column(
        "region_disposition", String(32), nullable=True
    )
    # Detector ownership is server-written provenance.  The pair is stable for
    # one job item/candidate and lets crash recovery replace, rather than mix,
    # an earlier unreviewed publication from the same item.
    detector_job_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    detector_candidate_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # G5 classification is an all-or-null evidence bundle. The reviewer and
    # generation are server-owned so confidence can never silently become an
    # acceptance decision or lose the lineage under which it was reviewed.
    background_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    background_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    background_rationale_codes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    background_reviewer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    background_generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # G6 trust is separate from the legacy recognition/confirmed fields.  The
    # review stores only checksum/QC metadata; OCR attempts are immutable rows
    # below, and the reviewer plus generation remain server-owned.
    ocr_review: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ocr_reviewer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ocr_generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    style: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    repair: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recognition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ocr_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    translation_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    image: Mapped[ImageAsset] = relationship(back_populates="regions")


class Revision(Base):
    __tablename__ = "revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(20))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    project_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED.value, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Private, structured provenance binding. It is deliberately separate from
    # provider/options data so a job cannot accidentally acquire or lose page
    # lineage through ordinary option normalization.
    lineage_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="jobs")
    items: Mapped[list[JobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobItem.position"
    )


class JobItem(Base):
    __tablename__ = "job_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    image_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    region_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED.value)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="items")


class PageGeneration(Base):
    """A fresh, source-anchored processing lineage for one page."""

    __tablename__ = "page_generations"
    __table_args__ = (
        UniqueConstraint("run_id", "image_id", name="uq_page_generation_run_image"),
        CheckConstraint(
            "state IN ('active', 'completed', 'superseded')",
            name="ck_page_generation_state",
        ),
        Index(
            "uq_page_generation_active_image",
            "image_id",
            unique=True,
            sqlite_where=text("state = 'active'"),
        ),
    )

    # The caller supplies the UUID so the external per-page ledger and the
    # project database share one stable identity.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"), index=True)
    restart_from_source: Mapped[bool] = mapped_column(Boolean)
    parameter_set_id: Mapped[str] = mapped_column(String(128))
    parameter_set_hash: Mapped[str] = mapped_column(String(64))
    source_project_id: Mapped[str] = mapped_column(String(36))
    source_image_id: Mapped[str] = mapped_column(String(36))
    source_checksum: Mapped[str] = mapped_column(String(64))
    source_relative_path: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="active", index=True)
    next_sequence: Mapped[int] = mapped_column(Integer, default=1)
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_source: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PageLineageEvent(Base):
    """Append-only, checksum-bound evidence for a page generation mutation."""

    __tablename__ = "page_lineage_events"
    __table_args__ = (
        UniqueConstraint("generation_id", "sequence", name="uq_page_lineage_sequence"),
        CheckConstraint(
            "state IN ('pending', 'accepted', 'rejected', 'blocked', 'not-applicable')",
            name="ck_page_lineage_event_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(80))
    gate: Mapped[str | None] = mapped_column(String(40), nullable=True)
    state: Mapped[str] = mapped_column(String(20))
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_source: Mapped[str] = mapped_column(String(20))
    input_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parameter_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    job_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    decision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegionOCRAttempt(Base):
    """Immutable G6 OCR evidence for one region and one input crop."""

    __tablename__ = "region_ocr_attempts"
    __table_args__ = (
        UniqueConstraint(
            "job_item_id",
            "region_id",
            "input_variant",
            name="uq_region_ocr_attempt_job_region_variant",
        ),
        CheckConstraint(
            "input_variant IN ('original', 'quality')",
            name="ck_region_ocr_attempt_input_variant",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_region_ocr_attempt_confidence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    region_id: Mapped[str] = mapped_column(
        ForeignKey("text_regions.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    job_item_id: Mapped[str] = mapped_column(
        ForeignKey("job_items.id", ondelete="RESTRICT"), index=True
    )
    input_variant: Mapped[str] = mapped_column(String(20))
    parent_checksum: Mapped[str] = mapped_column(String(64))
    crop_checksum: Mapped[str] = mapped_column(String(64))
    crop_box: Mapped[dict[str, int]] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parameter_hash: Mapped[str] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(40), nullable=True)
    direction: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    text_checksum: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageMaskDraft(Base):
    """Mutable G7 recipe.  Raster outputs are immutable PageMaskArtifact rows."""

    __tablename__ = "page_mask_drafts"

    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="CASCADE"), primary_key=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"), index=True)
    parent_checksum: Mapped[str] = mapped_column(String(64))
    quality_checksum: Mapped[str] = mapped_column(String(64))
    recipe: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    state_checksum: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PageMaskArtifact(Base):
    """Append-only identity and raster facts for one actual G7 mask."""

    __tablename__ = "page_mask_artifacts"
    __table_args__ = (
        UniqueConstraint("job_item_id", name="uq_page_mask_artifact_job_item"),
        UniqueConstraint("generation_id", "sequence", name="uq_page_mask_artifact_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    job_item_id: Mapped[str] = mapped_column(
        ForeignKey("job_items.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    parent_checksum: Mapped[str] = mapped_column(String(64))
    quality_checksum: Mapped[str] = mapped_column(String(64))
    draft_checksum: Mapped[str] = mapped_column(String(64))
    mask_checksum: Mapped[str] = mapped_column(String(64), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    render_scale: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(128))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    nonzero_pixels: Mapped[int] = mapped_column(Integer)
    bbox: Mapped[dict[str, int]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageMaskReview(Base):
    """Append-only G7 decision bound to an exact raster artifact and actor."""

    __tablename__ = "page_mask_reviews"
    __table_args__ = (
        UniqueConstraint("generation_id", "sequence", name="uq_mask_review_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_mask_artifacts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(120))
    mask_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coverage_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    collateral_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reviewer: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageCleanPlateCandidate(Base):
    """Append-only G8 clean-plate candidate bound to the accepted G7 raster."""

    __tablename__ = "page_clean_plate_candidates"
    __table_args__ = (
        UniqueConstraint("job_item_id", name="uq_clean_plate_candidate_job_item"),
        UniqueConstraint("generation_id", "sequence", name="uq_clean_plate_candidate_sequence"),
        CheckConstraint(
            "origin_kind IN ('deterministic', 'ai', 'classical', 'mixed')",
            name="ck_clean_plate_candidate_origin",
        ),
        CheckConstraint(
            "outside_mask_change_count = 0",
            name="ck_clean_plate_candidate_outside_mask",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    job_item_id: Mapped[str] = mapped_column(
        ForeignKey("job_items.id", ondelete="RESTRICT"), unique=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    parent_checksum: Mapped[str] = mapped_column(String(64))
    quality_checksum: Mapped[str] = mapped_column(String(64))
    background_checksum: Mapped[str] = mapped_column(String(64))
    mask_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("page_mask_artifacts.id", ondelete="RESTRICT"), index=True
    )
    mask_checksum: Mapped[str] = mapped_column(String(64), index=True)
    route_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    route_checksum: Mapped[str] = mapped_column(String(64))
    origin_kind: Mapped[str] = mapped_column(String(20))
    provider_ids: Mapped[list[str]] = mapped_column(JSON)
    model_versions: Mapped[list[str]] = mapped_column(JSON)
    parameter_hash: Mapped[str] = mapped_column(String(64))
    candidate_checksum: Mapped[str] = mapped_column(String(64), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    render_scale: Mapped[float] = mapped_column(Float)
    outside_mask_change_count: Mapped[int] = mapped_column(Integer)
    anomalies: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageCleanPlateReview(Base):
    """One immutable visual conclusion for one exact G8 candidate."""

    __tablename__ = "page_clean_plate_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_clean_plate_review_candidate"),
        UniqueConstraint("generation_id", "sequence", name="uq_clean_plate_review_sequence"),
        CheckConstraint(
            "state IN ('accepted', 'rejected', 'not-applicable')",
            name="ck_clean_plate_review_state",
        ),
        Index(
            "uq_clean_plate_terminal_generation",
            "generation_id",
            unique=True,
            sqlite_where=text("state IN ('accepted', 'not-applicable')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_clean_plate_candidates.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(80))
    parent_checksum: Mapped[str] = mapped_column(String(64))
    candidate_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mask_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reviewer: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageCloudFullPageCandidate(Base):
    """Append-only evidence for one externally rendered whole-page G8 candidate."""

    __tablename__ = "page_cloud_full_page_candidates"
    __table_args__ = (
        UniqueConstraint("job_item_id", name="uq_cloud_full_page_candidate_job_item"),
        UniqueConstraint("generation_id", "sequence", name="uq_cloud_full_page_candidate_seq"),
        UniqueConstraint(
            "generation_id", "invocation_id", name="uq_cloud_full_page_candidate_invocation"
        ),
        CheckConstraint(
            "route_profile = 'cloud-full-page-repair-v1'",
            name="ck_cloud_full_page_candidate_profile",
        ),
        CheckConstraint(
            "raw_width > 0 AND raw_height > 0 AND normalized_width > 0 AND normalized_height > 0",
            name="ck_cloud_full_page_candidate_grid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    job_item_id: Mapped[str] = mapped_column(
        ForeignKey("job_items.id", ondelete="RESTRICT"), unique=True, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), unique=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    route_profile: Mapped[str] = mapped_column(String(64))
    invocation_id: Mapped[str] = mapped_column(String(128))
    parent_checksum: Mapped[str] = mapped_column(String(64))
    legacy_state_checksum: Mapped[str] = mapped_column(String(64))
    project_checksum: Mapped[str] = mapped_column(String(64))
    source_checksum: Mapped[str] = mapped_column(String(64))
    quality_checksum: Mapped[str] = mapped_column(String(64))
    background_checksum: Mapped[str] = mapped_column(String(64))
    mask_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("page_mask_artifacts.id", ondelete="RESTRICT"), index=True
    )
    mask_checksum: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    tool: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    ordered_input_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    ordered_input_digest: Mapped[str] = mapped_column(String(64))
    raw_checksum: Mapped[str] = mapped_column(String(64))
    raw_relative_path: Mapped[str] = mapped_column(Text)
    raw_media_type: Mapped[str] = mapped_column(String(100))
    raw_width: Mapped[int] = mapped_column(Integer)
    raw_height: Mapped[int] = mapped_column(Integer)
    normalized_checksum: Mapped[str] = mapped_column(String(64), index=True)
    normalized_relative_path: Mapped[str] = mapped_column(Text)
    normalized_media_type: Mapped[str] = mapped_column(String(100))
    normalized_width: Mapped[int] = mapped_column(Integer)
    normalized_height: Mapped[int] = mapped_column(Integer)
    normalization_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    normalization_digest: Mapped[str] = mapped_column(String(64))
    delta_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    delta_digest: Mapped[str] = mapped_column(String(64))
    route_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    route_checksum: Mapped[str] = mapped_column(String(64))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    state_checksum: Mapped[str] = mapped_column(String(64))
    ancestry: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def candidate_checksum(self) -> str:
        """Compatibility projection used by route-aware downstream bindings."""
        return self.normalized_checksum


class PageCloudFullPageReview(Base):
    """One immutable ten-check conclusion for a cloud whole-page candidate."""

    __tablename__ = "page_cloud_full_page_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_cloud_full_page_review_candidate"),
        UniqueConstraint("generation_id", "sequence", name="uq_cloud_full_page_review_seq"),
        UniqueConstraint("revision_id", name="uq_cloud_full_page_review_revision"),
        CheckConstraint(
            "state IN ('accepted', 'rejected')", name="ck_cloud_full_page_review_state"
        ),
        Index(
            "uq_cloud_full_page_accepted_generation",
            "generation_id",
            unique=True,
            sqlite_where=text("state = 'accepted'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("page_cloud_full_page_candidates.id", ondelete="RESTRICT"), index=True
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(120))
    parent_checksum: Mapped[str] = mapped_column(String(64))
    candidate_checksum: Mapped[str] = mapped_column(String(64))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    reviewer: Mapped[dict[str, Any]] = mapped_column(JSON)
    state_checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegionTranslationCandidate(Base):
    """Immutable G9 translation candidate; text never enters public lineage events."""

    __tablename__ = "region_translation_candidates"
    __table_args__ = (
        UniqueConstraint(
            "generation_id",
            "region_id",
            "revision_number",
            name="uq_translation_candidate_revision",
        ),
        UniqueConstraint("job_item_id", "region_id", name="uq_translation_candidate_job_region"),
        CheckConstraint(
            "origin_kind IN ('model', 'manual', 'agent', 'dictionary')",
            name="ck_translation_candidate_origin",
        ),
        CheckConstraint("revision_number >= 1", name="ck_translation_candidate_revision_number"),
        CheckConstraint(
            "(job_id IS NULL AND job_item_id IS NULL) OR "
            "(job_id IS NOT NULL AND job_item_id IS NOT NULL)",
            name="ck_translation_candidate_job_bundle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    region_id: Mapped[str] = mapped_column(
        ForeignKey("text_regions.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    revision_number: Mapped[int] = mapped_column(Integer)
    supersedes_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("region_translation_candidates.id", ondelete="RESTRICT"), nullable=True
    )
    origin_kind: Mapped[str] = mapped_column(String(20))
    g8_checksum: Mapped[str] = mapped_column(String(64))
    clean_plate_checksum: Mapped[str] = mapped_column(String(64))
    source_text_checksum: Mapped[str] = mapped_column(String(64))
    source_region_revision: Mapped[int] = mapped_column(Integer)
    context_checksum: Mapped[str] = mapped_column(String(64))
    context_policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(128))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    target_language: Mapped[str] = mapped_column(String(40))
    translation_text: Mapped[str] = mapped_column(Text)
    candidate_checksum: Mapped[str] = mapped_column(String(64), index=True)
    computed_qc_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    job_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_items.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegionTranslationReview(Base):
    """Immutable reviewer decision for one exact G9 candidate."""

    __tablename__ = "region_translation_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_translation_review_candidate"),
        UniqueConstraint("generation_id", "sequence", name="uq_translation_review_sequence"),
        CheckConstraint("state IN ('accepted', 'rejected')", name="ck_translation_review_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    region_id: Mapped[str] = mapped_column(
        ForeignKey("text_regions.id", ondelete="RESTRICT"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("region_translation_candidates.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(80))
    candidate_checksum: Mapped[str] = mapped_column(String(64))
    source_text_checksum: Mapped[str] = mapped_column(String(64))
    context_checksum: Mapped[str] = mapped_column(String(64))
    g8_checksum: Mapped[str] = mapped_column(String(64))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    qc_flags: Mapped[list[str]] = mapped_column(JSON)
    reviewer: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageTranslationReview(Base):
    """Immutable terminal G9 page review."""

    __tablename__ = "page_translation_reviews"
    __table_args__ = (
        UniqueConstraint("generation_id", name="uq_translation_terminal_generation"),
        CheckConstraint(
            "state IN ('accepted', 'not-applicable')", name="ck_translation_terminal_state"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(80))
    g8_checksum: Mapped[str] = mapped_column(String(64))
    translation_state_checksum: Mapped[str] = mapped_column(String(64))
    terminal_checksum: Mapped[str] = mapped_column(String(64), unique=True)
    accepted_candidate_ids: Mapped[list[str]] = mapped_column(JSON)
    reviewer: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageTypesetCandidate(Base):
    """Immutable G10 whole-page typeset candidate."""

    __tablename__ = "page_typeset_candidates"
    __table_args__ = (
        UniqueConstraint("job_item_id", name="uq_typeset_candidate_job_item"),
        UniqueConstraint("generation_id", "sequence", name="uq_typeset_candidate_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    job_item_id: Mapped[str] = mapped_column(
        ForeignKey("job_items.id", ondelete="RESTRICT"), unique=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    parent_checksum: Mapped[str] = mapped_column(String(64))
    g9_terminal_checksum: Mapped[str] = mapped_column(String(64), index=True)
    translation_state_checksum: Mapped[str] = mapped_column(String(64))
    clean_plate_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_clean_plate_candidates.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    cloud_full_page_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("page_cloud_full_page_candidates.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    clean_plate_checksum: Mapped[str] = mapped_column(String(64))
    region_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    route_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    route_checksum: Mapped[str] = mapped_column(String(64))
    style_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    style_checksum: Mapped[str] = mapped_column(String(64))
    layout_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    layout_checksum: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(128))
    parameter_hash: Mapped[str] = mapped_column(String(64))
    candidate_checksum: Mapped[str] = mapped_column(String(64), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    render_scale: Mapped[float] = mapped_column(Float)
    overflow_region_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    anomalies: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageTypesetReview(Base):
    """One immutable visual conclusion for one exact G10 candidate."""

    __tablename__ = "page_typeset_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_typeset_review_candidate"),
        UniqueConstraint("generation_id", "sequence", name="uq_typeset_review_sequence"),
        CheckConstraint("state IN ('accepted', 'rejected')", name="ck_typeset_review_state"),
        Index(
            "uq_typeset_terminal_generation",
            "generation_id",
            unique=True,
            sqlite_where=text("state = 'accepted'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("page_generations.id", ondelete="RESTRICT"), index=True
    )
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id", ondelete="RESTRICT"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("page_typeset_candidates.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(80))
    parent_checksum: Mapped[str] = mapped_column(String(64))
    candidate_checksum: Mapped[str] = mapped_column(String(64))
    route_checksum: Mapped[str] = mapped_column(String(64))
    style_checksum: Mapped[str] = mapped_column(String(64))
    layout_checksum: Mapped[str] = mapped_column(String(64))
    g9_terminal_checksum: Mapped[str] = mapped_column(String(64))
    clean_plate_checksum: Mapped[str] = mapped_column(String(64))
    observed_width: Mapped[int] = mapped_column(Integer)
    observed_height: Mapped[int] = mapped_column(Integer)
    observed_render_scale: Mapped[float] = mapped_column(Float)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    reviewer: Mapped[dict[str, Any]] = mapped_column(JSON)
    terminal_checksum: Mapped[str] = mapped_column(String(64), unique=True)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def create_project_engine(database_path: Path) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    # ``create_all`` intentionally does not alter an existing portable SQLite
    # project. Keep the migration small and idempotent so old projects reopen
    # without requiring a separate migration tool or destructive rebuild.
    columns = {column["name"] for column in inspect(engine).get_columns("text_regions")}
    if "recognition" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE text_regions ADD COLUMN recognition JSON NOT NULL DEFAULT '{}'"
            )
    # G4 fields are deliberately nullable on upgrade.  Historical rows have no
    # trustworthy paragraph, ruby, disposition, or detector-item evidence and
    # must remain visibly unknown instead of receiving a fabricated backfill.
    g4_columns = {
        "paragraph_group_id": "VARCHAR(128)",
        "ruby_parent_id": ("VARCHAR(36) REFERENCES text_regions(id) ON DELETE RESTRICT"),
        "region_disposition": "VARCHAR(32)",
        "detector_job_item_id": "VARCHAR(36)",
        "detector_candidate_index": "INTEGER",
    }
    missing_g4_columns = [name for name in g4_columns if name not in columns]
    if missing_g4_columns:
        with engine.begin() as connection:
            for name in missing_g4_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE text_regions ADD COLUMN {name} {g4_columns[name]}"
                )
    # G5 remains visibly unknown on upgraded projects. No historical row has
    # enough evidence to infer a background class, confidence, reviewer, or
    # owning generation, so the five fields are added only as nullable data.
    g5_columns = {
        "background_category": "VARCHAR(32)",
        "background_confidence": "FLOAT",
        "background_rationale_codes": "JSON",
        "background_reviewer": "JSON",
        "background_generation_id": (
            "VARCHAR(36) REFERENCES page_generations(id) ON DELETE RESTRICT"
        ),
    }
    missing_g5_columns = [name for name in g5_columns if name not in columns]
    if missing_g5_columns:
        with engine.begin() as connection:
            for name in missing_g5_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE text_regions ADD COLUMN {name} {g5_columns[name]}"
                )
    # Legacy recognition/trust rows cannot be promoted into G6.  Upgrades keep
    # the strict review bundle visibly NULL and create the append-only attempt
    # table through metadata without inferring any historical evidence.
    g6_columns = {
        "ocr_review": "JSON",
        "ocr_reviewer": "JSON",
        "ocr_generation_id": ("VARCHAR(36) REFERENCES page_generations(id) ON DELETE RESTRICT"),
    }
    missing_g6_columns = [name for name in g6_columns if name not in columns]
    if missing_g6_columns:
        with engine.begin() as connection:
            for name in missing_g6_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE text_regions ADD COLUMN {name} {g6_columns[name]}"
                )
    image_columns = {column["name"] for column in inspect(engine).get_columns("images")}
    if "inpaint_provenance" not in image_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE images ADD COLUMN inpaint_provenance JSON")
    if "inpaint_classical_approval" not in image_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE images ADD COLUMN inpaint_classical_approval JSON"
            )
    if "inpaint_ai_candidate_reviews" not in image_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE images ADD COLUMN inpaint_ai_candidate_reviews JSON"
            )
    job_columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    if "lineage_context" not in job_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN lineage_context JSON")
    typeset_columns = {
        column["name"] for column in inspect(engine).get_columns("page_typeset_candidates")
    }
    if "cloud_full_page_candidate_id" not in typeset_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE page_typeset_candidates "
                "ADD COLUMN cloud_full_page_candidate_id VARCHAR(36) "
                "REFERENCES page_cloud_full_page_candidates(id) ON DELETE RESTRICT"
            )
    # ORM code never updates or deletes evidence rows. Database triggers make
    # that append-only rule survive raw SQL and future application regressions.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS page_typeset_candidates_validate_insert")
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS page_cloud_full_page_candidates_validate_insert"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS page_cloud_full_page_reviews_validate_insert"
        )
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_text_region_detector_candidate
            ON text_regions(detector_job_item_id, detector_candidate_index)
            WHERE detector_job_item_id IS NOT NULL
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_text_regions_ruby_parent_id
            ON text_regions(ruby_parent_id)
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_text_regions_detector_job_item_id
            ON text_regions(detector_job_item_id)
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_text_regions_background_generation_id
            ON text_regions(background_generation_id)
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_typeset_cloud_full_page_candidate_id
            ON page_typeset_candidates(cloud_full_page_candidate_id)
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_text_regions_ocr_generation_id
            ON text_regions(ocr_generation_id)
            """
        )
        for action in ("INSERT", "UPDATE"):
            connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS text_regions_g4_validate_{action.lower()}"
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS text_regions_g4_validate_{action.lower()}
                BEFORE {action} ON text_regions
                BEGIN
                    SELECT RAISE(ABORT, 'invalid region disposition')
                    WHERE NEW.region_disposition IS NOT NULL
                      AND NEW.region_disposition NOT IN
                          ('translate', 'ignore', 'keep-art', 'redraw-art', 'false-positive');
                    SELECT RAISE(ABORT, 'invalid detector candidate identity')
                    WHERE (NEW.detector_job_item_id IS NULL)
                          <> (NEW.detector_candidate_index IS NULL)
                       OR NEW.detector_candidate_index < 0;
                    SELECT RAISE(ABORT, 'only ruby regions may have a ruby parent')
                    WHERE NEW.ruby_parent_id IS NOT NULL AND NEW.region_type <> 'ruby';
                    SELECT RAISE(ABORT, 'ruby region cannot reference itself')
                    WHERE NEW.ruby_parent_id = NEW.id;
                    SELECT RAISE(ABORT, 'ruby parent must be on the same image')
                    WHERE NEW.ruby_parent_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM text_regions AS parent
                          WHERE parent.id = NEW.ruby_parent_id
                            AND parent.image_id = NEW.image_id
                      );
                    SELECT RAISE(ABORT, 'ruby parent cannot be ruby')
                    WHERE NEW.ruby_parent_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM text_regions AS parent
                          WHERE parent.id = NEW.ruby_parent_id
                            AND parent.region_type = 'ruby'
                      );
                    SELECT RAISE(ABORT, 'ruby parent cannot be a false positive')
                    WHERE NEW.ruby_parent_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM text_regions AS parent
                          WHERE parent.id = NEW.ruby_parent_id
                            AND parent.region_disposition = 'false-positive'
                      );
                    SELECT RAISE(ABORT, 'ruby paragraph group mismatch')
                    WHERE NEW.ruby_parent_id IS NOT NULL
                      AND NEW.paragraph_group_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM text_regions AS parent
                          WHERE parent.id = NEW.ruby_parent_id
                            AND parent.paragraph_group_id IS NOT NULL
                            AND parent.paragraph_group_id <> NEW.paragraph_group_id
                      );
                END
                """
            )
        required_ocr_checks = (
            "'original-and-quality-compared', 'source-text-characters-checked', "
            "'punctuation-checked', 'direction-checked', 'reading-order-checked', "
            "'empty-or-garbled-checked', 'duplicate-fragment-checked', "
            "'template-contamination-checked', 'page-text-consistency-checked'"
        )
        allowed_ocr_flags = (
            "'none', 'original-quality-disagree', 'low-japanese-character-ratio', "
            "'ocr-empty-attempt', 'ocr-garbled-attempt', 'duplicate-fragment', "
            "'template-contamination', 'manual-correction'"
        )
        for action in ("INSERT", "UPDATE"):
            connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS text_regions_g6_validate_{action.lower()}"
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS text_regions_g6_validate_{action.lower()}
                BEFORE {action} ON text_regions
                BEGIN
                    SELECT RAISE(ABORT, 'incomplete OCR review bundle')
                    WHERE NOT (
                        (NEW.ocr_review IS NULL
                         AND NEW.ocr_reviewer IS NULL
                         AND NEW.ocr_generation_id IS NULL)
                        OR
                        (NEW.ocr_review IS NOT NULL
                         AND NEW.ocr_reviewer IS NOT NULL
                         AND NEW.ocr_generation_id IS NOT NULL)
                    );
                    SELECT RAISE(ABORT, 'invalid OCR review')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND (
                          NOT json_valid(NEW.ocr_review)
                          OR json_type(NEW.ocr_review) <> 'object'
                          OR json_extract(NEW.ocr_review, '$.sourceMode') NOT IN
                              ('original-attempt', 'quality-attempt', 'manual-correction')
                          OR json_type(NEW.ocr_review, '$.selectedAttemptId') <> 'text'
                          OR json_type(NEW.ocr_review, '$.sourceTextChecksum') <> 'text'
                          OR length(json_extract(NEW.ocr_review, '$.sourceTextChecksum')) <> 64
                          OR json_extract(NEW.ocr_review, '$.sourceTextChecksum')
                              GLOB '*[^0-9a-f]*'
                          OR json_type(NEW.ocr_review, '$.qcChecks') <> 'array'
                          OR json_array_length(json_extract(NEW.ocr_review, '$.qcChecks')) <> 9
                          OR json_type(NEW.ocr_review, '$.qcFlags') <> 'array'
                          OR json_array_length(json_extract(NEW.ocr_review, '$.qcFlags')) < 1
                      );
                    SELECT RAISE(ABORT, 'invalid OCR review check')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM json_each(NEW.ocr_review, '$.qcChecks')
                          WHERE type <> 'text' OR value NOT IN ({required_ocr_checks})
                      );
                    SELECT RAISE(ABORT, 'duplicate OCR review check')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND (
                          SELECT COUNT(*) FROM json_each(NEW.ocr_review, '$.qcChecks')
                      ) <> (
                          SELECT COUNT(DISTINCT value)
                          FROM json_each(NEW.ocr_review, '$.qcChecks')
                      );
                    SELECT RAISE(ABORT, 'invalid OCR review flag')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM json_each(NEW.ocr_review, '$.qcFlags')
                          WHERE type <> 'text' OR value NOT IN ({allowed_ocr_flags})
                      );
                    SELECT RAISE(ABORT, 'duplicate OCR review flag')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND (
                          SELECT COUNT(*) FROM json_each(NEW.ocr_review, '$.qcFlags')
                      ) <> (
                          SELECT COUNT(DISTINCT value)
                          FROM json_each(NEW.ocr_review, '$.qcFlags')
                      );
                    SELECT RAISE(ABORT, 'OCR review none flag must be exclusive')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM json_each(NEW.ocr_review, '$.qcFlags')
                          WHERE value = 'none'
                      )
                      AND json_array_length(json_extract(NEW.ocr_review, '$.qcFlags')) <> 1;
                    SELECT RAISE(ABORT, 'OCR review attempt is not current')
                    WHERE NEW.ocr_review IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM region_ocr_attempts AS attempt
                          JOIN job_items AS item ON item.id = attempt.job_item_id
                          WHERE attempt.id = json_extract(
                                  NEW.ocr_review, '$.selectedAttemptId'
                              )
                            AND attempt.region_id = NEW.id
                            AND attempt.generation_id = NEW.ocr_generation_id
                            AND item.status = 'completed'
                      );
                    SELECT RAISE(ABORT, 'invalid OCR reviewer')
                    WHERE NEW.ocr_reviewer IS NOT NULL
                      AND (
                          NOT json_valid(NEW.ocr_reviewer)
                          OR json_type(NEW.ocr_reviewer) <> 'object'
                      );
                END
                """
            )
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS region_ocr_attempts_validate_insert")
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS region_ocr_attempts_validate_insert
            BEFORE INSERT ON region_ocr_attempts
            BEGIN
                SELECT RAISE(ABORT, 'invalid OCR attempt checksum')
                WHERE length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.crop_checksum) <> 64
                   OR NEW.crop_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.parameter_hash) <> 64
                   OR NEW.parameter_hash GLOB '*[^0-9a-f]*'
                   OR length(NEW.text_checksum) <> 64
                   OR NEW.text_checksum GLOB '*[^0-9a-f]*';
                SELECT RAISE(ABORT, 'invalid OCR attempt crop')
                WHERE NOT json_valid(NEW.crop_box)
                   OR json_type(NEW.crop_box) <> 'object'
                   OR (SELECT COUNT(*) FROM json_each(NEW.crop_box)) <> 4
                   OR EXISTS (
                       SELECT 1 FROM json_each(NEW.crop_box)
                       WHERE key NOT IN ('x', 'y', 'width', 'height') OR type <> 'integer'
                   )
                   OR json_extract(NEW.crop_box, '$.x') < 0
                   OR json_extract(NEW.crop_box, '$.y') < 0
                   OR json_extract(NEW.crop_box, '$.width') < 1
                   OR json_extract(NEW.crop_box, '$.height') < 1;
                SELECT RAISE(ABORT, 'invalid OCR attempt evidence')
                WHERE NEW.input_variant NOT IN ('original', 'quality')
                   OR length(NEW.provider) < 1
                   OR length(NEW.provider) > 80
                   OR NEW.direction NOT IN ('horizontal', 'vertical')
                   OR (NEW.confidence IS NOT NULL AND NOT (
                       NEW.confidence >= 0 AND NEW.confidence <= 1
                   ));
                SELECT RAISE(ABORT, 'OCR attempt lineage mismatch')
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM text_regions AS region
                    JOIN page_generations AS generation
                      ON generation.id = NEW.generation_id
                    JOIN job_items AS item ON item.id = NEW.job_item_id
                    JOIN jobs AS job ON job.id = NEW.job_id
                    WHERE region.id = NEW.region_id
                      AND region.image_id = NEW.image_id
                      AND region.region_type <> 'ruby'
                      AND region.region_disposition IN ('translate', 'redraw-art')
                      AND generation.image_id = NEW.image_id
                      AND generation.state = 'active'
                      AND item.job_id = NEW.job_id
                      AND item.image_id = NEW.image_id
                      AND item.region_id IS NULL
                      AND job.kind = 'ocr'
                      AND job.lineage_context IS NOT NULL
                );
            END
            """
        )
        for action in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS region_ocr_attempts_append_only_{action.lower()}"
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS region_ocr_attempts_append_only_{action.lower()}
                BEFORE {action} ON region_ocr_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'OCR attempts are append-only');
                END
                """
            )
        background_categories = (
            "'white-solid', 'black-solid', 'other-solid', 'simple-gradient', "
            "'screentone', 'complex-lineart', 'illustration/character'"
        )
        background_rationales = (
            "'uniform-near-white', 'uniform-near-black', 'uniform-other-color', "
            "'smooth-gradient-continuity', 'periodic-screentone', "
            "'structural-lines-cross-region', 'character-or-illustration-detail', "
            "'mixed-visual-signals'"
        )
        for action in ("INSERT", "UPDATE"):
            connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS text_regions_g5_validate_{action.lower()}"
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS text_regions_g5_validate_{action.lower()}
                BEFORE {action} ON text_regions
                BEGIN
                    SELECT RAISE(ABORT, 'incomplete background classification bundle')
                    WHERE NOT (
                        (NEW.background_category IS NULL
                         AND NEW.background_confidence IS NULL
                         AND NEW.background_rationale_codes IS NULL
                         AND NEW.background_reviewer IS NULL
                         AND NEW.background_generation_id IS NULL)
                        OR
                        (NEW.background_category IS NOT NULL
                         AND NEW.background_confidence IS NOT NULL
                         AND NEW.background_rationale_codes IS NOT NULL
                         AND NEW.background_reviewer IS NOT NULL
                         AND NEW.background_generation_id IS NOT NULL)
                    );
                    SELECT RAISE(ABORT, 'invalid background category')
                    WHERE NEW.background_category IS NOT NULL
                      AND NEW.background_category NOT IN ({background_categories});
                    SELECT RAISE(ABORT, 'invalid background confidence')
                    WHERE NEW.background_confidence IS NOT NULL
                      AND NOT (
                          NEW.background_confidence >= 0
                          AND NEW.background_confidence <= 1
                      );
                    SELECT RAISE(ABORT, 'invalid background rationale codes')
                    WHERE NEW.background_rationale_codes IS NOT NULL
                      AND (
                          NOT json_valid(NEW.background_rationale_codes)
                          OR json_type(NEW.background_rationale_codes) <> 'array'
                          OR json_array_length(NEW.background_rationale_codes) < 1
                      );
                    SELECT RAISE(ABORT, 'invalid background rationale code')
                    WHERE NEW.background_rationale_codes IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM json_each(NEW.background_rationale_codes)
                          WHERE type <> 'text' OR value NOT IN ({background_rationales})
                      );
                    SELECT RAISE(ABORT, 'duplicate background rationale code')
                    WHERE NEW.background_rationale_codes IS NOT NULL
                      AND (
                          SELECT COUNT(*) FROM json_each(NEW.background_rationale_codes)
                      ) <> (
                          SELECT COUNT(DISTINCT value)
                          FROM json_each(NEW.background_rationale_codes)
                      );
                    SELECT RAISE(ABORT, 'background rationale does not support category')
                    WHERE NEW.background_category IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM json_each(NEW.background_rationale_codes)
                          WHERE value = CASE NEW.background_category
                              WHEN 'white-solid' THEN 'uniform-near-white'
                              WHEN 'black-solid' THEN 'uniform-near-black'
                              WHEN 'other-solid' THEN 'uniform-other-color'
                              WHEN 'simple-gradient' THEN 'smooth-gradient-continuity'
                              WHEN 'screentone' THEN 'periodic-screentone'
                              WHEN 'complex-lineart' THEN 'structural-lines-cross-region'
                              WHEN 'illustration/character'
                                  THEN 'character-or-illustration-detail'
                          END
                      );
                    SELECT RAISE(ABORT, 'invalid background reviewer')
                    WHERE NEW.background_reviewer IS NOT NULL
                      AND (
                          NOT json_valid(NEW.background_reviewer)
                          OR json_type(NEW.background_reviewer) <> 'object'
                      );
                END
                """
            )
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS text_regions_g4_validate_parent_update")
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS text_regions_g4_validate_parent_update
            BEFORE UPDATE OF image_id, region_type, paragraph_group_id, region_disposition
            ON text_regions
            BEGIN
                SELECT RAISE(ABORT, 'ruby parent cannot become ruby')
                WHERE NEW.region_type = 'ruby'
                  AND EXISTS (
                      SELECT 1 FROM text_regions AS child
                      WHERE child.ruby_parent_id = OLD.id
                  );
                SELECT RAISE(ABORT, 'ruby child paragraph group mismatch')
                WHERE NEW.paragraph_group_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM text_regions AS child
                      WHERE child.ruby_parent_id = OLD.id
                        AND child.paragraph_group_id IS NOT NULL
                        AND child.paragraph_group_id <> NEW.paragraph_group_id
                  );
                SELECT RAISE(ABORT, 'ruby parent and children must remain on the same image')
                WHERE EXISTS (
                    SELECT 1 FROM text_regions AS child
                    WHERE child.ruby_parent_id = OLD.id
                      AND child.image_id <> NEW.image_id
                );
                SELECT RAISE(ABORT, 'ruby parent cannot become a false positive')
                WHERE NEW.region_disposition = 'false-positive'
                  AND EXISTS (
                      SELECT 1 FROM text_regions AS child
                      WHERE child.ruby_parent_id = OLD.id
                  );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS text_regions_g4_restrict_parent_delete
            BEFORE DELETE ON text_regions
            WHEN EXISTS (
                SELECT 1 FROM text_regions AS child
                WHERE child.ruby_parent_id = OLD.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'text region has ruby children');
            END
            """
        )
        # A direct parent-region delete remains forbidden, but deleting an
        # entire image/project must not deadlock against the self-FK RESTRICT
        # action. Remove ruby children first while the image cascade is active.
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS images_g4_delete_regions
            BEFORE DELETE ON images
            BEGIN
                DELETE FROM text_regions
                WHERE image_id = OLD.id AND ruby_parent_id IS NOT NULL;
                DELETE FROM text_regions
                WHERE image_id = OLD.id;
            END
            """
        )
        connection.exec_driver_sql(PAGE_LINEAGE_EVENTS_NO_UPDATE_TRIGGER_SQL)
        connection.exec_driver_sql(PAGE_LINEAGE_EVENTS_NO_DELETE_TRIGGER_SQL)
        connection.exec_driver_sql(G0_REVISION_NO_UPDATE_TRIGGER_SQL)
        connection.exec_driver_sql(G0_REVISION_NO_DELETE_TRIGGER_SQL)
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g8_no_update
            BEFORE UPDATE ON revisions
            WHEN EXISTS (
                SELECT 1 FROM page_lineage_events AS event
                WHERE event.revision_id = OLD.id
                  AND event.gate = 'G8_cleanPlate'
            )
            BEGIN
                SELECT RAISE(ABORT, 'G8 lineage revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g8_no_delete
            BEFORE DELETE ON revisions
            WHEN EXISTS (
                SELECT 1 FROM page_lineage_events AS event
                WHERE event.revision_id = OLD.id
                  AND event.gate = 'G8_cleanPlate'
            )
            BEGIN
                SELECT RAISE(ABORT, 'G8 lineage revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g8_cloud_no_update
            BEFORE UPDATE ON revisions
            WHEN EXISTS (
                SELECT 1 FROM page_lineage_events AS event
                WHERE event.revision_id = OLD.id
                  AND event.gate = 'G8_cloudFullPage'
            )
            BEGIN
                SELECT RAISE(ABORT, 'Cloud G8 lineage revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g8_cloud_no_delete
            BEFORE DELETE ON revisions
            WHEN EXISTS (
                SELECT 1 FROM page_lineage_events AS event
                WHERE event.revision_id = OLD.id
                  AND event.gate = 'G8_cloudFullPage'
            )
            BEGIN
                SELECT RAISE(ABORT, 'Cloud G8 lineage revisions are append-only');
            END
            """
        )
        for table in (
            "page_mask_artifacts",
            "page_mask_reviews",
            "page_clean_plate_candidates",
            "page_clean_plate_reviews",
            "page_cloud_full_page_candidates",
            "page_cloud_full_page_reviews",
            "region_translation_candidates",
            "region_translation_reviews",
            "page_translation_reviews",
            "page_typeset_candidates",
            "page_typeset_reviews",
        ):
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} rows are append-only');
                END
                """
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} rows are append-only');
                END
                """
            )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_cloud_full_page_candidates_validate_insert
            BEFORE INSERT ON page_cloud_full_page_candidates
            BEGIN
                SELECT RAISE(ABORT, 'invalid cloud full-page candidate identity')
                WHERE length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.legacy_state_checksum) <> 64
                   OR NEW.legacy_state_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.project_checksum) <> 64
                   OR NEW.project_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.source_checksum) <> 64
                   OR NEW.source_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.quality_checksum) <> 64
                   OR NEW.quality_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.background_checksum) <> 64
                   OR NEW.background_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.mask_checksum) <> 64
                   OR NEW.mask_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.prompt_sha256) <> 64
                   OR NEW.prompt_sha256 GLOB '*[^0-9a-f]*'
                   OR length(NEW.ordered_input_digest) <> 64
                   OR NEW.ordered_input_digest GLOB '*[^0-9a-f]*'
                   OR length(NEW.raw_checksum) <> 64
                   OR NEW.raw_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.normalized_checksum) <> 64
                   OR NEW.normalized_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.normalization_digest) <> 64
                   OR NEW.normalization_digest GLOB '*[^0-9a-f]*'
                   OR length(NEW.delta_digest) <> 64
                   OR NEW.delta_digest GLOB '*[^0-9a-f]*'
                   OR length(NEW.route_checksum) <> 64
                   OR NEW.route_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.parameter_hash) <> 64
                   OR NEW.parameter_hash GLOB '*[^0-9a-f]*'
                   OR length(NEW.state_checksum) <> 64
                   OR NEW.state_checksum GLOB '*[^0-9a-f]*'
                   OR NEW.sequence <> 1
                   OR NEW.provider IS NULL OR length(NEW.provider) NOT BETWEEN 1 AND 80
                   OR NEW.tool IS NULL OR length(NEW.tool) NOT BETWEEN 1 AND 80
                   OR NEW.model_version IS NULL OR length(NEW.model_version) NOT BETWEEN 1 AND 128;
                SELECT RAISE(ABORT, 'invalid cloud full-page candidate manifests')
                WHERE NOT json_valid(NEW.ordered_input_manifest)
                   OR json_type(NEW.ordered_input_manifest) <> 'array'
                   OR json_array_length(NEW.ordered_input_manifest) < 2
                   OR NOT json_valid(NEW.normalization_manifest)
                   OR json_type(NEW.normalization_manifest) <> 'object'
                   OR NOT json_valid(NEW.delta_manifest)
                   OR json_type(NEW.delta_manifest) <> 'object'
                   OR NOT json_valid(NEW.route_manifest)
                   OR json_type(NEW.route_manifest) <> 'object'
                   OR NOT json_valid(NEW.ancestry)
                   OR json_type(NEW.ancestry) <> 'object'
                   OR (SELECT COUNT(*) FROM json_each(NEW.ancestry)) <> 3
                   OR json_extract(NEW.ancestry, '$.originKind') <> 'direct-ai'
                   OR json_extract(NEW.ancestry, '$.providerClaimStatus')
                      <> 'operator-attested-client-supplied-unverified'
                   OR json_type(NEW.ancestry, '$.operatorAttestation') <> 'object'
                   OR (SELECT COUNT(*) FROM json_each(
                       json_extract(NEW.ancestry, '$.operatorAttestation'))) <> 2
                   OR json_type(NEW.ancestry, '$.operatorAttestation.attested') <> 'true'
                   OR json_extract(NEW.ancestry, '$.operatorAttestation.scope')
                      <> 'provider-tool-model-claim';
                SELECT RAISE(ABORT, 'invalid cloud full-page candidate paths')
                WHERE NEW.raw_relative_path <> 'generated/lineage-cloud-full-pages/'
                      || NEW.generation_id || '/' || NEW.id || '/raw.bin'
                   OR NEW.normalized_relative_path <> 'generated/lineage-cloud-full-pages/'
                      || NEW.generation_id || '/' || NEW.id || '/normalized.png'
                   OR NEW.raw_media_type NOT IN ('image/png', 'image/jpeg', 'image/webp')
                   OR NEW.normalized_media_type <> 'image/png'
                   OR NEW.raw_height <= NEW.raw_width
                   OR NEW.normalized_width <= 0
                   OR NEW.normalized_height <= NEW.normalized_width
                   OR abs(
                       CAST(NEW.raw_width AS REAL) / NEW.raw_height
                       - CAST(NEW.normalized_width AS REAL) / NEW.normalized_height
                   ) / (CAST(NEW.normalized_width AS REAL) / NEW.normalized_height) > 0.01;
                SELECT RAISE(ABORT, 'invalid cloud full-page candidate ownership')
                WHERE NOT EXISTS (
                    SELECT 1 FROM page_generations AS generation
                    JOIN images AS image ON image.id = NEW.image_id
                    JOIN page_mask_artifacts AS mask ON mask.id = NEW.mask_artifact_id
                    JOIN jobs AS job ON job.id = NEW.job_id
                    JOIN job_items AS item ON item.id = NEW.job_item_id
                    JOIN revisions AS revision ON revision.id = NEW.revision_id
                    WHERE generation.id = NEW.generation_id
                      AND generation.state = 'active'
                      AND generation.image_id = NEW.image_id
                      AND generation.project_id = image.project_id
                      AND mask.generation_id = NEW.generation_id
                      AND mask.image_id = NEW.image_id
                      AND mask.mask_checksum = NEW.mask_checksum
                      AND mask.width = NEW.normalized_width
                      AND mask.height = NEW.normalized_height
                      AND job.project_id = generation.project_id
                      AND job.kind = 'cloud-full-page-repair'
                      AND job.status = 'completed'
                      AND item.job_id = job.id AND item.image_id = NEW.image_id
                      AND item.region_id IS NULL AND item.position = 0
                      AND item.status = 'completed' AND item.progress = 1.0
                      AND json_extract(item.output, '$.candidateId') = NEW.id
                      AND json_extract(item.output, '$.rawChecksum') = NEW.raw_checksum
                      AND json_extract(item.output, '$.normalizedChecksum')
                          = NEW.normalized_checksum
                      AND json_extract(item.output, '$.routeChecksum') = NEW.route_checksum
                      AND job.total = 1 AND job.completed = 1 AND job.progress = 1.0
                      AND json_extract(job.options, '$.routeProfile') = NEW.route_profile
                      AND (SELECT COUNT(*) FROM json_each(job.options)) = 1
                      AND revision.project_id = generation.project_id
                      AND revision.entity_type = 'page-cloud-full-page-candidate'
                      AND revision.entity_id = NEW.id
                      AND revision.operation = 'create'
                      AND json(revision.before) = json('{}')
                      AND json_extract(revision.after, '$.candidateId') = NEW.id
                      AND json_extract(revision.after, '$.generationId') = NEW.generation_id
                      AND json_extract(revision.after, '$.imageId') = NEW.image_id
                      AND json_extract(revision.after, '$.jobId') = NEW.job_id
                      AND json_extract(revision.after, '$.jobItemId') = NEW.job_item_id
                      AND json_extract(revision.after, '$.routeProfile') = NEW.route_profile
                      AND json_extract(revision.after, '$.claimStatus')
                          = 'operator-attested-client-supplied-unverified'
                      AND json_extract(revision.after, '$.rawChecksum') = NEW.raw_checksum
                      AND json_extract(revision.after, '$.normalizedChecksum')
                          = NEW.normalized_checksum
                      AND json_type(revision.after, '$.requestRevision') = 'integer'
                      AND (SELECT COUNT(*) FROM json_each(revision.after)) = 10
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_cloud_full_page_reviews_validate_insert
            BEFORE INSERT ON page_cloud_full_page_reviews
            BEGIN
                SELECT RAISE(ABORT, 'invalid cloud full-page review')
                WHERE length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.candidate_checksum) <> 64
                   OR NEW.candidate_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.state_checksum) <> 64
                   OR NEW.state_checksum GLOB '*[^0-9a-f]*'
                   OR NEW.sequence <> 1
                   OR NOT json_valid(NEW.checks)
                   OR json_type(NEW.checks) <> 'array'
                   OR json_array_length(NEW.checks) <> 10
                   OR NOT json_valid(NEW.reviewer)
                   OR json_type(NEW.reviewer) <> 'object'
                   OR EXISTS (
                       SELECT 1 FROM json_each(NEW.checks) AS entry
                       WHERE json_type(entry.value) <> 'object'
                          OR json_extract(entry.value, '$.check') NOT IN (
                              'full-page-fidelity', 'no-new-text', 'no-new-objects',
                              'unrelated-content-preserved',
                              'target-source-text-unreadable',
                              'no-white-or-gray-hole', 'no-blur-band',
                              'no-repeated-texture', 'background-continuous',
                              'structure-preserved'
                          )
                          OR json_type(entry.value, '$.passed') NOT IN ('true', 'false')
                          OR (SELECT COUNT(*) FROM json_each(entry.value)) <> 2
                   )
                   OR (SELECT COUNT(DISTINCT json_extract(value, '$.check'))
                       FROM json_each(NEW.checks)) <> 10
                   OR (NEW.state = 'accepted' AND (
                       NEW.reason <> 'cloud-full-page-repair-complete'
                       OR EXISTS (
                           SELECT 1 FROM json_each(NEW.checks)
                           WHERE json_extract(value, '$.passed') <> 1
                       )
                   ))
                   OR (NEW.state = 'rejected' AND NOT EXISTS (
                       SELECT 1 FROM json_each(NEW.checks)
                       WHERE json_extract(value, '$.passed') = 0
                   ))
                   OR NOT EXISTS (
                       SELECT 1 FROM page_cloud_full_page_candidates AS candidate
                       JOIN revisions AS revision ON revision.id = NEW.revision_id
                       WHERE candidate.id = NEW.candidate_id
                         AND candidate.generation_id = NEW.generation_id
                         AND candidate.image_id = NEW.image_id
                         AND candidate.normalized_checksum = NEW.candidate_checksum
                         AND revision.entity_type = 'page-cloud-full-page-review'
                         AND revision.entity_id = NEW.id
                         AND revision.operation = NEW.state
                         AND revision.project_id = (
                             SELECT project_id FROM page_generations
                             WHERE id = NEW.generation_id
                         )
                         AND json(revision.before) = json('{}')
                         AND json_extract(revision.after, '$.reviewId') = NEW.id
                         AND json_extract(revision.after, '$.candidateId') = NEW.candidate_id
                         AND json_extract(revision.after, '$.candidateChecksum')
                             = NEW.candidate_checksum
                         AND json_extract(revision.after, '$.state') = NEW.state
                         AND json_extract(revision.after, '$.reason') = NEW.reason
                         AND json(json_extract(revision.after, '$.checks')) = json(NEW.checks)
                         AND (SELECT COUNT(*) FROM json_each(revision.after)) = 6
                   );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g9_no_update
            BEFORE UPDATE ON revisions
            WHEN EXISTS (
                SELECT 1 FROM region_translation_candidates AS candidate
                WHERE candidate.revision_id = OLD.id
            )
            OR EXISTS (SELECT 1 FROM region_translation_reviews WHERE revision_id = OLD.id)
            OR EXISTS (SELECT 1 FROM page_translation_reviews WHERE revision_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'G9 translation revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g9_no_delete
            BEFORE DELETE ON revisions
            WHEN EXISTS (
                SELECT 1 FROM region_translation_candidates AS candidate
                WHERE candidate.revision_id = OLD.id
            )
            OR EXISTS (SELECT 1 FROM region_translation_reviews WHERE revision_id = OLD.id)
            OR EXISTS (SELECT 1 FROM page_translation_reviews WHERE revision_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'G9 translation revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g10_no_update
            BEFORE UPDATE ON revisions
            WHEN EXISTS (SELECT 1 FROM page_typeset_candidates WHERE revision_id = OLD.id)
              OR EXISTS (SELECT 1 FROM page_typeset_reviews WHERE revision_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'G10 typeset revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS revisions_g10_no_delete
            BEFORE DELETE ON revisions
            WHEN EXISTS (SELECT 1 FROM page_typeset_candidates WHERE revision_id = OLD.id)
              OR EXISTS (SELECT 1 FROM page_typeset_reviews WHERE revision_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'G10 typeset revisions are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS region_translation_candidates_validate_insert
            BEFORE INSERT ON region_translation_candidates
            BEGIN
                SELECT RAISE(ABORT, 'invalid translation candidate ancestry')
                WHERE (NEW.revision_number = 1 AND NEW.supersedes_candidate_id IS NOT NULL)
                   OR (NEW.revision_number > 1 AND NOT EXISTS (
                       SELECT 1 FROM region_translation_candidates AS parent
                       WHERE parent.id = NEW.supersedes_candidate_id
                         AND parent.generation_id = NEW.generation_id
                         AND parent.image_id = NEW.image_id
                         AND parent.region_id = NEW.region_id
                         AND parent.revision_number = NEW.revision_number - 1
                   ));
                SELECT RAISE(ABORT, 'invalid translation candidate job binding')
                WHERE (NEW.job_id IS NULL) <> (NEW.job_item_id IS NULL)
                   OR (NEW.job_item_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM job_items AS item JOIN jobs AS job ON job.id = item.job_id
                       WHERE item.id = NEW.job_item_id AND job.id = NEW.job_id
                         AND item.image_id = NEW.image_id AND item.region_id IS NULL
                         AND job.kind = 'translate' AND job.lineage_context IS NOT NULL
                         AND EXISTS (
                             SELECT 1 FROM json_each(job.lineage_context, '$.pages') AS page
                             WHERE json_extract(page.value, '$.imageId') = NEW.image_id
                               AND json_extract(
                                   page.value, '$.pageGenerationId'
                               ) = NEW.generation_id
                         )
                   ));
                SELECT RAISE(ABORT, 'invalid translation candidate revision')
                WHERE NOT EXISTS (
                    SELECT 1 FROM revisions AS revision
                    WHERE revision.id = NEW.revision_id
                      AND revision.entity_type = 'translation-candidate'
                      AND revision.entity_id = NEW.id
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS region_translation_reviews_validate_insert
            BEFORE INSERT ON region_translation_reviews
            BEGIN
                SELECT RAISE(ABORT, 'invalid translation review ownership')
                WHERE NOT EXISTS (
                    SELECT 1 FROM region_translation_candidates AS candidate
                    WHERE candidate.id = NEW.candidate_id
                      AND candidate.generation_id = NEW.generation_id
                      AND candidate.image_id = NEW.image_id
                      AND candidate.region_id = NEW.region_id
                      AND candidate.candidate_checksum = NEW.candidate_checksum
                      AND candidate.source_text_checksum = NEW.source_text_checksum
                      AND candidate.context_checksum = NEW.context_checksum
                      AND candidate.g8_checksum = NEW.g8_checksum
                );
                SELECT RAISE(ABORT, 'invalid translation review revision')
                WHERE NOT EXISTS (
                    SELECT 1 FROM revisions AS revision
                    WHERE revision.id = NEW.revision_id
                      AND revision.entity_type = 'translation-review'
                      AND revision.entity_id = NEW.id
                      AND revision.operation = 'review'
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_translation_reviews_validate_insert
            BEFORE INSERT ON page_translation_reviews
            BEGIN
                SELECT RAISE(ABORT, 'invalid translation terminal ownership')
                WHERE NOT EXISTS (
                    SELECT 1 FROM page_generations AS generation
                    WHERE generation.id = NEW.generation_id
                      AND generation.image_id = NEW.image_id
                      AND generation.state = 'active'
                );
                SELECT RAISE(ABORT, 'invalid translation terminal revision')
                WHERE NOT EXISTS (
                    SELECT 1 FROM revisions AS revision
                    WHERE revision.id = NEW.revision_id
                      AND revision.entity_type = 'translation-page-review'
                      AND revision.entity_id = NEW.id
                      AND revision.operation = 'review'
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_mask_artifacts_validate_insert
            BEFORE INSERT ON page_mask_artifacts
            BEGIN
                SELECT RAISE(ABORT, 'invalid mask raster facts')
                WHERE NEW.width <= 0 OR NEW.height <= 0 OR NEW.nonzero_pixels <= 0
                   OR NEW.nonzero_pixels > NEW.width * NEW.height
                   OR length(NEW.mask_checksum) <> 64
                   OR NEW.mask_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.quality_checksum) <> 64
                   OR NEW.quality_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.draft_checksum) <> 64
                   OR NEW.draft_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.parameter_hash) <> 64
                   OR NEW.parameter_hash GLOB '*[^0-9a-f]*'
                   OR NEW.provider <> 'deterministic-mask'
                   OR NEW.model_version <> 'create-mask-v1'
                   OR NEW.relative_path <> 'generated/lineage-masks/'
                      || NEW.generation_id || '/' || NEW.id || '.png'
                   OR NEW.render_scale NOT IN (1, 2, 3, 4)
                   OR NOT json_valid(NEW.bbox)
                   OR json_type(NEW.bbox) <> 'object'
                   OR (SELECT COUNT(*) FROM json_each(NEW.bbox)) <> 4
                   OR json_type(NEW.bbox, '$.x') <> 'integer'
                   OR json_type(NEW.bbox, '$.y') <> 'integer'
                   OR json_type(NEW.bbox, '$.width') <> 'integer'
                   OR json_type(NEW.bbox, '$.height') <> 'integer'
                   OR json_extract(NEW.bbox, '$.x') < 0
                   OR json_extract(NEW.bbox, '$.y') < 0
                   OR json_extract(NEW.bbox, '$.width') <= 0
                   OR json_extract(NEW.bbox, '$.height') <= 0
                   OR json_extract(NEW.bbox, '$.x') + json_extract(NEW.bbox, '$.width') > NEW.width
                   OR json_extract(NEW.bbox, '$.y')
                      + json_extract(NEW.bbox, '$.height') > NEW.height;
                SELECT RAISE(ABORT, 'invalid mask artifact lineage ownership')
                WHERE NOT EXISTS (
                    SELECT 1 FROM job_items AS item
                    JOIN jobs AS job ON job.id = item.job_id
                    JOIN page_generations AS generation
                      ON generation.id = NEW.generation_id
                    WHERE item.id = NEW.job_item_id
                      AND item.job_id = NEW.job_id
                      AND item.image_id = NEW.image_id
                      AND job.kind = 'mask'
                      AND job.project_id = generation.project_id
                      AND generation.image_id = NEW.image_id
                      AND generation.state = 'active'
                      AND EXISTS (
                          SELECT 1 FROM json_each(job.lineage_context, '$.pages') AS page
                          WHERE json_extract(page.value, '$.imageId') = NEW.image_id
                            AND json_extract(page.value, '$.pageGenerationId')
                                = NEW.generation_id
                      )
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_mask_reviews_validate_insert
            BEFORE INSERT ON page_mask_reviews
            BEGIN
                SELECT RAISE(ABORT, 'invalid mask review state')
                WHERE NEW.state NOT IN ('accepted', 'rejected', 'not-applicable');
                SELECT RAISE(ABORT, 'invalid not-applicable mask review')
                WHERE NEW.state = 'not-applicable'
                  AND (NEW.artifact_id IS NOT NULL OR NEW.mask_checksum IS NOT NULL
                       OR json_array_length(NEW.coverage_checks) <> 0
                       OR json_array_length(NEW.collateral_checks) <> 0);
                SELECT RAISE(ABORT, 'mask review requires artifact')
                WHERE NEW.state <> 'not-applicable'
                  AND (NEW.artifact_id IS NULL OR NEW.mask_checksum IS NULL);
                SELECT RAISE(ABORT, 'mask review artifact identity mismatch')
                WHERE NEW.state <> 'not-applicable'
                  AND NOT EXISTS (
                      SELECT 1 FROM page_mask_artifacts AS artifact
                      WHERE artifact.id = NEW.artifact_id
                        AND artifact.generation_id = NEW.generation_id
                        AND artifact.image_id = NEW.image_id
                        AND artifact.mask_checksum = NEW.mask_checksum
                  );
                SELECT RAISE(ABORT, 'invalid mask review check counts')
                WHERE NEW.state <> 'not-applicable'
                  AND (json_array_length(NEW.coverage_checks) <> 5
                       OR json_array_length(NEW.collateral_checks) <> 5);
                SELECT RAISE(ABORT, 'invalid mask coverage check')
                WHERE EXISTS (
                    SELECT 1 FROM json_each(NEW.coverage_checks) AS entry
                    WHERE json_type(entry.value) <> 'object'
                       OR (SELECT COUNT(*) FROM json_each(entry.value)) <> 2
                       OR json_extract(entry.value, '$.check') NOT IN
                          ('body-glyphs-covered', 'punctuation-covered',
                           'strokes-and-shadows-covered', 'ruby-covered',
                           'antialias-edges-covered')
                       OR json_type(entry.value, '$.passed') NOT IN ('true', 'false')
                );
                SELECT RAISE(ABORT, 'duplicate mask coverage check')
                WHERE (SELECT COUNT(DISTINCT json_extract(value, '$.check'))
                       FROM json_each(NEW.coverage_checks))
                      <> json_array_length(NEW.coverage_checks);
                SELECT RAISE(ABORT, 'invalid mask collateral check')
                WHERE EXISTS (
                    SELECT 1 FROM json_each(NEW.collateral_checks) AS entry
                    WHERE json_type(entry.value) <> 'object'
                       OR (SELECT COUNT(*) FROM json_each(entry.value)) <> 2
                       OR json_extract(entry.value, '$.check') NOT IN
                          ('bubble-borders-protected', 'characters-protected',
                           'speed-lines-protected', 'screentone-protected',
                           'nearby-art-protected')
                       OR json_type(entry.value, '$.passed') NOT IN ('true', 'false')
                );
                SELECT RAISE(ABORT, 'duplicate mask collateral check')
                WHERE (SELECT COUNT(DISTINCT json_extract(value, '$.check'))
                       FROM json_each(NEW.collateral_checks))
                      <> json_array_length(NEW.collateral_checks);
                SELECT RAISE(ABORT, 'accepted mask review has failed checks')
                WHERE NEW.state = 'accepted'
                  AND (NEW.reason <> 'complete-and-no-collateral'
                       OR EXISTS (SELECT 1 FROM json_each(NEW.coverage_checks)
                                 WHERE json_extract(value, '$.passed') <> 1)
                       OR EXISTS (SELECT 1 FROM json_each(NEW.collateral_checks)
                                 WHERE json_extract(value, '$.passed') <> 1));
                SELECT RAISE(ABORT, 'rejected mask review has no failed check')
                WHERE NEW.state = 'rejected'
                  AND NOT EXISTS (
                      SELECT 1 FROM json_each(NEW.coverage_checks)
                      WHERE json_extract(value, '$.passed') = 0
                      UNION ALL
                      SELECT 1 FROM json_each(NEW.collateral_checks)
                      WHERE json_extract(value, '$.passed') = 0
                  );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_clean_plate_candidates_validate_insert
            BEFORE INSERT ON page_clean_plate_candidates
            BEGIN
                SELECT RAISE(ABORT, 'invalid clean plate candidate raster facts')
                WHERE NEW.width <= 0 OR NEW.height <= 0
                   OR NEW.outside_mask_change_count <> 0
                   OR length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.quality_checksum) <> 64
                   OR NEW.quality_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.background_checksum) <> 64
                   OR NEW.background_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.mask_checksum) <> 64
                   OR NEW.mask_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.route_checksum) <> 64
                   OR NEW.route_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.parameter_hash) <> 64
                   OR NEW.parameter_hash GLOB '*[^0-9a-f]*'
                   OR length(NEW.candidate_checksum) <> 64
                   OR NEW.candidate_checksum GLOB '*[^0-9a-f]*'
                   OR NEW.origin_kind NOT IN ('deterministic', 'ai', 'classical', 'mixed')
                   OR NEW.relative_path <> 'generated/lineage-clean-plates/'
                      || NEW.generation_id || '/' || NEW.id || '.png'
                   OR NEW.render_scale NOT IN (1, 2, 3, 4)
                   OR NOT json_valid(NEW.route_manifest)
                   OR json_type(NEW.route_manifest) <> 'array'
                   OR json_array_length(NEW.route_manifest) < 1
                   OR NOT json_valid(NEW.provider_ids)
                   OR json_type(NEW.provider_ids) <> 'array'
                   OR json_array_length(NEW.provider_ids) < 1
                   OR NOT json_valid(NEW.model_versions)
                   OR json_type(NEW.model_versions) <> 'array'
                   OR json_array_length(NEW.model_versions) < 1
                   OR NOT json_valid(NEW.anomalies)
                   OR json_type(NEW.anomalies) <> 'array';
                SELECT RAISE(ABORT, 'invalid clean plate candidate lineage ownership')
                WHERE NOT EXISTS (
                    SELECT 1 FROM job_items AS item
                    JOIN jobs AS job ON job.id = item.job_id
                    JOIN page_generations AS generation
                      ON generation.id = NEW.generation_id
                    JOIN page_mask_artifacts AS mask
                      ON mask.id = NEW.mask_artifact_id
                    WHERE item.id = NEW.job_item_id
                      AND item.job_id = NEW.job_id
                      AND item.image_id = NEW.image_id
                      AND item.region_id IS NULL
                      AND job.kind = 'inpaint'
                      AND job.project_id = generation.project_id
                      AND generation.image_id = NEW.image_id
                      AND generation.state = 'active'
                      AND mask.generation_id = NEW.generation_id
                      AND mask.image_id = NEW.image_id
                      AND mask.mask_checksum = NEW.mask_checksum
                      AND EXISTS (
                          SELECT 1 FROM json_each(job.lineage_context, '$.pages') AS page
                          WHERE json_extract(page.value, '$.imageId') = NEW.image_id
                            AND json_extract(page.value, '$.pageGenerationId')
                                = NEW.generation_id
                      )
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_clean_plate_reviews_validate_insert
            BEFORE INSERT ON page_clean_plate_reviews
            BEGIN
                SELECT RAISE(ABORT, 'invalid clean plate review checksum')
                WHERE length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR (NEW.candidate_checksum IS NOT NULL
                       AND (length(NEW.candidate_checksum) <> 64
                            OR NEW.candidate_checksum GLOB '*[^0-9a-f]*'))
                   OR (NEW.mask_checksum IS NOT NULL
                       AND (length(NEW.mask_checksum) <> 64
                            OR NEW.mask_checksum GLOB '*[^0-9a-f]*'));
                SELECT RAISE(ABORT, 'invalid not-applicable clean plate review')
                WHERE NEW.state = 'not-applicable'
                  AND (NEW.candidate_id IS NOT NULL
                       OR NEW.candidate_checksum IS NOT NULL
                       OR NEW.mask_checksum IS NOT NULL
                       OR NEW.reason <> 'no-clean-plate-required'
                       OR json_array_length(NEW.checks) <> 0);
                SELECT RAISE(ABORT, 'clean plate review requires candidate')
                WHERE NEW.state <> 'not-applicable'
                  AND (NEW.candidate_id IS NULL
                       OR NEW.candidate_checksum IS NULL
                       OR NEW.mask_checksum IS NULL);
                SELECT RAISE(ABORT, 'clean plate review candidate identity mismatch')
                WHERE NEW.state <> 'not-applicable'
                  AND NOT EXISTS (
                      SELECT 1 FROM page_clean_plate_candidates AS candidate
                      WHERE candidate.id = NEW.candidate_id
                        AND candidate.generation_id = NEW.generation_id
                        AND candidate.image_id = NEW.image_id
                        AND candidate.parent_checksum = NEW.parent_checksum
                        AND candidate.candidate_checksum = NEW.candidate_checksum
                        AND candidate.mask_checksum = NEW.mask_checksum
                  );
                SELECT RAISE(ABORT, 'invalid clean plate review checks')
                WHERE NEW.state <> 'not-applicable'
                  AND (
                      NOT json_valid(NEW.checks)
                      OR json_type(NEW.checks) <> 'array'
                      OR json_array_length(NEW.checks) <> 7
                      OR EXISTS (
                          SELECT 1 FROM json_each(NEW.checks) AS entry
                          WHERE json_type(entry.value) <> 'object'
                             OR (SELECT COUNT(*) FROM json_each(entry.value)) <> 2
                             OR json_extract(entry.value, '$.check') NOT IN
                                ('outside-mask-unchanged', 'source-text-unreadable',
                                 'no-white-or-gray-hole', 'no-blur-band',
                                 'no-repeated-texture', 'background-continuous',
                                 'structure-preserved')
                             OR json_type(entry.value, '$.passed') NOT IN ('true', 'false')
                      )
                      OR (SELECT COUNT(DISTINCT json_extract(value, '$.check'))
                          FROM json_each(NEW.checks)) <> 7
                  );
                SELECT RAISE(ABORT, 'accepted clean plate review has failed checks')
                WHERE NEW.state = 'accepted'
                  AND (NEW.reason <> 'clean-plate-complete'
                       OR EXISTS (SELECT 1 FROM json_each(NEW.checks)
                                 WHERE json_extract(value, '$.passed') <> 1));
                SELECT RAISE(ABORT, 'rejected clean plate review has no failed check')
                WHERE NEW.state = 'rejected'
                  AND (NEW.reason NOT IN
                       ('residual-text-readable', 'hole-or-block', 'blur-band',
                        'repeated-texture', 'background-discontinuous',
                        'structure-damaged', 'outside-mask-changed',
                        'multiple-visual-failures')
                       OR NOT EXISTS (SELECT 1 FROM json_each(NEW.checks)
                                     WHERE json_extract(value, '$.passed') = 0));
                SELECT RAISE(ABORT, 'invalid clean plate reviewer')
                WHERE NOT json_valid(NEW.reviewer)
                   OR json_type(NEW.reviewer) <> 'object';
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_typeset_candidates_validate_insert
            BEFORE INSERT ON page_typeset_candidates
            BEGIN
                SELECT RAISE(ABORT, 'invalid typeset candidate raster facts')
                WHERE NEW.width <= 0 OR NEW.height <= 0
                   OR NEW.render_scale NOT IN (1, 2, 3, 4)
                   OR NEW.provider <> 'pillow-g10'
                   OR NEW.model_version <> 'g10-typeset-v1'
                   OR NEW.relative_path <> 'generated/lineage-typesets/'
                      || NEW.generation_id || '/' || NEW.id || '.png'
                   OR length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.g9_terminal_checksum) <> 64
                   OR NEW.g9_terminal_checksum GLOB '*[^0-9a-f]*'
                   OR NEW.parent_checksum <> NEW.g9_terminal_checksum
                   OR length(NEW.translation_state_checksum) <> 64
                   OR NEW.translation_state_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.clean_plate_checksum) <> 64
                   OR NEW.clean_plate_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.route_checksum) <> 64
                   OR NEW.route_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.style_checksum) <> 64
                   OR NEW.style_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.layout_checksum) <> 64
                   OR NEW.layout_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.parameter_hash) <> 64
                   OR NEW.parameter_hash GLOB '*[^0-9a-f]*'
                   OR length(NEW.candidate_checksum) <> 64
                   OR NEW.candidate_checksum GLOB '*[^0-9a-f]*';
                SELECT RAISE(ABORT, 'invalid typeset candidate manifests')
                WHERE NOT json_valid(NEW.region_manifest)
                   OR json_type(NEW.region_manifest) <> 'array'
                   OR NOT json_valid(NEW.route_manifest)
                   OR json_type(NEW.route_manifest) <> 'array'
                   OR NOT json_valid(NEW.style_manifest)
                   OR json_type(NEW.style_manifest) <> 'array'
                   OR NOT json_valid(NEW.layout_manifest)
                   OR json_type(NEW.layout_manifest) <> 'array'
                   OR json_array_length(NEW.region_manifest)
                      <> json_array_length(NEW.route_manifest)
                   OR json_array_length(NEW.route_manifest)
                      <> json_array_length(NEW.style_manifest)
                   OR NOT json_valid(NEW.overflow_region_ids)
                   OR json_type(NEW.overflow_region_ids) <> 'array'
                   OR NOT json_valid(NEW.anomalies)
                   OR json_type(NEW.anomalies) <> 'array';
                SELECT RAISE(ABORT, 'invalid typeset route manifest')
                WHERE EXISTS (
                    SELECT 1 FROM json_each(NEW.route_manifest) AS entry
                    WHERE json_type(entry.value) <> 'object'
                       OR json_extract(entry.value, '$.route') NOT IN
                          ('bubble', 'ordinary', 'art-lettering', 'keep', 'ignore')
                );
                SELECT RAISE(ABORT, 'invalid typeset candidate lineage ownership')
                WHERE NOT EXISTS (
                    SELECT 1 FROM job_items AS item
                    JOIN jobs AS job ON job.id = item.job_id
                    JOIN page_generations AS generation ON generation.id = NEW.generation_id
                    JOIN page_translation_reviews AS translation
                      ON translation.generation_id = NEW.generation_id
                    WHERE item.id = NEW.job_item_id
                      AND item.job_id = NEW.job_id
                      AND item.image_id = NEW.image_id
                      AND item.region_id IS NULL
                      AND job.kind = 'typeset'
                      AND job.lineage_context IS NOT NULL
                      AND job.project_id = generation.project_id
                      AND generation.image_id = NEW.image_id
                      AND generation.state = 'active'
                      AND translation.terminal_checksum = NEW.g9_terminal_checksum
                      AND translation.translation_state_checksum
                          = NEW.translation_state_checksum
                      AND EXISTS (
                          SELECT 1 FROM json_each(job.lineage_context, '$.pages') AS page
                          WHERE json_extract(page.value, '$.imageId') = NEW.image_id
                            AND json_extract(page.value, '$.pageGenerationId')
                                = NEW.generation_id
                      )
                );
                SELECT RAISE(ABORT, 'invalid typeset clean plate binding')
                WHERE NEW.clean_plate_candidate_id IS NOT NULL
                  AND NEW.cloud_full_page_candidate_id IS NOT NULL;
                SELECT RAISE(ABORT, 'invalid typeset clean plate binding')
                WHERE NEW.clean_plate_candidate_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM page_clean_plate_candidates AS clean
                      JOIN page_clean_plate_reviews AS review
                        ON review.candidate_id = clean.id AND review.state = 'accepted'
                      WHERE clean.id = NEW.clean_plate_candidate_id
                        AND clean.generation_id = NEW.generation_id
                        AND clean.image_id = NEW.image_id
                        AND clean.candidate_checksum = NEW.clean_plate_checksum
                  );
                SELECT RAISE(ABORT, 'invalid typeset cloud full-page binding')
                WHERE NEW.cloud_full_page_candidate_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM page_cloud_full_page_candidates AS clean
                      JOIN page_cloud_full_page_reviews AS review
                        ON review.candidate_id = clean.id AND review.state = 'accepted'
                      WHERE clean.id = NEW.cloud_full_page_candidate_id
                        AND clean.generation_id = NEW.generation_id
                        AND clean.image_id = NEW.image_id
                        AND clean.normalized_checksum = NEW.clean_plate_checksum
                  );
                SELECT RAISE(ABORT, 'invalid typeset candidate revision')
                WHERE NOT EXISTS (
                    SELECT 1 FROM revisions AS revision
                    WHERE revision.id = NEW.revision_id
                      AND revision.entity_type = 'typeset-candidate'
                      AND revision.entity_id = NEW.id
                      AND revision.operation = 'create'
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_typeset_reviews_validate_insert
            BEFORE INSERT ON page_typeset_reviews
            BEGIN
                SELECT RAISE(ABORT, 'invalid typeset review observation')
                WHERE NEW.observed_width <= 0 OR NEW.observed_height <= 0
                   OR NEW.observed_render_scale NOT IN (1, 2, 3, 4)
                   OR length(NEW.parent_checksum) <> 64
                   OR NEW.parent_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.candidate_checksum) <> 64
                   OR NEW.candidate_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.route_checksum) <> 64
                   OR NEW.route_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.style_checksum) <> 64
                   OR NEW.style_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.layout_checksum) <> 64
                   OR NEW.layout_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.g9_terminal_checksum) <> 64
                   OR NEW.g9_terminal_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.clean_plate_checksum) <> 64
                   OR NEW.clean_plate_checksum GLOB '*[^0-9a-f]*'
                   OR length(NEW.terminal_checksum) <> 64
                   OR NEW.terminal_checksum GLOB '*[^0-9a-f]*';
                SELECT RAISE(ABORT, 'typeset review candidate identity mismatch')
                WHERE NOT EXISTS (
                    SELECT 1 FROM page_typeset_candidates AS candidate
                    WHERE candidate.id = NEW.candidate_id
                      AND candidate.generation_id = NEW.generation_id
                      AND candidate.image_id = NEW.image_id
                      AND candidate.parent_checksum = NEW.parent_checksum
                      AND candidate.candidate_checksum = NEW.candidate_checksum
                      AND candidate.route_checksum = NEW.route_checksum
                      AND candidate.style_checksum = NEW.style_checksum
                      AND candidate.layout_checksum = NEW.layout_checksum
                      AND candidate.g9_terminal_checksum = NEW.g9_terminal_checksum
                      AND candidate.clean_plate_checksum = NEW.clean_plate_checksum
                      AND candidate.width = NEW.observed_width
                      AND candidate.height = NEW.observed_height
                      AND candidate.render_scale = NEW.observed_render_scale
                );
                SELECT RAISE(ABORT, 'invalid typeset visual checks')
                WHERE NOT json_valid(NEW.checks)
                   OR json_type(NEW.checks) <> 'array'
                   OR json_array_length(NEW.checks) <> 8
                   OR EXISTS (
                       SELECT 1 FROM json_each(NEW.checks) AS entry
                       WHERE json_type(entry.value) <> 'object'
                          OR (SELECT COUNT(*) FROM json_each(entry.value)) <> 2
                          OR json_extract(entry.value, '$.check') NOT IN
                             ('original-clean-final-compared', 'translation-complete',
                              'hierarchy-reading-order-preserved', 'key-art-unobstructed',
                              'typography-source-matched', 'bubble-contained',
                              'art-lettering-composition-matched', 'overflow-free')
                          OR json_type(entry.value, '$.passed') NOT IN ('true', 'false')
                   )
                   OR (SELECT COUNT(DISTINCT json_extract(value, '$.check'))
                       FROM json_each(NEW.checks)) <> 8;
                SELECT RAISE(ABORT, 'accepted typeset review has failed evidence')
                WHERE NEW.state = 'accepted'
                  AND (NEW.reason <> 'typeset-reviewed'
                       OR EXISTS (SELECT 1 FROM json_each(NEW.checks)
                                 WHERE json_extract(value, '$.passed') <> 1)
                       OR EXISTS (
                           SELECT 1 FROM page_typeset_candidates AS candidate
                           WHERE candidate.id = NEW.candidate_id
                             AND (json_array_length(candidate.overflow_region_ids) <> 0
                                  OR json_array_length(candidate.anomalies) <> 0)
                       ));
                SELECT RAISE(ABORT, 'rejected typeset review reason mismatch')
                WHERE NEW.state = 'rejected'
                  AND (
                      NOT EXISTS (SELECT 1 FROM json_each(NEW.checks)
                                  WHERE json_extract(value, '$.passed') = 0)
                      OR (
                          NEW.reason <> 'multiple-visual-failures'
                          AND NOT EXISTS (
                              SELECT 1 FROM json_each(NEW.checks)
                              WHERE json_extract(value, '$.passed') = 0
                                AND json_extract(value, '$.check') = NEW.reason
                          )
                      )
                      OR (
                          NEW.reason = 'multiple-visual-failures'
                          AND (SELECT COUNT(*) FROM json_each(NEW.checks)
                               WHERE json_extract(value, '$.passed') = 0) < 2
                      )
                  );
                SELECT RAISE(ABORT, 'invalid typeset reviewer')
                WHERE NOT json_valid(NEW.reviewer)
                   OR json_type(NEW.reviewer) <> 'object';
                SELECT RAISE(ABORT, 'invalid typeset review revision')
                WHERE NOT EXISTS (
                    SELECT 1 FROM revisions AS revision
                    WHERE revision.id = NEW.revision_id
                      AND revision.entity_type = 'typeset-review'
                      AND revision.entity_id = NEW.id
                      AND revision.operation = 'review'
                );
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS page_typeset_reviews_validate_known_defects
            BEFORE INSERT ON page_typeset_reviews
            WHEN EXISTS (
                SELECT 1 FROM page_typeset_candidates AS candidate
                WHERE candidate.id = NEW.candidate_id
                  AND (json_array_length(candidate.overflow_region_ids) <> 0
                       OR json_array_length(candidate.anomalies) <> 0)
            )
            AND EXISTS (
                SELECT 1 FROM json_each(NEW.checks)
                WHERE json_extract(value, '$.check') = 'overflow-free'
                  AND json_extract(value, '$.passed') = 1
            )
            BEGIN
                SELECT RAISE(ABORT, 'typeset overflow evidence contradicts known defects');
            END
            """
        )
    # Do not normalize rows here. ``ProjectRegistry.open`` needs to observe an
    # empty or malformed payload so it can fail closed and invalidate derived
    # artifacts. Eager SQL backfills used to hide stale schema-2 rows by making
    # them look current before the project-level migration ran.
    return engine


SessionFactory = sessionmaker
