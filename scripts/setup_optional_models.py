from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSpec:
    filename: str
    url: str
    sha256: str
    license: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    filename: str
    extract_dir: str
    urls: tuple[str, ...]
    sha256: str
    license: str
    notes: str = ""


MODELS = {
    "ppocr": ModelSpec(
        filename="text_detection_cn_ppocrv3_2023may.onnx",
        url=(
            "https://huggingface.co/opencv/text_detection_ppocr/resolve/main/"
            "text_detection_cn_ppocrv3_2023may.onnx"
        ),
        sha256="03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587",
        license="Apache-2.0",
        notes="OpenCV Zoo PP-OCRv3 detection graph.",
    ),
    "lama": ModelSpec(
        filename="inpainting_lama_2025jan.onnx",
        url=(
            "https://huggingface.co/opencv/inpainting_lama/resolve/main/"
            "inpainting_lama_2025jan.onnx"
        ),
        sha256="7df918ac3921d3daf0aae1d219776cf0dc4e4935f035af81841b40adcf74fdf2",
        license="Apache-2.0",
        notes="OpenCV Zoo LaMa 512x512 inpainting graph.",
    ),
    "realesrgan": ModelSpec(
        filename="RealESRGAN_x4plus_anime_6B.onnx",
        url=(
            "https://huggingface.co/deepghs/imgutils-models/resolve/main/"
            "real_esrgan/RealESRGAN_x4plus_anime_6B.onnx"
        ),
        sha256="2648cab4c4343541c1aa291c6754e9e8edbe7a813fffc2a677423dd12cb6b7f7",
        license="BSD-3-Clause",
        notes=(
            "ONNX conversion of xinntao/Real-ESRGAN RealESRGAN_x4plus_anime_6B "
            "weights. Native scale is 4x; 2x/3x requests downscale that result."
        ),
    ),
}

ARCHIVES = {
    "argos-ja-en": ArchiveSpec(
        filename="translate-ja_en-1_1.argosmodel",
        extract_dir="argos-ja-en",
        urls=(
            "https://argos-net.com/v1/translate-ja_en-1_1.argosmodel",
            "https://data.argosopentech.com/argospm/v1/translate-ja_en-1_1.argosmodel",
            (
                "https://huggingface.co/TiberiuCristianLeon/Argostranslate/resolve/"
                "50b9550bd4ea6890825218ccf42fd8741b8dc0e1/translate-ja_en-1_1.argosmodel"
            ),
        ),
        sha256="623e3477959a815eb0a5ef53e09079ae8f1f9d3bbcd230473baf28c03fb83335",
        license="CC-BY-4.0",
        notes="Argos Translate Japanese-English CTranslate2 package from OPUS-MT.",
    ),
    "argos-en-zh": ArchiveSpec(
        filename="translate-en_zh-1_9.argosmodel",
        extract_dir="argos-en-zh",
        urls=(
            "https://argos-net.com/v1/translate-en_zh-1_9.argosmodel",
            "https://data.argosopentech.com/argospm/v1/translate-en_zh-1_9.argosmodel",
            (
                "https://huggingface.co/TiberiuCristianLeon/Argostranslate/resolve/"
                "50b9550bd4ea6890825218ccf42fd8741b8dc0e1/translate-en_zh-1_9.argosmodel"
            ),
        ),
        sha256="433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab",
        license="CC-BY-4.0",
        notes="Argos Translate English-Chinese CTranslate2 package from OPUS-MT.",
    ),
}

ALIASES = {
    "argos-ja-zh": ("argos-ja-en", "argos-en-zh"),
}

BUNDLE_SELECTION = ("ppocr", "lama", "realesrgan", "argos-ja-zh")

SETTING_FOR = {
    "ppocr": "ppocr_detection_model",
    "lama": "lama_inpainting_model",
    "realesrgan": "realesrgan_onnx_model",
    "argos-ja-en": "argos_ja_en_model",
    "argos-en-zh": "argos_en_zh_model",
}

CHOICES = tuple(sorted(MODELS) + sorted(ARCHIVES) + sorted(ALIASES))
MANIFEST_NAME = "manifest.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guarded_path(
    key: str,
    *,
    environ: Mapping[str, str] | None = None,
    run_guard=subprocess.run,
) -> Path:
    active_environ = os.environ if environ is None else environ
    home = active_environ.get("HOME", "").strip()
    configured = active_environ.get("STORAGE_GOVERNANCE_GUARD", "").strip()
    guard = (
        Path(configured)
        if configured
        else Path(home) / ".config/storage-governance/guard.sh"
    )
    if not home or not guard.is_absolute():
        raise RuntimeError("storage governance guard path is missing or not absolute")
    if (
        not guard.exists()
        or guard.is_symlink()
        or not guard.is_file()
        or guard.resolve() != guard
        or not os.access(guard, os.X_OK)
    ):
        raise RuntimeError(
            "storage governance guard is not a canonical executable file"
        )
    result = run_guard(
        [str(guard), "--get-path", key],
        capture_output=True,
        text=True,
        env=active_environ,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"storage governance guard rejected model destination{suffix}"
        )
    lines = result.stdout.rstrip("\n").splitlines()
    if len(lines) != 1 or not Path(lines[0]).is_absolute():
        raise RuntimeError(
            f"storage mapping {key} is missing, ambiguous, or not absolute"
        )
    root = Path(lines[0])
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise RuntimeError(f"storage mapping {key} is not a canonical real directory")
    return root


def validate_guarded_bundle_destination(
    destination: Path,
    *,
    environ: Mapping[str, str] | None = None,
    run_guard=subprocess.run,
) -> Path:
    expanded = destination.expanduser()
    if not expanded.is_absolute():
        raise RuntimeError("model bundle destination must be absolute")
    candidate = Path(os.path.normpath(expanded))
    if candidate != expanded:
        raise RuntimeError("model bundle destination must be canonical")
    roots = (
        _guarded_path("roots.models", environ=environ, run_guard=run_guard),
        _guarded_path("roots.artifacts", environ=environ, run_guard=run_guard),
    )
    for root in roots:
        if candidate == root or not candidate.is_relative_to(root):
            continue
        root_device = root.stat().st_dev
        cursor = root
        for part in candidate.relative_to(root).parts:
            cursor /= part
            if not cursor.exists() and not cursor.is_symlink():
                break
            info = cursor.lstat()
            if cursor.is_symlink():
                raise RuntimeError(
                    "model bundle destination must not contain symbolic links"
                )
            if info.st_dev != root_device:
                raise RuntimeError(
                    "model bundle destination escaped the verified external filesystem"
                )
            if not cursor.is_dir():
                raise RuntimeError(
                    "model bundle destination contains a non-directory component"
                )
        return candidate
    raise RuntimeError(
        "model bundle destination is outside the verified external roots"
    )


def verify_file(path: Path, sha256: str) -> bool:
    return path.is_file() and file_sha256(path) == sha256


def copy_verified_file(source: Path, destination: Path, sha256: str) -> None:
    if not verify_file(source, sha256):
        raise RuntimeError(f"refusing to copy unverified file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if not verify_file(destination, sha256):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch after copy: {destination}")


def manifest_entry(name: str) -> dict[str, str]:
    if name in MODELS:
        spec = MODELS[name]
        return {
            "name": name,
            "kind": "file",
            "path": spec.filename,
            "sha256": spec.sha256,
            "license": spec.license,
            "setting": SETTING_FOR[name],
        }
    spec = ARCHIVES[name]
    return {
        "name": name,
        "kind": "archive",
        "path": spec.extract_dir,
        "archive": spec.filename,
        "sha256": spec.sha256,
        "license": spec.license,
        "setting": SETTING_FOR[name],
    }


def write_manifest(path: Path, names: list[str]) -> None:
    payload = {
        "schemaVersion": 1,
        "downloadsAtStartup": False,
        "models": [manifest_entry(name) for name in names],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def verify_named(name: str, models_dir: Path) -> dict[str, str | bool | None]:
    if name in MODELS:
        spec = MODELS[name]
        target = models_dir / spec.filename
        if verify_file(target, spec.sha256):
            return {"name": name, "available": True, "error": None}
        if not target.is_file():
            return {"name": name, "available": False, "error": "model file is missing"}
        return {"name": name, "available": False, "error": "checksum mismatch"}
    spec = ARCHIVES[name]
    archive = models_dir / spec.filename
    extract = models_dir / spec.extract_dir
    if not verify_file(archive, spec.sha256):
        if not archive.is_file():
            return {"name": name, "available": False, "error": "archive is missing"}
        return {"name": name, "available": False, "error": "checksum mismatch"}
    if not archive_ready(extract):
        return {
            "name": name,
            "available": False,
            "error": "extracted package is incomplete",
        }
    return {"name": name, "available": True, "error": None}


def copy_named(name: str, source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    if name in MODELS:
        spec = MODELS[name]
        copy_verified_file(
            source_dir / spec.filename, destination_dir / spec.filename, spec.sha256
        )
        return
    spec = ARCHIVES[name]
    copy_verified_file(
        source_dir / spec.filename, destination_dir / spec.filename, spec.sha256
    )
    source_extract = source_dir / spec.extract_dir
    destination_extract = destination_dir / spec.extract_dir
    if not archive_ready(source_extract):
        raise RuntimeError(f"{name} extracted package is missing required files")
    if destination_extract.exists():
        shutil.rmtree(destination_extract)
    shutil.copytree(source_extract, destination_extract)
    if not archive_ready(destination_extract):
        raise RuntimeError(f"{name} extracted package is incomplete after copy")


def expand_selection(names: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for name in names:
        parts = ALIASES.get(name, (name,))
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            expanded.append(part)
    return expanded


def archive_ready(extract_path: Path) -> bool:
    return (
        extract_path.is_dir()
        and (extract_path / "metadata.json").is_file()
        and (extract_path / "sentencepiece.model").is_file()
        and (extract_path / "model" / "model.bin").is_file()
    )


def download_verified(urls: tuple[str, ...], sha256: str, destination: Path) -> str:
    last_error: Exception | None = None
    for url in urls:
        try:
            print(f"downloading {url}")
            with (
                urllib.request.urlopen(url, timeout=600) as response,
                destination.open("wb") as target,
            ):
                shutil.copyfileobj(response, target, length=1024 * 1024)
            actual = file_sha256(destination)
            if actual == sha256:
                return url
            last_error = RuntimeError(
                f"checksum mismatch from {url}: expected {sha256}, received {actual}"
            )
        except (OSError, RuntimeError, ValueError) as error:
            last_error = error
            destination.unlink(missing_ok=True)
    assert last_error is not None
    raise last_error


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    resolved = destination.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts:
                raise RuntimeError(f"Unsafe archive member: {name}")
            target = (destination / name).resolve()
            if not target.is_relative_to(resolved):
                raise RuntimeError(f"Unsafe archive member: {name}")
        zipped.extractall(destination)


def package_root(extracted: Path) -> Path:
    if (extracted / "metadata.json").is_file():
        return extracted
    children = [path for path in extracted.iterdir() if path.is_dir()]
    if len(children) == 1 and (children[0] / "metadata.json").is_file():
        return children[0]
    raise RuntimeError("Argos archive did not contain metadata.json")


def _copy_from_model_dirs(
    name: str,
    filename: str,
    sha256: str,
    target: Path,
    model_dirs: tuple[Path, ...],
) -> bool:
    for model_dir in model_dirs:
        source = model_dir.expanduser() / filename
        if source.resolve() == target.resolve():
            continue
        if verify_file(source, sha256):
            print(f"[{name}] copying verified {filename}")
            copy_verified_file(source, target, sha256)
            return True
    return False


def install_model(
    name: str,
    spec: ModelSpec,
    target_dir: Path,
    *,
    force: bool,
    copy_from_model_dirs: tuple[Path, ...] = (),
    no_download: bool = False,
) -> None:
    target = target_dir / spec.filename
    print(f"[{name}] license: {spec.license}")
    if spec.notes:
        print(f"[{name}] {spec.notes}")
    if target.is_file() and file_sha256(target) == spec.sha256 and not force:
        print(f"[{name}] already verified: {target}")
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    if not force and _copy_from_model_dirs(
        name, spec.filename, spec.sha256, target, copy_from_model_dirs
    ):
        print(f"[{name}] installed and verified: {target}")
        print(f"[{name}] sha256: {spec.sha256}")
        return
    if no_download:
        raise RuntimeError(
            f"{name} is not available locally and downloads are disabled"
        )
    with tempfile.NamedTemporaryFile(
        dir=target_dir,
        prefix=f".{spec.filename}.",
        suffix=".download",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            download_verified((spec.url,), spec.sha256, temporary_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    temporary_path.replace(target)
    print(f"[{name}] installed and verified: {target}")
    print(f"[{name}] sha256: {spec.sha256}")


def install_archive(
    name: str,
    spec: ArchiveSpec,
    target_dir: Path,
    *,
    force: bool,
    copy_from_model_dirs: tuple[Path, ...] = (),
    no_download: bool = False,
) -> None:
    archive_path = target_dir / spec.filename
    extract_path = target_dir / spec.extract_dir
    print(f"[{name}] license: {spec.license}")
    if spec.notes:
        print(f"[{name}] {spec.notes}")
    archive_ok = archive_path.is_file() and file_sha256(archive_path) == spec.sha256
    if archive_ok and archive_ready(extract_path) and not force:
        print(f"[{name}] already verified: {extract_path}")
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    if not force:
        for source_models in copy_from_model_dirs:
            source_models = source_models.expanduser()
            source_archive = source_models / spec.filename
            source_extract = source_models / spec.extract_dir
            if source_archive.resolve() == archive_path.resolve():
                continue
            if verify_file(source_archive, spec.sha256) and archive_ready(
                source_extract
            ):
                print(f"[{name}] copying verified {spec.extract_dir}")
                copy_named(name, source_models, target_dir)
                print(f"[{name}] installed and verified: {extract_path}")
                print(f"[{name}] sha256: {spec.sha256}")
                return
    if no_download:
        raise RuntimeError(
            f"{name} is not available locally and downloads are disabled"
        )
    if not archive_ok or force:
        with tempfile.NamedTemporaryFile(
            dir=target_dir,
            prefix=f".{spec.filename}.",
            suffix=".download",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                download_verified(spec.urls, spec.sha256, temporary_path)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
        temporary_path.replace(archive_path)
    staging = Path(tempfile.mkdtemp(dir=target_dir, prefix=f".{spec.extract_dir}."))
    try:
        safe_extract(archive_path, staging)
        root = package_root(staging)
        if extract_path.exists():
            shutil.rmtree(extract_path)
        shutil.move(str(root), str(extract_path))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not archive_ready(extract_path):
        raise RuntimeError(f"{name} extracted package is missing required files")
    print(f"[{name}] installed and verified: {extract_path}")
    print(f"[{name}] sha256: {spec.sha256}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly download optional local models with license and SHA-256 "
            "verification. The application never downloads models at startup. "
            "Default selection is the ONNX vision models; Argos translation "
            "packages are opt-in."
        )
    )
    parser.add_argument(
        "models",
        nargs="*",
        choices=CHOICES,
        default=None,
        help="Model names. Defaults to ONNX vision models, or the app bundle set with --bundle-dest.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Deprecated and rejected as a model destination; use the guarded --bundle-dest route",
    )
    parser.add_argument(
        "--copy-from",
        action="append",
        type=Path,
        default=[],
        help="Existing data directories to copy verified models from before downloading",
    )
    parser.add_argument(
        "--copy-from-models-dir",
        action="append",
        type=Path,
        default=[],
        help="Existing model directories to copy verified models from before downloading",
    )
    parser.add_argument(
        "--bundle-dest",
        type=Path,
        default=None,
        help="Copy verified models into this directory and write manifest.json",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify files in --data-dir or --bundle-dest without downloading",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="copy or reuse verified files only; never download",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an already verified model"
    )
    parser.add_argument(
        "--print-specs",
        action="store_true",
        help="print license and checksum metadata without downloading",
    )
    args = parser.parse_args()
    if not args.print_specs:
        if args.data_dir is not None:
            parser.error(
                "--data-dir is not a supported model destination; use --bundle-dest"
            )
        if args.bundle_dest is None:
            parser.error("model setup requires an explicit guarded --bundle-dest")
    return args


def print_spec(name: str) -> None:
    if name in MODELS:
        spec = MODELS[name]
        print(f"{name}\t{spec.license}\t{spec.sha256}\t{spec.filename}")
        return
    spec = ARCHIVES[name]
    print(f"{name}\t{spec.license}\t{spec.sha256}\t{spec.filename}")


def selected_names(args: argparse.Namespace) -> list[str]:
    if args.models:
        return expand_selection(list(args.models))
    if args.bundle_dest is not None:
        return expand_selection(list(BUNDLE_SELECTION))
    return sorted(MODELS)


def main() -> int:
    args = parse_args()
    selected = selected_names(args)
    if args.print_specs:
        for name in selected:
            print_spec(name)
        return 0
    target_dir = validate_guarded_bundle_destination(args.bundle_dest)
    if args.verify_only:
        failed = 0
        for name in selected:
            result = verify_named(name, target_dir)
            status = "ok" if result["available"] else "unavailable"
            print(f"[{name}] {status}: {result['error'] or 'verified'}")
            if not result["available"]:
                failed += 1
        return 1 if failed else 0
    copy_from_model_dirs = tuple(
        path.expanduser() / "models" for path in args.copy_from
    ) + tuple(path.expanduser() for path in args.copy_from_models_dir)
    for name in selected:
        if name in MODELS:
            install_model(
                name,
                MODELS[name],
                target_dir,
                force=args.force,
                copy_from_model_dirs=copy_from_model_dirs,
                no_download=args.no_download,
            )
            continue
        install_archive(
            name,
            ARCHIVES[name],
            target_dir,
            force=args.force,
            copy_from_model_dirs=copy_from_model_dirs,
            no_download=args.no_download,
        )
    if args.bundle_dest is not None:
        destination = args.bundle_dest.expanduser().resolve()
        if destination != target_dir:
            for name in selected:
                copy_named(name, target_dir, destination)
        write_manifest(destination / MANIFEST_NAME, selected)
        print(f"wrote bundle manifest: {destination / MANIFEST_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
