from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from manga_localizer.config import Settings
from manga_localizer.database import (
    ImageAsset,
    Job,
    JobItem,
    PageGeneration,
    PageLineageEvent,
    PageTypesetCandidate,
    PageTypesetReview,
    RegionTranslationCandidate,
    Revision,
    TextRegion,
)
from manga_localizer.imaging import typesetting as imaging_typesetting
from manga_localizer.main import create_app
from manga_localizer.services import exporting as exporting_service
from manga_localizer.services import typesets as typeset_service
from manga_localizer.services.exporting import ensure_project_bundle
from manga_localizer.services.page_lineage import PageLineageConflict, job_mutation_binding
from manga_localizer.services.projects import ProjectError
from manga_localizer.services.typesets import (
    publish_typeset_candidate,
    require_current_typeset_acceptance,
)

from .test_page_lineage import (
    _TRANSLATION_CHECKS,
    _create_g9_candidate,
    _current_lineage_context,
    _mutation_lineage,
    _prepare_g6_accepted_page,
    _prepare_g8_accepted_page,
)

_TYPESET_CHECKS = [
    {"check": check, "passed": True} for check in typeset_service.TYPESET_VISUAL_CHECKS
]


def _prepare_g9_terminal(
    client: TestClient,
    app,
    tmp_path: Path,
    *,
    disposition: str = "translate",
    region_type: str = "dialogue",
    extra_dispositions: tuple[str, ...] = (),
    base_rotation: float = 0,
    prepared: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared = _prepare_g8_accepted_page(
        client,
        app,
        tmp_path,
        disposition=disposition,
        region_type=region_type,
        extra_dispositions=extra_dispositions,
        rotation=base_rotation,
        prepared=prepared,
    )
    return _complete_g9_terminal(client, prepared)


def _complete_g9_terminal(
    client: TestClient,
    prepared: dict[str, object],
    *,
    translation_text: str | None = None,
) -> dict[str, object]:
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    for index, region in enumerate(context["eligibleRegions"]):
        context = _create_g9_candidate(
            client,
            image_id=str(image["id"]),
            generation_id=generation_id,
            context=context,
            region=region,
            translation_text=(
                translation_text if translation_text is not None else f"第{index + 1}句合规译文"
            ),
        )
    for candidate in list(context["candidates"]):
        context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
        reviewed = client.patch(
            f"/api/images/{image['id']}/page-gates/translation/candidates/"
            f"{candidate['candidateId']}",
            json={
                "decision": "accept",
                "reason": "translation-reviewed",
                "observedCandidateChecksum": candidate["candidateChecksum"],
                "observedSourceTextChecksum": candidate["sourceTextChecksum"],
                "observedContextChecksum": candidate["contextChecksum"],
                "observedG8Checksum": context["g8Checksum"],
                "checks": _TRANSLATION_CHECKS,
                "qcFlags": ["none"],
                "expectedRevision": context["imageRevision"],
                "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
            },
        )
        assert reviewed.status_code == 200, reviewed.text
    context = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    terminal = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "accept",
            "observedTranslationStateChecksum": context["translationStateChecksum"],
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert terminal.status_code == 200, terminal.text
    return prepared | {
        "g9Context": client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    }


def _run_typeset(
    client: TestClient,
    app,
    prepared: dict[str, object],
    *,
    options: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": options or {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    job = app.state.queue.get_job(store, queued.json()["id"])
    assert job.status == "completed", (job.error, [item.error for item in job.items])
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset")
    assert context.status_code == 200, context.text
    return queued.json(), context.json()


def _review_body(
    context: dict[str, object],
    candidate: dict[str, object],
    generation_id: str,
    *,
    decision: str = "accept",
    reason: str = "typeset-reviewed",
    checks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "decision": decision,
        "reason": reason,
        "observedCandidateChecksum": candidate["candidateChecksum"],
        "observedRouteChecksum": candidate["routeChecksum"],
        "observedStyleChecksum": candidate["styleChecksum"],
        "observedLayoutChecksum": candidate["layoutChecksum"],
        "observedTranslationTerminalChecksum": candidate["g9TerminalChecksum"],
        "observedCleanPlateChecksum": candidate["cleanPlateChecksum"],
        "observedWidth": candidate["width"],
        "observedHeight": candidate["height"],
        "observedRenderScale": candidate["renderScale"],
        "checks": checks or _TYPESET_CHECKS,
        "expectedRevision": context["imageRevision"],
        "lineage": _mutation_lineage(generation_id, int(context["nextSequence"])),
    }


def test_g10_whole_page_candidate_review_terminal_and_consumer(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path, base_rotation=7)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    initial = client.get(f"/api/images/{image['id']}/page-gates/typeset")
    assert initial.status_code == 200, initial.text
    assert initial.json()["state"] == "pending"
    assert initial.json()["routeManifest"][0]["route"] == "bubble"
    assert initial.json()["availableFonts"]
    default = initial.json()["styleDefaults"]["bubble"]
    font = next(
        row for row in initial.json()["availableFonts"] if row["token"] == default["fontToken"]
    )
    assert font["fontChecksum"] == default["fontChecksum"]

    partial = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "regionIds": [initial.json()["routeManifest"][0]["regionId"]],
            "options": {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert partial.status_code == 409
    assert partial.json()["detail"]["reason"] == "g10-whole-page-required"
    render = client.post(
        f"/api/projects/{project['id']}/render",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert render.status_code == 409
    assert render.json()["detail"]["reason"] == "g10-render-blocked"
    unsupported_normal = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {
                "regionStyles": {initial.json()["routeManifest"][0]["regionId"]: {"scaleX": 1.2}}
            },
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert unsupported_normal.status_code == 409
    assert unsupported_normal.json()["detail"]["reason"] == "g10-style-invalid"

    _job, context = _run_typeset(
        client,
        app,
        prepared,
        options={
            "regionStyles": {
                initial.json()["routeManifest"][0]["regionId"]: {
                    "rotation": 11,
                    "verticalAlign": "center",
                }
            }
        },
    )
    assert len(context["candidates"]) == 1
    candidate = context["candidates"][0]
    assert candidate["routeManifest"][0]["route"] == "bubble"
    assert candidate["layoutManifest"][0]["rotation"] == pytest.approx(18)
    assert candidate["styleManifest"][0]["style"]["verticalAlign"] == "center"
    assert candidate["candidateChecksum"] != candidate["parentChecksum"]
    produced = next(
        event
        for event in client.get(f"/api/page-generations/{generation_id}/events").json()
        if event["operation"] == "typeset-candidate-produced"
    )
    assert produced["outputChecksum"] != candidate["candidateChecksum"]
    assert "fontToken" not in json_text(produced["evidence"])
    artifact = client.get(candidate["artifactUrl"])
    assert artifact.status_code == 200
    assert hashlib.sha256(artifact.content).hexdigest() == candidate["candidateChecksum"]

    legacy = client.patch(
        f"/api/images/{image['id']}/stage-reviews/typeset",
        json={
            "state": "pending",
            "expectedRevision": context["imageRevision"],
            "lineage": _mutation_lineage(generation_id, context["nextSequence"]),
        },
    )
    assert legacy.status_code == 409
    assert legacy.json()["detail"]["reason"] == "g10-legacy-stage-review-blocked"

    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(context, candidate, generation_id),
    )
    assert accepted.status_code == 200, accepted.text
    final = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert final["state"] == "accepted"
    assert final["terminalChecksum"] == accepted.json()["event"]["outputChecksum"]
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        generation = session.get(PageGeneration, generation_id)
        assert image_row is not None and generation is not None
        checksum, path, row = require_current_typeset_acceptance(
            store, session, image_row, generation
        )
        assert checksum == final["terminalChecksum"]
        assert row.id == candidate["candidateId"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == candidate["candidateChecksum"]

    immutable_enqueue = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert immutable_enqueue.status_code == 409
    assert immutable_enqueue.json()["detail"]["reason"] == "g10-typeset-accepted"
    immutable_review = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(final, candidate, generation_id),
    )
    assert immutable_review.status_code == 409
    assert immutable_review.json()["detail"]["reason"] == "g10-typeset-accepted"
    immutable_legacy = client.patch(
        f"/api/images/{image['id']}/stage-reviews/typeset",
        json={
            "state": "pending",
            "expectedRevision": final["imageRevision"],
            "lineage": _mutation_lineage(generation_id, final["nextSequence"]),
        },
    )
    assert immutable_legacy.status_code == 409
    assert immutable_legacy.json()["detail"]["reason"] == "g10-legacy-stage-review-blocked"


def json_text(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_g10_redraw_art_route_affine_pixels_and_capability_fail_closed(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g9_terminal(
        client,
        app,
        tmp_path,
        disposition="redraw-art",
        region_type="sound_effect",
    )
    image = prepared["targetImage"]
    project = prepared["targetProject"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict) and isinstance(project, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert [row["route"] for row in context["routeManifest"]] == ["art-lettering"]
    assert context["artLetteringCapability"]["features"] == [
        "explicit-installed-chinese-display-font",
        "fill-stroke",
        "rotation",
        "nonuniform-scale",
        "shear-affine",
        "opacity",
        "visual-center",
        "alignment",
        "line-spacing",
    ]
    region_id = context["routeManifest"][0]["regionId"]
    display = context["availableDisplayFonts"][0]
    override = {
        "fontToken": display["token"],
        "fontSize": 36,
        "minFontSize": 10,
        "fill": "#CC2200",
        "strokeColor": "#FFFFFF",
        "strokeWidth": 2,
        "rotation": 17,
        "scaleX": 1.2,
        "scaleY": 0.8,
        "shearX": 0.2,
        "shearY": -0.1,
        "opacity": 0.85,
        "visualCenterX": 0.4,
        "visualCenterY": 0.6,
        "align": "end",
        "lineSpacing": 0.25,
    }
    unsupported_spacing = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {"regionStyles": {region_id: {"letterSpacing": 1}}},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert unsupported_spacing.status_code == 409
    assert unsupported_spacing.json()["detail"]["reason"] == "g10-art-lettering-capability-required"
    _job, produced = _run_typeset(
        client, app, prepared, options={"regionStyles": {region_id: override}}
    )
    candidate = produced["candidates"][0]
    assert candidate["routeManifest"][0]["route"] == "art-lettering"
    style = candidate["styleManifest"][0]["style"]
    assert style["fontToken"] == display["token"]
    assert style["fontChecksum"] == display["fontChecksum"]
    assert style["fontSource"] == "region-override"
    assert candidate["layoutManifest"][0]["rotation"] == pytest.approx(17)

    unsupported_prepared = _prepare_g9_terminal(
        client,
        app,
        tmp_path / "unsupported",
        disposition="redraw-art",
        region_type="sound_effect",
    )
    unsupported_image = unsupported_prepared["targetImage"]
    unsupported_project = unsupported_prepared["targetProject"]
    unsupported_generation = str(unsupported_prepared["generationId"])
    assert isinstance(unsupported_image, dict) and isinstance(unsupported_project, dict)
    unsupported_context = client.get(
        f"/api/images/{unsupported_image['id']}/page-gates/typeset"
    ).json()
    unsupported_region = unsupported_context["routeManifest"][0]["regionId"]
    blocked = client.post(
        f"/api/projects/{unsupported_project['id']}/typeset",
        json={
            "imageIds": [unsupported_image["id"]],
            "options": {"regionStyles": {unsupported_region: {"curveWarp": 0.5}}},
            "lineage": _current_lineage_context(
                client, str(unsupported_image["id"]), unsupported_generation
            ),
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "g10-art-lettering-capability-required"

    no_font_prepared = _prepare_g9_terminal(
        client,
        app,
        tmp_path / "no-font",
        disposition="redraw-art",
        region_type="sound_effect",
    )
    no_font_image = no_font_prepared["targetImage"]
    no_font_project = no_font_prepared["targetProject"]
    no_font_generation = str(no_font_prepared["generationId"])
    assert isinstance(no_font_image, dict) and isinstance(no_font_project, dict)
    monkeypatch.setattr(typeset_service, "_display_fonts", lambda: [])
    blocked_font = client.post(
        f"/api/projects/{no_font_project['id']}/typeset",
        json={
            "imageIds": [no_font_image["id"]],
            "options": {},
            "lineage": _current_lineage_context(
                client, str(no_font_image["id"]), no_font_generation
            ),
        },
    )
    assert blocked_font.status_code == 409
    assert blocked_font.json()["detail"]["reason"] == "g10-art-lettering-capability-required"


def _compact_display_font() -> dict:
    for record in typeset_service._display_fonts():
        font = ImageFont.truetype(str(record["path"]), size=36)
        missing = font.getmask(chr(0x10FFFF), mode="L")
        if all(
            font.getmask(char, mode="L").getbbox() is not None
            and (font.getmask(char, mode="L").size, bytes(font.getmask(char, mode="L")))
            != (missing.size, bytes(missing))
            for char in "缩地﹃﹄"
        ):
            return record
    pytest.skip("No installed display font covers the compact-title fixture")


def _compact_draw_options(scale: int = 1) -> dict:
    return {
        "font_path": _compact_display_font()["path"],
        "font_size": 40 * scale,
        "direction": "vertical",
        "fill": "#000000",
        "stroke_color": "#ffffff",
        "stroke_width": 2 * scale,
        "letter_spacing": 0,
        "line_spacing": 0.15,
        "align": "center",
        "vertical_quote_layout": "compact-corner-quotes-v1",
    }


def _compact_glyph_tiles(measured: dict, rendered: Image.Image) -> list[Image.Image]:
    boxes = []
    for glyph in measured["glyphs"]:
        if measured.get("compactModifiers") is not None:
            boxes.append(
                (glyph["x"], glyph["y"], glyph["x"] + glyph["width"], glyph["y"] + glyph["height"])
            )
        else:
            boxes.append(tuple(glyph["bbox"][i] + glyph["xy"[i % 2]] for i in range(4)))
    return [rendered.crop(box) for box in boxes]


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_compact_modifiers_neutral_values_keep_original_pixels_and_style_defaults(
    scale: int,
) -> None:
    options = _compact_draw_options(scale)
    old_plan = typeset_service._measure_art_text("『缩地』", **options)
    neutral = options | {"body_stroke_width": 0, "quote_scale_x": 1, "quote_scale_y": 1}
    neutral_plan = typeset_service._measure_art_text("『缩地』", **neutral)
    old = typeset_service._draw_art_text("『缩地』", _measurement=old_plan, **options)
    current = typeset_service._draw_art_text("『缩地』", _measurement=neutral_plan, **neutral)
    assert old.size == current.size and old.tobytes() == current.tobytes()
    assert "compactModifiers" not in neutral_plan and "extraPixelWork" not in neutral_plan
    assert {k: v for k, v in old_plan.items() if k != "font"} == {
        k: v for k, v in neutral_plan.items() if k != "font"
    }
    absent = typeset_service._normalize_style(
        "art-lettering", {"verticalQuoteLayout": "compact-corner-quotes-v1"}
    )
    explicit = typeset_service._normalize_style(
        "art-lettering",
        {
            "verticalQuoteLayout": "compact-corner-quotes-v1",
            "bodyStrokeWidth": 0,
            "quoteScaleX": 1,
            "quoteScaleY": 1,
        },
    )
    assert not {"bodyStrokeWidth", "quoteScaleX", "quoteScaleY"} & absent.keys()
    assert typeset_service._digest(absent) != typeset_service._digest(explicit)
    assert absent == {
        k: v
        for k, v in explicit.items()
        if k not in {"bodyStrokeWidth", "quoteScaleX", "quoteScaleY"}
    }


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_compact_modifiers_body_weight_increases_ink_without_changing_quotes(scale: int) -> None:
    options = _compact_draw_options(scale)
    old_plan = typeset_service._measure_art_text("『缩地』", **options)
    old = typeset_service._draw_art_text("『缩地』", _measurement=old_plan, **options)
    weighted = options | {"body_stroke_width": 2 * scale}
    plan = typeset_service._measure_art_text("『缩地』", **weighted)
    assert plan["margin"] == old_plan["margin"]
    rendered = typeset_service._draw_art_text("『缩地』", _measurement=plan, **weighted)
    before, after = _compact_glyph_tiles(old_plan, old), _compact_glyph_tiles(plan, rendered)

    def count_color(tile: Image.Image, color: tuple[int, int, int]) -> int:
        raw = tile.tobytes()
        return sum(
            tuple(raw[i : i + 3]) == color and raw[i + 3] == 255 for i in range(0, len(raw), 4)
        )

    for index in (0, 3):
        assert before[index].size == after[index].size
        assert before[index].tobytes() == after[index].tobytes()
    for index in (1, 2):
        assert count_color(after[index], (0, 0, 0)) > count_color(before[index], (0, 0, 0))
        assert count_color(after[index], (255, 255, 255)) > 0
        assert after[index].width > before[index].width


@pytest.mark.parametrize("scale", [1, 2, 4])
@pytest.mark.parametrize("stroke", [0, 2, 8])
def test_compact_modifiers_tiles_match_unclipped_reference_and_scaled_quote_layout(
    scale: int, stroke: int
) -> None:
    options = _compact_draw_options(scale) | {
        "stroke_width": stroke * scale,
        "body_stroke_width": 2 * scale,
        "quote_scale_x": 0.65,
        "quote_scale_y": 0.45,
    }
    measured = typeset_service._measure_art_text("『缩地』", **options)
    rendered = typeset_service._draw_art_text("『缩地』", _measurement=measured, **options)
    tiles = _compact_glyph_tiles(measured, rendered)
    for glyph, tile in zip(measured["glyphs"], tiles, strict=True):
        # An independently oversized drawing canvas detects any clipping in the
        # measured ink bbox, including the second black stroke over the outline.
        pad = 3 * options["font_size"]
        reference = Image.new("RGBA", (pad * 3, pad * 3))
        draw = ImageDraw.Draw(reference)
        draw.text(
            (pad, pad),
            glyph["text"],
            font=measured["font"],
            fill="black",
            stroke_width=glyph["outerStrokeWidth"],
            stroke_fill="white",
        )
        if glyph["role"] == "body":
            draw.text(
                (pad, pad),
                glyph["text"],
                font=measured["font"],
                fill="black",
                stroke_width=2 * scale,
                stroke_fill="black",
            )
        ink = reference.getchannel("A").getbbox()
        assert ink is not None
        reference = reference.crop(ink)
        assert reference.size == (
            glyph["bbox"][2] - glyph["bbox"][0],
            glyph["bbox"][3] - glyph["bbox"][1],
        )
        if glyph["role"] == "quote":
            assert tile.size == (
                max(1, round(reference.width * 0.65)),
                max(1, round(reference.height * 0.45)),
            )
            reference = reference.resize(tile.size, Image.Resampling.BICUBIC)
        # Match placement onto a transparent layer: alpha compositing removes
        # undefined RGB bytes under zero alpha left by bicubic interpolation.
        placed = Image.new("RGBA", reference.size)
        placed.alpha_composite(reference)
        reference = placed
        assert tile.size == reference.size and tile.tobytes() == reference.tobytes()
    glyphs = measured["glyphs"]
    assert (
        glyphs[1]["y"] - glyphs[0]["y"] - glyphs[0]["height"]
        == glyphs[3]["y"] - glyphs[2]["y"] - glyphs[2]["height"]
        > 0
    )
    assert all(a["y"] + a["height"] <= b["y"] for a, b in pairwise(glyphs))
    ink = rendered.getchannel("A").getbbox()
    assert ink is not None
    assert ink[0] >= measured["margin"] and ink[1] >= measured["margin"]
    assert (
        ink[2] <= rendered.width - measured["margin"]
        and ink[3] <= rendered.height - measured["margin"]
    )


@pytest.mark.parametrize("quote_scale", [0.25, 0.6, 4])
def test_compact_modifiers_quote_scaling_does_not_resize_body(quote_scale: float) -> None:
    options = _compact_draw_options()
    old_plan = typeset_service._measure_art_text("『缩地』", **options)
    old = typeset_service._draw_art_text("『缩地』", _measurement=old_plan, **options)
    changed = options | {"quote_scale_x": quote_scale, "quote_scale_y": quote_scale}
    plan = typeset_service._measure_art_text("『缩地』", **changed)
    rendered = typeset_service._draw_art_text("『缩地』", _measurement=plan, **changed)
    before, after = _compact_glyph_tiles(old_plan, old), _compact_glyph_tiles(plan, rendered)
    for index in (1, 2):
        assert before[index].size == after[index].size
        assert before[index].tobytes() == after[index].tobytes()
    for index in (0, 3):
        assert after[index].size == (
            max(1, round(before[index].width * quote_scale)),
            max(1, round(before[index].height * quote_scale)),
        )
        assert after[index].getchannel("A").getbbox() is not None


@pytest.mark.parametrize("field", ["bodyStrokeWidth", "quoteScaleX", "quoteScaleY"])
@pytest.mark.parametrize("value", [None, False, True, "1", -1, 33, float("nan"), float("inf")])
def test_compact_modifiers_reject_invalid_values_and_noncompact_routes(
    field: str, value: object
) -> None:
    with pytest.raises(PageLineageConflict):
        typeset_service._normalize_style(
            "art-lettering", {"verticalQuoteLayout": "compact-corner-quotes-v1", field: value}
        )
    for route in ("bubble", "ordinary", "art-lettering"):
        with pytest.raises(PageLineageConflict):
            typeset_service._normalize_style(route, {field: 1})


def test_compact_modifiers_require_compact_mode_and_matching_measurement() -> None:
    options = _compact_draw_options()
    for field in ("body_stroke_width", "quote_scale_x", "quote_scale_y"):
        with pytest.raises(PageLineageConflict):
            typeset_service._draw_art_text(
                "『缩地』", **(options | {"vertical_quote_layout": None, field: 1})
            )
    changed = options | {"body_stroke_width": 2}
    plan = typeset_service._measure_art_text("『缩地』", **changed)
    with pytest.raises(PageLineageConflict, match="does not match"):
        typeset_service._draw_art_text("『缩地』", _measurement=plan, **options)
    with pytest.raises(PageLineageConflict):
        typeset_service._normalize_style(
            "art-lettering",
            {"verticalQuoteLayout": "compact-corner-quotes-v1", "bodyStrokeWidth": 0.5},
        )
    for field in ("quoteScaleX", "quoteScaleY"):
        with pytest.raises(PageLineageConflict):
            typeset_service._normalize_style(
                "art-lettering", {"verticalQuoteLayout": "compact-corner-quotes-v1", field: 0.24}
            )


def test_compact_modifiers_failed_probes_consume_budget_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _compact_draw_options() | {"body_stroke_width": 2}
    measured = typeset_service._measure_art_text("『缩地』", **options)
    partial = {"remaining": measured["extraPixelWork"] // 2}
    initial = partial["remaining"]
    with pytest.raises(PageLineageConflict):
        typeset_service._measure_art_text("『缩地』", measurement_budget=partial, **options)
    assert 0 <= partial["remaining"] < initial
    with pytest.raises(ValueError):
        typeset_service._measure_art_text("『缩地』", pixel_work_limit=True, **options)
    budget = {"remaining": 2_000_000}
    spent = []
    measure = typeset_service._measure_art_text

    def counted_measure(*args, **kwargs):
        result = measure(*args, **kwargs)
        spent.append(result["extraPixelWork"])
        return result

    def reject_transform(*args, **kwargs):
        raise PageLineageConflict(
            "test oversized transform",
            resource="typeset",
            reason="g10-art-lettering-resource-limit",
        )

    style = typeset_service._normalize_style(
        "art-lettering",
        {
            "fontToken": _compact_display_font()["token"],
            "fontSize": 80,
            "minFontSize": 6,
            "bodyStrokeWidth": 2,
            "verticalQuoteLayout": "compact-corner-quotes-v1",
        },
    )
    region = {
        "regionId": "compact",
        "geometry": {"x": 10, "y": 10, "width": 180, "height": 360, "rotation": 0},
        "direction": "vertical",
        "readingOrder": 0,
        "paragraphGroupId": None,
    }
    monkeypatch.setattr(typeset_service, "_measure_art_text", counted_measure)
    monkeypatch.setattr(typeset_service, "_art_transform_plan", reject_transform)
    with pytest.raises(PageLineageConflict):
        typeset_service._render_art_region(
            Image.new("RGBA", (400, 400)),
            text="『缩地』",
            region=region,
            style=style,
            scale=1,
            work_budget=budget,
        )
    assert len(spent) > 1
    assert budget["remaining"] == 2_000_000 - sum(spent)
    assert 0 <= budget["remaining"] < 2_000_000


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_compact_modifiers_extreme_strokes_and_quote_scales_remain_bounded(scale: int) -> None:
    options = _compact_draw_options(scale) | {
        "stroke_width": 32 * scale,
        "body_stroke_width": 32 * scale,
        "quote_scale_x": 4,
        "quote_scale_y": 0.25,
    }
    measured = typeset_service._measure_art_text("『缩地』", **options)
    rendered = typeset_service._draw_art_text("『缩地』", _measurement=measured, **options)
    assert rendered.width < typeset_service._ART_LAYER_MAX_SIDE
    assert rendered.height < typeset_service._ART_LAYER_MAX_SIDE
    assert rendered.width * rendered.height < typeset_service._ART_LAYER_MAX_PIXELS
    assert rendered.getchannel("A").getbbox() is not None


def test_compact_modifiers_exhausted_budget_rejects_before_glyph_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _compact_draw_options() | {"body_stroke_width": 2}
    calls = []

    def forbidden_probe(*args, **kwargs):
        calls.append(1)
        raise AssertionError("exhausted budget must reject before rasterizing glyphs")

    monkeypatch.setattr(typeset_service, "_require_compact_quote_glyphs", forbidden_probe)
    with pytest.raises(PageLineageConflict) as captured:
        typeset_service._measure_art_text("『缩地』", pixel_work_limit=0, **options)
    assert captured.value.reason == "g10-art-lettering-resource-limit"
    assert calls == []


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_compact_corner_quotes_use_complete_ink_bounds_and_equal_end_gaps(scale: int) -> None:
    options = _compact_draw_options(scale)
    measured = typeset_service._measure_art_text("『缩地』", **options)
    rendered = typeset_service._draw_art_text("『缩地』", _measurement=measured, **options)
    repeated = typeset_service._draw_art_text("『缩地』", **options)
    legacy = typeset_service._draw_art_text(
        "『缩地』", **(options | {"vertical_quote_layout": None})
    )
    assert rendered.tobytes() == repeated.tobytes()
    assert rendered.height < legacy.height
    assert "".join(g["text"] for g in measured["glyphs"]) == "﹃缩地﹄"
    alpha = rendered.getchannel("A")
    ink = alpha.getbbox()
    assert ink is not None
    assert ink[0] >= measured["margin"] and ink[1] >= measured["margin"]
    assert ink[2] <= rendered.width - measured["margin"]
    assert ink[3] <= rendered.height - measured["margin"]
    boxes = [
        (g["x"] + g["bbox"][0], g["y"] + g["bbox"][1], g["x"] + g["bbox"][2], g["y"] + g["bbox"][3])
        for g in measured["glyphs"]
    ]
    for box in boxes:
        assert alpha.crop(box).getbbox() is not None
    assert boxes[1][1] - boxes[0][3] == boxes[3][1] - boxes[2][3]
    assert boxes[1][1] - boxes[0][3] > 0
    assert all(a[3] <= b[1] for a, b in pairwise(boxes))


def test_compact_corner_quotes_respect_column_alignment_and_safe_large_size() -> None:
    options = _compact_draw_options()
    plans = {
        align: typeset_service._measure_art_text("『缩地』", **(options | {"align": align}))
        for align in ("start", "center", "end")
    }
    assert plans["start"]["glyphs"][0]["x"] < plans["end"]["glyphs"][0]["x"]
    # Four full cells at 3*font_size would incorrectly reject this safe layer.
    large = typeset_service._measure_art_text(
        "『缩地』", **(options | {"font_size": 2048, "stroke_width": 8})
    )
    assert large["width"] * large["height"] < typeset_service._ART_LAYER_MAX_PIXELS
    assert large["height"] < typeset_service._ART_LAYER_MAX_SIDE


@pytest.mark.parametrize("route", ["bubble", "ordinary", "art-lettering"])
def test_compact_corner_quotes_are_never_a_default_style(route: str) -> None:
    assert "verticalQuoteLayout" not in typeset_service._normalize_style(route, {})
    assert typeset_service.TYPESET_MODEL_VERSION == "g10-typeset-v1"
    assert typeset_service.TYPESET_CONTRACT_VERSION == "g10-typeset-v1"
    if route != "art-lettering":
        with pytest.raises(PageLineageConflict, match="versioned art-lettering"):
            typeset_service._normalize_style(
                route, {"verticalQuoteLayout": "compact-corner-quotes-v1"}
            )


@pytest.mark.parametrize("value", [None, False, True, 1, "", "future-v2", [], {}])
def test_compact_corner_quotes_reject_invalid_mode_values(value: object) -> None:
    with pytest.raises(PageLineageConflict):
        typeset_service._normalize_style("art-lettering", {"verticalQuoteLayout": value})


@pytest.mark.parametrize(
    "text",
    [
        "缩地",
        "『缩地",
        "缩地』",
        "『』",
        "『 』",
        "『缩\n地』",
        "『缩\r地』",
        "『「缩」地』",
        "「缩地」",
    ],
)
@pytest.mark.parametrize("modifiers", [{}, {"body_stroke_width": 2}])
def test_compact_corner_quotes_reject_unsupported_title_structure(
    text: str, modifiers: dict
) -> None:
    with pytest.raises(PageLineageConflict) as error:
        typeset_service._draw_art_text(text, **(_compact_draw_options() | modifiers))
    assert error.value.reason == "g10-style-invalid"


@pytest.mark.parametrize("modifiers", [{}, {"body_stroke_width": 2}])
def test_compact_corner_quotes_reject_horizontal_missing_glyph_and_oversized_title(
    modifiers: dict,
) -> None:
    options = _compact_draw_options() | modifiers
    with pytest.raises(PageLineageConflict) as error:
        typeset_service._draw_art_text("『缩地』", **(options | {"direction": "horizontal"}))
    assert error.value.reason == "g10-style-invalid"
    with pytest.raises(PageLineageConflict) as error:
        typeset_service._draw_art_text("『缩" + chr(0x10FFFF) + "』", **options)
    assert error.value.reason == "g10-art-lettering-capability-required"
    with pytest.raises(PageLineageConflict) as error:
        typeset_service._draw_art_text("『" + "缩" * 2048 + "』", **options)
    assert error.value.reason == "g10-art-lettering-resource-limit"
    font = ImageFont.truetype(str(options["font_path"]), size=36)

    class MissingPresentationQuote:
        def getmask(self, char, **kwargs):
            return font.getmask(chr(0x10FFFF) if char == "﹃" else char, **kwargs)

    with pytest.raises(PageLineageConflict) as error:
        typeset_service._require_compact_quote_glyphs(MissingPresentationQuote(), "缩地")
    assert error.value.reason == "g10-art-lettering-capability-required"


@pytest.mark.parametrize("scale", [1, 2, 4])
@pytest.mark.parametrize(
    "modifiers", [{}, {"bodyStrokeWidth": 2, "quoteScaleX": 0.7, "quoteScaleY": 0.45}]
)
def test_compact_corner_quotes_autofit_and_affine_remain_bounded(
    scale: int, modifiers: dict
) -> None:
    display = _compact_display_font()
    style = typeset_service._normalize_style(
        "art-lettering",
        {
            "fontToken": display["token"],
            "fontSize": 128,
            "minFontSize": 6,
            "padding": 0,
            "rotation": 5,
            "scaleX": 1.05,
            "scaleY": 1.1,
            "shearX": 0.02,
            "verticalQuoteLayout": "compact-corner-quotes-v1",
            **modifiers,
        },
    )
    region = {
        "regionId": "compact",
        "geometry": {"x": 20, "y": 20, "width": 100, "height": 260, "rotation": 0},
        "direction": "vertical",
        "readingOrder": 0,
        "paragraphGroupId": None,
    }
    canvas = Image.new("RGBA", (400 * scale, 400 * scale), "white")
    budget = {"remaining": typeset_service._ART_TOTAL_PIXEL_WORK_BUDGET}
    layout = typeset_service._render_art_region(
        canvas, text="『缩地』", region=region, style=style, scale=scale, work_budget=budget
    )
    assert not layout["overflow"]
    assert layout["verticalQuoteLayout"] == "compact-corner-quotes-v1"
    assert 0 <= budget["remaining"] < typeset_service._ART_TOTAL_PIXEL_WORK_BUDGET
    for increment in (0, 1):
        options = _compact_draw_options(scale) | {
            "font_size": layout["fontSize"] + increment,
            "stroke_width": round(style["strokeWidth"] * scale),
        }
        if modifiers:
            options.update(body_stroke_width=2 * scale, quote_scale_x=0.7, quote_scale_y=0.45)
            assert all(layout[field] == value for field, value in modifiers.items())
        raw = typeset_service._draw_art_text("『缩地』", **options)
        transformed = typeset_service._art_transform(raw, style, rotation=5)
        fits = transformed.width <= 100 * scale and transformed.height <= 260 * scale
        assert fits == (increment == 0)


@pytest.mark.parametrize(
    "field,tamper",
    [
        (field, tamper)
        for field in ("verticalQuoteLayout", "bodyStrokeWidth", "quoteScaleX", "quoteScaleY")
        for tamper in ("remove", "value")
    ],
)
def test_compact_corner_quotes_http_replay_and_retry_binding(
    client: TestClient, app, tmp_path: Path, field: str, tamper: str
) -> None:
    prepared = _prepare_g8_accepted_page(
        client, app, tmp_path, disposition="redraw-art", region_type="title"
    )
    prepared = _complete_g9_terminal(client, prepared, translation_text="『缩地』")
    image, store = prepared["targetImage"], prepared["store"]
    initial = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    region_id = initial["routeManifest"][0]["regionId"]
    override = {
        "fontToken": _compact_display_font()["token"],
        "fontSize": 24,
        "minFontSize": 6,
        "padding": 0,
        "verticalQuoteLayout": "compact-corner-quotes-v1",
    }
    if field != "verticalQuoteLayout":
        override.update(bodyStrokeWidth=1, quoteScaleX=0.7, quoteScaleY=0.45)
    job, context = _run_typeset(
        client, app, prepared, options={"regionStyles": {region_id: override}}
    )
    candidate = context["candidates"][0]
    assert (
        candidate["styleManifest"][0]["style"]["verticalQuoteLayout"] == "compact-corner-quotes-v1"
    )
    assert candidate["layoutManifest"][0]["verticalQuoteLayout"] == "compact-corner-quotes-v1"
    assert client.get(candidate["artifactUrl"]).status_code == 200
    with store.session() as session:
        persisted_job = session.get(Job, job["id"])
        assert (
            persisted_job.options["regionStyles"][region_id]["verticalQuoteLayout"]
            == "compact-corner-quotes-v1"
        )
        row = session.get(PageTypesetCandidate, candidate["candidateId"])
        assert (
            typeset_service._retry_region_styles(row)[region_id]["verticalQuoteLayout"]
            == "compact-corner-quotes-v1"
        )
        for key in ("bodyStrokeWidth", "quoteScaleX", "quoteScaleY"):
            if key in override:
                assert persisted_job.options["regionStyles"][region_id][key] == override[key]
                assert candidate["styleManifest"][0]["style"][key] == override[key]
                assert candidate["layoutManifest"][0][key] == override[key]
                assert typeset_service._retry_region_styles(row)[region_id][key] == override[key]
    assert client.get(f"/api/images/{image['id']}/page-gates/typeset").status_code == 200
    changed = json.loads(json.dumps(candidate["styleManifest"]))
    if tamper == "remove":
        changed[0]["style"].pop(field)
    else:
        changed[0]["style"][field] = "future-v2" if field == "verticalQuoteLayout" else 2
    with sqlite3.connect(store.database_path) as db:
        db.execute("DROP TRIGGER page_typeset_candidates_no_update")
        db.execute(
            "UPDATE page_typeset_candidates SET style_manifest=? WHERE id=?",
            (json.dumps(changed), candidate["candidateId"]),
        )
    response = client.get(f"/api/images/{image['id']}/page-gates/typeset")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "g10-replay-invalid"


def test_compact_corner_quotes_invalid_binding_is_rejected_before_enqueue(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(
        client, app, tmp_path, disposition="redraw-art", region_type="title"
    )
    prepared = _complete_g9_terminal(client, prepared, translation_text="『缩地")
    image, project, store = prepared["targetImage"], prepared["targetProject"], prepared["store"]
    generation_id = str(prepared["generationId"])
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    region_id = context["routeManifest"][0]["regionId"]

    def counts():
        with store.session() as session:
            return [
                session.scalar(select(func.count()).select_from(model))
                for model in (Job, JobItem, Revision, PageLineageEvent, PageTypesetCandidate)
            ]

    before = counts()
    result = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {
                "regionStyles": {
                    region_id: {
                        "fontToken": _compact_display_font()["token"],
                        "verticalQuoteLayout": "compact-corner-quotes-v1",
                    }
                }
            },
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert result.status_code == 409
    assert result.json()["detail"]["reason"] == "g10-style-invalid"
    assert counts() == before


def test_compact_modifier_overrides_are_validated_before_any_enqueue_mutation(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g8_accepted_page(
        client, app, tmp_path, disposition="redraw-art", region_type="title"
    )
    prepared = _complete_g9_terminal(client, prepared, translation_text="『缩地』")
    image, project, store = prepared["targetImage"], prepared["targetProject"], prepared["store"]
    generation_id = str(prepared["generationId"])
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    region_id = context["routeManifest"][0]["regionId"]

    def snapshot():
        with store.session() as session:
            return [
                session.scalar(select(func.count()).select_from(model))
                for model in (Job, JobItem, Revision, PageLineageEvent, PageTypesetCandidate)
            ] + [
                session.get(ImageAsset, image["id"]).revision,
                session.get(PageGeneration, generation_id).next_sequence,
            ]

    before = snapshot()
    for field in ("bodyStrokeWidth", "quoteScaleX", "quoteScaleY"):
        bad_overrides = [{field: 1}] + [
            {"verticalQuoteLayout": "compact-corner-quotes-v1", field: value}
            for value in (None, True, "1", -1, 33)
        ]
        for override in bad_overrides:
            result = client.post(
                f"/api/projects/{project['id']}/typeset",
                json={
                    "imageIds": [image["id"]],
                    "options": {"regionStyles": {region_id: override}},
                    "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
                },
            )
            assert result.status_code == 409, result.text
            assert result.json()["detail"]["reason"] == "g10-style-invalid"
            assert snapshot() == before


def test_art_renderer_combines_base_rotation_and_style_fields_deterministically() -> None:
    display = typeset_service._display_fonts()[0]
    style = typeset_service._base_style("art-lettering") | {
        "fontToken": display["token"],
        "fontChecksum": display["fontChecksum"],
        "fontSource": "region-override",
        "rotation": 13.0,
        "scaleX": 1.25,
        "scaleY": 0.75,
        "shearX": 0.15,
        "shearY": -0.05,
        "opacity": 0.8,
        "visualCenterX": 0.4,
        "visualCenterY": 0.6,
        "align": "end",
        "lineSpacing": 0.4,
    }
    region = {
        "regionId": "art-region",
        "geometry": {"x": 20.0, "y": 20.0, "width": 180.0, "height": 180.0, "rotation": 22.0},
        "readingOrder": 0,
        "direction": "vertical",
        "paragraphGroupId": None,
    }

    def render(
        current_style: dict[str, object], current_region: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        canvas = Image.new("RGBA", (240, 240), "white")
        layout = typeset_service._render_art_region(
            canvas,
            text="中文艺术字",
            region=current_region,
            style=current_style,
            scale=1,
        )
        return hashlib.sha256(typeset_service._png_bytes(canvas)).hexdigest(), layout

    first, layout = render(style, region)
    repeated, repeated_layout = render(style, region)
    assert first == repeated
    assert layout == repeated_layout
    assert layout["rotation"] == pytest.approx(35)
    no_base, _ = render(style, region | {"geometry": region["geometry"] | {"rotation": 0.0}})
    assert no_base != first
    centered, _ = render(style | {"align": "center"}, region)
    assert centered != first
    tighter, _ = render(style | {"lineSpacing": 0.0}, region)
    assert tighter != first


def test_g10_normal_renderer_preserves_exact_region_and_paragraph_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_path = tmp_path / "clean.png"
    Image.new("RGBA", (100, 60), "white").save(clean_path)
    style = typeset_service._base_style("ordinary") | {
        "fontSize": 14,
        "minFontSize": 14,
        "autoFit": False,
    }
    regions = [
        {
            "regionId": "reading-first",
            "geometry": {"x": 46.0, "y": 10.0, "width": 30.0, "height": 30.0, "rotation": 0.0},
            "readingOrder": 0,
            "direction": "horizontal",
            "paragraphGroupId": "paragraph-a",
        },
        {
            "regionId": "reading-second",
            "geometry": {"x": 10.0, "y": 10.0, "width": 30.0, "height": 30.0, "rotation": 0.0},
            "readingOrder": 1,
            "direction": "horizontal",
            "paragraphGroupId": "paragraph-b",
        },
    ]
    routes = [{"regionId": entry["regionId"], "route": "ordinary"} for entry in regions]
    styles = [
        {"regionId": entry["regionId"], "route": "ordinary", "style": style} for entry in regions
    ]
    accepted = {
        "reading-first": SimpleNamespace(translation_text="第一"),
        "reading-second": SimpleNamespace(translation_text="第二"),
    }
    calls: list[list[dict[str, object]]] = []
    render_normal = typeset_service.typeset_image

    def capture_normal(image, render_regions, *, geometry_scale):
        calls.append([dict(entry) for entry in render_regions])
        return render_normal(image, render_regions, geometry_scale=geometry_scale)

    monkeypatch.setattr(typeset_service, "typeset_image", capture_normal)
    _payload, layouts, _overflow, _anomalies = typeset_service._render_candidate(
        clean_path=clean_path,
        region_manifest=regions,
        route_manifest=routes,
        style_manifest=styles,
        accepted_by_region=accepted,
        scale=1,
    )
    assert [len(call) for call in calls] == [1, 1]
    assert [call[0]["id"] for call in calls] == ["reading-first", "reading-second"]
    assert [call[0]["translationText"] for call in calls] == ["第一", "第二"]
    assert [call[0]["order"] for call in calls] == [0, 1]
    assert [call[0]["paragraphGroupId"] for call in calls] == [
        "paragraph-a",
        "paragraph-b",
    ]
    assert [layout["regionId"] for layout in layouts] == ["reading-first", "reading-second"]


def test_g10_art_renderer_rejects_oversized_layer_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = typeset_service._display_fonts()[0]
    style = typeset_service._base_style("art-lettering") | {
        "fontToken": display["token"],
        "fontChecksum": display["fontChecksum"],
        "fontSize": 512,
        "minFontSize": 512,
        "scaleX": 4.0,
        "scaleY": 4.0,
        "shearX": 1.0,
        "shearY": 1.0,
        "rotation": 180.0,
        "lineSpacing": 3.0,
        "autoFit": False,
    }
    region = {
        "regionId": "oversized-art",
        "geometry": {"x": 0.0, "y": 0.0, "width": 64.0, "height": 64.0, "rotation": 0.0},
        "readingOrder": 0,
        "direction": "vertical",
        "paragraphGroupId": None,
    }
    canvas = Image.new("RGBA", (256, 256), "white")
    allocations: list[tuple[int, int]] = []
    image_new = typeset_service.Image.new

    def guarded_new(mode, size, *args, **kwargs):
        dimensions = (int(size[0]), int(size[1]))
        allocations.append(dimensions)
        assert dimensions[0] <= typeset_service._ART_LAYER_MAX_SIDE
        assert dimensions[1] <= typeset_service._ART_LAYER_MAX_SIDE
        assert dimensions[0] * dimensions[1] <= typeset_service._ART_LAYER_MAX_PIXELS
        return image_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(typeset_service.Image, "new", guarded_new)
    with pytest.raises(PageLineageConflict) as captured:
        typeset_service._render_art_region(
            canvas,
            text="中" * 20_000,
            region=region,
            style=style,
            scale=4,
        )
    assert captured.value.reason == "g10-art-lettering-resource-limit"
    assert allocations == []


def test_g10_art_autofit_is_bounded_and_selects_largest_fitting_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = typeset_service._display_fonts()[0]
    style = typeset_service._base_style("art-lettering") | {
        "fontToken": display["token"],
        "fontChecksum": display["fontChecksum"],
        "fontSize": 512,
        "minFontSize": 6,
        "padding": 0,
        "autoFit": True,
    }
    region = {
        "regionId": "bounded-art",
        "geometry": {"x": 10.0, "y": 10.0, "width": 100.0, "height": 100.0, "rotation": 0.0},
        "readingOrder": 0,
        "direction": "vertical",
        "paragraphGroupId": None,
    }
    canvas = Image.new("RGBA", (480, 480), "white")
    draw_calls: list[int] = []
    transform_calls: list[int] = []
    draw_art = typeset_service._draw_art_text
    transform_art = typeset_service._art_transform

    def bounded_draw(*args, **kwargs):
        draw_calls.append(int(kwargs["font_size"]))
        return draw_art(*args, **kwargs)

    def bounded_transform(layer, current_style, *, rotation):
        transform_calls.append(layer.width * layer.height)
        return transform_art(layer, current_style, rotation=rotation)

    monkeypatch.setattr(typeset_service, "_draw_art_text", bounded_draw)
    monkeypatch.setattr(typeset_service, "_art_transform", bounded_transform)
    layout = typeset_service._render_art_region(
        canvas,
        text="中",
        region=region,
        style=style,
        scale=4,
    )
    assert len(draw_calls) == len(transform_calls)
    assert 1 <= len(draw_calls) <= typeset_service._G10_MAX_FIT_ATTEMPTS
    assert len(draw_calls) < 20
    selected = int(layout["fontSize"])
    assert 24 <= selected < 2048

    font_path = typeset_service._font_path(style, route="art-lettering")

    def transformed_size(font_size: int) -> tuple[int, int]:
        raw = draw_art(
            "中",
            font_path=font_path,
            font_size=font_size,
            direction="vertical",
            fill=str(style["fill"]),
            stroke_color=str(style["strokeColor"]),
            stroke_width=round(float(style["strokeWidth"]) * 4),
            letter_spacing=0,
            line_spacing=float(style["lineSpacing"]),
            align=str(style["align"]),
        )
        try:
            transformed = transform_art(raw, style, rotation=0)
        finally:
            raw.close()
        try:
            return transformed.size
        finally:
            transformed.close()

    selected_size = transformed_size(selected)
    next_size = transformed_size(selected + 1)
    assert selected_size[0] <= 400 and selected_size[1] <= 400
    assert next_size[0] > 400 or next_size[1] > 400


@pytest.mark.parametrize("compact", [False, True])
def test_g10_art_pixel_work_budget_is_shared_across_the_whole_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compact: bool
) -> None:
    clean_path = tmp_path / "clean.png"
    Image.new("RGBA", (300, 160), "white").save(clean_path)
    display = typeset_service._display_fonts()[0]
    style = typeset_service._base_style("art-lettering") | {
        "fontToken": display["token"],
        "fontChecksum": display["fontChecksum"],
        "fontSize": 64,
        "minFontSize": 64,
        "autoFit": False,
    }
    extra_draw = {}
    text = "中字"
    if compact:
        display = _compact_display_font()
        style.update(
            fontToken=display["token"],
            fontChecksum=display["fontChecksum"],
            verticalQuoteLayout="compact-corner-quotes-v1",
            bodyStrokeWidth=2,
            quoteScaleX=0.65,
            quoteScaleY=0.45,
        )
        extra_draw = {
            "vertical_quote_layout": "compact-corner-quotes-v1",
            "body_stroke_width": 2,
            "quote_scale_x": 0.65,
            "quote_scale_y": 0.45,
        }
        text = "『缩地』"
    font_path = typeset_service._font_path(style, route="art-lettering")
    measurement = typeset_service._measure_art_text(
        text,
        font_path=font_path,
        font_size=64,
        direction="vertical",
        fill=str(style["fill"]),
        stroke_color=str(style["strokeColor"]),
        stroke_width=int(style["strokeWidth"]),
        letter_spacing=float(style["letterSpacing"]),
        line_spacing=float(style["lineSpacing"]),
        align=str(style["align"]),
        **extra_draw,
    )
    plan = typeset_service._art_transform_plan(
        int(measurement["width"]),
        int(measurement["height"]),
        style,
        rotation=0,
    )
    one_region_work = int(measurement["width"]) * int(measurement["height"])
    one_region_work += measurement.get("extraPixelWork", 0)
    one_region_work += sum(
        dimensions[0] * dimensions[1] for dimensions in plan if dimensions is not None
    )
    monkeypatch.setattr(
        typeset_service,
        "_ART_TOTAL_PIXEL_WORK_BUDGET",
        one_region_work + one_region_work // 2,
    )
    regions = [
        {
            "regionId": f"art-{index}",
            "geometry": {
                "x": float(10 + index * 140),
                "y": 10.0,
                "width": 120.0,
                "height": 120.0,
                "rotation": 0.0,
            },
            "readingOrder": index,
            "direction": "vertical",
            "paragraphGroupId": None,
        }
        for index in range(2)
    ]
    with pytest.raises(PageLineageConflict) as captured:
        typeset_service._render_candidate(
            clean_path=clean_path,
            region_manifest=regions,
            route_manifest=[
                {"regionId": region["regionId"], "route": "art-lettering"} for region in regions
            ],
            style_manifest=[
                {"regionId": region["regionId"], "route": "art-lettering", "style": style}
                for region in regions
            ],
            accepted_by_region={
                region["regionId"]: SimpleNamespace(translation_text=text) for region in regions
            },
            scale=1,
        )
    assert captured.value.reason == "g10-art-lettering-resource-limit"


def test_g10_normal_autofit_is_bounded_and_selects_largest_fitting_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_path = tmp_path / "scaled-clean.png"
    Image.new("RGBA", (480, 480), "white").save(clean_path)
    style = typeset_service._base_style("ordinary") | {
        "fontSize": 512,
        "minFontSize": 6,
        "padding": 0,
        "autoFit": True,
    }
    region = {
        "regionId": "bounded-normal",
        "geometry": {"x": 10.0, "y": 10.0, "width": 100.0, "height": 100.0, "rotation": 0.0},
        "readingOrder": 0,
        "direction": "horizontal",
        "paragraphGroupId": None,
    }
    route = {"regionId": region["regionId"], "route": "ordinary"}
    load_calls: list[int] = []
    load_font = imaging_typesetting._load_font

    def bounded_load(font_path, size, font_family=None):
        load_calls.append(int(size))
        return load_font(font_path, size, font_family)

    monkeypatch.setattr(imaging_typesetting, "_load_font", bounded_load)
    _payload, layouts, _overflow, _anomalies = typeset_service._render_candidate(
        clean_path=clean_path,
        region_manifest=[region],
        route_manifest=[route],
        style_manifest=[{"regionId": region["regionId"], "route": "ordinary", "style": style}],
        accepted_by_region={region["regionId"]: SimpleNamespace(translation_text="中文测试")},
        scale=4,
    )
    assert 1 <= len(load_calls) <= typeset_service._G10_MAX_FIT_ATTEMPTS
    assert len(load_calls) < 20
    selected = int(layouts[0]["fontSize"])
    assert 24 <= selected < 2048
    scaled_style = typeset_service._scaled_normal_style(style, 4, route="ordinary")

    def exact_overflow(font_size: int) -> bool:
        _font, _lines, overflow, _height = imaging_typesetting._horizontal_fit(
            "中文测试",
            width=400,
            height=400,
            font_path=scaled_style["fontPath"],
            font_family=None,
            min_size=font_size,
            max_size=font_size,
            line_spacing=float(scaled_style["lineSpacing"]),
            stroke_width=int(scaled_style["strokeWidth"]),
            letter_spacing=float(scaled_style["letterSpacing"]),
            auto_wrap=True,
            max_attempts=1,
        )
        return overflow

    assert exact_overflow(selected) is False
    assert exact_overflow(selected + 1) is True


def test_g10_normal_long_text_fails_before_legacy_quadratic_wrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_path = tmp_path / "clean.png"
    Image.new("RGBA", (64, 64), "white").save(clean_path)
    style = typeset_service._base_style("ordinary")
    region = {
        "regionId": "long-normal",
        "geometry": {"x": 0.0, "y": 0.0, "width": 8.0, "height": 8.0, "rotation": 0.0},
        "readingOrder": 0,
        "direction": "horizontal",
        "paragraphGroupId": None,
    }
    calls = 0
    render_normal = typeset_service.typeset_image

    def counted_render(*args, **kwargs):
        nonlocal calls
        calls += 1
        return render_normal(*args, **kwargs)

    monkeypatch.setattr(typeset_service, "typeset_image", counted_render)
    with pytest.raises(PageLineageConflict) as captured:
        typeset_service._render_candidate(
            clean_path=clean_path,
            region_manifest=[region],
            route_manifest=[{"regionId": region["regionId"], "route": "ordinary"}],
            style_manifest=[{"regionId": region["regionId"], "route": "ordinary", "style": style}],
            accepted_by_region={
                region["regionId"]: SimpleNamespace(translation_text="中" * 20_000)
            },
            scale=1,
        )
    assert captured.value.reason == "g10-typesetting-resource-limit"
    assert calls == 0


def test_g10_keep_ignore_ruby_and_false_positive_reviewable_after_g8_na(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g6_accepted_page(
        client,
        app,
        tmp_path,
        disposition="keep-art",
        extra_dispositions=("ignore", "false-positive"),
        include_ruby=True,
    )
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    mask = client.get(f"/api/images/{image['id']}/page-gates/mask").json()
    assert mask["eligibleRegionIds"] == []
    g7 = client.patch(
        f"/api/images/{image['id']}/page-gates/mask",
        json={
            "decision": "not-applicable",
            "reason": "no-eligible-regions",
            "selectedArtifactId": None,
            "observedMaskChecksum": None,
            "coverageChecks": [],
            "collateralChecks": [],
            "expectedRevision": mask["imageRevision"],
            "lineage": _mutation_lineage(generation_id, mask["nextSequence"]),
        },
    )
    assert g7.status_code == 200, g7.text
    clean = client.get(f"/api/images/{image['id']}/page-gates/clean-plate").json()
    g8 = client.patch(
        f"/api/images/{image['id']}/page-gates/clean-plate",
        json={
            "decision": "not-applicable",
            "reason": "no-clean-plate-required",
            "candidateId": None,
            "observedCandidateChecksum": None,
            "observedWidth": None,
            "observedHeight": None,
            "checks": [],
            "expectedRevision": clean["imageRevision"],
            "lineage": _mutation_lineage(generation_id, clean["nextSequence"]),
        },
    )
    assert g8.status_code == 200, g8.text
    translation = client.get(f"/api/images/{image['id']}/page-gates/translation").json()
    g9 = client.patch(
        f"/api/images/{image['id']}/page-gates/translation",
        json={
            "decision": "not-applicable",
            "observedTranslationStateChecksum": translation["translationStateChecksum"],
            "expectedRevision": translation["imageRevision"],
            "lineage": _mutation_lineage(generation_id, translation["nextSequence"]),
        },
    )
    assert g9.status_code == 200, g9.text
    monkeypatch.setattr(typeset_service, "installed_typeset_fonts", lambda: ())
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset")
    assert context.status_code == 200, context.text
    assert context.json()["cleanPlateCandidateId"] is None
    assert context.json()["availableFonts"] == []
    assert [entry["route"] for entry in context.json()["routeManifest"]] == ["keep", "ignore"]
    _job, produced = _run_typeset(client, app, prepared)
    candidate = produced["candidates"][0]
    assert (
        len(candidate["regionManifest"])
        == len(candidate["routeManifest"])
        == len(candidate["styleManifest"])
    )
    assert all(entry["style"] is None for entry in candidate["styleManifest"])
    assert candidate["layoutManifest"] == []
    assert candidate["cleanPlateCandidateId"] is None
    assert candidate["cleanPlateChecksum"] == prepared["qualityChecksum"]
    quality = client.get(f"/api/images/{image['id']}/generated/preprocessed")
    artifact = client.get(candidate["artifactUrl"])
    assert quality.status_code == artifact.status_code == 200
    with (
        Image.open(BytesIO(quality.content)) as quality_image,
        Image.open(BytesIO(artifact.content)) as artifact_image,
    ):
        quality_rgba = quality_image.convert("RGBA")
        artifact_rgba = artifact_image.convert("RGBA")
        assert artifact_rgba.size == quality_rgba.size
        assert artifact_rgba.tobytes() == quality_rgba.tobytes()
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(produced, candidate, generation_id),
    )
    assert accepted.status_code == 200, accepted.text
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(PageTypesetReview)) == 1


def test_g10_mixed_translate_and_redraw_art_freezes_both_routes(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(
        client,
        app,
        tmp_path,
        disposition="translate",
        region_type="dialogue",
        extra_dispositions=("redraw-art",),
    )
    image = prepared["targetImage"]
    assert isinstance(image, dict)
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert [entry["route"] for entry in context["routeManifest"]] == [
        "bubble",
        "art-lettering",
    ]
    assert all(entry["translationCandidateId"] for entry in context["routeManifest"])
    _job, produced = _run_typeset(client, app, prepared)
    candidate = produced["candidates"][0]
    assert [entry["route"] for entry in candidate["styleManifest"]] == [
        "bubble",
        "art-lettering",
    ]
    assert [entry["route"] for entry in candidate["layoutManifest"]] == [
        "bubble",
        "art-lettering",
    ]


@pytest.mark.parametrize("render_route", ["ordinary", "bubble", "art-lettering"])
@pytest.mark.parametrize("protected_route", ["keep", "ignore"])
def test_g10_render_routes_restore_exact_rgba_under_rotated_protected_regions(
    tmp_path: Path,
    render_route: str,
    protected_route: str,
) -> None:
    clean_path = tmp_path / f"{render_route}-{protected_route}.png"
    parent = Image.new("RGBA", (240, 200))
    parent.putdata(
        [
            (
                (x * 3 + y) % 256,
                (x + y * 5) % 256,
                (x * 7 + y * 11) % 256,
                (x * 13 + y * 17) % 256,
            )
            for y in range(parent.height)
            for x in range(parent.width)
        ]
    )
    parent.save(clean_path)
    style = typeset_service._base_style(render_route) | {
        "fontSize": 32,
        "minFontSize": 32,
        "fill": "#FF1200",
        "strokeColor": "#0011FF",
        "strokeWidth": 2,
        "autoFit": False,
    }
    regions = [
        {
            "regionId": "rendered",
            "geometry": {
                "x": 10.0,
                "y": 10.0,
                "width": 100.0,
                "height": 80.0,
                "rotation": 8.0,
            },
            "readingOrder": 0,
            "direction": "horizontal",
            "paragraphGroupId": "rendered-paragraph",
        },
        {
            "regionId": "protected-center",
            "geometry": {
                "x": 25.0,
                "y": 20.0,
                "width": 60.0,
                "height": 50.0,
                "rotation": 31.0,
            },
            "readingOrder": 1,
            "direction": "horizontal",
            "paragraphGroupId": None,
        },
        {
            "regionId": "protected-boundary",
            "geometry": {
                "x": 0.0,
                "y": 0.0,
                "width": 24.0,
                "height": 24.0,
                "rotation": -37.0,
            },
            "readingOrder": 2,
            "direction": "vertical",
            "paragraphGroupId": None,
        },
    ]
    opposite_protected = "ignore" if protected_route == "keep" else "keep"
    routes = [
        {"regionId": "rendered", "route": render_route},
        {"regionId": "protected-center", "route": protected_route},
        {"regionId": "protected-boundary", "route": opposite_protected},
    ]
    payload, _layouts, _overflow, anomalies = typeset_service._render_candidate(
        clean_path=clean_path,
        region_manifest=regions,
        route_manifest=routes,
        style_manifest=[
            {"regionId": "rendered", "route": render_route, "style": style},
            {"regionId": "protected-center", "route": protected_route, "style": None},
            {"regionId": "protected-boundary", "route": opposite_protected, "style": None},
        ],
        accepted_by_region={"rendered": SimpleNamespace(translation_text="重叠像素测试")},
        scale=2,
    )
    assert anomalies == [typeset_service._PROTECTED_PIXELS_RESTORED_ANOMALY]
    with Image.open(BytesIO(payload)) as opened:
        candidate = opened.convert("RGBA")
    protected_mask = typeset_service._protected_route_mask(
        candidate.size,
        region_manifest=regions,
        route_manifest=routes,
        scale=2,
    )
    difference = typeset_service._rgba_difference_mask(candidate, parent)
    protected_difference = typeset_service.ImageChops.multiply(difference, protected_mask)
    assert protected_mask.getbbox() is not None
    assert protected_difference.getbbox() is None


def test_g10_overlapping_keep_geometry_records_anomaly_and_blocks_acceptance(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(
        client,
        app,
        tmp_path,
        extra_dispositions=("ignore", "ignore", "ignore", "ignore", "keep-art"),
    )
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _job, context = _run_typeset(client, app, prepared)
    candidate = context["candidates"][0]
    assert candidate["anomalies"] == [typeset_service._PROTECTED_PIXELS_RESTORED_ANOMALY]

    clean_response = client.get(
        f"/api/images/{image['id']}/page-gates/clean-plate/candidates/"
        f"{candidate['cleanPlateCandidateId']}"
    )
    artifact_response = client.get(candidate["artifactUrl"])
    assert clean_response.status_code == artifact_response.status_code == 200
    with (
        Image.open(BytesIO(clean_response.content)) as clean_opened,
        Image.open(BytesIO(artifact_response.content)) as candidate_opened,
    ):
        clean_parent = clean_opened.convert("RGBA")
        rendered = candidate_opened.convert("RGBA")
    protected_mask = typeset_service._protected_route_mask(
        rendered.size,
        region_manifest=candidate["regionManifest"],
        route_manifest=candidate["routeManifest"],
        scale=float(candidate["renderScale"]),
    )
    protected_difference = typeset_service.ImageChops.multiply(
        typeset_service._rgba_difference_mask(rendered, clean_parent),
        protected_mask,
    )
    assert protected_difference.getbbox() is None

    blocked = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(
            context,
            candidate,
            generation_id,
            decision="accept",
            reason="typeset-reviewed",
        ),
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "g10-checks-invalid"


def test_g10_nonoverlapping_keep_geometry_remains_acceptable(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(
        client,
        app,
        tmp_path,
        extra_dispositions=("keep-art",),
    )
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _job, context = _run_typeset(client, app, prepared)
    candidate = context["candidates"][0]
    assert candidate["anomalies"] == []
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(context, candidate, generation_id),
    )
    assert accepted.status_code == 200, accepted.text


def test_g10_reject_retry_preserves_history_and_accepts_new_candidate(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    _first_job, first_context = _run_typeset(client, app, prepared)
    first = first_context["candidates"][0]
    failed_checks = [dict(entry) for entry in _TYPESET_CHECKS]
    failed_checks[4]["passed"] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{first['candidateId']}",
        json=_review_body(
            first_context,
            first,
            generation_id,
            decision="reject",
            reason="typography-source-matched",
            checks=failed_checks,
        ),
    )
    assert rejected.status_code == 200, rejected.text
    retry_context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert retry_context["state"] == "pending"
    assert set(retry_context["retryRegionStyles"]) == {first["routeManifest"][0]["regionId"]}
    retry_style = next(iter(retry_context["retryRegionStyles"].values()))
    assert "fontToken" in retry_style
    assert "fontChecksum" not in retry_style
    assert "fontSource" not in retry_style

    _second_job, second_context = _run_typeset(
        client,
        app,
        prepared,
        options={"regionStyles": retry_context["retryRegionStyles"]},
    )
    assert len(second_context["candidates"]) == 2
    second = second_context["candidates"][-1]
    assert second["candidateId"] != first["candidateId"]
    assert second["candidateChecksum"] == first["candidateChecksum"]
    produced_events = [
        event
        for event in client.get(f"/api/page-generations/{generation_id}/events").json()
        if event["operation"] == "typeset-candidate-produced"
    ]
    assert len(produced_events) == 2
    assert produced_events[0]["outputChecksum"] != produced_events[1]["outputChecksum"]
    accepted = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{second['candidateId']}",
        json=_review_body(second_context, second, generation_id),
    )
    assert accepted.status_code == 200, accepted.text
    terminal = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert terminal["state"] == "accepted"
    assert len(terminal["candidates"]) == 2
    assert [row["state"] for row in terminal["reviews"]] == ["rejected", "accepted"]

    with store.session() as session:
        secret_job = Job(
            project_id=project["id"],
            kind="preprocess",
            status="completed",
            progress=1.0,
            total=0,
            completed=0,
            options={
                "apiToken": "synthetic-secret-token",
                "nested": {"password": "synthetic-password"},
            },
        )
        session.add(secret_job)
        session.flush()
        secret_job_id = secret_job.id
    store.write_snapshot()
    bundle_root = tmp_path / "portable"
    ensure_project_bundle(store, bundle_root)
    assert len(list(bundle_root.glob("generated/lineage-masks/*/*.png"))) == 1
    assert len(list(bundle_root.glob("generated/lineage-clean-plates/*/*.png"))) == 1
    assert len(list(bundle_root.glob("generated/lineage-typesets/*/*.png"))) == 2
    with sqlite3.connect(bundle_root / "project/project.sqlite3") as database:
        typeset_options = [
            json.loads(row[0])
            for row in database.execute(
                "SELECT options FROM jobs WHERE kind = 'typeset' ORDER BY created_at"
            ).fetchall()
        ]
        scrubbed = json.loads(
            database.execute("SELECT options FROM jobs WHERE id = ?", (secret_job_id,)).fetchone()[
                0
            ]
        )
    retry_region_id = next(iter(retry_context["retryRegionStyles"]))
    assert (
        typeset_options[-1]["regionStyles"][retry_region_id]["fontToken"]
        == retry_style["fontToken"]
    )
    assert "apiToken" not in scrubbed
    assert "password" not in scrubbed["nested"]

    fresh_app = create_app(
        Settings(data_dir=tmp_path / "fresh-portable-catalog"),
        start_worker=False,
    )
    with TestClient(fresh_app) as fresh:
        opened = fresh.post(
            "/api/projects/open",
            json={"manifestPath": str(bundle_root / "project/project.json")},
        )
        assert opened.status_code == 200, opened.text
        reopened = fresh.get(f"/api/images/{image['id']}/page-gates/typeset")
        assert reopened.status_code == 200, reopened.text
        reopened_context = reopened.json()
        assert reopened_context["state"] == "accepted"
        assert [row["candidateId"] for row in reopened_context["candidates"]] == [
            first["candidateId"],
            second["candidateId"],
        ]
        for row in reopened_context["candidates"]:
            artifact = fresh.get(row["artifactUrl"])
            assert artifact.status_code == 200
            assert hashlib.sha256(artifact.content).hexdigest() == row["candidateChecksum"]

    with store.session() as session:
        protected_event = session.scalar(
            select(PageLineageEvent).where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.gate == "G7_mask",
                PageLineageEvent.revision_id.is_not(None),
            )
        )
        assert protected_event is not None and protected_event.revision_id is not None
        protected_revision = session.get(Revision, protected_event.revision_id)
        assert protected_revision is not None
        protected_revision.before = {
            **dict(protected_revision.before or {}),
            "apiToken": "synthetic-protected-secret",
        }
    blocked_root = tmp_path / "blocked-portable"
    with pytest.raises(ProjectError, match="non-portable secret material"):
        ensure_project_bundle(store, blocked_root)
    assert not (blocked_root / "project/project.sqlite3").exists()
    assert not (blocked_root / "project/project.json").exists()


def test_portable_bundle_excludes_concurrent_g10_publication_from_entire_snapshot(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    _first_job, first_context = _run_typeset(client, app, prepared)
    first = first_context["candidates"][0]
    failed_checks = [dict(entry) for entry in _TYPESET_CHECKS]
    failed_checks[4]["passed"] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{first['candidateId']}",
        json=_review_body(
            first_context,
            first,
            generation_id,
            decision="reject",
            reason="typography-source-matched",
            checks=failed_checks,
        ),
    )
    assert rejected.status_code == 200, rejected.text
    retry_context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    queued = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {"regionStyles": retry_context["retryRegionStyles"]},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert queued.status_code == 202, queued.text

    snapshot_taken = threading.Event()
    writer_attempted = threading.Event()
    writer_acquired = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []
    portable_assets = exporting_service._portable_assets

    def observed_assets(current_store):
        assets = portable_assets(current_store)
        snapshot_taken.set()
        assert writer_attempted.wait(2)
        # The exporter still owns the ProjectStore RLock after discovering all
        # DB-referenced rasters, so a new G10 publisher cannot enter here.
        assert not writer_acquired.is_set()
        return assets

    monkeypatch.setattr(exporting_service, "_portable_assets", observed_assets)

    def publish_retry() -> None:
        try:
            assert snapshot_taken.wait(2)
            writer_attempted.set()
            with store.lock:
                writer_acquired.set()
            claimed = app.state.queue._claim_next()
            assert claimed == (store, queued.json()["id"])
            asyncio.run(app.state.queue._execute(*claimed))
        except BaseException as error:  # pragma: no cover - surfaced below
            writer_errors.append(error)
        finally:
            writer_finished.set()

    writer = threading.Thread(target=publish_retry, daemon=True)
    writer.start()
    bundle_root = tmp_path / "concurrent-portable"
    ensure_project_bundle(store, bundle_root)
    writer.join(timeout=10)
    assert writer_finished.is_set()
    assert writer_acquired.is_set()
    assert writer_errors == []

    with sqlite3.connect(bundle_root / "project/project.sqlite3") as database:
        bundled_candidates = database.execute(
            "SELECT id FROM page_typeset_candidates ORDER BY sequence"
        ).fetchall()
    assert bundled_candidates == [(first["candidateId"],)]
    assert len(list(bundle_root.glob("generated/lineage-typesets/*/*.png"))) == 1
    live_context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert len(live_context["candidates"]) == 2


def test_g10_publication_is_recoverable_but_not_reviewable_before_completion(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    with store.session() as session:
        job = session.get(Job, queued.json()["id"])
        generation = session.get(PageGeneration, generation_id)
        assert job is not None and generation is not None
        item = job.items[0]
        job.status = "running"
        item.status = "running"
        item.started_at = datetime.now(UTC)
        binding = job_mutation_binding(job, item, {str(image["id"]): generation})
        assert binding is not None
    append_event = typeset_service._append_event

    def crash_after_file(*args, **kwargs):
        if kwargs.get("operation") == "typeset-candidate-produced":
            raise RuntimeError("synthetic-publication-crash")
        return append_event(*args, **kwargs)

    monkeypatch.setattr(typeset_service, "_append_event", crash_after_file)
    with pytest.raises(RuntimeError, match="synthetic-publication-crash"):
        publish_typeset_candidate(store, job=job, item=item, binding=binding)
    orphaned = list((store.root / "generated" / "lineage-typesets" / generation_id).glob("*.png"))
    assert len(orphaned) == 1
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(PageTypesetCandidate)) == 0
    monkeypatch.setattr(typeset_service, "_append_event", append_event)
    published = publish_typeset_candidate(store, job=job, item=item, binding=binding)
    render_candidate = typeset_service._render_candidate
    font_catalog = typeset_service._font_catalog_by_token
    validate_replay = typeset_service.validate_typeset_replay
    recovery_render_calls = 0
    recovery_font_calls = 0
    recovery_replay_calls = 0

    def unavailable_renderer(**_kwargs):
        nonlocal recovery_render_calls
        recovery_render_calls += 1
        raise RuntimeError("renderer-must-not-run-during-published-recovery")

    def unavailable_font_catalog():
        nonlocal recovery_font_calls
        recovery_font_calls += 1
        raise RuntimeError("font-catalog-must-not-run-during-published-recovery")

    def unavailable_replay(*_args, **_kwargs):
        nonlocal recovery_replay_calls
        recovery_replay_calls += 1
        raise RuntimeError("full-replay-must-not-run-during-published-recovery")

    monkeypatch.setattr(typeset_service, "_render_candidate", unavailable_renderer)
    monkeypatch.setattr(typeset_service, "_font_catalog_by_token", unavailable_font_catalog)
    monkeypatch.setattr(typeset_service, "validate_typeset_replay", unavailable_replay)
    recovered = publish_typeset_candidate(store, job=job, item=item, binding=binding)
    assert recovered["recovered"] is True
    assert recovered["candidateId"] == published["candidateId"]
    assert recovery_render_calls == 0
    assert recovery_font_calls == 0
    assert recovery_replay_calls == 0
    monkeypatch.setattr(typeset_service, "_render_candidate", render_candidate)
    monkeypatch.setattr(typeset_service, "_font_catalog_by_token", font_catalog)
    monkeypatch.setattr(typeset_service, "validate_typeset_replay", validate_replay)
    context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    candidate = context["candidates"][0]
    assert candidate["completed"] is False
    blocked = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(context, candidate, generation_id),
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "g10-review-not-ready"
    with store.session() as session:
        stored_candidate = session.scalar(select(PageTypesetCandidate))
        assert stored_candidate is not None
        candidate_revision_id = stored_candidate.revision_id
        assert session.scalar(select(func.count()).select_from(PageTypesetCandidate)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(Revision)
                .where(
                    Revision.entity_type == "typeset-candidate",
                    Revision.entity_id == candidate["candidateId"],
                )
            )
            == 1
        )
    assert store.recover_jobs() == 1
    monkeypatch.setattr(typeset_service, "_render_candidate", unavailable_renderer)
    monkeypatch.setattr(typeset_service, "_font_catalog_by_token", unavailable_font_catalog)
    monkeypatch.setattr(typeset_service, "validate_typeset_replay", unavailable_replay)
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    assert recovery_render_calls == 0
    assert recovery_font_calls == 0
    assert recovery_replay_calls == 0
    recovered_job = app.state.queue.get_job(store, queued.json()["id"])
    assert recovered_job.status == "completed"
    assert recovered_job.items[0].status == "completed"
    monkeypatch.setattr(typeset_service, "_render_candidate", render_candidate)
    monkeypatch.setattr(typeset_service, "_font_catalog_by_token", font_catalog)
    monkeypatch.setattr(typeset_service, "validate_typeset_replay", validate_replay)
    completed_context = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    assert completed_context["candidates"][0]["candidateId"] == candidate["candidateId"]
    assert completed_context["candidates"][0]["completed"] is True
    with store.session() as session:
        stored_candidate = session.scalar(select(PageTypesetCandidate))
        assert stored_candidate is not None
        assert stored_candidate.revision_id == candidate_revision_id
        assert session.scalar(select(func.count()).select_from(PageTypesetCandidate)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(Revision)
                .where(
                    Revision.entity_type == "typeset-candidate",
                    Revision.entity_id == candidate["candidateId"],
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageLineageEvent)
                .where(
                    PageLineageEvent.generation_id == generation_id,
                    PageLineageEvent.operation == "typeset-candidate-produced",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageLineageEvent)
                .where(
                    PageLineageEvent.generation_id == generation_id,
                    PageLineageEvent.operation == "typeset-job-completed",
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "tamper_kind",
    ["missing-event", "candidate-png", "candidate-revision", "job-options"],
)
def test_g10_published_recovery_rejects_incomplete_or_tampered_evidence_without_rendering(
    client: TestClient,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    queued = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    with store.session() as session:
        job = session.get(Job, queued.json()["id"])
        generation = session.get(PageGeneration, generation_id)
        assert job is not None and generation is not None
        item = job.items[0]
        job.status = "running"
        item.status = "running"
        item.started_at = datetime.now(UTC)
        binding = job_mutation_binding(job, item, {str(image["id"]): generation})
        assert binding is not None
    published = publish_typeset_candidate(store, job=job, item=item, binding=binding)
    with store.session() as session:
        row = session.get(PageTypesetCandidate, published["candidateId"])
        assert row is not None
        revision_id = row.revision_id
        candidate_path = store.root / row.relative_path
    if tamper_kind == "candidate-png":
        candidate_path.write_bytes(b"tampered-g10-candidate")
    else:
        with sqlite3.connect(store.database_path) as database:
            if tamper_kind == "missing-event":
                database.execute("DROP TRIGGER page_lineage_events_no_delete")
                database.execute(
                    "DELETE FROM page_lineage_events "
                    "WHERE generation_id = ? AND job_item_id = ? "
                    "AND operation = 'typeset-candidate-produced'",
                    (generation_id, item.id),
                )
            elif tamper_kind == "candidate-revision":
                database.execute("DROP TRIGGER revisions_g10_no_update")
                database.execute(
                    'UPDATE revisions SET "after" = ? WHERE id = ?',
                    (json.dumps({"tampered": True}), revision_id),
                )
            else:
                database.execute(
                    "UPDATE jobs SET options = ? WHERE id = ?",
                    (json.dumps({"regionStyles": {}, "concurrency": 8}), job.id),
                )

    render_calls = 0
    replay_calls = 0

    def unavailable_renderer(**_kwargs):
        nonlocal render_calls
        render_calls += 1
        raise RuntimeError("tampered recovery must not reach renderer")

    def unavailable_replay(*_args, **_kwargs):
        nonlocal replay_calls
        replay_calls += 1
        raise RuntimeError("tampered recovery must not reach full replay")

    monkeypatch.setattr(typeset_service, "_render_candidate", unavailable_renderer)
    monkeypatch.setattr(typeset_service, "validate_typeset_replay", unavailable_replay)
    with pytest.raises(PageLineageConflict):
        publish_typeset_candidate(store, job=job, item=item, binding=binding)
    assert render_calls == 0
    assert replay_calls == 0
    with store.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageTypesetCandidate)
                .where(PageTypesetCandidate.job_item_id == item.id)
            )
            == 1
        )


def test_g10_missing_enqueue_job_item_is_rejected_by_replay(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    lineage = _current_lineage_context(client, str(image["id"]), generation_id)
    with store.session() as session:
        job = Job(
            project_id=project["id"],
            kind="typeset",
            status="queued",
            total=1,
            options={"regionStyles": {}, "concurrency": 1},
            lineage_context=lineage,
        )
        session.add(job)
        session.flush()
        session.add(
            JobItem(
                job_id=job.id,
                image_id=image["id"],
                region_id=None,
                position=0,
                status="queued",
            )
        )
    response = client.get(f"/api/images/{image['id']}/page-gates/typeset")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "g10-replay-invalid"


def test_g10_render_cas_drift_publishes_no_candidate(
    client: TestClient, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    project = prepared["targetProject"]
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(project, dict) and isinstance(image, dict)
    original_render = typeset_service._render_candidate
    drifted = False

    def render_with_drift(**kwargs):
        nonlocal drifted
        result = original_render(**kwargs)
        if not drifted:
            with store.session() as session:
                image_row = session.get(ImageAsset, image["id"])
                assert image_row is not None
                image_row.revision += 1
            drifted = True
        return result

    monkeypatch.setattr(typeset_service, "_render_candidate", render_with_drift)
    queued = client.post(
        f"/api/projects/{project['id']}/typeset",
        json={
            "imageIds": [image["id"]],
            "options": {},
            "lineage": _current_lineage_context(client, str(image["id"]), generation_id),
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = app.state.queue._claim_next()
    assert claimed == (store, queued.json()["id"])
    asyncio.run(app.state.queue._execute(*claimed))
    job = app.state.queue.get_job(store, queued.json()["id"])
    assert job.status == "failed"
    assert "stale failure was discarded" in (job.items[0].error or "")
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(PageTypesetCandidate)) == 0
    generated = store.root / "generated" / "lineage-typesets" / generation_id
    assert not generated.exists() or list(generated.iterdir()) == []


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "job-status",
        "event-evidence",
        "candidate-checksum",
        "candidate-revision",
        "candidate-png",
        "review-checks",
    ],
)
def test_g10_exact_replay_rejects_persisted_tamper(
    client: TestClient, app, tmp_path: Path, tamper_kind: str
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    _job, context = _run_typeset(client, app, prepared)
    candidate = context["candidates"][0]
    if tamper_kind == "review-checks":
        accepted = client.patch(
            f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
            json=_review_body(context, candidate, generation_id),
        )
        assert accepted.status_code == 200, accepted.text

    with store.session() as session:
        row = session.get(PageTypesetCandidate, candidate["candidateId"])
        assert row is not None
        item_id = row.job_item_id
        revision_id = row.revision_id
        event_id = session.scalar(
            select(PageLineageEvent.id).where(
                PageLineageEvent.generation_id == generation_id,
                PageLineageEvent.operation == "typeset-candidate-produced",
            )
        )
        review_id = session.scalar(
            select(PageTypesetReview.id).where(PageTypesetReview.candidate_id == row.id)
        )
        candidate_path = store.root / row.relative_path
    with sqlite3.connect(store.database_path) as database:
        if tamper_kind == "job-status":
            database.execute("UPDATE job_items SET status = 'failed' WHERE id = ?", (item_id,))
        elif tamper_kind == "event-evidence":
            assert event_id is not None
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                database.execute(
                    "UPDATE page_lineage_events SET evidence = ? WHERE id = ?",
                    (json.dumps({"tampered": True}), event_id),
                )
            database.rollback()
            database.execute("DROP TRIGGER page_lineage_events_no_update")
            database.execute(
                "UPDATE page_lineage_events SET evidence = ? WHERE id = ?",
                (json.dumps({"tampered": True}), event_id),
            )
        elif tamper_kind == "candidate-checksum":
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                database.execute(
                    "UPDATE page_typeset_candidates SET candidate_checksum = ? WHERE id = ?",
                    ("0" * 64, candidate["candidateId"]),
                )
            database.rollback()
            database.execute("DROP TRIGGER page_typeset_candidates_no_update")
            database.execute(
                "UPDATE page_typeset_candidates SET candidate_checksum = ? WHERE id = ?",
                ("0" * 64, candidate["candidateId"]),
            )
        elif tamper_kind == "candidate-revision":
            database.execute("DROP TRIGGER revisions_g10_no_update")
            database.execute(
                'UPDATE revisions SET "after" = ? WHERE id = ?',
                (json.dumps({"tampered": True}), revision_id),
            )
        elif tamper_kind == "review-checks":
            assert review_id is not None
            database.execute("DROP TRIGGER page_typeset_reviews_no_update")
            database.execute(
                "UPDATE page_typeset_reviews SET checks = ? WHERE id = ?",
                (json.dumps(list(reversed(_TYPESET_CHECKS))), review_id),
            )
    if tamper_kind == "candidate-png":
        candidate_path.write_bytes(candidate_path.read_bytes() + b"tamper")
    response = client.get(f"/api/images/{image['id']}/page-gates/typeset")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] in {
        "g9-replay-invalid",
        "g10-replay-invalid",
        "g10-candidate-file-invalid",
    }


def test_g10_overflow_observation_and_rejection_must_match_server_facts(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    image = prepared["targetImage"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    initial = client.get(f"/api/images/{image['id']}/page-gates/typeset").json()
    region_id = initial["routeManifest"][0]["regionId"]
    _job, context = _run_typeset(
        client,
        app,
        prepared,
        options={
            "regionStyles": {
                region_id: {
                    "fontSize": 512,
                    "minFontSize": 512,
                    "autoFit": False,
                }
            }
        },
    )
    candidate = context["candidates"][0]
    assert candidate["overflowRegionIds"] == [region_id]
    stale = _review_body(context, candidate, generation_id)
    stale["observedRouteChecksum"] = "0" * 64
    stale_response = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=stale,
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["reason"] == "g10-observation-mismatch"
    contradictory = [dict(entry) for entry in _TYPESET_CHECKS]
    contradictory[4]["passed"] = False
    contradiction = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(
            context,
            candidate,
            generation_id,
            decision="reject",
            reason="typography-source-matched",
            checks=contradictory,
        ),
    )
    assert contradiction.status_code == 409
    assert contradiction.json()["detail"]["reason"] == "g10-checks-invalid"
    overflow_checks = [dict(entry) for entry in _TYPESET_CHECKS]
    overflow_checks[-1]["passed"] = False
    rejected = client.patch(
        f"/api/images/{image['id']}/page-gates/typeset/candidates/{candidate['candidateId']}",
        json=_review_body(
            context,
            candidate,
            generation_id,
            decision="reject",
            reason="overflow-free",
            checks=overflow_checks,
        ),
    )
    assert rejected.status_code == 200, rejected.text


def test_g10_empty_route_manifest_fails_closed_for_g3_g4_recheck(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    assert isinstance(image, dict)
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        region = session.scalar(select(TextRegion).where(TextRegion.image_id == image["id"]))
        assert image_row is not None and region is not None
        region.content_disposition = "false-positive"
        session.flush()
        with pytest.raises(PageLineageConflict) as captured:
            typeset_service._route_and_region_manifests(session, image_row, {})
        assert captured.value.reason == "g10-route-manifest-empty"
        session.rollback()


def test_g10_translate_sound_effect_tamper_never_routes_to_ordinary(
    client: TestClient, app, tmp_path: Path
) -> None:
    prepared = _prepare_g9_terminal(client, app, tmp_path)
    image = prepared["targetImage"]
    store = prepared["store"]
    generation_id = str(prepared["generationId"])
    assert isinstance(image, dict)
    with store.session() as session:
        image_row = session.get(ImageAsset, image["id"])
        region = session.scalar(select(TextRegion).where(TextRegion.image_id == image["id"]))
        candidates = list(
            session.scalars(
                select(RegionTranslationCandidate).where(
                    RegionTranslationCandidate.generation_id == generation_id
                )
            ).all()
        )
        assert image_row is not None and region is not None and len(candidates) == 1
        region.region_type = "sound_effect"
        session.flush()
        with pytest.raises(PageLineageConflict) as captured:
            typeset_service._route_and_region_manifests(
                session, image_row, {region.id: candidates[0]}
            )
        assert captured.value.reason == "g10-unknown-translate-type"
        session.rollback()


def test_art_lettering_vertical_measures_map_horizontal_dashes() -> None:
    font = imaging_typesetting.default_cjk_font()
    if font is None:
        pytest.skip("No usable system CJK font")
    options = {
        "font_path": font,
        "font_size": 40,
        "direction": "vertical",
        "fill": "#000000",
        "stroke_color": "#ffffff",
        "stroke_width": 0,
        "letter_spacing": 0,
        "line_spacing": 0.15,
        "align": "center",
    }
    em_dash = typeset_service._measure_art_text(
        "\u7684\u4e00\u51fb\u2014\u2014\uff01\uff01", **options
    )
    assert "\u2014" not in em_dash["rendered"]
    assert "\ufe31" in em_dash["rendered"]
    horizontal_bar = typeset_service._measure_art_text(
        "\u8cab\u304f\u4e00\u6483\u2015\u2015!!", **options
    )
    assert "\u2015" not in horizontal_bar["rendered"]
    assert "\ufe31" in horizontal_bar["rendered"]
    horizontal = typeset_service._measure_art_text(
        "\u7684\u4e00\u51fb\u2014\u2014\uff01\uff01", **{**options, "direction": "horizontal"}
    )
    assert "\u2014" in horizontal["rendered"]
    assert "\ufe31" not in horizontal["rendered"]
