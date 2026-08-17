# Development

## Bootstrap

Use Node.js 22.22.2 or newer and Python 3.12.

```bash
npm install
uv sync --project backend --group dev
npm --prefix frontend install
npx playwright install chromium
```

`npm run setup` installs the application dependencies without downloading a browser. Use
`npm run setup:test` when preparing to run Playwright. The backend intentionally targets Python 3.12.

## Run

```bash
npm run dev
```

The Vite server proxies `/api` to FastAPI. `scripts/dev.mjs` loads the optional Git-ignored root `.env`
and passes its values to both processes. Without an explicit value, runtime catalog data and projects
created without an output path live below `~/.manga-localizer`; set `MANGA_LOCALIZER_DATA_DIR` to move
them. The provided `.env.example` deliberately sets that variable to the repository-relative
`.manga-localizer` directory, so a copied sample changes the location until edited. `MANGA_LOCALIZER_PORT`
and `MANGA_LOCALIZER_WEB_PORT` change the API and Web ports, and the launcher derives the Vite proxy
target automatically unless `VITE_DEV_API_TARGET` is set.

### Optional local models

Developer bootstrap and ordinary application startup still do not download models.
`npm run package:app` may download the same checksum-verified files at *package*
time and copy them into `Manga Localizer.app`; those weights stay out of git.
The packaged app reads `Contents/Resources/models/manifest.json` and reports
unavailable when a bundled file is missing or the SHA-256 does not match.

To install the PP-OCR and LaMa models explicitly for a source checkout with
fixed SHA-256 verification, target the same data directory used by the application:

```bash
npm run setup:models -- --data-dir .manga-localizer ppocr lama realesrgan
uv sync --project backend --extra ai --group dev
```

Install the optional local Japanese-to-Chinese translator separately. It is not part of `setup:ai`:

```bash
npm run setup:models -- --data-dir .manga-localizer argos-ja-zh
uv sync --project backend --extra mt --group dev
```

Without a repository `.env`, omit `--data-dir .manga-localizer` to use the normal
`~/.manga-localizer` default, or run `npm run setup:ai`. `realesrgan-onnx` is the local AI
upscaler once that model and the `ai` extra are installed. `realesrgan-ncnn` still wraps a separately
installed `realesrgan-ncnn-vulkan` executable; place the binary under the data directory or set
`MANGA_LOCALIZER_REALESRGAN_NCNN_COMMAND`.

Compare classic Lanczos against AI upscaling into an ignored output directory:

```bash
uv run --project backend --extra ai python scripts/compare_upscale.py \
  --input tests/real-data/<dataset>/input \
  --output tests/real-data/<dataset>/runs/<new-run> \
  --data-dir .manga-localizer \
  --factor 2
```

Compare local inpainting candidates on a public synthetic line-art page, or on private pages with an
explicit mask, into an ignored output directory:

```bash
uv run --project backend --extra ai python scripts/compare_inpaint.py \
  --synthetic \
  --output tests/real-data/<dataset>/runs/<new-inpaint-run> \
  --data-dir .manga-localizer
```

Compare the local Argos Japanese-to-Chinese translator on public synthetic phrases into an ignored
directory. The summary stores character counts and CJK ratios, not translations or private OCR text:

```bash
uv run --project backend --extra mt python scripts/compare_translate.py \
  --output tests/real-data/<dataset>/runs/<new-translate-run> \
  --data-dir .manga-localizer
```

Promote ignored detector-draft annotation JSON after local visual review. Progress prints aggregate
counts only. `--list-pending` prints page IDs for local use and must not be pasted into public reports.
Accept/reject copies into a new ignored directory and never auto-promotes empty pages:

```bash
uv run --project backend python scripts/review_detection_annotations.py \
  --annotations tests/real-data/<dataset>/annotations/<draft-set>
uv run --project backend python scripts/review_detection_annotations.py \
  --annotations tests/real-data/<dataset>/annotations/<draft-set> \
  --output tests/real-data/<dataset>/annotations/<reviewed-set> \
  --accept <page-id> --reject <page-id>
```

The 0.2.0 launcher was exercised through `npm run dev`: the root Web page, direct FastAPI
health endpoint, and the Vite `/api` proxy all responded successfully. Launcher platform logic also has
Node tests in the unified `npm run check` gate, including the macOS `.app` skeleton.

```bash
npm run package:app
```

builds `dist/macos/Manga Localizer.app`. On Darwin it also copies the app to `~/Applications`.
Pass `--no-install-user` to keep the copy only under `dist/`. Pass `--skip-download` only when
every required model is already verified in a `--copy-from` data directory. The `.app` and model
weights are gitignored.

## Verify

```bash
uv run --project backend ruff check backend
uv run --project backend ruff format --check backend
uv run --project backend pytest backend/tests
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
npm run test:e2e
npm run audit:release
```

Tests create temporary Unicode directories and generated images; they never depend on network calls,
credentials, model downloads, or commercial manga.

### Private real-data evaluation

Real material and all outputs must live under the ignored `tests/real-data/` boundary. Copy inputs there
before processing; never hard-code a personal source path. The evaluator exercises the real API and
stores aggregate/per-file metrics without OCR text:

```bash
uv run --project backend --extra ai python scripts/evaluate_real_data.py \
  --input tests/real-data/<dataset>/input \
  --output tests/real-data/<dataset>/runs/<new-run> \
  --stages preprocess,detect,ocr,inpaint,typeset,export \
  --detector-provider ppocr-v3 \
  --inpainter-provider lama-onnx
```

Unattended export defaults to `--export-format json`. Generated-image formats (`images` or
`both`) still require a current accepted page review and matching stage reviews; the evaluator
does not auto-accept empty or unreviewed pages.

It refuses a non-empty run directory and records source-checksum preservation, generated dimensions,
mask coverage, and changed pixels outside masks. These reports are stability/coverage evidence unless a
private ground-truth transcription/box set is supplied; region count and OCR confidence are not accuracy.

Detection/OCR box evaluation is separate. Generate or point at annotation JSON, then write a sanitized
report that omits transcriptions, filenames, checksums, and paths:

```bash
uv run --project backend python scripts/evaluate_detection_ocr.py \
  --synthetic \
  --detector ppocr-v3 \
  --output tests/real-data/synthetic-stress/runs/<new-run>
uv run --project backend python scripts/bootstrap_detection_annotations.py \
  --input tests/real-data/<dataset>/input \
  --output tests/real-data/<dataset>/annotations/<new-draft> \
  --detector ppocr-v3 \
  --no-ocr-draft
```

Draft annotation JSON is not independent ground truth until a human marks regions `reviewed`.

The current automated browser suite uses Chromium. The GitHub Actions workflow targets Ubuntu; macOS
is the primary local development environment, while Windows has documented startup steps but no CI job.

### Exact 0.2.0 source-tree evidence — 2026-08-06

| Gate | Result |
| --- | --- |
| `npm run check` launcher | 2 Node tests passed |
| Backend | Ruff lint and format passed; 78 pytest cases passed |
| Frontend | ESLint and TypeScript passed; 39 Vitest cases and production Vite build passed |
| Playwright | 2 Chromium scenarios passed, including the real local Tesseract flow |
| Root npm audit | 0 known vulnerabilities |
| Frontend npm audit | 0 known vulnerabilities |
| `pip-audit` | 0 known vulnerabilities |
| Release audit | 94 candidate files plus Git history scanned; 0 findings |

Commit, tag, remote, and CI status must be verified from Git and GitHub; they are not inferred from
these local test results.

## API changes

Keep frontend requests in its central client, update Pydantic schemas and OpenAPI behavior together,
and add a route/service test. Avoid exposing raw absolute paths beyond the local user-facing project UI.
