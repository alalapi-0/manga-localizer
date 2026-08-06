from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from manga_localizer.imaging.typesetting import default_cjk_font
from manga_localizer.providers.ocr import OCRUnavailable, TesseractOCRProvider
from manga_localizer.providers.translation import (
    DictionaryTranslationProvider,
    ManualTranslationProvider,
    MockTranslationProvider,
    OpenAICompatibleTranslationProvider,
    TranslationProviderError,
)


def test_translation_exact_interface_and_ordered_batches() -> None:
    manual = ManualTranslationProvider()
    assert manual.translate_text("日本語", []) == ""
    assert manual.translate_batch(["一", "二"]) == ["", ""]
    assert manual.get_capabilities()["automatic"] is False
    assert manual.health_check()["available"] is True

    mock = MockTranslationProvider()
    assert mock.translate_text("こんにちは") == "你好"
    assert mock.translate_batch(["ありがとう", "はい"]) == ["谢谢", "是"]
    assert mock.get_capabilities()["deterministic"] is True

    dictionary = DictionaryTranslationProvider({"猫": "猫咪", "黒猫": "黑猫"})
    assert dictionary.translate_text("黒猫と猫") == "黑猫と猫咪"
    assert dictionary.translate_batch(["猫", "黒猫"]) == ["猫咪", "黑猫"]
    assert dictionary.health_check()["entries"] == 2


def test_openai_compatible_is_text_only_bounded_and_uses_chat_completions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        user = json.loads(body["messages"][1]["content"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"译:{user['text']}"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleTranslationProvider(
        api_key="sk-memory-only-value",
        base_url="https://translator.test/v1",
        model="compatible-model",
        max_context_items=2,
        max_context_chars=5,
        max_text_chars=4,
        client=client,
    )
    translated = provider.translate_text(
        "本文过长了",
        ["abc", "def", "never-sent"],
        glossary={"語": "词"},
        character_names={"太郎": "太郎"},
        target_language="zh-TW",
    )
    assert translated == "译:本文过长"
    request = requests[0]
    assert str(request.url) == "https://translator.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-memory-only-value"
    body = json.loads(request.content)
    assert body["model"] == "compatible-model"
    assert [message["role"] for message in body["messages"]] == ["developer", "user"]
    user = json.loads(body["messages"][1]["content"])
    assert user["neighboringText"] == ["abc", "de"]
    serialized = json.dumps(body).lower()
    assert "image_url" not in serialized
    assert "data:image" not in serialized
    assert set(user) == {
        "text",
        "targetLanguage",
        "neighboringText",
        "glossary",
        "characterNames",
    }
    assert user["targetLanguage"] == "zh-TW"
    assert "Traditional Chinese" in body["messages"][0]["content"]
    assert "store" not in body
    assert provider.get_capabilities()["sendsImages"] is False
    assert provider.get_capabilities()["configurable"] is True
    assert provider.health_check()["configured"] is True

    assert provider.translate_batch(["一", "二"], ["ctx"]) == ["译:一", "译:二"]
    assert len(requests) == 3
    client.close()


def test_openai_compatible_error_never_includes_key_or_remote_body() -> None:
    secret = "sk-super-private-value"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"echo {secret}"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleTranslationProvider(
            api_key=secret,
            base_url="https://translator.test/v1",
            client=client,
        )
        with pytest.raises(TranslationProviderError) as caught:
            provider.translate_text("秘密")
    assert secret not in str(caught.value)
    assert "echo" not in str(caught.value)
    assert "HTTP 401" in str(caught.value)


def test_openai_compatible_plain_http_is_loopback_only() -> None:
    local = OpenAICompatibleTranslationProvider(
        api_key="test-only-key",
        base_url="http://127.0.0.1:18080/v1",
    )
    assert local.health_check()["baseUrl"] == "http://127.0.0.1:18080/v1"
    with pytest.raises(ValueError, match="loopback"):
        OpenAICompatibleTranslationProvider(
            api_key="test-only-key",
            base_url="http://translator.example/v1",
        )


def test_tesseract_capabilities_are_honest_and_missing_binary_never_mocks() -> None:
    provider = TesseractOCRProvider()
    health = provider.health_check()
    capabilities = provider.get_capabilities()
    assert capabilities["directions"]["horizontal"] == ("jpn" in health["languages"])
    assert capabilities["directions"]["vertical"] == ("jpn_vert" in health["languages"])

    missing = TesseractOCRProvider("definitely-not-a-tesseract-binary")
    assert missing.health_check()["available"] is False
    with pytest.raises(OCRUnavailable):
        missing.recognize_image(Image.new("RGB", (20, 20), "white"))


def test_tesseract_decodes_japanese_stdout_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "manga_localizer.providers.ocr.shutil.which",
        lambda _command: "/test/tesseract",
    )

    def fake_run(command: list[str], **options) -> subprocess.CompletedProcess[str]:
        assert command[0] == "/test/tesseract"
        assert options["encoding"] == "utf-8"
        assert options["errors"] == "replace"
        return subprocess.CompletedProcess(command, 0, stdout="日本語\n", stderr="")

    monkeypatch.setattr("manga_localizer.providers.ocr.subprocess.run", fake_run)
    assert TesseractOCRProvider()._run("--version").stdout == "日本語\n"


def test_real_tesseract_methods_execute_on_generated_art(tmp_path: Path) -> None:
    provider = TesseractOCRProvider()
    health = provider.health_check()
    if "jpn" not in health["languages"]:
        pytest.skip("Local Tesseract jpn language pack is not installed")
    font_path = default_cjk_font()
    if font_path is None:
        pytest.skip("No local CJK font is available")
    image = Image.new("RGB", (640, 180), "white")
    font = ImageFont.truetype(str(font_path), 88)
    ImageDraw.Draw(image).text((24, 30), "日本語です", font=font, fill="black")
    path = tmp_path / "generated-japanese.png"
    image.save(path)

    text = provider.recognize_image(path, direction="horizontal")
    detections = provider.detect_text_regions(path, direction="horizontal")
    region = provider.recognize_region(
        path,
        {"x": 0, "y": 0, "width": image.width, "height": image.height},
        direction="horizontal",
    )
    assert text.strip()
    assert detections
    assert region.text.strip()
    # Exact glyph accuracy varies across packaged Tesseract versions and CJK font rasterizers.
    # This contract verifies that the real Japanese engine executes and returns editable output;
    # accuracy remains a reviewed product result rather than a deterministic unit-test oracle.
    assert any(item.text.strip() for item in detections)
    assert region.confidence is None or 0 <= region.confidence <= 1
    assert region.width == image.width


def test_real_tesseract_auto_selects_vertical_japanese(tmp_path: Path) -> None:
    provider = TesseractOCRProvider()
    health = provider.health_check()
    if not {"jpn", "jpn_vert"}.issubset(health["languages"]):
        pytest.skip("Local horizontal and vertical Japanese language packs are required")
    font_path = default_cjk_font()
    if font_path is None:
        pytest.skip("No local CJK font is available")
    image = Image.new("RGB", (260, 700), "white")
    font = ImageFont.truetype(str(font_path), 96)
    draw = ImageDraw.Draw(image)
    y = 35
    for character in "日本語です":
        box = draw.textbbox((0, 0), character, font=font)
        character_width = box[2] - box[0]
        draw.text(((image.width - character_width) / 2, y), character, font=font, fill="black")
        y += 120
    path = tmp_path / "generated-vertical-japanese.png"
    image.save(path)

    detections = provider.detect_text_regions(path, direction="auto")
    recognized = provider.recognize_region(
        path,
        {"x": 0, "y": 0, "width": image.width, "height": image.height},
        direction="auto",
    )

    assert detections
    assert detections[0].direction == "vertical"
    assert recognized.direction == "vertical"
    assert recognized.text.strip()
    assert provider.recognize_image(image, direction="auto").strip()
    assert recognized.confidence is None or 0 <= recognized.confidence <= 1


def test_full_page_detector_falls_back_to_editable_contour_regions(tmp_path: Path) -> None:
    provider = TesseractOCRProvider()
    if "jpn" not in provider.health_check()["languages"]:
        pytest.skip("Local Tesseract jpn language pack is not installed")
    font_path = default_cjk_font()
    if font_path is None:
        pytest.skip("No local CJK font is available")
    image = Image.new("RGB", (900, 1200), "#f4f1e8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 860, 1160), outline="#171717", width=8)
    draw.line((450, 40, 450, 1160), fill="#171717", width=6)
    draw.ellipse((510, 130, 820, 430), fill="white", outline="#171717", width=5)
    draw.ellipse((90, 650, 390, 950), fill="white", outline="#171717", width=5)
    font = ImageFont.truetype(str(font_path), 46)
    draw.multiline_text((570, 220), "こんにちは\nせかい", font=font, fill="#111111", spacing=12)
    draw.text((145, 750), "テストです", font=font, fill="#111111")
    path = tmp_path / "full-page.png"
    image.save(path)

    detections = provider.detect_text_regions(path, direction="auto")
    assert len(detections) >= 2
    recognized = [
        provider.recognize_region(path, region.to_dict(), direction=region.direction).text
        for region in detections
    ]
    assert sum(bool(text.strip()) for text in recognized) >= 2
