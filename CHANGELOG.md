# Changelog

All notable changes are documented here. The format follows Keep a Changelog and the project uses
Semantic Versioning.

## [Unreleased]

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
