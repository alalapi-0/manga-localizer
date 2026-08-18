# Manga Localizer — real-page problem report

Updated: 2026-08-18

Public operator log for the real-page computer-use loop. No private page
images, OCR text, or personal paths.

## Open findings

None.

## Cleared findings

### RP-8 — Blank Argos output erased a written translation

- Found: 2026-08-18 on the 130-page book. After a trusted box already
  had a manual **中文译文**, **翻译 (Argos 本地日→中)** completed 1/1
  and wrote an empty translation, then cleared `confirmed`.
- Cleared: persist keeps the existing non-empty translation when the
  provider returns blank, and leaves confirmation unchanged. Pytest
  `test_blank_provider_translation_keeps_existing_reviewed_text` passed.

### RP-9 — Reopened batch drawer kept leftover whole-book scope

- Found: 2026-08-18 after queuing the current page. Reopening
  **批处理与导出** showed **全部图像** and all 7 steps checked;
  **加入队列 · 130 张 · 7 步** was blocked by mixed pipeline + export
  gates. An operator looking for current-page JSON export had to undo
  that leftover by hand.
- Cleared: closing the drawer unmounts it, so the next open remounts
  on **当前页** with detect+OCR. Vitest covers close → select
  **全部图像** → reopen. CI `32127229143` failed frontend lint on an
  earlier `useEffect` reset; remount replaces that.

### RP-1 — Status filter hidden on typical workbench widths

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

- Status: docs CI `32120142568` (`c5ebb68`) independently rechecked
  success. Same 130-page book, 1100×800, bundle `index-BzEhPVG5.js`.
  Operator continued without waiting for approval cards.
- Path on the 340×594 text page: confirm/ignore finished (4 trusted
  boxes); Argos translate 1/1; LaMa inpaint 1/1 (修复 4 · 跳过 0);
  Pillow typeset 1/1 (整页重排); **成品** already **已接受** after
  visual review; **标记本页已检查**; JSON **安全导出** 1/1.
- Next OCR-done text page (1166×540): ignored empty boxes, confirmed
  the remaining CJK box, Argos translate 1/1, LaMa inpaint 1/1
  (修复 1 · 跳过 0), Pillow typeset 1/1, **成品** accepted after
  visual review, **标记本页已检查**, JSON **安全导出** 1/1.
- Found and fixed RP-8 (blank translator overwrite) and RP-9
  (reopened drawer leftover **全部图像** + all steps).
- Did not click **确认本页无文字** on a wide strip that had only
  empty leftover boxes. Did not auto-accept empty pages.
- Empty / leftover-empty pages skipped this pass: 1 zero-box page
  plus 1 empty-OCR strip (15 ignored empty boxes, page left 待检查).
- Product Round 8 is not complete
- Private trees were not committed
