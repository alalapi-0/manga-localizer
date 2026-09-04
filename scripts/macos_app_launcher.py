#!/usr/bin/env python3
"""Supervise the local workbench API from a macOS application bundle."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DARWIN_APP_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


def is_private_lan_ipv4(host: str) -> bool:
    parts = (
        [int(part) for part in host.split(".")]
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host)
        else []
    )
    if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
        return False
    first, second = parts[0], parts[1]
    if first == 10:
        return True
    if first == 192 and second == 168:
        return True
    return first == 172 and 16 <= second <= 31


def first_private_lan_ipv4() -> str | None:
    try:
        text = subprocess.check_output(["ifconfig"], text=True)
    except OSError:
        return None
    for match in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)", text):
        host = match.group(1)
        if is_private_lan_ipv4(host):
            return host
    return None


def application_bind_host(*, lan_access: bool, requested_host: str) -> str:
    host = requested_host.strip().strip("[]") or "127.0.0.1"
    if not lan_access:
        if host not in {"127.0.0.1", "localhost", "::1"} and not host.startswith(
            "127."
        ):
            raise SystemExit(
                "Manga Localizer application services must bind to a loopback host"
            )
        return "127.0.0.1" if host in {"localhost", "::1"} else host
    if is_private_lan_ipv4(host):
        return host
    lan_address = first_private_lan_ipv4()
    if lan_address:
        return lan_address
    raise SystemExit(
        "No private LAN IPv4 address is available for phone companion access"
    )


def resources_root() -> Path:
    configured = os.environ.get("MANGA_LOCALIZER_RESOURCES")
    if configured:
        return Path(configured).expanduser()
    here = Path(__file__).resolve().parent
    if (here / "frontend").is_dir() or (here / "models").is_dir():
        return here
    return here


def application_data_dir(env: dict[str, str]) -> Path:
    configured = env.get("MANGA_LOCALIZER_DATA_DIR", "").strip()
    if not configured:
        raise SystemExit(
            "MANGA_LOCALIZER_DATA_DIR must be injected by the guarded application wrapper"
        )
    candidate = Path(configured)
    if not candidate.is_absolute():
        raise SystemExit("MANGA_LOCALIZER_DATA_DIR must be an absolute path")
    if candidate.is_symlink() or not candidate.is_dir():
        raise SystemExit("MANGA_LOCALIZER_DATA_DIR must be a real directory")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SystemExit("MANGA_LOCALIZER_DATA_DIR cannot be canonicalized") from error
    if resolved != candidate:
        raise SystemExit("MANGA_LOCALIZER_DATA_DIR must be canonical")
    return candidate


def window_launch(
    url: str,
    *,
    window_helper: Path | None = None,
    path_exists=Path.exists,
) -> tuple[list[str], str]:
    if window_helper is not None and path_exists(window_helper):
        return [str(window_helper), url], "app-window"
    for candidate in DARWIN_APP_BROWSERS:
        if path_exists(Path(candidate)):
            return [candidate, f"--app={url}"], "app-window"
    return ["open", url], "browser-tab"


def wait_for_health(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error = "not started"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise SystemExit(f"Manga Localizer API did not become ready: {last_error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the bundled Manga Localizer workbench"
    )
    parser.add_argument(
        "--lan",
        action="store_true",
        help="bind one private LAN IPv4 for phone companion access",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="start the API only; used by packaging smoke checks",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resources = resources_root()
    env = os.environ.copy()
    lan_access = args.lan or env.get("MANGA_LOCALIZER_LAN_ACCESS") in {"1", "true"}
    host = application_bind_host(
        lan_access=lan_access,
        requested_host=env.get("MANGA_LOCALIZER_HOST", "127.0.0.1"),
    )
    port = env.get("MANGA_LOCALIZER_PORT", "8000")
    env["MANGA_LOCALIZER_HOST"] = host
    env["MANGA_LOCALIZER_PORT"] = port
    env["MANGA_LOCALIZER_LAN_ACCESS"] = "1" if lan_access else "0"
    env.setdefault("MANGA_LOCALIZER_FRONTEND_DIST", str(resources / "frontend"))
    env.setdefault("MANGA_LOCALIZER_MODEL_BUNDLE", str(resources / "models"))
    env["MANGA_LOCALIZER_DATA_DIR"] = str(application_data_dir(env))
    app_url = f"http://{host}:{port}"
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "manga_localizer.main:app",
            "--host",
            host,
            "--port",
            port,
        ],
        env=env,
    )
    window: subprocess.Popen[bytes] | None = None
    try:
        wait_for_health(f"{app_url}/api/health")
        if args.no_window:
            return 0
        helper = env.get("MANGA_LOCALIZER_WINDOW_HELPER")
        command, kind = window_launch(
            app_url,
            window_helper=Path(helper) if helper else None,
        )
        window = subprocess.Popen(command, env=env)
        if lan_access:
            print(f"Phone companion enabled. On the same Wi-Fi, open {app_url}")
        print(
            f"Opened Manga Localizer as a Mac application window at {app_url}"
            if kind == "app-window"
            else f"Opened Manga Localizer at {app_url}"
        )
        return window.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        for child in (window, api):
            if child is not None and child.poll() is None:
                child.terminate()
        for child in (window, api):
            if child is not None and child.poll() is None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()


if __name__ == "__main__":
    raise SystemExit(main())
