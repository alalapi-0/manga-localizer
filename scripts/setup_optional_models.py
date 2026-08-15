from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
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

CHOICES = tuple(sorted(MODELS) + sorted(ARCHIVES) + sorted(ALIASES))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def install_model(name: str, spec: ModelSpec, target_dir: Path, *, force: bool) -> None:
    target = target_dir / spec.filename
    print(f"[{name}] license: {spec.license}")
    if spec.notes:
        print(f"[{name}] {spec.notes}")
    if target.is_file() and file_sha256(target) == spec.sha256 and not force:
        print(f"[{name}] already verified: {target}")
        return
    target_dir.mkdir(parents=True, exist_ok=True)
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
    name: str, spec: ArchiveSpec, target_dir: Path, *, force: bool
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
        default=sorted(MODELS),
        help="Model names. Defaults to ONNX vision models only.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".manga-localizer",
        help="Application data directory (models are written below its models/ folder)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an already verified model"
    )
    parser.add_argument(
        "--print-specs",
        action="store_true",
        help="print license and checksum metadata without downloading",
    )
    return parser.parse_args()


def print_spec(name: str) -> None:
    if name in MODELS:
        spec = MODELS[name]
        print(f"{name}\t{spec.license}\t{spec.sha256}\t{spec.filename}")
        return
    spec = ARCHIVES[name]
    print(f"{name}\t{spec.license}\t{spec.sha256}\t{spec.filename}")


def main() -> int:
    args = parse_args()
    selected = expand_selection(args.models or sorted(MODELS))
    if args.print_specs:
        for name in selected:
            print_spec(name)
        return 0
    target_dir = args.data_dir.expanduser().resolve() / "models"
    for name in selected:
        if name in MODELS:
            install_model(name, MODELS[name], target_dir, force=args.force)
            continue
        install_archive(name, ARCHIVES[name], target_dir, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
