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
- Optional local LaMa ONNX inpainting provider with lazy thread-safe inference, context crops, exact
  mask-outside preservation, and an explicit checksum-verifying model installer.
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
  Contact sheets remain in the ignored private run directory for local visual review.

### Known limitations

- The private set has no box/transcription ground truth, so region coverage, confidence, and character
  counts are proxies rather than detection recall or OCR accuracy.
- AI preprocessing via Real-ESRGAN ONNX is local, optional, and native 4×; 2×/3× requests downscale
  that AI output. The NCNN CLI adapter remains available when a licensed local executable is
  installed. LaMa remains CPU-expensive and imperfect on line art fully hidden by lettering.
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
