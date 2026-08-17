# Manga Localizer — local desktop application loop

把下面整段粘进 `/loop`（动态节奏，不要写 `25m`）：

```
Continue Manga Localizer from .agent/DESKTOP_LOOP_PROMPT.md and .agent/STATE.md. Sentinel AGENT_LOOP_WAKE_manga_desktop. Do not re-arm AGENT_LOOP_WAKE_manga_app or AGENT_LOOP_WAKE_manga_realdata. Do not arm a 25-minute fallback sleeper. Build a double-clickable local Mac application that supervises the existing workbench API, downloads checksum-verified models at package time, and bundles them inside the app. Then iterate the highest-priority missing product capability until the local app can run the full pipeline offline. Commit and push only public files to origin/main. Never commit tests/real-data, model weights, or private OCR/artwork.
```

This file is the live `/loop` prompt. Dynamic schedule. Sentinel:
`AGENT_LOOP_WAKE_manga_desktop`.

This is not Goal Mode. Persistence is `/loop` only.

## Objective

Replace the “open a webpage / Chrome `--app=` tab” prototype with a **local
Mac application** the user can launch like other desktop software. Required
models must be **downloaded once at package/setup time, checksum-verified, and
copied into the application bundle**. After that foundation exists, keep
iterating the highest-priority missing capability until the local app can run
the full localization pipeline offline.

`npm run app` (uvicorn + Chromium `--app=`) is evidence of the current
prototype, not the destination.

## Destination

A double-clickable `Manga Localizer.app` (or equivalent) on this Mac that:

1. Starts the local FastAPI workbench on loopback without asking the user to
   run terminal commands.
2. Opens a dedicated application window (not a normal browser tab).
3. Serves the built frontend from inside the bundle.
4. Finds bundled PP-OCR, LaMa, Real-ESRGAN anime, and Argos ja→zh weights
   without a separate `npm run setup:models` after install.
5. Reports honest unavailable health if a bundled file is missing or the
   checksum fails. Never download models during ordinary application startup
   when the bundle already has them.
6. Keeps LAN / iPhone companion explicit (`app:lan` or an in-app toggle),
   default bind `127.0.0.1`.

Choose the smallest coherent shell that can ship this: extend
`scripts/app.mjs` / `scripts/app-platform.mjs` into a real `.app` wrapper
(pywebview, Briefcase, a signed-ready app skeleton, or an equivalent). Do not
rewrite the React/FastAPI workbench as a new framework. Do not start from
Electron/Tauri unless the existing launcher cannot become a real app.

## Models and runtimes (bundle, do not commit)

Use `scripts/setup_optional_models.py` and the existing SHA-256 specs. Package
time may download; git must not store weights.

Required bundle set for a complete local app:

- `ppocr` — PP-OCRv3 detection ONNX
- `lama` — LaMa inpaint ONNX
- `realesrgan` — RealESRGAN anime 4× ONNX
- `argos-ja-zh` — Argos ja→en and en→zh packages
- backend extras `ai` and `mt` inside the app runtime
- Tesseract with `jpn` / `jpn_vert` (bundle or a documented first-run install
  that the app can perform once, still checksummed, still not at every launch)

Never `git add` ONNX/Argos/Tesseract data, `.manga-localizer/`, or
`tests/real-data/`. Public docs may name the files, licenses, and hashes.

## Product constraints

- Processing stays on the Mac. Do not rewrite OCR/inpaint onto iOS.
- Default API remains loopback. LAN companion stays explicit.
- Do not wait for reversible naming, icon, or window-chrome details. The user
  verifies by launching the app.
- Do not auto-accept empty pages. Do not claim product Round 8 complete.
- Do not claim App Store, notarized, or signed distribution. A local `.app`
  that runs on this machine is enough.
- Do not force-push or rewrite published history.
- Do not re-arm `AGENT_LOOP_WAKE_manga_app` or
  `AGENT_LOOP_WAKE_manga_realdata`. Late wakes for those sentinels: skip
  rewrite, do not resume those old loops.

## Round procedure

1. Read `.agent/STATE.md`, this prompt, `git status`, and the task-owned diff.
   Treat pre-existing unrelated WIP as protected.
2. Pick the single highest-priority unblocked gap in this order:
   1. Double-clickable local app process/window (no manual `uvicorn` / browser)
   2. Package-time model download + copy into the app, with checksum tests
   3. App launch finds bundled models and enables PP-OCR / LaMa /
      Real-ESRGAN / Argos without a separate developer setup
   4. Highest-priority missing or broken workbench capability that blocks
      offline localization (detection enclosure, OCR, translation, repair,
      typeset, export, review UX). Prefer a defect the current app actually
      cannot do over speculative rewrites.
3. Implement the smallest complete public change. Remove superseded prototype
   paths in the same round when they are truly replaced.
4. Verify with the closest tests, then `npm run check` / `audit:release` as
   appropriate. Launch or smoke the local app when the round touches
   packaging. Do not open private JPEG/PNG with vision tools.
5. Update `.agent/STATE.md` (and routed public docs only when the decision or
   evidence changed). Private notes stay under `tests/real-data/` if needed.
6. Commit **public** files only. Fast-forward merge onto `main` when safe.
   Push `origin/main` for public commits. Never push model weights or private
   manga.

## Wake / stop

- Primary wake: a public-fix push’s CI completing, or a local package/app
  build finishing. Use `AGENT_LOOP_WAKE_manga_desktop` and
  `json.loads(..., strict=False)`.
- Do not arm a 25-minute fallback sleeper. If a heartbeat is required, use
  an event watcher first; otherwise wait for the next `/loop` invocation.
- On user stop: kill watcher PIDs, do not re-arm.

## Done for this loop

Stop and leave NEEDS_USER only when all of the following are true:

- A local application launches without a developer terminal workflow.
- Bundled models are present, checksum-verified, and selected providers are
  available in the running app.
- The workbench can complete an offline page path: import → detect → OCR →
  review → translate (local/manual) → inpaint → typeset → export, with
  remaining gaps recorded as honest human/quality gates rather than missing
  software.
- Public tests/gates for the task-owned change passed.
- No private tree was committed.

Until the user does a unified visual check of the private books, Round 8
stays incomplete.
