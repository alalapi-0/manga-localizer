from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from PIL import Image, ImageColor


def _dimensions(image: Path | Image.Image | tuple[int, int] | np.ndarray) -> tuple[int, int]:
    if isinstance(image, tuple):
        return int(image[0]), int(image[1])
    if isinstance(image, Path):
        with Image.open(image) as opened:
            return opened.size
    if isinstance(image, Image.Image):
        return image.size
    if image.ndim < 2:
        raise ValueError("Image array must have at least two dimensions")
    return int(image.shape[1]), int(image.shape[0])


def create_mask(
    image: Path | Image.Image | tuple[int, int] | np.ndarray,
    regions: Sequence[Mapping[str, Any]],
    *,
    padding: int = 3,
    dilation: int = 1,
    feather: int = 0,
) -> np.ndarray:
    """Create a single-channel mask from editable rotated rectangles or polygons."""
    width, height = _dimensions(image)
    if width <= 0 or height <= 0:
        raise ValueError("Mask dimensions must be positive")
    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        polygon = region.get("polygon") or region.get("maskPolygon")
        if polygon:
            points = np.array(
                [[float(point[0]), float(point[1])] for point in polygon], dtype=np.float32
            )
            if len(points) < 3:
                raise ValueError("Mask polygon must have at least three points")
            cv2.fillPoly(mask, [np.rint(points).astype(np.int32)], 255)
            continue
        x = float(region["x"])
        y = float(region["y"])
        region_width = float(region["width"])
        region_height = float(region["height"])
        if region_width <= 0 or region_height <= 0:
            raise ValueError("Mask region dimensions must be positive")
        region_padding = int(region.get("padding", padding))
        center = (x + region_width / 2, y + region_height / 2)
        size = (region_width + region_padding * 2, region_height + region_padding * 2)
        angle = float(region.get("rotation", 0))
        points = cv2.boxPoints((center, size, angle))
        cv2.fillPoly(mask, [np.rint(points).astype(np.int32)], 255)
    if dilation > 0:
        size = dilation * 2 + 1
        mask = cv2.dilate(mask, np.ones((size, size), dtype=np.uint8), iterations=1)
    if feather > 0:
        size = feather * 2 + 1
        mask = cv2.GaussianBlur(mask, (size, size), 0)
    return mask


def _pil_image(image: Path | Image.Image | np.ndarray) -> Image.Image:
    if isinstance(image, Path):
        with Image.open(image) as opened:
            return opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
    if isinstance(image, Image.Image):
        return image.copy()
    if image.ndim == 2:
        return Image.fromarray(image.astype(np.uint8), mode="L")
    return Image.fromarray(image.astype(np.uint8))


def inpaint(
    image: Path | Image.Image | np.ndarray,
    mask: Path | Image.Image | np.ndarray,
    *,
    radius: float = 3.0,
    method: str = "telea",
    fill_color: str = "#ffffff",
) -> Image.Image:
    """Inpaint locally with OpenCV Telea or Navier-Stokes, preserving an existing alpha channel."""
    source = _pil_image(image)
    mask_image = _pil_image(mask).convert("L")
    if source.size != mask_image.size:
        raise ValueError("Image and inpainting mask dimensions differ")
    if radius <= 0:
        raise ValueError("Inpainting radius must be positive")
    source_array = np.asarray(source.convert("RGB"), dtype=np.uint8)
    normalized_method = method.lower().replace("_", "-")
    mask_array = np.asarray(mask_image, dtype=np.uint8)
    if normalized_method == "solid":
        fill = np.asarray(ImageColor.getrgb(fill_color), dtype=np.float32)
        alpha = mask_array.astype(np.float32)[..., np.newaxis] / 255.0
        blended = source_array.astype(np.float32) * (1.0 - alpha) + fill * alpha
        result = Image.fromarray(np.rint(blended).astype(np.uint8), mode="RGB")
        if source.mode == "RGBA":
            result.putalpha(source.getchannel("A"))
        return result
    source_bgr = cv2.cvtColor(source_array, cv2.COLOR_RGB2BGR)
    binary_mask = np.where(mask_array > 0, 255, 0).astype(np.uint8)
    flag = cv2.INPAINT_TELEA if normalized_method == "telea" else cv2.INPAINT_NS
    if normalized_method not in {"telea", "ns", "navier-stokes"}:
        raise ValueError("Inpainting method must be 'telea', 'navier-stokes', or 'solid'")
    result_bgr = cv2.inpaint(source_bgr, binary_mask, radius, flag)
    result = Image.fromarray(cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB), mode="RGB")
    if source.mode == "RGBA":
        result.putalpha(source.getchannel("A"))
    return result


class InpaintingProvider(Protocol):
    def create_mask(
        self,
        image: Path | Image.Image | tuple[int, int] | np.ndarray,
        regions: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> np.ndarray: ...

    def inpaint(
        self,
        image: Path | Image.Image | np.ndarray,
        mask: Path | Image.Image | np.ndarray,
        **options: Any,
    ) -> Image.Image: ...

    def health_check(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


class OpenCVInpaintingProvider:
    name = "opencv"

    @staticmethod
    def create_mask(
        image: Path | Image.Image | tuple[int, int] | np.ndarray,
        regions: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> np.ndarray:
        return create_mask(image, regions, **options)

    @staticmethod
    def inpaint(
        image: Path | Image.Image | np.ndarray,
        mask: Path | Image.Image | np.ndarray,
        **options: Any,
    ) -> Image.Image:
        return inpaint(image, mask, **options)

    def health_check(self) -> dict[str, Any]:
        return {"available": True, "version": cv2.__version__}

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "methods": ["telea", "navier-stokes", "solid"],
            "editableMask": True,
        }
