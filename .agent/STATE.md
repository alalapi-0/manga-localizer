# Manga Localizer — Project State

Updated: 2026-08-06

## Goal

Deliver a local-first, desktop-oriented Web workbench for manga text detection, Japanese OCR,
reading order, Japanese-to-Chinese translation, review, inpainting, typesetting, and safe batch
export. V0.1 establishes a useful OCR review loop; V0.2 completes the basic localization loop.

## Current round

Round 9 — verified 0.2.0 release snapshot and governed publication handoff.

## Environment evidence

- macOS on Apple Silicon (M4, Metal available).
- Node.js 26 and npm 11 available.
- uv available with CPython 3.12; the project targets Python 3.12 for mature ML/image wheels.
- Tesseract 5.5 is installed with `jpn`, `jpn_vert`, `chi_sim`, and `chi_tra` data.
- Git and authenticated GitHub CLI are available.
- The target directory was absent before this task; sibling repositories remain protected and untouched.

## Decisions

- Repository/distribution name: `manga-localizer`; Python import package: `manga_localizer`.
- Frontend: React, TypeScript, Vite, Zustand, React Konva, dense custom CSS tokens.
- Backend: FastAPI, Pydantic, SQLAlchemy/SQLite, Pillow, OpenCV, background asyncio workers.
- Default OCR: external Tesseract through a provider adapter; planned MangaOCR/PaddleOCR adapters are
  not implemented in this candidate and must remain optional when added.
- Tesseract TSV detection falls back to OpenCV contour grouping when full-page Japanese layout does
  not yield editable regions; all resulting OCR remains reviewable rather than presented as certain.
- Projects are portable: each output root contains `project/project.sqlite3` and a sanitized
  `project/project.json`; a local catalog only remembers recently opened project manifests.
- Uploaded browser files are copied into project-owned source storage and treated as read-only.
- Secrets are environment- or session-only and are never written to project JSON, SQLite, or logs.

## Protected boundaries

- Do not modify sibling projects or workspace-level control files.
- Never overwrite imported source images.
- Do not commit user images, output images, databases, environment files, credentials, model caches,
  downloaded model weights, or copyrighted fonts.
- Do not send images remotely. Text is sent remotely only when the user selects a remote translator.

## Completion ledger

- [x] Target isolation and toolchain survey
- [x] Local Git repository initialized on `main`
- [x] Architecture and task records finalized
- [x] Rounds 1–2: foundation and project/image management
- [x] Rounds 3–5: workbench, OCR, and translation
- [x] Rounds 6–7: inpainting, typesetting, queue, and export
- [x] Round 8: automated verification and browser walkthrough
- [x] Round 9: local open-source documentation, exact-candidate verification, and release scans
- [x] Round 9: clean-release handoff for final commit, public repository creation, and push

## Latest verification evidence

- Unified `npm run check`: 2 launcher tests passed; backend Ruff lint/format and 78 pytest cases passed;
  frontend ESLint/TypeScript, 39 Vitest cases, and the production Vite build passed.
- End to end: two Chromium scenarios passed, including project reopen and the complete real
  detection → OCR → review → inpaint → typeset → export path while preserving source checksums.
- Browser walkthrough: generated Unicode folder imported; real OCR produced three editable regions;
  autosave/reload, dark/light themes, comparison shortcut, 1440×900, and 1280×720 were verified with
  no browser console warnings or errors. The redistributable screenshot is in `docs/assets/`.
- Unified launcher: `npm run dev` served the root page and API; direct health and the Vite `/api` proxy
  passed with temporary root `.env` port overrides, then the ignored test file was removed.
- Security/dependency checks: both npm audits and `pip-audit` reported 0 known vulnerabilities; the
  release audit scanned 94 candidate files plus Git history with 0 findings.

## Blockers

None for the verified release snapshot. Remote translation live tests remain opt-in and are not required
for offline acceptance. Commit, tag, remote, and CI state are delivery metadata and must be verified from
Git and GitHub rather than inferred from this source-tree snapshot.
