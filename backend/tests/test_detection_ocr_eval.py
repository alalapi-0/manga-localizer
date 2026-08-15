from __future__ import annotations

from pathlib import Path

from PIL import Image

from manga_localizer.evaluation.detection_ocr import (
    REQUIRED_CATEGORIES,
    AnnotationBox,
    PageAnnotation,
    character_error_rate,
    evaluate_detection_ocr,
    iou,
    match_boxes,
    sanitize_report,
)
from manga_localizer.evaluation.synthetic import STRESS_PAGE_SPECS, generate_detection_stress_pages
from manga_localizer.providers.detection import (
    TextDetectionUnavailable,
    UnionTextDetectionProvider,
)
from manga_localizer.providers.ocr import OCRRegion


def _box(**overrides: object) -> AnnotationBox:
    payload = {
        "x": 10,
        "y": 10,
        "width": 40,
        "height": 20,
        "text": "こんにちは",
        "categories": ("bubble", "horizontal"),
    }
    payload.update(overrides)
    return AnnotationBox(**payload)  # type: ignore[arg-type]


def test_iou_and_greedy_matching_are_one_to_one() -> None:
    truth = [_box(x=0, y=0, width=10, height=10), _box(x=50, y=0, width=10, height=10)]
    guesses = [
        _box(x=1, y=1, width=10, height=10, detector_confidence=0.05),
        _box(x=1, y=1, width=9, height=9, detector_confidence=0.99),
        _box(x=80, y=80, width=10, height=10),
    ]
    matches = match_boxes(truth, guesses, iou_threshold=0.5)
    assert len(matches) == 1
    assert matches[0][0] == 0
    assert iou(truth[0], guesses[0]) >= 0.5


def test_evaluation_keeps_low_confidence_predictions_and_separates_ocr() -> None:
    page = PageAnnotation(
        page_id="bubble",
        width=100,
        height=100,
        boxes=(_box(),),
        independence="ground-truth",
    )
    predictions = [
        _box(detector_confidence=0.01, ocr_confidence=0.2, text="こんにちは"),
        _box(x=80, y=80, width=12, height=12, text="", detector_confidence=0.99),
    ]
    report = evaluate_detection_ocr([(page, predictions)])
    assert report["confidence"]["usedToDropPredictions"] is False
    assert report["detection"]["truePositives"] == 1
    assert report["detection"]["falsePositives"] == 1
    assert report["ocr"]["cer"] == 0.0
    assert report["categories"]["bubble"]["recall"] == 1.0


def test_character_error_rate_ignores_cjk_whitespace() -> None:
    assert character_error_rate("こん にちは", "こんにちは") == 0.0
    assert character_error_rate("あ", "い") == 1.0


def test_negative_page_false_positives_and_sanitized_privacy() -> None:
    page = PageAnnotation(
        page_id="secret-filename",
        width=64,
        height=64,
        boxes=(),
        negative=True,
        independence="ground-truth",
    )
    report = evaluate_detection_ocr([(page, [_box(text="must-not-leak", detector_confidence=0.9)])])
    sanitized = sanitize_report(report)
    encoded = str(sanitized)
    assert "must-not-leak" not in encoded
    assert "secret-filename" not in encoded
    assert sanitized["privacy"] == {
        "ocrTextStored": False,
        "absolutePathsStored": False,
        "imageNamesStored": False,
        "checksumsStored": False,
    }
    assert sanitized["pageSummaries"][0]["id"] == "page-0001"
    assert sanitized["negatives"]["pages"] == 1
    assert sanitized["negatives"]["falsePositiveRegions"] == 1


def test_synthetic_stress_set_covers_required_categories() -> None:
    pages = generate_detection_stress_pages()
    assert [page.spec for page in pages] == list(STRESS_PAGE_SPECS)
    categories = {
        category for page in pages for box in page.annotation.boxes for category in box.categories
    }
    categories.add("negative")
    assert set(REQUIRED_CATEGORIES) <= categories
    assert any(page.annotation.negative for page in pages)
    assert all(page.annotation.independence == "ground-truth" for page in pages)
    assert all(isinstance(page.image, Image.Image) for page in pages)


def test_reviewed_only_skips_detector_drafts() -> None:
    draft = PageAnnotation(
        page_id="draft",
        width=40,
        height=40,
        boxes=(_box(status="draft"),),
        status="draft",
        independence="detector-draft",
    )
    report = evaluate_detection_ocr([(draft, [_box()])], reviewed_only=True)
    assert report["pages"] == 0
    assert report["draftPagesSkipped"] == 1
    assert report["annotationIndependence"] == "detector-draft"


class _FakeDetector:
    def __init__(
        self,
        regions: list[OCRRegion],
        *,
        available: bool = True,
        error: str | None = None,
    ):
        self.regions = regions
        self._available = available
        self._error = error

    def detect_text_regions(self, image, **_options) -> list[OCRRegion]:
        del image
        return list(self.regions)

    def health_check(self) -> dict[str, object]:
        return {"available": self._available, "error": self._error}


def test_union_detector_keeps_overlapping_and_low_confidence_proposals() -> None:
    low = OCRRegion(1, 1, 20, 20, "", 0.02, "horizontal")
    overlap = OCRRegion(2, 2, 20, 20, "", 0.99, "horizontal")
    extra = OCRRegion(80, 80, 12, 12, "", 0.4, "vertical")
    provider = UnionTextDetectionProvider(
        _FakeDetector([low, overlap]),
        _FakeDetector([extra]),
    )
    result = provider.detect_text_regions(Image.new("RGB", (100, 100), "white"))
    assert result == [low, overlap, extra]
    capabilities = provider.get_capabilities()
    assert capabilities["keepsAllProposals"] is True
    assert capabilities["mergesOverlaps"] is False
    assert capabilities["dropsLowConfidence"] is False
    assert capabilities["tesseractContourFallback"] is False


def test_union_detector_is_unavailable_unless_both_members_work() -> None:
    provider = UnionTextDetectionProvider(
        _FakeDetector([], available=False, error="model missing"),
        _FakeDetector([], available=True),
    )
    assert provider.health_check()["available"] is False
    try:
        provider.detect_text_regions(Image.new("RGB", (8, 8), "white"))
    except TextDetectionUnavailable as error:
        assert "model missing" in str(error)
    else:
        raise AssertionError("union detector must fail closed when a member is unavailable")


def test_write_stress_set_round_trips(tmp_path: Path) -> None:
    from manga_localizer.evaluation.synthetic import write_detection_stress_set

    written = write_detection_stress_set(tmp_path)
    assert (tmp_path / "manifest.json").is_file()
    assert any(path.suffix == ".png" for path in written)
    assert any(path.name.endswith("negative-lineart.json") for path in written)
