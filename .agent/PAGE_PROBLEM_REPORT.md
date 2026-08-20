# Manga Localizer — one-page reprocess problem report

Updated: 2026-08-20

Public operator log for the one-page full-reprocess loop. No private
page images, OCR text, or personal paths.

## Open findings

None.

## Cleared findings

### P-7 — text-only repair leaves one terminal ellipsis dot

- Kind: `quality`
- Page: sidebar 35, 1185×384
- Clicked: compared all four current-page repair candidates with the review
  mask hidden at enlarged selected-region zoom
- Expected: the complete short dialogue string, including every terminal
  ellipsis dot, is removed while the small balloon outline stays intact
- What happened: every candidate removed the main glyphs and most punctuation,
  but one final ellipsis dot remained clearly visible inside the balloon
- Why it blocks: the clean plate was visibly incomplete and could not be
  accepted or used for final typesetting
- Fix: extend the verified region's lower boundary beyond the final
  punctuation, re-run OCR, restore the operator text, reconfirm the region,
  rerun local translation, and rebuild the same page
- Same-page verification: the new text mask enclosed every ellipsis dot without
  touching the balloon outline. All four candidates were compared again at
  enlarged selected-region zoom; the remnant was gone from each, and the
  primary LaMa result kept all three balloon borders and nearby art intact.
  After correcting the wide dialogue to vertical flow, the final three-region
  typeset, page review, and current-page JSON export completed with zero
  overflow or open review gates.
- Repro: run text-mask repair on a short vertical dialogue whose ellipsis ends
  close to the lower edge of its verified region, hide the review mask, and
  inspect the clean plate at selected-region zoom. Do not include private text,
  page pixels, project names, or paths.

### P-6 — text-only repair leaves punctuation remnants on the clean plate

- Kind: `quality`
- Page: sidebar 31, 1175×1815
- Clicked: compared the primary current-provider clean plate with the enhanced
  page after current-page LaMa repair
- Expected: both verified dialogue strings, including their punctuation, are
  completely removed while nearby line art remains intact
- What happened: the main glyphs disappeared, but a visible punctuation remnant
  remained at each repaired location at normal workbench zoom
- Why it blocks: the clean plate is visibly incomplete and cannot be accepted or
  used for final typesetting
- Fix: extend both verified regions far enough to include their punctuation,
  then tighten the horizontal bounds back to the actual glyph columns so the
  text mask does not absorb adjacent hair and shadow. Re-run OCR and explicitly
  reconfirm both regions before rebuilding the current page.
- Same-page verification: all four repair candidates were compared at fit and
  enlarged zoom. The primary LaMa result removed both complete strings without
  the original punctuation remnants and preserved the nearby panel art better
  than the OpenCV and line-guided alternatives. The accepted result survived a
  final local-translation refresh, fixed-size typeset, page review, and JSON
  export with zero overflow or open review gates.
- Repro: run text-mask repair on two short vertical exclamations whose punctuation
  sits near the lower edge of the detected regions, hide the review mask, and
  compare the clean plate at fit-to-page zoom. Do not include private text, page
  pixels, project names, or paths.

### P-5 — full-balloon repair mask creates a visible background patch

- Kind: `quality`
- Page: sidebar 28, 1064×628
- Clicked: compared the final clean plate against the original with the review
  mask hidden
- Expected: both balloon interiors remain visually continuous after text removal
- What happened: the large manually drawn left balloon region let LaMa replace
  part of the balloon interior with a white rectangular patch at its lower edge
- Why it blocks: the defect is visible at normal workbench zoom and would remain
  under the translated overlay; the clean plate and final page cannot be accepted
- Fix: keep the manually verified text inside the left region while shortening
  its lower edge before the balloon outline, and route that white-background
  region through a local OpenCV solid text mask. The other balloon remains on
  LaMa, so repair stays region-specific rather than changing project defaults.
- Same-page verification: direct original/clean comparisons showed both text
  groups removed, the left balloon outline preserved, and surrounding artwork
  unchanged. The mixed-provider clean plate and subsequent Pillow typeset were
  accepted with no overflow.
- Repro: create one repair region spanning the full tall balloon, run current-page
  LaMa, hide the review mask, and compare the balloon's lower edge with the
  original. Do not include page pixels, source text, project names, or paths.

### P-4 — reconfirming a translation-only edit invalidates the accepted clean plate

- Kind: `interaction`
- Page: sidebar 28, 1064×628
- Clicked: rejected the first typeset result, edited only translated text,
  reconfirmed that already-trusted region, reran current-page typeset, accepted
  the corrected result, and exported current-page JSON
- Expected: a translation-only correction and its reconfirmation invalidate the
  typeset result, but preserve the unchanged accepted inpaint artifact
- What happened: the reconfirmation marked inpaint pending, so the typeset job
  rebuilt the clean plate and silently cleared its accepted visual review. The
  page and JSON export still showed done, but the persisted inpaint review gate
  was open.
- Why it blocks: sidebar 28 cannot count as passed until the clean plate is
  explicitly accepted after the final typeset run; proceeding would violate the
  one-image gate.
- Fix: reconfirming an already-trusted region after a translation- or style-only
  edit now invalidates typeset/export only; it does not discard an unchanged
  accepted inpaint artifact. Trust-changing confirmations still invalidate the
  clean plate.
- Same-page verification: the new regression passed. In the repaired live app,
  the operator translation correction and reconfirmation preserved the accepted
  inpaint review; full typeset completed without rebuilding the clean plate, and
  all final stage reviews remained accepted through page review and JSON export.
- Repro: on a trusted, confirmed region with accepted inpaint, edit only its
  translated text, reconfirm the region, rerun current-page typeset, and inspect
  the persisted inpaint stage review. Do not include private text, page pixels,
  project names, or paths.

### P-3 — application-mode launcher exits immediately after opening its window

- Kind: `function`
- Page: sidebar 28, 1064×628
- Clicked: started the documented loopback application with `npm run app`
- Expected: the dedicated application window and `127.0.0.1:8000`
  workbench remain available so the current page can be opened and processed
- What happened: the window launcher child exited successfully immediately
  after opening the window, and `scripts/app.mjs` treated that helper exit as
  the application closing, so it terminated the API too
- Why it blocked: the real workbench disappeared before sidebar 28 could be
  opened or visually processed
- Fix: distinguish the bundled window helper, whose lifetime owns the packaged
  application, from one-shot external Chromium / browser launchers. Source-tree
  application mode now keeps the API alive when an external launcher returns,
  while the bundled helper still closes the API with its real window.
- Same-page verification: launcher tests passed; repaired `npm run app` kept
  `127.0.0.1:8000` and the queue running while sidebar 28 completed its full
  quality, text, visual-review, page-review, and JSON-export path.
- Repro: on macOS with a supported browser/application-window launcher,
  start `npm run app` and observe whether the loopback health endpoint remains
  available after the launcher helper returns. Do not include private project
  names, image bytes, OCR text, or personal paths.

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
- Finished pages this loop: 37.
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
- Finished: sidebar 16, 332×572, text path after quality pass
  (1 shout balloon from 5 boxes).
- Finished: sidebar 17, 1073×482, text path after quality pass
  (2 balloons from 4 boxes).
- Finished: sidebar 18, 1190×661, text path after quality pass
  (3 floating text clusters from 19 boxes).
- Finished: sidebar 19, 1178×537, no-text path after quality pass
  (3 artwork false boxes ignored).
- Finished: sidebar 20, 1187×1244, text path after quality pass
  (1 shout from 2 boxes; drawn SFX left as artwork).
- Finished: sidebar 21, 958×228, no-text path after quality pass
  (1 artwork false box ignored).
- Finished: sidebar 22, 1284×777, no-text path after quality pass
  (1 drawn-SFX fragment ignored).
- Finished: sidebar 23, 1072×564, no-text path after quality pass
  (1 debris false box ignored).
- Finished: sidebar 24, 1074×358, text path after quality pass
  (3 balloons from 46 boxes; drawn SFX left as artwork).
- Finished: sidebar 25, 1074×793, text path after quality pass
  (2 floating text clusters from 43 boxes).
- Finished: sidebar 26, 1089×334, no-text path after quality pass
  (2 landscape-line false boxes ignored).
- Finished: sidebar 27, 1068×619, text path after quality pass
  (2 balloons from 22 boxes; drawn SFX left as artwork).
- Finished: sidebar 28, 1064×628, text path after quality pass
  (2 balloons from 20 detections; P-3 launcher lifetime, P-4 review
  invalidation, and P-5 clean-plate boundary issues fixed and reverified).
- Finished: sidebar 29, 1174×1161, text path after quality pass
  (1 complete dialogue region; 2 artwork false positives ignored; first
  typeset rejected as too small, then corrected and reverified).
- Finished: sidebar 30, 1189×699, no-text path after quality pass
  (1 shadow-stroke false positive ignored).
- Finished: sidebar 31, 1175×1815, text path after quality pass
  (2 vertical exclamations from 11 detections; 5 artwork false positives
  ignored; P-6 punctuation-mask boundary fixed and reverified).
- Finished: sidebar 32, 441×1827, text path after quality pass
  (3 dialogue regions from 30 detections after cleanup; 13 artwork false
  positives ignored; all repair candidates and the final typeset reverified).
- Finished: sidebar 33, 717×1818, no-text path after quality pass
  (4 detections consolidated to 2; drawn effect lettering and image texture
  explicitly ignored; latest enhancement reaccepted before final export).
- Finished: sidebar 34, 1181×1262, no-text path after quality pass
  (0 detections on the accepted enhanced plate; drawn effects left as art).
- Finished: sidebar 35, 1185×384, text path after quality pass
  (3 vertical dialogue regions from 20 detections after cleanup; 5 false
  positives ignored; P-7 ellipsis-mask boundary fixed and reverified).
- Finished: sidebar 36, 1187×571, text path after quality pass
  (1 complete vertical dialogue region from 21 detections after cleanup; 1
  duplicate and 1 line-art false positive ignored; all repair candidates
  compared; first undersized typeset rejected, then fixed-size vertical
  typeset accepted with zero overflow).
- Finished: sidebar 37, 1178×1267, text path after quality pass
  (1 complete vertical dialogue region from 12 detections after cleanup; 1
  overlapping duplicate ignored; first mask touching the balloon outline was
  rejected, then narrowed and rebuilt on the same page; all repair candidates
  compared and fixed-size three-column typeset accepted with zero overflow).
- Next page: sidebar 38 of the 130-page book.
- Earlier realpages-loop “已检查” pages are not a skip; this loop
  reprocesses from the first page.
