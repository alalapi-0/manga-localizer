# Manga Localizer — one-page reprocess problem report

Updated: 2026-08-19

Public operator log for the one-page full-reprocess loop. No private
page images, OCR text, or personal paths.

## Open findings

None.

## Cleared findings

### P-2 — tiled detect on a wide 4× plate floods the page with tiny boxes

- Kind: `function`
- Page: sidebar 5, 1110×312
- Clicked: accepted Real-ESRGAN ONNX 4× (enhanced 4440×1248), then
  current-page **文字检测** + **日文 OCR**
- Expected: a small set of balloon / SFX boxes so confirm / ignore
  can finish the text path
- What happened: detect+OCR completed 1/1 with 58 boxes (15 leftover
  ignored). Most new boxes are 3–8 px fragments. **整理本页选框**
  left 55. The inspector asks to confirm dozens of leftovers.
- Why it blocked: confirm / translate / inpaint / typeset could not
  finish while fragment boxes buried the real text.
- Fix: drop boxes smaller than a short-side-scaled minimum on the
  detector plate and after mapping back to the page. Re-detect also
  replaces leftover tiny unconfirmed auto boxes even when OCR filled
  them. Same-page detect+OCR then returned 26 boxes with 0 sub-minimum
  fragments; visual review kept 2 real boxes and ignored the rest.
- Repro: on a wide short photographed page, accept a 4× plate, then
  current-page detect+OCR only. Do not include image bytes, OCR
  text, or personal paths.

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
- Finished pages this loop: 15.
- Finished: sidebar 1, 1184×701, no-text path after quality pass.
- Finished: sidebar 2, 1166×540, text path after quality pass.
- Finished: sidebar 3, 627×1843, no-text path after quality pass
  (P-1 tiled detect, then ignored artwork false boxes).
- Finished: sidebar 4, 340×594, text path after quality pass.
- Finished: sidebar 5, 1110×312, text path after quality pass
  (P-2 min-size filter, then 2 real boxes).
- Finished: sidebar 6, 1068×811, text path after quality pass
  (11 boxes merged to 1 balloon).
- Finished: sidebar 7, 1084×749, text path after quality pass
  (5 balloons after restoring one ignored empty box).
- Finished: sidebar 8, 1190×666, text path after quality pass
  (4 text clusters from 37 boxes).
- Finished: sidebar 9, 1185×551, no-text path after quality pass
  (0 boxes on the 4× plate).
- Finished: sidebar 10, 1185×1095, text path after quality pass
  (3 text clusters from 9 boxes).
- Finished: sidebar 11, 1188×435, text path after quality pass
  (1 dialogue box from 2 detections).
- Finished: sidebar 12, 1182×751, text path after quality pass
  (2 balloons from 9 boxes).
- Finished: sidebar 13, 1076×515, text path after quality pass
  (1 balloon from 9 boxes).
- Finished: sidebar 14, 1189×777, no-text path after quality pass
  (6 artwork false boxes ignored).
- Finished: sidebar 15, 1058×631, text path after quality pass
  (5 text clusters from 43 boxes).
- Next page: sidebar 16 of the 130-page book.
- Earlier realpages-loop “已检查” pages are not a skip; this loop
  reprocesses from the first page.
