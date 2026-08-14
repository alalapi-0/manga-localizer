from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging.preprocessing import PreprocessUnavailable
from manga_localizer.imaging.realesrgan_onnx import (
    MODEL_ID,
    MODEL_LICENSE,
    NATIVE_SCALE,
    RealESRGANONNXPreprocessProvider,
)


class FakeSession:
    def __init__(self) -> None:
        self.feeds: list[dict[str, np.ndarray]] = []

    @staticmethod
    def get_inputs() -> list[SimpleNamespace]:
        return [SimpleNamespace(name="input", shape=[1, 3, None, None])]

    def run(self, output_names, input_feed):
        assert output_names is None
        tensor = input_feed["input"]
        self.feeds.append({name: value.copy() for name, value in input_feed.items()})
        upscaled = np.repeat(np.repeat(tensor, NATIVE_SCALE, axis=2), NATIVE_SCALE, axis=3)
        return [upscaled]


def _configured_provider(
    tmp_path,
    session: FakeSession | None = None,
    **options,
) -> tuple[RealESRGANONNXPreprocessProvider, list[tuple[str, tuple[str, ...] | None]]]:
    model = tmp_path / "RealESRGAN_x4plus_anime_6B.onnx"
    model.write_bytes(b"test-placeholder")
    created: list[tuple[str, tuple[str, ...] | None]] = []
    fake = session or FakeSession()

    def factory(path: str, providers: tuple[str, ...] | None):
        created.append((path, providers))
        return fake

    return (
        RealESRGANONNXPreprocessProvider(model, session_factory=factory, **options),
        created,
    )


def test_health_and_capabilities_are_lazy_and_never_download(tmp_path) -> None:
    provider, created = _configured_provider(
        tmp_path,
        execution_providers=["CPUExecutionProvider"],
        tile_size=0,
        pre_pad=0,
    )
    health = provider.health_check()
    capabilities = provider.get_capabilities()
    assert health["available"] is True
    assert health["loaded"] is False
    assert health["model"] == MODEL_ID
    assert health["license"] == MODEL_LICENSE
    assert health["nativeScale"] == 4
    assert capabilities["aiUpscale"] is True
    assert capabilities["classicInterpolation"] is False
    assert capabilities["downloadsModelsAtStartup"] is False
    assert capabilities["scaleStrategy"] == "native-4x-then-lanczos-for-2-and-3"
    assert created == []


def test_unconfigured_missing_model_and_runtime_report_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    unconfigured = RealESRGANONNXPreprocessProvider()
    assert unconfigured.health_check()["available"] is False
    assert unconfigured.health_check()["configured"] is False

    missing = RealESRGANONNXPreprocessProvider(
        tmp_path / "missing.onnx",
        session_factory=lambda path, providers: FakeSession(),
    )
    assert missing.health_check()["modelExists"] is False
    with pytest.raises(PreprocessUnavailable, match="was not found"):
        missing.preprocess(Image.new("RGB", (8, 6)), enable_upscale=True)

    model = tmp_path / "model.onnx"
    model.touch()
    monkeypatch.setattr(
        "manga_localizer.imaging.realesrgan_onnx.importlib.util.find_spec",
        lambda name: None,
    )
    unavailable = RealESRGANONNXPreprocessProvider(model)
    health = unavailable.health_check()
    assert health["runtimeAvailable"] is False
    assert health["available"] is False
    assert health["error"] == "onnxruntime is not installed"
    with pytest.raises(PreprocessUnavailable, match="onnxruntime is not installed"):
        unavailable.preprocess(Image.new("RGB", (8, 6)), enable_upscale=True)


def test_native_4x_and_requested_2x_preserve_alpha_and_source(tmp_path) -> None:
    session = FakeSession()
    provider, created = _configured_provider(
        tmp_path,
        session,
        profile="off",
        tile_size=0,
        tile_pad=0,
        pre_pad=0,
    )
    pixels = np.zeros((6, 8, 4), dtype=np.uint8)
    pixels[..., :3] = (40, 90, 150)
    pixels[2:4, 3:5, :3] = (200, 10, 10)
    pixels[..., 3] = np.arange(8, dtype=np.uint8) * 30
    source = Image.fromarray(pixels, mode="RGBA")
    source_before = np.asarray(source).copy()

    native = provider.preprocess(source, enable_upscale=True, upscale_factor=4)
    requested = provider.preprocess(source, enable_upscale=True, upscale_factor=2)

    assert len(created) == 1
    assert provider.health_check()["loaded"] is True
    assert native.processed_size == (32, 24)
    assert requested.processed_size == (16, 12)
    assert native.image.mode == "RGBA"
    expected_alpha = source.getchannel("A").resize(native.image.size, Image.Resampling.LANCZOS)
    assert np.array_equal(np.asarray(native.image.getchannel("A")), np.asarray(expected_alpha))
    native_rgb = np.asarray(native.image.convert("RGB"))
    assert np.array_equal(native_rgb[8:16, 12:20][0, 0], np.array([200, 10, 10], dtype=np.uint8))
    assert np.array_equal(np.asarray(source), source_before)
    feed = session.feeds[0]["input"]
    assert feed.shape == (1, 3, 6, 8)
    assert feed.dtype == np.float32
    assert feed[0, 0, 0, 0] == pytest.approx(40 / 255)
    assert feed[0, 1, 0, 0] == pytest.approx(90 / 255)
    assert feed[0, 2, 0, 0] == pytest.approx(150 / 255)


def test_grayscale_sources_do_not_keep_model_chroma(tmp_path) -> None:
    class ChromaSession(FakeSession):
        def run(self, output_names, input_feed):
            upscaled = super().run(output_names, input_feed)[0]
            upscaled[:, 0] = np.clip(upscaled[:, 0] + 0.2, 0.0, 1.0)
            return [upscaled]

    session = ChromaSession()
    provider, _ = _configured_provider(
        tmp_path,
        session,
        profile="off",
        tile_size=0,
        tile_pad=0,
        pre_pad=0,
    )
    source = Image.new("L", (8, 6), 90)
    result = provider.preprocess(source, enable_upscale=True, upscale_factor=4)
    rgb = np.asarray(result.image.convert("RGB"))
    assert np.array_equal(rgb[..., 0], rgb[..., 1])
    assert np.array_equal(rgb[..., 1], rgb[..., 2])


def test_tiling_covers_the_full_canvas(tmp_path) -> None:
    session = FakeSession()
    provider, _ = _configured_provider(
        tmp_path,
        session,
        profile="off",
        tile_size=4,
        tile_pad=1,
        pre_pad=0,
    )
    source = Image.new("RGB", (6, 5), (12, 34, 56))
    result = provider.preprocess(source, enable_upscale=True, upscale_factor=4)
    assert result.processed_size == (24, 20)
    assert len(session.feeds) >= 2
    assert np.all(np.asarray(result.image.convert("RGB")) == (12, 34, 56))
