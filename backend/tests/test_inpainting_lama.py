from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from manga_localizer.providers.inpainting_lama import (
    LaMaONNXInpaintingProvider,
    LaMaProviderError,
    LaMaUnavailable,
)


class FakeSession:
    def __init__(self, color_bgr: tuple[int, int, int] = (15, 80, 240)) -> None:
        self.color_bgr = color_bgr
        self.feeds: list[dict[str, np.ndarray]] = []

    @staticmethod
    def get_inputs() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="image", shape=[1, 3, 512, 512]),
            SimpleNamespace(name="mask", shape=[1, 1, 512, 512]),
        ]

    def run(self, output_names, input_feed):
        assert output_names is None
        self.feeds.append({name: value.copy() for name, value in input_feed.items()})
        output = np.zeros((1, 3, 512, 512), dtype=np.float32)
        output[0, 0] = self.color_bgr[0]
        output[0, 1] = self.color_bgr[1]
        output[0, 2] = self.color_bgr[2]
        return [output]


def _configured_provider(
    tmp_path: Path,
    session: FakeSession | None = None,
    **options,
) -> tuple[LaMaONNXInpaintingProvider, list[tuple[str, tuple[str, ...] | None]]]:
    model = tmp_path / "inpainting_lama_2025jan.onnx"
    model.write_bytes(b"test-placeholder")
    created: list[tuple[str, tuple[str, ...] | None]] = []
    fake = session or FakeSession()

    def factory(path: str, providers: tuple[str, ...] | None):
        created.append((path, providers))
        return fake

    return (
        LaMaONNXInpaintingProvider(model, session_factory=factory, **options),
        created,
    )


def test_health_and_capabilities_are_lazy_and_never_download(tmp_path: Path) -> None:
    provider, created = _configured_provider(
        tmp_path,
        execution_providers=["CPUExecutionProvider"],
    )
    health = provider.health_check()
    capabilities = provider.get_capabilities()
    assert health == {
        "available": True,
        "provider": "lama-onnx",
        "configured": True,
        "modelPath": str(provider.model_path),
        "modelExists": True,
        "runtime": "injected",
        "runtimeAvailable": True,
        "runtimeVersion": None,
        "loaded": False,
        "error": None,
    }
    assert capabilities["modelInputSize"] == [512, 512]
    assert capabilities["inputNames"] == ["image", "mask"]
    assert capabilities["preservesAlpha"] is True
    assert capabilities["softMaskComposite"] is True
    assert capabilities["textPolarities"] == ["auto", "dark", "light"]
    assert capabilities["downloadsModelsAtStartup"] is False
    assert capabilities["executionProviders"] == ["CPUExecutionProvider"]
    assert created == []


def test_unconfigured_missing_model_and_runtime_report_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unconfigured = LaMaONNXInpaintingProvider()
    assert unconfigured.health_check()["available"] is False
    assert unconfigured.health_check()["configured"] is False

    missing = LaMaONNXInpaintingProvider(
        tmp_path / "missing.onnx",
        session_factory=lambda path, providers: FakeSession(),
    )
    assert missing.health_check()["modelExists"] is False
    with pytest.raises(LaMaUnavailable, match="was not found"):
        missing.inpaint(
            Image.new("RGB", (8, 8)),
            Image.new("L", (8, 8), 255),
        )

    model = tmp_path / "model.onnx"
    model.touch()
    monkeypatch.setattr(
        "manga_localizer.providers.inpainting_lama.importlib.util.find_spec",
        lambda name: None,
    )
    unavailable = LaMaONNXInpaintingProvider(model)
    health = unavailable.health_check()
    assert health["runtimeAvailable"] is False
    assert health["available"] is False
    assert health["error"] == "onnxruntime is not installed"
    with pytest.raises(LaMaUnavailable, match="onnxruntime is not installed"):
        unavailable.inpaint(Image.new("RGB", (8, 8)), Image.new("L", (8, 8), 255))


def test_rgba_inference_uses_official_tensor_contract_and_changes_only_mask(
    tmp_path: Path,
) -> None:
    session = FakeSession(color_bgr=(12, 34, 220))
    provider, created = _configured_provider(tmp_path, session, context_padding=7, feather=0)
    pixels = np.zeros((36, 48, 4), dtype=np.uint8)
    pixels[..., :3] = (25, 50, 75)
    pixels[..., 3] = np.arange(48, dtype=np.uint8)
    source = Image.fromarray(pixels, mode="RGBA")
    mask = np.zeros((36, 48), dtype=np.uint8)
    mask[13:23, 19:30] = 255
    source_before = np.asarray(source).copy()
    mask_before = mask.copy()

    result = provider.inpaint(source, mask)

    assert len(created) == 1
    assert provider.health_check()["loaded"] is True
    assert result.mode == "RGBA"
    assert result.size == source.size
    assert np.array_equal(np.asarray(result.getchannel("A")), source_before[..., 3])
    result_rgb = np.asarray(result.convert("RGB"))
    original_rgb = source_before[..., :3]
    assert np.array_equal(result_rgb[mask == 0], original_rgb[mask == 0])
    assert np.all(result_rgb[mask > 0] == np.array([220, 34, 12], dtype=np.uint8))
    assert np.array_equal(np.asarray(source), source_before)
    assert np.array_equal(mask, mask_before)

    feed = session.feeds[0]
    assert set(feed) == {"image", "mask"}
    assert feed["image"].shape == (1, 3, 512, 512)
    assert feed["mask"].shape == (1, 1, 512, 512)
    assert feed["image"].dtype == np.float32
    assert feed["mask"].dtype == np.float32
    assert set(np.unique(feed["mask"])) == {0.0, 1.0}
    # Input is BGR: the first channel contains the source's blue value.
    assert feed["image"][0, 0, 0, 0] == pytest.approx(75 * 0.00392)


def test_soft_composite_feathers_inward_and_keeps_zero_mask_pixels_exact(tmp_path: Path) -> None:
    provider, _ = _configured_provider(
        tmp_path,
        FakeSession(color_bgr=(255, 255, 255)),
        context_padding=2,
        feather=3,
    )
    source = np.zeros((30, 30, 3), dtype=np.uint8)
    mask = np.zeros((30, 30), dtype=np.uint8)
    mask[8:22, 8:22] = 255

    result = np.asarray(provider.inpaint(source, mask))

    assert np.all(result[mask == 0] == 0)
    assert 0 < result[8, 8, 0] < 255
    assert result[15, 15, 0] == 255


def test_path_pil_and_ndarray_inputs_preserve_files_and_alpha(tmp_path: Path) -> None:
    provider, _ = _configured_provider(tmp_path, feather=0)
    image_path = tmp_path / "source.png"
    mask_path = tmp_path / "mask.png"
    pixels = np.zeros((12, 16, 4), dtype=np.uint8)
    pixels[..., :3] = (10, 20, 30)
    pixels[..., 3] = 123
    Image.fromarray(pixels, mode="RGBA").save(image_path)
    mask = np.zeros((12, 16), dtype=np.uint8)
    mask[4:8, 6:10] = 255
    Image.fromarray(mask, mode="L").save(mask_path)
    image_bytes = image_path.read_bytes()
    mask_bytes = mask_path.read_bytes()

    from_paths = provider.inpaint(image_path, mask_path)
    from_pil = provider.inpaint(Image.open(image_path), Image.open(mask_path))
    from_arrays = provider.inpaint(pixels, mask)

    assert np.array_equal(np.asarray(from_paths), np.asarray(from_pil))
    assert np.array_equal(np.asarray(from_paths), np.asarray(from_arrays))
    assert image_path.read_bytes() == image_bytes
    assert mask_path.read_bytes() == mask_bytes


def test_empty_mask_is_copy_without_loading_session(tmp_path: Path) -> None:
    provider, created = _configured_provider(tmp_path)
    source = Image.new("RGB", (10, 9), (20, 30, 40))

    result = provider.inpaint(source, np.zeros((9, 10), dtype=np.uint8))

    assert result is not source
    assert np.array_equal(np.asarray(result), np.asarray(source))
    assert created == []


@pytest.mark.parametrize(
    ("factory_options", "message"),
    [
        ({"context_padding": -1}, "context_padding"),
        ({"context_padding": True}, "context_padding"),
        ({"feather": -1}, "feather"),
        ({"feather": 256}, "feather"),
        ({"execution_providers": "CPUExecutionProvider"}, "execution_providers"),
    ],
)
def test_invalid_configuration_is_rejected(
    factory_options: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        LaMaONNXInpaintingProvider(**factory_options)


def test_invalid_images_masks_dimensions_and_options_are_rejected(tmp_path: Path) -> None:
    provider, _ = _configured_provider(tmp_path)
    image = Image.new("RGB", (8, 8))
    mask = Image.new("L", (8, 8), 255)
    with pytest.raises(ValueError, match="HxWx3"):
        provider.inpaint(np.zeros((8, 8), dtype=np.uint8), mask)
    with pytest.raises(ValueError, match="single-channel"):
        provider.inpaint(image, Image.new("RGB", (8, 8)))
    with pytest.raises(ValueError, match="dimensions differ"):
        provider.inpaint(image, Image.new("L", (7, 8)))
    with pytest.raises(ValueError, match="uint8 or bool"):
        provider.inpaint(image, np.ones((8, 8), dtype=np.float32))
    with pytest.raises(TypeError, match="Unknown"):
        provider.inpaint(image, mask, radius=3)


def test_model_contract_output_and_session_failures_are_clear(tmp_path: Path) -> None:
    bad_inputs = FakeSession()
    bad_inputs.get_inputs = lambda: [SimpleNamespace(name="wrong", shape=[1, 3, 512, 512])]
    provider, _ = _configured_provider(tmp_path, bad_inputs)
    with pytest.raises(LaMaProviderError, match="'image' and 'mask'"):
        provider.inpaint(Image.new("RGB", (4, 4)), Image.new("L", (4, 4), 255))

    class BadOutputSession(FakeSession):
        def run(self, output_names, input_feed):
            return [np.zeros((1, 4, 512, 512), dtype=np.float32)]

    provider, _ = _configured_provider(tmp_path, BadOutputSession())
    with pytest.raises(LaMaProviderError, match="output must have shape"):
        provider.inpaint(Image.new("RGB", (4, 4)), Image.new("L", (4, 4), 255))

    class FailingSession(FakeSession):
        def run(self, output_names, input_feed):
            raise RuntimeError("backend execution failed")

    provider, _ = _configured_provider(tmp_path, FailingSession())
    with pytest.raises(LaMaProviderError, match="inference failed: backend execution failed"):
        provider.inpaint(Image.new("RGB", (4, 4)), Image.new("L", (4, 4), 255))


def test_session_initialization_and_inference_are_concurrency_safe(tmp_path: Path) -> None:
    class TrackingSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def run(self, output_names, input_feed):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            try:
                return super().run(output_names, input_feed)
            finally:
                with self.lock:
                    self.active -= 1

    session = TrackingSession()
    provider, created = _configured_provider(tmp_path, session, feather=0)
    image = Image.new("RGB", (12, 12), "black")
    mask = Image.new("L", (12, 12), 255)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: provider.inpaint(image, mask), range(8)))

    assert len(created) == 1
    assert session.max_active == 1
    assert all(result.size == image.size for result in results)
