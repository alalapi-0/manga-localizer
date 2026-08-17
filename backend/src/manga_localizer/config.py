from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from manga_localizer.security import is_loopback_host, is_private_lan_host, validate_remote_base_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MANGA_LOCALIZER_",
        env_file=None,
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".manga-localizer")
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("MANGA_LOCALIZER_HOST", "HOST"),
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("MANGA_LOCALIZER_PORT", "PORT"),
    )
    log_level: str = Field(
        default="info",
        validation_alias=AliasChoices("MANGA_LOCALIZER_LOG_LEVEL", "LOG_LEVEL"),
    )
    ocr_languages: str = Field(
        default="jpn,jpn_vert",
        validation_alias=AliasChoices("MANGA_LOCALIZER_OCR_LANGUAGES", "OCR_LANGUAGES"),
    )
    ocr_default_direction: str = "auto"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    frontend_dist: Path | None = None
    model_bundle: Path | None = None
    lan_access: bool = False
    tesseract_command: str = Field(
        default="tesseract",
        validation_alias=AliasChoices(
            "MANGA_LOCALIZER_TESSERACT_COMMAND",
            "MANGA_LOCALIZER_TESSERACT_CMD",
            "TESSERACT_CMD",
        ),
    )
    ppocr_detection_model: Path | None = None
    realesrgan_ncnn_command: str = "realesrgan-ncnn-vulkan"
    realesrgan_ncnn_models: Path | None = None
    realesrgan_onnx_model: Path | None = None
    lama_inpainting_model: Path | None = None
    argos_ja_en_model: Path | None = None
    argos_en_zh_model: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    thumbnail_size: int = 384
    worker_poll_seconds: float = 0.15
    remote_context_items: int = 6
    remote_context_chars: int = 4_000
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.json"

    @property
    def ocr_language_list(self) -> list[str]:
        return [language.strip() for language in self.ocr_languages.split(",") if language.strip()]

    @property
    def ppocr_detection_model_path(self) -> Path:
        return self.ppocr_detection_model or (
            self.data_dir / "models" / "text_detection_cn_ppocrv3_2023may.onnx"
        )

    @property
    def lama_inpainting_model_path(self) -> Path:
        return self.lama_inpainting_model or (
            self.data_dir / "models" / "inpainting_lama_2025jan.onnx"
        )

    @property
    def realesrgan_onnx_model_path(self) -> Path:
        return self.realesrgan_onnx_model or (
            self.data_dir / "models" / "RealESRGAN_x4plus_anime_6B.onnx"
        )

    @property
    def realesrgan_ncnn_models_path(self) -> Path:
        return self.realesrgan_ncnn_models or (self.data_dir / "realesrgan-ncnn-vulkan" / "models")

    @property
    def argos_ja_en_model_path(self) -> Path:
        return self.argos_ja_en_model or (self.data_dir / "models" / "argos-ja-en")

    @property
    def argos_en_zh_model_path(self) -> Path:
        return self.argos_en_zh_model or (self.data_dir / "models" / "argos-en-zh")

    @property
    def realesrgan_ncnn_search_paths(self) -> tuple[Path, ...]:
        return (self.data_dir / "realesrgan-ncnn-vulkan",)

    @field_validator("ocr_languages")
    @classmethod
    def valid_ocr_languages(cls, value: str) -> str:
        languages = [language.strip() for language in value.split(",") if language.strip()]
        if not languages:
            raise ValueError("ocr_languages must contain at least one Tesseract language")
        return ",".join(languages)

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"critical", "error", "warning", "info", "debug"}:
            raise ValueError("log_level must be critical, error, warning, info, or debug")
        return normalized

    @model_validator(mode="after")
    def bindable_host(self) -> Settings:
        host = self.host.strip()
        self.host = host
        if is_loopback_host(host):
            return self
        if self.lan_access and is_private_lan_host(host):
            return self
        raise ValueError(
            "host must be loopback, or a private LAN IPv4 when "
            "MANGA_LOCALIZER_LAN_ACCESS is enabled"
        )

    @field_validator("openai_model")
    @classmethod
    def nonempty_openai_model(cls, value: str) -> str:
        return value.strip() or "gpt-4.1-mini"

    @field_validator("openai_base_url")
    @classmethod
    def valid_openai_base_url(cls, value: str) -> str:
        return validate_remote_base_url(value)

    @field_validator("ocr_default_direction")
    @classmethod
    def valid_ocr_direction(cls, value: str) -> str:
        if value not in {"auto", "horizontal", "vertical"}:
            raise ValueError("ocr_default_direction must be auto, horizontal, or vertical")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
