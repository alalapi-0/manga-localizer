# Manga Localizer — real-page problem report

Updated: 2026-08-18

Public operator log for the real-page computer-use loop. No private page
images, OCR text, or personal paths.

## Open findings

None.

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

### RP-4 — Confirm toggle and text tab stay off the next-action path

- Cleared: 2026-08-18 live rebuild recheck on the 130-page book. From
  **项目**, **批处理与导出** detect+OCR on the viewed page completed 1/1;
  the inspector switched to **文本** without a manual tab click. Confirm
  stays above the fold at 1100×800. **还需确认并信任** still jumps to the
  first unready box.

### RP-5 — Optional local providers reported unavailable

- Cleared: 2026-08-18 after API restart. **Real-ESRGAN ONNX** and **Argos
  本地日→中** are selectable; **AI 重绘本页** is enabled. **Real-ESRGAN
  NCNN** remains `[不可用]` with no local executable — honest, not a
  missing-install skip. OpenAI stays `[未配置]`.

### RP-6 — Batch queue defaults to leftover checkbox selection, not the viewed page

- Cleared: 2026-08-18 live rebuild recheck. With a leftover sidebar
  checkbox still on another page, **批处理与导出** defaulted to **当前页**
  (checked) and **加入队列 · 1 张** ran detect+OCR on the viewed page
  (340×594, 11 boxes), not the leftover first page.

### RP-7 — Translation field is below the fold and the inspector cannot scroll

- Cleared: 2026-08-18 live rebuild recheck at 1100×800 after the inspector
  scroll + notice-order fix. Selecting a box with source text put
  **确认此文本框** at `y=285` and **中文译文** at `y=595–755` (above the
  fold). Helper notices sit after the form. `.inspector__content` is
  `overflow-y: auto` and can scroll (`scrollHeight` 1469 / `clientHeight`
  701).

## Last computer-use pass

- Status: CI-green follow-up on the 130-page manga01 full book
  (viewport 1100×800, rebuilt bundle `index-BzEhPVG5.js`)
- Path: reopen full book → skip 0-box first page → select a later text
  page → open one box. Confirm/source/translation are on the first
  screen. Did not confirm, ignore, auto-accept, or export this pass.
- Empty pages skipped: 1 this pass (plus prior zero-box pages left untouched)
- Product Round 8 is not complete
- Private trees were not committed
