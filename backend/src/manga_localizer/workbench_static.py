"""Serve the built workbench UI from the local API origin when a dist path is configured."""

from __future__ import annotations

from pathlib import Path

from manga_localizer.config import Settings


def resolve_frontend_dist(settings: Settings | None = None) -> Path | None:
    if settings is None or settings.frontend_dist is None:
        return None
    resolved = settings.frontend_dist.expanduser().resolve()
    if (resolved / "index.html").is_file():
        return resolved
    return None
