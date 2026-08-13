from __future__ import annotations

from copy import deepcopy
from typing import Any

from manga_localizer.database import TextRegion

RECOGNITION_VERSION = 1
TRUST_POLICY_VERSION = 1
TRUST_DISPOSITIONS = {"review", "trusted", "ignored"}
TRUST_REASONS = {
    "automatic-ocr-complete",
    "automatic-proposal",
    "human-confirmed",
    "human-ignored",
    "legacy-confirmed",
    "legacy-unverified",
    "manual-unconfirmed",
    "policy-version-changed",
    "trust-input-changed",
}
TRUST_REASON_PAIRS = {
    "review": {
        "automatic-ocr-complete",
        "automatic-proposal",
        "legacy-unverified",
        "manual-unconfirmed",
        "policy-version-changed",
        "trust-input-changed",
    },
    "trusted": {"human-confirmed", "legacy-confirmed"},
    "ignored": {"human-ignored"},
}


def _trust(disposition: str, reason: str) -> dict[str, Any]:
    return {
        "policyVersion": TRUST_POLICY_VERSION,
        "disposition": disposition,
        "reason": reason,
    }


def _valid_trust_pair(disposition: Any, reason: Any) -> bool:
    return (
        disposition in TRUST_DISPOSITIONS
        and reason in TRUST_REASONS
        and reason in TRUST_REASON_PAIRS[disposition]
    )


def _finite_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if 0 <= normalized <= 1 else None


def _normalize(recognition: Any) -> dict[str, Any] | None:
    if not isinstance(recognition, dict) or recognition.get("version") != RECOGNITION_VERSION:
        return None
    detection = recognition.get("detection")
    if isinstance(detection, dict):
        detection = {
            "confidence": _finite_confidence(detection.get("confidence")),
            "provider": detection.get("provider")
            if isinstance(detection.get("provider"), str)
            else None,
            "inputVariant": detection.get("inputVariant")
            if isinstance(detection.get("inputVariant"), str)
            else None,
            "language": detection.get("language")
            if isinstance(detection.get("language"), str)
            else None,
        }
    else:
        detection = None
    ocr = recognition.get("ocr")
    if isinstance(ocr, dict):
        attempt_count = ocr.get("attemptCount", 0)
        attempts: list[dict[str, Any]] = []
        for attempt in ocr.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            attempts.append(
                {
                    "provider": attempt.get("provider")
                    if isinstance(attempt.get("provider"), str)
                    else None,
                    "inputVariant": attempt.get("inputVariant")
                    if isinstance(attempt.get("inputVariant"), str)
                    else None,
                    "confidence": _finite_confidence(attempt.get("confidence")),
                    "direction": attempt.get("direction")
                    if attempt.get("direction") in {"horizontal", "vertical", "auto"}
                    else None,
                    "language": attempt.get("language")
                    if isinstance(attempt.get("language"), str)
                    else None,
                }
            )
        selected_index = ocr.get("selectedIndex")
        if type(selected_index) is not int or not 0 <= selected_index < len(attempts):
            selected_index = None
        normalized_attempt_count = (
            len(attempts)
            if attempts
            else attempt_count
            if type(attempt_count) is int and attempt_count >= 0
            else 0
        )
        ocr = {
            "confidence": _finite_confidence(ocr.get("confidence")),
            "provider": ocr.get("provider") if isinstance(ocr.get("provider"), str) else None,
            "attemptCount": normalized_attempt_count,
            "inputVariant": ocr.get("inputVariant")
            if isinstance(ocr.get("inputVariant"), str)
            else None,
            "direction": ocr.get("direction")
            if ocr.get("direction") in {"horizontal", "vertical", "auto"}
            else None,
            "language": ocr.get("language") if isinstance(ocr.get("language"), str) else None,
            "attempts": attempts,
            "selectedIndex": selected_index,
        }
        if selected_index is not None:
            ocr.update(attempts[selected_index])
    else:
        ocr = None
    trust = recognition.get("trust")
    disposition = trust.get("disposition") if isinstance(trust, dict) else None
    reason = trust.get("reason") if isinstance(trust, dict) else None
    if (
        not isinstance(trust, dict)
        or trust.get("policyVersion") != TRUST_POLICY_VERSION
        or not _valid_trust_pair(disposition, reason)
    ):
        # A trust-policy change must fail closed without destroying still-readable
        # detector/OCR evidence. Unknown recognition schema versions are rejected
        # above because their evidence shape cannot be interpreted safely.
        disposition = "review"
        reason = "policy-version-changed"
    return {
        "version": RECOGNITION_VERSION,
        "detection": detection,
        "ocr": ocr,
        "trust": _trust(disposition, reason),
    }


def recognition_policy_is_current(recognition: Any, *, ignored: bool | None = None) -> bool:
    normalized = _normalize(recognition)
    if normalized is None or normalized != recognition:
        return False
    trust = normalized["trust"]
    if ignored is None:
        return True
    disposition = trust.get("disposition")
    return disposition == "ignored" if ignored else disposition != "ignored"


def recognition_has_detection_evidence(recognition: Any) -> bool:
    normalized = _normalize(recognition)
    return normalized is not None and normalized["detection"] is not None


def recognition_has_ocr_evidence(recognition: Any) -> bool:
    normalized = _normalize(recognition)
    return normalized is not None and normalized["ocr"] is not None


def recognition_uses_input_variant(recognition: Any, input_variant: str) -> bool:
    normalized = _normalize(recognition)
    if normalized is None:
        return False
    detection = normalized["detection"]
    if detection is not None and detection.get("inputVariant") == input_variant:
        return True
    ocr = normalized["ocr"]
    if ocr is None:
        return False
    if ocr.get("inputVariant") == input_variant:
        return True
    return any(attempt.get("inputVariant") == input_variant for attempt in ocr.get("attempts", []))


def recognition_payload(region: TextRegion) -> dict[str, Any]:
    normalized = _normalize(region.recognition)
    if normalized is not None:
        # The explicit ignored flag is authoritative and always fails closed.
        if region.ignored and normalized["trust"]["disposition"] != "ignored":
            normalized["trust"] = _trust("ignored", "human-ignored")
        elif not region.ignored and normalized["trust"]["disposition"] == "ignored":
            normalized["trust"] = _trust("review", "trust-input-changed")
        return normalized
    if region.ignored:
        trust = _trust("ignored", "human-ignored")
    elif isinstance(region.recognition, dict) and region.recognition:
        trust = _trust("review", "policy-version-changed")
    elif region.confirmed:
        # ``confirmed`` has always been set only by an explicit editor action.
        trust = _trust("trusted", "legacy-confirmed")
    else:
        trust = _trust("review", "legacy-unverified")
    return {
        "version": RECOGNITION_VERSION,
        "detection": None,
        "ocr": None,
        "trust": trust,
    }


def region_trust(region: TextRegion) -> dict[str, Any]:
    return deepcopy(recognition_payload(region)["trust"])


def region_disposition(region: TextRegion) -> str:
    return str(region_trust(region)["disposition"])


def is_region_trusted(region: TextRegion) -> bool:
    return not region.ignored and region_disposition(region) == "trusted"


def persist_legacy_recognition(region: TextRegion) -> dict[str, Any]:
    """Materialize fail-closed legacy inference before downstream state can change."""
    value = recognition_payload(region)
    region.recognition = value
    return value


def _base(recognition: Any, *, default_reason: str = "legacy-unverified") -> dict[str, Any]:
    normalized = _normalize(recognition)
    if normalized is not None:
        return normalized
    return {
        "version": RECOGNITION_VERSION,
        "detection": None,
        "ocr": None,
        "trust": _trust("review", default_reason),
    }


def manual_recognition(*, confirmed: bool = False, ignored: bool = False) -> dict[str, Any]:
    if ignored:
        trust = _trust("ignored", "human-ignored")
    elif confirmed:
        trust = _trust("trusted", "human-confirmed")
    else:
        trust = _trust("review", "manual-unconfirmed")
    return {"version": RECOGNITION_VERSION, "detection": None, "ocr": None, "trust": trust}


def with_detection_evidence(
    recognition: Any,
    confidence: float | None,
    provider: str | None,
    *,
    input_variant: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    value = _base(recognition, default_reason="automatic-proposal")
    value["detection"] = {
        "confidence": _finite_confidence(confidence),
        "provider": provider,
        "inputVariant": input_variant,
        "language": language,
    }
    value["trust"] = _trust("review", "automatic-proposal")
    return value


def with_ocr_evidence(
    recognition: Any,
    confidence: float | None,
    provider: str | None,
    *,
    attempt_count: int = 0,
    input_variant: str | None = None,
    direction: str | None = None,
    attempts: list[dict[str, Any]] | None = None,
    selected_index: int | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    value = _base(recognition)
    previous_ocr = value.get("ocr")
    previous_attempts = (
        [dict(attempt) for attempt in previous_ocr.get("attempts", [])]
        if isinstance(previous_ocr, dict)
        else []
    )
    previous_attempt_count = (
        previous_ocr.get("attemptCount", len(previous_attempts))
        if isinstance(previous_ocr, dict)
        else 0
    )
    if type(previous_attempt_count) is not int or previous_attempt_count < len(previous_attempts):
        previous_attempt_count = len(previous_attempts)
    missing_previous_attempts = previous_attempt_count - len(previous_attempts)
    previous_attempts.extend(
        {
            "provider": None,
            "inputVariant": None,
            "confidence": None,
            "direction": None,
            "language": None,
        }
        for _ in range(missing_previous_attempts)
    )
    normalized_attempts: list[dict[str, Any]] = []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        normalized_attempts.append(
            {
                "provider": attempt.get("provider")
                if isinstance(attempt.get("provider"), str)
                else None,
                "inputVariant": attempt.get("inputVariant")
                if isinstance(attempt.get("inputVariant"), str)
                else None,
                "confidence": _finite_confidence(attempt.get("confidence")),
                "direction": attempt.get("direction")
                if attempt.get("direction") in {"horizontal", "vertical", "auto"}
                else None,
                "language": attempt.get("language")
                if isinstance(attempt.get("language"), str)
                else None,
            }
        )
    if attempts is None:
        normalized_attempts.extend(
            {
                "provider": None,
                "inputVariant": None,
                "confidence": None,
                "direction": None,
                "language": None,
            }
            for _ in range(max(0, int(attempt_count)))
        )
    if type(selected_index) is not int or not 0 <= selected_index < len(normalized_attempts):
        combined_selected_index = None
    else:
        combined_selected_index = len(previous_attempts) + selected_index
    combined_attempts = previous_attempts + normalized_attempts
    selected = None
    if combined_selected_index is not None:
        selected = combined_attempts[combined_selected_index]
    value["ocr"] = {
        "confidence": _finite_confidence(confidence),
        "provider": provider,
        "attemptCount": len(combined_attempts),
        "inputVariant": input_variant,
        "direction": direction if direction in {"horizontal", "vertical", "auto"} else None,
        "language": language,
        "attempts": combined_attempts,
        "selectedIndex": combined_selected_index,
    }
    if selected is not None:
        value["ocr"].update(selected)
    value["trust"] = _trust("review", "automatic-ocr-complete")
    return value


def with_human_confirmation(recognition: Any) -> dict[str, Any]:
    value = _base(recognition)
    value["trust"] = _trust("trusted", "human-confirmed")
    return value


def with_human_ignore(recognition: Any) -> dict[str, Any]:
    value = _base(recognition)
    value["trust"] = _trust("ignored", "human-ignored")
    return value


def with_human_unignore(recognition: Any) -> dict[str, Any]:
    value = _base(recognition)
    value["trust"] = _trust("review", "trust-input-changed")
    return value


def invalidate_trust(recognition: Any, reason: str = "trust-input-changed") -> dict[str, Any]:
    if reason not in TRUST_REASONS:
        raise ValueError(f"Unknown trust reason: {reason}")
    value = _base(recognition)
    value["trust"] = _trust("review", reason)
    return value
