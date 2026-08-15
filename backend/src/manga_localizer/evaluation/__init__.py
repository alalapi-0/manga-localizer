from manga_localizer.evaluation.detection_ocr import (
    REQUIRED_CATEGORIES,
    AnnotationBox,
    PageAnnotation,
    character_error_rate,
    evaluate_detection_ocr,
    iou,
    load_annotation_document,
    match_boxes,
    sanitize_report,
)
from manga_localizer.evaluation.synthetic import (
    STRESS_PAGE_SPECS,
    generate_detection_stress_pages,
    write_detection_stress_set,
)

__all__ = [
    "REQUIRED_CATEGORIES",
    "STRESS_PAGE_SPECS",
    "AnnotationBox",
    "PageAnnotation",
    "character_error_rate",
    "evaluate_detection_ocr",
    "generate_detection_stress_pages",
    "iou",
    "load_annotation_document",
    "match_boxes",
    "sanitize_report",
    "write_detection_stress_set",
]
