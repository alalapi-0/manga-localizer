from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / name
    specification = importlib.util.spec_from_file_location(path.stem, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[path.stem] = module
    specification.loader.exec_module(module)
    return module


def _write_page(directory: Path, stem: str, *, text: str, negative: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "status": "draft",
        "independence": "detector-draft",
        "negative": negative,
        "image": {
            "id": stem,
            "relativeName": f"{stem}.png",
            "width": 40,
            "height": 30,
        },
        "regions": []
        if negative
        else [
            {
                "x": 2,
                "y": 3,
                "width": 10,
                "height": 8,
                "text": text,
                "direction": "horizontal",
                "categories": ["bubble"],
                "status": "draft",
                "detectorConfidence": 0.4,
                "ocrConfidence": None,
            }
        ],
    }
    (directory / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_review_progress_omits_ocr_text_and_page_ids(tmp_path: Path, capsys) -> None:
    script = load_script("review_detection_annotations.py")
    annotations = tmp_path / "drafts"
    _write_page(annotations, "secret-page", text="非公開の原文")
    _write_page(annotations, "empty-page", text="", negative=True)
    result = script.run(
        argparse.Namespace(
            annotations=annotations,
            output=None,
            accept=[],
            reject=[],
            decisions=None,
            list_pending=False,
            label="test-review",
        )
    )
    assert result == 0
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["pages"] == 2
    assert report["draft"] == 2
    assert report["reviewed"] == 0
    assert report["empty"] == 1
    assert report["regions"] == 1
    assert report["privacy"]["ocrTextPrinted"] is False
    assert "非公開の原文" not in stdout
    assert "secret-page" not in stdout
    assert "empty-page" not in stdout


def test_review_accept_and_reject_write_ignored_copies(tmp_path: Path, monkeypatch, capsys) -> None:
    script = load_script("review_detection_annotations.py")
    annotations = tmp_path / "drafts"
    output = tmp_path / "reviewed"
    _write_page(annotations, "keep", text="非公開の原文")
    _write_page(annotations, "drop", text="別の原文")
    _write_page(annotations, "later", text="まだ草稿")

    def allow_test_output(path: Path) -> Path:
        resolved = path.resolve()
        resolved.mkdir()
        return resolved

    monkeypatch.setattr(script, "require_ignored_empty_output", allow_test_output)
    result = script.run(
        argparse.Namespace(
            annotations=annotations,
            output=output,
            accept=["keep"],
            reject=["drop"],
            decisions=None,
            list_pending=False,
            label="test-review",
        )
    )
    assert result == 0
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["reviewed"] == 1
    assert report["rejected"] == 1
    assert report["draft"] == 1
    assert "非公開の原文" not in stdout
    kept = json.loads((output / "keep.json").read_text(encoding="utf-8"))
    dropped = json.loads((output / "drop.json").read_text(encoding="utf-8"))
    later = json.loads((output / "later.json").read_text(encoding="utf-8"))
    assert kept["status"] == "reviewed"
    assert kept["independence"] == "ground-truth"
    assert kept["regions"][0]["status"] == "reviewed"
    assert kept["regions"][0]["text"] == "非公開の原文"
    assert dropped["status"] == "rejected"
    assert later["status"] == "draft"
    original = json.loads((annotations / "keep.json").read_text(encoding="utf-8"))
    assert original["status"] == "draft"


def test_review_rejects_unknown_and_conflicting_ids(tmp_path: Path) -> None:
    script = load_script("review_detection_annotations.py")
    annotations = tmp_path / "drafts"
    _write_page(annotations, "keep", text="x")
    with pytest.raises(script.ReviewError, match="Unknown page IDs"):
        script.apply_decisions(
            script.load_page_documents(annotations),
            accept={"missing"},
            reject=set(),
        )
    with pytest.raises(script.ReviewError, match="both accepted and rejected"):
        script.run(
            argparse.Namespace(
                annotations=annotations,
                output=tmp_path / "out",
                accept=["keep"],
                reject=["keep"],
                decisions=None,
                list_pending=False,
                label="test-review",
            )
        )
