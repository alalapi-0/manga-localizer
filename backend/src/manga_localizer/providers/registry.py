from __future__ import annotations

from typing import Any

from manga_localizer.config import Settings
from manga_localizer.imaging import (
    OpenCVInpaintingProvider,
    OpenCVPillowPreprocessProvider,
    PreprocessProvider,
    RealESRGANNCNNPreprocessProvider,
    RealESRGANONNXPreprocessProvider,
)
from manga_localizer.providers.detection import (
    PPOCRTextDetectionProvider,
    TextDetectionProvider,
    UnionTextDetectionProvider,
)
from manga_localizer.providers.inpainting_lama import LaMaONNXInpaintingProvider
from manga_localizer.providers.ocr import TesseractOCRProvider
from manga_localizer.providers.translation import (
    DictionaryTranslationProvider,
    ManualTranslationProvider,
    MockTranslationProvider,
    OpenAICompatibleTranslationProvider,
    TranslationProvider,
)
from manga_localizer.providers.translation_argos import ArgosJaZhTranslationProvider
from manga_localizer.security import validate_remote_base_url


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ocr = TesseractOCRProvider(settings.tesseract_command)
        self.ppocr = PPOCRTextDetectionProvider(settings.ppocr_detection_model_path)
        self.union_detector = UnionTextDetectionProvider(self.ppocr, self.ocr)
        self.preprocessing = OpenCVPillowPreprocessProvider(profile="off")
        self.realesrgan = RealESRGANNCNNPreprocessProvider(
            command=settings.realesrgan_ncnn_command,
            models_dir=settings.realesrgan_ncnn_models_path,
            search_paths=settings.realesrgan_ncnn_search_paths,
            profile="off",
        )
        self.realesrgan_onnx = RealESRGANONNXPreprocessProvider(
            settings.realesrgan_onnx_model_path,
            profile="off",
        )
        self.inpainting = OpenCVInpaintingProvider()
        self.lama = LaMaONNXInpaintingProvider(settings.lama_inpainting_model_path)
        self.manual = ManualTranslationProvider()
        self.mock = MockTranslationProvider()
        self.dictionary = DictionaryTranslationProvider()
        self.argos = ArgosJaZhTranslationProvider(
            settings.argos_ja_en_model_path,
            settings.argos_en_zh_model_path,
        )
        self._session_openai_key: str | None = settings.openai_api_key
        self._session_openai_base_url = settings.openai_base_url
        self._session_openai_model = settings.openai_model

    def detector(self, name: str) -> TextDetectionProvider:
        if name == "tesseract":
            return self.ocr
        if name in {"ppocr", "ppocr-v3", "paddleocr-detection"}:
            return self.ppocr
        if name in {"ppocr-v3+tesseract", "union", "ppocr-tesseract"}:
            return self.union_detector
        raise ValueError(f"Unknown text detection provider: {name}")

    def ocr_provider(self, name: str):
        if name == "tesseract":
            return self.ocr
        raise ValueError(f"Unknown OCR provider: {name}")

    def preprocessor(self, name: str) -> PreprocessProvider:
        if name in {"opencv", "opencv-pillow", "local"}:
            return self.preprocessing
        if name in {"realesrgan-ncnn", "realesrgan-ncnn-vulkan"}:
            return self.realesrgan
        if name in {"realesrgan", "realesrgan-onnx", "realesrgan-onnx-anime"}:
            return self.realesrgan_onnx
        raise ValueError(f"Unknown image preprocessing provider: {name}")

    def inpainter(self, name: str):
        if name in {"opencv", "opencv-inpaint"}:
            return self.inpainting
        if name in {"lama", "lama-onnx"}:
            return self.lama
        raise ValueError(f"Unknown inpainting provider: {name}")

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
        if name in {"argos-ja-zh", "argos", "local-nmt"}:
            return self.argos
        raise ValueError(f"Unknown translation provider: {name}")

    def capabilities(self) -> dict[str, Any]:
        openai = self.translation("openai-compatible", {})
        ocr = self.ocr.capabilities()
        ocr["configuredLanguages"] = self.settings.ocr_language_list
        ocr["defaultDirection"] = self.settings.ocr_default_direction
        return {
            "preprocessing": {
                "opencv-pillow": self.preprocessing.get_capabilities(),
                "realesrgan-onnx": self.realesrgan_onnx.get_capabilities(),
                "realesrgan-ncnn": self.realesrgan.get_capabilities(),
            },
            "detection": {
                "tesseract": ocr,
                "ppocr-v3": self.ppocr.get_capabilities(),
                "ppocr-v3+tesseract": self.union_detector.get_capabilities(),
            },
            "ocr": {"tesseract": ocr},
            "inpainting": {
                "opencv": self.inpainting.get_capabilities(),
                "lama-onnx": self.lama.get_capabilities(),
            },
            "translation": {
                "manual": self.manual.capabilities(),
                "mock": self.mock.capabilities(),
                "dictionary": self.dictionary.capabilities(),
                "argos-ja-zh": self.argos.capabilities(),
                "openai-compatible": openai.capabilities(),
            },
        }
