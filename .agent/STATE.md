# Manga Localizer — Project State

Updated: 2026-08-06

## Goal

Deliver a local-first, desktop-oriented Web workbench for manga text detection, Japanese OCR,
reading order, Japanese-to-Chinese translation, review, inpainting, typesetting, and safe batch
export. V0.1 establishes a useful OCR review loop; V0.2 completes the basic localization loop.

## Current round

Rounds 1–2 — repository foundation and project/image management.

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
- Default OCR: external Tesseract through a provider adapter; advanced MangaOCR/Paddle adapters stay optional.
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
- [ ] Rounds 1–2: foundation and project/image management
- [ ] Rounds 3–5: workbench, OCR, and translation
- [ ] Rounds 6–7: inpainting, typesetting, queue, and export
- [ ] Round 8: automated verification and browser walkthrough
- [ ] Round 9: open-source documentation, scan, commit, public repository, and push

## Blockers

None. Remote translation live tests remain opt-in and are not required for offline acceptance.
