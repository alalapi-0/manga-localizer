# Manga Localizer — computer-use UI evaluation loop (superseded)

**Superseded on 2026-08-18.** Do not use this file as a live `/loop`
prompt. Do not re-arm `AGENT_LOOP_WAKE_manga_ui`. Late wakes skip rewrite
and do not resume this loop.

The live loop is `.agent/PAGE_LOOP_PROMPT.md` with sentinel
`AGENT_LOOP_WAKE_manga_page`.

# Manga Localizer — computer-use UI evaluation loop

把下面整段粘进 `/loop`（动态节奏，不要写 `25m`）：

```
Continue Manga Localizer from .agent/UI_LOOP_PROMPT.md and .agent/STATE.md. Sentinel AGENT_LOOP_WAKE_manga_ui. Do not re-arm AGENT_LOOP_WAKE_manga_desktop, AGENT_LOOP_WAKE_manga_app, or AGENT_LOOP_WAKE_manga_realdata. Do not arm a 25-minute fallback sleeper. Operate the live local app as the user with computer use: drive the workbench UI, walk the offline pipeline, write a problem report for every real defect, then fix from that report. Repeat until a computer-use pass finds no product defects. Commit and push only public files to origin/main. Never commit tests/real-data, model weights, or private OCR/artwork.
```

This file is the live `/loop` prompt. Dynamic schedule. Sentinel:
`AGENT_LOOP_WAKE_manga_ui`.

This is not Goal Mode. Persistence is `/loop` only.

`.agent/DESKTOP_LOOP_PROMPT.md` is superseded. Late wakes for
`AGENT_LOOP_WAKE_manga_desktop`, `AGENT_LOOP_WAKE_manga_app`, or
`AGENT_LOOP_WAKE_manga_realdata` skip rewrite and do not resume those loops.

## Objective

Stand in for the user. Use computer-use tools (Playwright and/or Chrome
DevTools against the same origin the Mac app serves, normally
`http://127.0.0.1:8000`) to **actually click, type, import, run jobs, and
inspect the workbench**. Unit tests and `/api/health` are supporting
evidence, not a substitute for operating the UI.

Each round: drive the product → record what broke → fix from that report →
drive the same path again. Continue until a full computer-use pass finds no
product defects.

## Destination

A local `Manga Localizer.app` (or the same workbench on loopback) that a
person can use offline:

1. Launch without a developer terminal.
2. Create or reopen a project from the UI.
3. Import pages from the UI.
4. Run detect → OCR → review/confirm → local/manual translate → inpaint →
   typeset → export from the workbench, not from ad-hoc scripts.
5. Honest unavailable/error UI when a provider or artifact is missing.
   Never download models at ordinary startup.

LAN / iPhone companion stays explicit. Default bind remains `127.0.0.1`.

## Computer-use rules

- Prefer the live app window’s origin. If the `.app` is not running, start
  `Manga Localizer.app` or the packaged wrapper; do not ask the user to run
  `uvicorn` by hand.
- Drive visible controls: sidebar, canvas, inspector, batch drawer, export.
  Follow on-screen Chinese labels (**单图 / 多图 / 文件夹 / 文本 / 修复 /
  排版 / 项目 / 处理**).
- Use accessibility/DOM snapshots as the primary observation. Do not send
  private manga JPEG/PNG pixels to vision models. Public synthetic fixtures
  are the default computer-use corpus.
- If a private project is already open, operate it through DOM/controls
  only. Do not copy private pages, OCR text, or artwork into public reports,
  git, or chat dumps.
- Do not auto-accept empty pages. Do not claim product Round 8 complete.
- A Playwright e2e that never opens the real window does not count as this
  loop’s computer-use pass.

## Problem report

When the operator cannot complete a user-visible step, write or update
`.agent/UI_PROBLEM_REPORT.md` before fixing. Each finding needs:

- What the user did (control, page role, expected next state)
- What happened (UI text, disabled control, job failure, missing preview)
- Why it blocks the offline path
- Repro without private image bytes, OCR text, or absolute personal paths

Clear a finding only after a later computer-use pass shows the same step
works. An empty report with a dated “pass” section is the stop condition
for defects, not for Round 8 visual quality.

Private operator notes, if needed, stay under ignored `tests/real-data/`.

## Product constraints

- Processing stays on the Mac. Do not rewrite OCR/inpaint onto iOS.
- Do not wait for reversible naming, icon, or window-chrome details.
- Do not start from Electron/Tauri unless the existing `.app` cannot host
  the workbench.
- Do not force-push or rewrite published history.
- Never `git add` ONNX/Argos/Tesseract data, `.manga-localizer/`,
  `tests/real-data/`, or the built `.app`.

## Round procedure

1. Read `.agent/STATE.md`, this prompt, the current
   `.agent/UI_PROBLEM_REPORT.md`, `git status`, and the task-owned diff.
   Treat unrelated WIP as protected.
2. If the app/API is down, launch the local app, then confirm the workbench
   origin responds.
3. Computer-use the highest-priority unfinished user path:
   launch → create/open project → import → detect → OCR → review →
   translate (local/manual) → inpaint → typeset → export.
   Prefer re-checking open report items before exploring new chrome.
4. If the path fails, append a finding to the problem report. If it
   succeeds, record a short pass note and pick the next broken control.
5. Fix the smallest complete public change for the current finding.
   Remove superseded prototype paths in the same round when they are truly
   replaced.
6. Re-run the same computer-use step, then the closest tests, then
   `npm run check` / `audit:release` as appropriate.
7. Update `.agent/STATE.md` and routed public docs only when the decision
   or evidence changed.
8. Commit **public** files only. Fast-forward onto `main` when safe. Push
   `origin/main` for public commits.

## Wake / stop

- Primary wake: a computer-use pass finishing (report updated), or a
  public-fix push’s CI completing. Use `AGENT_LOOP_WAKE_manga_ui` and
  `json.loads(..., strict=False)`.
- Do not arm a 25-minute fallback sleeper. If a heartbeat is required, use
  an event watcher first; otherwise wait for the next `/loop` invocation.
- On user stop: kill watcher PIDs, do not re-arm.
- Do not re-arm `AGENT_LOOP_WAKE_manga_desktop`,
  `AGENT_LOOP_WAKE_manga_app`, or `AGENT_LOOP_WAKE_manga_realdata`.

## Done for this loop

Stop and leave NEEDS_USER only when all of the following are true:

- A computer-use pass completed the offline page path in the live UI
  without a new product defect.
- `.agent/UI_PROBLEM_REPORT.md` has no open findings.
- Remaining gaps are honest human/quality gates (empty-page review, font
  fit, restoration taste), not missing or broken software.
- Public tests/gates for the task-owned change passed.
- No private tree was committed.

Until the user does a unified visual check of the private books, Round 8
stays incomplete.
