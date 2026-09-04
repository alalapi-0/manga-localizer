from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / name
    specification = importlib.util.spec_from_file_location(path.stem, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[path.stem] = module
    specification.loader.exec_module(module)
    return module


def test_bootstrap_materializes_and_verifies_explicit_no_text_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = load_script("bootstrap_clean_plate_run.py")
    input_root = tmp_path / "input"
    input_root.mkdir()
    source = input_root / "page.png"
    Image.new("RGB", (32, 24), "#f4f4f4").save(source)
    manifest_path = tmp_path / "review-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "privacy": {
                    "gitIgnored": True,
                    "ocrTextStored": False,
                    "absolutePathsStored": False,
                },
                "images": [
                    {
                        "imageId": "page-0001",
                        "sourceRelativePath": "page.png",
                        "sourceChecksum": script.file_sha256(source),
                        "width": 32,
                        "height": 24,
                        "regions": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "run"

    def allow_test_output(path: Path) -> Path:
        resolved = path.resolve()
        resolved.mkdir()
        return resolved

    monkeypatch.setattr(script, "require_ignored_empty_output", allow_test_output)
    result = script.run(
        argparse.Namespace(
            input=input_root,
            review_manifest=manifest_path,
            output=output,
            expected_count=1,
            no_text_pages="1",
            label="test-clean-plate",
            poll_seconds=0.01,
            timeout_seconds=30.0,
        )
    )
    assert result == 0
    summary = json.loads((output / "summary.json").read_text("utf-8"))
    assert summary == {
        "schemaVersion": 1,
        "imageCount": 1,
        "pageVisualReviewCount": 1,
        "detectionCompleteImageCount": 1,
        "detectionPendingImageCount": 0,
        "completedNoTextImageCount": 1,
        "pendingTextImageCount": 0,
        "sourceChecksumFailures": 0,
        "dimensionFailures": 0,
        "changedOutsideMaskPixels": 0,
        "noTextMaskNonzeroPixels": 0,
        "allCompletedOutputsReviewed": True,
        "allImagesCompleted": False,
    }
    run_manifest = json.loads((output / "run-manifest.json").read_text("utf-8"))
    page = run_manifest["images"][0]
    assert page["reviewStatus"] == "no-text-reviewed"
    assert page["final"]["maskNonzeroPixels"] == 0
    assert page["final"]["changedOutsideMaskPixels"] == 0
    with sqlite3.connect(output / "workspace" / "project" / "project.sqlite3") as database:
        status = json.loads(database.execute("SELECT status FROM images").fetchone()[0])
        region_count = database.execute("SELECT count(*) FROM text_regions").fetchone()[0]
    assert status["reviewState"] == "no-text-reviewed"
    assert region_count == 0


def test_bootstrap_candidates_persist_review_only_detection_evidence(
    tmp_path: Path,
) -> None:
    script = load_script("bootstrap_clean_plate_run.py")
    input_root = tmp_path / "input"
    input_root.mkdir()
    Image.new("RGB", (32, 24), "white").save(input_root / "page.png")
    app = script.create_app(
        script.Settings(data_dir=tmp_path / "catalog"),
        start_worker=False,
    )

    with script.TestClient(app) as client:
        project = client.post(
            "/api/projects",
            json={"name": "generated-review", "outputPath": str(tmp_path / "project")},
        ).json()
        imported = client.post(
            f"/api/projects/{project['id']}/images/import-local",
            json={"paths": [str(input_root)]},
        )
        assert imported.status_code == 201
        image_by_path = script.bootstrap_candidates(
            client,
            project["id"],
            [
                {
                    "imageId": "page-0001",
                    "sourceRelativePath": "page.png",
                    "regions": [
                        {
                            "geometry": {
                                "x": 2,
                                "y": 3,
                                "width": 10,
                                "height": 8,
                                "rotation": 0,
                            },
                            "confidence": 0.73,
                        }
                    ],
                }
            ],
            set(),
        )
        regions = client.get(f"/api/images/{image_by_path['page.png']['id']}/regions").json()

    assert len(regions) == 1
    assert regions[0]["detectorConfidence"] == 0.73
    assert regions[0]["ocrConfidence"] is None
    assert regions[0]["trustDisposition"] == "review"
    assert regions[0]["trustReason"] == "automatic-proposal"
    assert regions[0]["recognition"]["detection"] == {
        "confidence": 0.73,
        "provider": "visual-review-union-candidates",
        "inputVariant": "original",
        "language": None,
    }


def test_review_pack_writes_anonymous_full_resolution_overlays(tmp_path: Path) -> None:
    script = load_script("prepare_clean_plate_review.py")
    input_root = tmp_path / "input"
    output = tmp_path / "output"
    input_root.mkdir()
    output.mkdir()
    Image.new("RGB", (120, 80), "white").save(input_root / "source.png")
    record = {
        "imageId": "page-0001",
        "sourceRelativePath": "source.png",
        "regions": [
            {
                "regionId": "region-0001",
                "geometry": {
                    "x": 20,
                    "y": 10,
                    "width": 40,
                    "height": 30,
                    "rotation": 0,
                },
                "polygon": None,
            }
        ],
    }
    numbered_count, coordinate_count = script.make_numbered_page_overlays(
        [record],
        input_root=input_root,
        output=output,
    )
    assert (numbered_count, coordinate_count) == (1, 1)
    for relative in (
        "numbered-pages/page-0001.png",
        "coordinate-pages/page-0001.png",
    ):
        with Image.open(output / relative) as image:
            image.load()
            assert image.size == (120, 80)


def test_setup_optional_models_prints_license_and_checksum_without_download(
    monkeypatch,
    capsys,
) -> None:
    script = load_script("setup_optional_models.py")
    realesrgan = script.MODELS["realesrgan"]
    assert realesrgan.license == "BSD-3-Clause"
    assert len(realesrgan.sha256) == 64
    assert realesrgan.filename.endswith(".onnx")
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup_optional_models.py", "--print-specs", "realesrgan"],
    )
    assert script.main() == 0
    output = capsys.readouterr().out
    assert "BSD-3-Clause" in output
    assert realesrgan.sha256 in output
    assert "RealESRGAN_x4plus_anime_6B.onnx" in output


def test_setup_optional_models_prints_argos_translation_packs_without_download(
    monkeypatch,
    capsys,
) -> None:
    script = load_script("setup_optional_models.py")
    ja_en = script.ARCHIVES["argos-ja-en"]
    en_zh = script.ARCHIVES["argos-en-zh"]
    assert ja_en.license == "CC-BY-4.0"
    assert en_zh.license == "CC-BY-4.0"
    assert len(ja_en.sha256) == 64
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup_optional_models.py", "--print-specs", "argos-ja-zh"],
    )
    assert script.main() == 0
    output = capsys.readouterr().out
    assert ja_en.sha256 in output
    assert en_zh.sha256 in output
    assert "translate-ja_en-1_1.argosmodel" in output
    assert "translate-en_zh-1_9.argosmodel" in output


def test_setup_optional_models_copies_verified_files_and_rejects_checksum_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = load_script("setup_optional_models.py")
    monkeypatch.setattr(
        script,
        "validate_guarded_bundle_destination",
        lambda destination: destination.expanduser().resolve(),
    )
    payload = b"tiny-realesrgan"
    source_dir = tmp_path / "source" / "models"
    source_dir.mkdir(parents=True)
    source = source_dir / "toy.onnx"
    source.write_bytes(payload)
    monkeypatch.setitem(
        script.MODELS,
        "realesrgan",
        script.ModelSpec(
            filename="toy.onnx",
            url="https://example.invalid/toy.onnx",
            sha256=__import__("hashlib").sha256(payload).hexdigest(),
            license="BSD-3-Clause",
        ),
    )
    dest = tmp_path / "bundle"
    script.copy_named("realesrgan", source_dir, dest)
    assert (dest / "toy.onnx").read_bytes() == payload
    assert script.verify_named("realesrgan", dest)["available"] is True
    (dest / "toy.onnx").write_bytes(b"tampered")
    assert script.verify_named("realesrgan", dest)["error"] == "checksum mismatch"
    copied_bundle = tmp_path / "copied-bundle"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_optional_models.py",
            "--bundle-dest",
            str(copied_bundle),
            "--copy-from-models-dir",
            str(source_dir),
            "--no-download",
            "realesrgan",
        ],
    )
    assert script.main() == 0
    assert (copied_bundle / "toy.onnx").read_bytes() == payload
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_optional_models.py",
            "--bundle-dest",
            str(tmp_path / "missing-bundle"),
            "--no-download",
            "realesrgan",
        ],
    )
    try:
        script.main()
        raise AssertionError("missing models must not be downloaded")
    except RuntimeError as error:
        assert "downloads are disabled" in str(error)


def test_setup_optional_models_rejects_every_implicit_or_data_dir_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = load_script("setup_optional_models.py")
    for argv in (
        ["setup_optional_models.py", "realesrgan"],
        [
            "setup_optional_models.py",
            "--data-dir",
            str(tmp_path / "internal-data"),
            "realesrgan",
        ],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        try:
            script.main()
            raise AssertionError("an implicit or data-dir destination must fail")
        except SystemExit as error:
            assert error.code == 2
    assert not (tmp_path / "internal-data").exists()


def test_setup_optional_models_accepts_destinations_only_below_guarded_external_roots(
    tmp_path: Path,
) -> None:
    script = load_script("setup_optional_models.py")
    home = tmp_path / "home"
    guard = home / ".config/storage-governance/guard.sh"
    guard.parent.mkdir(parents=True)
    guard.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    guard.chmod(0o755)
    models_root = tmp_path / "Models"
    artifacts_root = tmp_path / "Artifacts"
    models_root.mkdir()
    artifacts_root.mkdir()
    mappings = {
        "roots.models": models_root,
        "roots.artifacts": artifacts_root,
    }

    def run_guard(argv: list[str], **_kwargs: object) -> argparse.Namespace:
        return argparse.Namespace(
            returncode=0,
            stdout=f"{mappings[argv[-1]]}\n",
            stderr="",
        )

    environ = {"HOME": str(home), "STORAGE_GOVERNANCE_GUARD": str(guard)}
    model_bundle = models_root / "manga-localizer/model-bundle"
    package_bundle = artifacts_root / "manga-localizer/app/Models"
    assert (
        script.validate_guarded_bundle_destination(
            model_bundle,
            environ=environ,
            run_guard=run_guard,
        )
        == model_bundle
    )
    assert (
        script.validate_guarded_bundle_destination(
            package_bundle,
            environ=environ,
            run_guard=run_guard,
        )
        == package_bundle
    )
    try:
        script.validate_guarded_bundle_destination(
            tmp_path / "internal-fallback",
            environ=environ,
            run_guard=run_guard,
        )
        raise AssertionError("an internal destination must fail")
    except RuntimeError as error:
        assert "outside the verified external roots" in str(error)
    real_models = models_root / "real-models"
    real_models.mkdir()
    linked_models = models_root / "linked-models"
    linked_models.symlink_to(real_models, target_is_directory=True)
    try:
        script.validate_guarded_bundle_destination(
            linked_models / "bundle",
            environ=environ,
            run_guard=run_guard,
        )
        raise AssertionError("a symbolic-link destination component must fail")
    except RuntimeError as error:
        assert "must not contain symbolic links" in str(error)


def test_offline_diagnostics_prefer_configured_external_model_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "external-model-bundle"
    bundle.mkdir()
    ppocr = bundle / "text_detection_cn_ppocrv3_2023may.onnx"
    lama = bundle / "inpainting_lama_2025jan.onnx"
    realesrgan = bundle / "RealESRGAN_x4plus_anime_6B.onnx"
    ppocr.write_bytes(b"ppocr-fixture")
    lama.write_bytes(b"lama-fixture")
    realesrgan.write_bytes(b"realesrgan-fixture")
    archive_entries = []
    for name, archive_name, extract_name, setting in (
        ("argos-ja-en", "ja-en.argosmodel", "argos-ja-en", "argos_ja_en_model"),
        ("argos-en-zh", "en-zh.argosmodel", "argos-en-zh", "argos_en_zh_model"),
    ):
        archive = bundle / archive_name
        archive.write_bytes(name.encode())
        extract = bundle / extract_name
        (extract / "model").mkdir(parents=True)
        (extract / "metadata.json").write_text("{}", encoding="utf-8")
        (extract / "sentencepiece.model").write_bytes(b"sentencepiece")
        (extract / "model" / "model.bin").write_bytes(b"model")
        archive_entries.append(
            {
                "name": name,
                "kind": "archive",
                "path": extract_name,
                "archive": archive_name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "license": "test",
                "setting": setting,
            }
        )
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "downloadsAtStartup": False,
                "models": [
                    {
                        "name": "ppocr",
                        "kind": "file",
                        "path": ppocr.name,
                        "sha256": hashlib.sha256(ppocr.read_bytes()).hexdigest(),
                        "license": "test",
                        "setting": "ppocr_detection_model",
                    },
                    {
                        "name": "lama",
                        "kind": "file",
                        "path": lama.name,
                        "sha256": hashlib.sha256(lama.read_bytes()).hexdigest(),
                        "license": "test",
                        "setting": "lama_inpainting_model",
                    },
                    {
                        "name": "realesrgan",
                        "kind": "file",
                        "path": realesrgan.name,
                        "sha256": hashlib.sha256(realesrgan.read_bytes()).hexdigest(),
                        "license": "test",
                        "setting": "realesrgan_onnx_model",
                    },
                    *archive_entries,
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANGA_LOCALIZER_MODEL_BUNDLE", str(bundle))

    bootstrap = load_script("bootstrap_detection_annotations.py")
    evaluate = load_script("evaluate_detection_ocr.py")
    compare = load_script("compare_inpaint.py")
    upscale = load_script("compare_upscale.py")
    translate = load_script("compare_translate.py")
    assert bootstrap.resolve_ppocr_model(None) == ppocr
    assert evaluate.resolve_ppocr_model(None) == ppocr

    class AvailableLama:
        def __init__(self, model_path: Path) -> None:
            self.model_path = model_path

        def health_check(self) -> dict[str, bool]:
            return {"available": True}

    monkeypatch.setattr(compare, "LaMaONNXInpaintingProvider", AvailableLama)
    provider = compare.resolve_lama(
        argparse.Namespace(lama_model=None, data_dir=tmp_path / "internal-data")
    )
    assert provider is not None
    assert provider.model_path == lama
    assert upscale.resolve_realesrgan_model(argparse.Namespace(model=None)) == realesrgan
    translated_settings = translate.resolve_settings()
    assert translated_settings.argos_ja_en_model_path == bundle / "argos-ja-en"
    assert translated_settings.argos_en_zh_model_path == bundle / "argos-en-zh"

    ppocr.write_bytes(b"tampered")
    assert bootstrap.resolve_ppocr_model(None) is None


def test_macos_app_launcher_prefers_bundled_window_and_does_not_download() -> None:
    script = load_script("macos_app_launcher.py")
    command, kind = script.window_launch(
        "http://127.0.0.1:8000",
        window_helper=Path("/tmp/WorkbenchWindow"),
        path_exists=lambda path: str(path) == "/tmp/WorkbenchWindow",
    )
    assert kind == "app-window"
    assert command == ["/tmp/WorkbenchWindow", "http://127.0.0.1:8000"]
    assert "setup_optional_models" not in script.__dict__
    assert script.application_bind_host(lan_access=False, requested_host="127.0.0.1") == "127.0.0.1"


def test_macos_app_launcher_requires_guarded_canonical_data_dir(tmp_path: Path) -> None:
    script = load_script("macos_app_launcher.py")
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    assert script.application_data_dir({"MANGA_LOCALIZER_DATA_DIR": str(app_data)}) == app_data

    for configured in (None, "", "relative/app-data", str(tmp_path / "missing")):
        env = {} if configured is None else {"MANGA_LOCALIZER_DATA_DIR": configured}
        try:
            script.application_data_dir(env)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"unsafe app-data route was accepted: {configured!r}")

    redirected = tmp_path / "redirected-app-data"
    redirected.symlink_to(app_data, target_is_directory=True)
    try:
        script.application_data_dir({"MANGA_LOCALIZER_DATA_DIR": str(redirected)})
    except SystemExit:
        pass
    else:
        raise AssertionError("symlinked app-data route was accepted")


def test_compare_upscale_writes_relative_metrics_without_source_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = load_script("compare_upscale.py")
    source_dir = tmp_path / "input"
    output = tmp_path / "run"
    source_dir.mkdir()
    pixels = np.zeros((24, 32, 3), dtype=np.uint8)
    pixels[6:18, 8:24] = (12, 34, 200)
    pixels[10:14, 12:20] = (240, 240, 240)
    source = source_dir / "page.png"
    Image.fromarray(pixels, mode="RGB").save(source)
    original = source.read_bytes()

    from manga_localizer.imaging.preprocessing import PreprocessedImage

    class FakeAIProvider:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def health_check(self) -> dict[str, object]:
            return {
                "available": True,
                "error": None,
                "model": "RealESRGAN_x4plus_anime_6B",
                "license": "BSD-3-Clause",
                "nativeScale": 4,
                "modelExists": True,
            }

        def preprocess(self, image, **options):
            factor = int(options["upscale_factor"])
            working = image if isinstance(image, Image.Image) else Image.open(image)
            resized = working.convert("RGB").resize(
                (working.width * factor, working.height * factor),
                Image.Resampling.NEAREST,
            )
            return PreprocessedImage(image=resized, original_size=working.size)

    def allow_test_output(path: Path) -> Path:
        resolved = path.resolve()
        resolved.mkdir()
        return resolved

    monkeypatch.setattr(script, "require_ignored_empty_output", allow_test_output)
    monkeypatch.setattr(script, "RealESRGANONNXPreprocessProvider", FakeAIProvider)
    result = script.run(
        argparse.Namespace(
            input=[source],
            output=output,
            model=tmp_path / "explicit-test-model.onnx",
            factor=2,
            tile_size=64,
            label="test-upscale",
        )
    )
    assert result == 0
    assert source.read_bytes() == original
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["privacy"]["absolutePathsStored"] is False
    assert report["images"][0]["name"] == "page.png"
    assert report["images"][0]["sourceChecksumUnchanged"] is True
    assert report["images"][0]["aiDiffersFromClassic"] is True
    assert report["aggregate"]["sourceChecksumFailures"] == 0
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "/" + "Users" + "/" not in markdown
    assert (output / "classic/page.png").is_file()
    assert (output / "ai/page.png").is_file()


def test_detection_eval_script_writes_anonymous_metrics_without_ocr_text(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    script = load_script("evaluate_detection_ocr.py")
    from manga_localizer.providers.ocr import OCRRegion

    class FakeDetector:
        def health_check(self) -> dict[str, object]:
            return {"available": True, "error": None}

        def detect_text_regions(self, image, **_options):
            del image
            return [OCRRegion(210, 190, 220, 70, "こんにちは", 0.2, "horizontal")]

    class FakeOCR:
        def health_check(self) -> dict[str, object]:
            return {"available": True, "error": None}

        def recognize_region(self, image, region, **_options):
            del image, region
            return OCRRegion(0, 0, 1, 1, "こんにちは", 0.8, "horizontal")

    def allow_test_output(path: Path) -> Path:
        resolved = path.resolve()
        resolved.mkdir()
        return resolved

    monkeypatch.setattr(script, "require_ignored_empty_output", allow_test_output)
    monkeypatch.setattr(script, "build_detector", lambda _args: FakeDetector())
    monkeypatch.setattr(script, "TesseractOCRProvider", FakeOCR)
    output = tmp_path / "eval"
    result = script.run(
        argparse.Namespace(
            annotations=None,
            images=None,
            synthetic=True,
            write_synthetic=None,
            detector="ppocr-v3+tesseract",
            ppocr_model=None,
            ocr="tesseract",
            direction="auto",
            iou=0.5,
            reviewed_only=True,
            output=output,
            label="test-detection-eval",
        )
    )
    assert result == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    stdout = capsys.readouterr().out
    assert "こんにちは" not in stdout
    assert "こんにちは" not in (output / "report.md").read_text(encoding="utf-8")
    assert report["privacy"]["ocrTextStored"] is False
    assert report["privacy"]["imageNamesStored"] is False
    assert report["pageSummaries"][0]["id"] == "page-0001"
    assert report["confidence"]["usedToDropPredictions"] is False


def test_bootstrap_annotations_refuse_ocr_in_manifest_and_keep_relative_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = load_script("bootstrap_detection_annotations.py")
    from manga_localizer.providers.ocr import OCRRegion

    source_dir = tmp_path / "input"
    source_dir.mkdir()
    Image.new("RGB", (64, 48), "#f4f4f4").save(source_dir / "page.png")

    class FakeDetector:
        def health_check(self) -> dict[str, object]:
            return {"available": True, "error": None}

        def detect_text_regions(self, image, **_options):
            del image
            return [OCRRegion(4, 4, 20, 12, "", 0.11, "horizontal")]

    def allow_test_output(path: Path) -> Path:
        resolved = path.resolve()
        resolved.mkdir()
        return resolved

    monkeypatch.setattr(script, "require_ignored_empty_output", allow_test_output)
    monkeypatch.setattr(script, "build_detector", lambda _args: FakeDetector())
    output = tmp_path / "annotations"
    result = script.run(
        argparse.Namespace(
            input=source_dir,
            output=output,
            detector="tesseract",
            ppocr_model=None,
            ocr_draft=False,
            direction="auto",
            label="test-bootstrap",
        )
    )
    assert result == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    page = json.loads((output / "page.json").read_text(encoding="utf-8"))
    assert manifest["privacy"]["absolutePathsStored"] is False
    assert manifest["privacy"]["ocrTextStored"] is False
    assert manifest["independence"] == "detector-draft"
    assert page["image"]["relativeName"] == "page.png"
    assert "/" not in page["image"]["relativeName"]
    assert page["regions"][0]["detectorConfidence"] == 0.11
    assert page["regions"][0]["ocrConfidence"] is None
    assert page["status"] == "draft"
