# Manga Localizer — one-page reprocess problem report

Updated: 2026-08-19

Public operator log for the one-page full-reprocess loop. No private
page images, OCR text, or personal paths.

## Open findings

None.

## Cleared findings

### P-1 — enhanced detect+OCR returns zero boxes on a tall page

- Kind: `function`
- Page: sidebar 3, 627×1843
- Clicked: accepted Real-ESRGAN ONNX 4× (enhanced 2508×7372), then
  **批处理与导出** · **当前页** · **文字检测** + **日文 OCR** ·
  **加入队列 · 1 张 · 2 步**
- Expected: PP-OCRv3 boxes when the plate still has detector-sized
  marks, instead of a hard zero
- What happened: the first detect+OCR completed 1/1 with 0 regions
  because PP-OCR stretched the tall plate into 736×736. Letterbox
  alone was still 0; overlapping 736 tiles on the 4× plate produced
  10 boxes.
- Fix: PP-OCR letterbox plus overlapping tiles, then NMS. Same-page
  detect+OCR then returned 10 boxes. Visual review found they sat on
  artwork, not balloons or SFX; all 10 were ignored and
  **确认本页无文字** set `no-text-reviewed`.
- Repro: on a tall narrow photographed page, run current-page AI
  重绘 to a 4× plate, then current-page detect+OCR only. Do not
  include image bytes, OCR text, or personal paths.

## Progress

- Corpus: 130-page full book first, then remaining real books.
- Synthetic catalog leftovers are out of scope.
- Finished pages this loop: 4.
- Finished: sidebar 1, 1184×701, no-text path after quality pass.
- Finished: sidebar 2, 1166×540, text path after quality pass.
- Finished: sidebar 3, 627×1843, no-text path after quality pass
  (P-1 tiled detect, then ignored artwork false boxes).
- Finished: sidebar 4, 340×594, text path after quality pass.
- Next page: sidebar 5 of the 130-page book.
- Earlier realpages-loop “已检查” pages are not a skip; this loop
  reprocesses from the first page.
