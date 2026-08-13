"""Fail a release candidate containing common secrets or prohibited artifacts."""

from __future__ import annotations

import hashlib
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 10 * 1024 * 1024
RASTER_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
ALLOWED_RASTER = {
    PurePosixPath("docs/assets/workbench.jpg"): frozenset(
        {
            (
                97_292,
                "8e7c839df07430145d5100d80e60f9d28a60137aadc14caed7c8f980090d124d",
            )
        }
    ),
}
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

# Build path sentinels without embedding a real-looking personal path in this
# repository.
PERSONAL_PATHS = (
    re.compile(re.escape("/" + "Users" + "/") + r"[^/\s]+/"),
    re.compile(re.escape("/" + "home" + "/") + r"[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s]+\\"),
    re.compile(r"[A-Za-z]:/" + "Users" + r"/[^/\s]+/"),
)
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
    return [
        ROOT / item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def tracked_files() -> set[PurePosixPath]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    return {
        PurePosixPath(item.decode("utf-8", errors="surrogateescape"))
        for item in result.stdout.split(b"\0")
        if item
    }


def historical_blobs() -> list[
    tuple[str, PurePosixPath, int, str | None, str | None, bool]
]:
    """Return historical blob/path pairs without following symlink targets."""

    roots = set(
        subprocess.run(
            ["git", "rev-list", "--all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for ref in refs:
        tree = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{tree}}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if tree.returncode == 0:
            roots.add(tree.stdout.strip())
    entries: set[tuple[str, PurePosixPath, str]] = set()
    for root in roots:
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", root],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            if separator and object_type == "blob":
                relative = PurePosixPath(
                    raw_path.decode("utf-8", errors="surrogateescape")
                )
                entries.add((object_id, relative, mode))

    blobs: list[
        tuple[str, PurePosixPath, int, str | None, str | None, bool]
    ] = []
    for object_id, relative, mode in sorted(
        entries, key=lambda item: (str(item[1]), item[0], item[2])
    ):
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
        is_symlink = mode == "120000"
        text: str | None = None
        content_hash: str | None = None
        if (
            not is_symlink
            and relative.suffix.lower() in TEXT_SUFFIXES
            and size <= MAX_BYTES
        ):
            content = subprocess.run(
                ["git", "cat-file", "blob", object_id],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            text = content.decode("utf-8", errors="ignore")
        elif (
            not is_symlink
            and relative.suffix.lower() in RASTER_SUFFIXES
            and size <= MAX_BYTES
        ):
            content = subprocess.run(
                ["git", "cat-file", "blob", object_id],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            content_hash = hashlib.sha256(content).hexdigest()
        blobs.append(
            (
                f"history:{relative}@{object_id[:12]}",
                relative,
                size,
                text,
                content_hash,
                is_symlink,
            )
        )
    return blobs


def inspect_entry(
    display_path: str,
    relative: Path | PurePosixPath,
    size: int,
    text: str | None,
    *,
    content_hash: str | None = None,
    is_symlink: bool = False,
    tracked: bool = True,
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
    if is_symlink:
        findings.append((display_path, "symbolic link"))
    if relative.suffix.lower() in RASTER_SUFFIXES:
        allowed = ALLOWED_RASTER.get(PurePosixPath(*relative.parts))
        if not tracked or allowed is None or (size, content_hash) not in allowed:
            findings.append((display_path, "unexpected raster image"))
    if size > MAX_BYTES:
        findings.append((display_path, "file exceeds 10 MiB"))
    if text is None:
        return findings
    if any(pattern.search(text) for pattern in PERSONAL_PATHS):
        findings.append((display_path, "personal absolute path"))
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append((display_path, label))
    return findings


def main() -> int:
    candidates = candidate_files()
    tracked = tracked_files()
    history = historical_blobs()
    findings: list[tuple[str, str]] = []
    for path in candidates:
        relative = path.relative_to(ROOT)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            findings.append((str(relative), "candidate file is missing from working tree"))
            continue
        is_symlink = stat.S_ISLNK(metadata.st_mode)
        if not is_symlink and not stat.S_ISREG(metadata.st_mode):
            findings.append((str(relative), "non-regular file"))
            continue
        size = metadata.st_size
        text = None
        content_hash = None
        if (
            not is_symlink
            and path.suffix.lower() in TEXT_SUFFIXES
            and size <= MAX_BYTES
        ):
            text = path.read_text(encoding="utf-8", errors="ignore")
        elif (
            not is_symlink
            and path.suffix.lower() in RASTER_SUFFIXES
            and size <= MAX_BYTES
        ):
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        findings.extend(
            inspect_entry(
                str(relative),
                relative,
                size,
                text,
                content_hash=content_hash,
                is_symlink=is_symlink,
                tracked=PurePosixPath(*relative.parts) in tracked,
            )
        )
    for display_path, relative, size, text, content_hash, is_symlink in history:
        findings.extend(
            inspect_entry(
                display_path,
                relative,
                size,
                text,
                content_hash=content_hash,
                is_symlink=is_symlink,
            )
        )

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
