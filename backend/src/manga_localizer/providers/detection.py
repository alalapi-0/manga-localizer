from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from PIL import Image

from manga_localizer.providers.ocr import OCRRegion


class TextDetectionError(RuntimeError):
    pass


class TextDetectionUnavailable(TextDetectionError):
    pass


class TextDetectionProvider(Protocol):
    def detect_text_regions(
        self,
        image: Path | Image.Image | np.ndarray,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> list[OCRRegion]: ...

    def health_check(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


def _bgr_image(image: Path | Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(image, Path):
        try:
            with Image.open(image) as opened:
                # Project coordinates use the immutable file's raw pixel grid.
                # A provider-local EXIF transpose would invalidate that contract.
                picture = opened.convert("RGB")
                rgb = np.asarray(picture, dtype=np.uint8)
        except (OSError, ValueError) as error:
            raise TextDetectionError("Text detector could not decode the image") from error
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if isinstance(image, Image.Image):
        picture = image.convert("RGB")
        return cv2.cvtColor(np.asarray(picture, dtype=np.uint8), cv2.COLOR_RGB2BGR)
    array = np.asarray(image)
    if array.ndim == 2:
        return cv2.cvtColor(array.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    if array.ndim != 3 or array.shape[2] not in {3, 4}:
        raise ValueError("Detector image array must be grayscale, RGB/BGR, or RGBA/BGRA")
    # Provider arrays follow the imaging package convention and are interpreted as RGB(A).
    code = cv2.COLOR_RGBA2BGR if array.shape[2] == 4 else cv2.COLOR_RGB2BGR
    return cv2.cvtColor(array.astype(np.uint8), code)


class PPOCRTextDetectionProvider:
    """OpenCV DNN adapter for the optional PP-OCRv3 text-detection ONNX model."""

    name = "ppocr-v3"

    def __init__(
        self,
        model_path: Path | None,
        *,
        input_size: tuple[int, int] = (736, 736),
        binary_threshold: float = 0.3,
        polygon_threshold: float = 0.5,
        unclip_ratio: float = 1.8,
        max_candidates: int = 200,
    ):
        if len(input_size) != 2 or min(input_size) <= 0 or any(value % 32 for value in input_size):
            raise ValueError("PP-OCR input dimensions must be positive multiples of 32")
        if not 0 < binary_threshold < 1 or not 0 < polygon_threshold < 1:
            raise ValueError("PP-OCR thresholds must be between zero and one")
        if unclip_ratio <= 0:
            raise ValueError("PP-OCR unclip ratio must be positive")
        if max_candidates < 1:
            raise ValueError("PP-OCR max candidates must be positive")
        self.model_path = model_path.expanduser() if model_path is not None else None
        self.input_size = tuple(int(value) for value in input_size)
        self.binary_threshold = float(binary_threshold)
        self.polygon_threshold = float(polygon_threshold)
        self.unclip_ratio = float(unclip_ratio)
        self.max_candidates = int(max_candidates)
        self._detector: Any | None = None
        self._lock = threading.RLock()

    def _load(self) -> Any:
        with self._lock:
            if self._detector is not None:
                return self._detector
            if self.model_path is None or not self.model_path.is_file():
                raise TextDetectionUnavailable(
                    "PP-OCR detection model is not configured; install the optional local model"
                )
            try:
                network = cv2.dnn.readNet(str(self.model_path.resolve()))
                detector = cv2.dnn_TextDetectionModel_DB(network)
                detector.setBinaryThreshold(self.binary_threshold)
                detector.setPolygonThreshold(self.polygon_threshold)
                detector.setUnclipRatio(self.unclip_ratio)
                detector.setMaxCandidates(self.max_candidates)
                detector.setInputParams(
                    1.0 / 255.0,
                    self.input_size,
                    (122.67891434, 116.66876762, 104.00698793),
                )
            except cv2.error as error:
                raise TextDetectionUnavailable(
                    "PP-OCR detection model could not be loaded"
                ) from error
            self._detector = detector
            return detector

    def detect_text_regions(
        self,
        image: Path | Image.Image | np.ndarray,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> list[OCRRegion]:
        del language
        if direction not in {"auto", "horizontal", "vertical"}:
            raise ValueError("Text direction must be auto, horizontal, or vertical")
        source = _bgr_image(image)
        with self._lock:
            detector = self._load()
            try:
                polygons, confidences = detector.detect(source)
            except cv2.error as error:
                raise TextDetectionError("PP-OCR text detection failed") from error
        regions: list[OCRRegion] = []
        image_height, image_width = source.shape[:2]
        for raw_polygon, raw_confidence in zip(polygons, confidences, strict=True):
            polygon_array = np.asarray(raw_polygon, dtype=np.float32).reshape(-1, 2)
            if len(polygon_array) < 3:
                continue
            polygon_array[:, 0] = np.clip(polygon_array[:, 0], 0, image_width - 1)
            polygon_array[:, 1] = np.clip(polygon_array[:, 1], 0, image_height - 1)
            x, y, width, height = cv2.boundingRect(polygon_array)
            right = min(image_width, max(0, x + width))
            bottom = min(image_height, max(0, y + height))
            x = max(0, min(image_width, x))
            y = max(0, min(image_height, y))
            width = right - x
            height = bottom - y
            if width <= 1 or height <= 1:
                continue
            inferred_direction = "vertical" if height > width * 1.2 else "horizontal"
            if direction != "auto" and inferred_direction != direction:
                continue
            polygon = tuple((float(point[0]), float(point[1])) for point in polygon_array)
            regions.append(
                OCRRegion(
                    x=int(x),
                    y=int(y),
                    width=int(width),
                    height=int(height),
                    text="",
                    confidence=max(0.0, min(1.0, float(raw_confidence))),
                    direction=inferred_direction,
                    polygon=polygon,
                )
            )
        return sorted(
            regions,
            key=lambda region: (
                -(region.x + region.width / 2) if direction in {"auto", "vertical"} else region.y,
                region.y if direction in {"auto", "vertical"} else region.x,
            ),
        )

    def health_check(self) -> dict[str, Any]:
        if self.model_path is None or not self.model_path.is_file():
            return {
                "available": False,
                "modelConfigured": False,
                "error": "Optional PP-OCR detection model is not installed",
            }
        try:
            self._load()
        except TextDetectionUnavailable as error:
            return {
                "available": False,
                "modelConfigured": True,
                "error": str(error),
            }
        return {"available": True, "modelConfigured": True, "error": None}

    def get_capabilities(self) -> dict[str, Any]:
        health = self.health_check()
        return {
            "provider": self.name,
            "available": health["available"],
            "local": True,
            "modelRequired": True,
            "detectTextRegions": health["available"],
            "polygonDetections": True,
            "directions": {"horizontal": True, "vertical": True},
            "error": health["error"],
        }
