from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from manga_localizer.config import Settings
from manga_localizer.model_bundle import apply_model_bundle
from manga_localizer.providers.translation import (
    TranslationProviderError,
    TranslationUnavailable,
)
from manga_localizer.providers.translation_argos import ArgosJaZhTranslationProvider

PUBLIC_SAMPLES = (
    "こんにちは",
    "ありがとう",
    "待って",
    "大丈夫だ",
    "行こう",
)


class CompareError(RuntimeError):
    pass


def require_ignored_empty_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    probe = resolved
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    probe = probe.resolve(strict=True)
    root_result = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if root_result.returncode:
        raise CompareError("Output must be inside a Git worktree")
    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        resolved.relative_to(git_root)
    except ValueError as error:
        raise CompareError("Output must stay inside the selected repository") from error
    ignored = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "--quiet", str(resolved)],
        check=False,
    )
    if ignored.returncode:
        raise CompareError("Output is not covered by repository ignore rules")
    if resolved.exists() and any(resolved.iterdir()):
        raise CompareError("Output directory is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return round(cjk / len(text), 4)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Translate public synthetic Japanese phrases with the local Argos "
            "ja-en-zh provider. Writes ignored-directory metrics without private "
            "OCR text or absolute personal paths."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="translate-compare")
    return parser.parse_args()


def resolve_settings() -> Settings:
    settings = Settings()
    if settings.model_bundle is None:
        raise CompareError("Run through the guarded external runtime")
    settings, _ = apply_model_bundle(settings)
    return settings


def run(args: argparse.Namespace) -> int:
    output = require_ignored_empty_output(args.output)
    settings = resolve_settings()
    provider = ArgosJaZhTranslationProvider(
        settings.argos_ja_en_model_path,
        settings.argos_en_zh_model_path,
    )
    health = provider.health_check()
    pages: list[dict[str, Any]] = []
    mock_prefix = 0
    empty = 0
    failures = 0
    if health["available"]:
        for index, source in enumerate(PUBLIC_SAMPLES, start=1):
            try:
                translated = provider.translate_text(source)
            except (
                TranslationProviderError,
                TranslationUnavailable,
                OSError,
                ValueError,
            ) as error:
                failures += 1
                pages.append(
                    {
                        "pageId": f"phrase-{index:04d}",
                        "sourceChars": len(source),
                        "translatedChars": 0,
                        "cjkRatio": 0.0,
                        "empty": True,
                        "mockPrefix": False,
                        "errorType": type(error).__name__,
                    }
                )
                continue
            is_empty = not translated.strip()
            has_mock = translated.startswith("【模拟译文】")
            empty += int(is_empty)
            mock_prefix += int(has_mock)
            pages.append(
                {
                    "pageId": f"phrase-{index:04d}",
                    "sourceChars": len(source),
                    "translatedChars": len(translated),
                    "cjkRatio": cjk_ratio(translated),
                    "empty": is_empty,
                    "mockPrefix": has_mock,
                    "errorType": None,
                }
            )
    report = {
        "schemaVersion": 1,
        "label": args.label,
        "createdAt": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "privacy": {
            "gitIgnored": True,
            "ocrTextStored": False,
            "absolutePathsStored": False,
            "imageNamesStored": False,
            "translationsStored": False,
        },
        "configuration": {
            "provider": "argos-ja-zh",
            "available": health["available"],
            "error": health["error"],
            "pivot": ["ja", "en", "zh"],
            "sampleCount": len(PUBLIC_SAMPLES),
        },
        "aggregate": {
            "pages": len(pages),
            "empty": empty,
            "mockPrefix": mock_prefix,
            "failures": failures,
        },
        "pages": pages,
    }
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "available": health["available"],
                "pages": len(pages),
                "empty": empty,
                "mockPrefix": mock_prefix,
                "failures": failures,
                "error": health["error"],
            },
            ensure_ascii=True,
        )
    )
    if not health["available"]:
        return 2
    return 0 if failures == 0 and mock_prefix == 0 and empty == 0 else 1


def main() -> int:
    try:
        return run(parse_args())
    except CompareError as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
