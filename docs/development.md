# Development

## Bootstrap

Use Node.js 22.22.2 or newer and Python 3.12.

```bash
npm install
node scripts/external-uv.mjs sync --group dev
node scripts/external-frontend.mjs install
npx playwright install chromium
```

`npm run setup` installs the application dependencies without downloading a browser. Use
`npm run setup:test` when preparing to run Playwright. The backend intentionally targets Python 3.12.

On the governed local workstation, backend commands resolve `UV_PROJECT_ENVIRONMENT`, the uv cache, and
the frontend dependency tree only after the external SSD passes the shared Volume/Container UUID guard.
Run `npm run storage:check` to verify those routes. The local `backend/.venv` is intentionally a small
regular-file sentinel, while `frontend/node_modules` is the exact registered external link: unwrapped
fallbacks fail instead of silently recreating large internal environments. GitHub CI opts into a
repository-local frontend tree only when both `CI=true` and
`MANGA_LOCALIZER_CI_LOCAL_RUNTIME=1` are set, and routes uv to the runner's temporary directory.
Other missing-guard hosts fail closed; there is no implicit local fallback.

## Run

```bash
npm run dev
```

The Vite server proxies `/api` to FastAPI. `scripts/dev.mjs` loads the optional Git-ignored root `.env`
and passes its values to both processes. It resolves the required `MANGA_LOCALIZER_DATA_DIR` through the
governed external storage map; direct backend startup fails closed when that route is absent. The provided
`.env.example` does not define an internal data fallback. `MANGA_LOCALIZER_PORT`
and `MANGA_LOCALIZER_WEB_PORT` change the API and Web ports, and the launcher derives the Vite proxy
target automatically unless `VITE_DEV_API_TARGET` is set.

### Optional local models

Developer bootstrap and ordinary application startup still do not download models.
`npm run package:app` copies checksum-verified files from the UUID-guarded external model bundle
into `Manga Localizer.app`; it does not depend on a repository or home-directory model cache.
The packaged app reads `Contents/Resources/models/manifest.json` and reports
unavailable when a bundled file is missing or the SHA-256 does not match.

To install the PP-OCR and LaMa models explicitly with fixed SHA-256 verification:

```bash
npm run setup:models -- ppocr lama realesrgan
node scripts/external-uv.mjs sync --extra ai --group dev
```

Install the optional local Japanese-to-Chinese translator separately. It is not part of `setup:ai`:

```bash
npm run setup:models -- argos-ja-zh
node scripts/external-uv.mjs sync --extra mt --group dev
```

The guarded model bundle is independent of `MANGA_LOCALIZER_DATA_DIR`; that setting continues to
control catalogs and project data only. `realesrgan-onnx` is the local AI upscaler once that model
and the `ai` extra are installed. `realesrgan-ncnn` still wraps a separately
installed `realesrgan-ncnn-vulkan` executable; place the binary under the data directory or set
`MANGA_LOCALIZER_REALESRGAN_NCNN_COMMAND`.

For every real-data command below, first run `npm run storage:check`, then resolve the guarded root
with `npm run -s storage:data -- --print-real-data` and substitute that absolute result for
`<REAL_DATA_ROOT>`. Do not recreate a repository-local or home-directory data root.

Compare classic Lanczos against AI upscaling into an ignored output directory:

```bash
node scripts/external-uv.mjs run --with-guarded-models --extra ai python scripts/compare_upscale.py \
  --input <REAL_DATA_ROOT>/<dataset>/input \
  --output <REAL_DATA_ROOT>/<dataset>/runs/<new-run> \
  --factor 2
```

Compare local inpainting candidates on a synthetic line-art page, or on real pages with an
explicit mask, into an ignored output directory:

```bash
node scripts/external-uv.mjs run --with-guarded-models --extra ai python scripts/compare_inpaint.py \
  --synthetic \
  --output <REAL_DATA_ROOT>/<dataset>/runs/<new-inpaint-run>
```

Compare the local Argos Japanese-to-Chinese translator on synthetic phrases into an external output
directory. The summary stores character counts and CJK ratios rather than translations or OCR text:

```bash
node scripts/external-uv.mjs run --with-guarded-models --extra mt python scripts/compare_translate.py \
  --output <REAL_DATA_ROOT>/<dataset>/runs/<new-translate-run>
```

Promote ignored detector-draft annotation JSON after local visual review. Progress prints aggregate
counts only. `--list-pending` prints page IDs for local use and must not be pasted into public reports.
Accept/reject copies into a new ignored directory and never auto-promotes empty pages:

```bash
node scripts/external-uv.mjs run python scripts/review_detection_annotations.py \
  --annotations <REAL_DATA_ROOT>/<dataset>/annotations/<draft-set>
node scripts/external-uv.mjs run python scripts/review_detection_annotations.py \
  --annotations <REAL_DATA_ROOT>/<dataset>/annotations/<draft-set> \
  --output <REAL_DATA_ROOT>/<dataset>/annotations/<reviewed-set> \
  --accept <page-id> --reject <page-id>
```

The 0.2.0 launcher was exercised through `npm run dev`: the root Web page, direct FastAPI
health endpoint, and the Vite `/api` proxy all responded successfully. Launcher platform logic also has
Node tests in the unified `npm run check` gate, including the macOS `.app` skeleton.

```bash
npm run package:app
```

builds the app at the artifact destination returned by the guard only after the registered external
volume passes the storage identity check. `--install-user` never copies this
heavy bundle internally: it delegates to the canonical per-user maintenance installer, which refreshes
the existing governed thin entry at `~/Applications/Manga Localizer.app` from its fixed verified
template and fails if that managed entry or installer is unavailable. Pass `--skip-download` only when
every required model is already verified in the external model bundle. The `.app` and model weights
are gitignored.

## Verify

```bash
node scripts/external-uv.mjs run ruff check backend
node scripts/external-uv.mjs run ruff format --check backend
node scripts/external-uv.mjs run pytest backend/tests
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
npm run test:e2e
npm run audit:release
```

Tests create temporary Unicode directories and generated images; they never depend on network calls,
credentials, model downloads, or commercial manga.

### Real-data evaluation

Real material and all outputs live under the guarded external `<REAL_DATA_ROOT>`. The evaluator exercises
the real API and stores aggregate/per-file metrics without OCR text:

```bash
node scripts/external-uv.mjs run --extra ai python scripts/evaluate_real_data.py \
  --input <REAL_DATA_ROOT>/<dataset>/input \
  --output <REAL_DATA_ROOT>/<dataset>/runs/<new-run> \
  --stages preprocess,detect,ocr,inpaint,typeset,export \
  --detector-provider ppocr-v3 \
  --inpainter-provider lama-onnx
```

Unattended export defaults to `--export-format json`. Generated-image formats (`images` or
`both`) still require a current accepted page review and matching stage reviews; the evaluator
does not auto-accept empty or unreviewed pages.

It refuses a non-empty run directory and records source-checksum preservation, generated dimensions,
mask coverage, and changed pixels outside masks. These reports are stability/coverage evidence unless a
ground-truth transcription/box set is supplied; region count and OCR confidence are not accuracy.

Detection/OCR box evaluation is separate. Generate or point at annotation JSON, then write a sanitized
report that omits transcriptions, filenames, checksums, and paths:

```bash
node scripts/external-uv.mjs run --with-guarded-models python scripts/evaluate_detection_ocr.py \
  --synthetic \
  --detector ppocr-v3 \
  --output <REAL_DATA_ROOT>/synthetic-stress/runs/<new-run>
node scripts/external-uv.mjs run --with-guarded-models python scripts/bootstrap_detection_annotations.py \
  --input <REAL_DATA_ROOT>/<dataset>/input \
  --output <REAL_DATA_ROOT>/<dataset>/annotations/<new-draft> \
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

### Native G2 reconstruction

G2 reconstruction restores insufficient illustration detail after accepted G1; it is not G8 text
removal. Record an explicit G2 `yes`, then prepare immutable original and accepted G1 inputs:

```sh
npm run g2:image -- prepare --image-id <image-uuid> --runtime codex \
  --session-id <stable-session> --attempt-id <unique-attempt> \
  --prompt-path <absolute-prompt.txt> --prepare-dir <new-absolute-directory>
```

Inspect both inputs and call the executing Agent's native image tool with original first, G1 second.
Preserve identity, expression, composition, all original text/ruby/SFX, objects and grayscale style;
do not translate or erase lettering. The CLI never calls a model, accepts a candidate, uses a key,
or falls back to another provider. Import the actual returned raster:

```sh
npm run g2:image -- import --request-path <prepared-directory/request.json> \
  --raw-path <absolute-native-result.png>
```

Optional lettering lock keeps accepted G1 ink inside a same-grid mask while native-normalized pixels
remain outside. Raw is still the immutable native tool output; do not submit a local collage as
GenerateImage/image_gen raw. `--lettering-mask` binds a new invocation (mask digest is part of the
request) and the pending candidate is the deterministic composite:

```sh
npm run g2:image -- import --request-path <prepared-directory/request.json> \
  --raw-path <absolute-native-result.png> --lettering-mask <absolute-mask.png>
```

Preparation binds prompt, ordered inputs, actor/session, attempt, active generation and current G1/G2
events. Import verifies those bindings, refreshes only CAS fields, and returns pending. Raw, baseline
snapshot and normalized output are immutable; same-invocation replay must match all bytes/parameters.
Normalization is whole-frame only (upright single-frame PNG/JPEG/WebP, at most 40 MiB/32M pixels,
aspect difference at most 1%). Native discrete buckets that exceed 1% against the G1 target are
center cover-cropped first; the 1% gate then compares fitted vs target. Opposite-orientation raw
that cannot fit still fails closed. Provider identity still describes the raw source
(`operator-attested-client-supplied-unverified`). Lettering lock is a second deterministic stage,
replayed from stored mask bytes; default import without a mask is unchanged.

Use `GET /api/images/{id}/page-gates/reconstruction` for candidates and fresh CAS. Compare original,
G1 and candidate, then `PATCH .../reconstruction/candidates` with candidate/checksum, decision, all
eight ordered visual checks, expectedRevision and lineage. Only an accepted current candidate becomes
`GET /api/images/{id}/generated/quality`; pending/rejected/stale candidates cannot feed detection,
masks, G8 or strict freeze. `generated/preprocessed` remains the actual G1 image. Historical candidate
raw/baseline/normalized artifacts remain readable without restoring their production authority.

### Native-image coordinate registration

New strict G8 is native-cloud-only. Use `cloud:image --mode native` preparation, the current Agent's
native image tool, then pending-only import and explicit visual review. Local G8 enqueue, direct
production, queued recovery-to-success, fallback changes and candidate reviews return
`g8-native-cloud-required`; there is no config toggle or local-first prerequisite. Cloud failure
does not trigger another provider. Historical local artifacts and accepted lineage still replay
read-only, while exact artifact-free N/A and non-generative local stages remain valid. Tests may
construct pre-retirement history only within the explicit `historical_local_g8` context; current
policy tests run with the real guard. Model files and historical evidence are not deleted.

`cloud:image` keeps canonical whole-frame normalization v1 as its default. Native preparation and
ingest can explicitly select `--normalization-profile canonical-whole-frame-registration-v1` when
small whole-page coordinate drift produces strict-mask seams. Use the same selection for both steps.
This profile estimates only a bounded global affine transform from isolated, reciprocal SIFT matches
outside the edit mask, fits a deterministic robust model, and validates on separate spatial cells.
Insufficient or spatially concentrated support, held-out disagreement, excessive drift, or editable border padding
fails closed; it never falls back silently or applies a local non-rigid warp.

The existing strict mask composite still preserves every outside-mask pixel. Algorithm limits,
dependencies, input/support/match hashes, transform and validation evidence are frozen in the existing
normalization JSON; server ingest and replay recompute them rather than trusting a supplied matrix.
No database migration is needed. Same-invocation retries may refresh CAS fields, but cannot change
normalization evidence, even when output bytes happen to match. Existing v1 bytes/manifests and native
invocation identities are unchanged. Registration is not visual acceptance; a new generation and
independent G8 review remain necessary after a rejected candidate.

Final-review retries may change the parameter-set identity only when explicitly naming the current
generation with `retryFromGenerationId`. The new attempt starts at G0 from the immutable source and
freezes its own parameter ID/hash; all historical attempts retain their own G0 and creation-revision
bindings. Retrying the same parent is idempotent only for the already-created successor's exact
parameter pair. A plain repair request still requires the current head's parameter pair; a stale
ancestor cannot create a branch. Every lookup validates each attempt and the complete supersession chain.
