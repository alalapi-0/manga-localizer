from manga_localizer.providers.inpainting import InpaintingProvider, OpenCVInpaintingProvider
from manga_localizer.providers.ocr import OCRProvider, TesseractOCRProvider
from manga_localizer.providers.translation import (
    DictionaryTranslationProvider,
    ManualTranslationProvider,
    MockTranslationProvider,
    OpenAICompatibleTranslationProvider,
    TranslationProvider,
)
from manga_localizer.providers.translation_argos import ArgosJaZhTranslationProvider

__all__ = [
    "ArgosJaZhTranslationProvider",
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
