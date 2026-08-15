from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from manga_localizer.evaluation.detection_ocr import (
    DETECTOR_DRAFT_INDEPENDENCE,
    GROUND_TRUTH_INDEPENDENCE,
    REJECTED_STATUS,
    REVIEWED_STATUS,
    apply_review_decision,
    load_annotation_document,
)


class ReviewError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote ignored detector-draft annotation JSON after local human review. "
            "Stdout is aggregate counts unless --list-pending is set. OCR text is never printed."
        )
    )
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--accept", nargs="*", default=[], metavar="PAGE_ID")
    parser.add_argument("--reject", nargs="*", default=[], metavar="PAGE_ID")
    parser.add_argument("--decisions", type=Path)
    parser.add_argument(
        "--list-pending",
        action="store_true",
        help="Print pending page IDs for local review. Do not paste them into public reports.",
    )
    parser.add_argument("--label", default="detection-annotation-review")
    return parser.parse_args()


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
        raise ReviewError("Output must be inside a Git worktree")
    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        resolved.relative_to(git_root)
    except ValueError as error:
        raise ReviewError("Output must stay inside the selected repository") from error
    ignored = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "--quiet", str(resolved)],
        check=False,
    )
    if ignored.returncode:
        raise ReviewError("Output is not covered by repository ignore rules")
    if resolved.exists() and any(resolved.iterdir()):
        raise ReviewError("Output directory is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def load_page_documents(directory: Path) -> dict[str, dict[str, Any]]:
    resolved = directory.expanduser().resolve()
    if not resolved.is_dir():
        raise ReviewError(f"Annotation directory does not exist: {resolved}")
    pages: dict[str, dict[str, Any]] = {}
    for child in sorted(resolved.glob("*.json")):
        if child.name == "manifest.json":
            continue
        payload = json.loads(child.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReviewError(f"Annotation is not an object: {child.name}")
        pages[child.stem] = payload
    if not pages:
        raise ReviewError("No annotation JSON files found")
    return pages


def load_decisions(args: argparse.Namespace) -> tuple[set[str], set[str]]:
    accept = {str(item) for item in args.accept or [] if str(item).strip()}
    reject = {str(item) for item in args.reject or [] if str(item).strip()}
    if args.decisions is not None:
        payload = json.loads(args.decisions.expanduser().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReviewError("Decisions file must be a JSON object")
        accept.update(
            str(item) for item in payload.get("accept") or [] if str(item).strip()
        )
        reject.update(
            str(item) for item in payload.get("reject") or [] if str(item).strip()
        )
    overlap = accept & reject
    if overlap:
        raise ReviewError("The same page cannot be both accepted and rejected")
    return accept, reject


def progress_report(pages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    draft = 0
    reviewed = 0
    rejected = 0
    empty = 0
    regions = 0
    regions_reviewed = 0
    for payload in pages.values():
        page = load_annotation_document(payload)
        if page.status == REVIEWED_STATUS:
            reviewed += 1
        elif page.status == REJECTED_STATUS:
            rejected += 1
        else:
            draft += 1
        if page.negative or not page.boxes:
            empty += 1
        regions += len(page.boxes)
        regions_reviewed += sum(
            1 for box in page.boxes if box.status == REVIEWED_STATUS
        )
    return {
        "schemaVersion": 1,
        "pages": len(pages),
        "draft": draft,
        "reviewed": reviewed,
        "rejected": rejected,
        "empty": empty,
        "regions": regions,
        "regionsReviewed": regions_reviewed,
        "independence": {
            "detectorDraft": sum(
                1
                for payload in pages.values()
                if str(payload.get("independence") or "") == DETECTOR_DRAFT_INDEPENDENCE
            ),
            "groundTruth": sum(
                1
                for payload in pages.values()
                if str(payload.get("independence") or "") == GROUND_TRUTH_INDEPENDENCE
            ),
        },
        "privacy": {
            "ocrTextPrinted": False,
            "pageIdsPrinted": False,
            "relativeNamesPrinted": False,
        },
    }


def pending_ids(pages: dict[str, dict[str, Any]]) -> list[str]:
    pending: list[str] = []
    for page_id, payload in pages.items():
        page = load_annotation_document(payload, default_page_id=page_id)
        if page.status in {REVIEWED_STATUS, REJECTED_STATUS}:
            continue
        pending.append(page_id)
    return pending


def apply_decisions(
    pages: dict[str, dict[str, Any]],
    *,
    accept: set[str],
    reject: set[str],
) -> dict[str, dict[str, Any]]:
    unknown = (accept | reject) - set(pages)
    if unknown:
        raise ReviewError("Unknown page IDs were supplied")
    updated = {page_id: payload for page_id, payload in pages.items()}
    for page_id in accept:
        updated[page_id] = apply_review_decision(pages[page_id], "accept")
    for page_id in reject:
        updated[page_id] = apply_review_decision(pages[page_id], "reject")
    return updated


def write_pages(
    output: Path,
    pages: dict[str, dict[str, Any]],
    *,
    label: str,
    source_count: int,
    accepted: int,
    rejected: int,
) -> None:
    for page_id, payload in pages.items():
        (output / f"{page_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    counts = progress_report(pages)
    manifest = {
        "schemaVersion": 1,
        "label": label,
        "sourcePages": source_count,
        "accepted": accepted,
        "rejected": rejected,
        "pages": counts["pages"],
        "draft": counts["draft"],
        "reviewed": counts["reviewed"],
        "empty": counts["empty"],
        "regions": counts["regions"],
        "privacy": {
            "gitIgnored": True,
            "ocrTextStored": True,
            "absolutePathsStored": False,
            "ocrTextPrinted": False,
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "summary.json").write_text(
        json.dumps(counts, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run(args: argparse.Namespace) -> int:
    pages = load_page_documents(args.annotations)
    accept, reject = load_decisions(args)
    mutating = bool(accept or reject)
    if mutating and args.output is None:
        raise ReviewError("Accept/reject decisions require --output")
    if args.output is not None and not mutating:
        raise ReviewError("Output requires at least one accept or reject decision")

    if mutating:
        output = require_ignored_empty_output(args.output)
        updated = apply_decisions(pages, accept=accept, reject=reject)
        write_pages(
            output,
            updated,
            label=args.label,
            source_count=len(pages),
            accepted=len(accept),
            rejected=len(reject),
        )
        print(json.dumps(progress_report(updated), ensure_ascii=True))
        return 0

    if args.list_pending:
        for page_id in pending_ids(pages):
            print(page_id)
        return 0

    print(json.dumps(progress_report(pages), ensure_ascii=True))
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except ReviewError as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
