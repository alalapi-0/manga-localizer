from __future__ import annotations

import hashlib
import io
import json
import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont
from sqlalchemy import select

from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageCloudFullPageCandidate,
    PageGeneration,
    PageLineageEvent,
    PageTypesetCandidate,
    PageTypesetReview,
    RegionTranslationCandidate,
    Revision,
    TextRegion,
    new_id,
)
from manga_localizer.imaging.typesetting import (
    default_cjk_font,
    discover_system_fonts,
    typeset_image,
)
from manga_localizer.logging_utils import redact, without_secrets
from manga_localizer.security import atomic_write_bytes, resolve_write_target
from manga_localizer.services.clean_plates import require_current_clean_plate_acceptance
from manga_localizer.services.page_lineage import (
    JobMutationBinding,
    PageLineageConflict,
    _append_event,
    _safe_actor,
    require_image_mutation_lineage,
)
from manga_localizer.services.projects import (
    ProjectError,
    ProjectStore,
    RevisionConflict,
    add_revision,
)
from manga_localizer.services.translations import require_current_translation_acceptance

TYPESET_VISUAL_CHECKS = (
    "original-clean-final-compared",
    "translation-complete",
    "hierarchy-reading-order-preserved",
    "key-art-unobstructed",
    "typography-source-matched",
    "bubble-contained",
    "art-lettering-composition-matched",
    "overflow-free",
)
TYPESET_REJECT_REASONS = {*TYPESET_VISUAL_CHECKS, "multiple-visual-failures"}
TYPESET_ROUTES = {"bubble", "ordinary", "art-lettering", "keep", "ignore"}
TYPESET_PROVIDER = "pillow-g10"
TYPESET_MODEL_VERSION = "g10-typeset-v1"
TYPESET_CONTRACT_VERSION = "g10-typeset-v1"
ART_LETTERING_CONTRACT_VERSION = "g10-art-lettering-v1"
ART_LETTERING_FEATURES = (
    "explicit-installed-chinese-display-font",
    "fill-stroke",
    "rotation",
    "nonuniform-scale",
    "shear-affine",
    "opacity",
    "visual-center",
    "alignment",
    "line-spacing",
)


def _clean_candidate_ids(candidate: Any | None) -> tuple[str | None, str | None]:
    if isinstance(candidate, PageCloudFullPageCandidate):
        return None, candidate.id
    return (candidate.id if candidate is not None else None), None


_BUBBLE_TYPES = {"dialogue", "speech", "thought"}
_ORDINARY_TYPES = {"narration", "title", "background", "sign", "other"}
_DISPLAY_NAME_TOKENS = (
    "bold",
    "heavy",
    "black",
    "medium",
    "display",
    "poster",
    "impact",
    "heiti",
    "hei",
    "w6",
    "w7",
    "w8",
    "w9",
)
_STYLE_KEYS = {
    "fontToken",
    "fontSize",
    "minFontSize",
    "padding",
    "fill",
    "strokeColor",
    "strokeWidth",
    "rotation",
    "scaleX",
    "scaleY",
    "shearX",
    "shearY",
    "opacity",
    "visualCenterX",
    "visualCenterY",
    "align",
    "verticalAlign",
    "lineSpacing",
    "letterSpacing",
    "autoFit",
}
_UNSUPPORTED_ART_KEYS = {"curveWarp", "curve", "warp", "aiProvider", "aiModel"}
_PUBLIC_FONT_TOKEN_RE = re.compile(r"^installed-font-[0-9a-f]{24}$")
_ART_LAYER_MAX_SIDE = 16_384
_ART_LAYER_MAX_PIXELS = 32 * 1024 * 1024
_ART_TOTAL_PIXEL_WORK_BUDGET = 192 * 1024 * 1024
_G10_MAX_FIT_ATTEMPTS = 16
_NORMAL_MAX_REGION_GLYPHS = 2_048
_NORMAL_MAX_PAGE_GLYPHS = 8_192
_PROTECTED_PIXELS_RESTORED_ANOMALY = "keep-ignore-protected-pixels-restored"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as error:
        raise PageLineageConflict(
            "G10 input or candidate file could not be read",
            resource=f"path:{path.name}",
            reason="g10-artifact-unreadable",
        ) from error


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _candidate_relative(generation_id: str, candidate_id: str) -> Path:
    return Path("generated") / "lineage-typesets" / generation_id / f"{candidate_id}.png"


def _candidate_target(store: ProjectStore, generation_id: str, candidate_id: str) -> Path:
    return resolve_write_target(
        store.root,
        _candidate_relative(generation_id, candidate_id),
        protected_roots=(store.source_root,),
    )


def _active(session, image: ImageAsset) -> PageGeneration:
    generation = session.scalar(
        select(PageGeneration).where(
            PageGeneration.image_id == image.id,
            PageGeneration.project_id == image.project_id,
            PageGeneration.state == "active",
        )
    )
    if generation is None:
        raise PageLineageConflict(
            "G10 requires an active page generation",
            resource=f"image:{image.id}",
            reason="active-generation-missing",
        )
    return generation


def _font_supports_chinese(path: Path) -> bool:
    try:
        font = ImageFont.truetype(str(path), size=36)
        masks = [bytes(font.getmask(value, mode="L")) for value in ("中", "文", "永")]
        boxes = [font.getbbox(value) for value in ("中", "文", "永")]
    except (OSError, ValueError):
        return False
    return (
        all(mask and any(mask) for mask in masks)
        and len({hashlib.sha256(mask).hexdigest() for mask in masks}) >= 2
        and all(box[2] > box[0] and box[3] > box[1] for box in boxes)
    )


def _font_record(path: Path, *, role: str) -> dict[str, Any]:
    checksum = _sha256_file(path)
    return {
        "token": f"installed-font-{checksum[:24]}",
        "label": path.stem[:120],
        "capabilityChecksum": _digest(
            {
                "fileChecksum": checksum,
                "role": role,
                "contractVersion": ART_LETTERING_CONTRACT_VERSION,
                "chineseGlyphProbe": "中文永",
            }
        ),
        "fontChecksum": checksum,
        "role": role,
        "path": path,
    }


@lru_cache(maxsize=1)
def installed_typeset_fonts() -> tuple[dict[str, Any], ...]:
    fonts = list(discover_system_fonts())
    regular = default_cjk_font()
    ordered: list[Path] = []
    if regular is not None:
        ordered.append(regular)
    display_candidates = [
        path
        for path in fonts
        if any(token in path.stem.casefold() for token in _DISPLAY_NAME_TOKENS)
    ]
    ordered.extend(display_candidates)
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in ordered:
        resolved = path.resolve()
        if resolved in seen or not _font_supports_chinese(resolved):
            continue
        seen.add(resolved)
        role = (
            "display"
            if any(token in resolved.stem.casefold() for token in _DISPLAY_NAME_TOKENS)
            else "regular"
        )
        records.append(_font_record(resolved, role=role))
        if len(records) >= 32:
            break
    return tuple(records)


def _public_font(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "token": record["token"],
        "label": record["label"],
        "capabilityChecksum": record["capabilityChecksum"],
        "fontChecksum": record["fontChecksum"],
        "role": record["role"],
    }


def _font_catalog_by_token() -> dict[str, dict[str, Any]]:
    return {str(row["token"]): dict(row) for row in installed_typeset_fonts()}


def _regular_font() -> dict[str, Any] | None:
    records = installed_typeset_fonts()
    return next((dict(row) for row in records if row["role"] == "regular"), None) or (
        dict(records[0]) if records else None
    )


def _display_fonts() -> list[dict[str, Any]]:
    return [dict(row) for row in installed_typeset_fonts() if row["role"] == "display"]


def art_lettering_capability() -> dict[str, Any]:
    available = bool(_display_fonts())
    return {
        "available": available,
        "contractVersion": ART_LETTERING_CONTRACT_VERSION,
        "features": list(ART_LETTERING_FEATURES),
        "reason": None if available else "g10-art-lettering-capability-required",
    }


def _canonical_color(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise PageLineageConflict(
            f"G10 {field} is invalid",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    try:
        color = ImageColor.getrgb(value)
    except ValueError as error:
        raise PageLineageConflict(
            f"G10 {field} is invalid",
            resource="typeset-style",
            reason="g10-style-invalid",
        ) from error
    if len(color) != 3:
        raise PageLineageConflict(
            f"G10 {field} must be an opaque RGB color",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    return "#" + "".join(f"{channel:02X}" for channel in color)


def _bounded_number(
    value: object,
    *,
    field: str,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PageLineageConflict(
            f"G10 {field} must be numeric",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise PageLineageConflict(
            f"G10 {field} is outside its supported bounds",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    if integer:
        if not number.is_integer():
            raise PageLineageConflict(
                f"G10 {field} must be an integer",
                resource="typeset-style",
                reason="g10-style-invalid",
            )
        return int(number)
    return number


def _base_style(route: str) -> dict[str, Any]:
    regular = _regular_font()
    displays = _display_fonts()
    font = displays[0] if route == "art-lettering" and displays else regular
    if font is None:
        reason = (
            "g10-art-lettering-capability-required"
            if route == "art-lettering"
            else "g10-typesetting-capability-required"
        )
        raise PageLineageConflict(
            "G10 requires an explicit installed Chinese-capable font",
            resource=f"typeset-route:{route}",
            reason=reason,
        )
    if route == "art-lettering" and font["role"] != "display":
        raise PageLineageConflict(
            "Art lettering requires a proven display font",
            resource="typeset-route:art-lettering",
            reason="g10-art-lettering-capability-required",
        )
    return {
        "fontToken": font["token"],
        "fontChecksum": font["fontChecksum"],
        "fontSize": 48 if route == "art-lettering" else 32,
        "minFontSize": 12 if route == "art-lettering" else 8,
        "padding": 4,
        "fill": "#111111",
        "strokeColor": "#FFFFFF",
        "strokeWidth": 2 if route == "art-lettering" else 1,
        "rotation": 0.0,
        "scaleX": 1.0,
        "scaleY": 1.0,
        "shearX": 0.0,
        "shearY": 0.0,
        "opacity": 1.0,
        "visualCenterX": 0.5,
        "visualCenterY": 0.5,
        "align": "center",
        "lineSpacing": 0.15,
        "letterSpacing": 0.0,
        "autoFit": True,
        "fontSource": (
            "server-display-default" if route == "art-lettering" else "server-regular-default"
        ),
    }


def style_defaults() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for route in ("bubble", "ordinary"):
        try:
            result[route] = _base_style(route)
        except PageLineageConflict:
            result[route] = None
    try:
        result["artLettering"] = _base_style("art-lettering")
    except PageLineageConflict:
        result["artLettering"] = None
    return result


def _normalize_style(route: str, override: object) -> dict[str, Any]:
    if not isinstance(override, dict):
        raise PageLineageConflict(
            "G10 region style must be an object",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    unsupported = set(override) & _UNSUPPORTED_ART_KEYS
    if route == "art-lettering" and unsupported:
        raise PageLineageConflict(
            "Requested art-lettering transform is unsupported",
            resource="typeset-route:art-lettering",
            reason="g10-art-lettering-capability-required",
        )
    if set(override) - _STYLE_KEYS:
        raise PageLineageConflict(
            "G10 region style contains unsupported fields",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    if route in {"bubble", "ordinary"}:
        unsupported_normal = {
            "scaleX": 1.0,
            "scaleY": 1.0,
            "shearX": 0.0,
            "shearY": 0.0,
            "visualCenterX": 0.5,
            "visualCenterY": 0.5,
        }
        if any(
            field in override
            and (
                isinstance(override[field], bool)
                or not isinstance(override[field], (int, float))
                or float(override[field]) != expected
            )
            for field, expected in unsupported_normal.items()
        ):
            raise PageLineageConflict(
                "Ordinary and bubble routes do not support affine or visual-center overrides",
                resource="typeset-style",
                reason="g10-style-invalid",
            )
    if route == "art-lettering" and "letterSpacing" in override:
        value = override["letterSpacing"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != 0:
            raise PageLineageConflict(
                "Art lettering does not support nonzero letter spacing",
                resource="typeset-style",
                reason="g10-art-lettering-capability-required",
            )
    style = _base_style(route)
    catalog = _font_catalog_by_token()
    if "fontToken" in override:
        token = override["fontToken"]
        font = catalog.get(token) if isinstance(token, str) else None
        if font is None or (route == "art-lettering" and font["role"] != "display"):
            reason = (
                "g10-art-lettering-capability-required"
                if route == "art-lettering"
                else "g10-style-invalid"
            )
            raise PageLineageConflict(
                "Selected G10 font is unavailable or incapable",
                resource="typeset-style",
                reason=reason,
            )
        style["fontToken"] = font["token"]
        style["fontChecksum"] = font["fontChecksum"]
        style["fontSource"] = "region-override"
    style["fontSize"] = _bounded_number(
        override.get("fontSize", style["fontSize"]),
        field="fontSize",
        minimum=6,
        maximum=512,
        integer=True,
    )
    style["minFontSize"] = _bounded_number(
        override.get("minFontSize", style["minFontSize"]),
        field="minFontSize",
        minimum=6,
        maximum=512,
        integer=True,
    )
    if style["minFontSize"] > style["fontSize"]:
        raise PageLineageConflict(
            "G10 minFontSize must not exceed fontSize",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    style["padding"] = _bounded_number(
        override.get("padding", style["padding"]),
        field="padding",
        minimum=0,
        maximum=128,
        integer=True,
    )
    style["fill"] = _canonical_color(override.get("fill", style["fill"]), field="fill")
    style["strokeColor"] = _canonical_color(
        override.get("strokeColor", style["strokeColor"]), field="strokeColor"
    )
    style["strokeWidth"] = _bounded_number(
        override.get("strokeWidth", style["strokeWidth"]),
        field="strokeWidth",
        minimum=0,
        maximum=32,
        integer=True,
    )
    for field, minimum, maximum in (
        ("rotation", -180, 180),
        ("scaleX", 0.25, 4),
        ("scaleY", 0.25, 4),
        ("shearX", -1, 1),
        ("shearY", -1, 1),
        ("opacity", 0.05, 1),
        ("visualCenterX", 0, 1),
        ("visualCenterY", 0, 1),
        ("lineSpacing", 0, 3),
        ("letterSpacing", -10, 50),
    ):
        style[field] = _bounded_number(
            override.get(field, style[field]),
            field=field,
            minimum=minimum,
            maximum=maximum,
        )
    align = override.get("align", style["align"])
    if align not in {"start", "center", "end"}:
        raise PageLineageConflict(
            "G10 align is invalid",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    style["align"] = align
    if "verticalAlign" in override:
        vertical_align = override["verticalAlign"]
        if route == "art-lettering" or vertical_align not in {"start", "center", "end"}:
            raise PageLineageConflict(
                "G10 verticalAlign is invalid for this route",
                resource="typeset-style",
                reason="g10-style-invalid",
            )
        style["verticalAlign"] = vertical_align
    auto_fit = override.get("autoFit", style["autoFit"])
    if type(auto_fit) is not bool:
        raise PageLineageConflict(
            "G10 autoFit must be a boolean",
            resource="typeset-style",
            reason="g10-style-invalid",
        )
    style["autoFit"] = auto_fit
    return style


def resolve_typeset_job_options(options: dict[str, Any]) -> dict[str, Any]:
    if set(options) - {"regionStyles", "concurrency"}:
        raise PageLineageConflict(
            "Strict G10 typeset options contain unsupported fields",
            resource="typeset-options",
            reason="g10-options-invalid",
        )
    raw_styles = options.get("regionStyles", {})
    if not isinstance(raw_styles, dict) or len(raw_styles) > 4096:
        raise PageLineageConflict(
            "Strict G10 regionStyles must be a bounded object",
            resource="typeset-options",
            reason="g10-options-invalid",
        )
    styles: dict[str, dict[str, Any]] = {}
    for region_id, value in sorted(raw_styles.items()):
        if not isinstance(region_id, str) or not region_id or len(region_id) > 128:
            raise PageLineageConflict(
                "Strict G10 region style id is invalid",
                resource="typeset-options",
                reason="g10-options-invalid",
            )
        if not isinstance(value, dict):
            raise PageLineageConflict(
                "Strict G10 region style must be an object",
                resource=f"region:{region_id}",
                reason="g10-style-invalid",
            )
        styles[region_id] = dict(value)
    return {"regionStyles": styles, "concurrency": 1}


def sanitized_typeset_job_options(options: dict[str, Any]) -> dict[str, Any]:
    """Scrub secrets while retaining only proven public G10 font capability ids."""
    normalized = resolve_typeset_job_options(options)
    styles = normalized["regionStyles"]
    if any(set(style) - _STYLE_KEYS for style in styles.values()):
        raise PageLineageConflict(
            "Strict G10 region style contains unsupported fields",
            resource="typeset-options",
            reason="g10-style-invalid",
        )
    sanitized = redact(without_secrets(normalized))
    catalog = _font_catalog_by_token()
    for region_id, style in styles.items():
        public_token = style.get("fontToken")
        if public_token is None:
            continue
        if (
            not isinstance(public_token, str)
            or _PUBLIC_FONT_TOKEN_RE.fullmatch(public_token) is None
            or public_token not in catalog
        ):
            raise PageLineageConflict(
                "Strict G10 public font capability token is invalid",
                resource=f"region:{region_id}",
                reason="g10-style-invalid",
            )
        # Generic secret scrubbing intentionally removes every *Token key.
        # Restore only this checksum-derived, catalog-verified public capability.
        sanitized["regionStyles"][region_id]["fontToken"] = public_token
    return sanitized


def _current_bindings(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
) -> dict[str, Any]:
    g9_terminal_checksum, terminal = require_current_translation_acceptance(
        store, session, image, generation
    )
    g8_checksum, clean_path, clean_candidate = require_current_clean_plate_acceptance(
        store, session, image, generation
    )
    if terminal.g8_checksum != g8_checksum:
        raise PageLineageConflict(
            "G10 clean plate is not the exact accepted G9 parent",
            resource=f"image:{image.id}",
            reason="g10-parent-drift",
        )
    clean_checksum = (
        clean_candidate.candidate_checksum
        if clean_candidate is not None
        else _sha256_file(clean_path)
    )
    if _sha256_file(clean_path) != clean_checksum:
        raise PageLineageConflict(
            "G10 clean-plate bytes do not match their accepted checksum",
            resource=f"image:{image.id}",
            reason="g10-clean-plate-drift",
        )
    accepted_ids = list(terminal.accepted_candidate_ids or [])
    accepted_rows = list(
        session.scalars(
            select(RegionTranslationCandidate).where(
                RegionTranslationCandidate.generation_id == generation.id,
                RegionTranslationCandidate.id.in_(accepted_ids or ["__none__"]),
            )
        ).all()
    )
    accepted_by_region = {row.region_id: row for row in accepted_rows}
    if len(accepted_rows) != len(accepted_ids) or {row.id for row in accepted_rows} != set(
        accepted_ids
    ):
        raise PageLineageConflict(
            "G10 accepted translation candidate set is incomplete",
            resource=f"page-generation:{generation.id}",
            reason="g10-g9-parent-invalid",
        )
    return {
        "g8Checksum": g8_checksum,
        "g9TerminalChecksum": g9_terminal_checksum,
        "translationStateChecksum": terminal.translation_state_checksum,
        "cleanPlatePath": clean_path,
        "cleanPlateCandidate": clean_candidate,
        "cleanPlateChecksum": clean_checksum,
        "acceptedByRegion": accepted_by_region,
    }


def _route_and_region_manifests(
    session,
    image: ImageAsset,
    accepted_by_region: Mapping[str, RegionTranslationCandidate],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = list(
        session.scalars(
            select(TextRegion)
            .where(TextRegion.image_id == image.id)
            .order_by(TextRegion.reading_order, TextRegion.id)
        ).all()
    )
    region_manifest: list[dict[str, Any]] = []
    route_manifest: list[dict[str, Any]] = []
    for row in rows:
        if row.region_type == "ruby" or row.content_disposition == "false-positive":
            continue
        disposition = row.content_disposition
        if disposition not in {"translate", "redraw-art", "keep-art", "ignore"}:
            raise PageLineageConflict(
                "G10 encountered an unreviewed content disposition",
                resource=f"region:{row.id}",
                reason="g10-region-disposition-unknown",
            )
        translation = accepted_by_region.get(row.id)
        if disposition in {"translate", "redraw-art"} and translation is None:
            raise PageLineageConflict(
                "G10 route lacks an accepted translation candidate",
                resource=f"region:{row.id}",
                reason="g10-translation-binding-missing",
            )
        if disposition in {"keep-art", "ignore"} and translation is not None:
            raise PageLineageConflict(
                "G10 excluded route unexpectedly has a translation candidate",
                resource=f"region:{row.id}",
                reason="g10-route-invalid",
            )
        if disposition == "redraw-art":
            route = "art-lettering"
        elif disposition == "keep-art":
            route = "keep"
        elif disposition == "ignore":
            route = "ignore"
        elif row.region_type in _BUBBLE_TYPES:
            route = "bubble"
        elif row.region_type in _ORDINARY_TYPES:
            route = "ordinary"
        else:
            raise PageLineageConflict(
                "G10 cannot route an unknown translate semantic type",
                resource=f"region:{row.id}",
                reason="g10-unknown-translate-type",
            )
        geometry = {
            "x": float(row.x),
            "y": float(row.y),
            "width": float(row.width),
            "height": float(row.height),
            "rotation": float(row.rotation),
        }
        if (
            not all(math.isfinite(value) for value in geometry.values())
            or geometry["x"] < 0
            or geometry["y"] < 0
            or geometry["width"] <= 0
            or geometry["height"] <= 0
            or geometry["x"] + geometry["width"] > image.width
            or geometry["y"] + geometry["height"] > image.height
        ):
            raise PageLineageConflict(
                "G10 region geometry is outside the accepted page grid",
                resource=f"region:{row.id}",
                reason="g10-region-geometry-invalid",
            )
        candidate_id = translation.id if translation is not None else None
        candidate_checksum = translation.candidate_checksum if translation is not None else None
        region_manifest.append(
            {
                "regionId": row.id,
                "regionRevision": int(row.revision),
                "geometry": geometry,
                "readingOrder": int(row.reading_order),
                "regionType": row.region_type,
                "direction": row.direction,
                "paragraphGroupId": row.paragraph_group_id,
                "contentDisposition": disposition,
                "acceptedTranslationCandidateId": candidate_id,
                "acceptedTranslationCandidateChecksum": candidate_checksum,
            }
        )
        route_manifest.append(
            {
                "regionId": row.id,
                "readingOrder": int(row.reading_order),
                "route": route,
                "renderRequired": route in {"bubble", "ordinary", "art-lettering"},
                "translationCandidateId": candidate_id,
                "translationCandidateChecksum": candidate_checksum,
            }
        )
    if set(accepted_by_region) != {
        entry["regionId"]
        for entry in route_manifest
        if entry["route"] in {"bubble", "ordinary", "art-lettering"}
    }:
        raise PageLineageConflict(
            "G10 route manifest does not exactly consume G9 candidates",
            resource=f"image:{image.id}",
            reason="g10-route-invalid",
        )
    if not route_manifest:
        raise PageLineageConflict(
            "G10 cannot typeset a page whose reviewed region set is empty",
            resource=f"image:{image.id}",
            reason="g10-route-manifest-empty",
        )
    return region_manifest, route_manifest


def _style_manifest(
    route_manifest: list[dict[str, Any]],
    raw_overrides: object,
) -> list[dict[str, Any]]:
    overrides = raw_overrides if isinstance(raw_overrides, dict) else {}
    route_by_id = {entry["regionId"]: entry["route"] for entry in route_manifest}
    if set(overrides) - set(route_by_id):
        raise PageLineageConflict(
            "G10 regionStyles targets an excluded or missing region",
            resource="typeset-options",
            reason="g10-style-target-invalid",
        )
    if any(route_by_id[region_id] in {"keep", "ignore"} for region_id in overrides):
        raise PageLineageConflict(
            "G10 keep/ignore routes cannot receive rendering styles",
            resource="typeset-options",
            reason="g10-style-target-invalid",
        )
    result: list[dict[str, Any]] = []
    for route_entry in route_manifest:
        region_id = route_entry["regionId"]
        route = route_entry["route"]
        style = (
            _normalize_style(route, overrides.get(region_id, {}))
            if route_entry["renderRequired"]
            else None
        )
        result.append({"regionId": region_id, "route": route, "style": style})
    return result


def _job_contract(
    session,
    image: ImageAsset,
    bindings: Mapping[str, Any],
    options: Mapping[str, Any],
) -> dict[str, Any]:
    region_manifest, route_manifest = _route_and_region_manifests(
        session, image, bindings["acceptedByRegion"]
    )
    style_manifest = _style_manifest(route_manifest, options.get("regionStyles", {}))
    route_checksum = _digest(route_manifest)
    style_checksum = _digest(style_manifest)
    parameter_hash = _digest(
        {
            "contractVersion": TYPESET_CONTRACT_VERSION,
            "provider": TYPESET_PROVIDER,
            "modelVersion": TYPESET_MODEL_VERSION,
            "g9TerminalChecksum": bindings["g9TerminalChecksum"],
            "translationStateChecksum": bindings["translationStateChecksum"],
            "cleanPlateChecksum": bindings["cleanPlateChecksum"],
            "regionManifest": region_manifest,
            "routeChecksum": route_checksum,
            "styleChecksum": style_checksum,
        }
    )
    return {
        "regionManifest": region_manifest,
        "routeManifest": route_manifest,
        "routeChecksum": route_checksum,
        "styleManifest": style_manifest,
        "styleChecksum": style_checksum,
        "parameterHash": parameter_hash,
        "provider": TYPESET_PROVIDER,
        "modelVersion": TYPESET_MODEL_VERSION,
    }


def _render_scale(image: ImageAsset, clean_path: Path) -> tuple[float, int, int]:
    try:
        with Image.open(clean_path) as opened:
            opened.load()
            width, height = opened.size
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "Accepted G10 clean plate could not be decoded",
            resource=f"image:{image.id}",
            reason="g10-clean-plate-invalid",
        ) from error
    if image.width <= 0 or image.height <= 0:
        raise PageLineageConflict(
            "G10 source grid is invalid",
            resource=f"image:{image.id}",
            reason="g10-grid-invalid",
        )
    scale_x = width / image.width
    scale_y = height / image.height
    if not math.isclose(scale_x, scale_y, rel_tol=0, abs_tol=1e-9) or scale_x not in {
        1.0,
        2.0,
        3.0,
        4.0,
    }:
        raise PageLineageConflict(
            "G10 clean plate does not use a supported exact render grid",
            resource=f"image:{image.id}",
            reason="g10-grid-invalid",
        )
    return scale_x, width, height


def _font_path(style: Mapping[str, Any], *, route: str) -> Path:
    record = _font_catalog_by_token().get(str(style.get("fontToken")))
    if (
        record is None
        or record["fontChecksum"] != style.get("fontChecksum")
        or (route == "art-lettering" and record["role"] != "display")
        or _sha256_file(record["path"]) != record["fontChecksum"]
    ):
        reason = (
            "g10-art-lettering-capability-required"
            if route == "art-lettering"
            else "g10-typesetting-capability-required"
        )
        raise PageLineageConflict(
            "G10 frozen font capability is no longer available",
            resource=f"typeset-route:{route}",
            reason=reason,
        )
    return record["path"]


def _scaled_geometry(entry: Mapping[str, Any], scale: float) -> dict[str, Any]:
    geometry = entry["geometry"]
    return {
        "id": entry["regionId"],
        "x": float(geometry["x"]) * scale,
        "y": float(geometry["y"]) * scale,
        "width": float(geometry["width"]) * scale,
        "height": float(geometry["height"]) * scale,
        "rotation": float(geometry["rotation"]),
        "direction": entry["direction"],
        "order": int(entry["readingOrder"]),
        "paragraphGroupId": entry["paragraphGroupId"],
    }


def _scaled_normal_style(style: Mapping[str, Any], scale: float, *, route: str) -> dict[str, Any]:
    return {
        "fontPath": str(_font_path(style, route=route)),
        "fontSize": max(1, round(float(style["fontSize"]) * scale)),
        "minFontSize": max(1, round(float(style["minFontSize"]) * scale)),
        "padding": max(0, round(float(style["padding"]) * scale)),
        "fill": style["fill"],
        "strokeColor": style["strokeColor"],
        "strokeWidth": max(0, round(float(style["strokeWidth"]) * scale)),
        "opacity": style["opacity"],
        "align": style["align"],
        "verticalAlign": style.get("verticalAlign", "start"),
        "lineSpacing": style["lineSpacing"],
        "letterSpacing": float(style["letterSpacing"]) * scale,
        "autoFit": style["autoFit"],
        # This is an internal G10 renderer control, never a persisted style
        # field. It keeps the legacy Pillow fit helper on a deterministic
        # logarithmic work bound for the strict route.
        "_g10FitAttemptLimit": _G10_MAX_FIT_ATTEMPTS,
    }


def _measure_art_text(
    text: str,
    *,
    font_path: Path,
    font_size: int,
    direction: str,
    fill: str,
    stroke_color: str,
    stroke_width: int,
    letter_spacing: float,
    line_spacing: float,
    align: str,
) -> dict[str, Any]:
    compact = text.replace("\n", "")
    margin = stroke_width + 3
    cell = max(1, math.ceil(font_size * (1 + line_spacing) + abs(letter_spacing)))
    if direction == "vertical":
        estimated_width = font_size * 3 + margin * 2
        estimated_height = len(compact) * cell + margin * 2
    else:
        lines = text.splitlines() or [""]
        estimated_width = (
            max((len(line) for line in lines), default=0)
            * max(1, font_size * 3 + math.ceil(abs(letter_spacing)))
            + margin * 2
        )
        estimated_height = len(lines) * cell + margin * 2
    _guard_art_layer(estimated_width, estimated_height)
    font = ImageFont.truetype(str(font_path), size=max(1, font_size))
    rendered = "\n".join(compact) if direction == "vertical" else text
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    spacing = max(0, round(font_size * line_spacing + letter_spacing))
    pillow_align = {"start": "left", "center": "center", "end": "right"}[align]
    bbox = probe.multiline_textbbox(
        (0, 0),
        rendered,
        font=font,
        spacing=spacing,
        stroke_width=stroke_width,
        align=pillow_align,
    )
    width = max(1, math.ceil(bbox[2] - bbox[0]))
    height = max(1, math.ceil(bbox[3] - bbox[1]))
    layer_width = width + margin * 2
    layer_height = height + margin * 2
    _guard_art_layer(layer_width, layer_height)
    return {
        "font": font,
        "rendered": rendered,
        "spacing": spacing,
        "align": pillow_align,
        "bbox": bbox,
        "margin": margin,
        "width": layer_width,
        "height": layer_height,
    }


def _draw_art_text(
    text: str,
    *,
    font_path: Path,
    font_size: int,
    direction: str,
    fill: str,
    stroke_color: str,
    stroke_width: int,
    letter_spacing: float,
    line_spacing: float,
    align: str,
    _measurement: Mapping[str, Any] | None = None,
) -> Image.Image:
    measurement = dict(
        _measurement
        or _measure_art_text(
            text,
            font_path=font_path,
            font_size=font_size,
            direction=direction,
            fill=fill,
            stroke_color=stroke_color,
            stroke_width=stroke_width,
            letter_spacing=letter_spacing,
            line_spacing=line_spacing,
            align=align,
        )
    )
    font = measurement["font"]
    rendered = str(measurement["rendered"])
    spacing = int(measurement["spacing"])
    pillow_align = str(measurement["align"])
    bbox = measurement["bbox"]
    margin = int(measurement["margin"])
    layer_width = int(measurement["width"])
    layer_height = int(measurement["height"])
    layer = Image.new("RGBA", (layer_width, layer_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.multiline_text(
        (margin - bbox[0], margin - bbox[1]),
        rendered,
        font=font,
        fill=ImageColor.getcolor(fill, "RGBA"),
        stroke_width=stroke_width,
        stroke_fill=ImageColor.getcolor(stroke_color, "RGBA"),
        spacing=spacing,
        align=pillow_align,
    )
    return layer


def _guard_art_layer(width: int, height: int) -> None:
    if (
        width <= 0
        or height <= 0
        or width > _ART_LAYER_MAX_SIDE
        or height > _ART_LAYER_MAX_SIDE
        or width * height > _ART_LAYER_MAX_PIXELS
    ):
        raise PageLineageConflict(
            "G10 art-lettering layer exceeds the deterministic render budget",
            resource="typeset-route:art-lettering",
            reason="g10-art-lettering-resource-limit",
        )


def _art_transform_plan(
    width: int,
    height: int,
    style: Mapping[str, Any],
    *,
    rotation: float,
) -> tuple[tuple[int, int], tuple[int, int] | None, tuple[int, int] | None]:
    width = max(1, round(width * float(style["scaleX"])))
    height = max(1, round(height * float(style["scaleY"])))
    _guard_art_layer(width, height)
    resized = (width, height)
    shear_x = float(style["shearX"])
    shear_y = float(style["shearY"])
    sheared: tuple[int, int] | None = None
    if shear_x or shear_y:
        output_width = max(1, math.ceil(width + abs(shear_x) * height))
        output_height = max(1, math.ceil(height + abs(shear_y) * width))
        _guard_art_layer(output_width, output_height)
        sheared = (output_width, output_height)
        width, height = sheared
    rotated: tuple[int, int] | None = None
    if rotation:
        radians = math.radians(rotation)
        rotated_width = math.ceil(abs(width * math.cos(radians)) + abs(height * math.sin(radians)))
        rotated_height = math.ceil(abs(width * math.sin(radians)) + abs(height * math.cos(radians)))
        # Pillow's expand calculation can add a rounding pixel on either side.
        _guard_art_layer(rotated_width + 2, rotated_height + 2)
        rotated = (rotated_width + 2, rotated_height + 2)
    return resized, sheared, rotated


def _art_transform(layer: Image.Image, style: Mapping[str, Any], *, rotation: float) -> Image.Image:
    resized, sheared, _rotated = _art_transform_plan(
        layer.width,
        layer.height,
        style,
        rotation=rotation,
    )
    transformed = layer.resize(resized, resample=Image.Resampling.BICUBIC)
    shear_x = float(style["shearX"])
    shear_y = float(style["shearY"])
    if sheared is not None:
        output_width, output_height = sheared
        resized_width, resized_height = resized
        offset_x = max(0.0, shear_x * resized_height)
        offset_y = max(0.0, shear_y * resized_width)
        transformed = transformed.transform(
            (output_width, output_height),
            Image.Transform.AFFINE,
            (1.0, -shear_x, offset_x, -shear_y, 1.0, offset_y),
            resample=Image.Resampling.BICUBIC,
        )
    if rotation:
        transformed = transformed.rotate(
            -rotation,
            resample=Image.Resampling.BICUBIC,
            expand=True,
        )
    opacity = float(style["opacity"])
    if opacity < 1:
        transformed.putalpha(
            transformed.getchannel("A").point(lambda value: round(value * opacity))
        )
    return transformed


def _render_art_region(
    canvas: Image.Image,
    *,
    text: str,
    region: Mapping[str, Any],
    style: Mapping[str, Any],
    scale: float,
    work_budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    font_path = _font_path(style, route="art-lettering")
    geometry = _scaled_geometry(region, scale)
    target_width = max(1, round(float(geometry["width"])))
    target_height = max(1, round(float(geometry["height"])))
    padding = max(0, round(float(style["padding"]) * scale))
    max_size = max(1, round(float(style["fontSize"]) * scale))
    min_size = max(1, round(float(style["minFontSize"]) * scale))
    selected: Image.Image | None = None
    selected_size = max_size
    overflow = True
    fallback: Image.Image | None = None
    budget = work_budget if work_budget is not None else {"remaining": _ART_TOTAL_PIXEL_WORK_BUDGET}
    if type(budget.get("remaining")) is not int or budget["remaining"] < 0:
        raise ValueError("G10 art work budget must contain a non-negative integer remainder")
    attempts = 0
    combined_rotation = float(geometry["rotation"]) + float(style["rotation"])
    available_width = max(1, target_width - padding * 2)
    available_height = max(1, target_height - padding * 2)
    draw_parameters = {
        "text": text,
        "font_path": font_path,
        "direction": str(geometry["direction"]),
        "fill": str(style["fill"]),
        "stroke_color": str(style["strokeColor"]),
        "stroke_width": max(0, round(float(style["strokeWidth"]) * scale)),
        "letter_spacing": float(style["letterSpacing"]) * scale,
        "line_spacing": float(style["lineSpacing"]),
        "align": str(style["align"]),
    }

    def evaluate(font_size: int) -> tuple[Image.Image | None, bool]:
        nonlocal attempts
        if attempts >= _G10_MAX_FIT_ATTEMPTS:
            raise PageLineageConflict(
                "G10 art-lettering fitting exhausted its deterministic work budget",
                resource="typeset-route:art-lettering",
                reason="g10-art-lettering-resource-limit",
            )
        attempts += 1
        try:
            measurement = _measure_art_text(font_size=font_size, **draw_parameters)
            plan = _art_transform_plan(
                int(measurement["width"]),
                int(measurement["height"]),
                style,
                rotation=combined_rotation,
            )
        except PageLineageConflict as error:
            if error.reason != "g10-art-lettering-resource-limit":
                raise
            return None, True
        estimated_work = int(measurement["width"]) * int(measurement["height"])
        estimated_work += sum(
            dimensions[0] * dimensions[1] for dimensions in plan if dimensions is not None
        )
        if estimated_work > budget["remaining"]:
            raise PageLineageConflict(
                "G10 art-lettering fitting exhausted its deterministic work budget",
                resource="typeset-route:art-lettering",
                reason="g10-art-lettering-resource-limit",
            )
        budget["remaining"] -= estimated_work
        raw = _draw_art_text(
            font_size=font_size,
            _measurement=measurement,
            **draw_parameters,
        )
        try:
            candidate = _art_transform(raw, style, rotation=combined_rotation)
        finally:
            raw.close()
        candidate_overflow = (
            candidate.width > available_width or candidate.height > available_height
        )
        return candidate, candidate_overflow

    if not style["autoFit"]:
        selected, overflow = evaluate(max_size)
        if selected is None:
            raise PageLineageConflict(
                "G10 art-lettering layer exceeds the deterministic render budget",
                resource="typeset-route:art-lettering",
                reason="g10-art-lettering-resource-limit",
            )
    else:
        low, high = min_size, max_size
        while low <= high:
            font_size = (low + high) // 2
            candidate, candidate_overflow = evaluate(font_size)
            if candidate is None or candidate_overflow:
                if font_size == min_size and candidate is not None:
                    if fallback is not None:
                        fallback.close()
                    fallback = candidate
                elif candidate is not None:
                    candidate.close()
                high = font_size - 1
                continue
            if selected is not None:
                selected.close()
            selected = candidate
            selected_size = font_size
            overflow = False
            low = font_size + 1
        if selected is None:
            selected = fallback
            selected_size = min_size
            overflow = True
        elif fallback is not None:
            fallback.close()
    if selected is None:
        raise PageLineageConflict(
            "G10 art-lettering cannot produce a bounded minimum-size layer",
            resource="typeset-route:art-lettering",
            reason="g10-art-lettering-resource-limit",
        )
    assert selected is not None
    center_x = float(geometry["x"]) + target_width * float(style["visualCenterX"])
    center_y = float(geometry["y"]) + target_height * float(style["visualCenterY"])
    if style["align"] == "start":
        center_x -= max(0.0, target_width - selected.width - padding * 2) / 2
    elif style["align"] == "end":
        center_x += max(0.0, target_width - selected.width - padding * 2) / 2
    selected_width = selected.width
    selected_height = selected.height
    left = round(center_x - selected_width / 2)
    top = round(center_y - selected_height / 2)
    if (
        left < 0
        or top < 0
        or left + selected_width > canvas.width
        or top + selected_height > canvas.height
    ):
        overflow = True
    canvas.alpha_composite(selected, (left, top))
    selected.close()
    return {
        "regionId": region["regionId"],
        "route": "art-lettering",
        "bounds": {
            "x": left,
            "y": top,
            "width": selected_width,
            "height": selected_height,
        },
        "fontSize": selected_size,
        "overflow": overflow,
        "direction": geometry["direction"],
        "rotation": combined_rotation,
        "scaleX": float(style["scaleX"]),
        "scaleY": float(style["scaleY"]),
        "shearX": float(style["shearX"]),
        "shearY": float(style["shearY"]),
        "opacity": float(style["opacity"]),
        "visualCenterX": float(style["visualCenterX"]),
        "visualCenterY": float(style["visualCenterY"]),
        "align": style["align"],
    }


def _protected_route_mask(
    size: tuple[int, int],
    *,
    region_manifest: list[dict[str, Any]],
    route_manifest: list[dict[str, Any]],
    scale: float,
) -> Image.Image:
    """Rasterize the exact rotated keep/ignore coverage on the render grid."""
    protected = Image.new("L", size, 0)
    regions_by_id = {entry["regionId"]: entry for entry in region_manifest}
    for route_entry in route_manifest:
        if route_entry["route"] not in {"keep", "ignore"}:
            continue
        geometry = _scaled_geometry(regions_by_id[route_entry["regionId"]], scale)
        width = max(1, round(float(geometry["width"])))
        height = max(1, round(float(geometry["height"])))
        local = Image.new("L", (width, height), 255)
        rotation = float(geometry["rotation"])
        if rotation:
            rotated = local.rotate(
                -rotation,
                resample=Image.Resampling.NEAREST,
                expand=True,
                fillcolor=0,
            )
            local.close()
            local = rotated
        center_x = float(geometry["x"]) + float(geometry["width"]) / 2
        center_y = float(geometry["y"]) + float(geometry["height"]) / 2
        position = (
            round(center_x - local.width / 2),
            round(center_y - local.height / 2),
        )
        # Masked color paste is a deterministic max/union because the local
        # nearest-neighbor mask contains only 0 and 255. Pillow clips negative
        # or over-page positions to the destination image boundary.
        protected.paste(255, position, local)
        local.close()
    return protected


def _rgba_difference_mask(left: Image.Image, right: Image.Image) -> Image.Image:
    """Merge differences from all four RGBA channels into one L mask."""
    difference = ImageChops.difference(left, right)
    channels = difference.split()
    combined = ImageChops.lighter(channels[0], channels[1])
    next_combined = ImageChops.lighter(combined, channels[2])
    combined.close()
    combined = ImageChops.lighter(next_combined, channels[3])
    next_combined.close()
    difference.close()
    for channel in channels:
        channel.close()
    return combined


def _render_candidate(
    *,
    clean_path: Path,
    region_manifest: list[dict[str, Any]],
    route_manifest: list[dict[str, Any]],
    style_manifest: list[dict[str, Any]],
    accepted_by_region: Mapping[str, RegionTranslationCandidate],
    scale: float,
) -> tuple[bytes, list[dict[str, Any]], list[str], list[str]]:
    try:
        with Image.open(clean_path) as opened:
            opened.load()
            canvas = opened.convert("RGBA")
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "G10 clean plate could not be rendered",
            resource="typeset-candidate",
            reason="g10-clean-plate-invalid",
        ) from error
    has_protected_routes = any(entry["route"] in {"keep", "ignore"} for entry in route_manifest)
    clean_parent = canvas.copy() if has_protected_routes else None
    regions_by_id = {entry["regionId"]: entry for entry in region_manifest}
    styles_by_id = {entry["regionId"]: entry for entry in style_manifest}
    normal_regions: list[dict[str, Any]] = []
    normal_routes: dict[str, str] = {}
    art_routes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    normal_glyph_count = 0
    for route_entry in route_manifest:
        route = route_entry["route"]
        if route in {"keep", "ignore"}:
            continue
        region_id = route_entry["regionId"]
        translation = accepted_by_region[region_id]
        region = regions_by_id[region_id]
        style = styles_by_id[region_id]["style"]
        if not isinstance(style, dict):
            raise PageLineageConflict(
                "G10 render route has no frozen style",
                resource=f"region:{region_id}",
                reason="g10-style-invalid",
            )
        if route == "art-lettering":
            art_routes.append((region, style))
            continue
        glyph_count = len(translation.translation_text)
        normal_glyph_count += glyph_count
        if glyph_count > _NORMAL_MAX_REGION_GLYPHS or normal_glyph_count > _NORMAL_MAX_PAGE_GLYPHS:
            raise PageLineageConflict(
                "G10 ordinary typesetting exceeds its deterministic glyph-work budget",
                resource=f"region:{region_id}",
                reason="g10-typesetting-resource-limit",
            )
        render_region = _scaled_geometry(region, scale)
        render_region["rotation"] = float(render_region["rotation"]) + float(style["rotation"])
        render_region["translationText"] = translation.translation_text
        render_region["style"] = _scaled_normal_style(style, scale, route=route)
        normal_regions.append(render_region)
        normal_routes[region_id] = route
    layouts: list[dict[str, Any]] = []
    if normal_regions:
        # Strict G10 binds one accepted translation to one frozen region. The
        # legacy renderer may merge adjacent fragment boxes and redistribute
        # their text, so invoke it per region to prohibit cross-region and
        # cross-paragraph clustering while retaining its deterministic drawing.
        normal_layouts: list[dict[str, Any]] = []
        for render_region in normal_regions:
            normal_result = typeset_image(canvas, [render_region], geometry_scale=scale)
            if (
                len(normal_result.layouts) != 1
                or normal_result.layouts[0].get("regionId") != render_region["id"]
            ):
                raise PageLineageConflict(
                    "G10 ordinary renderer did not preserve its exact region binding",
                    resource=f"region:{render_region['id']}",
                    reason="g10-render-contract-invalid",
                )
            canvas = normal_result.image
            normal_layouts.append(normal_result.layouts[0])
        for raw in normal_layouts:
            region_id = str(raw.get("regionId"))
            region = regions_by_id[region_id]
            style = styles_by_id[region_id]["style"]
            geometry = _scaled_geometry(region, scale)
            layouts.append(
                {
                    "regionId": region_id,
                    "route": normal_routes[region_id],
                    "bounds": {
                        "x": round(float(geometry["x"])),
                        "y": round(float(geometry["y"])),
                        "width": round(float(geometry["width"])),
                        "height": round(float(geometry["height"])),
                    },
                    "fontSize": int(raw["fontSize"]),
                    "overflow": bool(raw["overflow"]),
                    "direction": raw["direction"],
                    "rotation": float(geometry["rotation"]) + float(style["rotation"]),
                    "scaleX": float(style["scaleX"]),
                    "scaleY": float(style["scaleY"]),
                    "shearX": float(style["shearX"]),
                    "shearY": float(style["shearY"]),
                    "opacity": float(style["opacity"]),
                    "visualCenterX": float(style["visualCenterX"]),
                    "visualCenterY": float(style["visualCenterY"]),
                    "align": style["align"],
                }
            )
    art_work_budget = {"remaining": _ART_TOTAL_PIXEL_WORK_BUDGET}
    for region, style in art_routes:
        translation = accepted_by_region[region["regionId"]]
        layouts.append(
            _render_art_region(
                canvas,
                text=translation.translation_text,
                region=region,
                style=style,
                scale=scale,
                work_budget=art_work_budget,
            )
        )
    order = {entry["regionId"]: index for index, entry in enumerate(route_manifest)}
    layouts.sort(key=lambda entry: order[entry["regionId"]])
    overflow_ids = [entry["regionId"] for entry in layouts if entry["overflow"]]
    anomalies: list[str] = []
    if clean_parent is not None:
        protected_mask = _protected_route_mask(
            canvas.size,
            region_manifest=region_manifest,
            route_manifest=route_manifest,
            scale=scale,
        )
        difference_mask = _rgba_difference_mask(canvas, clean_parent)
        protected_difference = ImageChops.multiply(difference_mask, protected_mask)
        if protected_difference.getbbox() is not None:
            anomalies.append(_PROTECTED_PIXELS_RESTORED_ANOMALY)
        # Restore all four parent channels under the binary coverage mask even
        # if no difference was observed, making the invariant unconditional.
        canvas.paste(clean_parent, (0, 0), protected_mask)
        protected_difference.close()
        difference_mask.close()
        protected_mask.close()
        clean_parent.close()
    payload = _png_bytes(canvas)
    return payload, layouts, overflow_ids, anomalies


def _candidate_state_payload(row: PageTypesetCandidate) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "sequence": row.sequence,
        "jobId": row.job_id,
        "jobItemId": row.job_item_id,
        "parentChecksum": row.parent_checksum,
        "g9TerminalChecksum": row.g9_terminal_checksum,
        "translationStateChecksum": row.translation_state_checksum,
        "cleanPlateCandidateId": row.clean_plate_candidate_id,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "regionManifest": row.region_manifest,
        "routeManifest": row.route_manifest,
        "routeChecksum": row.route_checksum,
        "styleManifest": row.style_manifest,
        "styleChecksum": row.style_checksum,
        "layoutManifest": row.layout_manifest,
        "layoutChecksum": row.layout_checksum,
        "provider": row.provider,
        "modelVersion": row.model_version,
        "parameterHash": row.parameter_hash,
        "candidateChecksum": row.candidate_checksum,
        "relativePath": row.relative_path,
        "width": row.width,
        "height": row.height,
        "renderScale": row.render_scale,
        "overflowRegionIds": row.overflow_region_ids,
        "anomalies": row.anomalies,
        "revisionId": row.revision_id,
    }
    # Preserve byte-for-byte legacy replay hashes.  The additive route identity
    # is present only for cloud candidates and therefore cannot perturb an old
    # generation whose new column is NULL.
    if row.cloud_full_page_candidate_id is not None:
        payload["cloudFullPageCandidateId"] = row.cloud_full_page_candidate_id
    return payload


def _review_state_payload(row: PageTypesetReview) -> dict[str, Any]:
    return {
        "id": row.id,
        "sequence": row.sequence,
        "candidateId": row.candidate_id,
        "state": row.state,
        "reason": row.reason,
        "parentChecksum": row.parent_checksum,
        "candidateChecksum": row.candidate_checksum,
        "routeChecksum": row.route_checksum,
        "styleChecksum": row.style_checksum,
        "layoutChecksum": row.layout_checksum,
        "g9TerminalChecksum": row.g9_terminal_checksum,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "observedWidth": row.observed_width,
        "observedHeight": row.observed_height,
        "observedRenderScale": row.observed_render_scale,
        "checks": row.checks,
        "reviewer": row.reviewer,
        "revisionId": row.revision_id,
    }


def _valid_visual_checks(value: object) -> bool:
    return bool(
        isinstance(value, list)
        and len(value) == len(TYPESET_VISUAL_CHECKS)
        and all(
            isinstance(entry, dict)
            and set(entry) == {"check", "passed"}
            and entry.get("check") == TYPESET_VISUAL_CHECKS[index]
            and type(entry.get("passed")) is bool
            for index, entry in enumerate(value)
        )
    )


def typeset_state_checksum(
    g9_terminal_checksum: str,
    candidates: list[PageTypesetCandidate],
    reviews: list[PageTypesetReview],
) -> str:
    if not candidates and not reviews:
        return g9_terminal_checksum
    return _digest(
        {
            "contractVersion": TYPESET_CONTRACT_VERSION,
            "g9TerminalChecksum": g9_terminal_checksum,
            "candidates": [_candidate_state_payload(row) for row in candidates],
            "reviews": [_review_state_payload(row) for row in reviews],
        }
    )


def _revision_matches(
    session,
    generation: PageGeneration,
    revision_id: str,
    *,
    entity_type: str,
    entity_id: str,
    operation: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    revision = session.get(Revision, revision_id)
    return bool(
        revision is not None
        and revision.project_id == generation.project_id
        and revision.entity_type == entity_type
        and revision.entity_id == entity_id
        and revision.operation == operation
        and revision.before == before
        and revision.after == after
    )


def _job_actor(job: Job) -> dict[str, str | None]:
    context = job.lineage_context
    if not isinstance(context, dict) or not isinstance(context.get("actor"), dict):
        raise PageLineageConflict(
            "G10 job actor is missing",
            resource=f"job:{job.id}",
            reason="g10-replay-invalid",
        )
    return _safe_actor(context["actor"])


def _event_actor(event: PageLineageEvent) -> dict[str, str | None]:
    return _safe_actor(
        {
            "actorKind": event.actor_kind,
            "actorId": event.actor_id,
            "taskId": event.task_id,
            "threadId": event.thread_id,
            "sessionId": event.session_id,
            "operationSource": event.operation_source,
        }
    )


def _job_expected_sequence(job: Job, generation: PageGeneration) -> int:
    pages = (job.lineage_context or {}).get("pages")
    if not isinstance(pages, list):
        raise PageLineageConflict(
            "G10 job page binding is invalid",
            resource=f"job:{job.id}",
            reason="g10-replay-invalid",
        )
    matches = [
        page
        for page in pages
        if isinstance(page, dict)
        and page.get("imageId") == generation.image_id
        and page.get("pageGenerationId") == generation.id
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("expectedSequence"), int):
        raise PageLineageConflict(
            "G10 job page binding is invalid",
            resource=f"job:{job.id}",
            reason="g10-replay-invalid",
        )
    return int(matches[0]["expectedSequence"])


def validate_g10_prefix_after_g9(
    session,
    generation: PageGeneration,
    *,
    g9_terminal_checksum: str,
) -> None:
    """Validate downstream operation grammar without recursively replaying G9."""

    terminal_event = session.scalar(
        select(PageLineageEvent)
        .where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.operation == "translation-stage-review",
            PageLineageEvent.output_checksum == g9_terminal_checksum,
        )
        .order_by(PageLineageEvent.sequence.desc())
        .limit(1)
    )
    if terminal_event is None:
        raise PageLineageConflict(
            "G10 prefix has no exact G9 boundary",
            resource=f"page-generation:{generation.id}",
            reason="g9-replay-invalid",
        )
    events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.sequence > terminal_event.sequence,
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    state = "idle"
    current_item: str | None = None
    accepted = False
    for event in events:
        if (
            accepted
            or event.gate != "G10_typeset"
            or event.stage != "typeset"
            or event.parent_checksum != g9_terminal_checksum
            or event.operation
            not in {
                "typeset-job-enqueued",
                "typeset-candidate-produced",
                "typeset-job-completed",
                "typeset-job-failed",
                "typeset-candidate-reviewed",
            }
        ):
            raise PageLineageConflict(
                "Only a valid G10 prefix may follow terminal G9",
                resource=f"event:{event.id}",
                reason="g9-replay-invalid",
            )
        if event.operation == "typeset-job-enqueued":
            valid = state == "idle" and event.job_item_id is not None
            state = "enqueued"
            current_item = event.job_item_id
        elif event.operation == "typeset-candidate-produced":
            candidate = session.scalar(
                select(PageTypesetCandidate).where(
                    PageTypesetCandidate.generation_id == generation.id,
                    PageTypesetCandidate.job_item_id == event.job_item_id,
                    PageTypesetCandidate.sequence == event.sequence,
                )
            )
            valid = (
                state == "enqueued"
                and event.job_item_id == current_item
                and candidate is not None
                and (event.evidence or {}).get("candidateId") == candidate.id
            )
            state = "published"
        elif event.operation == "typeset-job-completed":
            valid = state == "published" and event.job_item_id == current_item
            state = "completed"
        elif event.operation == "typeset-job-failed":
            valid = state == "enqueued" and event.job_item_id == current_item
            state = "idle"
            current_item = None
        else:
            review = session.scalar(
                select(PageTypesetReview).where(
                    PageTypesetReview.generation_id == generation.id,
                    PageTypesetReview.sequence == event.sequence,
                )
            )
            valid = (
                state == "completed"
                and review is not None
                and (event.evidence or {}).get("candidateId") == review.candidate_id
                and event.state == review.state
            )
            accepted = bool(review is not None and review.state == "accepted")
            state = "terminal" if accepted else "idle"
            current_item = None
        if not valid:
            raise PageLineageConflict(
                "G10 downstream event order is invalid",
                resource=f"event:{event.id}",
                reason="g9-replay-invalid",
            )


def _validate_candidate_file(store: ProjectStore, row: PageTypesetCandidate) -> Path:
    expected_relative = _candidate_relative(row.generation_id, row.id)
    if row.relative_path != expected_relative.as_posix():
        raise PageLineageConflict(
            "G10 candidate path is not canonical",
            resource=f"typeset-candidate:{row.id}",
            reason="g10-replay-invalid",
        )
    path = _candidate_target(store, row.generation_id, row.id)
    if not path.is_file() or _sha256_file(path) != row.candidate_checksum:
        raise PageLineageConflict(
            "G10 candidate bytes do not match immutable evidence",
            resource=f"typeset-candidate:{row.id}",
            reason="g10-candidate-file-invalid",
        )
    try:
        with Image.open(path) as opened:
            opened.load()
            if opened.format != "PNG" or opened.size != (row.width, row.height):
                raise ValueError
    except (OSError, ValueError) as error:
        raise PageLineageConflict(
            "G10 candidate grid does not match immutable evidence",
            resource=f"typeset-candidate:{row.id}",
            reason="g10-candidate-file-invalid",
        ) from error
    return path


def _recovered_candidate_result(row: PageTypesetCandidate) -> dict[str, Any]:
    return {
        "candidateId": row.id,
        "candidateChecksum": row.candidate_checksum,
        "routeChecksum": row.route_checksum,
        "styleChecksum": row.style_checksum,
        "layoutChecksum": row.layout_checksum,
        "overflowRegionIds": row.overflow_region_ids,
        "anomalies": row.anomalies,
        "provider": row.provider,
        "modelVersion": row.model_version,
        "parameterHash": row.parameter_hash,
        "recovered": True,
    }


def _validate_persisted_typeset_publication(
    store: ProjectStore,
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    job: Job,
    item: JobItem,
    candidate_id: str,
    allow_finishing: bool = False,
) -> dict[str, Any] | None:
    """Validate an already-published G10 item without invoking its renderer."""

    def invalid(message: str, resource: str | None = None) -> None:
        raise PageLineageConflict(
            message,
            resource=resource or f"job-item:{item.id}",
            reason="g10-replay-invalid",
        )

    matches = list(
        session.scalars(
            select(PageTypesetCandidate).where(PageTypesetCandidate.job_item_id == item.id)
        ).all()
    )
    if not matches:
        return None
    if len(matches) != 1:
        invalid("Recovered G10 job item has duplicate candidates")
    row = matches[0]
    # Detection of the immutable row intentionally precedes all upstream replay
    # and route checks. From here on this path verifies only persisted inputs,
    # frozen contracts, and artifact bytes; it never reconstructs pixels.
    bindings = _current_bindings(store, session, image, generation)
    validate_g10_prefix_after_g9(
        session,
        generation,
        g9_terminal_checksum=str(bindings["g9TerminalChecksum"]),
    )
    region_manifest, route_manifest = _route_and_region_manifests(
        session,
        image,
        bindings["acceptedByRegion"],
    )
    try:
        normalized_options = resolve_typeset_job_options(dict(job.options))
    except (PageLineageConflict, TypeError, ValueError):
        invalid("Recovered G10 job options are invalid", f"job:{job.id}")
    if normalized_options != job.options:
        invalid("Recovered G10 job options changed after enqueue", f"job:{job.id}")
    expected_scale, expected_width, expected_height = _render_scale(
        image,
        bindings["cleanPlatePath"],
    )
    style_entries = row.style_manifest if isinstance(row.style_manifest, list) else []
    layout_entries = row.layout_manifest if isinstance(row.layout_manifest, list) else []
    route_ids = [entry["regionId"] for entry in route_manifest]
    rendered_routes = [entry for entry in route_manifest if entry["renderRequired"]]
    frozen_styles = {
        str(entry.get("regionId")): entry.get("style")
        for entry in style_entries
        if isinstance(entry, dict)
    }
    route_by_id = {str(entry["regionId"]): entry["route"] for entry in route_manifest}
    raw_styles = normalized_options["regionStyles"]
    options_match_frozen = set(raw_styles) <= set(route_by_id)
    for region_id, override in raw_styles.items():
        frozen_style = frozen_styles.get(region_id)
        if (
            route_by_id.get(region_id) in {"keep", "ignore"}
            or not isinstance(frozen_style, dict)
            or set(override) - _STYLE_KEYS
        ):
            options_match_frozen = False
            break
        for field, value in override.items():
            try:
                expected_value = (
                    _canonical_color(value, field=field)
                    if field in {"fill", "strokeColor"}
                    else value
                )
            except PageLineageConflict:
                options_match_frozen = False
                break
            if frozen_style.get(field) != expected_value:
                options_match_frozen = False
                break
        if not options_match_frozen:
            break
    frozen_parameter_hash = _digest(
        {
            "contractVersion": TYPESET_CONTRACT_VERSION,
            "provider": TYPESET_PROVIDER,
            "modelVersion": TYPESET_MODEL_VERSION,
            "g9TerminalChecksum": bindings["g9TerminalChecksum"],
            "translationStateChecksum": bindings["translationStateChecksum"],
            "cleanPlateChecksum": bindings["cleanPlateChecksum"],
            "regionManifest": region_manifest,
            "routeChecksum": row.route_checksum,
            "styleChecksum": row.style_checksum,
        }
    )
    expected_after = {
        "candidateChecksum": row.candidate_checksum,
        "routeChecksum": row.route_checksum,
        "styleChecksum": row.style_checksum,
        "layoutChecksum": row.layout_checksum,
    }
    clean_candidate = bindings["cleanPlateCandidate"]
    style_shape_valid = len(style_entries) == len(route_manifest) and all(
        isinstance(style, dict)
        and style.get("regionId") == route.get("regionId")
        and style.get("route") == route.get("route")
        and (
            isinstance(style.get("style"), dict)
            if route.get("renderRequired")
            else style.get("style") is None
        )
        for style, route in zip(style_entries, route_manifest, strict=True)
    )
    layout_shape_valid = len(layout_entries) == len(rendered_routes) and all(
        isinstance(layout, dict)
        and layout.get("regionId") == route.get("regionId")
        and layout.get("route") == route.get("route")
        for layout, route in zip(layout_entries, rendered_routes, strict=True)
    )
    expected_overflow = [
        str(entry["regionId"])
        for entry in layout_entries
        if isinstance(entry, dict) and entry.get("overflow") is True
    ]
    if (
        row.id != candidate_id
        or row.generation_id != generation.id
        or row.image_id != image.id
        or row.job_id != job.id
        or row.job_item_id != item.id
        or item.job_id != job.id
        or item.image_id != image.id
        or item.region_id is not None
        or job.project_id != generation.project_id
        or job.kind != "typeset"
        or row.parent_checksum != bindings["g9TerminalChecksum"]
        or row.g9_terminal_checksum != bindings["g9TerminalChecksum"]
        or row.translation_state_checksum != bindings["translationStateChecksum"]
        or (row.clean_plate_candidate_id, row.cloud_full_page_candidate_id)
        != _clean_candidate_ids(clean_candidate)
        or row.clean_plate_checksum != bindings["cleanPlateChecksum"]
        or row.region_manifest != region_manifest
        or row.route_manifest != route_manifest
        or row.route_checksum != _digest(row.route_manifest)
        or row.style_checksum != _digest(row.style_manifest)
        or row.layout_checksum != _digest(row.layout_manifest)
        or row.provider != TYPESET_PROVIDER
        or row.model_version != TYPESET_MODEL_VERSION
        or row.parameter_hash != frozen_parameter_hash
        or row.width != expected_width
        or row.height != expected_height
        or row.render_scale != expected_scale
        or not options_match_frozen
        or len(row.region_manifest) != len(row.route_manifest)
        or route_ids != [entry.get("regionId") for entry in row.region_manifest]
        or not style_shape_valid
        or not layout_shape_valid
        or row.overflow_region_ids != expected_overflow
        or not isinstance(row.anomalies, list)
        or any(not isinstance(entry, str) or not entry for entry in row.anomalies)
        or not _revision_matches(
            session,
            generation,
            row.revision_id,
            entity_type="typeset-candidate",
            entity_id=row.id,
            operation="create",
            before=None,
            after=expected_after,
        )
    ):
        invalid("Recovered G10 candidate row is inconsistent", f"typeset-candidate:{row.id}")
    _validate_candidate_file(store, row)

    candidates = list(
        session.scalars(
            select(PageTypesetCandidate)
            .where(PageTypesetCandidate.generation_id == generation.id)
            .order_by(PageTypesetCandidate.sequence, PageTypesetCandidate.id)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(PageTypesetReview)
            .where(PageTypesetReview.generation_id == generation.id)
            .order_by(PageTypesetReview.sequence, PageTypesetReview.id)
        ).all()
    )
    if (
        not candidates
        or candidates[-1].id != row.id
        or any(candidate.image_id != image.id for candidate in candidates)
        or any(review.image_id != image.id for review in reviews)
        or any(review.sequence >= row.sequence for review in reviews)
        or len(reviews) != len(candidates) - 1
        or [review.candidate_id for review in reviews]
        != [candidate.id for candidate in candidates[:-1]]
        or any(review.state != "rejected" for review in reviews)
    ):
        invalid("Recovered G10 candidate is not the exact current publication")
    input_state = typeset_state_checksum(
        str(bindings["g9TerminalChecksum"]),
        candidates[:-1],
        reviews,
    )
    output_state = typeset_state_checksum(
        str(bindings["g9TerminalChecksum"]),
        candidates,
        reviews,
    )
    enqueue_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "typeset-job-enqueued",
            )
        ).all()
    )
    produced_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation == "typeset-candidate-produced",
            )
        ).all()
    )
    terminal_events = list(
        session.scalars(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.job_item_id == item.id,
                PageLineageEvent.operation.in_(("typeset-job-completed", "typeset-job-failed")),
            )
        ).all()
    )
    if len(enqueue_events) != 1 or len(produced_events) != 1 or terminal_events:
        invalid("Recovered G10 candidate lacks one exact open publication event")
    enqueue = enqueue_events[0]
    produced = produced_events[0]
    actor = _job_actor(job)
    enqueue_evidence = {
        "eventType": "job-enqueued",
        "qualityState": "pending-review",
        "targetKind": "image",
        "regionCount": len(row.route_manifest),
        "renderRegionCount": len(rendered_routes),
        "g9TerminalChecksum": row.g9_terminal_checksum,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "routeChecksum": row.route_checksum,
        "styleChecksum": row.style_checksum,
    }
    produced_evidence = {
        "eventType": "typeset-candidate-produced",
        "qualityState": "pending-review",
        "targetKind": "typeset-candidate",
        "candidateId": row.id,
        "candidateChecksum": row.candidate_checksum,
        "regionCount": len(row.route_manifest),
        "renderRegionCount": len(rendered_routes),
        "g9TerminalChecksum": row.g9_terminal_checksum,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "routeChecksum": row.route_checksum,
        "styleChecksum": row.style_checksum,
        "layoutChecksum": row.layout_checksum,
        "width": row.width,
        "height": row.height,
        "renderScale": row.render_scale,
        "overflowRegionIds": row.overflow_region_ids,
        "anomalies": row.anomalies,
    }
    common_enqueue_valid = (
        enqueue.gate == "G10_typeset"
        and enqueue.stage == "typeset"
        and enqueue.parent_checksum == row.g9_terminal_checksum
        and enqueue.git_commit is None
        and enqueue.sequence == _job_expected_sequence(job, generation)
        and enqueue.job_id == job.id
        and enqueue.job_item_id == item.id
        and enqueue.state == "pending"
        and enqueue.decision is None
        and enqueue.reason == "job-enqueued"
        and enqueue.revision_id is None
        and enqueue.input_checksum == input_state
        and enqueue.output_checksum == input_state
        and enqueue.provider == TYPESET_PROVIDER
        and enqueue.model_version == TYPESET_MODEL_VERSION
        and enqueue.parameter_hash == row.parameter_hash
        and _event_actor(enqueue) == actor
        and enqueue.started_at == job.created_at
        and enqueue.finished_at is None
        and enqueue.evidence == enqueue_evidence
    )
    common_produced_valid = (
        produced.gate == "G10_typeset"
        and produced.stage == "typeset"
        and produced.parent_checksum == row.g9_terminal_checksum
        and produced.git_commit is None
        and produced.sequence == row.sequence
        and produced.job_id == job.id
        and produced.job_item_id == item.id
        and produced.state == "pending"
        and produced.decision == "candidate-produced"
        and produced.reason == "typeset-review-required"
        and produced.revision_id == row.revision_id
        and produced.input_checksum == input_state
        and produced.output_checksum == output_state
        and produced.provider == row.provider
        and produced.model_version == row.model_version
        and produced.parameter_hash == row.parameter_hash
        and _event_actor(produced) == actor
        and produced.started_at == item.started_at
        and produced.finished_at is not None
        and produced.evidence == produced_evidence
    )
    allowed_statuses = {
        ("queued", "queued"),
        ("running", "queued"),
        ("running", "running"),
    }
    if allow_finishing:
        allowed_statuses.add(("running", "completed"))
    if (
        not common_enqueue_valid
        or not common_produced_valid
        or (job.status, item.status) not in allowed_statuses
    ):
        invalid("Recovered G10 publication evidence is inconsistent", f"event:{produced.id}")
    return {
        "candidate": row,
        "enqueue": enqueue,
        "produced": produced,
        "inputState": input_state,
        "outputState": output_state,
    }


def validate_typeset_replay(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
    *,
    bindings: Mapping[str, Any],
    allow_finishing_item_id: str | None = None,
    pending_enqueue_item_id: str | None = None,
) -> dict[str, Any]:
    def invalid(message: str, resource: str | None = None) -> None:
        raise PageLineageConflict(
            message,
            resource=resource or f"page-generation:{generation.id}",
            reason="g10-replay-invalid",
        )

    g9_checksum = str(bindings["g9TerminalChecksum"])
    validate_g10_prefix_after_g9(session, generation, g9_terminal_checksum=g9_checksum)
    events = list(
        session.scalars(
            select(PageLineageEvent)
            .where(
                PageLineageEvent.generation_id == generation.id,
                PageLineageEvent.gate == "G10_typeset",
            )
            .order_by(PageLineageEvent.sequence)
        ).all()
    )
    candidates = list(
        session.scalars(
            select(PageTypesetCandidate)
            .where(PageTypesetCandidate.generation_id == generation.id)
            .order_by(PageTypesetCandidate.sequence, PageTypesetCandidate.id)
        ).all()
    )
    reviews = list(
        session.scalars(
            select(PageTypesetReview)
            .where(PageTypesetReview.generation_id == generation.id)
            .order_by(PageTypesetReview.sequence, PageTypesetReview.id)
        ).all()
    )
    if any(row.image_id != image.id for row in [*candidates, *reviews]):
        invalid("G10 row ownership is invalid")
    candidate_by_sequence = {row.sequence: row for row in candidates}
    review_by_sequence = {row.sequence: row for row in reviews}
    if len(candidate_by_sequence) != len(candidates) or len(review_by_sequence) != len(reviews):
        invalid("G10 row sequence is duplicated")
    jobs: dict[str, tuple[JobItem, Job]] = {}
    for item, job in session.execute(
        select(JobItem, Job)
        .join(Job, Job.id == JobItem.job_id)
        .where(
            Job.kind == "typeset",
            Job.lineage_context.is_not(None),
            JobItem.image_id == image.id,
        )
    ).all():
        pages = (job.lineage_context or {}).get("pages")
        belongs = isinstance(pages, list) and any(
            isinstance(page, dict) and page.get("pageGenerationId") == generation.id
            for page in pages
        )
        if not belongs:
            continue
        if item.region_id is not None or job.project_id != generation.project_id:
            invalid("G10 job ownership is invalid", f"job:{job.id}")
        jobs[item.id] = (item, job)
    current_state = g9_checksum
    candidate_prefix: list[PageTypesetCandidate] = []
    review_prefix: list[PageTypesetReview] = []
    open_item: str | None = None
    published: PageTypesetCandidate | None = None
    completed = False
    consumed_candidates: set[str] = set()
    consumed_reviews: set[str] = set()
    enqueued_items: set[str] = set()
    accepted_review: PageTypesetReview | None = None
    enqueue_contracts: dict[str, dict[str, Any]] = {}
    for event in events:
        if (
            event.stage != "typeset"
            or event.parent_checksum != g9_checksum
            or event.git_commit is not None
        ):
            invalid("G10 event common fields are invalid", f"event:{event.id}")
        try:
            event_actor = _event_actor(event)
        except PageLineageConflict:
            invalid("G10 event actor is invalid", f"event:{event.id}")
        evidence = event.evidence
        if not isinstance(evidence, dict):
            invalid("G10 event evidence is invalid", f"event:{event.id}")
        if event.operation == "typeset-job-enqueued":
            match = jobs.get(str(event.job_item_id))
            if match is None:
                invalid("G10 enqueue has no exact job", f"event:{event.id}")
            item, job = match
            contract = _job_contract(session, image, bindings, job.options)
            render_count = sum(entry["renderRequired"] for entry in contract["routeManifest"])
            expected_evidence = {
                "eventType": "job-enqueued",
                "qualityState": "pending-review",
                "targetKind": "image",
                "regionCount": len(contract["routeManifest"]),
                "renderRegionCount": render_count,
                "g9TerminalChecksum": g9_checksum,
                "cleanPlateChecksum": bindings["cleanPlateChecksum"],
                "routeChecksum": contract["routeChecksum"],
                "styleChecksum": contract["styleChecksum"],
            }
            if (
                open_item is not None
                or accepted_review is not None
                or event.sequence != _job_expected_sequence(job, generation)
                or event.job_id != job.id
                or event.state != "pending"
                or event.decision is not None
                or event.reason != "job-enqueued"
                or event.revision_id is not None
                or event.input_checksum != current_state
                or event.output_checksum != current_state
                or event.provider != TYPESET_PROVIDER
                or event.model_version != TYPESET_MODEL_VERSION
                or event.parameter_hash != contract["parameterHash"]
                or event_actor != _job_actor(job)
                or event.started_at != job.created_at
                or event.finished_at is not None
                or evidence != expected_evidence
            ):
                invalid("G10 enqueue event is inconsistent", f"event:{event.id}")
            open_item = item.id
            enqueued_items.add(item.id)
            published = None
            completed = False
            enqueue_contracts[item.id] = contract
            continue
        if event.operation == "typeset-candidate-produced":
            row = candidate_by_sequence.get(event.sequence)
            match = jobs.get(str(event.job_item_id))
            if row is None or match is None or event.job_item_id != open_item:
                invalid("G10 publication has no exact open job", f"event:{event.id}")
            item, job = match
            contract = enqueue_contracts[item.id]
            expected_scale, expected_width, expected_height = _render_scale(
                image, bindings["cleanPlatePath"]
            )
            (
                expected_payload,
                expected_layout,
                expected_overflow,
                expected_anomalies,
            ) = _render_candidate(
                clean_path=bindings["cleanPlatePath"],
                region_manifest=contract["regionManifest"],
                route_manifest=contract["routeManifest"],
                style_manifest=contract["styleManifest"],
                accepted_by_region=bindings["acceptedByRegion"],
                scale=expected_scale,
            )
            expected_after = {
                "candidateChecksum": row.candidate_checksum,
                "routeChecksum": row.route_checksum,
                "styleChecksum": row.style_checksum,
                "layoutChecksum": row.layout_checksum,
            }
            if (
                row.job_id != job.id
                or row.job_item_id != item.id
                or row.parent_checksum != g9_checksum
                or row.g9_terminal_checksum != g9_checksum
                or row.translation_state_checksum != bindings["translationStateChecksum"]
                or (row.clean_plate_candidate_id, row.cloud_full_page_candidate_id)
                != _clean_candidate_ids(bindings["cleanPlateCandidate"])
                or row.clean_plate_checksum != bindings["cleanPlateChecksum"]
                or row.region_manifest != contract["regionManifest"]
                or row.route_manifest != contract["routeManifest"]
                or row.route_checksum != _digest(row.route_manifest)
                or row.route_checksum != contract["routeChecksum"]
                or row.style_manifest != contract["styleManifest"]
                or row.style_checksum != _digest(row.style_manifest)
                or row.style_checksum != contract["styleChecksum"]
                or row.layout_checksum != _digest(row.layout_manifest)
                or row.layout_manifest != expected_layout
                or row.provider != TYPESET_PROVIDER
                or row.model_version != TYPESET_MODEL_VERSION
                or row.parameter_hash != contract["parameterHash"]
                or len(row.route_manifest) != len(row.style_manifest)
                or len(row.route_manifest) != len(row.region_manifest)
                or row.width != expected_width
                or row.height != expected_height
                or row.render_scale != expected_scale
                or row.candidate_checksum != hashlib.sha256(expected_payload).hexdigest()
                or row.overflow_region_ids != expected_overflow
                or row.anomalies != expected_anomalies
                or not _revision_matches(
                    session,
                    generation,
                    row.revision_id,
                    entity_type="typeset-candidate",
                    entity_id=row.id,
                    operation="create",
                    before=None,
                    after=expected_after,
                )
            ):
                invalid("G10 candidate row is inconsistent", f"typeset-candidate:{row.id}")
            _validate_candidate_file(store, row)
            candidate_prefix.append(row)
            consumed_candidates.add(row.id)
            next_state = typeset_state_checksum(g9_checksum, candidate_prefix, review_prefix)
            render_count = sum(entry["renderRequired"] for entry in row.route_manifest)
            expected_evidence = {
                "eventType": "typeset-candidate-produced",
                "qualityState": "pending-review",
                "targetKind": "typeset-candidate",
                "candidateId": row.id,
                "candidateChecksum": row.candidate_checksum,
                "regionCount": len(row.route_manifest),
                "renderRegionCount": render_count,
                "g9TerminalChecksum": row.g9_terminal_checksum,
                "cleanPlateChecksum": row.clean_plate_checksum,
                "routeChecksum": row.route_checksum,
                "styleChecksum": row.style_checksum,
                "layoutChecksum": row.layout_checksum,
                "width": row.width,
                "height": row.height,
                "renderScale": row.render_scale,
                "overflowRegionIds": row.overflow_region_ids,
                "anomalies": row.anomalies,
            }
            if (
                event.job_id != job.id
                or event.state != "pending"
                or event.decision != "candidate-produced"
                or event.reason != "typeset-review-required"
                or event.revision_id != row.revision_id
                or event.input_checksum != current_state
                or event.output_checksum != next_state
                or event.provider != row.provider
                or event.model_version != row.model_version
                or event.parameter_hash != row.parameter_hash
                or event_actor != _job_actor(job)
                or event.started_at != item.started_at
                or event.finished_at is None
                or evidence != expected_evidence
            ):
                invalid("G10 publication event is inconsistent", f"event:{event.id}")
            current_state = next_state
            published = row
            continue
        if event.operation in {"typeset-job-completed", "typeset-job-failed"}:
            match = jobs.get(str(event.job_item_id))
            if match is None or event.job_item_id != open_item:
                invalid("G10 completion has no exact open job", f"event:{event.id}")
            item, job = match
            succeeded = event.operation == "typeset-job-completed"
            candidate = published
            completion_evidence = (
                {
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.candidate_checksum,
                    "g9TerminalChecksum": candidate.g9_terminal_checksum,
                    "cleanPlateChecksum": candidate.clean_plate_checksum,
                    "routeChecksum": candidate.route_checksum,
                    "styleChecksum": candidate.style_checksum,
                    "layoutChecksum": candidate.layout_checksum,
                    "width": candidate.width,
                    "height": candidate.height,
                    "renderScale": candidate.render_scale,
                    "overflowRegionIds": candidate.overflow_region_ids,
                    "anomalies": candidate.anomalies,
                }
                if candidate is not None
                else {
                    "g9TerminalChecksum": g9_checksum,
                    "cleanPlateChecksum": bindings["cleanPlateChecksum"],
                    "routeChecksum": enqueue_contracts[item.id]["routeChecksum"],
                    "styleChecksum": enqueue_contracts[item.id]["styleChecksum"],
                }
            )
            expected_evidence = {
                "eventType": "job-completed" if succeeded else "job-failed",
                "qualityState": "pending-review" if succeeded else "blocked",
                "targetKind": "image",
                **completion_evidence,
            }
            if (
                succeeded != (candidate is not None)
                or event.job_id != job.id
                or event.state != ("pending" if succeeded else "blocked")
                or event.decision is not None
                or event.reason != ("review-required" if succeeded else "job-execution-failed")
                or event.revision_id is not None
                or event.input_checksum
                != next(
                    candidate_event.input_checksum
                    for candidate_event in events
                    if candidate_event.operation == "typeset-job-enqueued"
                    and candidate_event.job_item_id == item.id
                )
                or event.output_checksum != (current_state if succeeded else None)
                or event.provider != TYPESET_PROVIDER
                or event.model_version != TYPESET_MODEL_VERSION
                or event.parameter_hash != enqueue_contracts[item.id]["parameterHash"]
                or event_actor != _job_actor(job)
                or event.started_at != item.started_at
                or event.finished_at != item.finished_at
                or item.status != ("completed" if succeeded else "failed")
                or job.status
                not in (("running", "completed") if succeeded else ("running", "failed"))
                or evidence != expected_evidence
            ):
                invalid("G10 completion event is inconsistent", f"event:{event.id}")
            if succeeded:
                completed = True
            else:
                open_item = None
                published = None
                completed = False
            continue
        if event.operation != "typeset-candidate-reviewed":
            invalid("G10 operation is unsupported", f"event:{event.id}")
        row = review_by_sequence.get(event.sequence)
        candidate = published
        if row is None or candidate is None or not completed:
            invalid("G10 review has no completed candidate", f"event:{event.id}")
        if not _valid_visual_checks(row.checks):
            invalid("G10 review checks are inconsistent", f"typeset-review:{row.id}")
        failed = [entry["check"] for entry in row.checks if not entry["passed"]]
        known_defects = bool(candidate.overflow_region_ids or candidate.anomalies)
        defect_check_matches = not known_defects or not row.checks[-1]["passed"]
        valid_decision = (
            row.state == "accepted"
            and row.reason == "typeset-reviewed"
            and not failed
            and not known_defects
        ) or (
            row.state == "rejected"
            and bool(failed)
            and defect_check_matches
            and (
                (row.reason == "multiple-visual-failures" and len(failed) >= 2)
                or row.reason in failed
            )
        )
        expected_after = {
            "candidateId": candidate.id,
            "state": row.state,
            "reason": row.reason,
            "terminalChecksum": row.terminal_checksum,
        }
        if (
            not valid_decision
            or row.candidate_id != candidate.id
            or row.parent_checksum != g9_checksum
            or row.candidate_checksum != candidate.candidate_checksum
            or row.route_checksum != candidate.route_checksum
            or row.style_checksum != candidate.style_checksum
            or row.layout_checksum != candidate.layout_checksum
            or row.g9_terminal_checksum != candidate.g9_terminal_checksum
            or row.clean_plate_checksum != candidate.clean_plate_checksum
            or row.observed_width != candidate.width
            or row.observed_height != candidate.height
            or row.observed_render_scale != candidate.render_scale
            or row.reviewer != event_actor
            or not _revision_matches(
                session,
                generation,
                row.revision_id,
                entity_type="typeset-review",
                entity_id=row.id,
                operation="review",
                before=None,
                after=expected_after,
            )
        ):
            invalid("G10 review row is inconsistent", f"typeset-review:{row.id}")
        review_prefix.append(row)
        consumed_reviews.add(row.id)
        next_state = typeset_state_checksum(g9_checksum, candidate_prefix, review_prefix)
        expected_evidence = {
            "eventType": "typeset-candidate-reviewed",
            "qualityState": row.state,
            "targetKind": "typeset-candidate",
            "candidateId": candidate.id,
            "candidateChecksum": candidate.candidate_checksum,
            "g9TerminalChecksum": candidate.g9_terminal_checksum,
            "cleanPlateChecksum": candidate.clean_plate_checksum,
            "routeChecksum": candidate.route_checksum,
            "styleChecksum": candidate.style_checksum,
            "layoutChecksum": candidate.layout_checksum,
            "width": candidate.width,
            "height": candidate.height,
            "renderScale": candidate.render_scale,
            "overflowRegionIds": candidate.overflow_region_ids,
            "anomalies": candidate.anomalies,
            "checks": row.checks,
        }
        if (
            row.terminal_checksum != next_state
            or event.state != row.state
            or event.decision != f"candidate-{row.state}"
            or event.reason != row.reason
            or event.job_id is not None
            or event.job_item_id is not None
            or event.revision_id != row.revision_id
            or event.input_checksum != current_state
            or event.output_checksum != next_state
            or event.provider != candidate.provider
            or event.model_version != candidate.model_version
            or event.parameter_hash != candidate.parameter_hash
            or event.started_at is not None
            or event.finished_at is not None
            or evidence != expected_evidence
        ):
            invalid("G10 review event is inconsistent", f"event:{event.id}")
        current_state = next_state
        open_item = None
        published = None
        completed = False
        if row.state == "accepted":
            accepted_review = row
    if consumed_candidates != {row.id for row in candidates} or consumed_reviews != {
        row.id for row in reviews
    }:
        invalid("G10 rows lack one-to-one publication events")
    missing_enqueues = set(jobs) - enqueued_items
    if pending_enqueue_item_id is not None:
        missing_enqueues.discard(pending_enqueue_item_id)
    if missing_enqueues:
        invalid("Current-generation G10 job has no enqueue event")
    if open_item is not None:
        item, job = jobs[open_item]
        if completed:
            valid_open_status = item.status == "completed" and job.status in {
                "running",
                "completed",
            }
        elif published is not None:
            valid_open_status = (job.status, item.status) in {
                ("queued", "queued"),
                ("running", "queued"),
                ("running", "running"),
            } or (
                item.id == allow_finishing_item_id
                and job.status == "running"
                and item.status == "completed"
            )
        else:
            valid_open_status = job.status in {"queued", "running"} and (
                item.status in {"queued", "running"}
                or (item.id == allow_finishing_item_id and item.status == "failed")
            )
        if not valid_open_status:
            invalid("Open G10 job item state is inconsistent", f"job-item:{item.id}")
    if accepted_review is not None and events[-1].operation != "typeset-candidate-reviewed":
        invalid("G10 accepted terminal has downstream evidence")
    return {
        "stateChecksum": current_state,
        "candidates": candidates,
        "reviews": reviews,
        "acceptedReview": accepted_review,
        "openItemId": open_item,
        "publishedCandidate": published,
        "completed": completed,
    }


def prepare_typeset_enqueue(
    store: ProjectStore,
    session,
    *,
    image: ImageAsset,
    generation: PageGeneration,
    job: Job,
    item: JobItem,
) -> dict[str, Any]:
    if item.region_id is not None:
        raise PageLineageConflict(
            "Strict G10 typeset requires one whole-page job item",
            resource=f"job-item:{item.id}",
            reason="g10-whole-page-required",
        )
    bindings = _current_bindings(store, session, image, generation)
    replay = validate_typeset_replay(
        store,
        session,
        image,
        generation,
        bindings=bindings,
        pending_enqueue_item_id=item.id,
    )
    if replay["acceptedReview"] is not None:
        raise PageLineageConflict(
            "Accepted G10 evidence is immutable",
            resource=f"image:{image.id}",
            reason="g10-typeset-accepted",
        )
    if replay["openItemId"] is not None:
        raise PageLineageConflict(
            "Another strict typeset job is active for this page",
            resource=f"image:{image.id}",
            reason="g10-typeset-job-active",
        )
    if replay["candidates"] and len(replay["reviews"]) != len(replay["candidates"]):
        raise PageLineageConflict(
            "The current G10 candidate must be reviewed before a retry",
            resource=f"image:{image.id}",
            reason="g10-review-required",
        )
    contract = _job_contract(session, image, bindings, job.options)
    return {
        "stateChecksum": replay["stateChecksum"],
        "g9TerminalChecksum": bindings["g9TerminalChecksum"],
        "translationStateChecksum": bindings["translationStateChecksum"],
        "cleanPlateCandidateId": (
            bindings["cleanPlateCandidate"].id
            if bindings["cleanPlateCandidate"] is not None
            else None
        ),
        "cleanPlateChecksum": bindings["cleanPlateChecksum"],
        "regionCount": len(contract["routeManifest"]),
        "renderRegionCount": sum(entry["renderRequired"] for entry in contract["routeManifest"]),
        **contract,
    }


def publish_typeset_candidate(
    store: ProjectStore,
    *,
    job: Job,
    item: JobItem,
    binding: JobMutationBinding,
) -> dict[str, Any]:
    if item.image_id is None:
        raise ProjectError("Typeset job item has no image")
    candidate_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"g10-typeset:{binding['generationId']}:{item.id}",
        )
    )
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, item.image_id)
            generation = session.get(PageGeneration, binding["generationId"])
            current_job = session.get(Job, job.id)
            current_item = session.get(JobItem, item.id)
            if image is None or generation is None or current_job is None or current_item is None:
                raise ProjectError("Typeset target disappeared")
            recovered = _validate_persisted_typeset_publication(
                store,
                session,
                image=image,
                generation=generation,
                job=current_job,
                item=current_item,
                candidate_id=candidate_id,
            )
            if recovered is not None:
                return _recovered_candidate_result(recovered["candidate"])
            bindings = _current_bindings(store, session, image, generation)
            replay = validate_typeset_replay(store, session, image, generation, bindings=bindings)
            if replay["openItemId"] != item.id or replay["acceptedReview"] is not None:
                raise PageLineageConflict(
                    "Typeset job is not the current open lineage item",
                    resource=f"job-item:{item.id}",
                    reason="g10-typeset-job-stale",
                )
            enqueue = session.scalar(
                select(PageLineageEvent).where(
                    PageLineageEvent.generation_id == generation.id,
                    PageLineageEvent.job_item_id == item.id,
                    PageLineageEvent.operation == "typeset-job-enqueued",
                )
            )
            contract = _job_contract(session, image, bindings, current_job.options)
            if (
                enqueue is None
                or enqueue.input_checksum != replay["stateChecksum"]
                or enqueue.output_checksum != replay["stateChecksum"]
                or (enqueue.evidence or {}).get("routeChecksum") != contract["routeChecksum"]
                or (enqueue.evidence or {}).get("styleChecksum") != contract["styleChecksum"]
                or enqueue.parameter_hash != contract["parameterHash"]
            ):
                raise PageLineageConflict(
                    "G10 route or style changed after enqueue",
                    resource=f"job-item:{item.id}",
                    reason="g10-typeset-job-stale",
                )
            render_scale, width, height = _render_scale(image, bindings["cleanPlatePath"])
            expected_revision = image.revision
            expected_state = replay["stateChecksum"]
            expected_g9 = bindings["g9TerminalChecksum"]
            expected_clean = bindings["cleanPlateChecksum"]
            accepted_by_region = dict(bindings["acceptedByRegion"])
            clean_path = Path(bindings["cleanPlatePath"])
    payload, layouts, overflow_ids, anomalies = _render_candidate(
        clean_path=clean_path,
        region_manifest=contract["regionManifest"],
        route_manifest=contract["routeManifest"],
        style_manifest=contract["styleManifest"],
        accepted_by_region=accepted_by_region,
        scale=render_scale,
    )
    checksum = hashlib.sha256(payload).hexdigest()
    layout_checksum = _digest(layouts)
    target = _candidate_target(store, binding["generationId"], candidate_id)
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, item.image_id)
            generation = session.get(PageGeneration, binding["generationId"])
            current_job = session.get(Job, job.id)
            current_item = session.get(JobItem, item.id)
            if image is None or generation is None or current_job is None or current_item is None:
                raise ProjectError("Typeset target disappeared before publication")
            recovered = _validate_persisted_typeset_publication(
                store,
                session,
                image=image,
                generation=generation,
                job=current_job,
                item=current_item,
                candidate_id=candidate_id,
            )
            if recovered is not None:
                return _recovered_candidate_result(recovered["candidate"])
            bindings = _current_bindings(store, session, image, generation)
            replay = validate_typeset_replay(store, session, image, generation, bindings=bindings)
            current_contract = _job_contract(session, image, bindings, current_job.options)
            if (
                image.revision != expected_revision
                or replay["stateChecksum"] != expected_state
                or bindings["g9TerminalChecksum"] != expected_g9
                or bindings["cleanPlateChecksum"] != expected_clean
                or current_contract != contract
                or replay["openItemId"] != current_item.id
            ):
                raise PageLineageConflict(
                    "Page changed while the G10 candidate was rendering",
                    resource=f"image:{image.id}",
                    reason="g10-typeset-job-stale",
                )
            if current_job.status == "cancelled" or current_item.status == "cancelled":
                raise PageLineageConflict(
                    "G10 job was cancelled before immutable publication",
                    resource=f"job-item:{item.id}",
                    reason="g10-typeset-job-cancelled",
                )
            if target.exists():
                if not target.is_file() or _sha256_file(target) != checksum:
                    raise PageLineageConflict(
                        "An orphaned G10 candidate path contains different bytes",
                        resource=f"typeset-candidate:{candidate_id}",
                        reason="g10-candidate-file-conflict",
                    )
            else:
                atomic_write_bytes(target, payload)
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="typeset-candidate",
                entity_id=candidate_id,
                operation="create",
                before=None,
                after={
                    "candidateChecksum": checksum,
                    "routeChecksum": contract["routeChecksum"],
                    "styleChecksum": contract["styleChecksum"],
                    "layoutChecksum": layout_checksum,
                },
            )
            session.flush()
            sequence = generation.next_sequence
            candidate = PageTypesetCandidate(
                id=candidate_id,
                generation_id=generation.id,
                image_id=image.id,
                job_id=current_job.id,
                job_item_id=current_item.id,
                sequence=sequence,
                parent_checksum=expected_g9,
                g9_terminal_checksum=expected_g9,
                translation_state_checksum=bindings["translationStateChecksum"],
                clean_plate_candidate_id=(_clean_candidate_ids(bindings["cleanPlateCandidate"])[0]),
                cloud_full_page_candidate_id=(
                    _clean_candidate_ids(bindings["cleanPlateCandidate"])[1]
                ),
                clean_plate_checksum=expected_clean,
                region_manifest=contract["regionManifest"],
                route_manifest=contract["routeManifest"],
                route_checksum=contract["routeChecksum"],
                style_manifest=contract["styleManifest"],
                style_checksum=contract["styleChecksum"],
                layout_manifest=layouts,
                layout_checksum=layout_checksum,
                provider=TYPESET_PROVIDER,
                model_version=TYPESET_MODEL_VERSION,
                parameter_hash=contract["parameterHash"],
                candidate_checksum=checksum,
                relative_path=_candidate_relative(generation.id, candidate_id).as_posix(),
                width=width,
                height=height,
                render_scale=render_scale,
                overflow_region_ids=overflow_ids,
                anomalies=anomalies,
                revision_id=revision.id,
            )
            session.add(candidate)
            session.flush()
            after_state = typeset_state_checksum(
                expected_g9, [*replay["candidates"], candidate], replay["reviews"]
            )
            _append_event(
                session,
                generation,
                operation="typeset-candidate-produced",
                gate="G10_typeset",
                state="pending",
                actor=binding["actor"],
                input_checksum=expected_state,
                output_checksum=after_state,
                parent_checksum=expected_g9,
                stage="typeset",
                provider=TYPESET_PROVIDER,
                model_version=TYPESET_MODEL_VERSION,
                parameter_hash=contract["parameterHash"],
                job_id=current_job.id,
                job_item_id=current_item.id,
                revision_id=revision.id,
                decision="candidate-produced",
                reason="typeset-review-required",
                evidence={
                    "eventType": "typeset-candidate-produced",
                    "qualityState": "pending-review",
                    "targetKind": "typeset-candidate",
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.candidate_checksum,
                    "regionCount": len(candidate.route_manifest),
                    "renderRegionCount": sum(
                        entry["renderRequired"] for entry in candidate.route_manifest
                    ),
                    "g9TerminalChecksum": candidate.g9_terminal_checksum,
                    "cleanPlateChecksum": candidate.clean_plate_checksum,
                    "routeChecksum": candidate.route_checksum,
                    "styleChecksum": candidate.style_checksum,
                    "layoutChecksum": candidate.layout_checksum,
                    "width": candidate.width,
                    "height": candidate.height,
                    "renderScale": candidate.render_scale,
                    "overflowRegionIds": candidate.overflow_region_ids,
                    "anomalies": candidate.anomalies,
                },
                started_at=current_item.started_at,
                finished_at=datetime.now(UTC),
                expected_sequence=sequence,
            )
        store.write_snapshot()
    return {
        "candidateId": candidate_id,
        "candidateChecksum": checksum,
        "routeChecksum": contract["routeChecksum"],
        "styleChecksum": contract["styleChecksum"],
        "layoutChecksum": layout_checksum,
        "overflowRegionIds": overflow_ids,
        "anomalies": anomalies,
        "provider": TYPESET_PROVIDER,
        "modelVersion": TYPESET_MODEL_VERSION,
        "parameterHash": contract["parameterHash"],
    }


def typeset_completion_evidence(
    store: ProjectStore,
    session,
    *,
    job: Job,
    item: JobItem,
    succeeded: bool,
) -> dict[str, Any]:
    if item.image_id is None:
        raise PageLineageConflict(
            "G10 completion has no image",
            resource=f"job-item:{item.id}",
            reason="g10-publication-missing",
        )
    image = session.get(ImageAsset, item.image_id)
    generation_id = next(
        (
            page.get("pageGenerationId")
            for page in (job.lineage_context or {}).get("pages", [])
            if isinstance(page, dict) and page.get("imageId") == item.image_id
        ),
        None,
    )
    generation = session.get(PageGeneration, generation_id) if generation_id else None
    if image is None or generation is None:
        raise PageLineageConflict(
            "G10 completion lost its generation",
            resource=f"job-item:{item.id}",
            reason="generation-mismatch",
        )
    candidate = session.scalar(
        select(PageTypesetCandidate).where(PageTypesetCandidate.job_item_id == item.id)
    )
    if candidate is not None:
        if not succeeded:
            raise PageLineageConflict(
                "A published G10 candidate must recover to completion",
                resource=f"job-item:{item.id}",
                reason="g10-published-job-failed",
            )
        expected_candidate_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"g10-typeset:{generation.id}:{item.id}",
            )
        )
        recovered = _validate_persisted_typeset_publication(
            store,
            session,
            image=image,
            generation=generation,
            job=job,
            item=item,
            candidate_id=expected_candidate_id,
            allow_finishing=True,
        )
        if recovered is None:
            raise PageLineageConflict(
                "Strict G10 cannot complete without exact publication",
                resource=f"job-item:{item.id}",
                reason="g10-publication-missing",
            )
        return {
            "outputChecksum": recovered["outputState"],
            "provider": candidate.provider,
            "modelVersion": candidate.model_version,
            "parameterHash": candidate.parameter_hash,
            "evidence": {
                "candidateId": candidate.id,
                "candidateChecksum": candidate.candidate_checksum,
                "g9TerminalChecksum": candidate.g9_terminal_checksum,
                "cleanPlateChecksum": candidate.clean_plate_checksum,
                "routeChecksum": candidate.route_checksum,
                "styleChecksum": candidate.style_checksum,
                "layoutChecksum": candidate.layout_checksum,
                "width": candidate.width,
                "height": candidate.height,
                "renderScale": candidate.render_scale,
                "overflowRegionIds": candidate.overflow_region_ids,
                "anomalies": candidate.anomalies,
            },
        }
    bindings = _current_bindings(store, session, image, generation)
    replay = validate_typeset_replay(
        store,
        session,
        image,
        generation,
        bindings=bindings,
        allow_finishing_item_id=item.id,
    )
    enqueue = session.scalar(
        select(PageLineageEvent).where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "typeset-job-enqueued",
        )
    )
    produced = session.scalar(
        select(PageLineageEvent).where(
            PageLineageEvent.generation_id == generation.id,
            PageLineageEvent.job_item_id == item.id,
            PageLineageEvent.operation == "typeset-candidate-produced",
        )
    )
    if enqueue is None or replay["openItemId"] != item.id:
        raise PageLineageConflict(
            "G10 completion has no current enqueue",
            resource=f"job-item:{item.id}",
            reason="g10-publication-missing",
        )
    if succeeded:
        if produced is None or produced.output_checksum != replay["stateChecksum"]:
            raise PageLineageConflict(
                "Strict G10 cannot complete without exact publication",
                resource=f"job-item:{item.id}",
                reason="g10-publication-missing",
            )
    if produced is not None:
        raise PageLineageConflict(
            "A published G10 candidate must recover to completion",
            resource=f"job-item:{item.id}",
            reason="g10-published-job-failed",
        )
    return {
        "outputChecksum": None,
        "provider": enqueue.provider,
        "modelVersion": enqueue.model_version,
        "parameterHash": enqueue.parameter_hash,
        "evidence": {
            "g9TerminalChecksum": bindings["g9TerminalChecksum"],
            "cleanPlateChecksum": bindings["cleanPlateChecksum"],
            "routeChecksum": (enqueue.evidence or {}).get("routeChecksum"),
            "styleChecksum": (enqueue.evidence or {}).get("styleChecksum"),
        },
    }


def _public_review(row: PageTypesetReview) -> dict[str, Any]:
    return {
        "id": row.id,
        "sequence": row.sequence,
        "candidateId": row.candidate_id,
        "state": row.state,
        "reason": row.reason,
        "parentChecksum": row.parent_checksum,
        "candidateChecksum": row.candidate_checksum,
        "routeChecksum": row.route_checksum,
        "styleChecksum": row.style_checksum,
        "layoutChecksum": row.layout_checksum,
        "g9TerminalChecksum": row.g9_terminal_checksum,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "observedWidth": row.observed_width,
        "observedHeight": row.observed_height,
        "observedRenderScale": row.observed_render_scale,
        "checks": row.checks,
        "reviewer": row.reviewer,
        "terminalChecksum": row.terminal_checksum,
        "revisionId": row.revision_id,
        "createdAt": row.created_at,
    }


def _public_candidate(
    session, row: PageTypesetCandidate, review: PageTypesetReview | None
) -> dict[str, Any]:
    completed = session.scalar(
        select(PageLineageEvent.id).where(
            PageLineageEvent.generation_id == row.generation_id,
            PageLineageEvent.job_item_id == row.job_item_id,
            PageLineageEvent.operation == "typeset-job-completed",
        )
    )
    return {
        "candidateId": row.id,
        "sequence": row.sequence,
        "jobId": row.job_id,
        "jobItemId": row.job_item_id,
        "parentChecksum": row.parent_checksum,
        "g9TerminalChecksum": row.g9_terminal_checksum,
        "translationStateChecksum": row.translation_state_checksum,
        "cleanPlateCandidateId": (row.clean_plate_candidate_id or row.cloud_full_page_candidate_id),
        "cloudFullPageCandidateId": row.cloud_full_page_candidate_id,
        "cleanPlateChecksum": row.clean_plate_checksum,
        "regionManifest": row.region_manifest,
        "routeManifest": row.route_manifest,
        "routeChecksum": row.route_checksum,
        "styleManifest": row.style_manifest,
        "styleChecksum": row.style_checksum,
        "layoutManifest": row.layout_manifest,
        "layoutChecksum": row.layout_checksum,
        "provider": row.provider,
        "modelVersion": row.model_version,
        "parameterHash": row.parameter_hash,
        "candidateChecksum": row.candidate_checksum,
        "width": row.width,
        "height": row.height,
        "renderScale": row.render_scale,
        "overflowRegionIds": row.overflow_region_ids,
        "anomalies": row.anomalies,
        "revisionId": row.revision_id,
        "completed": completed is not None,
        "artifactUrl": f"/api/images/{row.image_id}/page-gates/typeset/candidates/{row.id}",
        "review": _public_review(review) if review is not None else None,
        "createdAt": row.created_at,
    }


def _retry_region_styles(candidate: PageTypesetCandidate | None) -> dict[str, dict[str, Any]]:
    if candidate is None:
        return {}
    writable = _STYLE_KEYS
    result: dict[str, dict[str, Any]] = {}
    for entry in candidate.style_manifest:
        style = entry.get("style")
        if not isinstance(style, dict):
            continue
        result[str(entry["regionId"])] = {
            key: value for key, value in style.items() if key in writable
        }
    return result


def typeset_gate_context(store: ProjectStore, image_id: str) -> dict[str, Any]:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Typeset image was not found")
        generation = _active(session, image)
        bindings = _current_bindings(store, session, image, generation)
        replay = validate_typeset_replay(store, session, image, generation, bindings=bindings)
        _regions, routes = _route_and_region_manifests(session, image, bindings["acceptedByRegion"])
        route_checksum = _digest(routes)
        reviews_by_candidate = {row.candidate_id: row for row in replay["reviews"]}
        latest_rejected: PageTypesetCandidate | None = None
        for candidate in replay["candidates"]:
            review = reviews_by_candidate.get(candidate.id)
            if review is not None and review.state == "rejected":
                latest_rejected = candidate
        accepted = replay["acceptedReview"]
        fonts = [_public_font(row) for row in installed_typeset_fonts()]
        return {
            "imageId": image.id,
            "imageRevision": image.revision,
            "generationId": generation.id,
            "nextSequence": generation.next_sequence,
            "g9TerminalChecksum": bindings["g9TerminalChecksum"],
            "translationStateChecksum": bindings["translationStateChecksum"],
            "cleanPlateCandidateId": (
                bindings["cleanPlateCandidate"].id
                if bindings["cleanPlateCandidate"] is not None
                else None
            ),
            "cleanPlateChecksum": bindings["cleanPlateChecksum"],
            "state": "accepted" if accepted is not None else "pending",
            "terminalChecksum": accepted.terminal_checksum if accepted is not None else None,
            "candidates": [
                _public_candidate(session, row, reviews_by_candidate.get(row.id))
                for row in replay["candidates"]
            ],
            "reviews": [_public_review(row) for row in replay["reviews"]],
            "routeManifest": routes,
            "routeChecksum": route_checksum,
            "styleDefaults": style_defaults(),
            "availableFonts": fonts,
            "availableDisplayFonts": [row for row in fonts if row["role"] == "display"],
            "artLetteringCapability": art_lettering_capability(),
            "retryRegionStyles": _retry_region_styles(latest_rejected),
        }


def typeset_artifact_path(store: ProjectStore, image_id: str, candidate_id: str) -> Path:
    with store.session() as session:
        image = session.get(ImageAsset, image_id)
        if image is None:
            raise ProjectError("Typeset image was not found")
        generation = _active(session, image)
        bindings = _current_bindings(store, session, image, generation)
        replay = validate_typeset_replay(store, session, image, generation, bindings=bindings)
        candidate = next((row for row in replay["candidates"] if row.id == candidate_id), None)
        if candidate is None:
            raise ProjectError("Typeset candidate was not found")
        return _validate_candidate_file(store, candidate)


def record_typeset_candidate_review(
    store: ProjectStore,
    image_id: str,
    candidate_id: str,
    *,
    decision: str,
    reason: str,
    observed_candidate_checksum: str,
    observed_route_checksum: str,
    observed_style_checksum: str,
    observed_layout_checksum: str,
    observed_translation_terminal_checksum: str,
    observed_clean_plate_checksum: str,
    observed_width: int,
    observed_height: int,
    observed_render_scale: float,
    checks: list[dict[str, Any]],
    expected_revision: int,
    lineage: dict[str, Any],
) -> tuple[ImageAsset, PageLineageEvent]:
    if decision not in {"accept", "reject"}:
        raise ProjectError("Typeset review decision must be accept or reject")
    state = "accepted" if decision == "accept" else "rejected"
    with store.lock:
        with store.session() as session:
            image = session.get(ImageAsset, image_id)
            if image is None:
                raise ProjectError("Typeset image was not found")
            if image.revision != expected_revision:
                raise RevisionConflict(
                    f"Image revision is {image.revision}, expected {expected_revision}",
                    expected_revision=expected_revision,
                    actual_revision=image.revision,
                    resource=f"image:{image.id}",
                )
            lineage_binding = require_image_mutation_lineage(store, session, image, lineage)
            if lineage_binding is None:
                raise PageLineageConflict(
                    "G10 review requires active lineage",
                    resource=f"image:{image.id}",
                    reason="lineage-required",
                )
            generation, actor, expected_sequence = lineage_binding
            bindings = _current_bindings(store, session, image, generation)
            replay = validate_typeset_replay(store, session, image, generation, bindings=bindings)
            if replay["acceptedReview"] is not None:
                raise PageLineageConflict(
                    "Accepted G10 evidence is immutable",
                    resource=f"image:{image.id}",
                    reason="g10-typeset-accepted",
                )
            candidate = next((row for row in replay["candidates"] if row.id == candidate_id), None)
            if candidate is None:
                raise ProjectError("Typeset candidate was not found")
            if replay["openItemId"] != candidate.job_item_id or not replay["completed"]:
                raise PageLineageConflict(
                    "G10 candidate cannot be reviewed before exact job completion",
                    resource=f"typeset-candidate:{candidate.id}",
                    reason="g10-review-not-ready",
                )
            if any(row.candidate_id == candidate.id for row in replay["reviews"]):
                raise PageLineageConflict(
                    "G10 candidate review is immutable",
                    resource=f"typeset-candidate:{candidate.id}",
                    reason="g10-candidate-already-reviewed",
                )
            observed = (
                observed_candidate_checksum,
                observed_route_checksum,
                observed_style_checksum,
                observed_layout_checksum,
                observed_translation_terminal_checksum,
                observed_clean_plate_checksum,
                observed_width,
                observed_height,
                float(observed_render_scale),
            )
            actual = (
                candidate.candidate_checksum,
                candidate.route_checksum,
                candidate.style_checksum,
                candidate.layout_checksum,
                candidate.g9_terminal_checksum,
                candidate.clean_plate_checksum,
                candidate.width,
                candidate.height,
                candidate.render_scale,
            )
            if observed != actual:
                raise PageLineageConflict(
                    "G10 observed candidate evidence is stale",
                    resource=f"typeset-candidate:{candidate.id}",
                    reason="g10-observation-mismatch",
                )
            _validate_candidate_file(store, candidate)
            if not _valid_visual_checks(checks):
                raise PageLineageConflict(
                    "G10 requires the exact ordered eight-check visual contract",
                    resource=f"typeset-candidate:{candidate.id}",
                    reason="g10-checks-invalid",
                )
            if (candidate.overflow_region_ids or candidate.anomalies) and checks[-1]["passed"]:
                raise PageLineageConflict(
                    "G10 overflow-free evidence contradicts known candidate defects",
                    resource=f"typeset-candidate:{candidate.id}",
                    reason="g10-checks-invalid",
                )
            failed = [entry["check"] for entry in checks if not entry["passed"]]
            if state == "accepted":
                if (
                    reason != "typeset-reviewed"
                    or failed
                    or candidate.overflow_region_ids
                    or candidate.anomalies
                ):
                    raise PageLineageConflict(
                        "G10 acceptance requires all checks and zero defects",
                        resource=f"typeset-candidate:{candidate.id}",
                        reason="g10-acceptance-invalid",
                    )
            elif (
                reason not in TYPESET_REJECT_REASONS
                or not failed
                or (reason == "multiple-visual-failures" and len(failed) < 2)
                or (reason != "multiple-visual-failures" and reason not in failed)
            ):
                raise PageLineageConflict(
                    "G10 rejection reason must match failed visual evidence",
                    resource=f"typeset-candidate:{candidate.id}",
                    reason="g10-rejection-invalid",
                )
            review_id = new_id()
            image.revision += 1
            revision = add_revision(
                session,
                store.project(session),
                entity_type="typeset-review",
                entity_id=review_id,
                operation="review",
                before=None,
                after={},
            )
            # The terminal checksum freezes the revision id, while the immutable
            # revision row must be inserted with its final payload. Allocate the
            # id in memory so there is no insert-then-update window.
            revision.id = new_id()
            review = PageTypesetReview(
                id=review_id,
                generation_id=generation.id,
                image_id=image.id,
                candidate_id=candidate.id,
                sequence=expected_sequence,
                state=state,
                reason=reason,
                parent_checksum=bindings["g9TerminalChecksum"],
                candidate_checksum=candidate.candidate_checksum,
                route_checksum=candidate.route_checksum,
                style_checksum=candidate.style_checksum,
                layout_checksum=candidate.layout_checksum,
                g9_terminal_checksum=candidate.g9_terminal_checksum,
                clean_plate_checksum=candidate.clean_plate_checksum,
                observed_width=candidate.width,
                observed_height=candidate.height,
                observed_render_scale=candidate.render_scale,
                checks=checks,
                reviewer=actor,
                terminal_checksum="0" * 64,
                revision_id=revision.id,
            )
            terminal_checksum = typeset_state_checksum(
                bindings["g9TerminalChecksum"],
                replay["candidates"],
                [*replay["reviews"], review],
            )
            review.terminal_checksum = terminal_checksum
            revision.after = {
                "candidateId": candidate.id,
                "state": state,
                "reason": reason,
                "terminalChecksum": terminal_checksum,
            }
            session.flush([revision])
            session.add(review)
            session.flush()
            event = _append_event(
                session,
                generation,
                operation="typeset-candidate-reviewed",
                gate="G10_typeset",
                state=state,
                actor=actor,
                input_checksum=replay["stateChecksum"],
                output_checksum=terminal_checksum,
                parent_checksum=bindings["g9TerminalChecksum"],
                stage="typeset",
                provider=candidate.provider,
                model_version=candidate.model_version,
                parameter_hash=candidate.parameter_hash,
                revision_id=revision.id,
                decision=f"candidate-{state}",
                reason=reason,
                evidence={
                    "eventType": "typeset-candidate-reviewed",
                    "qualityState": state,
                    "targetKind": "typeset-candidate",
                    "candidateId": candidate.id,
                    "candidateChecksum": candidate.candidate_checksum,
                    "g9TerminalChecksum": candidate.g9_terminal_checksum,
                    "cleanPlateChecksum": candidate.clean_plate_checksum,
                    "routeChecksum": candidate.route_checksum,
                    "styleChecksum": candidate.style_checksum,
                    "layoutChecksum": candidate.layout_checksum,
                    "width": candidate.width,
                    "height": candidate.height,
                    "renderScale": candidate.render_scale,
                    "overflowRegionIds": candidate.overflow_region_ids,
                    "anomalies": candidate.anomalies,
                    "checks": checks,
                },
                expected_sequence=expected_sequence,
            )
        store.write_snapshot()
    return image, event


def require_current_typeset_acceptance(
    store: ProjectStore,
    session,
    image: ImageAsset,
    generation: PageGeneration,
) -> tuple[str, Path, PageTypesetCandidate]:
    bindings = _current_bindings(store, session, image, generation)
    replay = validate_typeset_replay(store, session, image, generation, bindings=bindings)
    review = replay["acceptedReview"]
    if review is None or review.terminal_checksum != replay["stateChecksum"]:
        raise PageLineageConflict(
            "G10 typeset is not currently accepted",
            resource=f"image:{image.id}",
            reason="g10-typeset-not-currently-accepted",
        )
    candidate = next((row for row in replay["candidates"] if row.id == review.candidate_id), None)
    if candidate is None:
        raise PageLineageConflict(
            "G10 accepted candidate is missing",
            resource=f"image:{image.id}",
            reason="g10-replay-invalid",
        )
    return review.terminal_checksum, _validate_candidate_file(store, candidate), candidate
