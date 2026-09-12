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
    candidate.write_text("x" * (2 * 1024 * 1024 + 1) + "\n" + token, encoding="utf-8")

    monkeypatch.setattr(audit_release, "ROOT", tmp_path)
    monkeypatch.setattr(audit_release, "candidate_files", lambda: [candidate])
    monkeypatch.setattr(audit_release, "tracked_files", lambda: {PurePosixPath("large.txt")})
    monkeypatch.setattr(audit_release, "historical_blobs", lambda: [])
    monkeypatch.setattr(audit_release, "index_blobs", lambda: [])
    monkeypatch.setattr(audit_release, "changed_paths", lambda: set())

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
    monkeypatch.setattr(audit_release, "tracked_files", lambda: {PurePosixPath("link.txt")})
    monkeypatch.setattr(audit_release, "historical_blobs", lambda: [])
    monkeypatch.setattr(audit_release, "index_blobs", lambda: [])
    monkeypatch.setattr(audit_release, "changed_paths", lambda: set())

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


def test_home_screen_icon_is_allowed_only_at_pinned_blob() -> None:
    relative = PurePosixPath("frontend/public/apple-touch-icon.png")
    approved = audit_release.ALLOWED_RASTER[relative]
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


# Real temporary Git repositories exercise the declared comparison and each layer.
def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def _commit(root: Path) -> str:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Audit fixture",
        "-c",
        "user.email=audit@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def audit_repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "note.txt").write_text("ordinary\n")
    base = _commit(tmp_path)
    monkeypatch.setattr(audit_release, "ROOT", tmp_path)
    return tmp_path, base


def _ci(base: str, root: Path) -> int:
    return audit_release.main(
        ["--mode", "normal-ci", "--base", base, "--head", _git(root, "rev-parse", "HEAD")]
    )


def _personal() -> str:
    return "/" + "Users" + "/fixture/private/file.txt\n"


@pytest.mark.parametrize("layer", ["head", "staged", "unstaged", "untracked", "staged-removed"])
def test_ci_rejects_new_personal_path_in_every_layer(audit_repo, capsys, layer):
    root, base = audit_repo
    path = root / ("new.txt" if layer == "untracked" else "note.txt")
    path.write_text(_personal())
    if layer == "head":
        _commit(root)
    elif layer in ("staged", "staged-removed"):
        _git(root, "add", "note.txt")
    if layer == "staged-removed":
        path.write_text("ordinary\n")
    assert _ci(base, root) == 1
    output = capsys.readouterr().out
    assert "personal absolute path" in output
    assert _personal().strip() not in output


def test_ci_allows_only_historical_paths_and_release_stays_strict(audit_repo, capsys):
    root, _ = audit_repo
    (root / "note.txt").write_text(_personal())
    base = _commit(root)
    assert _ci(base, root) == 0
    capsys.readouterr()
    assert audit_release.main() == 1
    default_output = capsys.readouterr().out
    assert audit_release.main(["--mode", "release"]) == 1
    assert capsys.readouterr().out == default_output
    assert "release_not_ready" in default_output
    assert _personal().strip() not in default_output


@pytest.mark.parametrize("removal", ["committed", "working"])
def test_ci_net_candidate_removes_intermediate_path_without_erasing_history(audit_repo, removal):
    root, base = audit_repo
    (root / "note.txt").write_text(_personal())
    _commit(root)
    (root / "note.txt").write_text("ordinary\n")
    if removal == "committed":
        _commit(root)
    assert _ci(base, root) == 0
    assert audit_release.main() == 1


@pytest.mark.parametrize("artifact", ["token", "private-key", "database", "symlink", "raster"])
def test_historical_hard_findings_fail_both_modes_after_removal(audit_repo, capsys, artifact):
    root, base = audit_repo
    path = root / {"database": "bad.sqlite3", "raster": "bad.png"}.get(artifact, "bad.txt")
    payload = {
        "token": "gh" + "p_" + "A" * 24,
        "private-key": "-----BEGIN " + "PRIVATE KEY-----",
    }.get(artifact, "synthetic")
    if artifact == "symlink":
        path.symlink_to("absent-target")
    else:
        path.write_text(payload)
    _commit(root)
    path.unlink()
    _commit(root)
    assert _ci(base, root) == 1
    assert audit_release.main() == 1
    assert payload not in capsys.readouterr().out


@pytest.mark.parametrize("layer", ["index", "worktree", "untracked"])
def test_current_hard_scan_cannot_hide_secret_behind_other_layer(audit_repo, capsys, layer):
    root, base = audit_repo
    path = root / ("new.txt" if layer == "untracked" else "note.txt")
    token = "gh" + "p_" + "A" * 24
    path.write_text(token)
    if layer == "index":
        _git(root, "add", "note.txt")
        path.write_text("ordinary\n")
    assert _ci(base, root) == 1
    assert audit_release.main() == 1
    assert token not in capsys.readouterr().out


@pytest.mark.parametrize(
    "case", ["missing", "zero", "unavailable", "mismatch", "nonancestor", "unsupported"]
)
def test_comparison_fails_closed(audit_repo, capsys, case):
    root, base = audit_repo
    head = _commit(root)
    args = ["--mode", "normal-ci"]
    if case == "zero":
        base = "0" * 40
    elif case == "unavailable":
        base = "f" * 40
    elif case == "mismatch":
        head = base
    elif case == "nonancestor":
        _git(root, "checkout", "--orphan", "unrelated")
        # Distinct tree prevents same-second root commits sharing an object ID.
        (root / "note.txt").write_text("unrelated root\n")
        base = _commit(root)
        _git(root, "checkout", "main")
    if case not in ("missing", "unsupported"):
        args += ["--base", base, "--head", head]
    assert audit_release.main(args) == 1
    assert "failed closed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "case", ["shallow", "sparse", "assume-unchanged", "partial", "missing-object", "unmerged"]
)
def test_incomplete_history_or_checkout_fails_closed(audit_repo, capsys, case):
    root, base = audit_repo
    if case == "shallow":
        (root / ".git/shallow").write_text(base + "\n")
    elif case == "sparse":
        _git(root, "config", "core.sparseCheckout", "true")
    elif case == "assume-unchanged":
        _git(root, "update-index", "--assume-unchanged", "note.txt")
    elif case == "partial":
        _git(root, "config", "remote.origin.promisor", "true")
    elif case == "missing-object":
        oid = _git(root, "rev-parse", "HEAD:note.txt")
        (root / ".git/objects" / oid[:2] / oid[2:]).unlink()
    else:
        import subprocess

        oid = _git(root, "rev-parse", "HEAD:note.txt")
        _git(root, "update-index", "--force-remove", "note.txt")
        subprocess.run(
            ["git", "update-index", "--index-info"],
            cwd=root,
            input=f"100644 {oid} 1\tnote.txt\n",
            text=True,
            check=True,
        )
    assert _ci(base, root) == 1
    assert "failed closed" in capsys.readouterr().out


@pytest.mark.parametrize("layer", ["working", "staged", "committed"])
def test_normal_deletion_and_empty_range_still_scan_hard_findings(audit_repo, layer):
    root, base = audit_repo
    assert _ci(base, root) == 0
    (root / "note.txt").unlink()
    if layer in ("staged", "committed"):
        _git(root, "add", "-A")
    if layer == "committed":
        _commit(root)
    assert _ci(base, root) == 0
    (root / "new.sqlite3").write_text("synthetic")
    assert _ci(base, root) == 1


def test_paths_with_whitespace_are_not_split(audit_repo, capsys):
    root, base = audit_repo
    (root / "space and\nnewline.txt").write_text(_personal())
    assert _ci(base, root) == 1
    assert "personal absolute path" in capsys.readouterr().out


def test_committed_cursor_resource_uses_git_objects_only(audit_repo, monkeypatch, capsys):
    root, _ = audit_repo
    resource = root / ".cursor/rules/example.md"
    resource.parent.mkdir(parents=True)
    resource.write_text("inert repository fixture")
    base = _commit(root)
    original_read = Path.read_text
    original_bytes = Path.read_bytes
    original_stat = Path.lstat

    def deny(method):
        def guarded(path, *args, **kwargs):
            assert ".cursor" not in path.parts, "live Cursor resource accessed"
            return method(path, *args, **kwargs)

        return guarded

    monkeypatch.setattr(Path, "read_text", deny(original_read))
    monkeypatch.setattr(Path, "read_bytes", deny(original_bytes))
    monkeypatch.setattr(Path, "lstat", deny(original_stat))
    assert _ci(base, root) == 0
    assert "inert_cursor_objects=1 cursor_live_content_reads=0" in capsys.readouterr().out


@pytest.mark.parametrize("layer", ["working", "staged", "untracked"])
def test_changed_cursor_resource_stops_before_content_reads(audit_repo, monkeypatch, capsys, layer):
    root, base = audit_repo
    resource = root / ".cursor/rules/example.md"
    resource.parent.mkdir(parents=True)
    resource.write_text("inert fixture")
    if layer != "untracked":
        base = _commit(root)
    resource.write_text("changed fixture")
    if layer == "staged":
        _git(root, "add", "-A")
    monkeypatch.setattr(
        audit_release, "historical_blobs", lambda: pytest.fail("content scan began")
    )
    monkeypatch.setattr(audit_release, "index_blobs", lambda: pytest.fail("index read began"))
    assert _ci(base, root) == 1
    assert "changed Cursor resource" in capsys.readouterr().out


def test_replacement_blob_cannot_hide_historical_secret(audit_repo, capsys):
    root, base = audit_repo
    token = "gh" + "p_" + "A" * 24
    (root / "note.txt").write_text(token)
    _commit(root)
    original = _git(root, "rev-parse", "HEAD:note.txt")
    (root / "note.txt").write_text("ordinary\n")
    _commit(root)
    replacement = _git(root, "rev-parse", "HEAD:note.txt")
    _git(root, "replace", original, replacement)
    assert _ci(base, root) == 1
    assert audit_release.main() == 1
    output = capsys.readouterr().out
    assert "GitHub token" in output
    assert token not in output


@pytest.mark.parametrize("source", ["file", "environment"])
def test_grafted_history_is_rejected(audit_repo, monkeypatch, capsys, source):
    root, base = audit_repo
    if source == "file":
        (root / ".git/info/grafts").write_text(base + "\n")
    else:
        monkeypatch.setenv("GIT_GRAFT_FILE", "unopened-fixture")
    assert _ci(base, root) == 1
    assert "grafted history is unsupported" in capsys.readouterr().out
