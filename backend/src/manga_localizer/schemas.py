from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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
    anomalies: list[str] = Field(default_factory=list)


class SelectInpaintCandidateRequest(APIModel):
    candidate_id: str
    expected_revision: int = Field(ge=0)


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
    inpaint_candidate: str | None = None
    inpaint_candidates: list[InpaintCandidateOut] = Field(default_factory=list)
    typeset_overflow_count: int = 0
    typeset_overflow_region_ids: list[str] = Field(default_factory=list)
    thumbnail_url: str
    content_url: str
    created_at: datetime
    updated_at: datetime


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

    @field_validator("repair")
    @classmethod
    def validate_repair(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_repair(value) if value is not None else None

    @model_validator(mode="after")
    def validate_review_flags(self) -> RegionPatch:
        if self.ignored is True and self.confirmed is True:
            raise ValueError("A region cannot be both ignored and confirmed")
        return self


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


class JobRequest(APIModel):
    image_ids: list[str] = Field(default_factory=list)
    region_ids: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


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
    }
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"Unknown repair fields: {', '.join(unknown_fields)}")
    normalized = dict(value)
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
        if not isinstance(mask_mode, str) or mask_mode not in {"region", "text"}:
            raise ValueError("maskMode must be region or text")
    if "method" in normalized:
        method = normalized["method"]
        aliases = {
            "telea": "telea",
            "ns": "navier-stokes",
            "navier-stokes": "navier-stokes",
            "navier_stokes": "navier-stokes",
            "solid": "solid",
        }
        if not isinstance(method, str) or method not in aliases:
            raise ValueError("method must be telea, navier-stokes, or solid")
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
        return normalized
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
    return normalized
