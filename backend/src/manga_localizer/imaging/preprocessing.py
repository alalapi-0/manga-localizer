from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

type PreprocessProfile = Literal[
    "off",
    "ocr-friendly",
    "balanced",
    "visual-quality",
]
type ImageInput = Path | Image.Image | np.ndarray

PREPROCESS_PROFILES: tuple[PreprocessProfile, ...] = (
    "off",
    "ocr-friendly",
    "balanced",
    "visual-quality",
)

_PROFILE_DEFAULTS: dict[PreprocessProfile, dict[str, bool]] = {
    "off": {
        "enable_upscale": False,
        "enable_denoise": False,
        "enable_sharpen": False,
        "enable_contrast_enhance": False,
        "enable_edge_optimize": False,
        "enable_binarize": False,
    },
    "ocr-friendly": {
        "enable_upscale": True,
        "enable_denoise": True,
        "enable_sharpen": True,
        "enable_contrast_enhance": True,
        # Real-data evaluation showed EDGE_ENHANCE more than doubled PP-OCR
        # candidates on line art, dominated by false positives. Keep it opt-in.
        "enable_edge_optimize": False,
        "enable_binarize": False,
    },
    "balanced": {
        "enable_upscale": False,
        "enable_denoise": True,
        "enable_sharpen": True,
        "enable_contrast_enhance": True,
        "enable_edge_optimize": False,
        "enable_binarize": False,
    },
    "visual-quality": {
        "enable_upscale": True,
        "enable_denoise": True,
        "enable_sharpen": True,
        "enable_contrast_enhance": True,
        "enable_edge_optimize": False,
        "enable_binarize": False,
    },
}

_CONFIG_OPTION_NAMES = frozenset(
    {
        "profile",
        "enable_upscale",
        "upscale_factor",
        "enable_denoise",
        "enable_sharpen",
        "enable_contrast_enhance",
        "enable_edge_optimize",
        "enable_binarize",
        "threshold",
    }
)


class PreprocessProviderError(RuntimeError):
    """Base error raised by an image preprocessing provider."""


class PreprocessUnavailable(PreprocessProviderError):
    """Raised when an explicitly configured optional provider cannot run."""


def _validate_bool(name: str, value: bool) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _validate_upscale_factor(value: int) -> None:
    if type(value) is not int or value not in {2, 3, 4}:
        raise ValueError("upscale_factor must be one of 2, 3, or 4")


def _validate_threshold(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("threshold must be an integer from 0 through 255")


@dataclass(frozen=True, slots=True, init=False)
class PreprocessConfig:
    """Resolved and validated preprocessing configuration."""

    profile: PreprocessProfile
    enable_upscale: bool
    upscale_factor: int
    enable_denoise: bool
    enable_sharpen: bool
    enable_contrast_enhance: bool
    enable_edge_optimize: bool
    enable_binarize: bool
    threshold: int

    def __init__(
        self,
        profile: PreprocessProfile = "balanced",
        *,
        enable_upscale: bool | None = None,
        upscale_factor: int = 2,
        enable_denoise: bool | None = None,
        enable_sharpen: bool | None = None,
        enable_contrast_enhance: bool | None = None,
        enable_edge_optimize: bool | None = None,
        enable_binarize: bool | None = None,
        threshold: int = 180,
    ) -> None:
        if profile not in PREPROCESS_PROFILES:
            allowed = ", ".join(PREPROCESS_PROFILES)
            raise ValueError(f"profile must be one of: {allowed}")
        _validate_upscale_factor(upscale_factor)
        _validate_threshold(threshold)
        defaults = _PROFILE_DEFAULTS[profile]
        switches = {
            "enable_upscale": enable_upscale,
            "enable_denoise": enable_denoise,
            "enable_sharpen": enable_sharpen,
            "enable_contrast_enhance": enable_contrast_enhance,
            "enable_edge_optimize": enable_edge_optimize,
            "enable_binarize": enable_binarize,
        }
        resolved: dict[str, bool] = {}
        for name, value in switches.items():
            if value is not None:
                _validate_bool(name, value)
            resolved[name] = defaults[name] if value is None else value

        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "upscale_factor", upscale_factor)
        object.__setattr__(self, "threshold", threshold)
        for name, value in resolved.items():
            object.__setattr__(self, name, value)

    def with_overrides(self, **options: Any) -> PreprocessConfig:
        unknown = sorted(set(options) - _CONFIG_OPTION_NAMES)
        if unknown:
            raise TypeError(f"Unknown preprocessing option(s): {', '.join(unknown)}")

        if "profile" in options:
            profile = options.pop("profile")
            base = PreprocessConfig(profile=profile)
        else:
            base = self
        values = base.to_dict()
        values.update(options)
        return PreprocessConfig(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "enable_upscale": self.enable_upscale,
            "upscale_factor": self.upscale_factor,
            "enable_denoise": self.enable_denoise,
            "enable_sharpen": self.enable_sharpen,
            "enable_contrast_enhance": self.enable_contrast_enhance,
            "enable_edge_optimize": self.enable_edge_optimize,
            "enable_binarize": self.enable_binarize,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class PreprocessedImage:
    """A processed PIL image plus its original-to-processed coordinate mapping."""

    image: Image.Image
    original_size: tuple[int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.image, Image.Image):
            raise TypeError("image must be a PIL image")
        width, height = self.original_size
        if width <= 0 or height <= 0:
            raise ValueError("original_size dimensions must be positive")
        if self.image.width <= 0 or self.image.height <= 0:
            raise ValueError("processed image dimensions must be positive")

    @property
    def processed_size(self) -> tuple[int, int]:
        return self.image.size

    @property
    def scale_x(self) -> float:
        return self.image.width / self.original_size[0]

    @property
    def scale_y(self) -> float:
        return self.image.height / self.original_size[1]

    @property
    def scale(self) -> tuple[float, float]:
        return self.scale_x, self.scale_y

    @property
    def original_to_processed_scale(self) -> tuple[float, float]:
        return self.scale

    def map_point(self, x: float, y: float) -> tuple[float, float]:
        return float(x) * self.scale_x, float(y) * self.scale_y

    def unmap_point(self, x: float, y: float) -> tuple[float, float]:
        return float(x) / self.scale_x, float(y) / self.scale_y

    def map_box(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> tuple[float, float, float, float]:
        mapped_x, mapped_y = self.map_point(x, y)
        return (
            mapped_x,
            mapped_y,
            float(width) * self.scale_x,
            float(height) * self.scale_y,
        )

    def map_region(self, region: Mapping[str, Any]) -> dict[str, Any]:
        required = {"x", "y", "width", "height"}
        missing = sorted(required - region.keys())
        if missing:
            raise ValueError(f"Region is missing coordinate(s): {', '.join(missing)}")
        mapped = dict(region)
        mapped["x"], mapped["y"], mapped["width"], mapped["height"] = self.map_box(
            float(region["x"]),
            float(region["y"]),
            float(region["width"]),
            float(region["height"]),
        )
        return mapped

    def unmap_box(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> tuple[float, float, float, float]:
        unmapped_x, unmapped_y = self.unmap_point(x, y)
        return (
            unmapped_x,
            unmapped_y,
            float(width) / self.scale_x,
            float(height) / self.scale_y,
        )


@runtime_checkable
class ImageEnhancementProvider(Protocol):
    name: str

    def preprocess(self, image: ImageInput, **options: Any) -> PreprocessedImage: ...

    def preprocess_batch(
        self,
        images: Sequence[ImageInput],
        **options: Any,
    ) -> list[PreprocessedImage]: ...

    def enhance_image(self, image: ImageInput, **options: Any) -> PreprocessedImage: ...

    def health_check(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


@runtime_checkable
class PreprocessProvider(ImageEnhancementProvider, Protocol):
    """Compatibility name for the unified image enhancement provider protocol."""


def _pil_image(image: ImageInput) -> Image.Image:
    if isinstance(image, Path):
        path = image.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("Image path must point to a file")
        with Image.open(path) as opened:
            opened.load()
            return opened.copy()
    if isinstance(image, Image.Image):
        if image.width <= 0 or image.height <= 0:
            raise ValueError("Image dimensions must be positive")
        return image.copy()
    if not isinstance(image, np.ndarray):
        raise TypeError("image must be a pathlib.Path, PIL image, or numpy ndarray")
    if image.dtype != np.uint8:
        raise ValueError("Image arrays must use uint8 pixels")
    if image.ndim == 2:
        if 0 in image.shape:
            raise ValueError("Image array dimensions must be positive")
        return Image.fromarray(np.array(image, copy=True), mode="L")
    if image.ndim != 3 or image.shape[2] not in {1, 3, 4} or 0 in image.shape[:2]:
        raise ValueError("Image arrays must have shape HxW, HxWx1, HxWx3, or HxWx4")
    pixels = np.array(image, copy=True)
    if pixels.shape[2] == 1:
        return Image.fromarray(pixels[..., 0], mode="L")
    return Image.fromarray(pixels, mode="RGB" if pixels.shape[2] == 3 else "RGBA")


def _alpha_channel(image: Image.Image) -> Image.Image | None:
    if "A" in image.getbands() or "transparency" in image.info:
        return image.convert("RGBA").getchannel("A")
    return None


def _apply_local_operations(source: Image.Image, config: PreprocessConfig) -> Image.Image:
    if not any(
        (
            config.enable_upscale,
            config.enable_denoise,
            config.enable_sharpen,
            config.enable_contrast_enhance,
            config.enable_edge_optimize,
            config.enable_binarize,
        )
    ):
        return source.copy()

    alpha = _alpha_channel(source)
    working = source.convert("RGB")
    if config.enable_upscale:
        target = (
            working.width * config.upscale_factor,
            working.height * config.upscale_factor,
        )
        working = working.resize(target, Image.Resampling.LANCZOS)
        if alpha is not None:
            alpha = alpha.resize(target, Image.Resampling.LANCZOS)

    if config.enable_denoise and min(working.size) >= 3:
        pixels = np.asarray(working, dtype=np.uint8)
        denoised = cv2.medianBlur(pixels, 3)
        working = Image.fromarray(denoised, mode="RGB")
    if config.enable_contrast_enhance:
        working = ImageEnhance.Contrast(working).enhance(1.2)
    if config.enable_edge_optimize:
        working = working.filter(ImageFilter.EDGE_ENHANCE)
    if config.enable_sharpen:
        working = working.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
    if config.enable_binarize:
        grayscale = np.asarray(working.convert("L"), dtype=np.uint8)
        _, binary = cv2.threshold(grayscale, config.threshold, 255, cv2.THRESH_BINARY)
        working = Image.fromarray(binary, mode="L").convert("RGB")

    if alpha is not None:
        working.putalpha(alpha)
    return working


class OpenCVPillowPreprocessProvider:
    """Dependency-light local preprocessing using the project's OpenCV and Pillow stack."""

    name = "opencv-pillow"

    def __init__(
        self,
        config: PreprocessConfig | None = None,
        **options: Any,
    ) -> None:
        unknown = sorted(set(options) - _CONFIG_OPTION_NAMES)
        if unknown:
            raise TypeError(f"Unknown preprocessing option(s): {', '.join(unknown)}")
        if config is not None and not isinstance(config, PreprocessConfig):
            raise TypeError("config must be a PreprocessConfig")
        base = config or PreprocessConfig(profile=options.pop("profile", "balanced"))
        self.config = base.with_overrides(**options) if options else base

    def preprocess(self, image: ImageInput, **options: Any) -> PreprocessedImage:
        source = _pil_image(image)
        config = self.config.with_overrides(**options) if options else self.config
        processed = _apply_local_operations(source, config)
        return PreprocessedImage(image=processed, original_size=source.size)

    def preprocess_batch(
        self,
        images: Sequence[ImageInput],
        **options: Any,
    ) -> list[PreprocessedImage]:
        return [self.preprocess(image, **options) for image in images]

    def enhance_image(self, image: ImageInput, **options: Any) -> PreprocessedImage:
        return self.preprocess(image, **options)

    def health_check(self) -> dict[str, Any]:
        return {
            "available": True,
            "provider": self.name,
            "opencvVersion": cv2.__version__,
            "pillowAvailable": True,
            "error": None,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "local": True,
            "aiUpscale": False,
            "classicInterpolation": True,
            "profiles": list(PREPROCESS_PROFILES),
            "operations": {
                "upscale": True,
                "denoise": True,
                "sharpen": True,
                "contrastEnhance": True,
                "edgeOptimize": True,
                "binarize": True,
            },
            "upscaleFactors": [2, 3, 4],
            "inputTypes": ["path", "pil", "ndarray"],
            "batch": True,
            "preservesAlpha": True,
            "modifiesSource": False,
            "config": self.config.to_dict(),
            "error": None,
        }


class RealESRGANNCNNPreprocessProvider:
    """Optional Real-ESRGAN NCNN CLI adapter with local postprocessing."""

    name = "realesrgan-ncnn"

    def __init__(
        self,
        command: str | Path = "realesrgan-ncnn-vulkan",
        *,
        timeout: float = 300.0,
        model_name: str | None = None,
        tile_size: int | None = None,
        gpu_id: int | None = None,
        models_dir: str | Path | None = None,
        search_paths: Sequence[str | Path] = (),
        config: PreprocessConfig | None = None,
        **options: Any,
    ) -> None:
        command_text = str(command)
        if not command_text.strip() or "\x00" in command_text:
            raise ValueError("command must be a non-empty executable path or name")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise ValueError("timeout must be a positive finite number")
        if timeout <= 0 or not math.isfinite(float(timeout)):
            raise ValueError("timeout must be a positive finite number")
        if model_name is not None and (not model_name.strip() or "\x00" in model_name):
            raise ValueError("model_name must be non-empty when provided")
        if tile_size is not None and (type(tile_size) is not int or tile_size < 0):
            raise ValueError("tile_size must be a non-negative integer")
        if gpu_id is not None and (type(gpu_id) is not int or gpu_id < -1):
            raise ValueError("gpu_id must be an integer greater than or equal to -1")
        if models_dir is not None:
            models_text = str(models_dir)
            if not models_text.strip() or "\x00" in models_text:
                raise ValueError("models_dir must be a non-empty local path")
        if isinstance(search_paths, str | bytes):
            raise TypeError("search_paths must be a sequence of paths")
        normalized_search_paths: tuple[Path, ...] = ()
        for item in search_paths:
            text = str(item)
            if not text.strip() or "\x00" in text:
                raise ValueError("search_paths must contain non-empty local paths")
            normalized_search_paths += (Path(text).expanduser(),)
        unknown = sorted(set(options) - _CONFIG_OPTION_NAMES)
        if unknown:
            raise TypeError(f"Unknown preprocessing option(s): {', '.join(unknown)}")
        if config is not None and not isinstance(config, PreprocessConfig):
            raise TypeError("config must be a PreprocessConfig")

        base = config or PreprocessConfig(profile=options.pop("profile", "visual-quality"))
        self.config = base.with_overrides(**options) if options else base
        self.command = command_text
        self.timeout = float(timeout)
        self.model_name = model_name
        self.tile_size = tile_size
        self.gpu_id = gpu_id
        self.models_dir = None if models_dir is None else Path(str(models_dir)).expanduser()
        self.search_paths = normalized_search_paths
        self._local = OpenCVPillowPreprocessProvider(profile="off")

    def _executable(self) -> Path | None:
        command_path = Path(self.command).expanduser()
        candidates: list[Path] = []
        if command_path.is_file():
            candidates.append(command_path)
        located = shutil.which(self.command)
        if located:
            candidates.append(Path(located))
        command_name = command_path.name
        for directory in self.search_paths:
            candidates.append(Path(directory) / command_name)
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve(strict=False)
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file() and os.access(resolved, os.X_OK):
                return resolved
        return None

    def _models_directory(self, executable: Path | None = None) -> Path | None:
        if self.models_dir is not None and self.models_dir.is_dir():
            return self.models_dir
        if executable is None:
            executable = self._executable()
        if executable is None:
            return None
        sibling = executable.parent / "models"
        return sibling if sibling.is_dir() else None

    def health_check(self) -> dict[str, Any]:
        executable = self._executable()
        models_directory = self._models_directory(executable)
        if executable is None:
            error = f"Executable was not found: {self.command}"
        else:
            error = None
        return {
            "available": executable is not None,
            "provider": self.name,
            "command": self.command,
            "executable": str(executable) if executable else None,
            "configured": True,
            "modelName": self.model_name,
            "modelsDirectory": str(models_directory) if models_directory else None,
            "error": error,
        }

    def get_capabilities(self) -> dict[str, Any]:
        health = self.health_check()
        return {
            "provider": self.name,
            "available": health["available"],
            "local": True,
            "aiUpscale": True,
            "classicInterpolation": False,
            "downloadsModelsAtStartup": False,
            "profiles": list(PREPROCESS_PROFILES),
            "operations": {
                "upscale": health["available"],
                "denoise": True,
                "sharpen": True,
                "contrastEnhance": True,
                "edgeOptimize": True,
                "binarize": True,
            },
            "upscaleFactors": [2, 3, 4],
            "inputTypes": ["path", "pil", "ndarray"],
            "batch": True,
            "preservesAlpha": True,
            "modifiesSource": False,
            "config": self.config.to_dict(),
            "error": health["error"],
        }

    def _run_cli(
        self,
        source: Image.Image,
        alpha: Image.Image | None,
        factor: int,
    ) -> Image.Image:
        executable = self._executable()
        if executable is None:
            raise PreprocessUnavailable(f"Real-ESRGAN executable was not found: {self.command}")

        with tempfile.TemporaryDirectory(prefix="manga-localizer-realesrgan-") as directory:
            temporary = Path(directory)
            input_path = temporary / "input.png"
            output_path = temporary / "output.png"
            source.convert("RGB").save(input_path, format="PNG")
            command = [
                str(executable),
                "-i",
                str(input_path),
                "-o",
                str(output_path),
                "-s",
                str(factor),
                "-f",
                "png",
            ]
            models_directory = self._models_directory(executable)
            if models_directory is not None:
                command.extend(["-m", str(models_directory)])
            if self.model_name is not None:
                command.extend(["-n", self.model_name])
            if self.tile_size is not None:
                command.extend(["-t", str(self.tile_size)])
            if self.gpu_id is not None:
                command.extend(["-g", str(self.gpu_id)])
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise PreprocessUnavailable(f"Real-ESRGAN could not run: {error}") from error
            if completed.returncode != 0:
                detail = completed.stderr.strip()[:500]
                raise PreprocessProviderError(
                    f"Real-ESRGAN failed ({completed.returncode}): {detail}"
                )
            if not output_path.is_file():
                raise PreprocessProviderError(
                    "Real-ESRGAN completed without creating an output image"
                )
            try:
                with Image.open(output_path) as opened:
                    opened.load()
                    result = opened.convert("RGB")
            except (OSError, ValueError) as error:
                raise PreprocessProviderError(
                    "Real-ESRGAN produced an unreadable output image"
                ) from error
            if alpha is not None:
                result.putalpha(alpha.resize(result.size, Image.Resampling.LANCZOS))
            return result

    def preprocess(self, image: ImageInput, **options: Any) -> PreprocessedImage:
        source = _pil_image(image)
        config = self.config.with_overrides(**options) if options else self.config
        if config.enable_upscale:
            enhanced = self._run_cli(source, _alpha_channel(source), config.upscale_factor)
        else:
            enhanced = source.copy()

        postprocessed = self._local.preprocess(
            enhanced,
            profile="off",
            enable_upscale=False,
            upscale_factor=config.upscale_factor,
            enable_denoise=config.enable_denoise,
            enable_sharpen=config.enable_sharpen,
            enable_contrast_enhance=config.enable_contrast_enhance,
            enable_edge_optimize=config.enable_edge_optimize,
            enable_binarize=config.enable_binarize,
            threshold=config.threshold,
        )
        return PreprocessedImage(image=postprocessed.image, original_size=source.size)

    def preprocess_batch(
        self,
        images: Sequence[ImageInput],
        **options: Any,
    ) -> list[PreprocessedImage]:
        return [self.preprocess(image, **options) for image in images]

    def enhance_image(self, image: ImageInput, **options: Any) -> PreprocessedImage:
        return self.preprocess(image, **options)


OpenCVPreprocessProvider = OpenCVPillowPreprocessProvider
LocalPreprocessProvider = OpenCVPillowPreprocessProvider
RealESRGANNCNNProvider = RealESRGANNCNNPreprocessProvider


def preprocess_image(image: ImageInput, **options: Any) -> PreprocessedImage:
    """Preprocess one image with the dependency-light default provider."""

    return OpenCVPillowPreprocessProvider().preprocess(image, **options)
