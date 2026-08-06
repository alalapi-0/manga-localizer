from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized.endswith("token") or any(
        marker in normalized
        for marker in (
            "apikey",
            "secret",
            "password",
            "credential",
            "authorization",
            "privatekey",
        )
    )


def without_secrets(value: Any) -> Any:
    """Drop credential-bearing mapping entries before persistence or API serialization."""
    if isinstance(value, Mapping):
        return {
            key: without_secrets(item) for key, item in value.items() if not is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [without_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(without_secrets(item) for item in value)
    return value


def redact(value: Any) -> Any:
    """Recursively redact common credential shapes without logging their values."""
    if isinstance(value, Mapping):
        return {
            key: "***" if is_sensitive_key(key) else redact(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact(item) for item in value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = value if isinstance(value, str) else str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(arg) for arg in record.args)
        return True


def configure_logging(level: str = "info") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
