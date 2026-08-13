# Provider system

Providers expose capability metadata and health independently from project workflows. Registries select
providers by stable identifiers; project settings contain no credentials. Optional providers must report
an unavailable health state rather than prevent the API or editor from starting, and no provider downloads
models during application startup.

## Image preprocessing provider

```python
preprocess(image, **options) -> PreprocessedImage
preprocess_batch(images, **options)
health_check()
get_capabilities()
```

`PreprocessedImage` carries the processed image, original and processed sizes, scale factors, and
original/processed coordinate mapping helpers. The queue persists preprocessing below
`generated/preprocessed/`; downstream regions always remain in immutable source-image coordinates.

- `opencv-pillow` is the dependency-light default. It supports profiles `off`, `ocr-friendly`,
  `balanced`, and `visual-quality`, plus independent upscale (2×/3×/4×), denoise, sharpen, contrast,
  edge, and binarize/threshold switches.
- `realesrgan-ncnn` wraps a separately installed local
  [Real-ESRGAN NCNN Vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) executable and can chain
  local post-processing. A missing executable produces an unavailable health result; model files are
  owned and validated at execution time by the external CLI, whose failures are surfaced by the job.
  Nothing is downloaded implicitly.

Real-data evaluation found edge enhancement unsafe as a default on detailed manga line art, so it is
available but opt-in. A low/empty OCR candidate from a preprocessed crop is retried against the original
crop and the stronger candidate is retained with attempt/input provenance.

## Text detection provider

```python
detect_text_regions(image, direction, language)
health_check()
get_capabilities()
```

- `tesseract` keeps the zero-model baseline and parses TSV geometry.
- `ppocr-v3` uses OpenCV DNN with the external
  [OpenCV Zoo PP-OCRv3 model](https://huggingface.co/opencv/text_detection_ppocr). It returns bounded
  polygons and never changes canonical source coordinates.

Detection and recognition are separate selections. A completed detection job with zero candidates is a
valid result; OCR does not silently replace it with a different detector. Unknown provider IDs fail the
job visibly instead of falling back while recording false provenance.

## OCR provider

```python
recognize_region(image, region)
recognize_image(image)
health_check()
get_capabilities()
```

The Tesseract adapter invokes the installed CLI directly, supports `jpn` and `jpn_vert`, and tries
horizontal/vertical modes when direction is automatic. MangaOCR and PaddleOCR recognition adapters are
not included yet; their larger dependencies cannot stop the baseline editor from starting.

## Translation provider

```python
translate_text(text, context)
translate_batch(items)
health_check()
get_capabilities()
```

Manual preserves the current reviewed translation without automatic mutation. Mock is deterministic.
Dictionary is a local non-LLM exact/glossary translator. OpenAI-compatible sends the current text plus
bounded preceding/following regions by reading order on the same page to a user-configured endpoint.
Provider errors are normalized without echoing request headers, remote response bodies, or credentials.

The compatibility adapter uses the widely implemented Chat Completions contract: bearer authentication,
`model` plus developer/user `messages`, and text from the first response choice. See the official
[Create chat completion API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

## Inpainting provider

```python
create_mask(image, regions, **options)
inpaint(image, mask, **options)
health_check()
get_capabilities()
```

- `opencv` is always available with Telea, Navier-Stokes, and solid fill. Feathered masks are
  soft-composited, so pixels outside the mask remain exact.
- `lama-onnx` implements the
  [OpenCV Zoo LaMa](https://github.com/opencv/opencv_zoo/tree/main/models/inpainting_lama) 512×512 ONNX
  contract through optional ONNX Runtime. It uses a context crop, lazy thread-safe session, inward
  feathering, alpha preservation, and exact zero-mask compositing.

Masks can use detector polygons/current region geometry or text-aware local segmentation, followed by
padding, dilation, and feathering. Moving/resizing/rotating a detector-created region discards its stale
polygon so the visible edited box becomes the manual boundary. The UI overlays the actual persisted
mask and stores bounded add/erase brush strokes for one selected region; the composed mask is the input
to the inpainting provider.

The `safe` repair policy is the default. It accepts confirmed regions, manual regions with source text,
and detector-created source text above the configured confidence threshold. `recognized` accepts every
non-empty source region; `all` is an explicit high-risk override. Skipped/repaired counts and the actual
provider are recorded in each job result.

Provider completion is not visual approval. Preprocessed, inpainted, and typeset artifacts have
separate persisted accept/reject records. Generated-image export requires accepted checksums for the
current artifacts (and the inpaint mask), while JSON-only export remains independent.

## Optional model setup

The repository contains no weights. Run the explicit checksum-verifying installer, targeting the same
data directory used by the application:

```bash
npm run setup:models -- ppocr
npm run setup:models -- lama
uv sync --project backend --extra ai --group dev  # required by LaMa
```

Or install both plus the runtime with `npm run setup:ai`. Configuration can override the standard model
locations with `MANGA_LOCALIZER_PPOCR_DETECTION_MODEL` and
`MANGA_LOCALIZER_LAMA_INPAINTING_MODEL`. Real-ESRGAN uses
`MANGA_LOCALIZER_REALESRGAN_NCNN_COMMAND`.

## Adding a provider

Implement the relevant protocol, declare stable capabilities, add exact registry selection, test success,
unavailable, invalid-ID, and concurrency paths, and document install/privacy/licensing implications. A
provider must preserve its input, avoid automatic downloads, emit canonical coordinates, and record the
implementation actually used.
