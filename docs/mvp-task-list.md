# MVP task list

The implementation order deliberately makes V0.1 useful before V0.2 rendering quality is complete.
Checked items are implemented in the current tree; runtime verification and release readiness are
reported separately rather than implied by this list.

## V0.1 — OCR review workbench

- [x] Create/open a project and autosave SQLite plus sanitized JSON state
- [x] Import one image, many images, or a nested folder without mutating sources
- [x] Preserve Unicode relative paths and render thumbnails/statuses
- [x] Display a desktop three-pane workbench and image canvas
- [x] Create, select, move, resize, rotate, merge, split, ignore, and delete rectangular text regions
- [x] Support safe keyboard navigation, undo, redo, and manual save
- [x] Detect/recognize Japanese text with a real Tesseract provider
- [x] Edit Japanese and Chinese text, confidence, type, direction, and reading order
- [x] Export original/translated text JSON alongside the original relative tree
- [x] Persist revision history and recover interrupted queue state

## V0.2 — localization workbench

- [x] Manual, deterministic mock, local dictionary, and OpenAI-compatible translators
- [x] Bounded same-page reading-order context, glossary, character names, and privacy disclosure
- [x] OpenCV rectangular-mask generation and inpainting with editable region settings
- [x] Horizontal/vertical Chinese typesetting with system fonts, fit, wrapping, stroke, and overflow
- [x] Original / inpainted / typeset preview modes and comparison view
- [x] Non-blocking batch OCR, translation, inpainting, typesetting, retry, pause, resume, and cancel
- [x] Safe single/batch export preserving directories and resolving conflicts
- [x] Backend, frontend, and Playwright end-to-end test suites using generated copyright-safe fixtures
- [x] Cross-platform start instructions, architecture/privacy/provider docs, CI, and community files
- [x] Sensitive-information scan tooling
- [x] Cumulative import boundaries and NFKC/case-folded cross-platform conflict handling
- [x] Revision-guarded autosave rebase and cooperative cancel/restart recovery
- [x] Atomic export bundle finalization with owner-marker and SQLite-sidecar recovery
- [x] Exact local candidate verification: launcher 2, backend 78, frontend 39, Playwright 2
- [x] Two npm audits, `pip-audit`, and 94-candidate-file plus Git-history release audit with zero findings
- [x] Clean-release metadata and governed GitHub publication handoff

Commit, tag, remote, and CI status are delivery metadata and should be verified from Git and GitHub,
not inferred from this tracked checklist.

## Unreleased real-data iteration

- [x] Persist bounded per-region mask brush/eraser edits and apply them to generated masks
- [x] Persist preprocess/inpaint/typeset accept/reject decisions across reopen
- [x] Bind visual reviews to generated artifact/mask checksums and gate generated-image export
- [x] Exclude unreviewed or stale generated artifacts from portable project bundles
- [x] Verify and deliver the post-OCR trust/disposition candidate: versioned detector/OCR evidence,
  fail-closed automatic proposals, relevant-input invalidation, and trusted-only translation/safe rendering
- [x] Deliver a runnable local Real-ESRGAN AI upscaler with explicit checksummed install, honest
  classic-vs-AI labeling, and a private comparison script
- [x] Deliver line-art-aware inpainting candidates with mask-outside preservation, grayscale LaMa
  output, local compare/select/accept, and a synthetic comparison script
- [x] Deliver a local Japanese-to-Chinese translator with checksummed Argos packages, no startup
  download, and a public synthetic comparison script
- [x] Deliver a privacy-safe detector-draft accept/reject promotion CLI that does not auto-review
  pages or print OCR text
- [x] Persist typesetting overflow on each page, surface it in the workbench, and skip reviewed pages
  during keyboard review
- [x] Suggest a per-page preprocessing profile from local image stats, with an explicit apply-to-page
  action that does not change the book-wide default
- [x] Map CJK punctuation to vertical presentation forms during vertical typesetting
- [x] Pack adjacent small typesetting boxes as fragment clusters without rewriting stored translation
  text
- [x] Retypeset overflowing boxes only, and select those boxes from the inspector overflow notice
- [x] Retypeset the currently selected box from the typesetting inspector
- [x] Overlay selected typeset boxes onto the last plate without redrawing untouched boxes
- [x] Redraw the whole page when a region-scoped typeset has no last typeset plate
- [x] Show overlay vs full-page typeset counts on the job queue card

## Explicitly deferred

- Deep-learning inpainting, artistic sound-effect redraw, automatic font matching
- Fully automatic speech-bubble detection, whole-book character reasoning
- Arbitrary polygon regions, whole-page raster mask editing, and JSON-only project import
- MangaOCR and PaddleOCR provider adapters
- PDF/EPUB ingestion, native installers, cloud sync, and collaboration
