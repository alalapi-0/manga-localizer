from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    region_count: int
    confirmed_count: int
    ignored_count: int
    processing_errors: list[dict[str, Any]]
    error: str | None
    revision: int
    detector_provider: str | None
    ocr_provider: str | None
    translator_provider: str | None
    inpainting_provider: str | None
    typesetting_provider: str | None
    thumbnail_url: str
    content_url: str
    created_at: datetime
    updated_at: datetime


class LocalImportRequest(APIModel):
    paths: list[Path] = Field(min_length=1)


class RegionCreate(APIModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: float = 0.0
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
    confidence: float | None = Field(default=None, ge=0, le=1)
    ignored: bool = False
    confirmed: bool = False
    style: dict[str, Any] = Field(default_factory=dict)
    repair: dict[str, Any] = Field(default_factory=dict)


class RegionPatch(APIModel):
    x: float | None = Field(default=None, ge=0)
    y: float | None = Field(default=None, ge=0)
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    rotation: float | None = None
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
    confidence: float | None = Field(default=None, ge=0, le=1)
    ignored: bool | None = None
    confirmed: bool | None = None
    style: dict[str, Any] | None = None
    repair: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=0)


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
    confidence: float | None
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


class JobRequest(APIModel):
    image_ids: list[str] = Field(default_factory=list)
    region_ids: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class JobItemOut(APIModel):
    id: str
    image_id: str | None
    region_id: str | None
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
    options: dict[str, Any]
    items: list[JobItemOut]
    created_at: datetime
    updated_at: datetime


class HealthOut(APIModel):
    status: Literal["ok", "degraded"]
    version: str
    database: str
    queue: str


class ConfigOut(APIModel):
    providers: dict[str, Any]
    capabilities: dict[str, Any]


class OpenAISessionConfig(APIModel):
    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    model: str | None = None
