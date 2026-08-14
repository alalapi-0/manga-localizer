from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSpec:
    filename: str
    url: str
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            print(f"[{name}] downloading {spec.url}")
            with urllib.request.urlopen(spec.url, timeout=120) as response:
                shutil.copyfileobj(response, temporary, length=1024 * 1024)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    actual = file_sha256(temporary_path)
    if actual != spec.sha256:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"{name} checksum mismatch: expected {spec.sha256}, received {actual}"
        )
    temporary_path.replace(target)
    print(f"[{name}] installed and verified: {target}")
    print(f"[{name}] sha256: {spec.sha256}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly download optional local models with license and SHA-256 "
            "verification. The application never downloads models at startup."
        )
    )
    parser.add_argument(
        "models", nargs="*", choices=sorted(MODELS), default=sorted(MODELS)
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


def main() -> int:
    args = parse_args()
    selected = args.models or sorted(MODELS)
    if args.print_specs:
        for name in selected:
            spec = MODELS[name]
            print(f"{name}\t{spec.license}\t{spec.sha256}\t{spec.filename}")
        return 0
    target_dir = args.data_dir.expanduser().resolve() / "models"
    for name in selected:
        install_model(name, MODELS[name], target_dir, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
