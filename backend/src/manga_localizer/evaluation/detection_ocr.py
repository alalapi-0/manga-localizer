from __future__ import annotations

import copy
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

REQUIRED_CATEGORIES = (
    "bubble",
    "non-bubble",
    "sfx",
    "art",
    "horizontal",
    "vertical",
    "single-char",
    "negative",
    "complex-lineart",
)

REVIEWED_STATUS = "reviewed"
DRAFT_STATUS = "draft"
REJECTED_STATUS = "rejected"
GROUND_TRUTH_INDEPENDENCE = "ground-truth"
DETECTOR_DRAFT_INDEPENDENCE = "detector-draft"


@dataclass(frozen=True)
class AnnotationBox:
    x: int
    y: int
    width: int
    height: int
    text: str = ""
    direction: str = "horizontal"
    categories: tuple[str, ...] = ()
    status: str = REVIEWED_STATUS
    detector_confidence: float | None = None
    ocr_confidence: float | None = None

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)


@dataclass(frozen=True)
class PageAnnotation:
    page_id: str
    width: int
    height: int
    boxes: tuple[AnnotationBox, ...] = ()
    negative: bool = False
    status: str = REVIEWED_STATUS
    independence: str = "ground-truth"


@dataclass
class _Counts:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    def precision(self) -> float | None:
        predicted = self.true_positives + self.false_positives
        if predicted <= 0:
            return None
        return self.true_positives / predicted

    def recall(self) -> float | None:
        actual = self.true_positives + self.false_negatives
        if actual <= 0:
            return None
        return self.true_positives / actual

    def f1(self) -> float | None:
        precision = self.precision()
        recall = self.recall()
        if precision is None or recall is None or precision + recall == 0:
            return None
        return 2 * precision * recall / (precision + recall)

    def as_dict(self) -> dict[str, Any]:
        return {
            "truePositives": self.true_positives,
            "falsePositives": self.false_positives,
            "falseNegatives": self.false_negatives,
            "precision": self.precision(),
            "recall": self.recall(),
            "f1": self.f1(),
        }


def iou(left: AnnotationBox, right: AnnotationBox) -> float:
    intersection_width = max(
        0,
        min(left.x + left.width, right.x + right.width) - max(left.x, right.x),
    )
    intersection_height = max(
        0,
        min(left.y + left.height, right.y + right.height) - max(left.y, right.y),
    )
    intersection = intersection_width * intersection_height
    if intersection <= 0:
        return 0.0
    union = left.area + right.area - intersection
    return intersection / union if union else 0.0


def match_boxes(
    ground_truth: Iterable[AnnotationBox],
    predictions: Iterable[AnnotationBox],
    *,
    iou_threshold: float = 0.5,
) -> list[tuple[int, int, float]]:
    if not 0 < iou_threshold <= 1:
        raise ValueError("IoU threshold must be between 0 exclusive and 1 inclusive")
    truth = list(ground_truth)
    guessed = list(predictions)
    ranked = sorted(
        (
            (truth_index, guess_index, iou(truth_box, guess_box))
            for truth_index, truth_box in enumerate(truth)
            for guess_index, guess_box in enumerate(guessed)
        ),
        key=lambda item: item[2],
        reverse=True,
    )
    used_truth: set[int] = set()
    used_guess: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for truth_index, guess_index, score in ranked:
        if score < iou_threshold:
            break
        if truth_index in used_truth or guess_index in used_guess:
            continue
        used_truth.add(truth_index)
        used_guess.add(guess_index)
        matches.append((truth_index, guess_index, score))
    return matches


def normalize_transcription(text: str) -> str:
    compact = "".join(unicodedata.normalize("NFKC", text or "").split())
    return compact.replace("\u3000", "")


def levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            substitution = previous[j - 1] + (left_char != right_char)
            current.append(min(insertion, deletion, substitution))
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    expected = normalize_transcription(reference)
    actual = normalize_transcription(hypothesis)
    if not expected and not actual:
        return 0.0
    denominator = max(len(expected), 1)
    return levenshtein(expected, actual) / denominator


def _box_from_mapping(payload: MappingLike, *, default_status: str) -> AnnotationBox:
    categories = payload.get("categories") or ()
    if isinstance(categories, str):
        categories = [categories]
    detector_confidence = payload.get("detectorConfidence", payload.get("detector_confidence"))
    ocr_confidence = payload.get("ocrConfidence", payload.get("ocr_confidence"))
    return AnnotationBox(
        x=int(payload["x"]),
        y=int(payload["y"]),
        width=int(payload["width"]),
        height=int(payload["height"]),
        text=str(payload.get("text") or ""),
        direction=str(payload.get("direction") or "horizontal"),
        categories=tuple(str(item) for item in categories),
        status=str(payload.get("status") or default_status),
        detector_confidence=_optional_confidence(detector_confidence),
        ocr_confidence=_optional_confidence(ocr_confidence),
    )


def _optional_confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


MappingLike = dict[str, Any]


def load_annotation_document(
    payload: MappingLike,
    *,
    default_page_id: str = "page",
) -> PageAnnotation:
    image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    page_id = str(payload.get("pageId") or payload.get("id") or image.get("id") or default_page_id)
    status = str(payload.get("status") or REVIEWED_STATUS)
    boxes = tuple(
        _box_from_mapping(item, default_status=status)
        for item in payload.get("regions") or payload.get("boxes") or ()
        if isinstance(item, dict)
    )
    negative = bool(payload.get("negative") or image.get("negative"))
    if not negative and not boxes:
        negative = True
    independence = str(
        payload.get("independence") or image.get("independence") or GROUND_TRUTH_INDEPENDENCE
    )
    return PageAnnotation(
        page_id=page_id,
        width=int(payload.get("width") or image.get("width") or 0),
        height=int(payload.get("height") or image.get("height") or 0),
        boxes=boxes,
        negative=negative,
        status=status,
        independence=independence,
    )


def apply_review_decision(payload: MappingLike, decision: str) -> dict[str, Any]:
    normalized = str(decision).strip().lower()
    if normalized not in {"accept", "reject"}:
        raise ValueError("Review decision must be accept or reject")
    result = copy.deepcopy(payload)
    if normalized == "accept":
        result["status"] = REVIEWED_STATUS
        result["independence"] = GROUND_TRUTH_INDEPENDENCE
        regions = result.get("regions")
        if isinstance(regions, list):
            updated: list[Any] = []
            for item in regions:
                if isinstance(item, dict):
                    region = dict(item)
                    region["status"] = REVIEWED_STATUS
                    updated.append(region)
                else:
                    updated.append(item)
            result["regions"] = updated
        return result
    result["status"] = REJECTED_STATUS
    result["independence"] = DETECTOR_DRAFT_INDEPENDENCE
    return result


def _filtered_boxes(
    boxes: Iterable[AnnotationBox],
    *,
    reviewed_only: bool,
) -> tuple[AnnotationBox, ...]:
    if not reviewed_only:
        return tuple(boxes)
    return tuple(box for box in boxes if box.status == REVIEWED_STATUS)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def evaluate_detection_ocr(
    pages: Iterable[tuple[PageAnnotation, Iterable[AnnotationBox]]],
    *,
    iou_threshold: float = 0.5,
    reviewed_only: bool = True,
) -> dict[str, Any]:
    overall = _Counts()
    categories: dict[str, _Counts] = defaultdict(_Counts)
    matched_cer_distances = 0
    matched_cer_chars = 0
    matched_transcriptions = 0
    ground_truth_transcriptions = 0
    negative_pages = 0
    negative_false_positives = 0
    draft_pages = 0
    prediction_categories_present = False
    independence_values: set[str] = set()
    page_summaries: list[dict[str, Any]] = []

    for index, (page, raw_predictions) in enumerate(pages, start=1):
        truth = _filtered_boxes(page.boxes, reviewed_only=reviewed_only)
        independence_values.add(page.independence)
        if reviewed_only and page.status != REVIEWED_STATUS and not truth:
            draft_pages += 1
            continue
        predictions = tuple(raw_predictions)
        prediction_categories_present = prediction_categories_present or any(
            box.categories for box in predictions
        )
        matches = match_boxes(truth, predictions, iou_threshold=iou_threshold)
        matched_truth = {item[0] for item in matches}
        matched_predictions = {item[1] for item in matches}
        true_positives = len(matches)
        false_positives = len(predictions) - true_positives
        false_negatives = len(truth) - true_positives
        overall.true_positives += true_positives
        overall.false_positives += false_positives
        overall.false_negatives += false_negatives

        if page.negative or (not truth and page.status == REVIEWED_STATUS):
            negative_pages += 1
            negative_false_positives += len(predictions)

        for category in {category for box in truth for category in box.categories}:
            category_truth = [box for box in truth if category in box.categories]
            category_matches = [
                match for match in matches if category in truth[match[0]].categories
            ]
            counts = categories[category]
            counts.true_positives += len(category_matches)
            counts.false_negatives += len(category_truth) - len(category_matches)

        unmatched_predictions = [
            predictions[index]
            for index, _box in enumerate(predictions)
            if index not in matched_predictions
        ]
        for box in unmatched_predictions:
            for category in box.categories or ("unlabeled",):
                categories[category].false_positives += 1

        for truth_index, guess_index, _score in matches:
            expected = truth[truth_index].text
            if not normalize_transcription(expected):
                continue
            ground_truth_transcriptions += 1
            matched_transcriptions += 1
            actual = predictions[guess_index].text
            expected_normalized = normalize_transcription(expected)
            matched_cer_distances += levenshtein(
                expected_normalized,
                normalize_transcription(actual),
            )
            matched_cer_chars += max(len(expected_normalized), 1)
        for truth_index, box in enumerate(truth):
            if truth_index in matched_truth:
                continue
            if normalize_transcription(box.text):
                ground_truth_transcriptions += 1

        independence_values.add(page.independence)
        if page.status != REVIEWED_STATUS:
            draft_pages += 1
        page_summaries.append(
            {
                "id": f"page-{index:04d}",
                "negative": bool(page.negative or not truth),
                "groundTruth": len(truth),
                "predictions": len(predictions),
                "truePositives": true_positives,
                "falsePositives": false_positives,
                "falseNegatives": false_negatives,
            }
        )

    independence = GROUND_TRUTH_INDEPENDENCE
    if independence_values == {DETECTOR_DRAFT_INDEPENDENCE}:
        independence = DETECTOR_DRAFT_INDEPENDENCE
    elif independence_values - {GROUND_TRUTH_INDEPENDENCE}:
        independence = "mixed"

    category_report = {}
    for name in (*REQUIRED_CATEGORIES, *sorted(set(categories) - set(REQUIRED_CATEGORIES))):
        if name not in categories:
            continue
        payload = categories[name].as_dict()
        if not prediction_categories_present:
            payload["precision"] = None
            payload["f1"] = None
        category_report[name] = payload
    return {
        "schemaVersion": 1,
        "iouThreshold": iou_threshold,
        "reviewedOnly": reviewed_only,
        "annotationIndependence": independence,
        "pages": len(page_summaries),
        "draftPagesSkipped": draft_pages,
        "detection": overall.as_dict(),
        "categories": category_report,
        "ocr": {
            "matchedTranscriptions": matched_transcriptions,
            "groundTruthTranscriptions": ground_truth_transcriptions,
            "transcriptionCoverage": _ratio(
                matched_transcriptions,
                ground_truth_transcriptions,
            ),
            "cer": (matched_cer_distances / matched_cer_chars if matched_cer_chars else None),
            "normalization": "nfkc-compact",
        },
        "negatives": {
            "pages": negative_pages,
            "falsePositiveRegions": negative_false_positives,
            "meanFalsePositivesPerPage": _ratio(
                negative_false_positives,
                negative_pages,
            ),
        },
        "confidence": {
            "usedToDropPredictions": False,
            "detectorAndOcrSeparated": True,
        },
        "pageSummaries": page_summaries,
    }


def sanitize_report(report: MappingLike) -> dict[str, Any]:
    """Return aggregate metrics with no transcriptions, paths, checksums, or filenames."""
    return {
        "schemaVersion": int(report.get("schemaVersion") or 1),
        "privacy": {
            "ocrTextStored": False,
            "absolutePathsStored": False,
            "imageNamesStored": False,
            "checksumsStored": False,
        },
        "iouThreshold": report.get("iouThreshold"),
        "reviewedOnly": report.get("reviewedOnly"),
        "annotationIndependence": report.get("annotationIndependence"),
        "pages": report.get("pages"),
        "draftPagesSkipped": report.get("draftPagesSkipped"),
        "detection": report.get("detection"),
        "categories": report.get("categories"),
        "ocr": report.get("ocr"),
        "negatives": report.get("negatives"),
        "confidence": report.get("confidence"),
        "pageSummaries": [
            {
                "id": page.get("id"),
                "negative": page.get("negative"),
                "groundTruth": page.get("groundTruth"),
                "predictions": page.get("predictions"),
                "truePositives": page.get("truePositives"),
                "falsePositives": page.get("falsePositives"),
                "falseNegatives": page.get("falseNegatives"),
            }
            for page in report.get("pageSummaries") or []
            if isinstance(page, dict)
        ],
    }
