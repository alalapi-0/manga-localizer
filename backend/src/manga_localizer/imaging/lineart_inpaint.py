from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from manga_localizer.imaging.inpainting import inpaint
from manga_localizer.imaging.manga_ai_postprocess import (
    manga_overview_lineart_cleanup,
    manga_tone_cleanup,
)

CANDIDATE_PRIMARY = "primary"
CANDIDATE_AI_MANGA_CLEAN = "ai-manga-clean"
CANDIDATE_AI_OVERVIEW_LINEART = "ai-overview-lineart"
CANDIDATE_OPENCV_NS = "opencv-ns"
CANDIDATE_OPENCV_TELEA = "opencv-telea"
CANDIDATE_LINEART = "lineart-guided"
CANDIDATE_LAMA_FULL_CONTEXT = "lama-full-context"
CANDIDATE_LAMA_COMPONENTS = "lama-components"
CANDIDATE_LAMA_OVERVIEW_REFINE = "lama-overview-refine"
CANDIDATE_IDS = frozenset(
    {
        CANDIDATE_PRIMARY,
        CANDIDATE_AI_MANGA_CLEAN,
        CANDIDATE_AI_OVERVIEW_LINEART,
        CANDIDATE_OPENCV_NS,
        CANDIDATE_OPENCV_TELEA,
        CANDIDATE_LINEART,
        CANDIDATE_LAMA_FULL_CONTEXT,
        CANDIDATE_LAMA_COMPONENTS,
        CANDIDATE_LAMA_OVERVIEW_REFINE,
    }
)
CANDIDATE_LABELS = {
    CANDIDATE_PRIMARY: "当前 Provider 结果",
    CANDIDATE_AI_MANGA_CLEAN: "AI 漫画重绘(清晰黑白)",
    CANDIDATE_AI_OVERVIEW_LINEART: "AI 全景重绘 + 线稿清晰化",
    CANDIDATE_OPENCV_NS: "OpenCV Navier-Stokes",
    CANDIDATE_OPENCV_TELEA: "OpenCV Telea",
    CANDIDATE_LINEART: "线稿引导(结构+纹理)",
    CANDIDATE_LAMA_FULL_CONTEXT: "LaMa 全局上下文(连续边界)",
    CANDIDATE_LAMA_COMPONENTS: "LaMa 逐空缺重绘(局部上下文)",
    CANDIDATE_LAMA_OVERVIEW_REFINE: "LaMa 全景引导重绘(大空缺)",
}
ANOMALY_MASK_OUTSIDE = "mask-outside-changed"
ANOMALY_CHROMA = "chroma-introduced"
ANOMALY_SMEAR = "possible-smear"

type ImageInput = Path | Image.Image | np.ndarray


def is_effectively_grayscale(image: Image.Image) -> bool:
    if image.mode in {"L", "LA"}:
        return True
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    return bool(np.max(np.max(rgb, axis=2) - np.min(rgb, axis=2)) <= 1)


def preserve_grayscale(result: Image.Image, source: Image.Image) -> Image.Image:
    """Collapse chroma introduced by RGB models on grayscale manga pages."""
    if not is_effectively_grayscale(source):
        return result
    gray = result.convert("L")
    if result.mode == "RGBA" or source.mode == "RGBA":
        converted = gray.convert("RGBA")
        alpha = result.getchannel("A") if "A" in result.getbands() else source.getchannel("A")
        converted.putalpha(alpha)
        return converted
    if result.mode == "L":
        return gray
    return gray.convert("RGB")


def _as_image(image: ImageInput) -> Image.Image:
    if isinstance(image, Path):
        with Image.open(image) as opened:
            opened.load()
            return opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
    if isinstance(image, Image.Image):
        return image.copy()
    if isinstance(image, np.ndarray):
        if image.dtype != np.uint8:
            raise ValueError("Image arrays must use uint8 pixels")
        if image.ndim == 2:
            return Image.fromarray(image, mode="L")
        if image.ndim == 3 and image.shape[2] == 3:
            return Image.fromarray(image, mode="RGB")
        if image.ndim == 3 and image.shape[2] == 4:
            return Image.fromarray(image, mode="RGBA")
        raise ValueError("Image arrays must have shape HxW, HxWx3, or HxWx4")
    raise TypeError("image must be a pathlib.Path, PIL image, or numpy ndarray")


def _as_mask(mask: ImageInput) -> np.ndarray:
    if isinstance(mask, Path):
        with Image.open(mask) as opened:
            opened.load()
            image = opened.convert("L")
    elif isinstance(mask, Image.Image):
        image = mask.convert("L")
    elif isinstance(mask, np.ndarray):
        if mask.dtype not in {np.dtype(np.uint8), np.dtype(np.bool_)}:
            raise ValueError("Mask arrays must use uint8 or bool pixels")
        pixels = mask[..., 0] if mask.ndim == 3 and mask.shape[2] == 1 else mask
        if pixels.ndim != 2:
            raise ValueError("Mask arrays must have shape HxW or HxWx1")
        if pixels.dtype == np.bool_:
            pixels = pixels.astype(np.uint8) * 255
        return np.array(pixels, copy=True)
    else:
        raise TypeError("mask must be a pathlib.Path, PIL image, or numpy ndarray")
    return np.asarray(image, dtype=np.uint8).copy()


def composite_mask_outside(
    source: Image.Image,
    generated: Image.Image,
    mask: np.ndarray,
) -> Image.Image:
    mode = "RGBA" if "A" in source.getbands() or "A" in generated.getbands() else "RGB"
    before = np.asarray(source.convert(mode), dtype=np.uint8)
    after = np.asarray(generated.convert(mode), dtype=np.uint8).copy()
    if before.shape != after.shape or mask.shape != before.shape[:2]:
        raise ValueError("Inpainting candidate dimensions are incompatible with the source mask")
    after[mask == 0] = before[mask == 0]
    result = Image.fromarray(after, mode=mode)
    if source.mode == "L" and result.mode == "RGB" and is_effectively_grayscale(source):
        return result.convert("L")
    return result


def lineart_guided_inpaint(
    image: ImageInput,
    mask: ImageInput,
    *,
    texture: Image.Image | None = None,
    radius: float = 3.0,
    render_scale: int = 1,
) -> Image.Image:
    """Blend Navier-Stokes structure with a smoother texture fill for manga line art."""
    source = _as_image(image)
    mask_array = _as_mask(mask)
    if source.size != (mask_array.shape[1], mask_array.shape[0]):
        raise ValueError("Image and inpainting mask dimensions differ")
    if not np.any(mask_array):
        return source.copy()

    structure = inpaint(
        source,
        mask_array,
        radius=radius,
        method="ns",
        render_scale=render_scale,
    )
    texture_image = (
        texture
        if texture is not None
        else inpaint(
            source,
            mask_array,
            radius=radius,
            method="telea",
            render_scale=render_scale,
        )
    )
    source_rgb = np.asarray(source.convert("RGB"), dtype=np.uint8)
    structure_rgb = np.asarray(structure.convert("RGB"), dtype=np.uint8)
    texture_rgb = np.asarray(texture_image.convert("RGB"), dtype=np.uint8)
    if structure_rgb.shape != source_rgb.shape or texture_rgb.shape != source_rgb.shape:
        raise ValueError("Line-art guided inpainting produced an incompatible image")

    structure_gray = cv2.cvtColor(structure_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gradient_x = cv2.Sobel(structure_gray, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(structure_gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.hypot(gradient_x, gradient_y)
    peak = float(np.max(magnitude))
    magnitude_norm = magnitude / peak if peak > 0 else magnitude

    source_gray = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2GRAY)
    unmasked = source_gray.copy()
    unmasked[mask_array > 0] = 0
    edge_guide = cv2.Canny(unmasked, 40, 120)
    edge_guide = cv2.dilate(edge_guide, np.ones((5, 5), np.uint8), iterations=2)
    guide = edge_guide.astype(np.float32) / 255.0
    structure_weight = np.clip(0.15 + 0.85 * np.maximum(magnitude_norm, guide), 0.0, 1.0)
    structure_weight = cv2.GaussianBlur(structure_weight, (5, 5), 0)
    weights = np.zeros_like(structure_weight)
    inside = mask_array > 0
    weights[inside] = structure_weight[inside]
    blended = texture_rgb.astype(np.float32) * (1.0 - weights[..., None])
    blended += structure_rgb.astype(np.float32) * weights[..., None]
    result_rgb = source_rgb.copy()
    result_rgb[inside] = np.rint(blended[inside]).astype(np.uint8)
    result_rgb[mask_array == 0] = source_rgb[mask_array == 0]
    result = Image.fromarray(result_rgb, mode="RGB")
    result = preserve_grayscale(result, source)
    result = composite_mask_outside(source, result, mask_array)
    if source.mode == "RGBA":
        result = result.convert("RGBA")
        result.putalpha(source.getchannel("A"))
    return result


def candidate_metrics(
    source: Image.Image,
    result: Image.Image,
    mask: np.ndarray,
) -> dict[str, Any]:
    source_rgb = np.asarray(source.convert("RGB"), dtype=np.int16)
    result_rgb = np.asarray(result.convert("RGB"), dtype=np.int16)
    if source_rgb.shape != result_rgb.shape or mask.shape != source_rgb.shape[:2]:
        raise ValueError("Candidate metrics require matching source, result, and mask shapes")
    outside = mask == 0
    inside = mask > 0
    changed_outside = int(
        np.count_nonzero(np.any(source_rgb[outside] != result_rgb[outside], axis=-1))
    )
    if np.any(inside):
        mean_abs_inside = float(np.mean(np.abs(source_rgb[inside] - result_rgb[inside])))
        chroma_inside = int(
            np.max(np.max(result_rgb[inside], axis=-1) - np.min(result_rgb[inside], axis=-1))
        )
    else:
        mean_abs_inside = 0.0
        chroma_inside = 0
    anomalies: list[str] = []
    if changed_outside:
        anomalies.append(ANOMALY_MASK_OUTSIDE)
    if is_effectively_grayscale(source) and chroma_inside > 2:
        anomalies.append(ANOMALY_CHROMA)
    if _looks_smeared(source_rgb, result_rgb, mask):
        anomalies.append(ANOMALY_SMEAR)
    return {
        "changedPixelsOutsideMask": changed_outside,
        "meanAbsDeltaInsideMask": round(mean_abs_inside, 3),
        "chromaInsideMask": chroma_inside,
        "anomalies": anomalies,
    }


def _looks_smeared(
    source_rgb: np.ndarray,
    result_rgb: np.ndarray,
    mask: np.ndarray,
) -> bool:
    inside = mask > 0
    dilated = cv2.dilate((mask > 0).astype(np.uint8), np.ones((9, 9), np.uint8))
    ring = (dilated > 0) & (mask == 0)
    if int(np.count_nonzero(inside)) < 16 or int(np.count_nonzero(ring)) < 16:
        return False
    source_gray = cv2.cvtColor(source_rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
    result_gray = cv2.cvtColor(result_rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
    ring_energy = float(cv2.Laplacian(source_gray, cv2.CV_32F)[ring].var())
    inside_energy = float(cv2.Laplacian(result_gray, cv2.CV_32F)[inside].var())
    return ring_energy >= 120.0 and inside_energy <= 18.0


def build_inpaint_candidates(
    source: ImageInput,
    mask: ImageInput,
    primary: Image.Image,
    *,
    radius: float = 3.0,
    render_scale: int = 1,
    full_context: Image.Image | None = None,
    component_context: Image.Image | None = None,
    overview_base: Image.Image | None = None,
    overview_refine: Image.Image | None = None,
) -> list[dict[str, Any]]:
    source_image = _as_image(source)
    mask_array = _as_mask(mask)
    if not np.any(mask_array):
        return []
    ns = inpaint(
        source_image,
        mask_array,
        radius=radius,
        method="ns",
        render_scale=render_scale,
    )
    telea = inpaint(
        source_image,
        mask_array,
        radius=radius,
        method="telea",
        render_scale=render_scale,
    )
    guided = lineart_guided_inpaint(
        source_image,
        mask_array,
        texture=primary,
        radius=radius,
        render_scale=render_scale,
    )
    candidate_images: list[tuple[str, Image.Image]] = [
        (CANDIDATE_PRIMARY, primary),
    ]
    if component_context is not None:
        candidate_images.append((CANDIDATE_LAMA_COMPONENTS, component_context))
    if overview_refine is not None:
        candidate_images.append((CANDIDATE_LAMA_OVERVIEW_REFINE, overview_refine))
    if overview_base is not None:
        overview_cleaned_pixels = manga_overview_lineart_cleanup(
            np.asarray(source_image.convert("RGB"), dtype=np.uint8),
            np.asarray(overview_base.convert("RGB"), dtype=np.uint8),
            mask_array,
            render_scale=render_scale,
        )
        if overview_cleaned_pixels is not None:
            overview_cleaned = Image.fromarray(overview_cleaned_pixels, mode="RGB")
            overview_cleaned = preserve_grayscale(overview_cleaned, source_image)
            if source_image.mode == "RGBA":
                overview_cleaned = overview_cleaned.convert("RGBA")
                overview_cleaned.putalpha(source_image.getchannel("A"))
            candidate_images.append((CANDIDATE_AI_OVERVIEW_LINEART, overview_cleaned))
    candidate_images.extend(
        [
            (CANDIDATE_OPENCV_NS, ns),
            (CANDIDATE_OPENCV_TELEA, telea),
            (CANDIDATE_LINEART, guided),
        ]
    )
    ai_base = full_context if full_context is not None else primary
    cleaned_pixels = manga_tone_cleanup(
        np.asarray(source_image.convert("RGB"), dtype=np.uint8),
        np.asarray(ai_base.convert("RGB"), dtype=np.uint8),
        mask_array,
        radius=radius,
    )
    if cleaned_pixels is not None:
        cleaned = Image.fromarray(cleaned_pixels, mode="RGB")
        cleaned = preserve_grayscale(cleaned, source_image)
        if source_image.mode == "RGBA":
            cleaned = cleaned.convert("RGBA")
            cleaned.putalpha(source_image.getchannel("A"))
        candidate_images.insert(1, (CANDIDATE_AI_MANGA_CLEAN, cleaned))
    if full_context is not None:
        candidate_images.append((CANDIDATE_LAMA_FULL_CONTEXT, full_context))
    built: list[dict[str, Any]] = []
    for candidate_id, image in candidate_images:
        restored = image
        if candidate_id != CANDIDATE_PRIMARY:
            restored = preserve_grayscale(image, source_image)
        restored = composite_mask_outside(source_image, restored, mask_array)
        metrics = candidate_metrics(source_image, restored, mask_array)
        built.append(
            {
                "id": candidate_id,
                "label": CANDIDATE_LABELS[candidate_id],
                "image": restored,
                **metrics,
            }
        )
    return built


def choose_default_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    used_only_lama: bool,
) -> str:
    ids = {str(item.get("id")) for item in candidates}
    if used_only_lama and CANDIDATE_AI_MANGA_CLEAN in ids:
        return CANDIDATE_AI_MANGA_CLEAN
    if CANDIDATE_PRIMARY in ids:
        return CANDIDATE_PRIMARY
    if not ids:
        raise ValueError("No inpainting candidates were generated")
    return str(next(iter(ids)))


def public_candidate_records(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in candidates:
        candidate_id = str(item.get("id", ""))
        if candidate_id not in CANDIDATE_IDS:
            continue
        anomalies = item.get("anomalies", [])
        record = {
            "id": candidate_id,
            "label": CANDIDATE_LABELS.get(candidate_id, candidate_id),
            "anomalies": [
                str(flag)
                for flag in anomalies
                if flag in {ANOMALY_MASK_OUTSIDE, ANOMALY_CHROMA, ANOMALY_SMEAR}
            ]
            if isinstance(anomalies, list)
            else [],
        }
        origin_kind = item.get("originKind")
        if origin_kind in {
            "direct-ai",
            "ai-derived",
            "classical",
            "deterministic-postprocess",
            "mixed",
        }:
            record["originKind"] = origin_kind
        records.append(record)
    return records
