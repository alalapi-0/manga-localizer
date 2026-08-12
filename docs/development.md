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

No model is downloaded by bootstrap or startup. To install the PP-OCR and LaMa models explicitly with
fixed SHA-256 verification, target the same data directory used by the application:

```bash
npm run setup:models -- --data-dir .manga-localizer ppocr lama
uv sync --project backend --extra ai --group dev
```

Without a repository `.env`, omit `--data-dir .manga-localizer` to use the normal
`~/.manga-localizer` default, or run `npm run setup:ai`. Real-ESRGAN is a CLI adapter; install
`realesrgan-ncnn-vulkan` and its model files separately and configure its executable when it is not on
`PATH`.

The 0.2.0 launcher was exercised through `npm run dev`: the root Web page, direct FastAPI
health endpoint, and the Vite `/api` proxy all responded successfully. Launcher platform logic also has
two Node tests in the unified `npm run check` gate.

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

It refuses a non-empty run directory and records source-checksum preservation, generated dimensions,
mask coverage, and changed pixels outside masks. These reports are stability/coverage evidence unless a
private ground-truth transcription/box set is supplied; region count and OCR confidence are not accuracy.

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
