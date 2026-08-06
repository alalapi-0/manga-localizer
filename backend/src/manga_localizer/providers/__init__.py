from manga_localizer.providers.inpainting import InpaintingProvider, OpenCVInpaintingProvider
from manga_localizer.providers.ocr import OCRProvider, TesseractOCRProvider
from manga_localizer.providers.translation import (
    DictionaryTranslationProvider,
    ManualTranslationProvider,
    MockTranslationProvider,
    OpenAICompatibleTranslationProvider,
    TranslationProvider,
)

__all__ = [
    "DictionaryTranslationProvider",
    "InpaintingProvider",
    "ManualTranslationProvider",
    "MockTranslationProvider",
    "OCRProvider",
    "OpenAICompatibleTranslationProvider",
    "OpenCVInpaintingProvider",
    "TesseractOCRProvider",
    "TranslationProvider",
]
