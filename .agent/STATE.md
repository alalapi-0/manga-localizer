# Manga Localizer — Project State

Updated: 2026-08-18

## Authority and purpose

This file is the compact current-state authority for implementation, verification, privacy boundaries,
and delivery status. `docs/real-data-iteration-status.md` is the routed detailed evidence and round log;
it does not define a competing current state. Update this file whenever the active candidate, registered
verification, protected boundaries, or known blockers materially change.

## Goal

Deliver a local-first manga localization workbench that runs as an Apple application: import external
image data, process it on the Mac, and use the same workbench from an iPhone when explicitly allowed.
The Unreleased iteration extends the verified 0.2.0 foundation without placing private inputs, OCR
text, models, databases, or generated artwork in the public candidate. The user verifies in the
workbench/app panel; do not stop for reversible packaging review.

## Current round and candidate

Real-page computer-use is on the 130-page manga01 full book.
RP-1–RP-9 stay cleared (RP-8 blank translator overwrite; RP-9
reopened batch drawer leftover whole-book scope). Independently
rechecked CI `32128671541` (`6e979e0`, frontend/backend/e2e success)
and `32127805767` (`35e3017`, success). Four text pages finished
confirm → translate → inpaint → typeset → accept → JSON export.
The user paused the live loop after this round. Do not re-arm
`AGENT_LOOP_WAKE_manga_realpages` until they resume. Product
Round 8 is not complete.

Unified visual check of both private books is still outstanding. Do not
auto-accept empty pages. Product Round 8 is not complete. Do not re-arm
`AGENT_LOOP_WAKE_manga_desktop`, `AGENT_LOOP_WAKE_manga_realdata`, or
`AGENT_LOOP_WAKE_manga_app`.

## Active loop prompt

Live prompt: `.agent/REALPAGES_LOOP_PROMPT.md`.
Sentinel: `AGENT_LOOP_WAKE_manga_realpages`.
`.agent/UI_LOOP_PROMPT.md`, `.agent/DESKTOP_LOOP_PROMPT.md`, and
`.agent/REALDATA_LOOP_PROMPT.md` are superseded. Do not re-arm
`AGENT_LOOP_WAKE_manga_ui`, `AGENT_LOOP_WAKE_manga_desktop`,
`AGENT_LOOP_WAKE_manga_realdata`, or `AGENT_LOOP_WAKE_manga_app`.
Do not arm a 25-minute fallback sleeper. Late wakes for the old sentinels
skip rewrite and do not resume those loops.

## Automation closeout

On 2026-08-18 the user stopped every project worker and loop. The
loopback API on `:8000` was killed. `AGENT_LOOP_WAKE_manga_ui` and the
older desktop/app/realdata sentinels must not be re-armed. The 2026-08-17
packaging/app loop, the real-data process/fix loop, and the synthetic UI
loop stay superseded. Late wakes for those sentinels skip rewrite.

## Environment evidence

- macOS on Apple Silicon (M4, Metal available), Node.js 26, npm 11, uv, and CPython 3.12.
- Tesseract 5.5 is installed with `jpn`, `jpn_vert`, `chi_sim`, and `chi_tra` data.
- OpenCV/Pillow are the dependency-light image baseline; ONNX Runtime is available through the optional
  `ai` extra for LaMa and Real-ESRGAN ONNX. CTranslate2 and SentencePiece are available through the
  optional `mt` extra for local Argos translation.
- The private PP-OCRv3, LaMa, and Real-ESRGAN anime ONNX weights are checksum-verified and live only in
  ignored local model directories. Real-ESRGAN NCNN remains a CLI adapter; no NCNN executable was run
  here.

## Decisions

- The shipping form is a local Mac application, not a browser tab. `npm run
  package:app` builds `Manga Localizer.app` with checksum-verified bundled
  models. `npm run app` remains the source-tree prototype.
- Repository/distribution name: `manga-localizer`; Python import package: `manga_localizer`.
- Frontend: React, TypeScript, Vite, Zustand, React Konva, and dense custom CSS tokens.
- Backend: FastAPI, Pydantic, SQLAlchemy/SQLite, Pillow, OpenCV, and background asyncio workers.
- Preprocessing has one provider/result/coordinate contract. `opencv-pillow` is always available and
  uses classic Lanczos (`aiUpscale: false`). `realesrgan-onnx` is the runnable local AI upscaler:
  explicit checksum/license install, no startup download, native 4×, 2×/3× downscale from that AI
  result, tile size 256 on this 16 GB M4, and grayscale preservation. `realesrgan-ncnn` remains optional
  and is never downloaded at ordinary application startup. Application packaging may download
  checksum-verified weights and copy them into the `.app` bundle; git still must not store those
  files. Each imported page stores a local profile suggestion
  from size plus a native-resolution contrast/sharpness sample. The editor may apply that hint to the
  current page or adopt it as the project default; it is never an automatic book-wide setting.
- Detection and recognition are separate selections. Tesseract remains the zero-model detector/OCR
  baseline; optional PP-OCRv3 supplies bounded detector polygons. `ppocr-v3+tesseract` merges
  overlapping, contained, and nearby aligned proposals from both detectors, then pads the box so
  glyphs are enclosed. It does not drop low-confidence text or grant trust. Re-running detection
  replaces stale empty unconfirmed auto boxes and oversized low-confidence unconfirmed auto
  leftovers (even when OCR filled garbage text), then skips duplicates of kept regions.
  Confirmed, ignored, translated, and ordinary-sized OCR boxes stay. A completed
  zero-detection result is authoritative and is not silently replaced during OCR.
- Low-resolution pages can be manually AI-redrawn with the local Real-ESRGAN anime 4× preprocessor.
  The workbench button never runs this automatically and does not change the project default.
- Annotated detection/OCR evaluation is path-parameterized. Public reports store only anonymous page
  IDs and aggregate precision, recall, CER, and negative-page false positives. Transcriptions, image
  names, checksums, and absolute paths stay out of sanitized output. Private draft JSON remains under
  `tests/real-data/` until a human marks it reviewed.
- Low/empty OCR on a preprocessed crop is retried against the immutable original crop, with the selected
  input and attempt count persisted as provenance.
- Detector confidence, OCR confidence, every OCR attempt across reruns, and the selected input are
  stored separately.
  Automatic proposals always remain `review`; only explicit human confirmation creates `trusted`, and
  confidence alone never authorizes translation or default safe rendering. Recognition-input edits or
  replacement of depended-on preprocessing revoke trust; translation/style/mask-only edits preserve it.
- Translation providers are exact registry selections. Manual, mock, and dictionary remain local
  baselines. `argos-ja-zh` is the optional local neural translator (Argos CTranslate2, English pivot,
  Simplified Chinese). OpenAI-compatible remains the only path that can send trusted text remotely, and
  only after the user selects it and supplies a session credential.
- Inpainting uses exact provider routing. OpenCV is the guaranteed fallback; optional LaMa ONNX is lazy,
  local, context-cropped, and composites with exact mask-outside preservation. Grayscale manga pages
  keep chroma suppressed after RGB LaMa inference. Each nonempty repair also stores comparison
  candidates (provider, Navier-Stokes, Telea, line-art-guided); LaMa-only pages default to the
  line-art-guided plate. Switching a candidate replaces the canonical inpainted bytes and clears
  dependent reviews.
- Repair defaults to the `safe` eligibility policy. Canonical repair settings are persisted across API,
  queue, and UI; text/full-region masks support padding, dilation, feathering, editable geometry, and an
  actual-mask preview. Bounded add/erase strokes are persisted per region. Typesetting requires safe
  eligibility and intersection with the generated mask, and cannot reuse an inpaint cache made under a
  different repair policy. Completed typesetting persists overflowing region IDs as review hints; they
  are cleared when typesetting is invalidated and are not an export hard gate.
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
- [x] Round 12: privacy-safe detection/OCR evaluation, public synthetic ground truth, union detector
  that keeps all proposals, ignored private draft annotations, public regression, and complete CI.
- [x] Round 13: line-art-aware inpainting candidates, LaMa grayscale preservation, local
  compare/select/accept, public synthetic comparison script, public regression, and complete CI.
- [x] Round 14: local Argos Japanese-to-Chinese translation, checksummed packages, public synthetic
  comparison script, public regression, and complete CI.
- [x] Round 15: privacy-safe detector-draft accept/reject promotion, public regression, and complete CI.
- [x] Round 16: persisted typesetting overflow review, unreviewed-page keyboard skip, public regression,
  and complete CI.
- [x] Round 17: per-page preprocessing profile suggestions, apply-to-page and adopt-as-default actions,
  public regression, and complete CI.
- [x] Round 18: vertical CJK punctuation presentation forms and hanging comma/period glyphs, with
  public regression, and complete CI.
- [x] Round 19: adjacent small-box fragment clustering for typesetting, with public regression,
  and complete CI.
- [x] Round 20: overflow-only typesetting and overflow-box selection in the inspector, with public
  regression, and complete CI.
- [x] Round 21: per-region typeset rerun from the typesetting inspector, with public regression,
  and complete CI.
- [x] Round 22: worker overlay of selected typeset region IDs, keeping untouched boxes and overflow
  IDs, with public regression, and complete CI.
- [x] Round 23: full-page typeset fallback when the overlay plate is missing, with public regression,
  and complete CI.
- [x] Round 24: job-queue overlay vs full-page typeset summary, with public regression,
  and complete CI.
- [x] Round 25: T / Shift+T shortcuts for selected-box and overflow-only typesetting, with public
  regression, and complete CI.
- [x] Round 26: switch the canvas to the typeset preview when the current page's typeset job
  completes, with public regression, and complete CI.
- [x] Round 27: select overflowing boxes after the current page's typeset job completes, with public
  regression, and complete CI.
- [x] Round 28: switch to the erased preview and review mask when the current page's inpaint job
  completes, with public regression, and complete CI.
- [x] Round 29: switch to the enhanced preview when the current page's preprocess job completes, with
  public regression, and complete CI.
- [x] Round 30: open original-vs-result compare when a visual-stage job for the current page completes,
  with public regression, and complete CI.
- [x] Round 31: forbid HTTP caching of generated preview images, with public regression, and complete CI.
- [x] Round 32: keep overlay boxes selected when a partial typeset job for the current page completes,
  with public regression, and complete CI.
- [x] Round 33: frame selected typeset boxes in the canvas after the current page's typeset job
  completes, with public regression, and complete CI.
- [x] Round 34: frame overflow boxes from the inspector overflow actions, with public regression,
  and complete CI.
- [x] Round 35: jump to overflowing pages and frame their overflow boxes, with public regression,
  and complete CI.
- [x] Round 36: open a job-queue item onto its page and frame overlay or leftover overflow boxes,
  with public regression, and complete CI.
- [x] Round 37: frame overflow boxes from the sidebar overflow pill, with public regression,
  and complete CI.
- [x] Round 38: keep adjacent image navigation on the visible sidebar list, with public regression,
  and complete CI.
- [x] Round 39: frame the selected box from Alt+arrows and the inspector region list, with public
  regression, and complete CI.
- [x] Round 40: frame the current selection from G and the canvas toolbar, with public regression,
  and complete CI.
- [x] Round 41: show visible-list page position and disable adjacent navigation at the ends, with
  public regression, and complete CI.
- [x] Round 42: open a failed queue item onto the matching inspector, with public regression,
  and complete CI.
- [x] Round 43: show the current page processing failure in the inspector with a same-page retry,
  with public regression, and complete CI.
- [x] Round 44: open the matching inspector from failed-filter adjacent navigation, with public
  regression, and complete CI.
- [x] Round 45: open the matching inspector when clicking a failed sidebar page, with public
  regression, and complete CI.
- [x] Round 46: jump to failed or unavailable pages with option-arrow and open the matching
  inspector, with public regression, and complete CI.
- [x] Round 47: retry a page processing failure from the inspector without opening the batch drawer,
  with public regression, and complete CI.
- [x] Round 48: replace a stale inspector failure with a queued or running notice after retry, with
  public regression, and complete CI.
- [x] Round 49: keep the current page in a status filter after retry until you leave it, with public
  regression, and complete CI.
- [x] Round 50: open the batch queue from the queued inspector notice after retry, with public
  regression, and complete CI.
- [x] Round 51: reveal the matching queue job from inspector notices, with public
  regression, and complete CI.
- [x] Round 52: open a Mac application window and a phone-sized workbench shell, with public
  regression, and complete CI.
- [x] Round 53: explicit same-LAN iPhone companion for photo import and Mac-side processing, with
  public regression, and complete CI.
- [x] Round 54: keep the iPhone workbench out of the safe area, with public regression, and complete
  CI.
- [x] Round 55: copy the same-LAN companion URL from the Mac window, with public regression, and
  complete CI.
- [x] Round 56: open batch processing from the compact phone panes, with public regression, and
  complete CI.
- [x] Round 57: iPhone home-screen icon for the same-LAN companion, with public regression, and
  complete CI.
- [x] Round 58: first-run create-project action on the empty sidebar, with public regression, and
  complete CI.
- [x] Round 59: first-run create-project actions on the empty canvas and inspector, with public
  regression, and complete CI.
- [x] Round 60: photo-library import from the empty canvas and inspector, with public regression.
  Remote e2e failed on run `31997919460`.
- [x] Round 61: keep inspector project settings available before import, with public regression, and
  complete CI.
- [x] Desktop app foundation: double-clickable local `Manga Localizer.app`,
  package-time checksummed PP-OCR/LaMa/Real-ESRGAN/Argos copy, and launch-time
  bundle selection with honest unavailable health.
- [x] Combined process/fix loop: 199/199 pages processed on the current pipeline; one
  public evaluator export-gate fix; no further software defect on the full pass.
- [ ] NEEDS_USER unified visual check of manga01+manga02, including empty-recognized
  pages and remaining manga01 clean-plate review. Product Round 8 is not complete.
- [ ] Next real-data checkpoint: remaining 112/130 visual reviews; local human use of the draft-review
  CLI to promote private detector-draft JSON into independent ground truth.

## Verification evidence

- 2026-08-18 docs CI: GitHub Actions run `32128671541` succeeded at
  `6e979e093d2029e2cf93880c50a284d7805ffcb2` (frontend, backend, e2e).
  A fourth text page (1073×482) finished detect/OCR → confirm →
  translate → inpaint → typeset → accept → JSON export. The user
  paused the live loop after this round. Private trees were not
  committed.
- 2026-08-18 RP-8/RP-9 CI: GitHub Actions run `32127805767` succeeded
  at `35e3017e1602a6f66af1eb75857050bc36c17582` (frontend, backend,
  e2e). Live remount of **批处理与导出** still defaults to **当前页**
  and detect+OCR. Private trees were not committed.
- 2026-08-18 real-page pass: operator finished one 340×594 text page
  through accept + JSON export, then confirmed a second OCR-done page
  and queued translate/inpaint/typeset. RP-8/RP-9 fixes are in the
  public tree; targeted pytest and the drawer-reset Vitest passed.
  Docs CI `32120142568` (`c5ebb68`) was independently rechecked
  success. Private trees were not committed.
- 2026-08-18 real-page pass 2 CI: GitHub Actions run `32118174494`
  succeeded at `05ce7e435c6c4d0f4e94c7a03fcf6207900e3b64` (frontend,
  backend, e2e).
- 2026-08-18 real-page pass 2 (130-page manga01 full book, viewport
  1100×800, workbench origin without a test query): reopened the full
  book from the catalog, skipped a 0-box first page, and found RP-6
  (batch **加入队列** defaulted to leftover checkbox selection). After
  choosing **当前页**, detect+OCR completed 1/1 with 4 boxes. RP-1/RP-2
  layout and RP-3/RP-5 provider checks held. Local frontend lint,
  typecheck, 170 Vitest cases, and production build passed. Private
  trees were not committed.
- 2026-08-18 real-page pass 1 (30-page catalog slice, viewport 1100×800):
  reopened from **切换项目**, PP-OCRv3 detect+OCR on one selected page
  completed 1/1 with 3 balloon boxes. Switching the project detector
  had already wiped the other 29 pages. **按状态筛选** was `display:none`
  below 1250px; **上一张图** sat at x=-9. Findings: RP-1–RP-5 in
  `.agent/REALPAGES_PROBLEM_REPORT.md`. Local public gates after the
  RP-1–RP-4 fix: frontend lint/typecheck/168 Vitest/build; backend Ruff
  and 249 pytest. Private trees were not committed.
- 2026-08-18 user stop: killed the loopback API and all loop watchers;
  replaced the live prompt with `.agent/REALPAGES_LOOP_PROMPT.md`
  (`AGENT_LOOP_WAKE_manga_realpages`). Real-page function + layout
  evaluation has not started.
- 2026-08-18 UI-3 computer-use recheck: Tesseract detect+OCR on a fresh
  public synthetic page left two panel-sized unconfirmed boxes; PP-OCR
  re-detect replaced them with three small balloon boxes. `npm run
  check:backend` passed (Ruff + 248 pytest). Private trees were not
  committed.
- 2026-08-18 UI-1/UI-2 computer-use recheck on refreshed loopback workbench:
  viewport 1100px showed **复核蒙版**; first-accept preprocess after
  typeset/inpaint kept all three `stageReviews` accepted. Independent CI
  recheck: GitHub Actions run `32101553739` at `30d27a5` succeeded.
- 2026-08-18 UI-1/UI-2 public fix: `npm run check` passed (9 launcher tests;
  backend Ruff lint/format and 245 pytest cases; frontend lint, typecheck,
  166 Vitest cases, and production build). Private trees were not committed.
- 2026-08-18 UI loop first computer-use pass on the live workbench: created a
  public synthetic project, imported the public generator page, ran
  preprocess/detect/OCR, switched to bundled PP-OCR/LaMa, confirmed boxes
  (ignored one leftover Tesseract panel box), manual translate, LaMa inpaint,
  typeset, visual accept, and safe export of translated image + JSON. Export
  was blocked until inpaint/typeset were re-accepted after a late preprocess
  accept. Did not auto-accept empty pages. Private books were not used as
  the evaluation corpus.
- 2026-08-18 UI-loop prompt CI: GitHub Actions run `32089117642` passed at
  `746480a80d69556378c66c8ce9df1a6ceb6d1319` (frontend, backend, e2e).
- 2026-08-17 desktop app package: launcher 9/9 Node tests passed; backend Ruff
  lint/format and 244 pytest cases passed, including checksum-mismatch
  unavailable health and verified bundle copy. `npm run package:app` wrote
  `dist/macos/Manga Localizer.app` with a native window helper; wrapper
  `--no-window` reached `/api/health`; bundled PP-OCR, LaMa, Real-ESRGAN
  ONNX, Argos ja→zh, and Tesseract were available. Private real-data trees
  and model weights were not committed.
- 2026-08-17 review-canvas/detection delivery: frontend lint, typecheck, 164 Vitest
  cases, and production build passed; backend Ruff lint/format and full pytest passed;
  launcher 5/5 passed. Private real-data trees were not committed.
- Combined process/fix loop: public evaluator export-gate tests passed locally
  (`backend/tests/test_evaluate_real_data.py`). Backend Ruff lint/format and 239 pytest
  cases passed. After that fix, private slices covering all 199 pages completed JSON
  export with 0 import/processing failures, 0 source-checksum failures, and 0
  mask-outside pixel changes. 30 empty-recognized pages were left for a human.
  Generated-image export without review still fails, as required. The loop is
  stopped for the user's unified visual check.
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
- Round 12 local synthetic ground-truth evaluation (7 generated pages, IoU 0.5, Tesseract OCR):
  PP-OCRv3 precision 1.0, recall 1.0, F1 1.0, 0 false positives on the no-text hatch page, matched
  transcription coverage 6/6, CER 0.421. Tesseract-alone precision 0.008, recall 0.333, 80 false
  positives on the negative page. Union recall 1.0 with precision 0.023 because it retains Tesseract
  proposals. Private ignored drafts: 130 pages, 727 PP-OCR boxes, 18 empty pages; 3 representative
  pages also have OCR drafts. Those private files are not independent ground truth.
- Round 12 local verification passed 2 launcher tests; backend Ruff lint/format and 203 pytest cases;
  frontend ESLint, TypeScript, 92 Vitest cases, and production build; release audit over 119 candidate
  files plus 334 historical blobs; `uv lock --check`; compileall; and `git diff --check`.
- Round 12 authoritative remote verification: GitHub CI run `31852816928` passed at
  `761c30d319455f11af82fc2358bc830797ebdac8`. Backend Ruff lint/format, 203 pytest cases, and the
  release audit passed. Frontend lint/typecheck/92 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 13 local synthetic inpaint comparison (one generated line-art page, local LaMa ONNX available):
  four candidates, zero mask-outside pixel changes, chroma 0, no automatic smear/chroma flags. LaMa
  primary inside-mask Laplacian variance 19256; line-art-guided 11020; Navier-Stokes 34; Telea 28.
  Contact sheets remain under the ignored real-data run directory and were not opened by a remote model.
- Round 13 local verification passed 2 launcher tests; backend Ruff lint/format and 209 pytest cases;
  frontend ESLint, TypeScript, 95 Vitest cases, and production build; release audit over 123 candidate
  files plus 357 historical blobs; `uv lock --check`; compileall; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 13 authoritative remote verification: GitHub CI run `31854780188` passed at
  `751d3a985bf9e320f2bf11b1f2c2c6681b620e45`. Backend Ruff lint/format, 209 pytest cases, and the
  release audit passed. Frontend lint/typecheck/95 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 14 local verification passed 2 launcher tests; backend Ruff lint/format and 214 pytest cases;
  frontend ESLint, TypeScript, 95 Vitest cases, and production build; release audit over 126 candidate
  files plus 390 historical blobs; `uv lock --check`; compileall; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 14 authoritative remote verification: GitHub CI run `31856326624` passed at
  `a0bd72cc03b1d29b33a5a92ada2b82613f28d581`. Backend Ruff lint/format, 214 pytest cases, and the
  release audit passed. Frontend lint/typecheck/95 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 15 local verification passed 2 launcher tests; backend Ruff lint/format and 218 pytest cases;
  frontend ESLint, TypeScript, 95 Vitest cases, and production build; release audit over 128 candidate
  files plus 420 historical blobs; `uv lock --check`; compileall; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 15 authoritative remote verification: GitHub CI run `31858177141` passed at
  `8d50361ac4cf8b5f296fd480e2c2c7bd1efe2219`. Backend Ruff lint/format, 218 pytest cases, and the
  release audit passed. Frontend lint/typecheck/95 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 16 local verification passed 2 launcher tests; backend Ruff lint/format and 219 pytest cases;
  frontend ESLint, TypeScript, 97 Vitest cases, and production build; release audit over 128 candidate
  files plus 435 historical blobs; `uv lock --check`; compileall; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 16 authoritative remote verification: GitHub CI run `31860160644` passed at
  `20b3b1e9236b866dd4cdf07aa9b6d865b03f3d2b`. Backend Ruff lint/format, 219 pytest cases, and the
  release audit passed. Frontend lint/typecheck/97 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 17 local verification passed 2 launcher tests; backend Ruff lint/format and 221 pytest cases;
  frontend ESLint, TypeScript, 99 Vitest cases, and production build; release audit over 128 candidate
  files plus 467 historical blobs; `uv lock --check --project backend`; compileall; and `git diff --check`.
  Playwright discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234,
  so live browser evidence remains the GitHub e2e job after push.
- Round 17 authoritative remote verification: GitHub CI run `31861476315` passed at
  `302837fa3403e79a2eb51ab5274ecc85eb56741e`. Backend Ruff lint/format, 221 pytest cases, and the
  release audit passed. Frontend lint/typecheck/99 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 18 local verification passed 2 launcher tests; backend Ruff lint/format and 222 pytest cases;
  release audit over 128 candidate files plus 492 historical blobs; `uv lock --check --project backend`;
  compileall; and `git diff --check`. Frontend was unchanged from Round 17 (99 Vitest). Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 18 authoritative remote verification: GitHub CI run `31874726926` passed at
  `41545b8e453aaebab9325ab253f9754168712acc`. Backend Ruff lint/format, 222 pytest cases, and the
  release audit passed. Frontend lint/typecheck/99 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 19 local verification passed 2 launcher tests; backend Ruff lint/format and 224 pytest cases;
  release audit over 128 candidate files plus 505 historical blobs; `uv lock --check --project backend`;
  compileall; and `git diff --check`. Frontend was unchanged from Round 17 (99 Vitest). Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 19 authoritative remote verification: GitHub CI run `31875271369` passed at
  `7dfccd324d29ab7c33055c70d7140e318c2b7cc7`. Backend Ruff lint/format, 224 pytest cases, and the
  release audit passed. Frontend lint/typecheck/99 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 20 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 101 Vitest cases,
  and the production build; release audit over 128 candidate files plus 518 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 19 (224 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser evidence
  remains the GitHub e2e job after push.
- Round 20 authoritative remote verification: GitHub CI run `31876251138` passed at
  `d02e873fd3860290ebf15bbb98586079ab40b1be`. Backend Ruff lint/format, 224 pytest cases, and the
  release audit passed. Frontend lint/typecheck/101 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 21 local verification passed frontend ESLint, TypeScript, 102 Vitest cases, and the production
  build; release audit over 128 candidate files plus 533 historical blobs; and `git diff --check`.
  Backend was unchanged from Round 19 (224 pytest). Playwright discovers both Chromium journeys; this
  environment lacks Playwright Chromium revision 1234, so live browser evidence remains the GitHub e2e
  job after push.
- Round 21 authoritative remote verification: GitHub CI run `31876680453` passed at
  `59a821b7707f19b8a8d2109c150b8e941981c895`. Backend Ruff lint/format, 224 pytest cases, and the
  release audit passed. Frontend lint/typecheck/102 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 22 local verification passed backend Ruff lint/format and 229 pytest cases, plus the release
  audit over 128 candidate files and 544 historical blobs. Frontend was unchanged from Round 21.
  Playwright discovers both Chromium journeys; this environment lacks Playwright Chromium revision
  1234, so live browser evidence remains the GitHub e2e job after push.
- Round 22 authoritative remote verification: GitHub CI run `31878242652` passed at
  `df15d7c6d0ae86d1189b3a3de081a1777046b739`. Backend Ruff lint/format, 229 pytest cases, and the
  release audit passed. Frontend lint/typecheck/102 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 23 local verification passed backend Ruff lint/format and 230 pytest cases, plus the release
  audit over 128 candidate files and 563 historical blobs. Frontend was unchanged from Round 22.
  Playwright discovers both Chromium journeys; this environment lacks Playwright Chromium revision
  1234, so live browser evidence remains the GitHub e2e job after push.
- Round 23 authoritative remote verification: GitHub CI run `31878760451` passed at
  `c8fb20beca736452f702121ad64b7a16ac52b1c3`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/102 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 24 local verification passed frontend ESLint, TypeScript, 104 Vitest cases, and the production
  build; release audit over 128 candidate files plus 576 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 24 authoritative remote verification: GitHub CI run `31879071282` passed at
  `5e8545bd7e747b22b0cb989ce4a5a0221ed598a1`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/104 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 25 local verification passed frontend ESLint, TypeScript, 106 Vitest cases, and the production
  build; release audit over 128 candidate files plus 587 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 25 authoritative remote verification: GitHub CI run `31879412533` passed at
  `d674775ba742aa0103669ce9f1f912b856737728`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/106 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 26 local verification passed frontend ESLint, TypeScript, 110 Vitest cases, and the production
  build; release audit over 128 candidate files plus 599 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 26 authoritative remote verification: GitHub CI run `31879945945` passed at
  `906c898bd664a9a2ffdc33d5ef3bb1a783c84e0c`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/110 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 27 local verification passed frontend ESLint, TypeScript, 111 Vitest cases, and the production
  build; release audit over 128 candidate files plus 609 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 27 authoritative remote verification: GitHub CI run `31880310109` passed at
  `ebdaae7e14c5a7359faf14ac546549250a985960`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/111 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 28 local verification passed frontend ESLint, TypeScript, 113 Vitest cases, and the production
  build; release audit over 128 candidate files plus 619 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 28 authoritative remote verification: GitHub CI run `31880607541` passed at
  `48c52e1a4c24ceb9051cd3a9354e325d7ded7cb2`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/113 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 29 local verification passed frontend ESLint, TypeScript, 115 Vitest cases, and the production
  build; release audit over 128 candidate files plus 629 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 29 authoritative remote verification: GitHub CI run `31880896973` passed at
  `e97fe14ba1492ee85fdea884e73aab10a9753470`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/115 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 30 local verification passed frontend ESLint, TypeScript, 115 Vitest cases, and the production
  build; release audit over 128 candidate files plus 640 historical blobs. Backend was unchanged from
  Round 23 (230 pytest). Playwright discovers both Chromium journeys; this environment lacks Playwright
  Chromium revision 1234, so live browser evidence remains the GitHub e2e job after push.
- Round 30 authoritative remote verification: GitHub CI run `31882096845` passed at
  `ca7bc89134a1f98a8f7536cad7539d18136bf6b0`. Backend Ruff lint/format, 230 pytest cases, and the
  release audit passed. Frontend lint/typecheck/115 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 31 local verification passed 2 launcher tests; backend Ruff lint/format and 231 pytest cases;
  frontend ESLint, TypeScript, 116 Vitest cases, and the production build; release audit over 128
  candidate files plus 650 historical blobs; and `git diff --check`. Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 31 authoritative remote verification: GitHub CI run `31882562724` passed at
  `656e3650b1fc45fc9c68febd3fcc6bc077854f55`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/116 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 32 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 118 Vitest cases,
  and the production build; release audit over 128 candidate files plus 663 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 32 authoritative remote verification: GitHub CI run `31883446023` passed at
  `b28ca6b25c7d3b33ff47db9a9f74ed90ed2b663c`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/118 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 33 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 119 Vitest cases,
  and the production build; release audit over 128 candidate files plus 673 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 33 authoritative remote verification: GitHub CI run `31883910085` passed at
  `e41261ab2e37aa974cde07b0d79aba9d7a22ae9b`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/119 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 34 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 120 Vitest cases,
  and the production build; release audit over 128 candidate files plus 686 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 34 authoritative remote verification: GitHub CI run `31884339883` passed at
  `b637b97d9a56a8ec73170adb6abb0c3a2811eb46`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/120 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 35 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 121 Vitest cases,
  and the production build; release audit over 128 candidate files plus 698 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 35 authoritative remote verification: GitHub CI run `31884703654` passed at
  `9005872fd41028d4c1f6eab81d9e80b8c25e267d`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/121 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 36 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 125 Vitest cases,
  and the production build; release audit over 128 candidate files plus 712 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 36 authoritative remote verification: GitHub CI run `31885226463` passed at
  `1184c07e1cabeb8257fe60601584910536d4ef2a`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/125 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 37 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 127 Vitest cases,
  and the production build; release audit over 128 candidate files plus 724 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 37 authoritative remote verification: GitHub CI run `31885552346` passed at
  `ee182935b4916bd810ca38fd5b48b738e7e9258b`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/127 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 38 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 129 Vitest cases,
  and the production build; release audit over 128 candidate files plus 735 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 38 authoritative remote verification: GitHub CI run `31885919299` passed at
  `35e6293e0e5d242aaad5cad55530f4f080262626`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/129 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 39 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 130 Vitest cases,
  and the production build; release audit over 128 candidate files plus 747 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 39 authoritative remote verification: GitHub CI run `31886262454` passed at
  `d8e7c05467ebf9359f61defd534c526c9e02fc21`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/130 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 40 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 132 Vitest cases,
  and the production build; release audit over 128 candidate files plus 758 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 40 authoritative remote verification: GitHub CI run `31886581607` passed at
  `520133a74f231a5464400e78ade7c8cf1b522dca`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/132 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 41 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 133 Vitest cases,
  and the production build; release audit over 128 candidate files plus 771 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 41 authoritative remote verification: GitHub CI run `31886984640` passed at
  `a1031f1604a0cb8372fe130ac64b380a251df0a7`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/133 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 42 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 135 Vitest cases,
  and the production build; release audit over 128 candidate files plus 782 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 42 authoritative remote verification: GitHub CI run `31888156798` passed at
  `ce2395e71f2029dc09c98d937a6ce7901672c1ae`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/135 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 43 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 137 Vitest cases,
  and the production build; release audit over 128 candidate files plus 792 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 43 authoritative remote verification: GitHub CI run `31888824260` passed at
  `4c7511695ba896bfe5834620b862ac669b4e74d9`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/137 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 44 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 139 Vitest cases,
  and the production build; release audit over 128 candidate files plus 806 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 44 authoritative remote verification: GitHub CI run `31889225201` passed at
  `17e21f25e73a76d6b421993b60476218c92557b7`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/139 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 45 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 141 Vitest cases,
  and the production build; release audit over 128 candidate files plus 817 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 45 authoritative remote verification: GitHub CI run `31889559133` passed at
  `fc58b181d3a647fb7b9feb4c89341fd1f820966f`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/141 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 46 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 143 Vitest cases,
  and the production build; release audit over 128 candidate files plus 827 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 46 authoritative remote verification: GitHub CI run `31923761102` passed at
  `3ffa0f79e017989bba11d56678b9a7ed2a4b2e55`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/143 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 47 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 143 Vitest cases,
  and the production build; release audit over 128 candidate files plus 840 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 47 authoritative remote verification: GitHub CI run `31924113820` passed at
  `322f0229a54a3bbd74bfc069180618649fa1a466`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/143 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 48 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 144 Vitest cases,
  and the production build; release audit over 128 candidate files plus 849 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 48 authoritative remote verification: GitHub CI run `31924614192` passed at
  `47ceeaf7aa475e3064921de144b21c7dc54010cc`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/144 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 49 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 146 Vitest cases,
  and the production build; release audit over 128 candidate files plus 860 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 49 authoritative remote verification: GitHub CI run `31924960171` passed at
  `118f351cece0dbdc5a54b6b6afc3c1a1689f2d6f`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/146 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 50 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 146 Vitest cases,
  and the production build; release audit over 128 candidate files plus 871 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 50 authoritative remote verification: GitHub CI run `31926599050` passed at
  `37d96590f80e7a01e1ca43884643176097184fac`. Backend Ruff lint/format, 231 pytest cases, and the
  release audit passed. Frontend lint/typecheck/146 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 51 local verification passed 2 launcher tests; frontend ESLint, TypeScript, 148 Vitest cases,
  and the production build; release audit over 128 candidate files plus 877 historical blobs; and
  `git diff --check`. Backend was unchanged from Round 31 (231 pytest). Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 51 authoritative remote verification: GitHub CI run `31931826305` passed at
  `0a4b6fcb1b8bab194468c89ecab07c70cdb749fd` on `main`. Backend Ruff lint/format, 231 pytest cases, and
  the release audit passed. Frontend lint/typecheck/148 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 52 local verification passed 4 launcher tests; backend Ruff lint/format and 233 pytest cases;
  frontend ESLint, TypeScript, 149 Vitest cases, and the production build; release audit over 133
  candidate files plus 887 historical blobs; and `git diff --check`. Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 52 authoritative remote verification: GitHub CI run `31933691451` passed at
  `6feb7bb2bc25bc5af314aae5188847a9d335baa0` on `main`. Backend Ruff lint/format, 233 pytest cases, and
  the release audit passed. Frontend lint/typecheck/149 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 53 local verification passed 5 launcher tests; backend Ruff lint/format and 235 pytest cases;
  frontend ESLint, TypeScript, 150 Vitest cases, and the production build; release audit over 133
  candidate files plus 909 historical blobs; and `git diff --check`. Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 53 authoritative remote verification: GitHub CI run `31934325707` passed at
  `0a23eae3253724af0cc0c717280a4833bcd3590c` on `main`. Backend Ruff lint/format, 235 pytest cases, and
  the release audit passed. Frontend lint/typecheck/150 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 54 local verification passed 5 launcher tests; backend Ruff lint/format and 235 pytest cases
  (unchanged from Round 53); frontend ESLint, TypeScript, 150 Vitest cases, and the production build;
  release audit over 133 candidate files plus 932 historical blobs; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 54 authoritative remote verification: GitHub CI run `31934721559` passed at
  `d2244becf2996a0ec7a5ba4d6a7184e6c3429ab5` on `main`. Backend Ruff lint/format, 235 pytest cases, and
  the release audit passed. Frontend lint/typecheck/150 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 55 local verification passed 5 launcher tests; backend Ruff lint/format and 235 pytest cases
  (unchanged from Round 53); frontend ESLint, TypeScript, 150 Vitest cases, and the production build;
  release audit over 133 candidate files plus 936 historical blobs; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 55 authoritative remote verification: GitHub CI run `31935090410` passed at
  `7b5dcaf5f0f22815366aebbb6a5b8aeb95b61efd` on `main`. Backend Ruff lint/format, 235 pytest cases, and
  the release audit passed. Frontend lint/typecheck/150 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 56 local verification passed 5 launcher tests; backend Ruff lint/format and 235 pytest cases
  (unchanged from Round 53); frontend ESLint, TypeScript, 151 Vitest cases, and the production build;
  release audit over 133 candidate files plus 944 historical blobs; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 56 authoritative remote verification: GitHub CI run `31935428949` passed at
  `6768ebad4f9d66081fed63cd58641a679907953f` on `main`. Backend Ruff lint/format, 235 pytest cases, and
  the release audit passed. Frontend lint/typecheck/151 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 57 local verification passed 5 launcher tests; backend Ruff lint/format and 236 pytest cases;
  frontend ESLint, TypeScript, 151 Vitest cases, and the production build; release audit over 136
  candidate files plus 952 historical blobs; and `git diff --check`. Playwright discovers both
  Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live browser
  evidence remains the GitHub e2e job after push.
- Round 57 authoritative remote verification: GitHub CI run `31935807229` passed at
  `1214c700135cbedcf58184fe483099269cd87458` on `main`. Backend Ruff lint/format, 236 pytest cases, and
  the release audit passed. Frontend lint/typecheck/151 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 58 local verification passed 5 launcher tests; backend Ruff lint/format and 236 pytest cases
  (unchanged from Round 57); frontend ESLint, TypeScript, 152 Vitest cases, and the production build;
  release audit over 136 candidate files plus 963 historical blobs; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 58 authoritative remote verification: GitHub CI run `31936106130` passed at
  `c625ae7c16543945204ac5c1edbc93f4f088c856` on `main`. Backend Ruff lint/format, 236 pytest cases, and
  the release audit passed. Frontend lint/typecheck/152 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 59 local verification passed 5 launcher tests; backend Ruff lint/format and 236 pytest cases
  (unchanged from Round 57); frontend ESLint, TypeScript, 154 Vitest cases, and the production build;
  release audit over 136 candidate files plus 970 historical blobs; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 59 authoritative remote verification: GitHub CI run `31994257863` passed at
  `9982fa7c441b95514a807066da0d3694cce976bc` on `main`. Backend Ruff lint/format, 236 pytest cases, and
  the release audit passed. Frontend lint/typecheck/154 tests/build passed. Both Playwright Chromium
  journeys passed.
- Round 60 local verification passed 5 launcher tests; backend Ruff lint/format and 236 pytest cases
  (unchanged from Round 57); frontend ESLint, TypeScript, 156 Vitest cases, and the production build;
  release audit over 136 candidate files plus 979 historical blobs; and `git diff --check`. Playwright
  discovers both Chromium journeys; this environment lacks Playwright Chromium revision 1234, so live
  browser evidence remains the GitHub e2e job after push.
- Round 60 remote verification: GitHub CI run `31997919460` failed at
  `e7a7327ba7085db2d9b105c9692c388a10b6d9a1` on `main`. Backend and frontend jobs passed; e2e timed out
  waiting for the inspector translator combobox because the empty-library inspector hid project
  settings.
- Round 61 local verification passed 5 launcher tests; frontend ESLint, TypeScript, 157 Vitest cases,
  and the production build; release audit over 136 candidate files plus 988 historical blobs; and
  `git diff --check`. Backend files were unchanged from Round 60. Playwright discovers both Chromium
  journeys; this environment lacks Playwright Chromium revision 1234, so live browser evidence remains
  the GitHub e2e job after push.
- Round 61 authoritative remote verification: GitHub CI run `32002551102` passed at
  `5c3047251cbffe9332f04dbaf68b60c4c875e4c1` on `main`. Backend Ruff lint/format, pytest, and the
  release audit passed. Frontend lint/typecheck/157 tests/build passed. Both Playwright Chromium
  journeys passed.

## Known limitations and blockers

No source-integrity or privacy blocker is currently known, but the full product objective is not yet
complete. Round 12 is a detection/OCR evaluation checkpoint rather than a claim of unattended
full-book output quality.

- Private pages still lack human-reviewed boxes/transcriptions. Detector-draft JSON is a starting
  proposal set, not precision/recall evidence.
- The representative export can use mock or local Argos translations for structural testing. Argos is
  general English-pivot MT, not manga-tuned, and currently Simplified Chinese only. Remaining font-fit
  issues and restoration artifacts still prevent unattended publication. Adjacent small OCR fragments
  can share a typeset run, and a region-scoped typeset overlays those boxes onto the last plate when
  the clean plate is still current. If that typeset file is missing, the job redraws every eligible
  box on the current inpaint plate instead of dropping untouched text. When a typeset job for the
  current page completes, the canvas switches to the typeset preview. A partial overlay keeps the
  boxes just redrawn selected; a full-page typeset still selects remaining overflowing boxes.
  The canvas then frames those selected boxes, including after compare splits the view.
  Inspector overflow actions (**选中溢出框** / **打开**) also frame those boxes and open typesetting.
  Sidebar overflow skip and **⌥⇧← / ⌥⇧→** jump to overflowing pages and frame their overflow boxes.
  The sidebar **排版溢出** pill also opens that page and frames those boxes.
  **← / →** follow the sidebar filter and search; under **排版溢出** they skip hidden pages and frame
  overflowing boxes. The footer counter is that visible list, and **← / →** disable at its ends.
  Under **失败 / 不可用**, **← / →** also open the matching inspector for that page's failed stage.
  Clicking a failed or unavailable sidebar page does the same.
  **⌥↓ / ⌥↑** and the inspector region list frame the selected box.
  **G** and the canvas **框住** control frame the current selection.
  Clicking a job-queue item opens that page and the matching inspector, then closes the batch drawer.
  Overlay typeset items select and frame the redrawn boxes; full-page typeset items frame leftover
  overflow. Completed inpaint items open the erased preview and review mask; completed preprocess
  items open the enhanced preview. Failed detect/OCR/translate items open **文本**; failed inpaint
  items open **修复**; failed typeset items open **排版**; failed preprocess/export items open **项目**.
  The inspector then shows that page's processing failure and can retry the failed stage for this page.
  After retry, a queued or running notice can open the matching batch job on demand.
  When an inpaint job for the current page completes, the canvas switches to the erased
  preview and shows the review mask. When a preprocess job for the current page completes, the canvas
  switches to the enhanced preview. Those visual-stage completions also open original-vs-result compare.
  Generated preprocess, inpaint, typeset, and mask responses are not stored in the browser HTTP cache,
  so an overlay typeset reloads the rewritten plate instead of the previous image.
  Widely separated or misaligned boxes still overflow independently.
  Geometry, mask, or trust edits still rebuild inpainting.
- MangaOCR/PaddleOCR recognition, arbitrary polygon/whole-page mask editing, and unattended
  publication-quality restoration remain roadmap work. Local visual review of Real-ESRGAN contact
  sheets and inpaint candidate sheets is still required before treating AI output as publication-quality.
- Tesseract TSV over-detects hatching/line art. Prefer `ppocr-v3` when precision on negatives matters;
  `ppocr-v3+tesseract` now merges those extra proposals instead of stacking them.
