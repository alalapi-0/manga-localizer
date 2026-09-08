"""Fail a release candidate containing common secrets or prohibited artifacts."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
import stat
import subprocess
import sys
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
    PurePosixPath("frontend/public/apple-touch-icon.png"): frozenset(
        {
            (
                1051,
                "3aebec4d7e82690a7a6bba5355314a96055639d70a0ca64d9813b1345d5d244b",
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
    ".webmanifest",
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


class AuditError(Exception):
    """A fixed-category failure; never includes command output or matched values."""


def git(*args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=ROOT,
        input=input,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AuditError("Git comparison or object coverage unavailable")
    return result.stdout


def verify_history() -> None:
    if "GIT_GRAFT_FILE" in os.environ:
        raise AuditError("grafted history is unsupported")
    graft_path = Path(git("rev-parse", "--git-path", "info/grafts").decode().strip())
    if not graft_path.is_absolute():
        graft_path = ROOT / graft_path
    if graft_path.exists() or graft_path.is_symlink():
        raise AuditError("grafted history is unsupported")
    if git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise AuditError("shallow history is unsupported")
    config = git("config", "--list", "--name-only").decode().splitlines()
    if any(
        name == "extensions.partialclone" or name.endswith(".promisor")
        for name in config
    ):
        raise AuditError("partial history is unsupported")
    git("fsck", "--connectivity-only", "--no-dangling")


def verify_comparison(base: str | None, head: str | None) -> tuple[str, str]:
    if not base or not head:
        raise AuditError("explicit base and head required; unsupported event context")
    resolved = []
    for value in (base, head):
        if not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
        ) or not value.strip("0"):
            raise AuditError("comparison requires nonzero full commit identities")
        resolved.append(
            git("rev-parse", "--verify", f"{value}^{{commit}}").decode().strip()
        )
    actual = git("rev-parse", "HEAD").decode().strip()
    if actual != resolved[1]:
        raise AuditError("declared head differs from checkout HEAD")
    git("merge-base", "--is-ancestor", *resolved)
    # Incomplete worktrees cannot define an effective local candidate.
    config = git("config", "--list").decode().lower().splitlines()
    if "core.sparsecheckout=true" in config:
        raise AuditError("sparse checkout is unsupported")
    for entry in git("ls-files", "-v", "-z").split(b"\0"):
        if entry and (entry[:1] == b"S" or entry[:1].islower()):
            raise AuditError("skip-worktree or assume-unchanged index is unsupported")
    return resolved[0], resolved[1]


def tree_entries(revision: str) -> list[tuple[str, PurePosixPath, str]]:
    entries = []
    for entry in git("ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        if object_type != "blob":
            raise AuditError("unsupported Git tree object")
        entries.append(
            (
                object_id,
                PurePosixPath(raw_path.decode("utf-8", errors="surrogateescape")),
                mode,
            )
        )
    return entries


def read_entries(entries, *, layer: str):
    """Read inert Git blobs, never a filesystem symlink or its target."""
    blobs = []
    cache = {}
    for object_id, relative, mode in sorted(
        set(entries), key=lambda item: (str(item[1]), item[0], item[2])
    ):
        if object_id not in cache:
            size = int(git("cat-file", "-s", object_id))
            cache[object_id] = (
                size,
                git("cat-file", "blob", object_id) if size <= MAX_BYTES else None,
            )
        size, content = cache[object_id]
        is_symlink = mode == "120000"
        text = None
        content_hash = None
        if not is_symlink and content is not None:
            if relative.suffix.lower() in TEXT_SUFFIXES:
                text = content.decode("utf-8", errors="ignore")
            elif relative.suffix.lower() in RASTER_SUFFIXES:
                content_hash = hashlib.sha256(content).hexdigest()
        blobs.append(
            (
                f"{layer}:{relative}@{object_id[:12]}",
                relative,
                size,
                text,
                content_hash,
                is_symlink,
            )
        )
    return blobs


def index_blobs():
    entries = []
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ")
        if stage != "0":
            raise AuditError("unmerged index is unsupported")
        if mode == "160000":
            raise AuditError("unsupported Git index object")
        entries.append(
            (
                object_id,
                PurePosixPath(raw_path.decode("utf-8", errors="surrogateescape")),
                mode,
            )
        )
    return read_entries(entries, layer="index")


def historical_blobs():
    verify_history()
    roots = set(git("rev-list", "--all", "HEAD").decode().splitlines())
    for ref in git("for-each-ref", "--format=%(refname)").decode().splitlines():
        result = subprocess.run(
            ["git", "--no-replace-objects", "rev-parse", "--verify", f"{ref}^{{tree}}"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            roots.add(result.stdout.decode().strip())
    entries = set()
    for root in roots:
        entries.update(tree_entries(root))
    return read_entries(entries, layer="history")


def cursor_resource(relative: PurePosixPath) -> bool:
    return any("cursor" in part.lower() for part in relative.parts)


def changed_paths() -> set[PurePosixPath]:
    entries = iter(
        git("status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    )
    changed = set()
    for entry in entries:
        if not entry:
            continue
        changed.add(PurePosixPath(entry[3:].decode("utf-8", errors="surrogateescape")))
        if entry[:1] in (b"R", b"C") or entry[1:2] in (b"R", b"C"):
            changed.add(
                PurePosixPath(next(entries).decode("utf-8", errors="surrogateescape"))
            )
    return changed


def personal_additions(before: dict, after: dict, layer: str) -> list[tuple[str, str]]:
    findings = []
    for relative, text in after.items():
        if text is None:
            continue
        prior = before.get(relative) or ""
        if text == prior:
            continue
        old_lines, new_lines = prior.splitlines(), text.splitlines()
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
        added = "\n".join(
            line
            for operation, _, _, start, end in matcher.get_opcodes()
            if operation in ("insert", "replace")
            for line in new_lines[start:end]
        )
        if any(pattern.search(added) for pattern in PERSONAL_PATHS):
            findings.append((f"{layer}:{relative}", "personal absolute path"))
    return findings


def inspect_entry(
    display_path: str,
    relative: Path | PurePosixPath,
    size: int,
    text: str | None,
    *,
    content_hash: str | None = None,
    is_symlink: bool = False,
    tracked: bool = True,
    personal_paths: bool = True,
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
    if personal_paths and any(pattern.search(text) for pattern in PERSONAL_PATHS):
        findings.append((display_path, "personal absolute path"))
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append((display_path, label))
    return findings


def audit(mode: str, base: str | None, head: str | None) -> int:
    if mode == "normal-ci":
        base, head = verify_comparison(base, head)
    candidates = candidate_files()
    tracked = tracked_files()
    if any(cursor_resource(path) for path in changed_paths()):
        raise AuditError(
            "changed Cursor resource requires an independent authorized scope"
        )
    history = historical_blobs()
    index = index_blobs()
    findings: list[tuple[str, str]] = []
    index_text = {relative: text for _, relative, _, text, _, _ in index}
    current_text = {}
    protected = [
        path
        for path in candidates
        if cursor_resource(PurePosixPath(path.relative_to(ROOT)))
    ]
    if protected:
        head_blobs = read_entries(tree_entries("HEAD"), layer="head")
        inert = {row[1]: row for row in head_blobs}
    else:
        inert = {}
    live_reads = 0
    inert_reads = 0
    for path in candidates:
        relative = PurePosixPath(path.relative_to(ROOT))
        if cursor_resource(relative):
            if relative not in inert:
                raise AuditError("untracked Cursor resource cannot be read")
            _, _, size, text, content_hash, is_symlink = inert[relative]
            inert_reads += 1
        else:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                if relative in tracked:
                    continue  # local deletion; the commit-ready index is still scanned
                raise AuditError("candidate changed while being inspected") from None
            is_symlink = stat.S_ISLNK(metadata.st_mode)
            if not is_symlink and not stat.S_ISREG(metadata.st_mode):
                findings.append((str(relative), "non-regular file"))
                continue
            size, text, content_hash = metadata.st_size, None, None
            if not is_symlink and size <= MAX_BYTES:
                if path.suffix.lower() in TEXT_SUFFIXES:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    live_reads += 1
                elif path.suffix.lower() in RASTER_SUFFIXES:
                    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    live_reads += 1
        current_text[relative] = text
        findings.extend(
            inspect_entry(
                str(relative),
                relative,
                size,
                text,
                content_hash=content_hash,
                is_symlink=is_symlink,
                tracked=relative in tracked,
                personal_paths=mode == "release",
            )
        )
    for display_path, relative, size, text, content_hash, is_symlink in [
        *history,
        *index,
    ]:
        findings.extend(
            inspect_entry(
                display_path,
                relative,
                size,
                text,
                content_hash=content_hash,
                is_symlink=is_symlink,
                personal_paths=mode == "release",
            )
        )
    if mode == "normal-ci":
        base_text = {
            row[1]: row[3] for row in read_entries(tree_entries(base), layer="base")
        }
        head_text = {
            row[1]: row[3] for row in read_entries(tree_entries(head), layer="head")
        }
        findings.extend(
            personal_additions(base_text, current_text, "base-to-candidate")
        )
        findings.extend(personal_additions(head_text, index_text, "head-to-index"))
        findings.extend(
            personal_additions(index_text, current_text, "index-to-working-tree")
        )
        findings.extend(
            personal_additions(
                {},
                {p: t for p, t in current_text.items() if p not in tracked},
                "untracked",
            )
        )
    print(
        f"Audit mode={mode}; base={base or 'not-applicable'}; head={head or 'not-applicable'}"
    )
    print(
        f"Coverage: candidate={len(candidates)} index_blobs={len(index)} historical_blob_paths={len(history)} live_content_reads={live_reads} inert_cursor_objects={inert_reads} cursor_live_content_reads=0"
    )
    if findings:
        print(
            "Audit failed; release_not_ready" if mode == "release" else "Audit failed"
        )
        for path, reason in sorted(set(findings)):
            print(f"- {path!r}: {reason}")
        return 1
    print(
        "Audit passed; normal CI is not release acceptance"
        if mode == "normal-ci"
        else "Release audit passed"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("release", "normal-ci"), default="release")
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args(argv or [])
    if args.mode == "release" and (args.base or args.head):
        parser.error("release mode does not accept a comparison range")
    try:
        return audit(args.mode, args.base, args.head)
    except (AuditError, OSError, subprocess.CalledProcessError) as error:
        category = (
            str(error)
            if isinstance(error, AuditError)
            else "candidate or object coverage unavailable"
        )
        print(f"Audit mode={args.mode} failed closed: {category}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
