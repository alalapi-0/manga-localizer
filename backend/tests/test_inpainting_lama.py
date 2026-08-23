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
    COMPONENT_CONTEXT_PADDING,
    COMPONENT_INFERENCE_PADDING,
    DEFAULT_INFERENCE_PADDING,
    TILE_OVERLAP,
    LaMaONNXInpaintingProvider,
    LaMaProviderError,
    LaMaUnavailable,
    _coverage_preserving_overview_mask,
    _padded_model_tile,
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
    assert capabilities["aspectPreservingInference"] is True
    assert capabilities["tiledInference"] is True
    assert capabilities["componentwiseCandidate"] is True
    assert capabilities["tileSize"] == 512
    assert capabilities["tileOverlap"] == TILE_OVERLAP
    assert capabilities["inputNames"] == ["image", "mask"]
    assert capabilities["preservesAlpha"] is True
    assert capabilities["softMaskComposite"] is True
    assert capabilities["inferenceMaskPadding"] == DEFAULT_INFERENCE_PADDING
    assert capabilities["editableMask"] is True
    assert capabilities["maskEditVersion"] == 1
    assert capabilities["maskModes"] == ["text", "region", "manual"]
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


def test_inference_padding_hides_glyph_edges_but_keeps_review_mask_authoritative(
    tmp_path: Path,
) -> None:
    session = FakeSession(color_bgr=(255, 255, 255))
    provider, _ = _configured_provider(
        tmp_path,
        session,
        context_padding=8,
        inference_padding=4,
        feather=0,
    )
    source = np.zeros((48, 64, 3), dtype=np.uint8)
    source[20:28, 29:35] = 190
    mask = np.zeros((48, 64), dtype=np.uint8)
    mask[21:27, 30:34] = 255
    baseline_session = FakeSession(color_bgr=(255, 255, 255))
    baseline_provider, _ = _configured_provider(
        tmp_path,
        baseline_session,
        context_padding=8,
        inference_padding=0,
        feather=0,
    )

    result = np.asarray(provider.inpaint(source, mask))
    baseline_provider.inpaint(source, mask)

    assert np.array_equal(result[mask == 0], source[mask == 0])
    assert np.all(result[mask > 0] == 255)
    inference_feed = session.feeds[0]["mask"][0, 0]
    baseline_feed = baseline_session.feeds[0]["mask"][0, 0]
    assert np.count_nonzero(inference_feed) > np.count_nonzero(baseline_feed)


def test_non_square_crop_is_native_resolution_and_letterboxed(tmp_path: Path) -> None:
    session = FakeSession(color_bgr=(20, 40, 60))
    provider, _ = _configured_provider(
        tmp_path,
        session,
        context_padding=0,
        inference_padding=0,
        feather=0,
    )
    source = np.zeros((90, 50, 3), dtype=np.uint8)
    source[..., 0] = np.arange(50, dtype=np.uint8)
    source[..., 1] = np.arange(90, dtype=np.uint8)[:, None]
    mask = np.zeros((90, 50), dtype=np.uint8)
    mask[20:70, 15:35] = 255

    provider.inpaint(source, mask)

    assert len(session.feeds) == 1
    crop_rgb = source[20:70, 15:35]
    crop_mask = mask[20:70, 15:35]
    padded_rgb, padded_mask, content = _padded_model_tile(crop_rgb, crop_mask)
    assert padded_rgb.shape == (512, 512, 3)
    assert padded_mask.shape == (512, 512)
    assert content[0].stop - content[0].start == 50
    assert content[1].stop - content[1].start == 20
    assert np.array_equal(padded_rgb[content], crop_rgb)
    assert np.array_equal(padded_mask[content], crop_mask)


@pytest.mark.parametrize("shape", [(1, 1), (2, 3)])
def test_padded_tile_never_exposes_copies_of_masked_edge_pixels(
    shape: tuple[int, int],
) -> None:
    height, width = shape
    sentinel = np.array([17, 91, 203], dtype=np.uint8)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[0, 0] = sentinel
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[0, 0] = 255

    padded_image, padded_mask, content = _padded_model_tile(image, mask)

    copied = np.all(padded_image == sentinel, axis=2)
    outside = np.ones((512, 512), dtype=bool)
    outside[content] = False
    assert np.array_equal(padded_image[content], image)
    assert np.array_equal(padded_mask[content], mask)
    assert not np.any(copied & outside & (padded_mask == 0))


def test_overview_mask_resize_preserves_thin_and_isolated_support() -> None:
    mask = np.zeros((2048, 4096), dtype=np.uint8)
    mask[17, 19] = 255
    mask[100:2000, 2047] = 255
    mask[2047, 4095] = 255

    overview = _coverage_preserving_overview_mask(mask, (512, 256))

    assert overview.shape == (256, 512)
    assert set(np.unique(overview)) <= {0, 255}
    rows, columns = np.nonzero(mask)
    target_rows = np.minimum(255, rows.astype(np.int64) * 256 // mask.shape[0])
    target_columns = np.minimum(511, columns.astype(np.int64) * 512 // mask.shape[1])
    assert np.all(overview[target_rows, target_columns] == 255)


def test_tall_mask_uses_overlapping_tiles_and_preserves_outside(tmp_path: Path) -> None:
    session = FakeSession(color_bgr=(210, 120, 30))
    provider, _ = _configured_provider(
        tmp_path,
        session,
        context_padding=32,
        inference_padding=0,
        feather=0,
    )
    source = np.zeros((1200, 180, 3), dtype=np.uint8)
    source[...] = (11, 22, 33)
    mask = np.zeros((1200, 180), dtype=np.uint8)
    mask[50:1150, 70:110] = 255

    result = np.asarray(provider.inpaint(source, mask))

    assert len(session.feeds) >= 3
    assert all(feed["image"].shape == (1, 3, 512, 512) for feed in session.feeds)
    assert all(feed["mask"].shape == (1, 1, 512, 512) for feed in session.feeds)
    assert np.array_equal(result[mask == 0], source[mask == 0])
    assert np.all(result[mask > 0] == np.array([30, 120, 210], dtype=np.uint8))


def test_overlapping_tile_predictions_are_blended_without_hard_seams(
    tmp_path: Path,
) -> None:
    class PerTileSession(FakeSession):
        def run(self, output_names, input_feed):
            self.feeds.append({name: value.copy() for name, value in input_feed.items()})
            value = 0.0 if len(self.feeds) == 1 else 255.0
            return [np.full((1, 3, 512, 512), value, dtype=np.float32)]

    session = PerTileSession()
    provider, _ = _configured_provider(
        tmp_path,
        session,
        context_padding=0,
        inference_padding=0,
        feather=0,
    )
    source = np.zeros((64, 896, 3), dtype=np.uint8)
    source[...] = (1, 2, 3)
    mask = np.full((64, 896), 255, dtype=np.uint8)

    result = np.asarray(provider.inpaint(source, mask))

    assert len(session.feeds) == 2
    scanline = result[32, :, 0].astype(np.int16)
    assert scanline[383] == 0
    assert scanline[512] == 255
    assert 0 < scanline[448] < 255
    assert int(np.max(np.abs(np.diff(scanline)))) <= 4


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


@pytest.mark.parametrize("render_scale", (2, 3, 4))
def test_empty_mask_accepts_scaled_render_context_limits_without_loading_session(
    tmp_path: Path,
    render_scale: int,
) -> None:
    provider, created = _configured_provider(tmp_path)
    source = Image.new("RGB", (10, 9), (20, 30, 40))

    result = provider.inpaint(
        source,
        np.zeros((9, 10), dtype=np.uint8),
        context_padding=4096 * render_scale,
        inference_padding=512 * render_scale,
        feather=255 * render_scale,
        render_scale=render_scale,
    )

    assert np.array_equal(np.asarray(result), np.asarray(source))
    assert created == []


def test_lama_rejects_values_above_scaled_render_limits(tmp_path: Path) -> None:
    provider, _ = _configured_provider(tmp_path)
    source = Image.new("RGB", (10, 9), (20, 30, 40))
    mask = np.zeros((9, 10), dtype=np.uint8)

    with pytest.raises(ValueError, match="context_padding"):
        provider.inpaint(source, mask, context_padding=16385, render_scale=4)
    with pytest.raises(ValueError, match="render_scale"):
        provider.inpaint(source, mask, render_scale=0)


@pytest.mark.parametrize(
    ("factory_options", "message"),
    [
        ({"context_padding": -1}, "context_padding"),
        ({"context_padding": True}, "context_padding"),
        ({"inference_padding": -1}, "inference_padding"),
        ({"inference_padding": True}, "inference_padding"),
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


def test_overview_refine_uses_global_and_core_ai_passes_with_one_final_soft_blend(
    tmp_path: Path,
) -> None:
    session = FakeSession(color_bgr=(200, 200, 200))
    provider, _ = _configured_provider(tmp_path, session, feather=0)
    source = np.zeros((700, 900, 3), dtype=np.uint8)
    mask = np.zeros((700, 900), dtype=np.uint8)
    mask[200:700, 300:900] = 255
    mask[200, 300:900] = 128

    result = np.asarray(
        provider.inpaint_overview_refine(
            source,
            mask,
            context_padding=64,
            inference_padding=4,
            feather=0,
        ).convert("RGB")
    )

    assert len(session.feeds) > 1
    assert np.array_equal(result[mask == 0], source[mask == 0])
    assert np.all(result[mask == 255] == 200)
    assert np.all(result[mask == 128] == 100)
    assert all(feed["image"].shape == (1, 3, 512, 512) for feed in session.feeds)
    assert all(feed["mask"].shape == (1, 1, 512, 512) for feed in session.feeds)


def test_overview_pair_keeps_global_snapshot_separate_from_native_refinement(
    tmp_path: Path,
) -> None:
    class PerPassSession(FakeSession):
        def run(self, output_names, input_feed):
            self.feeds.append({name: value.copy() for name, value in input_feed.items()})
            value = 80.0 if len(self.feeds) == 1 else 200.0
            return [np.full((1, 3, 512, 512), value, dtype=np.float32)]

    session = PerPassSession()
    provider, _ = _configured_provider(tmp_path, session, feather=0)
    source = np.zeros((700, 900, 3), dtype=np.uint8)
    mask = np.zeros((700, 900), dtype=np.uint8)
    mask[200:700, 300:900] = 255
    mask[200, 300:900] = 128

    overview, refined = provider.inpaint_overview_candidates(
        source,
        mask,
        context_padding=64,
        inference_padding=4,
        feather=0,
    )
    overview_pixels = np.asarray(overview.convert("RGB"))
    refined_pixels = np.asarray(refined.convert("RGB"))

    assert len(session.feeds) > 1
    assert np.all(overview_pixels[mask == 255] == 80)
    assert np.all(overview_pixels[mask == 128] == 40)
    assert np.all(refined_pixels[mask == 255] == 200)
    assert np.all(refined_pixels[mask == 128] == 100)
    assert np.array_equal(overview_pixels[mask == 0], source[mask == 0])
    assert np.array_equal(refined_pixels[mask == 0], source[mask == 0])


def test_overview_refine_fails_closed_when_native_core_cap_is_exceeded(
    tmp_path: Path,
) -> None:
    provider, _ = _configured_provider(tmp_path, FakeSession(), feather=0)
    source = np.zeros((900, 900, 3), dtype=np.uint8)
    mask = np.zeros((900, 900), dtype=np.uint8)
    mask[100:900, 100:900] = 255

    with pytest.raises(LaMaProviderError, match="too many refine tiles"):
        provider.inpaint_overview_refine(
            source,
            mask,
            context_padding=32,
            inference_padding=4,
            max_refine_tiles=1,
        )


def test_componentwise_lama_redraws_each_cavity_and_preserves_union_outside(
    tmp_path: Path,
) -> None:
    session = FakeSession(color_bgr=(20, 90, 180))
    provider, _ = _configured_provider(tmp_path, session, feather=0)
    source = Image.new("RGB", (96, 72), (240, 230, 220))
    mask = np.zeros((72, 96), dtype=np.uint8)
    mask[12:24, 10:28] = 255
    mask[44:60, 66:86] = 255

    result = provider.inpaint_components(
        source,
        mask,
        context_padding=COMPONENT_CONTEXT_PADDING,
        inference_padding=COMPONENT_INFERENCE_PADDING,
        feather=0,
    )

    pixels = np.asarray(result.convert("RGB"))
    original = np.asarray(source)
    assert len(session.feeds) == 2
    assert np.array_equal(pixels[mask == 0], original[mask == 0])
    assert np.all(pixels[mask > 0] == (180, 90, 20))


def test_componentwise_lama_fails_closed_before_loading_for_too_many_cavities(
    tmp_path: Path,
) -> None:
    provider, created = _configured_provider(tmp_path, feather=0)
    source = Image.new("RGB", (48, 32), "white")
    mask = np.zeros((32, 48), dtype=np.uint8)
    mask[4:8, 4:8] = 255
    mask[20:24, 36:40] = 255

    with pytest.raises(LaMaProviderError, match="too many repair cavities"):
        provider.inpaint_components(source, mask, max_components=1)

    assert created == []


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
