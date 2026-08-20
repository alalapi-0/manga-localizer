# Manga Localizer — Project State

Updated: 2026-08-21

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

Live work is one-page full reprocess: every real uploaded image, one
image per round, quality pass first, then text pipeline only when
the page has text. Earlier realpages “已检查” counts are not a skip.
This loop finished sidebar 1 (1184×701, no-text), sidebar 2
(1166×540, text), sidebar 3 (627×1843, no-text after P-1),
sidebar 4 (340×594, text), sidebar 5 (1110×312, text after
P-2), sidebar 6 (1068×811, text), sidebar 7 (1084×749,
text), sidebar 8 (1190×666, text), sidebar 9 (1185×551,
no-text), sidebar 10 (1185×1095, text), sidebar 11
(1188×435, text), sidebar 12 (1182×751, text), sidebar 13
(1076×515, text), sidebar 14 (1189×777, no-text), sidebar 15 (1058×631,
text), sidebar 16 (332×572, text), sidebar 17 (1073×482,
text), sidebar 18 (1190×661, text), sidebar 19 (1178×537,
no-text), sidebar 20 (1187×1244, text), sidebar 21 (958×228,
no-text), sidebar 22 (1284×777, no-text), sidebar 23 (1072×564,
no-text), sidebar 24 (1074×358, text), sidebar 25 (1074×793,
text), sidebar 26 (1089×334, no-text), and sidebar 27 (1068×619,
text), sidebar 28 (1064×628, text after P-3/P-4/P-5), sidebar 29
(1174×1161, text), sidebar 30 (1189×699, no-text), and sidebar 31
(1175×1815, text after P-6), and sidebar 32 (441×1827, text).
Sidebar 33 (717×1818, no-text) and sidebar 34 (1181×1262, no-text)
are also finished, followed by sidebar 35 (1185×384, text after P-7),
sidebar 36 (1187×571, text), sidebar 37 (1178×1267, text), and
sidebar 38 (1106×410, text after P-8), followed by sidebar 39
(1185×713, text), sidebar 40 (1076×487, no-text), and sidebar 41
(1190×765, no-text), followed by sidebar 42 (1109×319, no-text).
Sidebar 43 (1190×644, no-text) and sidebar 44 (472×1157, no-text
after rejecting enhancement) are also finished. Sidebar 45 (516×694, text
after P-9), sidebar 46 (646×447, no-text), and sidebar 47 (1282×1708,
text after P-10/P-11/P-12) are finished. Next page: sidebar 48. Product Round 8 is not
complete. No open finding blocks the next page; CI is not a page gate.

Do not re-arm `AGENT_LOOP_WAKE_manga_realpages`,
`AGENT_LOOP_WAKE_manga_ui`, `AGENT_LOOP_WAKE_manga_desktop`,
`AGENT_LOOP_WAKE_manga_realdata`, or `AGENT_LOOP_WAKE_manga_app`.

## Active loop prompt

Rule reference: `.agent/PAGE_LOOP_PROMPT.md`.
No `/loop`, watcher, sleeper, or sentinel is armed for the current goal.
`.agent/REALPAGES_LOOP_PROMPT.md`, `.agent/UI_LOOP_PROMPT.md`,
`.agent/DESKTOP_LOOP_PROMPT.md`, and `.agent/REALDATA_LOOP_PROMPT.md`
are superseded. Do not re-arm `AGENT_LOOP_WAKE_manga_realpages`,
`AGENT_LOOP_WAKE_manga_ui`, `AGENT_LOOP_WAKE_manga_desktop`,
`AGENT_LOOP_WAKE_manga_realdata`, or `AGENT_LOOP_WAKE_manga_app`.
Do not arm a 25-minute fallback sleeper. Late wakes for the old
sentinels skip rewrite and do not resume those loops.

## Automation closeout

On 2026-08-19 the user stopped every leftover project worker, queue,
watcher, and loopback API, then replaced the live prompt with
`.agent/PAGE_LOOP_PROMPT.md`. That stale API was stopped; the current page loop
may run a bounded real-app session on `:8000`. Do not re-arm
the superseded sentinels. The 2026-08-18 realpages loop, the
2026-08-17 packaging/app loop, the real-data process/fix loop, and
the synthetic UI loop stay superseded. Late wakes for those
sentinels skip rewrite.

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
  baseline; optional PP-OCRv3 supplies bounded detector polygons. PP-OCR letterboxes each tile
  instead of stretching a full page to 736×736; tall or large plates use overlapping
  input-sized tiles and NMS. Boxes smaller than a short-side-scaled
  minimum are dropped on the detector plate and after mapping back to
  the page, so a wide 4× plate does not flood the inspector with 3-8 px
  fragments. `ppocr-v3+tesseract` merges
  overlapping, contained, and nearby aligned proposals from both detectors, then pads the box so
  glyphs are enclosed. It does not drop low-confidence text or grant trust. Re-running detection
  replaces stale empty unconfirmed auto boxes, leftover tiny unconfirmed auto
  boxes, and oversized low-confidence unconfirmed auto leftovers (even when OCR
  filled garbage text), then skips duplicates of kept regions.
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
  candidates (provider, Navier-Stokes, Telea, line-art-guided); eligible LaMa pages also store an
  optional union-mask full-context candidate for mixed high-contrast boundaries. Candidate choice
  remains explicit; an optional-pass failure falls back to the four successful candidates. Switching a candidate replaces the canonical inpainted bytes and clears
  dependent reviews.
- Repair defaults to the `safe` eligibility policy. Canonical repair settings are persisted across API,
  queue, and UI; text/full-region masks support padding, dilation, feathering, editable geometry, and an
  actual-mask preview. Text-contour masks support `auto`, `dark`, and `light` polarity; explicit
  polarity masks only the selected glyph core. Bounded add/erase strokes are persisted per region. Typesetting requires safe
  eligibility and intersection with the generated mask, and cannot reuse an inpaint cache made under a
  different repair policy. Completed typesetting persists overflowing region IDs as review hints; they
  are cleared when typesetting is invalidated and are not an export hard gate.
- Preprocess, inpaint, and typeset results have revision-guarded accept/reject records bound to the
  exact response bytes decoded in the review canvas; inpaint also binds and visibly reviews its mask.
  Regeneration, changed bytes, or an upstream change clears or conflicts with affected reviews.
  Generated-image export and portable generated assets require current accepted results; JSON-only
  export remains independent. The per-region `confirmed` flag controls the page-review gate only;
  toggling it on an already-trusted region preserves current visual artifacts and accepted reviews,
  while a new trust decision or any pixel-affecting edit still invalidates its owning stages.
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
- [ ] Round 8: full-book clean-plate visual review is partial; this
  loop reprocesses from sidebar 1 and has finished 47/130.
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
- [ ] Next real-data checkpoint: remaining 83/130 visual reviews; local human use of the draft-review
  CLI to promote private detector-draft JSON into independent ground truth.

## Verification evidence

- 2026-08-21 page-loop sidebar 47 (1282×1708), P-10/P-11/P-12 cleared:
  four detected regions were reduced to three trusted text regions and one
  ignored false positive. The optional union-mask LaMa full-context candidate
  was compared with the regional candidates at fit and enlarged views with its
  actual mask shown and hidden; all repair areas removed the complete outlined
  lettering without the former blocks, stripes, or scalloped remnants while the
  mixed boundary and nearby artwork remained continuous. The first undersized
  typeset was rejected; fixed-size outlined vertical typesetting was rebuilt and
  accepted with zero overflow. The production bundle then passed the same-URL
  preview cycle with a visible canvas and no new console errors. After the
  confirmation-invalidation fix, unconfirming and reconfirming a trusted region
  preserved both accepted visual stages; the rebuilt final PNG exactly matched
  the previously accepted checksum. The page was marked checked and a one-page,
  JSON-only, serial export completed and finalized with no active job. Full local
  verification passed 9 launcher tests, backend Ruff lint/format and 270 pytest
  cases, frontend ESLint/TypeScript and 173 Vitest cases, the production build,
  `git diff --check`, and the release/privacy audit over 154 candidate files and
  1231 historical blobs. Private trees remain untracked.
- 2026-08-21 page-loop sidebar 46 (646×447): Real-ESRGAN ONNX 4× was
  compared with the original and accepted after sharpening the speed lines,
  figure contours, and screentone without changing the composition. Fresh
  current-page detect+OCR produced one empty fragment on drawn effect line art;
  it was explicitly ignored. A final full-page scan found no dialogue,
  caption, title, or other translatable text, so **确认本页无文字** completed
  the no-text path. No downstream text job or private export was run.
- 2026-08-21 page-loop sidebar 45 (516×694), P-9 cleared: the shared
  text-contour mask now segments through a guard band, rejects components that
  enter from its boundary, uses symmetric corroborated dark/light candidates,
  supports an explicit per-region text polarity, and never falls back to the
  complete detector rectangle for dense lettering. The live same-page mask was
  inspected both shown and hidden. Explicit dark polarity with a narrow hard
  fill removed the complete glyph cores and retained the outlined backing while
  excluding the connected blade, clothing, and speed-line structures. Eight
  trusted fragments were locally translated and reconfirmed; the clean plate
  and final vertical typeset were accepted, the page was marked checked, and
  current-page text-only JSON export completed without an active job or current
  overflow. Private trees were not committed.
- 2026-08-20 page-loop sidebar 44 (472×1157): Real-ESRGAN ONNX 4×
  (1888×4628) was compared with the original at fit and enlarged views. The
  result sharpened line work but removed a large intended foreground screentone,
  so it failed the visual gate and was explicitly rejected; the original was
  retained. Current-page detect+OCR produced two proposals. Enlarged
  selected-region review placed both on foreground hair contours and screentone
  rather than text, so both were explicitly ignored. A final full-page scan of
  the retained original found no dialogue, caption, title, effect lettering, or
  other translatable text. The page was marked **确认本页无文字** and
  current-page **仅文本 JSON** completed; no rejected image was exported.
  Preprocess/detection/OCR/export are done; the preprocess review remains
  `rejected` by design; `no-text-reviewed` is persisted; the text-only stages
  remain intentionally unrun; both false positives are ignored; no
  review-needed regions, processing errors, overflow, or active jobs remain.
  Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 43 (1190×644): Real-ESRGAN ONNX 4×
  (4760×2576) was compared with the original and accepted. It preserved the
  wide action composition, character silhouettes, speed lines, grayscale
  tones, and drawn effect lettering while improving line clarity. Current-page
  detect+OCR produced one small proposal. Enlarged selected-region review
  placed it on drawn effect lettering inside an action mark rather than
  dialogue, caption, title, or other translatable text, so it was explicitly
  ignored. A final full-page scan confirmed that the remaining large marks are
  effect artwork. The page was marked **确认本页无文字** and current-page
  **仅文本 JSON** completed. Preprocess/detection/OCR/export are done;
  preprocess is accepted; `no-text-reviewed` is persisted; the text-only stages
  remain intentionally unrun; the sole false positive is ignored; no
  review-needed regions, processing errors, overflow, or active jobs remain.
  Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 42 (1109×319): Real-ESRGAN ONNX 4×
  (4436×1276) was compared with the original and accepted. It preserved the
  horizontal two-panel composition, figures, speed lines, grayscale tones,
  punctuation, and large drawn effect lettering while improving line clarity.
  Current-page detect+OCR produced one proposal. Enlarged selected-region
  review placed it on the large drawn effect lettering rather than dialogue,
  caption, title, or other translatable text, so it was explicitly ignored. A
  final full-page scan confirmed the punctuation-only bubble and effect
  lettering should remain artwork. The page was marked **确认本页无文字** and
  current-page **仅文本 JSON** completed. Preprocess/detection/OCR/export are
  done; preprocess is accepted; `no-text-reviewed` is persisted; the text-only
  stages remain intentionally unrun; the sole false positive is ignored; no
  review-needed regions, processing errors, overflow, or active jobs remain.
  Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 41 (1190×765): Real-ESRGAN ONNX 4×
  (4760×3060) was compared with the original and accepted. It preserved the
  foreground and distant figures, speed lines, architecture, grayscale tones,
  and large drawn effect lettering while improving line clarity. Current-page
  detect+OCR produced one proposal. Enlarged selected-region review placed it
  on the foreground figure's back shadow and line art rather than text, so it
  was explicitly ignored. A final full-page scan confirmed that the visible
  lettering is drawn effect artwork and that no dialogue, caption, title, or
  other translatable text is present. The page was marked
  **确认本页无文字** and current-page **仅文本 JSON** completed.
  Preprocess/detection/OCR/export are done; preprocess is accepted;
  `no-text-reviewed` is persisted; the text-only stages remain intentionally
  unrun; the sole false positive is ignored; no review-needed regions,
  processing errors, overflow, or active jobs remain. Open findings remain
  none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 40 (1076×487): Real-ESRGAN ONNX 4×
  (4304×1948) was compared with the original and accepted. It preserved the
  foreground figure, distant group, skyline, clouds, ground contours, and
  grayscale tones while improving line clarity. Current-page detect+OCR
  produced one proposal. Enlarged selected-region review placed it on an arm
  contour and image texture rather than text, so it was explicitly ignored. A
  final full-page scan found no dialogue, caption, title, effect, or other
  translatable text. The page was marked **确认本页无文字** and
  current-page **仅文本 JSON** completed. Preprocess/detection/OCR/export
  are done; preprocess is accepted; `no-text-reviewed` is persisted; the
  text-only stages remain intentionally unrun; the sole false positive is
  ignored; no review-needed regions, processing errors, overflow, or active
  jobs remain. Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 39 (1185×713): Real-ESRGAN ONNX 4×
  (4740×2852) was compared with the original and accepted. Current-page
  detect+OCR produced 13 proposals; cleanup left 5. Visual review consolidated
  the complete three-column floating dialogue into one region and explicitly
  ignored 4 overlapping fragments. The retained region was expanded to cover
  every column, re-OCRed, operator-corrected, confirmed, trusted, and translated
  locally. Its first repair mask reached the cloud line below the text, so that
  result was rejected. Shortening the lower boundary, repeating
  OCR/translation/confirmation, and rebuilding kept the complete dialogue while
  excluding the cloud. All four repair candidates were compared with the mask
  hidden; the primary LaMa result removed the full text, preserved the hair and
  cloud contours, and restored the gray gradient more cleanly than the OpenCV
  alternatives. A 44 px fixed vertical Pillow typeset formed three balanced
  right-to-left columns and was checked for punctuation, centering, legibility,
  and containment before acceptance with zero overflow. Page review and
  current-page **仅文本 JSON** completed. All seven stages are done;
  preprocess/inpaint/typeset are accepted; the active region is confirmed and
  trusted; 4 duplicate fragments are ignored; no review-needed regions,
  processing errors, overflow, or active jobs remain. Open findings remain
  none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 38 (1106×410): Real-ESRGAN ONNX 4×
  (4424×1640) was compared with the original and accepted. Current-page
  detect+OCR produced 24 proposals; cleanup left 12. Visual review retained 2
  vertical dialogue regions, ignored 5 contained duplicates, left one large
  drawn effect as artwork, and explicitly ignored 4 line-art false positives.
  Both retained regions were operator-corrected, confirmed, trusted, and
  translated locally. The first repair mask touched adjacent face and figure
  detail, so it was rejected; both regions were narrowed, re-OCRed,
  reconfirmed, retranslated, and rebuilt on the same page. P-8 was then
  recorded when the current-provider and line-guided clean plates still left
  the short dialogue's terminal long dash. Extending that region's lower
  boundary, repeating OCR/translation/confirmation, and rebuilding enclosed
  the complete dash. All four candidates were rechecked with the mask hidden;
  the primary LaMa result removed both complete dialogue groups while
  preserving the nearby face contour, figure texture, panel borders, and drawn
  effect. The final 28 px auto-fit vertical Pillow typeset was checked in both
  regions for right-to-left order, punctuation, centering, legibility, and
  containment and accepted with zero overflow. Page review and current-page
  **仅文本 JSON** completed. All seven stages are done;
  preprocess/inpaint/typeset are accepted; 2 active regions are confirmed and
  trusted; 10 duplicates or artwork false positives are ignored; no
  review-needed regions, processing errors, overflow, or active jobs remain.
  P-8 cleared and open findings are none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 37 (1178×1267): Real-ESRGAN ONNX 4×
  (4712×5068) was compared with the original and accepted. Current-page
  detect+OCR produced 12 proposals; cleanup consolidated them to 2. Visual
  review retained one complete vertical dialogue region and explicitly ignored
  one overlapping duplicate. The retained region was operator-corrected,
  confirmed, trusted, and translated locally. Its first repair mask covered all
  glyphs but also touched the left balloon outline, so that result was rejected.
  The same region was narrowed, re-OCRed, reconfirmed, retranslated, and rebuilt
  before any page advance. The second mask enclosed only text; all four repair
  candidates were compared with the mask hidden, and the primary LaMa result
  removed the complete dialogue while preserving the balloon border and nearby
  line art. A 60 px fixed vertical Pillow typeset reproduced the original
  three-column visual weight and was checked for right-to-left order,
  punctuation, centering, legibility, and containment before acceptance with
  zero overflow. Page review and current-page **仅文本 JSON** completed. All
  seven stages are done; preprocess/inpaint/typeset are accepted; the active
  region is confirmed and trusted; the duplicate is ignored; no review-needed
  regions, processing errors, overflow, or active jobs remain. Open findings
  remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 36 (1187×571): Real-ESRGAN ONNX 4×
  (4748×2284) was compared with the original and accepted. Current-page
  detect+OCR produced 21 proposals; cleanup left 3. Visual review retained one
  complete vertical dialogue region and explicitly ignored one overlapping
  duplicate plus one line-art false positive. The retained region was expanded
  to enclose the complete glyph group, operator-corrected, confirmed, trusted,
  and translated locally. All four repair candidates were compared with the
  review mask hidden at enlarged zoom; the primary LaMa result removed the
  text cleanly while preserving the balloon border and surrounding art. The
  first 28 px auto-fit typeset was rejected as visibly too small. A 44 px fixed
  vertical typeset was then regenerated and checked for right-to-left column
  order, centering, legibility, and containment before acceptance with zero
  overflow. Page review and current-page **仅文本 JSON** completed. All seven
  stages are done; preprocess/inpaint/typeset are accepted; the active region
  is confirmed and trusted; 2 false positives are ignored; no review-needed
  regions, processing errors, overflow, or active jobs remain. Open findings
  remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 35 (1185×384): Real-ESRGAN ONNX 4×
  (4740×1536) was compared with the original and accepted. Current-page
  detect+OCR produced 20 proposals; cleanup left 8. Visual review retained 3
  dialogue regions and explicitly ignored 5 overlapping duplicates or artwork
  false positives. The three retained regions were bounded to their complete
  glyph groups, operator-corrected, set to vertical dialogue, confirmed,
  trusted, and translated locally with concise operator wording. P-7 was
  recorded after every first-pass repair candidate left one final ellipsis dot
  in the small balloon. Extending that region's lower boundary, re-OCRing,
  reconfirming, and rebuilding enclosed the complete punctuation without
  touching the balloon outline. All four candidates were rechecked at enlarged
  selected-region zoom; the remnant disappeared, and the primary LaMa result
  preserved all balloon borders and surrounding art. The final 28 px auto-fit
  vertical Pillow typeset was checked region by region for column order,
  punctuation, centering, and legibility and accepted with zero overflow.
  Page review and current-page **仅文本 JSON** completed. All seven stages are
  done; preprocess/inpaint/typeset are accepted; 3 active regions are confirmed
  and trusted; 5 false positives are ignored; no review-needed regions,
  processing errors, overflow, or active jobs remain. P-7 cleared. Private
  trees were not committed.
- 2026-08-20 page-loop sidebar 34 (1181×1262): Real-ESRGAN ONNX 4×
  (4724×5048) was compared with the original at fit-to-page zoom and
  accepted. It preserved the central figure, cloud and debris contours,
  grayscale tone, and the large drawn effects while improving line clarity.
  Current-page detect+OCR returned 0 regions on the accepted enhanced plate.
  A complete visual scan confirmed that the visible lettering is part of the
  drawn effect artwork and that no dialogue, caption, title, or other
  translatable text is present. The page was marked **确认本页无文字** and
  current-page **仅文本 JSON** completed. Preprocess/detection/OCR/export are
  done; preprocess is accepted; `no-text-reviewed` is persisted;
  translation/inpaint/typeset are correctly skipped; no regions,
  review-needed items, processing errors, overflow, or active jobs remain.
  Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 33 (717×1818): the latest Real-ESRGAN
  ONNX 4× result (2868×7272) was compared with the original at fit-to-page
  zoom and accepted after its final same-page rerun. It sharpened the narrow
  full-page composition without changing panel lines, tones, figures, or the
  large drawn effects. Current-page detect+OCR produced 4 proposals; cleanup
  consolidated them to 2. Enlarged visual review placed one on the large drawn
  effect lettering and the other on non-text image texture, so both were
  explicitly ignored. A final full-page scan found no dialogue, caption, title,
  or other translatable text. The page was marked **确认本页无文字** and the
  final accepted artifact was exported as current-page **仅文本 JSON**.
  Preprocess/detection/OCR/export are done; preprocess is accepted;
  `no-text-reviewed` is persisted; translation/inpaint/typeset are correctly
  skipped; no active regions, review-needed regions, processing errors,
  overflow, or active jobs remain. Open findings remain none. Private trees
  were not committed.
- 2026-08-20 page-loop sidebar 32 (441×1827): Real-ESRGAN ONNX 4×
  (1764×7308) accepted after fit and enlarged comparisons preserved the narrow
  page's panel lines, tones, and drawn effects. Current-page detect+OCR produced
  30 proposals; cleanup left 16. Visual review kept 3 dialogue regions, ignored
  13 artwork false positives, and left the large drawn sound effects as art.
  The retained regions were bounded to their complete glyph groups, re-OCRed,
  operator-corrected, confirmed, trusted, and translated locally. All four
  repair candidates were compared with the mask shown and hidden at fit and
  selected-region zoom. The primary LaMa result removed all three text groups
  most cleanly while preserving balloon borders and surrounding line art, so it
  was accepted. The 28 px auto-fit vertical Pillow result remained legible,
  centered, and correctly columned in all three balloons at fit and enlarged
  zoom, with zero overflow, and was accepted. Page review and current-page
  **仅文本 JSON** export completed. All seven stages are done;
  preprocess/inpaint/typeset are accepted; 3 active regions are confirmed and
  trusted; 13 false positives are ignored; no review-needed regions,
  processing errors, overflow, or active jobs remain. Open findings remain
  none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 31 (1175×1815): Real-ESRGAN ONNX 4×
  (4700×7260) accepted. Current-page detect+OCR produced 11 proposals;
  cleanup left 7, visual review kept 2 short vertical exclamations and ignored
  5 artwork false positives while leaving the large drawn sound effect as art.
  Both retained regions were re-OCRed, operator-corrected, confirmed, and
  translated locally with concise operator wording. P-6 was recorded when the
  first text masks omitted terminal punctuation. Extending the lower bounds
  removed the remnants but initially absorbed nearby hair/shadow, so both boxes
  were tightened back to their glyph columns, re-OCRed, and reconfirmed. Four
  repair candidates were compared at fit and enlarged zoom; the primary LaMa
  result best preserved nearby art and was accepted. The first Pillow typeset
  was rejected as too small, the next as persistently overflowing, and fixed
  44 px vertical styles passed with zero overflow. A final translation-stage
  refresh, operator wording restoration, typeset rerun, page review, and
  current-page **仅文本 JSON** closed every invalidated stage. All seven stages
  are done; preprocess/inpaint/typeset are accepted; 2 active regions are
  confirmed and trusted; 5 false positives are ignored; no review-needed
  regions, processing errors, overflow, or active jobs remain. P-6 cleared.
  Private trees were not committed.
- 2026-08-20 page-loop sidebar 30 (1189×699): Real-ESRGAN ONNX 4×
  (4756×2796) accepted after original/result comparison preserved line art,
  gray fills, and motion strokes. Current-page detect+OCR produced one
  nonempty proposal; close visual inspection placed it entirely on drawn
  shadow strokes, so it was explicitly ignored. No dialogue, caption, title,
  or translatable drawn text remained anywhere on the page. The page was
  marked **确认本页无文字** and current-page **仅文本 JSON** completed.
  Preprocess/detection/OCR/export are done; the accepted preprocess review and
  `no-text-reviewed` state are persisted; translation/inpaint/typeset were
  correctly skipped; no review-needed regions, processing errors, overflow,
  or active jobs remain. Open findings remain none. Private trees were not
  committed.
- 2026-08-20 page-loop sidebar 29 (1174×1161): Real-ESRGAN ONNX 4×
  (4696×4644) accepted. Current-page detect+OCR produced three proposals;
  cleanup left two artwork false positives, both explicitly ignored, and one
  complete dialogue region was drawn manually, re-OCRed, confirmed, and
  translated locally with one operator wording correction. The primary LaMa
  clean plate and its text-only mask removed the lettering while preserving the
  balloon edge and surrounding art. The first Pillow typeset was rejected as too
  small; a style-only correction and reconfirmation preserved the accepted
  inpaint review, and the second result was readable, centered, and accepted with
  no overflow. **标记本页已检查** and current-page **仅文本 JSON** completed.
  All seven pipeline stages are done; preprocess/inpaint/typeset are accepted;
  the one active region is confirmed and trusted; two false positives are
  ignored; no review-needed regions, processing errors, overflow, or active jobs
  remain. Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 28 (1064×628): P-3 recorded before
  changing code; external Chromium launchers no longer own source-tree API
  lifetime, while the bundled helper still does. Repaired `npm run app`
  remained live for the full same-page recheck. Real-ESRGAN ONNX 4×
  (4256×2512) accepted; current-page detect+OCR returned 20 boxes.
  Visual review consolidated fragments, kept 2 complete balloons, and
  ignored 10 replaced fragments / drawn-SFX boxes. Both balloons were
  re-OCRed and confirmed; local translation used one operator correction.
  The first clean plate exposed P-5 at the lower balloon boundary, so the
  affected manual region was shortened before the outline and routed through
  a local OpenCV solid text mask while the other region remained on LaMa.
  Direct clean-plate comparison then showed both text groups removed, the
  balloon outline intact, and surrounding art unchanged. The first Pillow
  typeset was rejected for wording. P-4 then exposed that reconfirming this
  translation-only correction discarded the unchanged inpaint review; the
  invalidation rule was fixed and covered by regression. The live same-page
  retry preserved the accepted inpaint review, produced accepted typeset with
  no overflow, and finished **标记本页已检查** plus current-page **仅文本
  JSON**. All seven pipeline stages are done; preprocess/inpaint/typeset are
  accepted; both active regions are confirmed and trusted; no review-needed
  regions, processing errors, overflow, or active jobs remain. P-3/P-4/P-5
  cleared. Private trees were not committed.
- 2026-08-20 page-loop sidebar 27 (1068×619): Real-ESRGAN ONNX 4×
  (4272×2476) accepted, current-page detect+OCR returned 22 boxes
  with 0 sub-minimum fragments. Visual review kept 2 balloons and
  left drawn SFX as artwork. Operator translations; LaMa inpaint
  and Pillow typeset accepted; **标记本页已检查** and current-page
  **仅文本 JSON**. Open findings remain none. Private trees were
  not committed.
- 2026-08-20 page-loop sidebar 26 (1089×334): Real-ESRGAN ONNX 4×
  (4356×1336) accepted, current-page detect+OCR returned 2 boxes
  with 0 sub-minimum fragments. Visual review put them on landscape
  lines, not balloons; ignored; **确认本页无文字** set
  `no-text-reviewed`. Open findings remain none. Private trees were
  not committed.
- 2026-08-20 page-loop sidebar 25 (1074×793): Real-ESRGAN ONNX 4×
  (4296×3172) accepted, current-page detect+OCR returned 43 boxes
  with 0 sub-minimum fragments. Visual review kept 2 floating text
  clusters. Operator translations; LaMa inpaint and Pillow typeset
  accepted; **标记本页已检查** and current-page **仅文本 JSON**.
  Open findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 24 (1074×358): Real-ESRGAN ONNX 4×
  (4296×1432) accepted, current-page detect+OCR returned 46 boxes
  with 0 sub-minimum fragments. Visual review kept 3 balloons,
  ignored eye/SFX leftovers. Operator translations; LaMa inpaint
  and Pillow typeset accepted; **标记本页已检查** and current-page
  **仅文本 JSON**. Open findings remain none. Private trees were
  not committed.
- 2026-08-20 page-loop sidebar 23 (1072×564): Real-ESRGAN ONNX 4×
  (4288×2256) accepted, current-page detect+OCR returned 1 box with
  0 sub-minimum fragments. Visual review put it on debris, not
  balloons; ignored; **确认本页无文字** set `no-text-reviewed`. Open
  findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 22 (1284×777): Real-ESRGAN ONNX 4×
  (5136×3108) accepted, current-page detect+OCR returned 1 box with
  0 sub-minimum fragments. Visual review put it on drawn SFX, not
  balloons; ignored; **确认本页无文字** set `no-text-reviewed`. Open
  findings remain none. Private trees were not committed.
- 2026-08-20 page-loop sidebar 21 (958×228): Real-ESRGAN ONNX 4×
  (3832×912) accepted, current-page detect+OCR returned 1 box with
  0 sub-minimum fragments. Visual review put it on an eye corner,
  not unread balloons; ignored; **确认本页无文字** set
  `no-text-reviewed`. Open findings remain none. Private trees were
  not committed.
- 2026-08-19 page-loop sidebar 20 (1187×1244): Real-ESRGAN ONNX 4×
  (4748×4976) accepted, current-page detect+OCR returned 2 boxes
  with 0 sub-minimum fragments. Visual review kept 1 shout and left
  drawn SFX as artwork. Operator translation where Argos was blank;
  LaMa inpaint and Pillow typeset accepted; **标记本页已检查** and
  current-page **仅文本 JSON**. Open findings remain none. Private
  trees were not committed.
- 2026-08-19 page-loop sidebar 19 (1178×537): Real-ESRGAN ONNX 4×
  (4712×2148) accepted, current-page detect+OCR returned 3 boxes
  with 0 sub-minimum fragments. Visual review put them on face and
  hair marks, not unread balloons; all 3 ignored;
  **确认本页无文字** set `no-text-reviewed`. Open findings remain none.
  Private trees were not committed.
- 2026-08-19 page-loop sidebar 18 (1190×661): Real-ESRGAN ONNX 4×
  (4760×2644) accepted, current-page detect+OCR returned 19 boxes
  with 0 sub-minimum fragments. Visual review kept 3 floating text
  clusters and ignored ruby/overlap leftovers. Operator translations;
  LaMa inpaint and Pillow typeset accepted; **标记本页已检查** and
  current-page **仅文本 JSON**. Open findings remain none. Private
  trees were not committed.
- 2026-08-19 page-loop sidebar 17 (1073×482): Real-ESRGAN ONNX 4×
  (4292×1928) accepted, current-page detect+OCR returned 4 boxes
  with 0 sub-minimum fragments. Visual review kept 2 balloons and
  ignored ruby/speck leftovers. Operator translations; LaMa inpaint
  and Pillow typeset accepted; **标记本页已检查** and current-page
  **仅文本 JSON**. Open findings remain none. Private trees were
  not committed.
- 2026-08-19 page-loop sidebar 16 (332×572): Real-ESRGAN ONNX 4×
  (1328×2288) accepted, current-page detect+OCR returned 5 boxes
  with 0 sub-minimum fragments. Visual review expanded 1 shout
  balloon and ignored 4 column/ruby leftovers. Operator translation
  where Argos dropped confirm; LaMa inpaint and Pillow typeset
  accepted; **标记本页已检查** and current-page **仅文本 JSON**.
  Open findings remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 15 (1058×631): Real-ESRGAN ONNX 4×
  (4232×2524) accepted, current-page detect+OCR returned 43 boxes
  with 0 sub-minimum fragments. Visual review kept 5 text clusters
  and ignored wing/clothing leftovers. Operator translations where
  Argos was blank or unusable; LaMa inpaint and Pillow typeset
  accepted; **标记本页已检查** and current-page **仅文本 JSON**.
  Open findings remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 14 (1189×777): Real-ESRGAN ONNX 4×
  (4756×3108) accepted, current-page detect+OCR returned 6 boxes
  with 0 sub-minimum fragments. Visual review put them on faces,
  windows, and blast art, not balloons; all 6 ignored;
  **确认本页无文字** set `no-text-reviewed`. Open findings remain none.
  Private trees were not committed.
- 2026-08-19 page-loop sidebar 13 (1076×515): Real-ESRGAN ONNX 4×
  (4304×2060) accepted, current-page detect+OCR returned 9 boxes
  with 0 sub-minimum fragments. Visual review kept 1 balloon and
  ignored face/debris leftovers. Operator translation; LaMa inpaint
  and Pillow typeset accepted; **标记本页已检查** and current-page
  **仅文本 JSON**. Open findings remain none. Private trees were
  not committed.
- 2026-08-19 page-loop sidebar 12 (1182×751): Real-ESRGAN ONNX 4×
  (4728×3004) accepted, current-page detect+OCR returned 9 boxes
  with 0 sub-minimum fragments. Visual review kept 2 balloons and
  left drawn SFX as artwork. Operator translations; LaMa inpaint
  and Pillow typeset accepted; **标记本页已检查** and current-page
  **仅文本 JSON**. Open findings remain none. Private trees were
  not committed.
- 2026-08-19 page-loop sidebar 11 (1188×435): Real-ESRGAN ONNX 4×
  (4752×1740) accepted, current-page detect+OCR returned 2 boxes
  with 0 sub-minimum fragments. Visual review kept 1 dialogue box
  and ignored a speed-line fragment. Operator translation; LaMa
  inpaint and Pillow typeset accepted; **标记本页已检查** and
  current-page **仅文本 JSON**. Open findings remain none.
  Private trees were not committed.
- 2026-08-19 page-loop sidebar 10 (1185×1095): Real-ESRGAN ONNX 4×
  (4740×4380) accepted, current-page detect+OCR returned 9 boxes
  with 0 sub-minimum fragments. Visual review kept 3 text clusters.
  Operator translations; LaMa inpaint and Pillow typeset accepted;
  **标记本页已检查** and current-page **仅文本 JSON**. Open findings
  remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 9 (1185×551): Real-ESRGAN ONNX 4×
  (4740×2204) accepted, current-page detect+OCR returned 0 boxes.
  Visual review found action/SFX artwork, not unread balloons;
  **确认本页无文字** set `no-text-reviewed`. Open findings remain none.
  Private trees were not committed.
- 2026-08-19 page-loop sidebar 8 (1190×666): Real-ESRGAN ONNX 4×
  (4760×2664) accepted, current-page detect+OCR, then visual
  review kept 4 text clusters from 37 boxes (0 sub-minimum
  fragments). Operator translations; LaMa inpaint and Pillow
  typeset accepted; **标记本页已检查** and current-page **仅文本 JSON**.
  Open findings remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 7 (1084×749): Real-ESRGAN ONNX 4×
  (4336×2996) accepted, current-page detect+OCR, then visual
  review kept 5 balloons (one previously ignored empty box still
  had source glyphs). Operator translations where Argos was
  unusable; LaMa inpaint and Pillow typeset accepted; **标记本页已检查**
  and current-page **仅文本 JSON**. Open findings remain none.
  Private trees were not committed.
- 2026-08-19 page-loop sidebar 6 (1068×811): Real-ESRGAN ONNX 4×
  (4272×3244) accepted, current-page detect+OCR returned 11 boxes
  with 0 sub-minimum fragments (P-2 holding). Visual review merged
  them into 1 balloon; Argos ja→zh, LaMa inpaint and Pillow typeset
  accepted; **标记本页已检查** and current-page **仅文本 JSON**.
  Open findings remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 5 (1110×312): waiting for CI was
  the stall, not a page defect. P-2: tiled detect on the accepted
  4× plate (4440×1248) returned 58 boxes, mostly 3-8 px fragments.
  Public min-side filter plus stale-tiny replace passed targeted
  pytest. Same-page detect+OCR then returned 26 boxes with 0
  sub-minimum fragments. Visual review kept 2 real boxes; LaMa
  inpaint and Pillow typeset were accepted; **标记本页已检查** and
  current-page **仅文本 JSON**. P-2 cleared. Private trees were not
  committed.
- 2026-08-19 page-loop stall fix: waiting for the previous page’s
  CI was incorrectly treated as a hard stop. The live prompt now
  says CI is a wake only; the next page starts in the same turn.
  Sidebar 4 (340×594) then finished: Real-ESRGAN ONNX 4×
  (1360×2376) accepted, current-page detect+OCR, 2 real boxes
  confirmed after ignore, Argos ja→zh, LaMa inpaint and Pillow
  typeset accepted, **标记本页已检查**, current-page **仅文本 JSON**.
  Open findings remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 3 (627×1843): leftover API was
  stopped and restarted. P-1: PP-OCR 736 stretch/letterbox on the
  accepted 4× plate (2508×7372) returned 0 boxes; overlapping 736
  tiles found 10. Public letterbox/tile/NMS regression passed.
  Same-page detect+OCR then returned 10 boxes; visual review put
  them on artwork, not balloons/SFX. All 10 ignored;
  **确认本页无文字** set `no-text-reviewed`. P-1 cleared. Private
  trees were not committed.
- 2026-08-19 page-loop sidebar 2 (1166×540): quality pass accepted
  Real-ESRGAN ONNX 4× (4664×2160). Enhanced detect+OCR left one
  real box and four ignored empty/false boxes. Argos ja→zh wrote
  nonempty text; the box was reconfirmed after translate, then
  LaMa inpaint and Pillow typeset were accepted. One overflow box
  stayed in the balloon (font-fit only). **标记本页已检查** and
  current-page **仅文本 JSON** export completed. Open findings
  remain none. Private trees were not committed.
- 2026-08-19 page-loop sidebar 1 (1184×701): leftover API/watchers
  were stopped, then a fresh loopback API was started. Suggested
  OpenCV/Pillow enhance still looked like a low-quality capture;
  Real-ESRGAN ONNX AI 重绘 produced a 4× plate (4736×2804) and was
  accepted. Current-page detect+OCR on the enhanced plate returned
  0 boxes. Visual check found no balloon/SFX text; **确认本页无文字**
  set `no-text-reviewed`. Open findings remain none. Private trees
  were not committed.
- 2026-08-19 user stop + loop replace: leftover API/watchers were
  stopped (`:8000` free). Live prompt is now
  `.agent/PAGE_LOOP_PROMPT.md` (`AGENT_LOOP_WAKE_manga_page`): one
  real page per round, AI 超分 + AI 重绘 first, no-text pages stop
  there, text pages then 扣字/翻译/嵌字, report-then-fix, push
  `origin/main` after each finished page. Private trees were not
  committed.
- 2026-08-18 resume pass: the user said continue. Same 130-page book
  at 1100×800. A fifth text page (1190×661) finished after the first
  typeset was rejected (text on artwork) and two remaining boxes were
  re-inpainted. A sixth text page (1178×537) finished after a blank
  Argos result was filled by the operator. A tall leftover page
  (1187×1244) had one clothing-print false box ignored; **确认本页无文字**
  was not clicked. RP-9 remount still defaults to **当前页** · 1 张 ·
  2 步. Open findings remain none. Private trees were not committed.
- 2026-08-18 docs CI: GitHub Actions run `32128671541` succeeded at
  `6e979e093d2029e2cf93880c50a284d7805ffcb2` (frontend, backend, e2e).
  A fourth text page (1073×482) finished detect/OCR → confirm →
  translate → inpaint → typeset → accept → JSON export. Private
  trees were not committed.
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
