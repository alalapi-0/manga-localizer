from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from manga_localizer.imaging.preprocessing import (
    _CONFIG_OPTION_NAMES,
    PREPROCESS_PROFILES,
    OpenCVPillowPreprocessProvider,
    PreprocessConfig,
    PreprocessedImage,
    PreprocessProviderError,
    PreprocessUnavailable,
    _alpha_channel,
    _pil_image,
)

type ImageInput = Path | Image.Image | np.ndarray
type SessionFactory = Callable[[str, tuple[str, ...] | None], Any]

NATIVE_SCALE = 4
DEFAULT_TILE_SIZE = 256
DEFAULT_TILE_PAD = 10
DEFAULT_PRE_PAD = 10
MODEL_ID = "RealESRGAN_x4plus_anime_6B"
MODEL_LICENSE = "BSD-3-Clause"


class _Session(Protocol):
    def get_inputs(self) -> Sequence[Any]: ...

    def run(self, output_names: Any, input_feed: dict[str, np.ndarray]) -> Sequence[Any]: ...


def _validate_nonnegative_int(name: str, value: int, *, maximum: int) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 0 through {maximum}")


def _runtime_details() -> tuple[bool, str | None]:
    try:
        available = importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        return False, None
    try:
        return True, importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        return True, None


def _preferred_providers(onnxruntime: Any) -> tuple[str, ...] | None:
    try:
        available = list(onnxruntime.get_available_providers())
    except Exception:
        return None
    preferred = tuple(
        name for name in ("CoreMLExecutionProvider", "CPUExecutionProvider") if name in available
    )
    return preferred or None


def _input_name(session: _Session) -> str:
    try:
        inputs = session.get_inputs()
    except Exception as error:
        raise PreprocessUnavailable(f"Real-ESRGAN ONNX inputs are unreadable: {error}") from error
    if not inputs:
        raise PreprocessUnavailable("Real-ESRGAN ONNX session has no inputs")
    name = getattr(inputs[0], "name", None)
    if not isinstance(name, str) or not name.strip():
        raise PreprocessUnavailable("Real-ESRGAN ONNX input name is missing")
    return name


def _to_nchw(rgb: np.ndarray) -> np.ndarray:
    return np.transpose(rgb.astype(np.float32) / 255.0, (2, 0, 1))[None, ...]


def _from_output(output: Any, *, expected_size: tuple[int, int]) -> np.ndarray:
    array = np.asarray(output)
    if array.ndim == 4:
        array = array[0]
    if array.ndim != 3:
        raise PreprocessProviderError("Real-ESRGAN ONNX output must have 3 or 4 dimensions")
    if array.shape[0] in {1, 3} and array.shape[0] < min(array.shape[1], array.shape[2]):
        array = np.transpose(array, (1, 2, 0))
    if array.shape[2] == 1:
        array = np.repeat(array, 3, axis=2)
    if array.shape[2] != 3:
        raise PreprocessProviderError("Real-ESRGAN ONNX output must be RGB")
    width, height = expected_size
    if array.shape[0] != height or array.shape[1] != width:
        raise PreprocessProviderError(
            "Real-ESRGAN ONNX output size "
            f"{array.shape[1]}x{array.shape[0]} does not match {width}x{height}"
        )
    pixels = array.astype(np.float32)
    if float(np.nanmax(pixels)) <= 1.5:
        pixels *= 255.0
    return np.clip(np.rint(pixels), 0, 255).astype(np.uint8)


def _is_effectively_grayscale(image: Image.Image) -> bool:
    if image.mode in {"L", "LA"}:
        return True
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    return bool(np.max(np.max(rgb, axis=2) - np.min(rgb, axis=2)) <= 1)


def _reflect_pad(rgb: np.ndarray, pad_y: int, pad_x: int) -> np.ndarray:
    if pad_y == 0 and pad_x == 0:
        return rgb
    return np.pad(rgb, ((0, pad_y), (0, pad_x), (0, 0)), mode="reflect")


class RealESRGANONNXPreprocessProvider:
    """Optional Real-ESRGAN anime 4x provider backed by local ONNX Runtime."""

    name = "realesrgan-onnx"

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        execution_providers: Sequence[str] | None = None,
        tile_size: int = DEFAULT_TILE_SIZE,
        tile_pad: int = DEFAULT_TILE_PAD,
        pre_pad: int = DEFAULT_PRE_PAD,
        session_factory: SessionFactory | None = None,
        config: PreprocessConfig | None = None,
        **options: Any,
    ) -> None:
        unknown = sorted(set(options) - _CONFIG_OPTION_NAMES)
        if unknown:
            raise TypeError(f"Unknown preprocessing option(s): {', '.join(unknown)}")
        if config is not None and not isinstance(config, PreprocessConfig):
            raise TypeError("config must be a PreprocessConfig")
        _validate_nonnegative_int("tile_size", tile_size, maximum=4096)
        _validate_nonnegative_int("tile_pad", tile_pad, maximum=256)
        _validate_nonnegative_int("pre_pad", pre_pad, maximum=256)
        if model_path is None:
            normalized_model_path = None
        else:
            model_text = str(model_path)
            if not model_text.strip() or "\x00" in model_text:
                raise ValueError("model_path must be a non-empty local path")
            normalized_model_path = Path(model_text).expanduser().resolve(strict=False)
        if isinstance(execution_providers, str):
            raise ValueError("execution_providers must be a sequence of provider names")
        normalized_providers = (
            tuple(execution_providers) if execution_providers is not None else None
        )
        if normalized_providers is not None and any(
            not isinstance(item, str) or not item.strip() for item in normalized_providers
        ):
            raise ValueError("execution_providers must contain non-empty strings")
        if session_factory is not None and not callable(session_factory):
            raise TypeError("session_factory must be callable")

        base = config or PreprocessConfig(profile=options.pop("profile", "visual-quality"))
        self.config = base.with_overrides(**options) if options else base
        self.model_path = normalized_model_path
        self.execution_providers = normalized_providers
        self.tile_size = tile_size
        self.tile_pad = tile_pad
        self.pre_pad = pre_pad
        self._session_factory = session_factory
        self._session: _Session | None = None
        self._session_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._local = OpenCVPillowPreprocessProvider(profile="off")

    def _runtime_status(self) -> tuple[bool, str | None, str]:
        if self._session_factory is not None:
            return True, None, "injected"
        available, version = _runtime_details()
        return available, version, "onnxruntime"

    def health_check(self) -> dict[str, Any]:
        runtime_available, runtime_version, runtime = self._runtime_status()
        configured = self.model_path is not None
        model_exists = bool(self.model_path and self.model_path.is_file())
        loaded = self._session is not None
        if not configured:
            error = "Real-ESRGAN ONNX model path is not configured"
        elif not model_exists and not loaded:
            error = f"Real-ESRGAN ONNX model file was not found: {self.model_path}"
        elif not runtime_available and not loaded:
            error = "onnxruntime is not installed"
        else:
            error = None
        return {
            "available": error is None,
            "provider": self.name,
            "configured": configured,
            "modelPath": str(self.model_path) if self.model_path else None,
            "modelExists": model_exists,
            "model": MODEL_ID,
            "license": MODEL_LICENSE,
            "nativeScale": NATIVE_SCALE,
            "runtime": runtime,
            "runtimeAvailable": runtime_available,
            "runtimeVersion": runtime_version,
            "loaded": loaded,
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
            "modelFormat": "onnx",
            "model": MODEL_ID,
            "license": MODEL_LICENSE,
            "nativeScale": NATIVE_SCALE,
            "scaleStrategy": "native-4x-then-lanczos-for-2-and-3",
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
            "tileSize": self.tile_size,
            "tilePad": self.tile_pad,
            "prePad": self.pre_pad,
            "inputTypes": ["path", "pil", "ndarray"],
            "batch": True,
            "preservesAlpha": True,
            "preservesGrayscale": True,
            "modifiesSource": False,
            "executionProviders": list(self.execution_providers or ()),
            "config": self.config.to_dict(),
            "error": health["error"],
        }

    def _create_session(self) -> _Session:
        if self.model_path is None:
            raise PreprocessUnavailable("Real-ESRGAN ONNX model path is not configured")
        if not self.model_path.is_file():
            raise PreprocessUnavailable(
                f"Real-ESRGAN ONNX model file was not found: {self.model_path}"
            )
        if self._session_factory is not None:
            factory = self._session_factory
        else:
            runtime_available, _ = _runtime_details()
            if not runtime_available:
                raise PreprocessUnavailable("onnxruntime is not installed")
            try:
                onnxruntime = importlib.import_module("onnxruntime")
            except ImportError as error:
                raise PreprocessUnavailable("onnxruntime could not be imported") from error

            def factory(path: str, providers: tuple[str, ...] | None) -> Any:
                options: dict[str, Any] = {}
                selected = providers if providers is not None else _preferred_providers(onnxruntime)
                if selected:
                    options["providers"] = list(selected)
                session_options = onnxruntime.SessionOptions()
                session_options.log_severity_level = 3
                return onnxruntime.InferenceSession(
                    path,
                    sess_options=session_options,
                    **options,
                )

        try:
            session = factory(str(self.model_path), self.execution_providers)
        except PreprocessProviderError:
            raise
        except Exception as error:
            detail = str(error).strip()[:500]
            raise PreprocessUnavailable(
                f"Real-ESRGAN ONNX session could not be created: {detail}"
            ) from error
        _input_name(session)
        return session

    def _get_session(self) -> _Session:
        if self._session is not None:
            return self._session
        with self._session_lock:
            if self._session is None:
                self._session = self._create_session()
            return self._session

    def _infer_rgb(self, rgb: np.ndarray, session: _Session, input_name: str) -> np.ndarray:
        expected = (rgb.shape[1] * NATIVE_SCALE, rgb.shape[0] * NATIVE_SCALE)
        try:
            with self._inference_lock:
                outputs = session.run(None, {input_name: _to_nchw(rgb)})
        except Exception as error:
            detail = str(error).strip()[:500]
            raise PreprocessProviderError(f"Real-ESRGAN ONNX inference failed: {detail}") from error
        if not outputs:
            raise PreprocessProviderError("Real-ESRGAN ONNX inference returned no outputs")
        return _from_output(outputs[0], expected_size=expected)

    def _tile_infer(self, rgb: np.ndarray, session: _Session, input_name: str) -> np.ndarray:
        height, width = rgb.shape[:2]
        output = np.zeros((height * NATIVE_SCALE, width * NATIVE_SCALE, 3), dtype=np.uint8)
        for top in range(0, height, self.tile_size):
            for left in range(0, width, self.tile_size):
                bottom = min(top + self.tile_size, height)
                right = min(left + self.tile_size, width)
                padded_top = max(top - self.tile_pad, 0)
                padded_left = max(left - self.tile_pad, 0)
                padded_bottom = min(bottom + self.tile_pad, height)
                padded_right = min(right + self.tile_pad, width)
                tile = rgb[padded_top:padded_bottom, padded_left:padded_right]
                generated = self._infer_rgb(tile, session, input_name)
                crop_y = (top - padded_top) * NATIVE_SCALE
                crop_x = (left - padded_left) * NATIVE_SCALE
                crop_h = (bottom - top) * NATIVE_SCALE
                crop_w = (right - left) * NATIVE_SCALE
                output[
                    top * NATIVE_SCALE : bottom * NATIVE_SCALE,
                    left * NATIVE_SCALE : right * NATIVE_SCALE,
                ] = generated[crop_y : crop_y + crop_h, crop_x : crop_x + crop_w]
        return output

    def _enhance_rgb(self, rgb: np.ndarray) -> np.ndarray:
        session = self._get_session()
        input_name = _input_name(session)
        working = rgb
        if self.pre_pad:
            working = np.pad(
                working,
                ((self.pre_pad, self.pre_pad), (self.pre_pad, self.pre_pad), (0, 0)),
                mode="reflect",
            )
        modulus = 2
        pad_y = (modulus - working.shape[0] % modulus) % modulus
        pad_x = (modulus - working.shape[1] % modulus) % modulus
        working = _reflect_pad(working, pad_y, pad_x)
        if self.tile_size == 0 or max(working.shape[:2]) <= self.tile_size:
            generated = self._infer_rgb(working, session, input_name)
        else:
            generated = self._tile_infer(working, session, input_name)
        top = self.pre_pad * NATIVE_SCALE
        left = self.pre_pad * NATIVE_SCALE
        bottom = top + rgb.shape[0] * NATIVE_SCALE
        right = left + rgb.shape[1] * NATIVE_SCALE
        return generated[top:bottom, left:right]

    def preprocess(self, image: ImageInput, **options: Any) -> PreprocessedImage:
        source = _pil_image(image)
        config = self.config.with_overrides(**options) if options else self.config
        if config.enable_upscale:
            alpha = _alpha_channel(source)
            enhanced_rgb = self._enhance_rgb(np.asarray(source.convert("RGB"), dtype=np.uint8))
            enhanced = Image.fromarray(enhanced_rgb, mode="RGB")
            if _is_effectively_grayscale(source):
                enhanced = enhanced.convert("L").convert("RGB")
            target = (source.width * config.upscale_factor, source.height * config.upscale_factor)
            if enhanced.size != target:
                enhanced = enhanced.resize(target, Image.Resampling.LANCZOS)
            if alpha is not None:
                enhanced.putalpha(alpha.resize(enhanced.size, Image.Resampling.LANCZOS))
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
