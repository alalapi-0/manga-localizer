from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx

from manga_localizer.logging_utils import redact
from manga_localizer.security import validate_remote_base_url


class TranslationProviderError(RuntimeError):
    pass


class TranslationUnavailable(TranslationProviderError):
    pass


class TranslationProvider(Protocol):
    name: str

    def translate(
        self,
        text: str,
        *,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
        character_names: Mapping[str, str] | None = None,
        **options: Any,
    ) -> str: ...

    def translate_text(
        self,
        text: str,
        context: Sequence[str] = (),
        **options: Any,
    ) -> str: ...

    def translate_batch(
        self,
        items: Sequence[str],
        context: Sequence[str] = (),
        **options: Any,
    ) -> list[str]: ...

    def health_check(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


class ManualTranslationProvider:
    name = "manual"

    def translate(
        self,
        text: str,
        *,
        manual_text: str | None = None,
        **_: Any,
    ) -> str:
        return "" if manual_text is None else manual_text

    def translate_text(self, text: str, context: Sequence[str] = (), **options: Any) -> str:
        return self.translate(text, context=context, **options)

    def translate_batch(
        self, items: Sequence[str], context: Sequence[str] = (), **options: Any
    ) -> list[str]:
        return [self.translate_text(item, context, **options) for item in items]

    def health(self) -> dict[str, Any]:
        return {"available": True}

    def capabilities(self) -> dict[str, Any]:
        return {"provider": self.name, "available": True, "automatic": False, "remote": False}

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def get_capabilities(self) -> dict[str, Any]:
        return self.capabilities()


class MockTranslationProvider:
    name = "mock"

    _WORDS: ClassVar[dict[str, str]] = {
        "こんにちは": "你好",
        "ありがとう": "谢谢",
        "さようなら": "再见",
        "はい": "是",
        "いいえ": "不",
    }

    def translate(self, text: str, **_: Any) -> str:
        translated = text
        for source, target in self._WORDS.items():
            translated = translated.replace(source, target)
        return translated if translated != text else f"【模拟译文】{text}"

    def translate_text(self, text: str, context: Sequence[str] = (), **options: Any) -> str:
        return self.translate(text, context=context, **options)

    def translate_batch(
        self, items: Sequence[str], context: Sequence[str] = (), **options: Any
    ) -> list[str]:
        return [self.translate_text(item, context, **options) for item in items]

    def health(self) -> dict[str, Any]:
        return {"available": True}

    def capabilities(self) -> dict[str, Any]:
        return {"provider": self.name, "available": True, "deterministic": True, "remote": False}

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def get_capabilities(self) -> dict[str, Any]:
        return self.capabilities()


class DictionaryTranslationProvider:
    name = "dictionary"

    def __init__(self, entries: Mapping[str, str] | None = None):
        self.entries = dict(entries or {})

    @classmethod
    def from_tsv(cls, path: Path) -> DictionaryTranslationProvider:
        entries: dict[str, str] = {}
        for line in path.read_text("utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            source, separator, target = line.partition("\t")
            if separator and source:
                entries[source] = target
        return cls(entries)

    def translate(
        self,
        text: str,
        *,
        glossary: Mapping[str, str] | None = None,
        **_: Any,
    ) -> str:
        replacements = {**self.entries, **dict(glossary or {})}
        if not replacements:
            return text
        pattern = re.compile(
            "|".join(re.escape(source) for source in sorted(replacements, key=len, reverse=True))
        )
        return pattern.sub(lambda match: replacements[match.group(0)], text)

    def translate_text(self, text: str, context: Sequence[str] = (), **options: Any) -> str:
        return self.translate(text, context=context, **options)

    def translate_batch(
        self, items: Sequence[str], context: Sequence[str] = (), **options: Any
    ) -> list[str]:
        return [self.translate_text(item, context, **options) for item in items]

    def health(self) -> dict[str, Any]:
        return {"available": True, "entries": len(self.entries)}

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "deterministic": True,
            "remote": False,
            "entries": len(self.entries),
        }

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def get_capabilities(self) -> dict[str, Any]:
        return self.capabilities()


class OpenAICompatibleTranslationProvider:
    name = "openai-compatible"

    _TARGET_NAMES: ClassVar[dict[str, str]] = {
        "zh-CN": "Simplified Chinese",
        "zh-TW": "Traditional Chinese",
        "en": "English",
    }

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1-mini",
        timeout: float = 30.0,
        max_context_items: int = 6,
        max_context_chars: int = 4_000,
        max_text_chars: int = 8_000,
        client: httpx.Client | None = None,
    ):
        self._api_key = (
            api_key or os.getenv("MANGA_LOCALIZER_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        )
        self.base_url = validate_remote_base_url(base_url)
        self.model = model
        self.timeout = timeout
        self.max_context_items = max(0, max_context_items)
        self.max_context_chars = max(0, max_context_chars)
        self.max_text_chars = max(1, max_text_chars)
        self._client = client

    def _limited_context(self, context: Sequence[str]) -> list[str]:
        remaining = self.max_context_chars
        limited: list[str] = []
        for item in list(context)[: self.max_context_items]:
            if remaining <= 0:
                break
            clean = str(item)[:remaining]
            limited.append(clean)
            remaining -= len(clean)
        return limited

    @staticmethod
    def _limited_mapping(mapping: Mapping[str, str] | None, limit: int = 100) -> dict[str, str]:
        if not mapping:
            return {}
        return {str(key)[:200]: str(value)[:200] for key, value in list(mapping.items())[:limit]}

    def translate(
        self,
        text: str,
        *,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
        character_names: Mapping[str, str] | None = None,
        target_language: str = "zh-CN",
        **options: Any,
    ) -> str:
        if not self._api_key:
            raise TranslationUnavailable("OpenAI-compatible API key is not configured")
        target_language = str(options.get("targetLanguage") or target_language)
        target_name = self._TARGET_NAMES.get(target_language, target_language)
        primary = text[: self.max_text_chars]
        user_payload = {
            "text": primary,
            "targetLanguage": target_language,
            "neighboringText": self._limited_context(context),
            "glossary": self._limited_mapping(glossary),
            "characterNames": self._limited_mapping(character_names),
        }
        request = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "developer",
                    "content": (
                        f"Translate Japanese manga dialogue into concise {target_name}. "
                        "Return only the translation. Images and project files are never provided."
                    ),
                },
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        client = self._client or httpx.Client(timeout=self.timeout)
        owns_client = self._client is None
        try:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=request,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise TranslationProviderError(
                    "OpenAI-compatible response contained no translation"
                )
            return content.strip()
        except TranslationProviderError:
            raise
        except httpx.HTTPStatusError as error:
            raise TranslationProviderError(
                f"OpenAI-compatible request failed with HTTP {error.response.status_code}"
            ) from error
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            safe_type = type(error).__name__
            raise TranslationProviderError(
                f"OpenAI-compatible request failed ({safe_type}): {redact(error)}"
            ) from error
        finally:
            if owns_client:
                client.close()

    def translate_text(self, text: str, context: Sequence[str] = (), **options: Any) -> str:
        return self.translate(text, context=context, **options)

    def translate_batch(
        self, items: Sequence[str], context: Sequence[str] = (), **options: Any
    ) -> list[str]:
        # One bounded request per item preserves order and avoids whole-book disclosure.
        return [self.translate_text(item, context, **options) for item in items]

    def health(self) -> dict[str, Any]:
        return {
            "available": bool(self._api_key),
            "configured": bool(self._api_key),
            "baseUrl": self.base_url,
            "model": self.model,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": bool(self._api_key),
            # The provider can be selected before it is available so the user can
            # enter an in-memory session credential. This is deliberately separate
            # from ``available``: queue execution must remain blocked until a key is
            # actually configured.
            "configurable": True,
            "remote": True,
            "sendsImages": False,
            "maxContextItems": self.max_context_items,
            "maxContextChars": self.max_context_chars,
            "maxTextChars": self.max_text_chars,
        }

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def get_capabilities(self) -> dict[str, Any]:
        return self.capabilities()


# Concise compatibility names for plugin-style provider discovery.
ManualProvider = ManualTranslationProvider
MockProvider = MockTranslationProvider
DictionaryProvider = DictionaryTranslationProvider
OpenAICompatibleProvider = OpenAICompatibleTranslationProvider
