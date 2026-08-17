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
  edge, and binarize/threshold switches. Its upscaler is classic Lanczos interpolation and is reported
  with `aiUpscale: false`. Import records a per-page profile suggestion from local image stats; jobs
  still use the project or explicit job options, never that hint automatically.
- `realesrgan-onnx` is the local AI upscaler. It runs the BSD-3-Clause
  `RealESRGAN_x4plus_anime_6B` ONNX graph through the optional ONNX Runtime extra. The graph is native
  4×; requested 2×/3× results downscale that AI output with Lanczos. Tiling, alpha preservation, grayscale
  preservation, and local post-processing switches match the other preprocessors. A missing model or
  runtime produces an unavailable health result. Nothing is downloaded at startup.
- `realesrgan-ncnn` wraps a separately installed local
  [Real-ESRGAN NCNN Vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) executable and can chain
  local post-processing. It searches `PATH` and the application data directory, and passes a sibling
  `models/` folder to the CLI so temporary working directories cannot hide the weights. A missing
  executable produces an unavailable health result. Nothing is downloaded implicitly.

Real-data evaluation found edge enhancement unsafe as a default on detailed manga line art, so it is
available but opt-in. A low/empty OCR candidate from a preprocessed crop is retried against the original
crop and the stronger candidate is retained with attempt/input provenance.

## Text detection provider

```python
detect_text_regions(image, direction, language)
health_check()
get_capabilities()
```

- `tesseract` keeps the zero-model baseline and parses TSV geometry. When TSV finds nothing, it may
  add contour candidates; that fallback can over-detect hatching.
- `ppocr-v3` uses OpenCV DNN with the external
  [OpenCV Zoo PP-OCRv3 model](https://huggingface.co/opencv/text_detection_ppocr). It returns bounded
  polygons and never changes canonical source coordinates.
- `ppocr-v3+tesseract` merges overlapping, contained, and nearby aligned proposals from both
  detectors, then pads the surviving box so glyphs are enclosed. It does not drop low-confidence
  text or authorize trust. Union detection disables Tesseract contour fallback.

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

Recognition records every attempted input, its provider/direction/confidence, the selected result, and
the effective OCR language as evidence. Attempts accumulate across OCR reruns; `selectedIndex` points
into that cumulative history. `inputVariant` is either `original` or `preprocessed`. It identifies the
kind of input rather than a reproducible artifact generation, so replacing preprocessing output or its
provider/settings revokes trust for evidence that used `preprocessed`. Provider completion and
confidence never grant trust: all automatic proposals remain pending until a human confirms or ignores
them, and relevant OCR inputs changing revoke prior trust.

## Translation provider

```python
translate_text(text, context)
translate_batch(items)
health_check()
get_capabilities()
```

Manual preserves the current reviewed translation without automatic mutation. Mock is deterministic.
Dictionary is a local non-LLM exact/glossary translator. `argos-ja-zh` is the optional local neural
translator: it runs CTranslate2 Argos packages Japanese→English then English→Simplified Chinese on
this machine, never at startup, and never sends text anywhere. It is unavailable until the `mt` extra
and both checksum-verified packages are installed. Glossary and character names are applied as exact
replacements / protected terms. Traditional Chinese and other targets still need a remote translator.
OpenAI-compatible sends the current text plus
bounded preceding/following regions by reading order on the same page to a user-configured endpoint.
Both targets and context must carry explicit current human trust; pending and ignored regions are
excluded. A generated translation preserves the OCR trust decision but clears the separate
current-content confirmation when it changes the translated text, so the result must be reviewed and
reconfirmed before page review.
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
  feathering, alpha preservation, grayscale preservation on effectively gray pages, and exact zero-mask
  compositing.

After a page is repaired, the queue also writes comparison candidates: the provider result, OpenCV
Navier-Stokes, OpenCV Telea, and a line-art-guided blend that keeps Navier-Stokes structure where edges
continue and the smoother/AI fill in interiors. Pixels outside the mask stay bit-exact on every
candidate. A page that used only LaMa selects the line-art-guided plate by default; mixed or OpenCV
pages keep the provider result. Switching a candidate replaces the canonical inpainted bytes, clears
inpaint/typeset reviews, and does not export the unused alternatives. Automatic flags
(`mask-outside-changed`, `chroma-introduced`, `possible-smear`) are anomaly hints, not visual approval.

Masks can use detector polygons/current region geometry or text-aware local segmentation, followed by
padding, dilation, and feathering. Moving/resizing/rotating a detector-created region discards its stale
polygon so the visible edited box becomes the manual boundary. The UI overlays the actual persisted
mask and stores bounded add/erase brush strokes for one selected region; the composed mask is the input
to the inpainting provider.

The `safe` repair policy is the default. It accepts only regions with a current-policy `trusted`
disposition; confidence is evidence, not authorization. `recognized` accepts every non-empty source
region and `all` is an explicit high-risk override. Skipped/repaired counts and aggregate provider/trust
evidence are recorded without OCR text or region IDs. Typesetting may reuse an existing inpainted plate
only when it was generated under the same repair policy; a legacy, missing, `recognized`, or `all`
policy is never reused as a `safe` plate.

Provider completion is not visual approval. Preprocessed, inpainted, and typeset artifacts have
separate persisted accept/reject records. Generated-image export requires accepted checksums for the
current artifacts (and the inpaint mask), while JSON-only export remains independent.

## Optional model setup

The repository contains no weights. Run the explicit checksum-verifying installer, targeting the same
data directory used by the application:

```bash
npm run setup:models -- ppocr
npm run setup:models -- lama
npm run setup:models -- realesrgan
npm run setup:models -- argos-ja-zh
uv sync --project backend --extra ai --group dev  # required by LaMa and Real-ESRGAN ONNX
uv sync --project backend --extra mt --group dev  # required by Argos local translation
```

Or install the listed models plus the runtime with `npm run setup:ai`. Local Japanese-to-Chinese
translation is a separate explicit step: `npm run setup:mt`. Configuration can override the
standard model locations with `MANGA_LOCALIZER_PPOCR_DETECTION_MODEL`,
`MANGA_LOCALIZER_LAMA_INPAINTING_MODEL`, `MANGA_LOCALIZER_REALESRGAN_ONNX_MODEL`,
`MANGA_LOCALIZER_ARGOS_JA_EN_MODEL`, and `MANGA_LOCALIZER_ARGOS_EN_ZH_MODEL`. The NCNN adapter
uses `MANGA_LOCALIZER_REALESRGAN_NCNN_COMMAND` and optional
`MANGA_LOCALIZER_REALESRGAN_NCNN_MODELS`. Inspect licenses and checksums without downloading:

```bash
npm run setup:models -- --print-specs
```

## Adding a provider

Implement the relevant protocol, declare stable capabilities, add exact registry selection, test success,
unavailable, invalid-ID, and concurrency paths, and document install/privacy/licensing implications. A
provider must preserve its input, avoid automatic downloads, emit canonical coordinates, and record the
implementation actually used.
