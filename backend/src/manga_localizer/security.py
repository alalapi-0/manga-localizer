from __future__ import annotations

import os
import re
import shutil
import unicodedata
import uuid
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse


class UnsafePathError(ValueError):
    pass


class UnsafeRemoteEndpointError(ValueError):
    pass


_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_WINDOWS_INVALID = set('<>:"|?*')


def _validate_portable_component(component: str) -> None:
    if component.endswith((" ", ".")):
        raise UnsafePathError("Path components must not end with a space or dot")
    if any(ord(character) < 32 or character in _WINDOWS_INVALID for character in component):
        raise UnsafePathError("Path contains characters unsupported on Windows")
    basename = component.split(".", 1)[0].upper()
    if basename in _WINDOWS_RESERVED:
        raise UnsafePathError("Path contains a Windows-reserved name")


def portable_path_key(relative: str | Path) -> str:
    """Return a cross-platform collision key for an already safe relative path."""
    raw = relative.as_posix() if isinstance(relative, Path) else relative
    safe = safe_relative_path(raw)
    return "/".join(unicodedata.normalize("NFKC", component).casefold() for component in safe.parts)


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_remote_base_url(value: str) -> str:
    """Validate a persisted/session remote endpoint without ever echoing its value."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnsafeRemoteEndpointError("Remote base URL must be a non-empty HTTP(S) URL")
    if any(ord(character) < 32 for character in value):
        raise UnsafeRemoteEndpointError("Remote base URL contains control characters")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
    except ValueError as error:
        raise UnsafeRemoteEndpointError("Remote base URL is malformed") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeRemoteEndpointError(
            "Remote base URL must not contain credentials, a query, or a fragment"
        )
    if parsed.scheme == "http" and not is_loopback_host(hostname):
        raise UnsafeRemoteEndpointError(
            "Plain HTTP is allowed only for an explicitly configured loopback service"
        )
    return value.rstrip("/")


def normalize_remote_endpoints(value: Any, *, drop_invalid: bool = False) -> Any:
    """Normalize endpoint fields recursively, or remove unsafe fields from legacy records."""
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in {"baseurl", "remoteendpoint"}:
                if item is None or item == "":
                    result[key] = item
                    continue
                try:
                    result[key] = validate_remote_base_url(item)
                except (TypeError, UnsafeRemoteEndpointError):
                    if not drop_invalid:
                        raise UnsafeRemoteEndpointError(
                            "Remote endpoint settings contain an unsafe URL"
                        ) from None
                continue
            result[key] = normalize_remote_endpoints(item, drop_invalid=drop_invalid)
        return result
    if isinstance(value, list):
        return [normalize_remote_endpoints(item, drop_invalid=drop_invalid) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_remote_endpoints(item, drop_invalid=drop_invalid) for item in value)
    return value


def safe_relative_path(raw: str) -> Path:
    """Validate an untrusted browser relative path without normalizing away attacks."""
    if not raw or "\x00" in raw:
        raise UnsafePathError("Relative path is empty or contains NUL")
    candidate = raw.replace("\\", "/")
    if (
        candidate.startswith(("/", "//"))
        or _DRIVE_RE.match(candidate)
        or PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(raw).is_absolute()
    ):
        raise UnsafePathError("Absolute, drive, and UNC paths are not allowed")
    if "//" in candidate or candidate.endswith("/"):
        raise UnsafePathError("Path must not contain empty components")
    parts = PurePosixPath(candidate).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError("Path traversal is not allowed")
    for part in parts:
        _validate_portable_component(part)
    return Path(*parts)


def resolve_within(root: Path, relative: str | Path, *, allow_missing: bool = True) -> Path:
    root = root.expanduser().resolve()
    rel = safe_relative_path(relative.as_posix() if isinstance(relative, Path) else relative)
    target = root.joinpath(rel)
    resolved = target.resolve(strict=not allow_missing)
    if not resolved.is_relative_to(root):
        raise UnsafePathError("Path escapes the allowed root")
    # Existing symlink parents can escape even when the leaf does not exist.
    parent = target.parent
    while parent != root and parent.exists():
        if parent.is_symlink() and not parent.resolve().is_relative_to(root):
            raise UnsafePathError("Symlink escapes the allowed root")
        parent = parent.parent
    return resolved


def resolve_write_target(
    root: Path,
    relative: str | Path,
    *,
    protected_roots: tuple[Path, ...] = (),
) -> Path:
    """Resolve a new/replace target without following any existing symlink component."""
    root = root.expanduser().resolve()
    rel = safe_relative_path(relative.as_posix() if isinstance(relative, Path) else relative)
    entry = root
    for part in rel.parts:
        entry = entry / part
        if entry.is_symlink():
            raise UnsafePathError("Write targets must not contain symlinks")
        if not entry.exists():
            break
    target = resolve_within(root, rel)
    for protected in protected_roots:
        boundary = protected.expanduser().resolve()
        if target == boundary or target.is_relative_to(boundary):
            raise UnsafePathError("Write target overlaps immutable source storage")
    return target


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_copy_file(source: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as source_handle, temp.open("xb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def cleanup_stale_atomic_temps(path: Path) -> None:
    pattern = re.compile(rf"^\.{re.escape(path.name)}\.(?:[0-9]+|[0-9a-f]{{32}})\.tmp$")
    if not path.parent.is_dir():
        return
    for candidate in path.parent.iterdir():
        if pattern.fullmatch(candidate.name) and candidate.is_file() and not candidate.is_symlink():
            candidate.unlink(missing_ok=True)
