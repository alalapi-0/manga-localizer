from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging import (
    ImageEnhancementProvider,
    OpenCVPillowPreprocessProvider,
    PreprocessConfig,
    PreprocessProvider,
    PreprocessProviderError,
    PreprocessUnavailable,
    RealESRGANNCNNPreprocessProvider,
)


def _no_effects(**options) -> dict[str, object]:
    return {
        "profile": "off",
        "enable_denoise": False,
        "enable_sharpen": False,
        "enable_contrast_enhance": False,
        "enable_edge_optimize": False,
        "enable_binarize": False,
        **options,
    }


def test_profiles_configuration_protocol_and_capabilities_are_explicit() -> None:
    off = PreprocessConfig(profile="off")
    assert off.enable_upscale is False
    assert off.enable_denoise is False
    ocr = PreprocessConfig(profile="ocr-friendly")
    assert ocr.enable_upscale is True
    assert ocr.enable_edge_optimize is False
    balanced = PreprocessConfig(profile="balanced")
    assert balanced.enable_denoise is True
    assert balanced.enable_upscale is False
    visual = PreprocessConfig(profile="visual-quality", enable_upscale=False)
    assert visual.enable_upscale is False

    provider = OpenCVPillowPreprocessProvider(profile="off", enable_sharpen=True)
    assert isinstance(provider, ImageEnhancementProvider)
    assert isinstance(provider, PreprocessProvider)
    assert provider.config.enable_sharpen is True
    assert provider.health_check()["available"] is True
    capabilities = provider.get_capabilities()
    assert capabilities["profiles"] == [
        "off",
        "ocr-friendly",
        "balanced",
        "visual-quality",
    ]
    assert capabilities["operations"] == {
        "upscale": True,
        "denoise": True,
        "sharpen": True,
        "contrastEnhance": True,
        "edgeOptimize": True,
        "binarize": True,
    }
    assert capabilities["preservesAlpha"] is True
    assert capabilities["modifiesSource"] is False


def test_upscale_size_scale_mapping_batch_and_all_input_types(tmp_path: Path) -> None:
    pixels = np.zeros((7, 11, 3), dtype=np.uint8)
    pixels[..., 0] = np.arange(11, dtype=np.uint8)
    original_pixels = pixels.copy()
    pil = Image.fromarray(pixels, mode="RGB")
    path = tmp_path / "source.png"
    pil.save(path)
    original_file = path.read_bytes()
    provider = OpenCVPillowPreprocessProvider()

    array_result = provider.preprocess(
        pixels,
        **_no_effects(enable_upscale=True, upscale_factor=3),
    )
    assert array_result.original_size == (11, 7)
    assert array_result.processed_size == (33, 21)
    assert array_result.scale == (3.0, 3.0)
    assert array_result.original_to_processed_scale == (3.0, 3.0)
    assert array_result.map_point(2, 4) == (6.0, 12.0)
    assert array_result.map_box(1, 2, 3, 4) == (3.0, 6.0, 9.0, 12.0)
    assert array_result.map_region({"id": "r", "x": 1, "y": 2, "width": 3, "height": 4}) == {
        "id": "r",
        "x": 3.0,
        "y": 6.0,
        "width": 9.0,
        "height": 12.0,
    }
    assert np.array_equal(pixels, original_pixels)

    pil_before = np.asarray(pil).copy()
    pil_result, path_result = provider.preprocess_batch(
        [pil, path],
        **_no_effects(enable_upscale=True, upscale_factor=2),
    )
    assert pil_result.processed_size == (22, 14)
    assert path_result.processed_size == (22, 14)
    assert np.array_equal(np.asarray(pil), pil_before)
    assert path.read_bytes() == original_file


def test_switches_execute_independently_and_binarize_uses_threshold() -> None:
    rng = np.random.default_rng(7)
    pixels = np.tile(np.arange(32, dtype=np.uint8), (24, 1)) * 8
    rgb = np.repeat(pixels[..., np.newaxis], 3, axis=2)
    noise = rng.integers(-35, 36, size=rgb.shape, dtype=np.int16)
    source = Image.fromarray(np.clip(rgb.astype(np.int16) + noise, 0, 255).astype(np.uint8))
    provider = OpenCVPillowPreprocessProvider(profile="off")
    baseline = np.asarray(provider.preprocess(source).image)

    for option in (
        "enable_denoise",
        "enable_sharpen",
        "enable_contrast_enhance",
        "enable_edge_optimize",
    ):
        changed = np.asarray(provider.preprocess(source, **{option: True}).image)
        assert not np.array_equal(changed, baseline), option

    binary = np.asarray(
        provider.preprocess(source, enable_binarize=True, threshold=120).image.convert("RGB")
    )
    assert set(np.unique(binary)).issubset({0, 255})
    assert {0, 255}.issubset(set(np.unique(binary)))


def test_alpha_is_preserved_and_source_is_not_mutated() -> None:
    pixels = np.zeros((9, 13, 4), dtype=np.uint8)
    pixels[..., :3] = (80, 120, 160)
    pixels[..., 3] = np.arange(13, dtype=np.uint8) * 19
    source = Image.fromarray(pixels, mode="RGBA")
    original = np.asarray(source).copy()

    result = OpenCVPillowPreprocessProvider().preprocess(
        source,
        **_no_effects(
            enable_upscale=True,
            upscale_factor=2,
            enable_contrast_enhance=True,
        ),
    )
    expected_alpha = source.getchannel("A").resize(result.image.size, Image.Resampling.LANCZOS)
    assert result.image.mode == "RGBA"
    assert np.array_equal(np.asarray(result.image.getchannel("A")), np.asarray(expected_alpha))
    assert np.array_equal(np.asarray(source), original)

    unchanged = OpenCVPillowPreprocessProvider(profile="off").preprocess(source)
    assert unchanged.image.mode == source.mode
    assert np.array_equal(np.asarray(unchanged.image), original)
    assert unchanged.image is not source


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"profile": "unknown"}, "profile"),
        ({"enable_upscale": 1}, "enable_upscale"),
        ({"upscale_factor": 0}, "upscale_factor"),
        ({"upscale_factor": 2.0}, "upscale_factor"),
        ({"threshold": -1}, "threshold"),
        ({"threshold": 256}, "threshold"),
    ],
)
def test_invalid_configuration_is_rejected(options: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OpenCVPillowPreprocessProvider(**options)


def test_invalid_runtime_options_and_arrays_are_rejected() -> None:
    provider = OpenCVPillowPreprocessProvider(profile="off")
    with pytest.raises(TypeError, match="Unknown"):
        provider.preprocess(Image.new("RGB", (2, 2)), undocumented=True)
    with pytest.raises(ValueError, match="uint8"):
        provider.preprocess(np.zeros((2, 2, 3), dtype=np.float32))
    with pytest.raises(ValueError, match="shape"):
        provider.preprocess(np.zeros((2, 2, 2), dtype=np.uint8))
    with pytest.raises(TypeError, match=r"pathlib\.Path"):
        provider.preprocess("not-a-path")  # type: ignore[arg-type]


def test_realesrgan_missing_command_is_unavailable_without_startup_side_effects(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-installed-realesrgan"
    provider = RealESRGANNCNNPreprocessProvider(command=missing)
    health = provider.health_check()
    assert health["available"] is False
    assert health["executable"] is None
    assert provider.get_capabilities()["downloadsModelsAtStartup"] is False
    with pytest.raises(PreprocessUnavailable, match="not found"):
        provider.preprocess(Image.new("RGB", (4, 3)), **_no_effects(enable_upscale=True))

    local_only = provider.preprocess(Image.new("RGB", (4, 3)), profile="off")
    assert local_only.processed_size == (4, 3)


def test_realesrgan_fake_cli_success_preserves_alpha_and_chains_postprocessing(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-realesrgan"
    executable.write_text(
        f"""#!{sys.executable}
import sys
from pathlib import Path
from PIL import Image

args = sys.argv[1:]
input_path = Path(args[args.index('-i') + 1])
output_path = Path(args[args.index('-o') + 1])
scale = int(args[args.index('-s') + 1])
with Image.open(input_path) as source:
    source.convert('RGB').resize(
        (source.width * scale, source.height * scale), Image.Resampling.NEAREST
    ).save(output_path, format='PNG')
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    alpha = np.arange(6 * 8, dtype=np.uint8).reshape(6, 8)
    pixels = np.zeros((6, 8, 4), dtype=np.uint8)
    pixels[..., :3] = (40, 90, 150)
    pixels[..., 3] = alpha
    source = Image.fromarray(pixels, mode="RGBA")
    source_before = np.asarray(source).copy()
    provider = RealESRGANNCNNPreprocessProvider(
        command=executable,
        profile="off",
        model_name="realesrgan-x4plus-anime",
    )

    result = provider.preprocess(
        source,
        enable_upscale=True,
        upscale_factor=2,
        enable_binarize=True,
        threshold=100,
    )
    assert provider.health_check()["available"] is True
    assert result.processed_size == (16, 12)
    assert result.scale == (2.0, 2.0)
    assert result.image.mode == "RGBA"
    expected_alpha = source.getchannel("A").resize(result.image.size, Image.Resampling.LANCZOS)
    assert np.array_equal(np.asarray(result.image.getchannel("A")), np.asarray(expected_alpha))
    assert set(np.unique(np.asarray(result.image.convert("RGB")))).issubset({0, 255})
    assert np.array_equal(np.asarray(source), source_before)


def test_realesrgan_nonzero_exit_and_missing_output_are_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "realesrgan"
    executable.touch(mode=0o755)
    provider = RealESRGANNCNNPreprocessProvider(command=executable, profile="off")

    monkeypatch.setattr(
        "manga_localizer.imaging.preprocessing.subprocess.run",
        lambda command, **options: subprocess.CompletedProcess(command, 8, "", "bad model"),
    )
    with pytest.raises(PreprocessProviderError, match=r"failed \(8\): bad model"):
        provider.preprocess(Image.new("RGB", (3, 3)), enable_upscale=True)

    monkeypatch.setattr(
        "manga_localizer.imaging.preprocessing.subprocess.run",
        lambda command, **options: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(PreprocessProviderError, match="without creating"):
        provider.preprocess(Image.new("RGB", (3, 3)), enable_upscale=True)


def test_realesrgan_ncnn_discovers_search_paths_and_passes_models_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "realesrgan-ncnn-vulkan"
    models = install_root / "models"
    models.mkdir(parents=True)
    executable = install_root / "realesrgan-ncnn-vulkan"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    captured: dict[str, list[str]] = {}

    def fake_run(command, **options):
        captured["command"] = [str(item) for item in command]
        output = command[command.index("-o") + 1]
        Image.new("RGB", (6, 4), "white").save(output, format="PNG")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("manga_localizer.imaging.preprocessing.subprocess.run", fake_run)
    provider = RealESRGANNCNNPreprocessProvider(
        command="realesrgan-ncnn-vulkan",
        search_paths=(install_root,),
        profile="off",
    )
    health = provider.health_check()
    assert health["available"] is True
    assert health["executable"] == str(executable.resolve())
    assert health["modelsDirectory"] == str(models.resolve())
    result = provider.preprocess(Image.new("RGB", (3, 2)), enable_upscale=True, upscale_factor=2)
    assert result.processed_size == (6, 4)
    assert "-m" in captured["command"]
    assert captured["command"][captured["command"].index("-m") + 1] == str(models.resolve())
