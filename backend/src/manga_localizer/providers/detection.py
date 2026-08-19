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


PPOCR_PAD_BGR = (122.67891434, 116.66876762, 104.00698793)
DETECTION_MIN_SIDE_FLOOR = 8
DETECTION_MIN_SIDE_CAP = 32


def detection_min_side_for_image(image_width: int, image_height: int) -> int:
    """Smallest usable box on the plate the detector actually sees.

    Tile noise on a 4x plate is a few pixels after mapping back to the
    original page. The threshold scales with the short side so an
    unenhanced small page still keeps compact SFX, while a wide 4x
    plate drops 3-8 px fragments.
    """
    short = min(max(0, int(image_width)), max(0, int(image_height)))
    if short < 1:
        return DETECTION_MIN_SIDE_FLOOR
    return max(DETECTION_MIN_SIDE_FLOOR, min(DETECTION_MIN_SIDE_CAP, short // 24))


def detection_region_is_usable(width: int, height: int, *, min_side: int) -> bool:
    return int(width) >= min_side and int(height) >= min_side


def letterbox_detection_image(
    source: np.ndarray,
    input_size: tuple[int, int],
    *,
    pad_bgr: tuple[float, float, float] = PPOCR_PAD_BGR,
) -> tuple[np.ndarray, float, float, float]:
    """Fit ``source`` into ``input_size`` without stretching, then pad."""
    target_width, target_height = (int(input_size[0]), int(input_size[1]))
    source_height, source_width = source.shape[:2]
    if source_width < 1 or source_height < 1 or target_width < 1 or target_height < 1:
        raise TextDetectionError("Text detector received an empty image")
    scale = min(target_width / source_width, target_height / source_height)
    resized_width = max(1, min(target_width, round(source_width * scale)))
    resized_height = max(1, min(target_height, round(source_height * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(source, (resized_width, resized_height), interpolation=interpolation)
    pad = np.array(pad_bgr, dtype=np.float32)
    canvas = np.full((target_height, target_width, 3), pad, dtype=np.float32)
    pad_x = (target_width - resized_width) // 2
    pad_y = (target_height - resized_height) // 2
    canvas[pad_y : pad_y + resized_height, pad_x : pad_x + resized_width] = resized
    return canvas.astype(np.uint8), float(scale), float(pad_x), float(pad_y)


def unletterbox_detection_points(
    points: np.ndarray,
    *,
    scale: float,
    pad_x: float,
    pad_y: float,
    image_width: int,
    image_height: int,
) -> np.ndarray:
    if scale <= 0:
        raise TextDetectionError("Text detector letterbox scale must be positive")
    mapped = np.asarray(points, dtype=np.float32).reshape(-1, 2).copy()
    mapped[:, 0] = (mapped[:, 0] - pad_x) / scale
    mapped[:, 1] = (mapped[:, 1] - pad_y) / scale
    mapped[:, 0] = np.clip(mapped[:, 0], 0, max(0, image_width - 1))
    mapped[:, 1] = np.clip(mapped[:, 1], 0, max(0, image_height - 1))
    return mapped


def detection_tile_origins(
    image_width: int,
    image_height: int,
    tile_width: int,
    tile_height: int,
    *,
    overlap: int,
) -> list[tuple[int, int, int, int]]:
    """Cover the image with overlapping tiles that never exceed the source."""
    if image_width < 1 or image_height < 1 or tile_width < 1 or tile_height < 1:
        raise TextDetectionError("Text detector received an empty image")
    window_width = min(tile_width, image_width)
    window_height = min(tile_height, image_height)
    stride_x = max(1, window_width - min(max(0, overlap), window_width - 1))
    stride_y = max(1, window_height - min(max(0, overlap), window_height - 1))

    def _axis(length: int, window: int, stride: int) -> list[int]:
        if length <= window:
            return [0]
        origins = list(range(0, length - window, stride))
        last = length - window
        if not origins or origins[-1] != last:
            origins.append(last)
        return origins

    return [
        (x, y, min(window_width, image_width - x), min(window_height, image_height - y))
        for y in _axis(image_height, window_height, stride_y)
        for x in _axis(image_width, window_width, stride_x)
    ]


def suppress_overlapping_detections(
    regions: list[OCRRegion],
    *,
    iou_threshold: float = 0.5,
) -> list[OCRRegion]:
    kept: list[OCRRegion] = []
    for region in sorted(regions, key=lambda item: item.confidence, reverse=True):
        if all(_box_iou(region, existing) < iou_threshold for existing in kept):
            kept.append(region)
    return kept


def _region_from_letterboxed_polygon(
    raw_polygon: Any,
    raw_confidence: Any,
    *,
    scale: float,
    pad_x: float,
    pad_y: float,
    image_width: int,
    image_height: int,
    direction: str,
) -> OCRRegion | None:
    polygon_array = unletterbox_detection_points(
        np.asarray(raw_polygon, dtype=np.float32),
        scale=scale,
        pad_x=pad_x,
        pad_y=pad_y,
        image_width=image_width,
        image_height=image_height,
    )
    if len(polygon_array) < 3:
        return None
    x, y, width, height = cv2.boundingRect(polygon_array)
    right = min(image_width, max(0, x + width))
    bottom = min(image_height, max(0, y + height))
    x = max(0, min(image_width, x))
    y = max(0, min(image_height, y))
    width = right - x
    height = bottom - y
    if not detection_region_is_usable(
        width,
        height,
        min_side=detection_min_side_for_image(image_width, image_height),
    ):
        return None
    inferred_direction = "vertical" if height > width * 1.2 else "horizontal"
    if direction != "auto" and inferred_direction != direction:
        return None
    polygon = tuple((float(point[0]), float(point[1])) for point in polygon_array)
    return OCRRegion(
        x=int(x),
        y=int(y),
        width=int(width),
        height=int(height),
        text="",
        confidence=max(0.0, min(1.0, float(raw_confidence))),
        direction=inferred_direction,
        polygon=polygon,
    )


def _offset_region(region: OCRRegion, pad_x: int, pad_y: int) -> OCRRegion:
    polygon = (
        tuple((point[0] + pad_x, point[1] + pad_y) for point in region.polygon)
        if region.polygon is not None
        else None
    )
    return OCRRegion(
        x=region.x + pad_x,
        y=region.y + pad_y,
        width=region.width,
        height=region.height,
        text=region.text,
        confidence=region.confidence,
        direction=region.direction,
        polygon=polygon,
    )


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
        unclip_ratio: float = 2.2,
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
        image_height, image_width = source.shape[:2]
        tiles = detection_tile_origins(
            image_width,
            image_height,
            self.input_size[0],
            self.input_size[1],
            overlap=max(1, min(self.input_size) // 4),
        )
        regions: list[OCRRegion] = []
        source_min_side = detection_min_side_for_image(image_width, image_height)
        with self._lock:
            detector = self._load()
            for tile_x, tile_y, tile_width, tile_height in tiles:
                crop = source[tile_y : tile_y + tile_height, tile_x : tile_x + tile_width]
                letterboxed, scale, pad_x, pad_y = letterbox_detection_image(
                    crop,
                    self.input_size,
                )
                try:
                    polygons, confidences = detector.detect(letterboxed)
                except cv2.error as error:
                    raise TextDetectionError("PP-OCR text detection failed") from error
                for raw_polygon, raw_confidence in zip(polygons, confidences, strict=True):
                    region = _region_from_letterboxed_polygon(
                        raw_polygon,
                        raw_confidence,
                        scale=scale,
                        pad_x=pad_x,
                        pad_y=pad_y,
                        image_width=tile_width,
                        image_height=tile_height,
                        direction=direction,
                    )
                    if region is None:
                        continue
                    offset = _offset_region(region, tile_x, tile_y)
                    if not detection_region_is_usable(
                        offset.width,
                        offset.height,
                        min_side=source_min_side,
                    ):
                        continue
                    regions.append(offset)
        return sorted(
            suppress_overlapping_detections(regions),
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
            "dropsLowConfidence": False,
            "mergesOverlaps": False,
            "directions": {"horizontal": True, "vertical": True},
            "error": health["error"],
        }


def _region_box(region: OCRRegion) -> tuple[int, int, int, int]:
    return region.x, region.y, region.x + region.width, region.y + region.height


def _box_intersection(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> int:
    width = max(0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _box_area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _box_iou(left: OCRRegion, right: OCRRegion) -> float:
    left_box = _region_box(left)
    right_box = _region_box(right)
    intersection = _box_intersection(left_box, right_box)
    if not intersection:
        return 0.0
    union = _box_area(left_box) + _box_area(right_box) - intersection
    return intersection / union if union else 0.0


def _overlap_of_smaller(left: OCRRegion, right: OCRRegion) -> float:
    left_box = _region_box(left)
    right_box = _region_box(right)
    smaller = min(_box_area(left_box), _box_area(right_box))
    return _box_intersection(left_box, right_box) / smaller if smaller else 0.0


def _inferred_direction(region: OCRRegion) -> str:
    if region.direction in {"horizontal", "vertical"}:
        return region.direction
    return "vertical" if region.height >= region.width else "horizontal"


def _box_gap(left: OCRRegion, right: OCRRegion) -> float:
    ax1, ay1, ax2, ay2 = _region_box(left)
    bx1, by1, bx2, by2 = _region_box(right)
    dx = max(0, ax1 - bx2, bx1 - ax2)
    dy = max(0, ay1 - by2, by1 - ay2)
    if dx == 0 and dy == 0:
        return 0.0
    return float((dx**2 + dy**2) ** 0.5)


def _boxes_aligned(left: OCRRegion, right: OCRRegion, direction: str) -> bool:
    ax1, ay1, ax2, ay2 = _region_box(left)
    bx1, by1, bx2, by2 = _region_box(right)
    if direction == "vertical":
        overlap = min(ax2, bx2) - max(ax1, bx1)
        span = min(ax2 - ax1, bx2 - bx1)
    else:
        overlap = min(ay2, by2) - max(ay1, by1)
        span = min(ay2 - ay1, by2 - by1)
    return span > 0 and overlap >= span * 0.4


def _image_bounds(image: Path | Image.Image | np.ndarray) -> tuple[int, int]:
    if isinstance(image, Path):
        with Image.open(image) as opened:
            return opened.size
    if isinstance(image, Image.Image):
        return image.size
    array = np.asarray(image)
    if array.ndim < 2:
        raise ValueError("Detector image array must have at least two dimensions")
    return int(array.shape[1]), int(array.shape[0])


def _merge_regions(left: OCRRegion, right: OCRRegion) -> OCRRegion:
    x = min(left.x, right.x)
    y = min(left.y, right.y)
    right_edge = max(left.x + left.width, right.x + right.width)
    bottom = max(left.y + left.height, right.y + right.height)
    primary = left if left.width * left.height >= right.width * right.height else right
    confidences = [value for value in (left.confidence, right.confidence) if value is not None]
    text = left.text.strip() or right.text.strip()
    return OCRRegion(
        x=x,
        y=y,
        width=max(1, right_edge - x),
        height=max(1, bottom - y),
        text=text,
        confidence=max(confidences) if confidences else None,
        direction=primary.direction,
        polygon=None,
    )


def _expand_region(region: OCRRegion, bounds: tuple[int, int]) -> OCRRegion:
    page_width, page_height = bounds
    pad_x = min(28, max(6, round(region.width * 0.08)))
    pad_y = min(28, max(6, round(region.height * 0.08)))
    if _inferred_direction(region) == "vertical":
        pad_x = min(32, max(pad_x, 8))
    else:
        pad_y = min(32, max(pad_y, 8))
    x = max(0, region.x - pad_x)
    y = max(0, region.y - pad_y)
    right = min(page_width, region.x + region.width + pad_x)
    bottom = min(page_height, region.y + region.height + pad_y)
    return OCRRegion(
        x=x,
        y=y,
        width=max(4, right - x),
        height=max(4, bottom - y),
        text=region.text,
        confidence=region.confidence,
        direction=region.direction,
        polygon=None,
    )


def should_merge_detection_regions(
    left: OCRRegion,
    right: OCRRegion,
    bounds: tuple[int, int],
) -> bool:
    page_area = max(1, bounds[0] * bounds[1])
    if _box_iou(left, right) >= 0.22 or _overlap_of_smaller(left, right) >= 0.5:
        union_width = max(left.x + left.width, right.x + right.width) - min(left.x, right.x)
        union_height = max(left.y + left.height, right.y + right.height) - min(left.y, right.y)
        return union_width * union_height <= page_area * 0.22
    if _inferred_direction(left) != _inferred_direction(right):
        return False
    short = min(left.width, left.height, right.width, right.height)
    if _box_gap(left, right) > max(8.0, short * 0.35):
        return False
    if not _boxes_aligned(left, right, _inferred_direction(left)):
        return False
    union_width = max(left.x + left.width, right.x + right.width) - min(left.x, right.x)
    union_height = max(left.y + left.height, right.y + right.height) - min(left.y, right.y)
    return union_width * union_height <= page_area * 0.18


def consolidate_text_regions(
    regions: list[OCRRegion],
    bounds: tuple[int, int],
    *,
    expand: bool = True,
) -> list[OCRRegion]:
    """Merge overlapping or aligned fragments, then optionally pad to enclose glyphs."""
    if not regions:
        return []
    parent = list(range(len(regions)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left, region in enumerate(regions):
        for right in range(left + 1, len(regions)):
            if should_merge_detection_regions(region, regions[right], bounds):
                union(left, right)

    clusters: dict[int, list[OCRRegion]] = {}
    order: list[int] = []
    for index, region in enumerate(regions):
        root = find(index)
        if root not in clusters:
            clusters[root] = []
            order.append(root)
        clusters[root].append(region)

    merged: list[OCRRegion] = []
    for root in order:
        cluster = clusters[root]
        current = cluster[0]
        for extra in cluster[1:]:
            current = _merge_regions(current, extra)
        merged.append(_expand_region(current, bounds) if expand else current)
    return merged


class UnionTextDetectionProvider:
    """Merge PP-OCR and Tesseract proposals into fewer, padded editable boxes.

    Overlaps, containments, and nearby aligned fragments become one box. Low
    confidence is not used to drop text. Both members must be available.
    """

    name = "ppocr-v3+tesseract"

    def __init__(self, ppocr: TextDetectionProvider, tesseract: TextDetectionProvider):
        self.ppocr = ppocr
        self.tesseract = tesseract

    def detect_text_regions(
        self,
        image: Path | Image.Image | np.ndarray,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> list[OCRRegion]:
        health = self.health_check()
        if not health["available"]:
            raise TextDetectionUnavailable(health["error"] or "Union text detector is unavailable")
        ppocr_regions = self.ppocr.detect_text_regions(
            image,
            direction=direction,
            language=language,
        )
        tesseract_regions = self.tesseract.detect_text_regions(
            image,
            direction=direction,
            language=language,
            include_contour_fallback=False,
        )
        return consolidate_text_regions(
            [*ppocr_regions, *tesseract_regions],
            _image_bounds(image),
        )

    def health_check(self) -> dict[str, Any]:
        ppocr = self.ppocr.health_check()
        tesseract = self.tesseract.health_check()
        available = bool(ppocr.get("available") and tesseract.get("available"))
        errors = [
            f"{name}: {payload['error']}"
            for name, payload in (("ppocr-v3", ppocr), ("tesseract", tesseract))
            if payload.get("error")
        ]
        return {
            "available": available,
            "members": {
                "ppocr-v3": {"available": bool(ppocr.get("available"))},
                "tesseract": {"available": bool(tesseract.get("available"))},
            },
            "error": (
                None if available else "; ".join(errors) or "Union text detector is unavailable"
            ),
        }

    def get_capabilities(self) -> dict[str, Any]:
        health = self.health_check()
        return {
            "provider": self.name,
            "available": health["available"],
            "local": True,
            "modelRequired": True,
            "detectTextRegions": health["available"],
            "polygonDetections": True,
            "keepsAllProposals": False,
            "dropsLowConfidence": False,
            "mergesOverlaps": True,
            "expandsBoxes": True,
            "unionOf": ["ppocr-v3", "tesseract"],
            "tesseractContourFallback": False,
            "directions": {"horizontal": True, "vertical": True},
            "error": health["error"],
        }
