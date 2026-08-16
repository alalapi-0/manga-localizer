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
- Adjacent small typesetting boxes can share one layout run: identical translations pack across the
  cluster, and distinct fragment texts concatenate in reading order. Large balloons stay independent.
- Overflow-only typesetting: the inspector can select overflowing boxes or rerun Pillow typesetting for
  those region IDs without touching the rest of the page.
- The typesetting inspector can rerun Pillow typesetting for the currently selected box after style or
  geometry edits.
- T retypesets the selected box and Shift+T retypesets overflowing boxes when the canvas is focused.
- After a typeset job for the current page completes, the canvas switches to the typeset preview,
  opens original-vs-result compare, and selects remaining overflowing boxes.
- After an inpaint job for the current page completes, the canvas switches to the erased preview,
  shows the review mask, opens compare, and opens the repair inspector.
- After a preprocess job for the current page completes, the canvas switches to the enhanced preview
  and opens original-vs-result compare.
- Clicking a job-queue item opens that page and the matching inspector, then closes the batch drawer.
  Overlay typeset items select and frame the redrawn boxes; full-page typeset items frame leftover
  overflow. Completed inpaint items open the erased preview and review mask; completed preprocess
  items open the enhanced preview. Failed detect/OCR/translate items open **文本**; failed inpaint
  items open **修复**; failed typeset items open **排版**; failed preprocess/export items open **项目**.
  The inspector then shows that page's processing failure and can retry the failed stage for this page
  without opening the batch drawer.
- The sidebar **排版溢出** pill opens that page, selects overflowing boxes, and frames them.
- Adjacent image navigation follows the sidebar filter and search. Under **排版溢出**, **← / →** skip
  hidden pages and frame overflowing boxes. The sidebar footer counts that visible list, and **← / →**
  disable at its ends. Under **失败 / 不可用**, **← / →** open the matching inspector for that page's
  failed stage. Clicking a failed or unavailable sidebar page does the same. **⌥← / ⌥→** jump to the
  previous / next failed or unavailable page in the full library and open that inspector.
- **⌥↓ / ⌥↑** and the inspector region list frame the selected box.
- **G** and the canvas **框住** control frame the current selection. **F** / **适窗** return to the
  whole page.
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
- Round 18 maps CJK punctuation to vertical presentation forms and hangs comma/period glyphs. Horizontal
  layouts keep authored punctuation. Local gates passed 2 launcher tests and 222 backend tests; frontend
  was unchanged from Round 17. The release audit scanned 128 candidate files and 492 historical blobs.
  GitHub CI run `31874726926` passed on `41545b8e453aaebab9325ab253f9754168712acc`.
- Round 19 packs adjacent small typesetting boxes as fragment clusters. Identical translations share
  the cluster; distinct fragment texts concatenate in reading order. Local gates passed 2 launcher
  tests and 224 backend tests; frontend was unchanged from Round 17. The release audit scanned 128
  candidate files and 505 historical blobs. GitHub CI run `31875271369` passed on
  `7dfccd324d29ab7c33055c70d7140e318c2b7cc7`.
- Round 20 adds inspector actions to select overflowing boxes or rerun Pillow typesetting for those
  region IDs only. Local gates passed 2 launcher tests and frontend lint/typecheck/101 tests plus the
  production build. The release audit scanned 128 candidate files and 518 historical blobs. GitHub CI
  run `31876251138` passed on `d02e873fd3860290ebf15bbb98586079ab40b1be`.
- Round 21 adds a typesetting-inspector action to rerun Pillow typesetting for the selected box only.
  Local gates passed frontend lint/typecheck/102 tests plus the production build. The release audit
  scanned 128 candidate files and 533 historical blobs. GitHub CI run `31876680453` passed on
  `59a821b7707f19b8a8d2109c150b8e941981c895`.
- Round 22 overlays Pillow typesetting for requested region IDs onto the last typeset plate, expands
  fragment-cluster mates, and keeps overflow IDs for untouched boxes. Translation and typography edits
  no longer discard a current inpaint plate. Local gates passed backend Ruff lint/format and 229
  pytest cases. The release audit scanned 128 candidate files and 544 historical blobs. Frontend was
  unchanged from Round 21. GitHub CI run `31878242652` passed on
  `df15d7c6d0ae86d1189b3a3de081a1777046b739`.
- Round 23 redraws the whole page when a region-scoped typeset cannot overlay because the last
  typeset plate is missing, instead of dropping untouched boxes. Local gates passed backend Ruff
  lint/format and 230 pytest cases. The release audit scanned 128 candidate files and 563 historical
  blobs. Frontend was unchanged from Round 22. GitHub CI run `31878760451` passed on
  `c8fb20beca736452f702121ad64b7a16ac52b1c3`.
- Round 24 shows overlay vs full-page typeset counts on the job queue card. Local gates passed
  frontend lint/typecheck/104 tests plus the production build. The release audit scanned 128
  candidate files and 576 historical blobs. Backend was unchanged from Round 23. GitHub CI run
  `31879071282` passed on `5e8545bd7e747b22b0cb989ce4a5a0221ed598a1`.
- Round 25 adds T and Shift+T shortcuts to retypeset the selected box or overflowing boxes. Local
  gates passed frontend lint/typecheck/106 tests plus the production build. The release audit scanned
  128 candidate files and 587 historical blobs. Backend was unchanged from Round 23. GitHub CI run
  `31879412533` passed on `d674775ba742aa0103669ce9f1f912b856737728`.
- Round 26 switches the canvas to the typeset preview when a typeset job for the current page
  completes. Local gates passed frontend lint/typecheck/110 tests plus the production build. The
  release audit scanned 128 candidate files and 599 historical blobs. Backend was unchanged from
  Round 23. GitHub CI run `31879945945` passed on `906c898bd664a9a2ffdc33d5ef3bb1a783c84e0c`.
- Round 27 selects overflowing boxes and opens the typesetting inspector when a typeset job for the
  current page completes. Local gates passed frontend lint/typecheck/111 tests plus the production
  build. The release audit scanned 128 candidate files and 609 historical blobs. Backend was
  unchanged from Round 23. GitHub CI run `31880310109` passed on
  `ebdaae7e14c5a7359faf14ac546549250a985960`.
- Round 28 switches the canvas to the erased preview and shows the review mask when an inpaint job
  for the current page completes. Local gates passed frontend lint/typecheck/113 tests plus the
  production build. The release audit scanned 128 candidate files and 619 historical blobs. Backend
  was unchanged from Round 23. GitHub CI run `31880607541` passed on
  `48c52e1a4c24ceb9051cd3a9354e325d7ded7cb2`.
- Round 29 switches the canvas to the enhanced preview when a preprocess job for the current page
  completes. Local gates passed frontend lint/typecheck/115 tests plus the production build. The
  release audit scanned 128 candidate files and 629 historical blobs. Backend was unchanged from
  Round 23. GitHub CI run `31880896973` passed on `e97fe14ba1492ee85fdea884e73aab10a9753470`.
- Round 30 opens original-vs-result compare when a preprocess, inpaint, or typeset job for the
  current page completes. Local gates passed frontend lint/typecheck/115 tests plus the production
  build. The release audit scanned 128 candidate files and 640 historical blobs. Backend was
  unchanged from Round 23. GitHub CI run `31882096845` passed on
  `ca7bc89134a1f98a8f7536cad7539d18136bf6b0`.
- Round 31 serves generated preprocess, inpaint, typeset, and mask images with
  `Cache-Control: private, no-store`. The canvas fetch uses `cache: 'no-store'` so overlay typesetting
  does not keep a cached previous plate. Local gates passed 2 launcher tests, backend lint/format/231
  pytest, frontend lint/typecheck/116 tests plus the production build. The release audit scanned 128
  candidate files and 650 historical blobs. GitHub CI run `31882562724` passed on
  `656e3650b1fc45fc9c68febd3fcc6bc077854f55`.
- Round 32 keeps the just-overlaid boxes selected when a partial typeset job for the current page
  completes. A full-page typeset still selects remaining overflowing boxes. Local gates passed 2
  launcher tests and frontend lint/typecheck/118 tests plus the production build. The release audit
  scanned 128 candidate files and 663 historical blobs. Backend was unchanged from Round 31.
  GitHub CI run `31883446023` passed on `b28ca6b25c7d3b33ff47db9a9f74ed90ed2b663c`.
- Round 33 frames the selected typeset boxes in the canvas after a typeset job for the current page
  completes. Fit-to-window clears that framing. Local gates passed 2 launcher tests and frontend
  lint/typecheck/119 tests plus the production build. The release audit scanned 128 candidate files
  and 673 historical blobs. Backend was unchanged from Round 31. GitHub CI run `31883910085`
  passed on `e41261ab2e37aa974cde07b0d79aba9d7a22ae9b`.
- Round 34 frames overflow boxes from the inspector **选中溢出框** and **打开** actions, and opens
  the typesetting tab. Local gates passed 2 launcher tests and frontend lint/typecheck/120 tests plus
  the production build. The release audit scanned 128 candidate files and 686 historical blobs.
  Backend was unchanged from Round 31. GitHub CI run `31884339883` passed on
  `b637b97d9a56a8ec73170adb6abb0c3a2811eb46`.
- Round 35 jumps to overflowing pages from the sidebar and **⌥⇧← / ⌥⇧→**, then selects and frames
  those overflow boxes. Local gates passed 2 launcher tests and frontend lint/typecheck/121 tests plus
  the production build. The release audit scanned 128 candidate files and 698 historical blobs.
  Backend was unchanged from Round 31. GitHub CI run `31884703654` passed on
  `9005872fd41028d4c1f6eab81d9e80b8c25e267d`.
- Round 36 opens a job-queue item onto its page, frames overlay or leftover overflow boxes, and
  switches completed inpaint or preprocess items to the matching preview. Local gates passed 2
  launcher tests and frontend lint/typecheck/125 tests plus the production build. The release audit
  scanned 128 candidate files and 712 historical blobs. Backend was unchanged from Round 31. GitHub
  CI run `31885226463` passed on `1184c07e1cabeb8257fe60601584910536d4ef2a`.
- Round 37 frames overflow boxes from the sidebar **排版溢出** pill. Local gates passed 2 launcher
  tests and frontend lint/typecheck/127 tests plus the production build. The release audit scanned
  128 candidate files and 724 historical blobs. Backend was unchanged from Round 31. GitHub CI run
  `31885552346` passed on `ee182935b4916bd810ca38fd5b48b738e7e9258b`.
- Round 38 keeps **← / →** on the visible sidebar list. Under the overflow filter they skip hidden
  pages and frame overflowing boxes. Local gates passed 2 launcher tests and frontend
  lint/typecheck/129 tests plus the production build. The release audit scanned 128 candidate files
  and 735 historical blobs. Backend was unchanged from Round 31. GitHub CI run `31885919299` passed
  on `35e6293e0e5d242aaad5cad55530f4f080262626`.
- Round 39 frames the selected box from **⌥↓ / ⌥↑** and the inspector region list. Local gates passed
  2 launcher tests and frontend lint/typecheck/130 tests plus the production build. The release audit
  scanned 128 candidate files and 747 historical blobs. Backend was unchanged from Round 31. GitHub
  CI run `31886262454` passed on `d8e7c05467ebf9359f61defd534c526c9e02fc21`.
- Round 40 frames the current selection from **G** and the canvas **框住** control. Local gates passed
  2 launcher tests and frontend lint/typecheck/132 tests plus the production build. The release audit
  scanned 128 candidate files and 758 historical blobs. Backend was unchanged from Round 31. GitHub
  CI run `31886581607` passed on `520133a74f231a5464400e78ade7c8cf1b522dca`.
- Round 41 shows the sidebar page counter on the visible (filtered/search) list and disables **← / →**
  at the ends of that list. Local gates passed 2 launcher tests and frontend lint/typecheck/133 tests
  plus the production build. The release audit scanned 128 candidate files and 771 historical blobs.
  Backend was unchanged from Round 31. GitHub CI run `31886984640` passed on
  `a1031f1604a0cb8372fe130ac64b380a251df0a7`.
- Round 42 opens a failed queue item onto that page and the matching inspector, then closes the batch
  drawer. Local gates passed 2 launcher tests and frontend lint/typecheck/135 tests plus the
  production build. The release audit scanned 128 candidate files and 782 historical blobs. Backend
  was unchanged from Round 31. GitHub CI run `31888156798` passed on
  `ce2395e71f2029dc09c98d937a6ce7901672c1ae`.
- Round 43 shows the current page's processing failure in the inspector and offers a same-page retry.
  Local gates passed 2 launcher tests and frontend lint/typecheck/137 tests plus the production build.
  The release audit scanned 128 candidate files and 792 historical blobs. Backend was unchanged from
  Round 31. GitHub CI run `31888824260` passed on `4c7511695ba896bfe5834620b862ac669b4e74d9`.
- Round 44 opens the matching inspector when **← / →** move through the **失败 / 不可用** list. Local
  gates passed 2 launcher tests and frontend lint/typecheck/139 tests plus the production build. The
  release audit scanned 128 candidate files and 806 historical blobs. Backend was unchanged from
  Round 31. GitHub CI run `31889225201` passed on `17e21f25e73a76d6b421993b60476218c92557b7`.
- Round 45 opens the matching inspector when clicking a failed or unavailable sidebar page. Local
  gates passed 2 launcher tests and frontend lint/typecheck/141 tests plus the production build. The
  release audit scanned 128 candidate files and 817 historical blobs. Backend was unchanged from
  Round 31. GitHub CI run `31889559133` passed on `fc58b181d3a647fb7b9feb4c89341fd1f820966f`.
- Round 46 jumps to failed or unavailable pages with **⌥← / ⌥→** and opens the matching inspector.
  Local gates passed 2 launcher tests and frontend lint/typecheck/143 tests plus the production build.
  The release audit scanned 128 candidate files and 827 historical blobs. Backend was unchanged from
  Round 31. GitHub CI run `31923761102` passed on `3ffa0f79e017989bba11d56678b9a7ed2a4b2e55`.
- Round 47 retries a page processing failure from the inspector without opening the batch drawer.
  Local gates passed 2 launcher tests and frontend lint/typecheck/143 tests plus the production build.
  The release audit scanned 128 candidate files and 840 historical blobs. Backend was unchanged from
  Round 31. GitHub CI is pending after push.

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
