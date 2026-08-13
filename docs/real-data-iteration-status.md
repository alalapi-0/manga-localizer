# Real-data iteration status

This is the sanitized public real-data summary and round log routed from `.agent/STATE.md`, the compact
current-state authority. It is updated after each completed round. Private images, OCR text,
machine-specific paths, model weights, and generated artwork are deliberately excluded.

Last updated: 2026-08-14

## Current phase

- Round 0 — repository audit and private-data setup: complete.
- Round 1 — unmodified 0.2.0 full-pipeline baseline: complete.
- Round 2 — preprocessing and stronger detection: complete.
- Round 3 — OCR stability, retry, and safe defaults: complete.
- Round 4 — text masks and real LaMa restoration: complete.
- Round 5 — configurable backend/UI pipeline: complete.
- Round 6 — real-data regression and failure-driven repair: complete.
- Round 7 — documentation, public-tree cleanup, and final gates: complete.
- Round 8 — full-book clean-plate visual review: partial; 18 of 130 pages have explicit output review.
- Round 9 — ignored aggregate failure evidence and durable visual-stage review checkpoint: delivered
  and verified by complete CI on the non-default task branch.
- Round 10 — post-OCR evidence and human trust gate: delivered to the non-default draft PR branch and
  verified by complete backend, frontend, privacy, and Playwright CI. Private real-data calibration is
  the next checkpoint; no new private quality result is claimed here.

## Private data boundary

The supplied material was copied into the project-local ignored tree before it was used. Real inputs,
models, reports, project databases, exports, contact sheets, and every generated artifact remain below
ignored local directories. The tracked evaluator accepts paths at runtime and never contains a personal
dataset path.

```text
tests/real-data/<dataset>/
├── input/
├── annotations/
├── samples/
└── runs/<round>-<configuration>/
    ├── catalog/
    ├── workspace/
    ├── report.json
    ├── report.md
    └── export-bundle/
```

The evaluator deliberately omits recognized text. Export bundles still contain source artwork and text
JSON and must remain private. The 130 supplied JPEGs also retain private EXIF description/time metadata;
they are not safe publication artifacts.

## Dataset and baseline

All 130 files (28 MiB) decode and import. They are grayscale-content JPEGs with heterogeneous crops and
sizes: 98 are below one megapixel, 99 have a short edge below 800 px, and 16 have an extreme aspect
ratio. This stresses low-resolution crops and line-art false positives more than JPEG corruption.

The unmodified 0.2.0 pipeline completed import, Tesseract detection/OCR, mock translation, OpenCV
inpainting, Pillow typesetting, and export across all 130 files with no stage-item failure:

| Baseline measurement | Result |
| --- | ---: |
| Detected / non-empty OCR regions | 2,542 / 1,835 |
| Empty region rate / empty pages | 27.8% / 1 |
| Mean OCR confidence | 0.386 |
| Detect / OCR seconds | 22.3 / 102.5 |
| Inpaint / typeset / export seconds | 27.5 / 30.0 / 87.1 |

Visual review proved the success count misleading. One detailed page received 101 rectangles over text
and artwork, and the renderer erased every non-ignored rectangle—including empty and unconfirmed
detections—destroying broad areas of line art. That destructive repair path became the primary P0.

## Round 2–3 detection and OCR comparisons

All figures below are coverage/stability proxies. The private set has no ground-truth boxes or
transcriptions, so they are not precision, recall, or character-error-rate claims.

| Configuration (130 images) | Regions | Non-empty | Non-empty rate | Empty pages | Characters | Mean conf. | Pre / detect / OCR s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| PP-OCRv3 + original image | 1,109 | 820 | 73.9% | 4 | 4,215 | 0.480 | 0 / 6.6 / 54.2 |
| OCR-friendly + edge enabled | 2,186 | 1,789 | 81.8% | 2 | 15,582 | 0.398 | 10.1 / 8.1 / 297.6 |
| OCR-friendly safe default | 708 | 603 | 85.2% | 25 | 4,280 | 0.583 | 9.8 / 8.2 / 63.4 |

The aggressive profile looked better by raw coverage but was invalidated by visual review: a known
no-text line-art page grew to 182 candidates and 131 non-empty OCR results. Removing edge enhancement
from the default made that page correctly remain at zero candidates. Compared with PP-OCR on the
original, the safe profile reduces candidates by 36%, keeps recognized-character volume within 2%, and
raises reported confidence, but increases empty pages from 4 to 25. It is therefore a precision-leaning
option, not a universally better default for every page.

The final full run preserved all 130 project source checksums and had no item failure. Of 708 regions,
351 low/empty preprocessed results were also attempted on the original crop; 130 selected the original
candidate and 221 retained the enhanced candidate. This makes preprocessing reversible at OCR-candidate
selection time instead of committing every page to one image variant.

Two failure-driven fixes materially changed these numbers:

1. PP-OCR polygons are clamped and mapped back to canonical source coordinates. This removed 11
   out-of-bounds OCR item failures observed in the first optional-detector run.
2. A completed zero-region detection is authoritative. Previously OCR misread it as “detection not run”
   and silently invoked the project Tesseract detector, recreating false positives.

## Round 4 mask and LaMa evidence

The optional LaMa provider uses the external OpenCV Zoo ONNX model through ONNX Runtime, with no startup
download. OpenCV remains the guaranteed fallback. The provider is lazy, thread-safe, context-cropped,
alpha-preserving, and composites only where the mask is nonzero.

Text-aware segmentation initially missed black strokes inside outlined Japanese glyphs. The final mask
keeps sparse strokes tight but falls back to the detector geometry when local segmentation already covers
most of a tight region, avoiding conspicuous holes. Moving/resizing/rotating a region now discards a stale
detector polygon so the visible edited geometry becomes the manual boundary.

A real complex-background crop was run through the actual model:

| Direct LaMa measurement | Result |
| --- | ---: |
| CPU inference | 3.8 s |
| Mask coverage | 1.60% |
| Changed pixels outside mask | 0 |
| Source checksum unchanged | yes |
| Output dimensions | unchanged |

The Japanese lettering was removed and the broad destructive baseline was avoided. Visual inspection
still found a pale reconstruction band where the original text covered detailed line art. This is a real
quality limit: LaMa predicts plausible context but cannot recover lines that were never visible.

## Round 5 complete representative pipeline

Three fixed representative images were selected from the project-local copy: a clear speech-bubble page,
a very low-resolution no-text line-art negative, and the complex background page. The exact configured
pipeline completed every stage with the real optional providers:

```text
preprocess -> PP-OCRv3 detect -> Tesseract OCR -> mock translate
           -> safe LaMa restore -> Pillow typeset -> portable export
```

| Measurement | Result |
| --- | ---: |
| Stage item failures | 0 / 21 |
| Detected / non-empty regions | 35 / 31 |
| OCR retries / original selected | 13 / 5 |
| Safe eligible / repaired regions | 15 / 15 |
| Low-confidence regions skipped | 20 |
| LaMa stage | 24.0 s |
| Source checksum failures | 0 |
| Repair dimension mismatches | 0 |
| Changed pixels outside masks | 0 |
| Mean mask coverage | 3.43% |

The no-text negative produced four low-confidence OCR false positives, but safe repair accepted zero;
its mask stayed empty and its repaired/typeset/exported pixels remained unchanged. The complex page
accepted one of five regions and repaired/typeset only that region. Visual review exposed and fixed a
pipeline bug where Chinese was still typeset over regions that safe repair had skipped; repair and
typesetting now share the same eligibility policy. The final candidate additionally requires each
typeset region to intersect the actual generated mask, so an eligible region whose text mask is empty
cannot be overlaid on untouched art.

The exact final candidate reran the same three-image pipeline after that mask gate and the canonical
repair defaults were added. All 21 stage items again completed, the 35/31 detection/OCR totals and 15
repaired regions remained stable, zero pixels changed outside masks, and the no-text negative remained
pixel-identical from source through repair and typesetting. Its schema-3 report records the provider,
profile, policy, direction, concurrency, and model-presence flags without recording model paths or OCR
text. This is structural and regression evidence, not a translation-quality result: the evaluator uses
mock translations, and visual review still shows OCR segmentation, vertical layout, and font-fit work
before unattended publication would be appropriate.

## Implemented capability summary

- Unified preprocessing protocols and result/coordinate contract.
- Always-available OpenCV/Pillow profiles with per-switch single/batch jobs and enhanced preview.
- Optional Real-ESRGAN NCNN adapter with honest unavailable state; not exercised here because the local
  executable/model was absent.
- Optional PP-OCRv3 polygon detector and explicit detection/OCR provider separation.
- Preprocessed-crop OCR retry against original, quality selection, and attempt/input provenance.
- Versioned separate detector/OCR evidence, retained automatic proposals, stable trust reasons, and
  trusted-only translation/default safe rendering, with fail-closed legacy migration and complete CI.
- Text-aware or full-region masks, padding, dilation, real soft feathering, and actual mask overlay.
- Persisted bounded brush/eraser strokes for manual correction of a selected region's mask.
- Exact inpainting provider selection; unknown IDs fail instead of silently using OpenCV.
- Optional real LaMa ONNX restoration plus OpenCV fallback.
- Safe/recognized/all repair policies, eligible/skipped/repaired metrics, and typesetting gated by both
  safe eligibility and the actual generated repair mask.
- Preprocess/compare/mask controls in the workbench and corrected task refresh/partial-creation behavior.
- Persisted preprocess/inpaint/typeset accept/reject decisions bound to artifact and mask checksums;
  generated-image export and portable generated assets require current accepted review.
- Generic private evaluator with per-file failures, OCR proxies, immutable-source checks, dimension and
  mask structural metrics.

## Remaining issues and next roadmap

1. **Ground truth:** create a private annotated stress set with box precision/recall and Japanese OCR CER.
   Current proxies cannot decide whether the safe profile's 25 empty pages are missed text or true
   negatives.
2. **Preprocessing policy:** support per-page profile suggestions and paired preview, not a book-wide
   assumption. Run the already integrated Real-ESRGAN adapter only after a licensed local executable and
   model are installed, then compare against the exact same annotations.
3. **Detection/OCR:** add MangaOCR or PaddleOCR recognition behind the existing protocol, region-level
   rerun/history controls, and calibrated confidence. Evaluate the delivered human trust gate against
   annotated/private-safe evidence; keep empty detection authoritative.
4. **Mask editing:** add arbitrary polygon regions and a whole-page raster workflow. Current brush and
   eraser strokes are bounded to one selected rectangular region.
5. **Restoration quality:** add tiled/GPU execution and compare LaMa with newer line-art-aware models.
   Preserve the exact mask-outside invariant and never auto-repair uncertain regions.
6. **Typesetting:** use real Chinese translation in visual acceptance, improve font/vertical punctuation
   matching, reduce fragmented small-box layouts, and add collision/overflow review before export.
7. **Performance:** reuse batched model tensors where supported. CPU LaMa is practical for selected
   regions, not an unreviewed book-wide `all` policy.

## Round log

- **Round 0:** audited repository/runtime/UI, copied and validated private data, and established ignore
  boundaries.
- **Round 1:** added the generic evaluator, ran the complete original pipeline, recorded per-file
  failures/proxies, and identified destructive repair.
- **Round 2:** added preprocessing artifacts/profiles/providers, optional Real-ESRGAN adapter, PP-OCR
  detection, canonical mapping, and before/after UI.
- **Round 3:** added OCR retry/quality selection, clamping, authoritative empty-detection semantics, and
  evidence-driven safe preprocessing defaults.
- **Round 4:** added text masks, soft feathering, exact provider routing, safe repair policy, LaMa ONNX,
  and real-model visual/structural verification.
- **Round 5:** integrated configurable batch/UI stages, mask overlay, edit-safe refresh, partial task
  visibility, and full representative real pipeline.
- **Round 6:** used new real failures to remove unsafe edge defaults, stale polygons, detector fallback,
  and unsafe typesetting over skipped regions; reran every affected comparison.
- **Round 7:** completed public documentation and cleanup; passed 130 backend tests, 64 frontend tests,
  two Playwright Chromium journeys, production builds, release/privacy scans, and the final three-image
  real-provider regression.
- **Round 8:** prepared review packs for all 130 pages, but recorded explicit completed output review for
  only 18 pages. This is a partial clean-plate checkpoint, not a full-book quality result.
- **Round 9:** recorded ignored aggregate evidence that no-text pages still receive many automatic text
  candidates and that structurally safe complex repairs can remain visually unacceptable. Added durable
  visual-stage accept/reject state, checksum binding, invalidation, UI controls, and export gating; the
  non-default branch passed full backend, frontend, privacy, and Playwright CI.
- **Round 10:** delivered versioned detection/OCR evidence and a fail-closed human trust gate to the
  non-default draft PR branch. Fresh independent review covered the implementation and CI repairs;
  GitHub CI run `31729184780` passed Ruff lint/format, 184 backend tests, the release/privacy audit,
  frontend lint/typecheck/build with 92 tests, and both Playwright journeys at
  `29305788cfbb8f4d1f36354ba89c40e18d15400e`. No new private quality result was generated or published.
