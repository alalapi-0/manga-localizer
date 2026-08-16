"""Serve the built workbench UI from the local API origin when a dist path is configured."""

from __future__ import annotations

from pathlib import Path

from manga_localizer.config import Settings
from manga_localizer.security import is_private_lan_host


def resolve_frontend_dist(settings: Settings | None = None) -> Path | None:
    if settings is None or settings.frontend_dist is None:
        return None
    resolved = settings.frontend_dist.expanduser().resolve()
    if (resolved / "index.html").is_file():
        return resolved
    return None


def workbench_url(settings: Settings) -> str:
    return f"http://{settings.host}:{settings.port}"


def companion_url_for(settings: Settings) -> str | None:
    if settings.lan_access and is_private_lan_host(settings.host):
        return workbench_url(settings)
    return None


def cors_origins_for(settings: Settings) -> list[str]:
    origins = list(settings.cors_origins)
    bound = workbench_url(settings)
    if bound not in origins:
        origins.append(bound)
    return origins
