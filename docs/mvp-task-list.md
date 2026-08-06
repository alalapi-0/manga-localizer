# MVP task list

The implementation order deliberately makes V0.1 useful before V0.2 rendering quality is complete.

## V0.1 — OCR review workbench

- [ ] Create/open a portable project and autosave SQLite + JSON state
- [ ] Import one image, many images, or a nested folder without mutating sources
- [ ] Preserve Unicode relative paths and render thumbnails/statuses
- [ ] Display a desktop three-pane workbench and image canvas
- [ ] Create, select, move, resize, rotate, merge, split, ignore, and delete text regions
- [ ] Support safe keyboard navigation, undo, redo, and manual save
- [ ] Detect/recognize Japanese text with a real Tesseract provider
- [ ] Edit Japanese and Chinese text, confidence, type, direction, and reading order
- [ ] Export original/translated text JSON alongside the original relative tree
- [ ] Persist revision history and recover interrupted queue state

## V0.2 — localization workbench

- [ ] Manual, deterministic mock, local dictionary, and OpenAI-compatible translators
- [ ] Limited neighboring/page context, glossary, character names, and privacy disclosure
- [ ] OpenCV mask generation and inpainting with editable region settings
- [ ] Horizontal/vertical Chinese typesetting with system fonts, fit, wrapping, stroke, and overflow
- [ ] Original / inpainted / typeset preview modes and comparison view
- [ ] Non-blocking batch OCR, translation, inpainting, typesetting, retry, pause, resume, and cancel
- [ ] Safe single/batch export preserving directories and resolving conflicts
- [ ] Backend, frontend, and Playwright end-to-end tests using generated copyright-safe fixtures
- [ ] Cross-platform start instructions, architecture/privacy/provider docs, CI, and community files
- [ ] Sensitive-information scan, clean initial commit, and authorized public GitHub publication

## Explicitly deferred

- Deep-learning inpainting, artistic sound-effect redraw, automatic font matching
- Fully automatic speech-bubble detection, whole-book character reasoning
- PDF/EPUB ingestion, native installers, cloud sync, and collaboration
