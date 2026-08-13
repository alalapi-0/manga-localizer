# Manga Localizer — Project State

Updated: 2026-08-13

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

Real-data Round 9's durable visual-stage signoff checkpoint is delivered to the non-default branch
`agent/manga-round7-governance-20260812` at `ae7146faf74d20babc63310236ffa9295f907cdd`
through draft PR #3. Fresh Judge/Governor review approved the implementation and each subsequent
CI-only repair. GitHub CI run `31708706339` passed the complete backend, frontend, and Playwright
gates for that exact remote commit. Round 8 prepared review material for all 130 pages but has explicit
completed output review for only 18; it remains a partial result, not a full-book visual acceptance.
Ignored Round 9 aggregate evidence confirms that confidence is not a sufficient text-validity gate and
that structurally safe complex repairs can remain visually unacceptable. The full product goal remains
active; the next quality checkpoint is the automatic post-OCR trust/disposition gate. No merge, tag,
release, or deployment has occurred.

## Environment evidence

- macOS on Apple Silicon (M4, Metal available), Node.js 26, npm 11, uv, and CPython 3.12.
- Tesseract 5.5 is installed with `jpn`, `jpn_vert`, `chi_sim`, and `chi_tra` data.
- OpenCV/Pillow are the dependency-light image baseline; ONNX Runtime is available through the optional
  `ai` extra for the exercised LaMa provider.
- The private PP-OCRv3 and LaMa ONNX weights are checksum-verified and live only in ignored local model
  directories. Real-ESRGAN NCNN is implemented and fake-CLI tested but no local executable/model was
  available for a real run.

## Decisions

- Repository/distribution name: `manga-localizer`; Python import package: `manga_localizer`.
- Frontend: React, TypeScript, Vite, Zustand, React Konva, and dense custom CSS tokens.
- Backend: FastAPI, Pydantic, SQLAlchemy/SQLite, Pillow, OpenCV, and background asyncio workers.
- Preprocessing has one provider/result/coordinate contract. `opencv-pillow` is always available;
  `realesrgan-ncnn` is optional, local, explicit, and never downloaded at application startup.
- Detection and recognition are separate selections. Tesseract remains the zero-model detector/OCR
  baseline; optional PP-OCRv3 supplies bounded detector polygons. A completed zero-detection result is
  authoritative and is not silently replaced during OCR.
- Low/empty OCR on a preprocessed crop is retried against the immutable original crop, with the selected
  input and attempt count persisted as provenance.
- Inpainting uses exact provider routing. OpenCV is the guaranteed fallback; optional LaMa ONNX is lazy,
  local, context-cropped, and composites with exact mask-outside preservation.
- Repair defaults to the `safe` eligibility policy. Canonical repair settings are persisted across API,
  queue, and UI; text/full-region masks support padding, dilation, feathering, editable geometry, and an
  actual-mask preview. Bounded add/erase strokes are persisted per region. Typesetting requires safe
  eligibility and intersection with the generated mask.
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

## Known limitations and blockers

No source-integrity or privacy blocker is currently known, but the full product objective is not yet
complete. Round 9 is delivered and verified; it is a safety/review checkpoint rather than a claim of
unattended full-book output quality.

- The private dataset has no annotated boxes/transcriptions, so coverage and confidence are proxies,
  not detection recall or OCR accuracy.
- The representative export uses deterministic mock translations for structural testing. Real Chinese
  translations require manual/remote review, and fragmented boxes, vertical layout, font fit, and LaMa
  reconstruction artifacts still prevent unattended publication.
- MangaOCR/PaddleOCR recognition, an automatic post-OCR trust gate, arbitrary polygon/whole-page mask
  editing, line-art-aware restoration, and a real Real-ESRGAN run remain roadmap work.
- Automatic regions still need a post-OCR trust/disposition gate that retains every proposal for human
  review while preventing unconfirmed automatic OCR from reaching translation or safe repair.
