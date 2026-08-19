# Manga Localizer — one-page full-reprocess loop

把下面整段粘进 `/loop`（动态节奏，不要写 `25m`）：

```
Continue Manga Localizer from .agent/PAGE_LOOP_PROMPT.md and .agent/STATE.md. Sentinel AGENT_LOOP_WAKE_manga_page. Do not re-arm AGENT_LOOP_WAKE_manga_realpages, AGENT_LOOP_WAKE_manga_ui, AGENT_LOOP_WAKE_manga_desktop, AGENT_LOOP_WAKE_manga_app, or AGENT_LOOP_WAKE_manga_realdata. Do not arm a 25-minute fallback sleeper. Stop every leftover project worker, queue, watcher, and loopback API before touching a page. Reprocess every real uploaded image from the start. One image equals one round. Do not start the next image until the current image is fully done. No-text pages: AI upscale and AI redraw only, repeat until the page no longer looks low quality. Text pages: the same quality pass first, then detect, OCR, inpaint, translate, and typeset. On any product problem, write .agent/PAGE_PROBLEM_REPORT.md and fix from that report before finishing the page. After each finished page, commit only public files and push origin/main. Never commit tests/real-data, model weights, or private OCR/artwork.
```

This file is the live `/loop` prompt. Dynamic schedule. Sentinel:
`AGENT_LOOP_WAKE_manga_page`.

This is not Goal Mode. Persistence is `/loop` only.

`.agent/REALPAGES_LOOP_PROMPT.md`, `.agent/UI_LOOP_PROMPT.md`,
`.agent/DESKTOP_LOOP_PROMPT.md`, and `.agent/REALDATA_LOOP_PROMPT.md`
are superseded. Late wakes for `AGENT_LOOP_WAKE_manga_realpages`,
`AGENT_LOOP_WAKE_manga_ui`, `AGENT_LOOP_WAKE_manga_desktop`,
`AGENT_LOOP_WAKE_manga_app`, or `AGENT_LOOP_WAKE_manga_realdata` skip
rewrite and do not resume those loops.

## Objective

Stand in for the user on this Mac. Reprocess **every real uploaded
page**, **one page per round**, until the whole corpus is finished.

A round is done only when that one page has finished its required path
and the public result is on `origin/main`. Then start the next page.

## Destination

All real catalog pages are visually acceptable and fully handled:

1. Photographed / low-quality pages first get **AI 超分** and **AI 重绘**
   until the operator no longer sees an obviously low-quality capture.
2. Pages with no text stop after that quality pass.
3. Pages with text then get **扣字 / 翻译 / 嵌字** (detect → OCR →
   confirm → translate → inpaint → typeset → accept → export).
4. Product defects go to the report, then get fixed, then the same page
   is finished before the next page starts.

LAN / iPhone companion stays explicit. Default bind remains `127.0.0.1`.

## Corpus

Process real uploaded catalog projects only, in this order:

1. The 130-page full book currently named `round8-clean-plate-final`.
2. Any remaining real book that is still unfinished after that book.
   The 27-page manga02 slice is next if it still needs visual quality
   or review.

Skip leftover synthetic catalog entries (`ui-loop-synthetic`,
`ui-loop-ui3`). They are not user uploads.

**Reprocess from the first sidebar page.** Earlier “已检查” pages are
not a skip. The 30-page photographed slice is not “the book.”

Identify pages in public notes by sidebar index and pixel size only.
Never write book-identifying filenames, OCR text, or personal paths.

## Hard stop before work

At the start of this loop, and again if leftover work appears:

1. Cancel every workbench queue / batch job.
2. Kill loopback API / `uvicorn` this loop did not need to keep.
3. Kill watchers for old sentinels. Do not re-arm them.
4. Do not start the next page while any previous page job is still
   queued, running, or blocked on a defect.

Do not arm a 25-minute fallback sleeper.

## One image = one round

- Open exactly one page. Finish it. Then the next page.
- Do not batch multiple pages in one round.
- Do not queue detect/OCR/translate/inpaint/typeset/export for any
  page except the current one.
- Quality retries (extra 超分 / AI 重绘) stay on the **same** page
  and count as the same round.
- A software fix for a defect found on this page also stays in this
  round. Re-run the failed step on **this** page until it passes.

## Page path

Always start with quality. Use the live workbench, not ad-hoc scripts.

### 1. Shared quality pass (every page)

1. Open the page. Look at **原图**.
2. Run local **AI 超分** / 图片增强 with Real-ESRGAN ONNX when the
   short side is small, the scan is soft, or the capture looks low
   quality. Prefer **按建议处理本页** or an explicit current-page
   **图片增强** job, then **AI 重绘本页**.
3. Compare **原图** and **增强**. Repeat 超分 / AI 重绘 on this page
   until the operator judges it no longer looks like an obviously
   low-quality capture, or until another pass no longer improves it.
4. If Real-ESRGAN is `[不可用]`, install the local `ai` extra and
   anime ONNX weights as the operator, then continue. Honest
   unavailable UI is not a skip.

### 2. No-text page

After the quality pass:

1. Run current-page detect+OCR once if the page has leftover empty
   boxes or has never been scanned on the enhanced image.
2. Ignore clothing-print / empty / non-text false boxes.
3. If there is still no real text, click **确认本页无文字** only
   after that visual check. Do not click it on a page that still
   looks like it has unread text.
4. JSON **安全导出** is optional on a no-text page. Do not export
   images that fail the visual-accept gate.

This page is then complete. Do not run 翻译 / 擦字 / 嵌字.

### 3. Text page

After the quality pass, on the enhanced page:

1. Current-page **文字检测** + **日文 OCR**.
2. Confirm real CJK boxes. Ignore empty, non-text, and boxes that
   sit on artwork / faces / clothes.
3. **翻译** (Argos 本地日→中). Re-confirm boxes that drop trust.
   If Argos writes blank, keep or enter a short operator translation;
   do not leave a trusted box empty when typeset needs text.
4. **擦字修复** (LaMa) then **嵌字排版**.
5. Open **成品**, judge as the user. **接受** only when text sits
   in balloons / SFX and does not stamp faces or clothes. **拒绝**
   and ignore the bad boxes, then re-run 擦字 / 嵌字.
6. **标记本页已检查**. **安全导出** → **仅文本 JSON** for this
   page only.

## Problem report

Live report: `.agent/PAGE_PROBLEM_REPORT.md`.

If anything blocks finishing the current page — job failure, missing
provider, unusable control, layout that hides the next action, quality
pass that cannot run, typeset that cannot be accepted even after
ignoring bad boxes — write the finding **before** changing code.

Each finding needs:

- Kind: `function` or `ux-layout`
- Current page as sidebar index + pixel size only
- What was clicked and what was expected
- What happened
- Why it blocks finishing this page
- Repro without private image bytes, OCR text, or personal paths

Fix from the report. Re-run the same page step. Clear the finding
only after this page (or a later page that hits the same step) shows
the step works. Do not start the next page while a finding that
blocks the current page is still open.

## Do not stall

These are not stop signals:

- Waiting for the user to click 接受 / 确认 / 导出
- Cursor approval cards for `git push` or a single operator click
- Font-fit taste on one balloon after a usable typeset
- Reversible naming nits

If an auto-review card appears, request approval once and keep doing
other unblocked work on **this** page. Do not idle on the card.

Do not stop to ask whether to continue. Finish this page, push, then
open the next unfinished real page.

## Computer-use

- Prefer `http://127.0.0.1:8000`. If the API is down, start the
  loopback API and built frontend yourself after the stop step.
  Do not ask the user to run commands.
- Click visible Chinese labels: **单图 / 多图 / 文件夹 / 文本 /
  修复 / 排版 / 项目 / 批处理与导出 / 原图 / 增强 / 擦除 / 成品 /
  按建议处理本页 / AI 重绘本页 / 接受 / 拒绝 / 确认本页无文字 /
  标记本页已检查**.
- Judge quality and typeset from local screenshots. Do not copy
  private page pixels, OCR text, or artwork into git, public
  reports, or chat.
- Public language: control names, viewport, job status, sidebar
  index, pixel size. No transcriptions, book filenames, or personal
  absolute paths.

## Missing tools

Ordinary app startup still must not download weights. The loop
operator may install what this Mac lacks, then continue:

- `npm run setup:ai` and `npm run setup:models`
- `npm run setup:mt` and Argos ja→zh
- Tesseract `jpn` / `jpn_vert`
- Playwright / Chromium, or `Manga Localizer.app`

Record honest unavailable UI first, then install, then retry the
same control.

## Product constraints

- Processing stays on the Mac.
- Do not force-push or rewrite published history.
- Never `git add` ONNX/Argos/Tesseract data, `.manga-localizer/`,
  `tests/real-data/`, or the built `.app`.

## Round procedure

1. Read this prompt, `.agent/STATE.md`, `.agent/PAGE_PROBLEM_REPORT.md`,
   `git status`, and the task-owned diff. Protect unrelated WIP.
2. Confirm no leftover queue / watcher / unexpected API job.
3. Select the next unfinished real page (sidebar order, first book
   first). One page only.
4. Run the quality pass. Branch to no-text or text path.
5. On a defect: write the report, fix, re-run this page.
6. When the page path is complete, update `.agent/STATE.md` with
   the finished sidebar index / size and the next page.
7. Run the closest public tests for any code change.
8. Commit **public** files only. Push `origin/main`.
9. Re-arm **only** `AGENT_LOOP_WAKE_manga_page` on that public
   push’s CI, or immediately continue the next page if no public
   code change was needed and CI is not the gate. Do not arm old
   sentinels. Do not arm a 25-minute sleeper.

## Wake / stop

- Primary wake: the current page finished and was pushed, or that
  push’s CI completed. Use `AGENT_LOOP_WAKE_manga_page` and
  `json.loads(..., strict=False)`.
- On user stop: kill watcher PIDs and the loopback API this loop
  started; do not re-arm.
- Do not re-arm `AGENT_LOOP_WAKE_manga_realpages`,
  `AGENT_LOOP_WAKE_manga_ui`, `AGENT_LOOP_WAKE_manga_desktop`,
  `AGENT_LOOP_WAKE_manga_app`, or `AGENT_LOOP_WAKE_manga_realdata`.

## Done for this loop

Stop and leave NEEDS_USER only when all of the following are true:

- Every real uploaded page has finished its required path above.
- `.agent/PAGE_PROBLEM_REPORT.md` has no open findings that block
  a page.
- Public tests/gates for task-owned changes passed.
- No private tree was committed.
- `origin/main` has the last finished-page public commit.

Until then, finish the current page, push, and open the next one.
