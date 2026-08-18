# Manga Localizer — computer-use problem report

Updated: 2026-08-18

Public operator log. No private page images, OCR text, or personal paths.

## Open findings

None.

## Cleared findings

### UI-3 — Stale full-panel Tesseract box survives PP-OCR re-detect

- Cleared 2026-08-18 on the refreshed API after the oversized-leftover
  replacement change.
- Fresh public synthetic project: Tesseract detect + OCR left two
  unconfirmed panel-sized boxes (~41% and ~46% of the page). One was empty;
  one had short junk OCR. Switching to PP-OCRv3 and re-running **文字检测**
  dropped both leftovers and proposed three small balloon boxes. Sidebar
  count went 2 → 3. Did not auto-accept an empty page.

### UI-1 — Inpaint review control hidden on typical workbench widths

- Cleared 2026-08-18 on the refreshed workbench at `30d27a5` (viewport 1100px).
- Switching to **擦除** showed **复核蒙版** in the review group, checked, and
  **接受** enabled when the stage was pending. Previously accepted inpaint
  showed **已接受** with the same checkbox still visible.

### UI-2 — First accept of unchanged preprocess wipes inpaint/typeset reviews

- Cleared 2026-08-18 on the refreshed API at `30d27a5`.
- Replay: withdraw all visual reviews, accept **成品**, accept **擦除** (mask
  auto-shown), then first-accept **增强**. Server `stageReviews` kept
  preprocess + inpaint + typeset as accepted. Typeset also survived the
  first inpaint accept.

## Last computer-use pass

- Status: UI-1, UI-2, and UI-3 verified. No open product defects.
- Path: reopen/create a public synthetic project → Tesseract detect + OCR
  produced panel leftovers → switch detector to PP-OCRv3 → re-run
  **文字检测** → leftovers gone, three balloon boxes proposed. Earlier pass
  also confirmed **复核蒙版** at 1100px and unchanged-preprocess accept.
- Did not auto-accept an empty page
- Product Round 8 is not complete
- Independent CI recheck of the previous public fix: GitHub Actions run
  `32101553739` at `30d27a5` succeeded (frontend, backend, e2e)
