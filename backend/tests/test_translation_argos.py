from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from manga_localizer.providers.translation import (
    TranslationProviderError,
    TranslationUnavailable,
)
from manga_localizer.providers.translation_argos import (
    ArgosJaZhTranslationProvider,
    ArgosPackageHop,
    package_is_ready,
)


class FakeHop:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def translate(self, text: str) -> str:
        return self.mapping.get(text, f"EN:{text}")


class FakeTranslator:
    def translate_batch(self, source, **options):
        del options
        joined = "".join(source[0])
        return [SimpleNamespace(hypotheses=[[joined, "译"]])]


class FakeTokenizer:
    def encode(self, sentence: str) -> list[str]:
        return [sentence]

    def decode(self, tokens) -> str:
        return "".join(tokens)


def _write_package(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "metadata.json").write_text(
        '{"from_code":"ja","to_code":"en"}',
        encoding="utf-8",
    )
    (path / "model").mkdir()
    (path / "model" / "model.bin").write_bytes(b"placeholder")
    (path / "sentencepiece.model").write_bytes(b"placeholder")


def test_argos_unavailable_without_runtime_or_packages(tmp_path: Path) -> None:
    missing = ArgosJaZhTranslationProvider(tmp_path / "missing-a", tmp_path / "missing-b")
    health = missing.health_check()
    assert health["available"] is False
    assert missing.get_capabilities()["remote"] is False
    assert missing.get_capabilities()["downloadsModelsAtStartup"] is False
    with pytest.raises(TranslationUnavailable):
        missing.translate_text("こんにちは")


def test_argos_pivot_glossary_and_english_target(tmp_path: Path) -> None:
    ja_en = tmp_path / "argos-ja-en"
    en_zh = tmp_path / "argos-en-zh"
    _write_package(ja_en)
    _write_package(en_zh)

    def factory(path: Path) -> FakeHop:
        if path.name == "argos-ja-en":
            return FakeHop(
                {
                    "こんにちは": "Hello",
                    "待って __T0__": "Wait __T0__",
                    "太郎": "Taro",
                }
            )
        return FakeHop(
            {
                "Hello": "你好",
                "Wait __T0__": "等一下 __T0__",
            }
        )

    provider = ArgosJaZhTranslationProvider(ja_en, en_zh, hop_factory=factory)
    assert provider.health_check()["available"] is True
    assert provider.translate_text("こんにちは") == "你好"
    assert provider.translate_text("待って 太郎", character_names={"太郎": "太郎"}) == "等一下 太郎"
    assert provider.translate_text("こんにちは", target_language="en") == "Hello"
    with pytest.raises(TranslationProviderError, match="Simplified Chinese"):
        provider.translate_text("こんにちは", target_language="zh-TW")
    assert provider.translate_text("こんにちは", glossary={"こんにちは": "您好"}) == "您好"


def test_argos_package_hop_uses_injected_translator(tmp_path: Path) -> None:
    package = tmp_path / "argos-ja-en"
    _write_package(package)
    hop = ArgosPackageHop(
        package,
        translator_factory=lambda _path: FakeTranslator(),
        tokenizer_factory=lambda _path: FakeTokenizer(),
    )
    assert hop.translate("hello") == "hello译"
    assert package_is_ready(package) is True
    assert package_is_ready(tmp_path / "missing") is False
