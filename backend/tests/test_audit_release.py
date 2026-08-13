from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_release.py"
SPEC = importlib.util.spec_from_file_location("audit_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_release)


def _reasons(findings: list[tuple[str, str]]) -> set[str]:
    return {reason for _, reason in findings}


def test_main_scans_secrets_past_two_mib(tmp_path: Path, monkeypatch, capsys) -> None:
    token = "gh" + "p_" + "A" * 24
    candidate = tmp_path / "large.txt"
    candidate.write_text(
        "x" * (2 * 1024 * 1024 + 1) + "\n" + token, encoding="utf-8"
    )

    monkeypatch.setattr(audit_release, "ROOT", tmp_path)
    monkeypatch.setattr(audit_release, "candidate_files", lambda: [candidate])
    monkeypatch.setattr(
        audit_release, "tracked_files", lambda: {PurePosixPath("large.txt")}
    )
    monkeypatch.setattr(audit_release, "historical_blobs", lambda: [])

    assert audit_release.main() == 1
    assert "GitHub token" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text",
    [
        "/" + "Users" + "/alice/project/file.txt",
        "/" + "home" + "/alice/project/file.txt",
        "C:\\" + "Users" + "\\alice\\project\\file.txt",
        "C:/" + "Users" + "/alice/project/file.txt",
    ],
)
def test_cross_platform_personal_paths_are_rejected(text: str) -> None:
    findings = audit_release.inspect_entry(
        "sample.txt", PurePosixPath("sample.txt"), len(text), text
    )

    assert "personal absolute path" in _reasons(findings)


def test_candidate_symlink_is_rejected_without_following_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("ordinary external content", encoding="utf-8")
    candidate = tmp_path / "link.txt"
    try:
        candidate.symlink_to(target)
    except OSError:
        pytest.skip("File symlinks are unavailable on this platform")

    monkeypatch.setattr(audit_release, "ROOT", tmp_path)
    monkeypatch.setattr(audit_release, "candidate_files", lambda: [candidate])
    monkeypatch.setattr(
        audit_release, "tracked_files", lambda: {PurePosixPath("link.txt")}
    )
    monkeypatch.setattr(audit_release, "historical_blobs", lambda: [])

    assert audit_release.main() == 1
    assert "symbolic link" in capsys.readouterr().out


def test_historical_symlink_is_rejected() -> None:
    findings = audit_release.inspect_entry(
        "history:link@abc", PurePosixPath("link"), 8, None, is_symlink=True
    )

    assert "symbolic link" in _reasons(findings)


def test_unexpected_raster_is_rejected() -> None:
    findings = audit_release.inspect_entry(
        "assets/new.png",
        PurePosixPath("assets/new.png"),
        12,
        None,
        content_hash="0" * 64,
    )

    assert "unexpected raster image" in _reasons(findings)


def test_tracked_documentation_raster_is_allowed_only_at_pinned_blob() -> None:
    relative, approved = next(iter(audit_release.ALLOWED_RASTER.items()))
    size, content_hash = next(iter(approved))

    findings = audit_release.inspect_entry(
        str(relative), relative, size, None, content_hash=content_hash, tracked=True
    )
    untracked_findings = audit_release.inspect_entry(
        str(relative), relative, size, None, content_hash=content_hash, tracked=False
    )

    assert "unexpected raster image" not in _reasons(findings)
    assert "unexpected raster image" in _reasons(untracked_findings)


def test_raster_allowlist_supports_approved_current_and_historical_versions(
    monkeypatch,
) -> None:
    relative = PurePosixPath("docs/assets/example.jpg")
    approved = frozenset({(10, "a" * 64), (12, "b" * 64)})
    monkeypatch.setattr(audit_release, "ALLOWED_RASTER", {relative: approved})

    for size, content_hash in approved:
        findings = audit_release.inspect_entry(
            str(relative),
            relative,
            size,
            None,
            content_hash=content_hash,
            tracked=True,
        )
        assert "unexpected raster image" not in _reasons(findings)
