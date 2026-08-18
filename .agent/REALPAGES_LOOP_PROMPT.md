# Manga Localizer — real-page computer-use evaluation loop

把下面整段粘进 `/loop`（动态节奏，不要写 `25m`）：

```
Continue Manga Localizer from .agent/REALPAGES_LOOP_PROMPT.md and .agent/STATE.md. Sentinel AGENT_LOOP_WAKE_manga_realpages. Do not re-arm AGENT_LOOP_WAKE_manga_ui, AGENT_LOOP_WAKE_manga_desktop, AGENT_LOOP_WAKE_manga_app, or AGENT_LOOP_WAKE_manga_realdata. Do not arm a 25-minute fallback sleeper. Stop waiting for human review, empty-page sign-off, naming nits, or push-approval cards. Use computer-use on this Mac to click the live workbench. Evaluate real manga pages for both pipeline function and whether a person can operate a clean, readable layout. Write findings to .agent/REALPAGES_PROBLEM_REPORT.md, then fix from that report. Repeat until a real-page computer-use pass finds no product, interaction, or layout defects. If a tool or optional model is missing, install it locally as the operator. Commit only public files. Never commit tests/real-data, model weights, or private OCR/artwork.
```

This file is the live `/loop` prompt. Dynamic schedule. Sentinel:
`AGENT_LOOP_WAKE_manga_realpages`.

This is not Goal Mode. Persistence is `/loop` only.

`.agent/UI_LOOP_PROMPT.md`, `.agent/DESKTOP_LOOP_PROMPT.md`, and
`.agent/REALDATA_LOOP_PROMPT.md` are superseded. Late wakes for
`AGENT_LOOP_WAKE_manga_ui`, `AGENT_LOOP_WAKE_manga_desktop`,
`AGENT_LOOP_WAKE_manga_app`, or `AGENT_LOOP_WAKE_manga_realdata` skip
rewrite and do not resume those loops.

## Objective

Stand in for the user on this Mac. Open the real workbench, click real
controls, and walk real manga pages. Unit tests and `/api/health` are
supporting evidence, not a substitute.

Each round covers **both**:

1. **Function** — import, detect, OCR, review/confirm, local/manual
   translate, inpaint, typeset, accept, export.
2. **Use and layout** — can a person find the next action; are controls
   visible on a typical window; is the chrome dense, clipped, overlapping,
   or visually noisy.

Drive the product → write the report → fix → drive the same path again.
Continue until a full real-page computer-use pass finds no product,
interaction, or layout defects.

## Destination

A local `Manga Localizer.app` (or the same workbench on loopback) that a
person can use offline on real books:

1. Launch without a developer terminal.
2. Reopen the existing real-data projects from the UI.
3. Process and review real pages from the workbench, not ad-hoc scripts.
4. Honest unavailable UI when a provider is missing, then the operator
   installs the missing extra/model and continues.
5. A layout a person can scan: next action is obvious, review groups stay
   visible, drawers do not bury the page, typical widths do not hide
   required controls.

LAN / iPhone companion stays explicit. Default bind remains `127.0.0.1`.

## Corpus

Prefer already-imported private catalog projects (the manga01 / manga02
slices and any full-book project on this machine). If none is open, reopen
one from the project switcher or `project/project.json`. Do not invent a
synthetic substitute when real pages are available.

Do not auto-accept empty pages. Skip them, record the count, and keep
working on pages that have text. That skip is not a stop. Do not claim
product Round 8 complete.

## Computer-use

- Prefer the live app window origin, normally `http://127.0.0.1:8000`.
  If the API or window is down, launch `Manga Localizer.app` or the
  packaged wrapper. If that cannot stay up, start the loopback API and
  built frontend yourself. Do not ask the user to run commands.
- Click visible Chinese labels: **单图 / 多图 / 文件夹 / 文本 / 修复 /
  排版 / 项目 / 批处理与导出 / 擦除 / 成品 / 增强 / 接受**.
- Primary observation is the accessibility/DOM tree plus local screenshots
  the operator inspects on this machine. Layout and beauty judgments need
  those screenshots. Do not copy private page pixels, OCR text, or artwork
  into git, public reports, or chat dumps.
- Public report language stays anonymous: control names, viewport width,
  disabled reasons, job status. No transcriptions, filenames that identify
  a book, or personal absolute paths.
- Private operator notes, if needed, stay under ignored `tests/real-data/`.
- A Playwright e2e that never opens the real window does not count.

## Do not stall

These are not stop signals:

- Empty-page human review
- Font-fit or restoration taste on a single balloon
- Reversible naming, icon, or window-chrome nits
- Cursor approval cards for `git push` or a single export click
- Waiting for the user to confirm a default that this prompt already sets

If an auto-review card appears, request approval once and immediately
continue other unblocked local work. Do not idle on the card. Public
commits stay local if push is blocked; keep fixing.

Do not stop to ask whether to continue. Pick the next open finding or the
next real page.

## Missing tools and models

Ordinary app startup still must not download weights. The **loop operator**
may install what the machine lacks, then continue:

- `npm run setup:ai` and `npm run setup:models` for PP-OCR / LaMa /
  Real-ESRGAN
- `npm run setup:mt` and Argos ja→zh packages
- Tesseract with `jpn` / `jpn_vert`
- Playwright / Chromium, or the packaged `.app`, so computer-use can click

Record honest unavailable UI first, then install, then re-check the same
control. Do not leave a provider permanently unused because extras were
not in the current venv.

## Problem report

Live report: `.agent/REALPAGES_PROBLEM_REPORT.md`.

Write or update it before fixing. Each finding needs:

- Kind: `function` or `ux-layout`
- What the operator clicked (control, page role, expected next state)
- What happened (disabled control, job failure, clipped chrome, unclear
  next action, visual clutter)
- Why it blocks a person using a real book
- Repro without private image bytes, OCR text, or personal paths

Clear a finding only after a later real-page computer-use pass shows the
same step works and the chrome is usable. An empty open-findings list
plus a dated pass is the software/UX stop. Round 8 visual taste is
separate.

## Product constraints

- Processing stays on the Mac. Do not rewrite OCR/inpaint onto iOS.
- Do not force-push or rewrite published history.
- Never `git add` ONNX/Argos/Tesseract data, `.manga-localizer/`,
  `tests/real-data/`, or the built `.app`.

## Round procedure

1. Read `.agent/STATE.md`, this prompt, `.agent/REALPAGES_PROBLEM_REPORT.md`,
   `git status`, and the task-owned diff. Treat unrelated WIP as protected.
2. If the app/API is down, launch it and confirm the workbench origin.
3. Install any missing operator tool/model, then computer-use the highest
   priority unfinished real-page path or open finding.
4. Append function and ux-layout findings before fixing.
5. Fix the smallest complete public change. Remove superseded chrome in
   the same round when it is truly replaced.
6. Re-run the same real-page step, then the closest tests, then
   `npm run check` as appropriate.
7. Update `.agent/STATE.md` and routed public docs only when the decision
   or evidence changed.
8. Commit **public** files only. Push `origin/main` when it is unblocked;
   if push waits on a card, continue local work.

## Wake / stop

- Primary wake: a computer-use pass finishing (report updated), or a
  public-fix push’s CI completing. Use `AGENT_LOOP_WAKE_manga_realpages`
  and `json.loads(..., strict=False)`.
- Do not arm a 25-minute fallback sleeper.
- On user stop: kill watcher PIDs and the loopback API this loop started;
  do not re-arm.
- Do not re-arm `AGENT_LOOP_WAKE_manga_ui`,
  `AGENT_LOOP_WAKE_manga_desktop`, `AGENT_LOOP_WAKE_manga_app`, or
  `AGENT_LOOP_WAKE_manga_realdata`.

## Done for this loop

Stop and leave NEEDS_USER only when all of the following are true:

- A computer-use pass walked real pages through the offline path without a
  new function or ux-layout defect.
- `.agent/REALPAGES_PROBLEM_REPORT.md` has no open findings.
- Remaining gaps are honest human taste (empty-page accept, font fit,
  restoration preference), not missing software or unusable chrome.
- Public tests/gates for the task-owned change passed.
- No private tree was committed.

Until a unified visual check of the private books is done, Round 8 stays
incomplete.
