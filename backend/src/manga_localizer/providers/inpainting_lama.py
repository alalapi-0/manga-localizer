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

from manga_localizer.imaging.inpainting import create_mask, validate_render_scale
from manga_localizer.imaging.lineart_inpaint import (
    composite_mask_outside,
    is_effectively_grayscale,
    preserve_grayscale,
)

type ImageInput = Path | Image.Image | np.ndarray
type SessionFactory = Callable[[str, tuple[str, ...] | None], Any]

MODEL_SIZE = 512
TILE_OVERLAP = 128
TILE_STRIDE = MODEL_SIZE - TILE_OVERLAP
IMAGE_INPUT_NAME = "image"
MASK_INPUT_NAME = "mask"
DEFAULT_INFERENCE_PADDING = 4
COMPONENT_CONTEXT_PADDING = 32
COMPONENT_INFERENCE_PADDING = 1
MAX_COMPONENT_CANDIDATE_PARTS = 128
OVERVIEW_CONTEXT_PADDING = 128
OVERVIEW_CORE_CONTEXT = 64
OVERVIEW_CORE_OVERLAP = 64
MAX_OVERVIEW_REFINE_PASSES = 64


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


def _expanded_inference_mask(mask: np.ndarray, padding: int) -> np.ndarray:
    """Hide contaminated glyph edges from LaMa without widening the review mask."""
    binary = np.where(mask > 0, 255, 0).astype(np.uint8)
    if not padding or not np.any(binary):
        return binary
    kernel_size = padding * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    return cv2.dilate(binary, kernel, iterations=1)


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


def _tile_axis_starts(length: int) -> list[int]:
    if length <= 0:
        raise ValueError("Tile dimensions must be positive")
    if length <= MODEL_SIZE:
        return [0]
    starts = list(range(0, length - MODEL_SIZE + 1, TILE_STRIDE))
    final_start = length - MODEL_SIZE
    if starts[-1] != final_start:
        starts.append(final_start)
    return starts


def _padded_model_tile(
    image_rgb: np.ndarray,
    mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, tuple[slice, slice]]:
    height, width = mask.shape
    if image_rgb.shape != (height, width, 3):
        raise ValueError("LaMa tile image and mask dimensions differ")
    if height > MODEL_SIZE or width > MODEL_SIZE:
        raise ValueError("LaMa native tiles cannot exceed 512x512 pixels")
    pad_top = (MODEL_SIZE - height) // 2
    pad_bottom = MODEL_SIZE - height - pad_top
    pad_left = (MODEL_SIZE - width) // 2
    pad_right = MODEL_SIZE - width - pad_left
    border_mode = cv2.BORDER_REFLECT_101 if min(height, width) > 1 else cv2.BORDER_REPLICATE
    padded_image = cv2.copyMakeBorder(
        image_rgb,
        pad_top,
        pad_bottom,
        pad_left,
        pad_right,
        border_mode,
    )
    padded_mask = cv2.copyMakeBorder(
        mask,
        pad_top,
        pad_bottom,
        pad_left,
        pad_right,
        border_mode,
    )
    padded_mask = np.where(padded_mask > 0, 255, 0).astype(np.uint8)
    content = (
        slice(pad_top, pad_top + height),
        slice(pad_left, pad_left + width),
    )
    return padded_image, padded_mask, content


def _model_inputs(image_rgb: np.ndarray, mask: np.ndarray) -> dict[str, np.ndarray]:
    if image_rgb.shape != (MODEL_SIZE, MODEL_SIZE, 3):
        raise ValueError("LaMa model image tile must be 512x512 RGB")
    if mask.shape != (MODEL_SIZE, MODEL_SIZE):
        raise ValueError("LaMa model mask tile must be 512x512")
    image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    image_tensor = np.transpose(image_bgr.astype(np.float32) * 0.00392, (2, 0, 1))[None]
    mask_tensor = (mask > 0).astype(np.float32)[None, None]
    return {
        IMAGE_INPUT_NAME: np.ascontiguousarray(image_tensor),
        MASK_INPUT_NAME: np.ascontiguousarray(mask_tensor),
    }


def _model_output(outputs: Sequence[Any]) -> np.ndarray:
    if not outputs:
        raise LaMaProviderError("LaMa inference returned no output")
    output = np.asarray(outputs[0])
    if output.ndim != 4 or output.shape[0] != 1 or output.shape[1] != 3:
        raise LaMaProviderError("LaMa output must have shape 1x3xHxW")
    if not np.issubdtype(output.dtype, np.number) or not np.all(np.isfinite(output)):
        raise LaMaProviderError("LaMa output must contain finite numeric pixels")
    output_bgr = np.transpose(output[0], (1, 2, 0))
    output_bgr = np.clip(np.rint(output_bgr), 0, 255).astype(np.uint8)
    if output_bgr.shape != (MODEL_SIZE, MODEL_SIZE, 3):
        raise LaMaProviderError("LaMa output spatial dimensions must be 512x512")
    return cv2.cvtColor(output_bgr, cv2.COLOR_BGR2RGB)


def _tile_blend_weights(
    height: int,
    width: int,
    *,
    top: int,
    left: int,
    full_height: int,
    full_width: int,
) -> np.ndarray:
    def axis_weights(length: int, start: int, full_length: int) -> np.ndarray:
        weights = np.ones(length, dtype=np.float32)
        fade = min(TILE_OVERLAP, length)
        phase = np.linspace(0.0, np.pi / 2.0, fade, dtype=np.float32)
        ramp = np.square(np.sin(phase))
        if start > 0:
            weights[:fade] = np.minimum(weights[:fade], ramp)
        if start + length < full_length:
            weights[-fade:] = np.minimum(weights[-fade:], ramp[::-1])
        return weights

    vertical = axis_weights(height, top, full_height)
    horizontal = axis_weights(width, left, full_width)
    return vertical[:, None] * horizontal[None, :]


def _blend_weights(mask: np.ndarray, feather: int) -> np.ndarray:
    weights = mask.astype(np.float32) / 255.0
    if feather:
        kernel = feather * 2 + 1
        weights = cv2.GaussianBlur(weights, (kernel, kernel), 0)
        # Feather inward only: a zero-valued mask pixel must remain bit-exactly unchanged.
        weights *= mask > 0
    return np.clip(weights, 0.0, 1.0)[..., None]


def _overview_axis_starts(length: int, core_size: int, overlap: int) -> list[int]:
    if length <= 0 or core_size <= 0 or not 0 <= overlap < core_size:
        raise ValueError("Overview-refine tile geometry is invalid")
    if length <= core_size:
        return [0]
    stride = core_size - overlap
    starts = list(range(0, length - core_size + 1, stride))
    final_start = length - core_size
    if starts[-1] != final_start:
        if len(starts) > 1 and final_start - starts[-1] < overlap:
            starts[-1] = final_start
        else:
            starts.append(final_start)
    return starts


def _overview_core_weights(
    height: int,
    width: int,
    *,
    top: int,
    left: int,
    full_height: int,
    full_width: int,
    overlap: int,
) -> np.ndarray:
    def axis_weights(length: int, start: int, full_length: int) -> np.ndarray:
        weights = np.ones(length, dtype=np.float32)
        fade = min(overlap, length)
        if fade <= 1:
            return weights
        phase = np.linspace(0.0, np.pi / 2.0, fade, dtype=np.float32)
        ramp = np.square(np.sin(phase))
        if start > 0:
            weights[:fade] = np.minimum(weights[:fade], ramp)
        if start + length < full_length:
            weights[-fade:] = np.minimum(weights[-fade:], ramp[::-1])
        return weights

    vertical = axis_weights(height, top, full_height)
    horizontal = axis_weights(width, left, full_width)
    return vertical[:, None] * horizontal[None, :]


def _coverage_preserving_overview_mask(mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Shrink a binary support without losing any source support on back-projection."""
    target_width, target_height = size
    support = mask > 0
    resized = cv2.resize(
        support.astype(np.float32),
        (target_width, target_height),
        interpolation=cv2.INTER_AREA,
    )
    overview = resized > 0
    rows, columns = np.nonzero(support)
    if len(rows):
        target_rows = np.minimum(
            target_height - 1,
            (rows.astype(np.int64) * target_height) // mask.shape[0],
        )
        target_columns = np.minimum(
            target_width - 1,
            (columns.astype(np.int64) * target_width) // mask.shape[1],
        )
        overview[target_rows, target_columns] = True
    return overview.astype(np.uint8) * 255


class LaMaONNXInpaintingProvider:
    """Optional local OpenCV-Zoo-compatible LaMa provider backed by ONNX Runtime."""

    name = "lama-onnx"

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        execution_providers: Sequence[str] | None = None,
        context_padding: int = 64,
        inference_padding: int = DEFAULT_INFERENCE_PADDING,
        feather: int = 4,
        session_factory: SessionFactory | None = None,
    ) -> None:
        _validate_nonnegative_int("context_padding", context_padding, maximum=4096)
        _validate_nonnegative_int("inference_padding", inference_padding, maximum=512)
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
        self.inference_padding = inference_padding
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
            "preservesGrayscale": True,
            "modifiesSource": False,
            "contextCrop": True,
            "aspectPreservingInference": True,
            "tiledInference": True,
            "componentwiseCandidate": True,
            "tileSize": MODEL_SIZE,
            "tileOverlap": TILE_OVERLAP,
            "inferenceMaskPadding": self.inference_padding,
            "softMaskComposite": True,
            "editableMask": True,
            "maskEditVersion": 1,
            "maskModes": ["text", "region", "manual"],
            "textPolarities": ["auto", "dark", "light"],
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
        inference_padding: int | None = None,
        feather: int | None = None,
        render_scale: int = 1,
        **options: Any,
    ) -> Image.Image:
        if options:
            unknown = ", ".join(sorted(options))
            raise TypeError(f"Unknown LaMa inpainting option(s): {unknown}")
        render_scale = validate_render_scale(render_scale)
        selected_padding = self.context_padding if context_padding is None else context_padding
        selected_inference_padding = (
            self.inference_padding if inference_padding is None else inference_padding
        )
        selected_feather = self.feather if feather is None else feather
        _validate_nonnegative_int("context_padding", selected_padding, maximum=4096 * render_scale)
        _validate_nonnegative_int(
            "inference_padding", selected_inference_padding, maximum=512 * render_scale
        )
        _validate_nonnegative_int("feather", selected_feather, maximum=255 * render_scale)

        source = _image_input(image)
        mask_image = _mask_input(mask)
        if source.size != mask_image.size:
            raise ValueError("Image and inpainting mask dimensions differ")
        source_rgb = np.asarray(source.convert("RGB"), dtype=np.uint8).copy()
        mask_array = np.asarray(mask_image, dtype=np.uint8).copy()
        inference_mask = _expanded_inference_mask(mask_array, selected_inference_padding)
        box = _context_box(inference_mask, selected_padding)
        if box is None:
            return source.copy()
        left, top, right, bottom = box
        crop_rgb = source_rgb[top:bottom, left:right].copy()
        crop_inference_mask = inference_mask[top:bottom, left:right].copy()
        crop_review_mask = mask_array[top:bottom, left:right].copy()
        session = self._get_session()
        crop_height, crop_width = crop_review_mask.shape
        accumulated = np.zeros((crop_height, crop_width, 3), dtype=np.float32)
        accumulated_weights = np.zeros((crop_height, crop_width), dtype=np.float32)
        try:
            with self._inference_lock:
                for tile_top in _tile_axis_starts(crop_height):
                    tile_bottom = min(tile_top + MODEL_SIZE, crop_height)
                    for tile_left in _tile_axis_starts(crop_width):
                        tile_right = min(tile_left + MODEL_SIZE, crop_width)
                        tile_review_mask = crop_review_mask[
                            tile_top:tile_bottom,
                            tile_left:tile_right,
                        ]
                        if not np.any(tile_review_mask):
                            continue
                        tile_rgb = crop_rgb[
                            tile_top:tile_bottom,
                            tile_left:tile_right,
                        ]
                        tile_inference_mask = crop_inference_mask[
                            tile_top:tile_bottom,
                            tile_left:tile_right,
                        ]
                        model_rgb, model_mask, content = _padded_model_tile(
                            tile_rgb,
                            tile_inference_mask,
                        )
                        outputs = session.run(None, _model_inputs(model_rgb, model_mask))
                        tile_generated = _model_output(outputs)[content]
                        tile_height, tile_width = tile_review_mask.shape
                        tile_weights = _tile_blend_weights(
                            tile_height,
                            tile_width,
                            top=tile_top,
                            left=tile_left,
                            full_height=crop_height,
                            full_width=crop_width,
                        )
                        accumulated[
                            tile_top:tile_bottom,
                            tile_left:tile_right,
                        ] += tile_generated.astype(np.float32) * tile_weights[..., None]
                        accumulated_weights[
                            tile_top:tile_bottom,
                            tile_left:tile_right,
                        ] += tile_weights
        except Exception as error:
            if isinstance(error, LaMaProviderError):
                raise
            detail = str(error).strip()[:500]
            raise LaMaProviderError(f"LaMa inference failed: {detail}") from error
        uncovered = (crop_review_mask > 0) & (accumulated_weights <= 0)
        if np.any(uncovered):
            raise LaMaProviderError("LaMa tiled inference left review-mask pixels uncovered")
        generated = crop_rgb.copy()
        predicted = accumulated_weights > 0
        generated[predicted] = np.clip(
            np.rint(accumulated[predicted] / accumulated_weights[predicted, None]),
            0,
            255,
        ).astype(np.uint8)
        weights = _blend_weights(crop_review_mask, selected_feather)
        blended = crop_rgb.astype(np.float32) * (1.0 - weights)
        blended += generated.astype(np.float32) * weights
        result_rgb = source_rgb.copy()
        result_rgb[top:bottom, left:right] = np.rint(blended).astype(np.uint8)
        result = Image.fromarray(result_rgb, mode="RGB")
        if is_effectively_grayscale(source):
            result = preserve_grayscale(result, source)
        result = composite_mask_outside(source, result, mask_array)
        if source.mode == "RGBA":
            result = result.convert("RGBA")
            result.putalpha(source.getchannel("A"))
        return result

    def inpaint_components(
        self,
        image: ImageInput,
        mask: ImageInput,
        *,
        context_padding: int = COMPONENT_CONTEXT_PADDING,
        inference_padding: int = COMPONENT_INFERENCE_PADDING,
        feather: int = 0,
        max_components: int = MAX_COMPONENT_CANDIDATE_PARTS,
        render_scale: int = 1,
    ) -> Image.Image:
        """Redraw disconnected repair cavities one at a time with local context.

        The persisted mask remains the sole write authority. Processing each
        connected support separately prevents distant glyph groups from sharing
        one oversized model crop while still letting earlier clean pixels become
        context for later cavities.
        """
        render_scale = validate_render_scale(render_scale)
        if type(max_components) is not int or not 1 <= max_components <= 1024:
            raise ValueError("max_components must be an integer from 1 through 1024")
        source = _image_input(image)
        mask_image = _mask_input(mask)
        if source.size != mask_image.size:
            raise ValueError("Image and inpainting mask dimensions differ")
        mask_array = np.asarray(mask_image, dtype=np.uint8).copy()
        support = mask_array > 0
        component_count, labels = cv2.connectedComponents(
            support.astype(np.uint8),
            connectivity=8,
        )
        if component_count <= 1:
            return source.copy()
        if component_count - 1 > max_components:
            raise LaMaProviderError("LaMa component candidate has too many repair cavities")

        cleaned = source
        for label in range(1, component_count):
            component_mask = np.where(labels == label, mask_array, 0).astype(np.uint8)
            cleaned = self.inpaint(
                cleaned,
                component_mask,
                context_padding=context_padding,
                inference_padding=inference_padding,
                feather=feather,
                render_scale=render_scale,
            )
        cleaned = composite_mask_outside(source, cleaned, mask_array)
        if source.mode == "RGBA":
            cleaned = cleaned.convert("RGBA")
            cleaned.putalpha(source.getchannel("A"))
        return cleaned

    def inpaint_overview_candidates(
        self,
        image: ImageInput,
        mask: ImageInput,
        *,
        context_padding: int = OVERVIEW_CONTEXT_PADDING,
        inference_padding: int = DEFAULT_INFERENCE_PADDING,
        feather: int = 0,
        max_refine_tiles: int = MAX_OVERVIEW_REFINE_PASSES,
        render_scale: int = 1,
    ) -> tuple[Image.Image, Image.Image]:
        """Return global-overview and native-refined redraws for one large cavity.

        The overview pass gives LaMa the whole repair cavity in one 512px view.
        Native-resolution core passes then see that overview as immutable generated
        context, rather than independently guessing every 512px page tile. The
        persisted mask remains the sole write and review authority.
        """
        render_scale = validate_render_scale(render_scale)
        _validate_nonnegative_int(
            "context_padding",
            context_padding,
            maximum=4096 * render_scale,
        )
        _validate_nonnegative_int(
            "inference_padding",
            inference_padding,
            maximum=512 * render_scale,
        )
        _validate_nonnegative_int(
            "feather",
            feather,
            maximum=255 * render_scale,
        )
        if type(max_refine_tiles) is not int or not 1 <= max_refine_tiles <= 1024:
            raise ValueError("max_refine_tiles must be an integer from 1 through 1024")

        source = _image_input(image)
        mask_image = _mask_input(mask)
        if source.size != mask_image.size:
            raise ValueError("Image and inpainting mask dimensions differ")
        source_rgb = np.asarray(source.convert("RGB"), dtype=np.uint8).copy()
        mask_array = np.asarray(mask_image, dtype=np.uint8).copy()
        support = mask_array > 0
        if not np.any(support):
            return source.copy(), source.copy()

        inference_mask = _expanded_inference_mask(mask_array, inference_padding)
        box = _context_box(inference_mask, context_padding)
        if box is None:
            return source.copy(), source.copy()
        left, top, right, bottom = box
        crop_rgb = source_rgb[top:bottom, left:right].copy()
        crop_inference_mask = inference_mask[top:bottom, left:right].copy()
        crop_height, crop_width = crop_inference_mask.shape
        overview_scale = min(1.0, MODEL_SIZE / crop_width, MODEL_SIZE / crop_height)
        overview_width = max(1, min(MODEL_SIZE, round(crop_width * overview_scale)))
        overview_height = max(1, min(MODEL_SIZE, round(crop_height * overview_scale)))
        if (overview_width, overview_height) == (crop_width, crop_height):
            overview_rgb = crop_rgb
        else:
            overview_rgb = cv2.resize(
                crop_rgb,
                (overview_width, overview_height),
                interpolation=cv2.INTER_AREA,
            )
        overview_mask = _coverage_preserving_overview_mask(
            crop_inference_mask,
            (overview_width, overview_height),
        )
        model_rgb, model_mask, content = _padded_model_tile(overview_rgb, overview_mask)
        session = self._get_session()
        try:
            with self._inference_lock:
                overview_outputs = session.run(None, _model_inputs(model_rgb, model_mask))
            overview_prediction = _model_output(overview_outputs)[content]
        except Exception as error:
            if isinstance(error, LaMaProviderError):
                raise
            detail = str(error).strip()[:500]
            raise LaMaProviderError(f"LaMa overview inference failed: {detail}") from error
        if overview_prediction.shape[:2] != (crop_height, crop_width):
            overview_prediction = cv2.resize(
                overview_prediction,
                (crop_width, crop_height),
                interpolation=cv2.INTER_CUBIC,
            )

        generated_rgb = source_rgb.copy()
        crop_support = support[top:bottom, left:right]
        generated_crop = generated_rgb[top:bottom, left:right]
        generated_crop[crop_support] = overview_prediction[crop_support]
        overview_rgb_result = generated_rgb.copy()

        rows, columns = np.nonzero(support)
        support_left = int(columns.min())
        support_top = int(rows.min())
        support_right = int(columns.max()) + 1
        support_bottom = int(rows.max()) + 1
        support_width = support_right - support_left
        support_height = support_bottom - support_top
        core_size = MODEL_SIZE - 2 * (OVERVIEW_CORE_CONTEXT + inference_padding)
        if core_size <= OVERVIEW_CORE_OVERLAP:
            raise LaMaProviderError("LaMa overview inference padding leaves no safe refine core")
        overlap = min(OVERVIEW_CORE_OVERLAP, core_size // 4)
        row_starts = _overview_axis_starts(support_height, core_size, overlap)
        column_starts = _overview_axis_starts(support_width, core_size, overlap)
        refine_tiles = [
            (row_start, column_start)
            for row_start in row_starts
            for column_start in column_starts
            if np.any(
                support[
                    support_top + row_start : min(
                        support_top + row_start + core_size,
                        support_bottom,
                    ),
                    support_left + column_start : min(
                        support_left + column_start + core_size,
                        support_right,
                    ),
                ]
            )
        ]
        if len(refine_tiles) > max_refine_tiles:
            raise LaMaProviderError("LaMa overview candidate requires too many refine tiles")

        accumulated = np.zeros((support_height, support_width, 3), dtype=np.float32)
        accumulated_weights = np.zeros((support_height, support_width), dtype=np.float32)
        overview_image = Image.fromarray(overview_rgb_result, mode="RGB")
        for row_start, column_start in refine_tiles:
            tile_top = support_top + row_start
            tile_left = support_left + column_start
            tile_bottom = min(tile_top + core_size, support_bottom)
            tile_right = min(tile_left + core_size, support_right)
            core_mask = np.zeros_like(mask_array)
            core_mask[tile_top:tile_bottom, tile_left:tile_right] = np.where(
                support[tile_top:tile_bottom, tile_left:tile_right],
                255,
                0,
            ).astype(np.uint8)
            local = self.inpaint(
                overview_image,
                core_mask,
                context_padding=OVERVIEW_CORE_CONTEXT,
                inference_padding=inference_padding,
                feather=0,
                render_scale=render_scale,
            )
            local_rgb = np.asarray(local.convert("RGB"), dtype=np.uint8)
            tile_height = tile_bottom - tile_top
            tile_width = tile_right - tile_left
            tile_weights = _overview_core_weights(
                tile_height,
                tile_width,
                top=row_start,
                left=column_start,
                full_height=support_height,
                full_width=support_width,
                overlap=overlap,
            )
            valid = support[tile_top:tile_bottom, tile_left:tile_right]
            target_rows = slice(row_start, row_start + tile_height)
            target_columns = slice(column_start, column_start + tile_width)
            accumulated_view = accumulated[target_rows, target_columns]
            weights_view = accumulated_weights[target_rows, target_columns]
            accumulated_view[valid] += (
                local_rgb[tile_top:tile_bottom, tile_left:tile_right][valid].astype(np.float32)
                * tile_weights[valid, None]
            )
            weights_view[valid] += tile_weights[valid]

        support_crop = support[support_top:support_bottom, support_left:support_right]
        uncovered = support_crop & (accumulated_weights <= 0)
        if np.any(uncovered):
            raise LaMaProviderError("LaMa overview refinement left review-mask pixels uncovered")
        refined_crop = generated_rgb[
            support_top:support_bottom,
            support_left:support_right,
        ]
        refined_crop[support_crop] = np.clip(
            np.rint(accumulated[support_crop] / accumulated_weights[support_crop, None]),
            0,
            255,
        ).astype(np.uint8)

        def finish(candidate_rgb: np.ndarray) -> Image.Image:
            weights = _blend_weights(mask_array, feather)
            blended = source_rgb.astype(np.float32) * (1.0 - weights)
            blended += candidate_rgb.astype(np.float32) * weights
            result = Image.fromarray(np.rint(blended).astype(np.uint8), mode="RGB")
            if is_effectively_grayscale(source):
                result = preserve_grayscale(result, source)
            result = composite_mask_outside(source, result, mask_array)
            if source.mode == "RGBA":
                result = result.convert("RGBA")
                result.putalpha(source.getchannel("A"))
            return result

        return finish(overview_rgb_result), finish(generated_rgb)

    def inpaint_overview_refine(
        self,
        image: ImageInput,
        mask: ImageInput,
        *,
        context_padding: int = OVERVIEW_CONTEXT_PADDING,
        inference_padding: int = DEFAULT_INFERENCE_PADDING,
        feather: int = 0,
        max_refine_tiles: int = MAX_OVERVIEW_REFINE_PASSES,
        render_scale: int = 1,
    ) -> Image.Image:
        """Return the native-resolution member of the overview candidate pair."""
        return self.inpaint_overview_candidates(
            image,
            mask,
            context_padding=context_padding,
            inference_padding=inference_padding,
            feather=feather,
            max_refine_tiles=max_refine_tiles,
            render_scale=render_scale,
        )[1]


LaMaInpaintingProvider = LaMaONNXInpaintingProvider

__all__ = [
    "COMPONENT_CONTEXT_PADDING",
    "COMPONENT_INFERENCE_PADDING",
    "DEFAULT_INFERENCE_PADDING",
    "MAX_COMPONENT_CANDIDATE_PARTS",
    "MAX_OVERVIEW_REFINE_PASSES",
    "MODEL_SIZE",
    "OVERVIEW_CONTEXT_PADDING",
    "OVERVIEW_CORE_CONTEXT",
    "OVERVIEW_CORE_OVERLAP",
    "TILE_OVERLAP",
    "LaMaInpaintingProvider",
    "LaMaONNXInpaintingProvider",
    "LaMaProviderError",
    "LaMaUnavailable",
]
