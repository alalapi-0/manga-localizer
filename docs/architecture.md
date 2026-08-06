# Architecture

Manga Localizer is a local single-user client/server application. The browser owns interaction state
and immediate canvas history. FastAPI owns durable state, safe filesystem operations, processing, and
exports. A future desktop wrapper may launch both without changing their API.

## Components

- **Workbench UI:** React, Zustand, and React Konva. It renders summaries from the API and sends
  debounced region patches. Undo/redo is local-first and each committed mutation produces a revision.
- **Application API:** FastAPI routers validate IDs, paths, settings, and provider inputs. Services,
  not routes, define transactions and processing workflows.
- **Project store:** one SQLite database beneath `output/project/`, plus a sanitized, inspectable JSON
  snapshot. Reopening requires both the manifest and its adjacent database; JSON-only import is not
  implemented. Generated artifacts live under fixed output categories and retain source-relative paths.
- **Provider registry:** runtime selection of OCR, translation, and inpainting implementations.
- **Typesetting engine:** deterministic Pillow renderer with font discovery and overflow reporting.
- **Job runner:** persisted job records and one process-local runner. It executes one job at a time,
  with a user-selected one-to-eight-item concurrency bound inside detection/OCR/translation/render
  jobs; filesystem export remains serialized. It converts interrupted `running` work back to
  resumable state at startup.

## Data and request flow

1. Import records each trusted local file/directory selection as a cumulative security boundary before
   decoding candidates, then validates every relative path, image type, dimension, and destination.
2. OCR stores regions and provenance; reading order is independently editable.
3. Translation receives the current text plus bounded preceding/following regions by reading order on
   the same page and records provider provenance.
4. Inpainting writes masks and previews outside source storage.
5. Typesetting reads the immutable source or repaired preview and writes a new rendered preview.
6. Export performs a final boundary/overwrite check, then exclusively creates new artifacts or uses
   atomic replacement for an explicitly selected overwrite where supported.

Region or upstream-provider changes invalidate every affected downstream status and generated file.
Preview endpoints and image export also require a current completed stage, so an old bitmap cannot be
silently paired with newly edited JSON.

## Reliability boundaries

SQLite foreign keys and transactions protect metadata. Project and region mutations use expected
revision guards; stale writes return a conflict. The frontend rebases project settings and newer local
region edits that arrive while an autosave is in flight onto the server revision, while unresolved
external conflicts remain visible to the user.

Import boundaries are cumulative across trusted-path imports and include selected files/directories
whose image candidates later fail validation. `Project.input_root` is only a nullable convenience
summary: platforms such as Windows cannot produce one for cross-drive selections, but the exact boundary
rows still protect originals. Relative-name collision checks compare each component with Unicode NFKC
normalization and case folding so a project remains safe on less-sensitive destination filesystems.

Runtime failures are stored as structured job/image errors and shown in the UI. Pause and cancel are
cooperative at item-batch boundaries: active items may finish, queued items stop, and persisted running
items/jobs are returned to the appropriate queued or cancelled state on restart. Running multiple API
workers against the same project is unsupported in the MVP.

Project snapshots and export overwrite operations use temporary siblings followed by atomic replacement.
A relative export output is canonicalized against the project root when the job is created, so restart or
a changed process working directory cannot redirect it. Custom bundle finalization is part of the export
job: the job stays nonterminal until the source/generated copy, sanitized SQLite database, and manifest
are complete. A job-derived owner marker authorizes cleanup of only that job's partial bundle; recovery
also removes its known SQLite `-journal`, `-wal`, and `-shm` sidecars before rebuilding.

A custom export root receives a reopenable project snapshot: immutable project-owned source copies,
current generated previews, a sanitized SQLite backup, and its JSON manifest. Machine-only original,
project, export, and exact import-boundary paths are removed from that backup, and `VACUUM` rebuilds the
database so deleted values do not remain in free pages. Delivery folders such as `translated/` remain
separate so users do not need to share the source-bearing project snapshot.

Remote base URLs must be HTTP(S), cannot embed credentials/query/fragment data, and may use plain HTTP
only on loopback. A remote endpoint or model change invalidates translation, typesetting, and export
status/artifacts. Unsafe legacy endpoint fields are removed when a project is reopened.

See [ADR 0001](adr/0001-local-first-modular-workbench.md) for rationale.
