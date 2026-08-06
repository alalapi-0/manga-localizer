"""Fail a release candidate containing common secrets or prohibited artifacts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 10 * 1024 * 1024
PROHIBITED_SUFFIXES = {
    ".ckpt",
    ".db",
    ".eot",
    ".gguf",
    ".h5",
    ".hdf5",
    ".joblib",
    ".key",
    ".onnx",
    ".otf",
    ".p12",
    ".pem",
    ".pkl",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
    ".tflite",
    ".traineddata",
    ".ttc",
    ".ttf",
    ".woff",
    ".woff2",
}
PROHIBITED_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_ed25519",
    "id_rsa",
}
PROHIBITED_RUNTIME_PARTS = {
    "exports",
    "generated",
    "masks",
    "original-text",
    "project",
    "source",
    "translated",
    "translated-text",
}

# Build path sentinels without embedding a real-looking personal path in this repository.
PERSONAL_UNIX_PATH = re.compile(re.escape("/" + "Users" + "/") + r"[^/\s]+/")
PERSONAL_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s]+\\")
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "GitLab token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Stripe live key": re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    "npm auth token": re.compile(
        r"(?im)^\s*(?://[^\s]+/:)?_authToken\s*=\s*[^${\s][^\s]*"
    ),
    "AWS access key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
}
TEXT_SUFFIXES = {
    "",
    ".css",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def historical_blobs() -> list[tuple[str, PurePosixPath, int, str | None]]:
    """Return each uniquely named historical blob without printing its contents."""

    result = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    objects: dict[str, str] = {}
    for line in result.stdout.splitlines():
        object_id, separator, object_path = line.partition(" ")
        if separator and object_path:
            objects.setdefault(object_id, object_path)

    blobs: list[tuple[str, PurePosixPath, int, str | None]] = []
    for object_id, object_path in objects.items():
        metadata = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objecttype) %(objectsize)"],
            cwd=ROOT,
            check=True,
            input=f"{object_id}\n",
            capture_output=True,
            text=True,
        ).stdout.strip()
        object_type, size_text = metadata.split(" ", 1)
        if object_type != "blob":
            continue
        size = int(size_text)
        relative = PurePosixPath(object_path)
        text: str | None = None
        if relative.suffix.lower() in TEXT_SUFFIXES and size <= 2 * 1024 * 1024:
            content = subprocess.run(
                ["git", "cat-file", "blob", object_id],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            text = content.decode("utf-8", errors="ignore")
        blobs.append((f"history:{object_path}@{object_id[:12]}", relative, size, text))
    return blobs


def inspect_entry(
    display_path: str,
    relative: Path | PurePosixPath,
    size: int,
    text: str | None,
) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if any(part in PROHIBITED_RUNTIME_PARTS for part in relative.parts[:-1]):
        findings.append((display_path, "portable project, source, or output artifact"))
    if relative.name in PROHIBITED_NAMES or (
        relative.name.startswith(".env.") and relative.name != ".env.example"
    ):
        findings.append((display_path, "prohibited filename"))
    if relative.suffix.lower() in PROHIBITED_SUFFIXES:
        findings.append((display_path, "database, model weight, or font artifact"))
    if size > MAX_BYTES:
        findings.append((display_path, "file exceeds 10 MiB"))
    if text is None:
        return findings
    if PERSONAL_UNIX_PATH.search(text) or PERSONAL_WINDOWS_PATH.search(text):
        findings.append((display_path, "personal absolute path"))
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append((display_path, label))
    return findings


def main() -> int:
    candidates = candidate_files()
    history = historical_blobs()
    findings: list[tuple[str, str]] = []
    for path in candidates:
        relative = path.relative_to(ROOT)
        if not path.is_file():
            continue
        size = path.stat().st_size
        text = None
        if path.suffix.lower() in TEXT_SUFFIXES and size <= 2 * 1024 * 1024:
            text = path.read_text(encoding="utf-8", errors="ignore")
        findings.extend(inspect_entry(str(relative), relative, size, text))
    for display_path, relative, size, text in history:
        findings.extend(inspect_entry(display_path, relative, size, text))

    if findings:
        print("Release audit failed:")
        for path, reason in sorted(set(findings)):
            print(f"- {path}: {reason}")
        return 1
    print(
        f"Release audit passed ({len(candidates)} candidate files and "
        f"{len(history)} historical blobs scanned)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
