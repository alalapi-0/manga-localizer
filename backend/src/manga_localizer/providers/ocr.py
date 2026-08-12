from __future__ import annotations

import csv
import io
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from PIL import Image

from manga_localizer.logging_utils import redact


class OCRProviderError(RuntimeError):
    pass


class OCRUnavailable(OCRProviderError):
    pass


@dataclass(frozen=True)
class OCRRegion:
    x: int
    y: int
    width: int
    height: int
    text: str
    confidence: float | None
    direction: str
    polygon: tuple[tuple[float, float], ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OCRProvider(Protocol):
    def detect_text_regions(
        self,
        image: Path | Image.Image,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> list[OCRRegion]: ...

    def recognize_region(
        self,
        image: Path | Image.Image,
        region: Mapping[str, float] | tuple[int, int, int, int],
        *,
        direction: str = "vertical",
        language: str | None = None,
    ) -> OCRRegion: ...

    def recognize_image(
        self,
        image: Path | Image.Image,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> str: ...

    def health_check(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


class TesseractOCRProvider:
    name = "tesseract"

    """A real local Tesseract adapter; unavailable language packs never fall back to mock OCR."""

    def __init__(self, command: str = "tesseract", timeout: float = 120.0):
        self.command = command
        self.timeout = timeout

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        executable = shutil.which(self.command)
        if executable is None:
            raise OCRUnavailable(f"Tesseract executable was not found: {self.command}")
        try:
            result = subprocess.run(
                [executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OCRUnavailable(f"Tesseract could not run: {redact(error)}") from error
        if result.returncode != 0:
            detail = str(redact(result.stderr.strip()))[:500]
            raise OCRProviderError(f"Tesseract failed ({result.returncode}): {detail}")
        return result

    def available_languages(self) -> list[str]:
        result = self._run("--list-langs")
        lines = [line.strip() for line in result.stdout.splitlines()]
        return sorted(line for line in lines if line and not line.lower().startswith("list of"))

    def _language(self, direction: str, language: str | None) -> str:
        selected = language or ("jpn_vert" if direction == "vertical" else "jpn")
        if selected not in self.available_languages():
            raise OCRUnavailable(f"Tesseract language pack is not installed: {selected}")
        return selected

    def _candidate_directions(self, direction: str, language: str | None) -> list[str]:
        if direction not in {"auto", "horizontal", "vertical"}:
            raise ValueError("OCR direction must be auto, horizontal, or vertical")
        if direction in {"horizontal", "vertical"}:
            return [direction]
        if language:
            return ["vertical" if "vert" in language.lower() else "horizontal"]
        available = set(self.available_languages())
        candidates = []
        if "jpn" in available:
            candidates.append("horizontal")
        if "jpn_vert" in available:
            candidates.append("vertical")
        if not candidates:
            raise OCRUnavailable("No Japanese Tesseract language pack is installed")
        return candidates

    @contextmanager
    def _image_path(self, image: Path | Image.Image) -> Iterator[Path]:
        if isinstance(image, Path):
            yield image.resolve(strict=True)
            return
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            image.save(temporary, format="PNG")
            yield temporary
        finally:
            temporary.unlink(missing_ok=True)

    def health(self) -> dict[str, Any]:
        try:
            version = self._run("--version").stdout.splitlines()[0]
            languages = self.available_languages()
        except OCRProviderError as error:
            return {
                "available": False,
                "version": None,
                "languages": [],
                "japaneseHorizontal": False,
                "japaneseVertical": False,
                "error": str(error),
            }
        return {
            "available": "jpn" in languages or "jpn_vert" in languages,
            "version": version,
            "languages": languages,
            "japaneseHorizontal": "jpn" in languages,
            "japaneseVertical": "jpn_vert" in languages,
            "error": None,
        }

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def capabilities(self) -> dict[str, Any]:
        health = self.health()
        return {
            "provider": "tesseract",
            "available": health["available"],
            "detectTextRegions": health["available"],
            "recognizeRegion": health["available"],
            "recognizeImage": health["available"],
            "languages": health["languages"],
            "directions": {
                "horizontal": health["japaneseHorizontal"],
                "vertical": health["japaneseVertical"],
            },
        }

    def get_capabilities(self) -> dict[str, Any]:
        return self.capabilities()

    def _tsv(
        self,
        image_path: Path,
        *,
        language: str,
        page_segmentation: int,
    ) -> list[dict[str, str]]:
        result = self._run(
            str(image_path),
            "stdout",
            "-l",
            language,
            "--psm",
            str(page_segmentation),
            "tsv",
        )
        return list(csv.DictReader(io.StringIO(result.stdout), delimiter="\t"))

    @staticmethod
    def _overlap(left: OCRRegion, right: OCRRegion) -> float:
        intersection_width = max(
            0,
            min(left.x + left.width, right.x + right.width) - max(left.x, right.x),
        )
        intersection_height = max(
            0,
            min(left.y + left.height, right.y + right.height) - max(left.y, right.y),
        )
        intersection = intersection_width * intersection_height
        if not intersection:
            return 0.0
        union = left.width * left.height + right.width * right.height - intersection
        return intersection / union if union else 0.0

    @staticmethod
    def _overlap_of_smaller(left: OCRRegion, right: OCRRegion) -> float:
        intersection_width = max(
            0,
            min(left.x + left.width, right.x + right.width) - max(left.x, right.x),
        )
        intersection_height = max(
            0,
            min(left.y + left.height, right.y + right.height) - max(left.y, right.y),
        )
        smaller = min(left.width * left.height, right.width * right.height)
        return intersection_width * intersection_height / smaller if smaller else 0.0

    @staticmethod
    def _quality(region: OCRRegion) -> float:
        characters = len("".join(region.text.split()))
        confidence = max(0.0, min(1.0, region.confidence or 0.0))
        orientation_matches = (
            region.direction == "vertical" and region.height >= region.width
        ) or (region.direction == "horizontal" and region.width >= region.height)
        orientation_weight = 1.25 if orientation_matches else 0.75
        return characters * (0.25 + confidence) * orientation_weight

    @staticmethod
    def _regions_from_rows(rows: list[dict[str, str]], direction: str) -> list[OCRRegion]:
        grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            grouped[
                (row.get("block_num", "0"), row.get("par_num", "0"), row.get("line_num", "0"))
            ].append(row)
        regions: list[OCRRegion] = []
        for words in grouped.values():
            left = min(int(word["left"]) for word in words)
            top = min(int(word["top"]) for word in words)
            right = max(int(word["left"]) + int(word["width"]) for word in words)
            bottom = max(int(word["top"]) + int(word["height"]) for word in words)
            confidences = [
                float(word["conf"]) for word in words if float(word.get("conf", -1)) >= 0
            ]
            separator = "" if direction == "vertical" else " "
            regions.append(
                OCRRegion(
                    x=left,
                    y=top,
                    width=right - left,
                    height=bottom - top,
                    text=separator.join(word["text"].strip() for word in words),
                    confidence=(sum(confidences) / len(confidences) / 100) if confidences else None,
                    direction=direction,
                )
            )
        return regions

    @classmethod
    def _contour_candidates(
        cls,
        image_path: Path,
        direction: str,
    ) -> list[OCRRegion]:
        """Find editable text-line candidates when Tesseract cannot classify the full page."""
        with Image.open(image_path) as opened:
            grayscale = np.asarray(opened.convert("L"), dtype=np.uint8)
        height, width = grayscale.shape
        _, threshold = cv2.threshold(
            grayscale,
            0,
            255,
            cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
        )
        component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(
            threshold,
            8,
        )
        cleaned = np.zeros_like(threshold)
        for index in range(1, component_count):
            component_width = int(component_stats[index, cv2.CC_STAT_WIDTH])
            component_height = int(component_stats[index, cv2.CC_STAT_HEIGHT])
            component_area = int(component_stats[index, cv2.CC_STAT_AREA])
            component_box_area = component_width * component_height
            if (
                component_area >= 3
                and component_box_area <= width * height * 0.025
                and component_width <= width * 0.4
                and component_height <= height * 0.4
            ):
                cleaned[component_labels == index] = 255
        orientations = (
            [("horizontal", (max(15, width // 30), max(5, height // 140)))]
            if direction == "horizontal"
            else [("vertical", (max(5, width // 140), max(15, height // 30)))]
            if direction == "vertical"
            else [
                ("horizontal", (max(15, width // 30), max(5, height // 140))),
                ("vertical", (max(5, width // 140), max(15, height // 30))),
            ]
        )
        grouped_candidates: dict[str, list[tuple[OCRRegion, int]]] = {}
        for candidate_direction, kernel_size in orientations:
            candidates: list[tuple[OCRRegion, int]] = []
            grouped = cv2.dilate(
                cleaned,
                cv2.getStructuringElement(cv2.MORPH_RECT, kernel_size),
                iterations=1,
            )
            contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, region_width, region_height = cv2.boundingRect(contour)
                box_area = region_width * region_height
                if (
                    region_width < 8
                    or region_height < 8
                    or box_area < 80
                    or box_area > width * height * 0.07
                    or region_width > width * 0.75
                    or region_height > height * 0.7
                ):
                    continue
                source_pixels = cleaned[y : y + region_height, x : x + region_width]
                density = float(np.count_nonzero(source_pixels)) / box_area
                if not 0.025 <= density <= 0.72:
                    continue
                labels, _, stats, _ = cv2.connectedComponentsWithStats(source_pixels, 8)
                meaningful = sum(
                    3 <= int(stats[index, cv2.CC_STAT_AREA]) <= box_area * 0.5
                    for index in range(1, labels)
                )
                if meaningful < 2:
                    continue
                padding = 4
                left = max(0, x - padding)
                top = max(0, y - padding)
                right = min(width, x + region_width + padding)
                bottom = min(height, y + region_height + padding)
                candidate = OCRRegion(
                    x=left,
                    y=top,
                    width=right - left,
                    height=bottom - top,
                    text="",
                    confidence=None,
                    direction=candidate_direction,
                )
                if any(cls._overlap(candidate, existing) > 0.55 for existing, _ in candidates):
                    continue
                candidates.append((candidate, meaningful))
            grouped_candidates[candidate_direction] = candidates
        if direction == "auto":
            selected = max(
                grouped_candidates.values(),
                key=lambda entries: (
                    sum(score for _, score in entries) / max(1, len(entries)),
                    sum(score for _, score in entries),
                ),
                default=[],
            )
        else:
            selected = grouped_candidates.get(direction, [])
        return [candidate for candidate, _ in selected]

    def detect_text_regions(
        self,
        image: Path | Image.Image,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> list[OCRRegion]:
        with self._image_path(image) as path:
            regions: list[OCRRegion] = []
            for candidate_direction in self._candidate_directions(direction, language):
                selected = self._language(candidate_direction, language)
                rows = self._tsv(
                    path,
                    language=selected,
                    page_segmentation=5 if candidate_direction == "vertical" else 11,
                )
                regions.extend(self._regions_from_rows(rows, candidate_direction))
            if direction == "auto" and regions:
                accepted: list[OCRRegion] = []
                for candidate in sorted(regions, key=self._quality, reverse=True):
                    if any(
                        self._overlap_of_smaller(candidate, existing) > 0.45
                        for existing in accepted
                    ):
                        continue
                    accepted.append(candidate)
                regions = accepted
            if not regions:
                regions = self._contour_candidates(path, direction)
        return sorted(
            regions,
            key=lambda region: (
                -(region.x + region.width / 2)
                if direction == "auto" or region.direction == "vertical"
                else region.y,
                region.y if direction == "auto" or region.direction == "vertical" else region.x,
            ),
        )

    def recognize_region(
        self,
        image: Path | Image.Image,
        region: Mapping[str, float] | tuple[int, int, int, int],
        *,
        direction: str = "vertical",
        language: str | None = None,
    ) -> OCRRegion:
        if isinstance(region, Mapping):
            box = tuple(round(float(region[key])) for key in ("x", "y", "width", "height"))
        else:
            box = tuple(int(value) for value in region)
        x, y, width, height = box
        if min(x, y) < 0 or width <= 0 or height <= 0:
            raise ValueError("OCR region has invalid geometry")
        with self._image_path(image) as path, Image.open(path) as source:
            if x + width > source.width or y + height > source.height:
                raise ValueError("OCR region exceeds image bounds")
            crop = source.crop((x, y, x + width, y + height))
            with self._image_path(crop) as crop_path:
                candidates: list[OCRRegion] = []
                for candidate_direction in self._candidate_directions(direction, language):
                    selected = self._language(candidate_direction, language)
                    rows = self._tsv(
                        crop_path,
                        language=selected,
                        page_segmentation=5 if candidate_direction == "vertical" else 6,
                    )
                    parsed = self._regions_from_rows(rows, candidate_direction)
                    text = ("" if candidate_direction == "vertical" else " ").join(
                        entry.text for entry in parsed if entry.text
                    )
                    confidences = [
                        entry.confidence for entry in parsed if entry.confidence is not None
                    ]
                    candidates.append(
                        OCRRegion(
                            x=x,
                            y=y,
                            width=width,
                            height=height,
                            text=text,
                            confidence=(sum(confidences) / len(confidences))
                            if confidences
                            else None,
                            direction=candidate_direction,
                        )
                    )
        return max(candidates, key=self._quality)

    def recognize_image(
        self,
        image: Path | Image.Image,
        *,
        direction: str = "auto",
        language: str | None = None,
    ) -> str:
        with self._image_path(image) as path, Image.open(path) as source:
            region = (0, 0, source.width, source.height)
            return self.recognize_region(
                path,
                region,
                direction=direction,
                language=language,
            ).text.strip()
