from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from manga_localizer.imaging.layered_structure import (
    LayeredStructureError,
    canonicalize_layered_structure_guide,
    render_layered_structure,
)
from manga_localizer.services.clean_plates import _digest, _render_candidate
from manga_localizer.services.inpaint_candidates import _write_once
from manga_localizer.services.projects import ProjectError


def _guide(*, polygon: list[list[int]] | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "domains": [
            {
                "id": "base",
                "mode": "reference",
                "referenceId": "reference-a",
                "polygon": polygon or [[1, 1], [4, 1], [4, 4], [1, 4]],
            }
        ],
        "strokes": [],
        "featherRadius": 0,
    }


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_layered_structure_guide_is_strict_canonical_and_hash_sensitive() -> None:
    first = canonicalize_layered_structure_guide(_guide(), source_size=(6, 6))
    reordered = _guide()
    reordered["domains"] = [
        {
            "polygon": [[1, 1], [4, 1], [4, 4], [1, 4]],
            "referenceId": "reference-a",
            "mode": "reference",
            "id": "base",
        }
    ]
    assert canonicalize_layered_structure_guide(reordered, source_size=(6, 6)) == first
    changed = canonicalize_layered_structure_guide(
        {**_guide(), "featherRadius": 1}, source_size=(6, 6)
    )
    assert _digest(first) != _digest(changed)

    for invalid in (
        {**_guide(), "path": "candidate.png"},
        {**_guide(), "version": 2},
        {
            **_guide(),
            "domains": [
                *_guide()["domains"],
                {
                    "id": "base",
                    "mode": "solid",
                    "rgb": [0, 0, 0],
                    "polygon": [[1, 1], [4, 1], [4, 4]],
                },
            ],
        },
        _guide(polygon=[[float("nan"), 1], [4, 1], [4, 4]]),
    ):
        with pytest.raises(LayeredStructureError):
            canonicalize_layered_structure_guide(invalid, source_size=(6, 6))


def test_layered_structure_requires_an_exact_partition_and_bounded_strokes() -> None:
    source = np.zeros((6, 6, 3), dtype=np.uint8)
    reference = np.full_like(source, 173)
    support = np.zeros((6, 6), dtype=np.uint8)
    support[1:5, 1:5] = 255
    guide = canonicalize_layered_structure_guide(_guide(), source_size=(6, 6))
    rendered = render_layered_structure(source, support, guide, {"reference-a": reference}, scale=1)
    assert np.all(rendered[support > 0] == 173)
    assert np.array_equal(rendered[support == 0], source[support == 0])

    gap = canonicalize_layered_structure_guide(
        _guide(polygon=[[1, 1], [3, 1], [3, 4], [1, 4]]), source_size=(6, 6)
    )
    with pytest.raises(LayeredStructureError, match="partition"):
        render_layered_structure(source, support, gap, {"reference-a": reference}, scale=1)

    stroke_outside = canonicalize_layered_structure_guide(
        {
            **_guide(),
            "strokes": [
                {
                    "id": "edge",
                    "mode": "solid",
                    "rgb": [0, 0, 0],
                    "points": [[1, 1], [1, 4]],
                    "width": 3,
                }
            ],
        },
        source_size=(6, 6),
    )
    with pytest.raises(LayeredStructureError, match="strokes"):
        render_layered_structure(
            source, support, stroke_outside, {"reference-a": reference}, scale=1
        )


def test_whole_page_layered_route_preserves_outside_rgb_and_alpha_without_inpainter() -> None:
    rgba = np.zeros((6, 6, 4), dtype=np.uint8)
    rgba[..., :3] = [12, 34, 56]
    rgba[..., 3] = np.arange(36, dtype=np.uint8).reshape(6, 6)
    mask = np.zeros((6, 6), dtype=np.uint8)
    mask[1:5, 1:5] = 255
    reference = np.full((6, 6, 3), [200, 150, 100], dtype=np.uint8)
    guide = canonicalize_layered_structure_guide(_guide(), source_size=(6, 6))
    manifest = [
        {
            "regionId": "region-a",
            "backgroundCategory": "illustration/character",
            "route": "layered-structure",
            "originKind": "mixed",
            "provider": "opencv",
            "modelVersion": "layered-structure-guide-v1",
            "parameterHash": hashlib.sha256(b"parameters").hexdigest(),
        }
    ]

    def forbidden_inpainter(_provider: str):
        raise AssertionError("layered structure route must not invoke an AI inpainter")

    payload, width, height, outside_count, anomalies = _render_candidate(
        quality_bytes=_png(Image.fromarray(rgba, mode="RGBA")),
        mask_bytes=_png(Image.fromarray(mask, mode="L")),
        rows=[SimpleNamespace(id="region-a", x=1, y=1, width=3, height=3)],
        manifest=manifest,
        normalized={
            "ownerMaskStrategy": None,
            "layeredStructureGuide": guide,
        },
        scale=1,
        inpainter=forbidden_inpainter,
        layered_reference_bytes={"reference-a": _png(Image.fromarray(reference, mode="RGB"))},
    )
    assert (width, height, outside_count, anomalies) == (6, 6, 0, [])
    with Image.open(io.BytesIO(payload)) as opened:
        actual = np.asarray(opened.convert("RGBA"), dtype=np.uint8)
    assert np.array_equal(actual[..., 3], rgba[..., 3])
    assert np.array_equal(actual[mask == 0, :3], rgba[mask == 0, :3])
    assert np.all(actual[mask > 0, :3] == reference[mask > 0])


@pytest.mark.parametrize("scale", [2, 3, 4])
def test_source_cell_rasterization_covers_edges_adjacent_domains_strokes_and_feather(
    scale: int,
) -> None:
    source_shape = (3, 4)
    quality_shape = (source_shape[0] * scale, source_shape[1] * scale)
    source = np.full((*quality_shape, 3), 10, dtype=np.uint8)
    reference = np.full_like(source, 90)
    support = np.full(quality_shape, 255, dtype=np.uint8)
    guide = canonicalize_layered_structure_guide(
        {
            "version": 1,
            "domains": [
                {
                    "id": "left",
                    "mode": "reference",
                    "referenceId": "reference-a",
                    "polygon": [[0, 0], [1, 0], [1, 2], [0, 2]],
                },
                {
                    "id": "right",
                    "mode": "solid",
                    "rgb": [180, 181, 182],
                    "polygon": [[2, 0], [3, 0], [3, 2], [2, 2]],
                },
            ],
            "strokes": [
                {
                    "id": "left-stroke",
                    "mode": "solid",
                    "rgb": [1, 2, 3],
                    "points": [[0, 0], [0, 2]],
                    "width": 1,
                }
            ],
            "featherRadius": 0,
        },
        source_size=(4, 3),
    )
    first = render_layered_structure(
        source, support, guide, {"reference-a": reference}, scale=scale
    )
    second = render_layered_structure(
        source, support, guide, {"reference-a": reference}, scale=scale
    )
    assert np.array_equal(first, second)
    assert np.all(first[:, -scale:] == [180, 181, 182])
    assert np.all(first[:, :scale] == [1, 2, 3])
    assert np.all(first[:, scale : 2 * scale] == 90)

    inset_support = np.zeros(quality_shape, dtype=np.uint8)
    inset_support[scale : 2 * scale, scale : 3 * scale] = 255
    feathered_guide = canonicalize_layered_structure_guide(
        {
            "version": 1,
            "domains": [
                {
                    "id": "all",
                    "mode": "solid",
                    "rgb": [210, 210, 210],
                    "polygon": [[0, 0], [3, 0], [3, 2], [0, 2]],
                }
            ],
            "strokes": [],
            "featherRadius": 1,
        },
        source_size=(4, 3),
    )
    feathered = render_layered_structure(
        source,
        inset_support,
        feathered_guide,
        {},
        scale=scale,
    )
    assert np.array_equal(feathered[inset_support == 0], source[inset_support == 0])
    assert np.any(feathered[inset_support > 0] != source[inset_support > 0])
    assert np.array_equal(
        feathered,
        render_layered_structure(source, inset_support, feathered_guide, {}, scale=scale),
    )


def test_write_once_is_idempotent_and_concurrent_publication_never_clobbers(
    tmp_path: Path,
) -> None:
    target = tmp_path / "snapshot" / "artifact.png"
    _write_once(target, b"first-complete-payload")
    _write_once(target, b"first-complete-payload")
    with pytest.raises(ProjectError, match="collision"):
        _write_once(target, b"different-payload")
    assert target.read_bytes() == b"first-complete-payload"

    raced = tmp_path / "snapshot" / "raced.png"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda payload: _publish_result(raced, payload),
                (b"a" * 4096, b"b" * 4096),
            )
        )
    assert sorted(results) == ["collision", "published"]
    assert raced.read_bytes() in {b"a" * 4096, b"b" * 4096}


def _publish_result(path: Path, payload: bytes) -> str:
    try:
        _write_once(path, payload)
    except ProjectError:
        return "collision"
    return "published"
