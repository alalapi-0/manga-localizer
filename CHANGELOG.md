# Changelog

All notable changes are documented here. The format follows Keep a Changelog and the project uses
Semantic Versioning.

## [Unreleased]

### Added

- Persisted, batch-capable image preprocessing with OpenCV/Pillow profiles and independent upscale,
  denoise, sharpen, contrast, edge, and binarization controls; enhanced-image preview and canonical
  coordinate mapping.
- Optional Real-ESRGAN NCNN preprocessing adapter with an honest unavailable state and no implicit
  downloads.
- Optional Real-ESRGAN ONNX anime 4× preprocessor using checksum-verified local weights, tiled
  inference, and explicit 2×/3× downscale from the native 4× result. Classic Lanczos remains a
  separate compatibility upscaler.
- Optional PP-OCRv3 OpenCV-DNN polygon detector, separated from Tesseract recognition.
- Optional `ppocr-v3+tesseract` union detector that concatenates both proposal lists without
  confidence filtering or overlap merging. Union disables Tesseract's empty-page contour fallback.
- Privacy-safe detection/OCR evaluation with IoU matching, separate detector/OCR confidence, CER,
  negative-page false positives, a public synthetic stress set, and ignored private draft annotations.
- Optional local LaMa ONNX inpainting provider with lazy thread-safe inference, context crops, exact
  mask-outside preservation, grayscale preservation on manga pages, and an explicit checksum-verifying
  model installer.
- Line-art-guided inpainting candidates: each repair job stores the provider result plus OpenCV
  Navier-Stokes, Telea, and a structure/texture blend. Pages that used only LaMa default to the
  line-art-guided plate. Editors can compare, switch, accept, or reject locally; auto metrics only flag
  mask-outside changes, chroma, or possible smearing.
- Optional local Argos Japanese-to-Chinese translator using checksum-verified CTranslate2 packages and
  an English pivot. It stays unavailable without the `mt` extra and both packages, never downloads at
  startup, and never sends text off-machine. Traditional Chinese still needs a remote translator.
- Typesetting overflow review: completed Pillow layouts persist overflowing region IDs, the workbench
  filters and highlights those boxes, and Shift+arrow skips already-checked pages. Overflow is a review
  hint, not an export hard gate.
- Per-page preprocessing profile suggestions from source-image size, contrast, and sharpness. The
  workbench can process the current page with that profile or adopt it as the project default; it never
  auto-applies a book-wide assumption.
- Vertical typesetting maps CJK punctuation to presentation forms and hangs comma/period glyphs.
  Horizontal layouts keep the authored punctuation; stored translation text is not rewritten.
- Privacy-safe detector-draft review promotion: a local human lists page IDs to accept or reject, and
  the CLI copies ignored annotation JSON into a new ignored directory. Progress output is aggregate
  counts only and never prints OCR text or page IDs. Empty pages are not auto-promoted.
- Actual mask preview, text-aware/full-region mask strategies, padding/dilation/feather controls, and
  editable region boundaries plus persisted bounded brush/eraser strokes for manual mask correction.
- Durable accept/reject review for preprocessing, inpainting, and typesetting artifacts, with
  revision history and application-restart recovery.
- Versioned per-region detection/OCR evidence with provider/input/language provenance, separate
  confidence values, OCR attempts retained across reruns, a stable trust disposition/reason, and
  fail-closed legacy project migration that invalidates repair/typeset caches created under the former
  confidence policy.
- Private path-parameterized real-data evaluator with a non-sensitive run-configuration snapshot,
  aggregate/per-image OCR coverage, stage failures, source checksum, dimensions, mask coverage, and
  mask-outside change metrics; OCR text and model paths are omitted.

### Changed

- OCR retries low/empty preprocessed crops against the immutable original and records attempt/input
  provenance.
- Inpainting now selects the requested registry provider exactly, rejects unknown IDs, records actual
  provenance, and defaults to a safe eligibility policy that skips empty or untrusted automatic regions.
- OpenCV repair soft-composites feathered masks instead of discarding the feather through
  binarization; typesetting and inpainting provider selection are now independent. Typesetting requires
  both safe-repair eligibility and intersection with the actual generated mask.
- Completed zero-detection pages remain authoritative and are no longer silently re-detected by the
  project fallback during OCR.
- Moving, resizing, merging, or splitting a detector-created region discards its stale polygon so its
  current geometry controls the manual mask.
- Repair settings now have one persisted canonical default across API, queue, and UI; full-region mode
  explicitly ignores detector polygons.
- Batch jobs enter the frontend state as each stage is created, and task refresh rechecks request
  freshness and pending edits before applying a server response. The batch action footer remains
  reachable on short viewports.
- Generated preview and comparison controls, including keyboard shortcuts and cross-page state, stay
  disabled or return to the original until a real enhanced, repaired, or typeset artifact exists.
- OCR-friendly edge enhancement is opt-in after real line-art testing showed severe false positives.
- Generated-image export requires accepted, checksum-current inpaint and, when applicable, typeset
  results; inpaint acceptance also binds the mask, upstream changes clear dependent reviews, and
  unreviewed generated artifacts are excluded from portable bundles. JSON-only export remains available
  without image review.
- Automatic detection/OCR proposals remain reviewable regardless of confidence. Only explicit human
  trust can authorize translation/context or the default safe repair/typesetting path; relevant source,
  geometry, type, direction, confidence, or provenance changes revoke that authorization.
- Detection reruns retain prior proposals, public job stage outputs report operational/aggregate fields
  without OCR text, region IDs, internal options, or filesystem paths, and native Tab navigation is no
  longer captured for region cycling.
- Preprocessing changes revoke trust that depended on the preprocessed variant, and safe typesetting
  never reuses a plate generated under the `recognized` or `all` repair policy.

### Verification

- Copied 130 private JPEG inputs into the ignored project test boundary and completed multiple full
  detection/OCR comparisons plus a complete original end-to-end baseline; no real image, output, OCR
  text, model weight, or personal path is tracked.
- Ran the real LaMa model on a complex background crop: source checksum and dimensions were preserved
  and zero pixels outside the mask changed. Visual reconstruction improved substantially over the
  destructive baseline but still showed a visible light reconstruction band.
- Added focused backend/frontend regression coverage for preprocessing, coordinate mapping, OCR retry,
  empty-detection semantics, provider routing, LaMa contracts, text masks, safe editing, partial batch
  creation, and pending-edit refresh behavior.
- The prior Round 7 candidate passed 130 backend tests, 64 frontend tests, two Playwright Chromium journeys,
  production builds, release/privacy checks, and a repeated three-image real PP-OCRv3/LaMa pipeline with
  zero stage failures or mask-outside pixel changes.
- The Round 10 trust-gate checkpoint passed Ruff lint/format, 184 backend tests, the release/privacy
  audit, frontend lint/typecheck/build with 92 tests, and both Playwright Chromium journeys on the exact
  non-default-branch commit. No new private real-data quality result is claimed by this verification.
- Round 11 installed and checksum-verified the BSD-3-Clause Real-ESRGAN anime ONNX model locally, ran
  it on three representative private pages against classic Lanczos, and recorded only aggregate
  structural metrics: zero source-checksum failures, correct 2× output sizes, AI output distinct from
  Lanczos on every page, and substantially higher Laplacian variance after grayscale preservation.
  Contact sheets remain in the ignored private run directory for local visual review. GitHub CI run
  `31851316610` passed on `866ad13728a029f468e447aa6c39bebe42121d92`.
- Round 12 evaluated detectors against a public synthetic ground-truth stress set (bubble, non-bubble,
  SFX/art, vertical, single-character, complex line-art, and a no-text hatch negative). PP-OCRv3
  reached precision/recall 1.0 with zero negative-page false positives; matched Tesseract OCR CER was
  0.42. Tesseract-alone produced 80 false positives on the negative page. Private ignored drafts cover
  all 130 pages (727 PP-OCR boxes, 18 empty pages) and are not independent ground truth. GitHub CI run
  `31852816928` passed on `761c30d319455f11af82fc2358bc830797ebdac8`.
- Round 13 stores provider, Navier-Stokes, Telea, and line-art-guided inpainting candidates after each
  nonempty repair, keeps mask-outside pixels exact, preserves grayscale on LaMa manga pages, and adds a
  synthetic local comparison script. Automatic smear/chroma flags are anomaly hints, not visual
  approval. Complex line art and large SFX still need human compare/accept. Local gates passed 2
  launcher tests, 209 backend tests, and 95 frontend tests plus the production build. GitHub CI run
  `31854780188` passed on `751d3a985bf9e320f2bf11b1f2c2c6681b620e45`.
- Round 14 adds an optional local Argos Japanese-to-Chinese translator with checksum-verified packages,
  an English pivot, glossary/name protection, and a public synthetic comparison script. It does not send
  text off-machine. Traditional Chinese and manga-tuned quality remain out of scope for this provider.
  Local gates passed 2 launcher tests, 214 backend tests, and 95 frontend tests plus the production
  build. The release audit scanned 126 candidate files and 390 historical blobs. GitHub CI run
  `31856326624` passed on `a0bd72cc03b1d29b33a5a92ada2b82613f28d581`.
- Round 15 adds a local detector-draft accept/reject promotion CLI. It does not open images, does not
  auto-promote empty pages, and prints only aggregate counts. Local gates passed 2 launcher tests, 218
  backend tests, and 95 frontend tests plus the production build. The release audit scanned 128
  candidate files and 420 historical blobs. GitHub CI run
  `31858177141` passed on `8d50361ac4cf8b5f296fd480e2c2c7bd1efe2219`.
- Round 16 persists typesetting overflow IDs for workbench review, highlights overflowing boxes, and
  adds Shift+arrow skip of already-checked pages. Overflow is not an export hard gate. Local gates
  passed 2 launcher tests, 219 backend tests, and 97 frontend tests plus the production build. The
  release audit scanned 128 candidate files and 435 historical blobs. GitHub CI run
  `31860160644` passed on `20b3b1e9236b866dd4cdf07aa9b6d865b03f3d2b`.
- Round 17 adds per-page preprocessing profile suggestions from local size, contrast, and sharpness.
  The workbench can process the current page with that profile or adopt it as the project default; it
  never auto-applies a book-wide assumption. Local gates passed 2 launcher tests, 221 backend tests,
  and 99 frontend tests plus the production build. The release audit scanned 128 candidate files and
  467 historical blobs. GitHub CI run `31861476315` passed on
  `302837fa3403e79a2eb51ab5274ecc85eb56741e`.

### Known limitations

- The private set still lacks human-reviewed boxes/transcriptions. Detector-draft JSON is a proposal
  bootstrap, not precision, recall, or OCR accuracy.
- AI preprocessing via Real-ESRGAN ONNX is local, optional, and native 4×; 2×/3× requests downscale
  that AI output. The NCNN CLI adapter remains available when a licensed local executable is
  installed. LaMa remains CPU-expensive and imperfect on line art fully hidden by lettering.
  Local Argos translation is an English-pivot general MT package, not a manga-tuned translator, and
  currently emits Simplified Chinese only.
- Mask correction strokes are scoped to one selected rectangular region; arbitrary whole-page raster
  editing and arbitrary persisted region polygons remain roadmap work.

## [0.2.0] - 2026-08-06

### Added

- Local-first FastAPI/React workbench foundation and portable SQLite projects.
- Unicode-aware image import, region review, Tesseract OCR, translation providers, OpenCV inpainting,
  Pillow typesetting, persistent jobs, and safe structured export.
- User-bounded item concurrency, automatic horizontal/vertical Japanese OCR selection, downstream
  artifact invalidation, and reopenable sanitized custom export snapshots.
- Cumulative exact import boundaries, NFKC/case-folded portable conflict handling, revision-guarded
  autosave, cooperative cancellation/restart recovery, and crash-recoverable atomic export bundles.
- Strict remote endpoint validation and translation/typesetting/export invalidation when the configured
  endpoint or model changes.
- Offline backend/frontend/E2E verification, privacy and provider documentation, and release audit.

### Verification

- `npm run check`: 2 launcher tests, 78 backend pytest cases, and 39 frontend Vitest cases, with all
  lint, format, typecheck, and production-build gates passing.
- Two Playwright Chromium scenarios passed, including the real local Tesseract pipeline.
- Both npm audits and `pip-audit` reported 0 known vulnerabilities; the release audit scanned 94
  candidate files plus Git history with 0 findings.
- The one-command launcher served the root page and API, including the Vite `/api` proxy.

This version records the first complete V0.2 MVP feature set.

### Known limitations

- Baseline text detection, OCR, inpainting, and typesetting require manual correction on complex art.
- Native desktop packaging and advanced ML providers are deferred.
