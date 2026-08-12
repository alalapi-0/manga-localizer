from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from PIL import Image

from manga_localizer.imaging.inpainting import create_mask

type ImageInput = Path | Image.Image | np.ndarray
type SessionFactory = Callable[[str, tuple[str, ...] | None], Any]

MODEL_SIZE = 512
IMAGE_INPUT_NAME = "image"
MASK_INPUT_NAME = "mask"


class LaMaProviderError(RuntimeError):
    """Base error raised by the optional LaMa ONNX provider."""


class LaMaUnavailable(LaMaProviderError):
    """Raised when LaMa lacks a configured model or ONNX Runtime."""


class _Session(Protocol):
    def get_inputs(self) -> Sequence[Any]: ...

    def run(self, output_names: Any, input_feed: Mapping[str, np.ndarray]) -> Sequence[Any]: ...


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


def _image_input(image: ImageInput) -> Image.Image:
    if isinstance(image, Path):
        path = image.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("Image path must point to a file")
        with Image.open(path) as opened:
            opened.load()
            source = opened.copy()
    elif isinstance(image, Image.Image):
        source = image.copy()
    elif isinstance(image, np.ndarray):
        if image.dtype != np.uint8:
            raise ValueError("Image arrays must use uint8 pixels")
        if image.ndim != 3 or image.shape[2] not in {3, 4} or 0 in image.shape[:2]:
            raise ValueError("Image arrays must have shape HxWx3 or HxWx4")
        pixels = np.array(image, copy=True)
        source = Image.fromarray(pixels, mode="RGB" if pixels.shape[2] == 3 else "RGBA")
    else:
        raise TypeError("image must be a pathlib.Path, PIL image, or numpy ndarray")

    if source.width <= 0 or source.height <= 0:
        raise ValueError("Image dimensions must be positive")
    if "A" in source.getbands() or "transparency" in source.info:
        return source.convert("RGBA")
    return source.convert("RGB")


def _mask_input(mask: ImageInput) -> Image.Image:
    if isinstance(mask, Path):
        path = mask.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("Mask path must point to a file")
        with Image.open(path) as opened:
            opened.load()
            source = opened.copy()
    elif isinstance(mask, Image.Image):
        source = mask.copy()
    elif isinstance(mask, np.ndarray):
        if mask.dtype not in {np.dtype(np.uint8), np.dtype(np.bool_)}:
            raise ValueError("Mask arrays must use uint8 or bool pixels")
        if mask.ndim == 3 and mask.shape[2] == 1:
            pixels = np.array(mask[..., 0], copy=True)
        elif mask.ndim == 2:
            pixels = np.array(mask, copy=True)
        else:
            raise ValueError("Mask arrays must have shape HxW or HxWx1")
        if 0 in pixels.shape:
            raise ValueError("Mask dimensions must be positive")
        if pixels.dtype == np.bool_:
            pixels = pixels.astype(np.uint8) * 255
        source = Image.fromarray(pixels, mode="L")
    else:
        raise TypeError("mask must be a pathlib.Path, PIL image, or numpy ndarray")

    if len(source.getbands()) != 1:
        raise ValueError("Mask must be a single-channel image")
    if source.width <= 0 or source.height <= 0:
        raise ValueError("Mask dimensions must be positive")
    return source.convert("L")


def _context_box(mask: np.ndarray, padding: int) -> tuple[int, int, int, int] | None:
    rows, columns = np.nonzero(mask > 0)
    if not len(rows):
        return None
    height, width = mask.shape
    left = max(0, int(columns.min()) - padding)
    top = max(0, int(rows.min()) - padding)
    right = min(width, int(columns.max()) + 1 + padding)
    bottom = min(height, int(rows.max()) + 1 + padding)
    return left, top, right, bottom


def _shape_matches(actual: Any, expected: tuple[int, int, int, int]) -> bool:
    if not isinstance(actual, Sequence) or len(actual) != len(expected):
        return False
    for dimension, required in zip(actual, expected, strict=True):
        if isinstance(dimension, int) and dimension not in {-1, required}:
            return False
    return True


def _validate_session(session: Any) -> _Session:
    try:
        inputs = {item.name: item for item in session.get_inputs()}
    except (AttributeError, TypeError) as error:
        raise LaMaProviderError("LaMa session does not expose ONNX input metadata") from error
    if IMAGE_INPUT_NAME not in inputs or MASK_INPUT_NAME not in inputs:
        raise LaMaProviderError("LaMa model must expose 'image' and 'mask' inputs")
    image_shape = getattr(inputs[IMAGE_INPUT_NAME], "shape", None)
    mask_shape = getattr(inputs[MASK_INPUT_NAME], "shape", None)
    if not _shape_matches(image_shape, (1, 3, MODEL_SIZE, MODEL_SIZE)):
        raise LaMaProviderError("LaMa image input must have shape 1x3x512x512")
    if not _shape_matches(mask_shape, (1, 1, MODEL_SIZE, MODEL_SIZE)):
        raise LaMaProviderError("LaMa mask input must have shape 1x1x512x512")
    return session


def _model_inputs(image_rgb: np.ndarray, mask: np.ndarray) -> dict[str, np.ndarray]:
    image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    resized_image = cv2.resize(
        image_bgr,
        (MODEL_SIZE, MODEL_SIZE),
        interpolation=cv2.INTER_AREA,
    )
    resized_mask = cv2.resize(
        mask,
        (MODEL_SIZE, MODEL_SIZE),
        interpolation=cv2.INTER_NEAREST,
    )
    image_tensor = np.transpose(resized_image.astype(np.float32) * 0.00392, (2, 0, 1))[None]
    mask_tensor = (resized_mask > 0).astype(np.float32)[None, None]
    return {
        IMAGE_INPUT_NAME: np.ascontiguousarray(image_tensor),
        MASK_INPUT_NAME: np.ascontiguousarray(mask_tensor),
    }


def _model_output(outputs: Sequence[Any], size: tuple[int, int]) -> np.ndarray:
    if not outputs:
        raise LaMaProviderError("LaMa inference returned no output")
    output = np.asarray(outputs[0])
    if output.ndim != 4 or output.shape[0] != 1 or output.shape[1] != 3:
        raise LaMaProviderError("LaMa output must have shape 1x3xHxW")
    if not np.issubdtype(output.dtype, np.number) or not np.all(np.isfinite(output)):
        raise LaMaProviderError("LaMa output must contain finite numeric pixels")
    output_bgr = np.transpose(output[0], (1, 2, 0))
    output_bgr = np.clip(np.rint(output_bgr), 0, 255).astype(np.uint8)
    resized_bgr = cv2.resize(output_bgr, size, interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(resized_bgr, cv2.COLOR_BGR2RGB)


def _blend_weights(mask: np.ndarray, feather: int) -> np.ndarray:
    weights = mask.astype(np.float32) / 255.0
    if feather:
        kernel = feather * 2 + 1
        weights = cv2.GaussianBlur(weights, (kernel, kernel), 0)
        # Feather inward only: a zero-valued mask pixel must remain bit-exactly unchanged.
        weights *= mask > 0
    return np.clip(weights, 0.0, 1.0)[..., None]


class LaMaONNXInpaintingProvider:
    """Optional local OpenCV-Zoo-compatible LaMa provider backed by ONNX Runtime."""

    name = "lama-onnx"

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        execution_providers: Sequence[str] | None = None,
        context_padding: int = 64,
        feather: int = 4,
        session_factory: SessionFactory | None = None,
    ) -> None:
        _validate_nonnegative_int("context_padding", context_padding, maximum=4096)
        _validate_nonnegative_int("feather", feather, maximum=255)
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

        self.model_path = normalized_model_path
        self.execution_providers = normalized_providers
        self.context_padding = context_padding
        self.feather = feather
        self._session_factory = session_factory
        self._session: _Session | None = None
        self._session_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @staticmethod
    def create_mask(
        image: Path | Image.Image | tuple[int, int] | np.ndarray,
        regions: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> np.ndarray:
        return create_mask(image, regions, **options)

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
            error = "LaMa model path is not configured"
        elif not model_exists and not loaded:
            error = f"LaMa model file was not found: {self.model_path}"
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
            "aiInpainting": True,
            "modelFormat": "onnx",
            "modelInputSize": [MODEL_SIZE, MODEL_SIZE],
            "inputNames": [IMAGE_INPUT_NAME, MASK_INPUT_NAME],
            "inputTypes": ["path", "pil", "ndarray"],
            "preservesAlpha": True,
            "modifiesSource": False,
            "contextCrop": True,
            "softMaskComposite": True,
            "downloadsModelsAtStartup": False,
            "executionProviders": list(self.execution_providers or ()),
            "error": health["error"],
        }

    def _create_session(self) -> _Session:
        if self.model_path is None:
            raise LaMaUnavailable("LaMa model path is not configured")
        if not self.model_path.is_file():
            raise LaMaUnavailable(f"LaMa model file was not found: {self.model_path}")
        if self._session_factory is not None:
            factory = self._session_factory
        else:
            runtime_available, _ = _runtime_details()
            if not runtime_available:
                raise LaMaUnavailable("onnxruntime is not installed")
            try:
                onnxruntime = importlib.import_module("onnxruntime")
            except ImportError as error:
                raise LaMaUnavailable("onnxruntime could not be imported") from error

            def factory(path: str, providers: tuple[str, ...] | None) -> Any:
                options = {"providers": list(providers)} if providers else {}
                session_options = onnxruntime.SessionOptions()
                # The OpenCV Zoo model contains harmless unused initializers. Keep
                # routine local runs readable while preserving runtime errors.
                session_options.log_severity_level = 3
                return onnxruntime.InferenceSession(
                    path,
                    sess_options=session_options,
                    **options,
                )

        try:
            session = factory(str(self.model_path), self.execution_providers)
        except LaMaProviderError:
            raise
        except Exception as error:
            detail = str(error).strip()[:500]
            raise LaMaUnavailable(f"LaMa ONNX session could not be created: {detail}") from error
        return _validate_session(session)

    def _get_session(self) -> _Session:
        if self._session is not None:
            return self._session
        with self._session_lock:
            if self._session is None:
                self._session = self._create_session()
            return self._session

    def inpaint(
        self,
        image: ImageInput,
        mask: ImageInput,
        *,
        context_padding: int | None = None,
        feather: int | None = None,
        **options: Any,
    ) -> Image.Image:
        if options:
            unknown = ", ".join(sorted(options))
            raise TypeError(f"Unknown LaMa inpainting option(s): {unknown}")
        selected_padding = self.context_padding if context_padding is None else context_padding
        selected_feather = self.feather if feather is None else feather
        _validate_nonnegative_int("context_padding", selected_padding, maximum=4096)
        _validate_nonnegative_int("feather", selected_feather, maximum=255)

        source = _image_input(image)
        mask_image = _mask_input(mask)
        if source.size != mask_image.size:
            raise ValueError("Image and inpainting mask dimensions differ")
        source_rgb = np.asarray(source.convert("RGB"), dtype=np.uint8).copy()
        mask_array = np.asarray(mask_image, dtype=np.uint8).copy()
        box = _context_box(mask_array, selected_padding)
        if box is None:
            return source.copy()
        left, top, right, bottom = box
        crop_rgb = source_rgb[top:bottom, left:right].copy()
        crop_mask = mask_array[top:bottom, left:right].copy()
        feeds = _model_inputs(crop_rgb, crop_mask)
        session = self._get_session()
        try:
            with self._inference_lock:
                outputs = session.run(None, feeds)
        except Exception as error:
            detail = str(error).strip()[:500]
            raise LaMaProviderError(f"LaMa inference failed: {detail}") from error
        generated = _model_output(outputs, (right - left, bottom - top))
        weights = _blend_weights(crop_mask, selected_feather)
        blended = crop_rgb.astype(np.float32) * (1.0 - weights)
        blended += generated.astype(np.float32) * weights
        result_rgb = source_rgb.copy()
        result_rgb[top:bottom, left:right] = np.rint(blended).astype(np.uint8)
        result = Image.fromarray(result_rgb, mode="RGB")
        if source.mode == "RGBA":
            result.putalpha(source.getchannel("A"))
        return result


LaMaInpaintingProvider = LaMaONNXInpaintingProvider

__all__ = [
    "LaMaInpaintingProvider",
    "LaMaONNXInpaintingProvider",
    "LaMaProviderError",
    "LaMaUnavailable",
]
