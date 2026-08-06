# Data model

SQLite is authoritative. IDs are UUID strings and timestamps are UTC ISO-8601 values.

## Project

Stores name, input/output roots, source/target languages, provider identifiers, non-secret provider
settings, glossary, character dictionary, export settings, schema version, and timestamps. The JSON
snapshot mirrors portable settings and content but omits credentials and machine-only catalog state.
It is an inspectable companion manifest, not a JSON-only project import format; reopening requires the
adjacent SQLite database.

Custom export roots contain a complete reopenable snapshot, including project-owned `source/` and
state-consistent `generated/` trees. Its SQLite copy clears original input paths, the prior project
root, cumulative import boundaries, and job `outputPath` options. It enables secure deletion and runs
`VACUUM` before atomic publication; the working project database may retain those local paths for normal
use.

## Image

Stores `image_id`, `source_path`, normalized `relative_path`, dimensions, checksum, processing/status
fields, timestamps, and structured errors. The unique key is project plus relative path.

## Import boundary

Each trusted local import selection stores an exact file or directory `ImportBoundary` before any
candidate is decoded. Rows accumulate across import operations and remain present even when a selected
candidate later fails image validation. They, rather than the nullable `Project.input_root` summary, are
the authoritative no-overwrite set. `input_root` can be empty when the platform cannot calculate a
common path, notably for selections spanning Windows drives.

## Text region

Stores region/image IDs, a rotatable rectangular bounding box, direction, reading order, source and
translated text, OCR confidence, text type, ignored/confirmed flags, provider provenance, typography,
mask settings, and timestamps. Region numbers shown in the UI derive from editable reading order.
Arbitrary polygon regions are not persisted by the MVP schema.

## Revision

Stores entity type/ID, operation, before/after JSON, and timestamp. It supports audit and durable
recovery; the frontend also keeps a bounded immediate undo/redo stack for fast canvas interaction.
Mutable projects and regions expose monotonically increasing revisions. Update/delete requests carry
the expected revision, and a mismatch fails with a conflict instead of applying a stale write. Autosave
rebases newer queued local mutations onto the acknowledged revision before sending the next mutation.

## Job

Stores type, status, provider/options payload, progress counts, error details, and timestamps. Ordered
job-item rows store the requested image/region IDs, per-item state, output metadata, and start/finish
times. A persisted concurrency option bounds parallel item work from one to eight; export normalizes it
to one. Item-level failures do not discard successes. Pause and cancel take effect between item batches
rather than interrupting image processing already running in a worker thread. On restart, interrupted
running work becomes resumable; a running item whose parent was already cancelled becomes cancelled and
is available to explicit retry.

Export jobs persist `bundleFinalized=false` when created and do not become terminally completed until the
portable bundle is atomically finalized. Job-scoped owner markers distinguish recoverable partial output
from unrelated content and permit cleanup of recognized temporary database files and SQLite sidecars.

## Path rules

Relative paths use POSIX separators in metadata and are normalized on filesystem access. Absolute,
drive-qualified, empty, dot-dot, NUL-containing, or resolved escaping paths are rejected. Source and
output targets are compared after canonical resolution before any export. Every generated destination,
including the portable manifest/database pair, is checked against all recorded trusted-local originals.
Portable collision keys normalize every path component with Unicode NFKC and `casefold`; import rename
and export rename/skip/overwrite behavior therefore does not depend on the host filesystem's case or
Unicode-normalization sensitivity. Relative custom export roots are resolved against the project root at
job creation and persisted as absolute paths for recovery.
