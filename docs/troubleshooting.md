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

## Chinese text renders as boxes

Choose a locally installed CJK font in Project Settings. The renderer searches common system fonts but
does not download or bundle copyrighted font files.

## OpenCV leaves visible artifacts

Adjust the text box, mask padding, dilation, repair radius, or fill method. Preserve/ignore the region or
select one region and refine its actual mask with the brush/eraser. Rerun inpaint and typeset, inspect
both results, and explicitly accept only usable output. The baseline interpolator still cannot
reconstruct detailed line art semantically, so difficult repairs may need an external editor.

## Export says a visual-stage review is missing or stale

Finish the page-level text review, switch the canvas to **擦除**, display and inspect **复核蒙版**, then
accept the inpaint result. Accept **成品** as well when exporting a typeset image. The decision is bound
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
