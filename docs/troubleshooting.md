# Troubleshooting

## OCR provider unavailable

Run `tesseract --version` and `tesseract --list-langs`. Install both `jpn` and `jpn_vert`, or set
`MANGA_LOCALIZER_TESSERACT_COMMAND` to the executable in the root `.env`. Restart `npm run dev` after
changing the file.

## A folder import loses nesting

Use the dedicated folder input in a browser that supports `webkitdirectory`. Individual file selection
does not expose a common root, so those files intentionally import by filename unless relative paths are
provided by a future desktop shell. Folder import strips the selected root folder itself and preserves
the tree below it.

## `inputRoot` is empty after a local import

`inputRoot` is only a common-path summary and can be empty when selected paths have no usable common
parent, especially across Windows drives. Exact file/directory import boundaries are still stored
cumulatively, including selections that failed image validation, so original-overwrite protection is
unchanged.

## Chinese text overflows a balloon

Open the overflowing page from the sidebar **排版溢出** filter or the inspector warning. Use
**只重排溢出框** to rerun typesetting for those boxes only, or **选中溢出框** then merge/resize.
Shrink the font or enable auto-fit, then click **重排当前框** or press **T**. **只重排溢出框**
(**⇧T**) and **重排当前框**
overlay those boxes onto the last typeset plate when the clean plate is still current; other boxes
keep their pixels. If the previous typeset file is missing, that action redraws the whole page so
other boxes are not dropped. Generated plates are not kept in the browser HTTP cache, so the canvas
reloads the rewritten file instead of an old overlay. When an overlay typeset for the current page
finishes, the boxes just redrawn stay selected in the typesetting inspector so you can adjust and
press **T** again. A full-page typeset still selects remaining overflowing boxes. The job queue then
shows whether that run overlaid selected boxes or
redrew the whole page. When a typeset job for the current page finishes, the canvas switches to
**成品**. Remaining overflowing boxes are selected and the typesetting inspector opens so you can
resize, refit, or press **⇧T** without hunting for them. The canvas also opens **对比** so the original
and result sit side by side; press **B** to close it. Geometry or mask edits still rebuild inpainting for the page. Overflow is recorded
from the last successful typeset and is a review hint, not an export hard gate. Shift+Left/Right
skips pages already marked reviewed so you can keep moving through a book. Vertical balloons keep
ordinary CJK quotes and punctuation in the translation; the renderer maps them to vertical
presentation forms and hangs comma/period glyphs. Horizontal balloons leave that text unchanged.
Adjacent small OCR fragments that share direction and sit close together can share one typeset run;
merge boxes only when you want a single editable region.

## Chinese text renders as boxes

Choose a locally installed CJK font in Project Settings. The renderer searches common system fonts but
does not download or bundle copyrighted font files.

## OpenCV leaves visible artifacts

Adjust the text box, mask padding, dilation, repair radius, or fill method. Preserve/ignore the region or
select one region and refine its actual mask with the brush/eraser. After inpainting, compare the stored
candidates (provider result, Navier-Stokes, Telea, and line-art-guided) in the repair inspector or the
擦除 toolbar, then accept only a usable plate. Automatic smear/chroma flags are hints. The interpolators
still cannot invent missing line art, so some repairs may need an external editor.

## Export says a visual-stage review is missing or stale

Finish the page-level text review, switch the canvas to **擦除**, display and inspect **复核蒙版**, then
accept the inpaint result. When an inpaint job for the current page finishes, the canvas switches to
**擦除** and shows the mask so that review can start without changing those controls by hand. When a
preprocess job for the current page finishes, the canvas switches to **增强**. Those visual-stage
completions also open **对比**. Accept
**成品** as well when exporting a typeset image. The decision is bound
to the exact bytes loaded by the canvas. A changed artifact or mask
no longer matches the saved review and must be reloaded, rerun, or accepted again. Upstream changes clear
dependent review state. JSON-only export intentionally does not require visual-stage acceptance.

## Port already in use

Set `MANGA_LOCALIZER_PORT` or `MANGA_LOCALIZER_WEB_PORT` in the root `.env`, then restart
`npm run dev`; the launcher updates the proxy automatically. Set `VITE_DEV_API_TARGET` only when the API
is managed separately. The launcher rejects non-loopback bind addresses because the MVP has no
authentication; use an authenticated reverse proxy only as an explicitly unsupported advanced setup.

## A remote translation endpoint is rejected

Use HTTPS unless the service is explicitly bound to loopback. Base URLs cannot contain credentials,
queries, fragments, or control characters. Changing a valid remote endpoint or model intentionally marks
translation, typesetting, and export as pending; rerun those stages before export.

## Duplicate names receive `-2` or an export conflict

Names are compared component by component with Unicode NFKC normalization and case folding. Two visually
similar Unicode names, or names differing only by case, may collide on Windows or macOS even when the
current filesystem allows both. Keep the automatic rename or choose distinct portable names.

## An export was interrupted

Restart the application and resume/retry the persisted job. Export completion is withheld until its
portable database and manifest have finalized. A hidden job-scoped owner marker lets the same job clean
its own temporary bundle and SQLite sidecars without treating unrelated files as recoverable output.
Do not copy unrelated content into a partially created `project/` directory.

Relative custom output paths are anchored to the project root when the job is created. Use an absolute
path when you intend a location outside that project; changing the shell working directory will not
redirect an existing job.

## Project will not open

Enter the full path to `output/project/project.json`. Confirm the adjacent `project.sqlite3` still exists
and is writable: the manifest is inspectable but cannot reconstruct a project by itself. Do not hand-edit
schema versions; make a copy and report the sanitized error if migration fails.

## Real-ESRGAN is unavailable

Install the backend `ai` extra and the checksum-verified anime ONNX model, then restart
`npm run dev`:

```bash
npm run setup:models -- realesrgan
uv sync --project backend --extra ai --group dev
```

`realesrgan-onnx` is the local AI provider. `opencv-pillow` Lanczos is classic interpolation and will
not appear as AI upscaling. The NCNN adapter still needs a separately installed
`realesrgan-ncnn-vulkan` executable; it is optional once the ONNX provider is installed.

## Local Argos Japanese-to-Chinese translation is unavailable

Install the backend `mt` extra and both checksum-verified Argos packages, then restart `npm run dev`:

```bash
npm run setup:mt
```

Or, with a repository data directory:

```bash
uv sync --project backend --extra mt --group dev
npm run setup:models -- --data-dir .manga-localizer argos-ja-zh
```

`argos-ja-zh` translates locally through English and currently produces Simplified Chinese. It does not
replace human review. Mock translation remains a deterministic demo and is not this provider.

## Detector drafts should not count as ground truth

Private detector-draft JSON stays `draft` until a local human lists page IDs for
`scripts/review_detection_annotations.py`. That command copies accepted/rejected pages into a new
ignored directory. Progress output is aggregate counts; do not paste `--list-pending` page IDs into
public reports. Empty pages are not auto-promoted.
