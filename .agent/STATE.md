# Manga Localizer — Project State

Updated: 2026-08-15

## Authority and purpose

This file is the compact current-state authority for implementation, verification, privacy boundaries,
and delivery status. `docs/real-data-iteration-status.md` is the routed detailed evidence and round log;
it does not define a competing current state. Update this file whenever the active candidate, registered
verification, protected boundaries, or known blockers materially change.

## Goal

Deliver a local-first, desktop-oriented manga localization workbench covering immutable image import,
optional preprocessing, text detection, Japanese OCR, review, translation, safe text removal/background
restoration, Chinese typesetting, resumable batch work, and portable export. The active Unreleased
iteration extends the verified 0.2.0 foundation using the user's private 130-image dataset without
placing private inputs, OCR text, models, databases, or generated artwork in the public candidate.

## Current round and candidate

Round 11 delivers a runnable local Real-ESRGAN AI upscaler on the non-default branch
`agent/manga-round7-governance-20260812` through draft PR #3. The Round 11 feature commit is
`866ad13728a029f468e447aa6c39bebe42121d92` with GitHub CI run `31851316610` green. The previous
Round 10 trust-gate head is `0d6ff98387447c176ef5addeeaa21d007df05db3`. Round 11 adds
`realesrgan-onnx` (BSD-3-Clause `RealESRGAN_x4plus_anime_6B`, checksum-verified explicit install, native
4× with honest 2×/3× downscale, tiling, grayscale preservation) and keeps `realesrgan-ncnn` as an
optional CLI adapter that now discovers a data-dir binary and passes a sibling `models/` folder. Classic
Lanczos remains `opencv-pillow` and is labeled `aiUpscale: false`. A third-party NCNN executable was not
run in this environment; the ONNX provider is the exercised equivalent local AI path. Three
representative private pages were compared against Lanczos in an ignored directory: source checksums
unchanged, output sizes exact, AI distinct from classic, Laplacian variance 47.3 → 2428.0, unique colors
kept at 8-bit grayscale, 65 s on M4 CPU. Contact sheets were not published or sent to a remote vision
model. Round 8 remains 18/130 explicit visual reviews. The full product goal remains active. No merge,
tag, release, or deployment has occurred.

## Environment evidence

- macOS on Apple Silicon (M4, Metal available), Node.js 26, npm 11, uv, and CPython 3.12.
- Tesseract 5.5 is installed with `jpn`, `jpn_vert`, `chi_sim`, and `chi_tra` data.
- OpenCV/Pillow are the dependency-light image baseline; ONNX Runtime is available through the optional
  `ai` extra for LaMa and Real-ESRGAN ONNX.
- The private PP-OCRv3, LaMa, and Real-ESRGAN anime ONNX weights are checksum-verified and live only in
  ignored local model directories. Real-ESRGAN NCNN remains a CLI adapter; no NCNN executable was run
  here.

## Decisions

- Repository/distribution name: `manga-localizer`; Python import package: `manga_localizer`.
- Frontend: React, TypeScript, Vite, Zustand, React Konva, and dense custom CSS tokens.
- Backend: FastAPI, Pydantic, SQLAlchemy/SQLite, Pillow, OpenCV, and background asyncio workers.
- Preprocessing has one provider/result/coordinate contract. `opencv-pillow` is always available and
  uses classic Lanczos (`aiUpscale: false`). `realesrgan-onnx` is the runnable local AI upscaler:
  explicit checksum/license install, no startup download, native 4×, 2×/3× downscale from that AI
  result, tile size 256 on this 16 GB M4, and grayscale preservation. `realesrgan-ncnn` remains optional
  and never downloaded at application startup.
- Detection and recognition are separate selections. Tesseract remains the zero-model detector/OCR
  baseline; optional PP-OCRv3 supplies bounded detector polygons. A completed zero-detection result is
  authoritative and is not silently replaced during OCR.
- Low/empty OCR on a preprocessed crop is retried against the immutable original crop, with the selected
  input and attempt count persisted as provenance.
- Detector confidence, OCR confidence, every OCR attempt across reruns, and the selected input are
  stored separately.
  Automatic proposals always remain `review`; only explicit human confirmation creates `trusted`, and
  confidence alone never authorizes translation or default safe rendering. Recognition-input edits or
  replacement of depended-on preprocessing revoke trust; translation/style/mask-only edits preserve it.
- Inpainting uses exact provider routing. OpenCV is the guaranteed fallback; optional LaMa ONNX is lazy,
  local, context-cropped, and composites with exact mask-outside preservation.
- Repair defaults to the `safe` eligibility policy. Canonical repair settings are persisted across API,
  queue, and UI; text/full-region masks support padding, dilation, feathering, editable geometry, and an
  actual-mask preview. Bounded add/erase strokes are persisted per region. Typesetting requires safe
  eligibility and intersection with the generated mask, and cannot reuse an inpaint cache made under a
  different repair policy.
- Preprocess, inpaint, and typeset results have revision-guarded accept/reject records bound to the
  exact response bytes decoded in the review canvas; inpaint also binds and visibly reviews its mask.
  Regeneration, changed bytes, or an upstream change clears or conflicts with affected reviews.
  Generated-image export and portable generated assets require current accepted results; JSON-only
  export remains independent.
- Moving, resizing, merging, or splitting a detector region removes its stale polygon while preserving
  the remaining repair provenance. Generated preview/compare controls are gated by current artifacts.
- Projects remain portable: each output root contains `project/project.sqlite3` and a sanitized
  `project/project.json`; a local catalog only remembers recently opened manifests.
- The private evaluator is path-parameterized, refuses a non-empty output directory, omits OCR text and
  model paths, and records non-sensitive configuration plus per-image structural metrics.
- Secrets are environment- or session-only and are never written to project JSON, SQLite, or logs.
- Public job stage outputs and failure messages are fixed operational/aggregate projections. Detailed
  options, paths, provider exceptions, and delivery metadata remain only in private project state.

## Protected boundaries

- Do not modify sibling projects or workspace-level control files.
- Never overwrite imported source images.
- Do not commit user images, outputs, private reports, databases, environment files, credentials, model
  caches, downloaded model weights, copyrighted fonts, or machine-specific paths.
- Do not send images remotely. Text is sent remotely only when the user explicitly selects a remote
  translator.
- `tests/real-data/` and `.manga-localizer/` are ignored private boundaries. Export bundles remain
  private because they contain source artwork and text JSON.

## Completion ledger

- [x] Original 0.2.0 foundation: project/image management, workbench, OCR/translation, repair,
  typesetting, persistent queue, export, reopen, automated verification, and release documentation.
- [x] Real-data Round 0: repository/runtime audit, private test boundary, and dataset copy/validation.
- [x] Round 1: complete original 130-image baseline and prioritized failure inventory.
- [x] Round 2: OpenCV/Pillow preprocessing, optional Real-ESRGAN adapter, enhanced preview, and
  PP-OCRv3 detector.
- [x] Round 3: canonical coordinate clamping, authoritative empty detection, OCR retry/selection, and
  evidence-driven safe preprocessing defaults.
- [x] Round 4: text/full-region masks, soft feathering, exact provider routing, safe repair policy, and
  real LaMa inference/visual review.
- [x] Round 5: configurable UI/batch pipeline, actual mask overlay, partial-job visibility, and
  edit-safe refresh.
- [x] Round 6: failure-driven fixes for false-positive edges, stale polygons/artifacts, skipped-region
  typesetting, profile precedence, repair defaults, zero-effect feedback, and preview/compare guards.
- [x] Round 7: public documentation, evaluator configuration evidence, full gates, exact real-provider
  regression, and release/privacy audit.
- [ ] Round 8: full-book clean-plate visual review is partial at 18/130 explicitly completed pages.
- [x] Round 9: ignored aggregate evidence, durable visual-stage review, checksum-bound generated-image
  export, governed review, non-default-branch delivery, and complete CI verification.
- [x] Round 10: post-OCR evidence/trust gate, public regression, governed review, non-default-branch
  delivery, and complete backend/frontend/privacy/browser CI verification.
- [x] Round 11: runnable local Real-ESRGAN ONNX upscaler, explicit model install, NCNN model-dir fix,
  private classic-vs-AI comparison, public regression, and complete CI on the non-default branch.
- [ ] Next real-data checkpoint: privacy-safe annotated detection/OCR evaluation and local visual review
  of the ignored Real-ESRGAN contact sheets; then line-art-aware restoration and real translation.

## Verification evidence

- Prior Round 7 `npm run check` reproduced on 2026-08-12: 2 launcher tests; backend Ruff lint/format and
  130 pytest cases; frontend ESLint/TypeScript, 64 Vitest cases, and the production Vite build all passed.
- End to end: 2 Playwright Chromium journeys passed, covering import, preprocessing, real local
  detection/OCR, review/edit, actual mask preview, repair, typesetting, export, and reopen.
- Private dataset: all 130 supplied JPEGs were copied into the ignored project boundary before use,
  decoded/imported, and completed the original baseline plus multiple full detection/OCR comparisons.
- Exact real-provider regression: 3 representative images completed all 21 stage items using
  OpenCV/Pillow, PP-OCRv3, Tesseract, safe LaMa, Pillow typesetting, and export. Results were 35 detected
  / 31 non-empty OCR regions, 13 OCR retries / 5 original selections, 15 eligible / 15 repaired / 20
  skipped regions, zero source checksum or dimension failures, and zero changed pixels outside masks.
  One zero-mask negative remained pixel-identical from source through repair and typesetting.
- Prior Round 7 release/privacy reproduced on 2026-08-12: `npm run audit:release` scanned 108 candidate
  files and all reachable historical blobs with zero findings. `uv lock --check`, compileall, and
  `git diff --check` passed; ignored/private/model/DB paths have zero tracked files and the public
  candidate contains no private sample name or personal absolute path.
- Round 9 remote verification: GitHub CI run `31708706339` passed at
  `ae7146faf74d20babc63310236ffa9295f907cdd`. Backend Ruff lint and format passed; all 149 pytest
  cases passed; the release audit scanned 108 candidate files and 214 historical blobs with zero
  findings. Frontend ESLint, TypeScript, 75 Vitest cases, and the production build passed. Both
  Playwright Chromium journeys passed in 42.5 seconds.
- Round 9 local verification also passed 2 launcher tests, frontend lint/typecheck/75 tests/build,
  E2E spec lint and two-test discovery, `uv lock --check`, compileall, `git diff --check`, and the direct
  release audit. Backend dependencies were unavailable in the offline local environment, so the exact
  remote candidate's successful CI is the authoritative backend and live-browser evidence.
- Round 10 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 92 Vitest cases,
  and production build; two-test Playwright discovery/compilation; 10 isolated release-audit tests;
  `uv lock --check --offline`; compileall; `git diff --check`; and a direct release audit over 110
  candidate files plus 202 historical entries. The task-created incomplete backend virtual environment
  was moved out of the repository.
- Round 10 authoritative remote verification: GitHub CI run `31730263494` passed at
  `0d6ff98387447c176ef5addeeaa21d007df05db3`. Backend Ruff lint/format, all 184 pytest cases, and the
  release audit passed. Frontend ESLint, TypeScript, all 92 Vitest cases, and production build passed.
  Both Playwright Chromium journeys passed.
- Round 11 local verification passed 2 launcher tests; backend Ruff lint/format and 192 pytest cases;
  frontend ESLint, TypeScript, 92 Vitest cases, and production build; release audit over 113 candidate
  files plus 305 historical blobs; `uv lock --check`; compileall; and `git diff --check`. Playwright
  discovered both Chromium journeys; this environment lacked Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 11 authoritative remote verification: GitHub CI run `31851316610` passed at
  `866ad13728a029f468e447aa6c39bebe42121d92`. Backend Ruff lint/format, pytest, and the release audit
  passed. Frontend lint/typecheck/92 tests/build passed. Both Playwright Chromium journeys passed.
- Round 11 private upscale comparison: three representative pages, requested 2× from native 4× AI,
  tile 256, BSD-3-Clause RealESRGAN_x4plus_anime_6B, ONNX Runtime 1.28.0 on M4 CPU. Zero source checksum
  failures, exact output sizes, AI distinct from Lanczos on every page, mean Laplacian variance
  47.324 → 2427.957, unique colors remained 8-bit grayscale after chroma suppression, 64.9 s total.
  Contact sheets stay under the ignored real-data run directory and were not opened by a remote model.

## Known limitations and blockers

No source-integrity or privacy blocker is currently known, but the full product objective is not yet
complete. Round 11 is an AI-upscale checkpoint rather than a claim of unattended full-book output
quality.

- The private dataset has no annotated boxes/transcriptions, so coverage and confidence are proxies,
  not detection recall or OCR accuracy.
- The representative export uses deterministic mock translations for structural testing. Real Chinese
  translations require manual/remote review, and fragmented boxes, vertical layout, font fit, and LaMa
  reconstruction artifacts still prevent unattended publication.
- MangaOCR/PaddleOCR recognition, arbitrary polygon/whole-page mask editing, and line-art-aware
  restoration remain roadmap work. Local visual review of Real-ESRGAN contact sheets is still required
  before treating AI upscaling as publication-quality.
- The post-OCR trust gate is delivered and CI-verified, but its precision/recall and calibration still
  require privacy-safe annotated or aggregate real-data evaluation.
