# Manga Localizer — real-page problem report

Updated: 2026-08-18

Public operator log for the real-page computer-use loop. No private page
images, OCR text, or personal paths.

## Open findings

### RP-4 — Confirm toggle and text tab stay off the next-action path

- Kind: `ux-layout`
- Status: **partial**. After the public layout change, **还需确认并信任**
  opens **文本**, selects the first unready box, and **确认此文本框** sits
  above the fold at 1100×800 (`y≈524` in an 800px inspector).
- Remaining: a detect/OCR job that finishes between two job polls used to
  be ignored (`previous` missing), so **项目** stayed selected. Public
  source now treats a newly appeared completed job as a completion when
  the client was already polling. Needs a live rebuild recheck.
- Repro: run detect+OCR from **批处理与导出** with **项目** selected; do
  not click **文本**. Note whether the tab switches when the job completes.

### RP-6 — Batch queue defaults to leftover checkbox selection, not the viewed page

- Kind: `function`
- Clicked: opened a 130-page full book, skipped a 0-box first page, opened a
  later page that already had boxes, then **批处理与导出** → **加入队列 ·
  1 张 · 2 步** (detect + OCR). The drawer labeled **当前页** as the viewed
  page. **批选图像** stayed checked on the leftover first-page checkbox.
- Happened: the job ran on the first page (0 boxes, detect/OCR done). The
  viewed page stayed `detection: pending`. After explicitly choosing
  **当前页** and re-queuing, detect+OCR completed on the viewed page
  (4 balloon boxes).
- Why it blocks: a person looking at page N and pressing **加入队列**
  processes a leftover batch-selected page. Empty-page detect then looks
  like “this page has no text” when the page they are reviewing was never
  queued.
- Repro: select page A with the sidebar checkbox, click page B to view it,
  open **批处理与导出** without changing the scope radio, queue detect.
  The job image is A, not B.

## Cleared findings

### RP-1 — Status filter hidden on typical workbench widths

- Cleared: 2026-08-18 full-book pass, viewport 1100×800. **按状态筛选** is
  `display: block` at `x=12, y=230, w=230` and is usable.

### RP-2 — Adjacent-page footer clipped in the 252px sidebar

- Cleared: 2026-08-18 full-book pass. **上一张图** is at `x=25` (26×26),
  **下一张图** at `x=203`; neither is clipped.

### RP-3 — Changing the project detector wipes every page

- Cleared: 2026-08-18 full-book pass. Switching **文本检测** to PP-OCRv3
  (and translator/inpainter defaults) saved without dropping the existing
  854 region records. Stage statuses stayed as they were.

### RP-5 — Optional local providers reported unavailable

- Cleared: 2026-08-18 after API restart. **Real-ESRGAN ONNX** and **Argos
  本地日→中** are selectable; **AI 重绘本页** is enabled. **Real-ESRGAN
  NCNN** remains `[不可用]` with no local executable — honest, not a
  missing-install skip. OpenAI stays `[未配置]`.

## Last computer-use pass

- Status: restarted real-page loop on the 130-page manga01 full book
  (not the 30-page slice; not the leftover synthetic catalog entries)
- Viewport: 1100×800 at `http://127.0.0.1:8000/` (no test query)
- Path: reopen full book → **项目** providers (PP-OCR / Argos / LaMa) →
  skip 0-box first page → batch detect+OCR misfire (RP-6) → **当前页**
  detect+OCR 1/1 with 4 boxes → **还需确认并信任** opens **文本** with
  confirm above the fold. Did not auto-accept empty pages (first page
  0 boxes after detect; skipped).
- Empty pages skipped: 1 this pass (plus prior zero-box pages left untouched)
- Product Round 8 is not complete
- Private trees were not committed
