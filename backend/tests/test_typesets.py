from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
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
            translation_text=f"第{index + 1}句合规译文",
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


def test_g10_art_pixel_work_budget_is_shared_across_the_whole_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    font_path = typeset_service._font_path(style, route="art-lettering")
    measurement = typeset_service._measure_art_text(
        "中字",
        font_path=font_path,
        font_size=64,
        direction="vertical",
        fill=str(style["fill"]),
        stroke_color=str(style["strokeColor"]),
        stroke_width=int(style["strokeWidth"]),
        letter_spacing=float(style["letterSpacing"]),
        line_spacing=float(style["lineSpacing"]),
        align=str(style["align"]),
    )
    plan = typeset_service._art_transform_plan(
        int(measurement["width"]),
        int(measurement["height"]),
        style,
        rotation=0,
    )
    one_region_work = int(measurement["width"]) * int(measurement["height"])
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
                region["regionId"]: SimpleNamespace(translation_text="中字") for region in regions
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
