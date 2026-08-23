from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_script() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "evaluate_real_data.py"
    specification = importlib.util.spec_from_file_location(path.stem, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[path.stem] = module
    specification.loader.exec_module(module)
    return module


def _run_args(
    input_path: Path,
    output: Path,
    *,
    export_format: str,
    stages: str = "detect,export",
) -> argparse.Namespace:
    return argparse.Namespace(
        input=input_path,
        output=output,
        label="evaluator-export-gate",
        concurrency=1,
        poll_seconds=0.05,
        timeout_seconds=60.0,
        stages=stages,
        direction="auto",
        detector_provider="tesseract",
        ocr_provider="tesseract",
        inpainter_provider="opencv",
        repair_policy="safe",
        preprocessor_provider="opencv-pillow",
        preprocessing_profile="off",
        ppocr_model=None,
        lama_model=None,
        export_format=export_format,
    )


def test_evaluator_defaults_to_json_export_without_generated_assets(
    tmp_path: Path,
) -> None:
    script = load_script()
    args = script.parse_args(
        ["--input", str(tmp_path / "input"), "--output", str(tmp_path / "run")]
    )
    assert args.export_format == "json"
    options = script.job_options("export", args, tmp_path / "run")
    assert options["format"] == "json"
    configuration = script.report_configuration(args, ["export"])
    assert configuration["exportFormat"] == "json"


def test_evaluator_json_export_completes_without_page_review(tmp_path: Path) -> None:
    script = load_script()
    source = tmp_path / "input"
    source.mkdir()
    Image.new("RGB", (80, 100), "white").save(source / "page.png")
    output = tmp_path / "run"
    result = script.run(_run_args(source, output, export_format="json"))
    assert result == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["configuration"]["exportFormat"] == "json"
    assert report["aggregate"]["importedImages"] == 1
    assert report["aggregate"]["importFailures"] == 0
    export_stage = report["stages"][-1]
    assert export_stage["kind"] == "export"
    assert export_stage["status"] == "completed"
    assert export_stage["failedItems"] == 0
    assert (output / "export-bundle" / "original-text" / "page.json").is_file()
    assert not (output / "export-bundle" / "translated").exists()
    assert not (output / "export-bundle" / "clean").exists()


def test_evaluator_generated_export_stays_gated_without_review(tmp_path: Path) -> None:
    script = load_script()
    source = tmp_path / "input"
    source.mkdir()
    Image.new("RGB", (80, 100), "white").save(source / "page.png")
    output = tmp_path / "run"
    result = script.run(
        _run_args(
            source,
            output,
            export_format="both",
            stages="inpaint,typeset,export",
        )
    )
    assert result == 1
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["configuration"]["exportFormat"] == "both"
    typeset_stage = next(stage for stage in report["stages"] if stage["kind"] == "typeset")
    assert typeset_stage["status"] == "blocked"
    assert typeset_stage["failedItems"] == 1
    export_stage = next(stage for stage in report["stages"] if stage["kind"] == "export")
    assert export_stage["status"] == "failed"
    assert export_stage["failedItems"] == 1
    assert report["aggregate"]["failedImages"] == 1
    assert not (output / "export-bundle").exists() or not any(
        (output / "export-bundle").rglob("*.png")
    )
