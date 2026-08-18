# Manga Localizer — computer-use problem report

Updated: 2026-08-18

Public operator log. No private page images, OCR text, or personal paths.

## Open findings

### UI-1 — Inpaint review control hidden on typical workbench widths

- **Did:** Create a public synthetic project, import the public generator page, run
  preprocess / detect / OCR, confirm boxes, run LaMa inpaint, switch the canvas
  to **擦除**, try to accept the visual review so generated-image export can proceed.
- **Saw:** The review group said **请显示蒙版复核** and **接受** stayed disabled.
  The toolbar **复核蒙版** checkbox is classed `toolbar-check`, which
  `display: none` at `max-width: 1250px`. Playwright’s a11y tree omitted it.
  **修复 → 显示实际蒙版** still works when a region is selected.
- **Blocks:** Generated-image export requires an accepted inpaint review. A
  default Mac app / computer-use viewport is often under 1250px, so the
  required control is gone.
- **Repro:** Public `scripts/generate_test_image.py` page. Viewport or window
  width &lt; 1250px. No private artwork.
- **Status:** Fix in this round (keep the review-group checkbox visible; switching
  to **擦除** shows the mask). Clear only after a later computer-use pass on the
  refreshed app.

### UI-2 — First accept of unchanged preprocess wipes inpaint/typeset reviews

- **Did:** Accept **成品**, then accept **擦除** (after showing the mask), then
  accept **增强**. Open **批处理与导出 → 安全导出** (成品图 + JSON).
- **Saw:** Queue stayed disabled: “所选图像版本尚未全部通过视觉复核” /
  “1 页排版图未接受。1 页无字底图未接受。” Server `stageReviews` kept only
  preprocess. Re-accepting inpaint and typeset *after* preprocess unblocked export.
- **Why:** `artifact_changed` is true when `previous_review is None`, so the
  first accept of an upstream stage clears dependents even when those
  dependents already match the current bytes.
- **Blocks:** A natural review order (downstream first, preprocess later) voids
  export gates the user just completed.
- **Repro:** Public synthetic page; accept typeset and inpaint, then accept
  preprocess without regenerating preprocess bytes.
- **Status:** Fix in this round (only a checksum change clears dependents).
  Clear only after a later computer-use pass on the refreshed API.

### UI-3 — Stale full-panel Tesseract box survives PP-OCR re-detect

- **Did:** Run default Tesseract detect + OCR on the public synthetic page, then
  switch detector to PP-OCRv3 and re-run detect + OCR. Press **整理本页选框**.
- **Saw:** A leftover unconfirmed box covering most of the left panel remained.
  PP-OCR proposed the two-line balloon; the other balloon was not proposed.
  Cleanup did not drop the leftover box. Ignore + a manual box unblocked the path.
- **Blocks:** Re-running a different detector cannot recover text under a huge
  kept false-positive. This is not empty-box replacement (the leftover had text).
- **Repro:** Public generator page; Tesseract then PP-OCR. Do not auto-accept
  empty pages.
- **Status:** Open. Workaround exists. Not fixed in this round.

## Cleared findings

None. Fixes above still need a computer-use pass on the refreshed app/API.

## Last computer-use pass

- Status: path completed with workarounds; product defects recorded
- Path: launch live app → new public project → 单图 import of the public
  synthetic page → preprocess + detect + OCR → provider switch to PP-OCR /
  Argos / LaMa → re-detect/OCR → ignore leftover box → manual box for the
  missed balloon → confirm + manual translate → LaMa inpaint + typeset →
  visual accept (mask via inspector) → page reviewed → export blocked by UI-2
  → re-accept inpaint/typeset → 安全导出 completed (translated image + JSON)
- Did not auto-accept an empty page
- Private catalog projects were left untouched after the new public project
  was created
