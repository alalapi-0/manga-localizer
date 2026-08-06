from __future__ import annotations

from typing import Any

from manga_localizer.config import Settings
from manga_localizer.imaging import OpenCVInpaintingProvider
from manga_localizer.providers.ocr import TesseractOCRProvider
from manga_localizer.providers.translation import (
    DictionaryTranslationProvider,
    ManualTranslationProvider,
    MockTranslationProvider,
    OpenAICompatibleTranslationProvider,
    TranslationProvider,
)
from manga_localizer.security import validate_remote_base_url


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ocr = TesseractOCRProvider(settings.tesseract_command)
        self.inpainting = OpenCVInpaintingProvider()
        self.manual = ManualTranslationProvider()
        self.mock = MockTranslationProvider()
        self.dictionary = DictionaryTranslationProvider()
        self._session_openai_key: str | None = settings.openai_api_key
        self._session_openai_base_url = settings.openai_base_url
        self._session_openai_model = settings.openai_model

    def configure_openai_session(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        # Deliberately in memory only. Passing None clears the session credential.
        normalized_base_url = validate_remote_base_url(base_url) if base_url else None
        self._session_openai_key = api_key
        if normalized_base_url:
            self._session_openai_base_url = normalized_base_url
        if model:
            self._session_openai_model = model

    def translation(self, name: str, options: dict[str, Any]) -> TranslationProvider:
        if name == "manual":
            return self.manual
        if name == "mock":
            return self.mock
        if name == "dictionary":
            entries = options.get("dictionary") or {}
            return DictionaryTranslationProvider(entries)
        if name in {"openai", "openai-compatible"}:
            return OpenAICompatibleTranslationProvider(
                api_key=self._session_openai_key,
                base_url=str(options.get("baseUrl") or self._session_openai_base_url),
                model=str(options.get("model") or self._session_openai_model),
                max_context_items=self.settings.remote_context_items,
                max_context_chars=self.settings.remote_context_chars,
            )
        raise ValueError(f"Unknown translation provider: {name}")

    def capabilities(self) -> dict[str, Any]:
        openai = self.translation("openai-compatible", {})
        ocr = self.ocr.capabilities()
        ocr["configuredLanguages"] = self.settings.ocr_language_list
        ocr["defaultDirection"] = self.settings.ocr_default_direction
        return {
            "ocr": {"tesseract": ocr},
            "inpainting": {"opencv": self.inpainting.get_capabilities()},
            "translation": {
                "manual": self.manual.capabilities(),
                "mock": self.mock.capabilities(),
                "dictionary": self.dictionary.capabilities(),
                "openai-compatible": openai.capabilities(),
            },
        }
