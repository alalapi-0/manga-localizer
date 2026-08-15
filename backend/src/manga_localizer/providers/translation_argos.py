from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from manga_localizer.providers.translation import (
    TranslationProviderError,
    TranslationUnavailable,
)

PACKAGE_JA_EN = "argos-ja-en"
PACKAGE_EN_ZH = "argos-en-zh"
PROVIDER_NAME = "argos-ja-zh"
MODEL_LICENSE = "CC-BY-4.0"
PIVOT = ("ja", "en", "zh")
PLACEHOLDER = "__T{index}__"
PLACEHOLDER_RE = re.compile(r"__T\s*(\d+)\s*__")
SENTENCE_RE = re.compile(r"(?<=[\u3002\uff01\uff1f!?])")


class _Translator(Protocol):
    def translate_batch(self, source: Sequence[Sequence[str]], **options: Any) -> Sequence[Any]: ...


class _Tokenizer(Protocol):
    def encode(self, sentence: str) -> list[str]: ...

    def decode(self, tokens: Sequence[str]) -> str: ...


class _Hop(Protocol):
    def translate(self, text: str) -> str: ...


type HopFactory = Callable[[Path], _Hop]


def _runtime_details() -> tuple[bool, str | None]:
    try:
        available = (
            importlib.util.find_spec("ctranslate2") is not None
            and importlib.util.find_spec("sentencepiece") is not None
        )
    except (ImportError, ValueError):
        available = False
    if not available:
        return False, None
    try:
        return True, importlib.metadata.version("ctranslate2")
    except importlib.metadata.PackageNotFoundError:
        return True, None


def package_is_ready(directory: Path | None) -> bool:
    if directory is None:
        return False
    root = directory.expanduser()
    if not root.is_dir():
        return False
    return (
        (root / "metadata.json").is_file()
        and (root / "sentencepiece.model").is_file()
        and (root / "model" / "model.bin").is_file()
    )


def _split_sentences(paragraph: str) -> list[str]:
    stripped = paragraph.strip()
    if not stripped:
        return [paragraph]
    if len(stripped) <= 250:
        return [paragraph]
    parts = [part for part in SENTENCE_RE.split(paragraph) if part != ""]
    return parts or [paragraph]


class SentencePieceTokenizer:
    def __init__(self, model_file: Path):
        self.model_file = model_file
        self._processor: Any | None = None

    def _processor_instance(self) -> Any:
        if self._processor is None:
            try:
                import sentencepiece as sentencepiece_module
            except ImportError as error:
                raise TranslationUnavailable("sentencepiece is not installed") from error
            self._processor = sentencepiece_module.SentencePieceProcessor(
                model_file=str(self.model_file)
            )
        return self._processor

    def encode(self, sentence: str) -> list[str]:
        return list(self._processor_instance().encode(sentence, out_type=str))

    def decode(self, tokens: Sequence[str]) -> str:
        decoded = self._processor_instance().decode_pieces(list(tokens)).replace("▁", " ")
        return decoded[1:] if decoded.startswith(" ") else decoded


class ArgosPackageHop:
    def __init__(
        self,
        directory: Path,
        *,
        translator_factory: Callable[[str], _Translator] | None = None,
        tokenizer_factory: Callable[[Path], _Tokenizer] | None = None,
    ):
        self.directory = directory.expanduser()
        self._translator_factory = translator_factory
        self._tokenizer_factory = tokenizer_factory
        self._translator: _Translator | None = None
        self._tokenizer: _Tokenizer | None = None
        self._lock = threading.Lock()
        metadata_path = self.directory / "metadata.json"
        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            try:
                loaded = json.loads(metadata_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                metadata = loaded
        self.to_code = str(metadata.get("to_code") or "")
        self.from_code = str(metadata.get("from_code") or "")

    def _load(self) -> tuple[_Translator, _Tokenizer]:
        with self._lock:
            if self._translator is not None and self._tokenizer is not None:
                return self._translator, self._tokenizer
            if not package_is_ready(self.directory):
                raise TranslationUnavailable(f"Argos package is incomplete: {self.directory.name}")
            tokenizer_path = self.directory / "sentencepiece.model"
            if self._tokenizer_factory is not None:
                tokenizer = self._tokenizer_factory(tokenizer_path)
            else:
                tokenizer = SentencePieceTokenizer(tokenizer_path)
            model_dir = str(self.directory / "model")
            if self._translator_factory is not None:
                translator = self._translator_factory(model_dir)
            else:
                try:
                    import ctranslate2
                except ImportError as error:
                    raise TranslationUnavailable("ctranslate2 is not installed") from error
                try:
                    translator = ctranslate2.Translator(model_dir, device="cpu")
                except Exception as error:
                    raise TranslationUnavailable(
                        f"Argos CTranslate2 model could not be loaded: {type(error).__name__}"
                    ) from error
            self._translator = translator
            self._tokenizer = tokenizer
            return translator, tokenizer

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        translator, tokenizer = self._load()
        joiner = "" if self.to_code in {"zh", "ja", "zt"} else " "
        translated_paragraphs: list[str] = []
        for paragraph in text.split("\n"):
            pieces: list[str] = []
            for sentence in _split_sentences(paragraph):
                if not sentence.strip():
                    pieces.append(sentence)
                    continue
                encoded = tokenizer.encode(sentence)
                if not encoded:
                    pieces.append(sentence)
                    continue
                try:
                    results = translator.translate_batch(
                        [encoded],
                        replace_unknowns=True,
                        beam_size=2,
                    )
                    tokens = results[0].hypotheses[0]
                except Exception as error:
                    raise TranslationProviderError(
                        f"Argos translation failed ({type(error).__name__})"
                    ) from error
                pieces.append(tokenizer.decode(tokens))
            translated_paragraphs.append(joiner.join(pieces))
        return "\n".join(translated_paragraphs)


def _protect(text: str, mapping: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    protected = text
    restored: dict[str, str] = {}
    for index, (source, target) in enumerate(
        sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True)
    ):
        source_text = str(source)
        target_text = str(target)
        if not source_text or source_text not in protected:
            continue
        token = PLACEHOLDER.format(index=index)
        protected = protected.replace(source_text, token)
        restored[token] = target_text
    return protected, restored


def _restore(text: str, restored: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = PLACEHOLDER.format(index=int(match.group(1)))
        return restored.get(token, match.group(0))

    return PLACEHOLDER_RE.sub(replace, text)


class ArgosJaZhTranslationProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        ja_en_dir: Path | None = None,
        en_zh_dir: Path | None = None,
        *,
        hop_factory: HopFactory | None = None,
    ):
        self.ja_en_dir = ja_en_dir
        self.en_zh_dir = en_zh_dir
        self._hop_factory = hop_factory
        self._ja_en: _Hop | None = None
        self._en_zh: _Hop | None = None
        self._lock = threading.Lock()

    def _unavailable_reason(self) -> str | None:
        if self._hop_factory is not None:
            return None
        runtime_available, _version = _runtime_details()
        if not runtime_available:
            return "ctranslate2 and sentencepiece are not installed"
        if not package_is_ready(self.ja_en_dir):
            return "Argos Japanese-English package is not installed"
        if not package_is_ready(self.en_zh_dir):
            return "Argos English-Chinese package is not installed"
        return None

    def _hops(self) -> tuple[_Hop, _Hop]:
        reason = self._unavailable_reason()
        if reason:
            raise TranslationUnavailable(reason)
        with self._lock:
            if self._ja_en is not None and self._en_zh is not None:
                return self._ja_en, self._en_zh
            assert self.ja_en_dir is not None
            assert self.en_zh_dir is not None
            factory = self._hop_factory or (lambda path: ArgosPackageHop(path))
            self._ja_en = factory(self.ja_en_dir)
            self._en_zh = factory(self.en_zh_dir)
            return self._ja_en, self._en_zh

    def translate(
        self,
        text: str,
        *,
        glossary: Mapping[str, str] | None = None,
        character_names: Mapping[str, str] | None = None,
        target_language: str = "zh-CN",
        **options: Any,
    ) -> str:
        source = text.strip()
        if not source:
            return ""
        target_language = str(options.get("targetLanguage") or target_language)
        replacements = {**dict(glossary or {}), **dict(character_names or {})}
        if source in replacements:
            return replacements[source]
        ja_en, en_zh = self._hops()
        protected, restored = _protect(source, replacements)
        english = ja_en.translate(protected)
        if target_language in {"en", "en-US", "en-GB"}:
            return _restore(english, restored).strip()
        if target_language not in {"zh-CN", "zh", "zh-Hans"}:
            raise TranslationProviderError(
                "argos-ja-zh currently produces Simplified Chinese; "
                "choose zh-CN or a remote translator for other targets"
            )
        chinese = en_zh.translate(english)
        return _restore(chinese, restored).strip()

    def translate_text(self, text: str, context: Sequence[str] = (), **options: Any) -> str:
        del context
        return self.translate(text, **options)

    def translate_batch(
        self, items: Sequence[str], context: Sequence[str] = (), **options: Any
    ) -> list[str]:
        return [self.translate_text(item, context, **options) for item in items]

    def health(self) -> dict[str, Any]:
        runtime_available, runtime_version = _runtime_details()
        reason = self._unavailable_reason()
        return {
            "available": reason is None,
            "loaded": self._ja_en is not None and self._en_zh is not None,
            "error": reason,
            "runtime": runtime_version,
            "runtimeAvailable": runtime_available,
            "jaEnPackage": package_is_ready(self.ja_en_dir),
            "enZhPackage": package_is_ready(self.en_zh_dir),
            "license": MODEL_LICENSE,
            "pivot": list(PIVOT),
        }

    def capabilities(self) -> dict[str, Any]:
        health = self.health()
        return {
            "provider": self.name,
            "available": health["available"],
            "remote": False,
            "sendsImages": False,
            "downloadsModelsAtStartup": False,
            "deterministic": False,
            "pivot": list(PIVOT),
            "license": MODEL_LICENSE,
            "error": health["error"],
        }

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def get_capabilities(self) -> dict[str, Any]:
        return self.capabilities()
