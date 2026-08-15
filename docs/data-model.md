# Data model

SQLite is authoritative. IDs are UUID strings and timestamps are UTC ISO-8601 values.

## Project

Stores name, input/output roots, source/target languages, provider identifiers, non-secret provider
settings, glossary, character dictionary, export settings, schema version, and timestamps. The JSON
snapshot mirrors portable settings and content but omits credentials and machine-only catalog state.
It is an inspectable companion manifest, not a JSON-only project import format; reopening requires the
adjacent SQLite database.

Custom export roots contain a complete reopenable snapshot, including project-owned `source/` and
only accepted, checksum-current `generated/` artifacts. Omitted visual stages are marked pending in
the portable snapshot so reopen never advertises a missing generated file as current. Its SQLite copy
clears original input paths, the prior project root, cumulative import boundaries, and job `outputPath`
options. It enables secure deletion and runs `VACUUM` before atomic publication; the working project
database may retain those local paths for normal use.

## Image

Stores `image_id`, `source_path`, normalized `relative_path`, dimensions, checksum, processing/status
fields, timestamps, and structured errors. The unique key is project plus relative path.

`status.stageReviews` is keyed by `preprocess`, `inpaint`, and `typeset`. Pending is represented by an
absent entry. Accepted/rejected records contain `reviewedAt`, `resultRevision`, and the exact
`artifactChecksum`; inpaint also contains `maskChecksum`. The client computes these values from the
same response bytes it decodes in the review canvas, and the server recomputes and compares both before
persisting the decision. Rejecting or withdrawing an upstream result, regenerating it, or accepting
different upstream bytes clears dependent reviews.

When inpainting produces a nonempty mask, `status.inpaintCandidate` names the selected plate and
`status.inpaintCandidates` lists compact ids, labels, and anomaly flags. Candidate PNG files live under
`generated/inpaint-candidates/` and are local working files; portable bundles keep only the selected
canonical `generated/inpainted/` artifact.

When typesetting completes, `status.typesetOverflowCount` and `status.typesetOverflowRegionIds` record
which current boxes could not fit. Invalidating typesetting clears those fields. The API projects them
only while `status.typeset` is `done`. They are review hints, not an export hard gate. Vertical layout
maps CJK punctuation to presentation forms at render time and does not rewrite stored translation text.
Adjacent small boxes may share one render-time layout cluster; stored region geometry and text stay
separate. The workbench can rerun typesetting for persisted overflow region IDs without changing other
boxes.

`status.preprocessSuggestion` records a local, non-binding profile hint from source-image size and a
native-resolution sample of contrast/sharpness. Import writes the sampled suggestion; listing an older
image without that record falls back to size only. Changing preprocessing settings or running a job
does not rewrite it, and it is never an automatic book-wide default.

## Import boundary

Each trusted local import selection stores an exact file or directory `ImportBoundary` before any
candidate is decoded. Rows accumulate across import operations and remain present even when a selected
candidate later fails image validation. They, rather than the nullable `Project.input_root` summary, are
the authoritative no-overwrite set. `input_root` can be empty when the platform cannot calculate a
common path, notably for selections spanning Windows drives.

## Text region

Stores region/image IDs, a rotatable rectangular bounding box, direction, reading order, source and
translated text, compatibility confidence, text type, ignored/confirmed flags, provider provenance,
typography, mask settings, timestamps, and a versioned `recognition` object. Recognition v1 contains
separate detector evidence, OCR evidence and attempts/selected input/language, plus trust policy version,
disposition (`review`, `trusted`, or `ignored`), and a stable reason code. The API projects the separate
detector/OCR confidence values and trust state as read-only fields. Automatic evidence never grants
trust; explicit confirmation does. Unknown policy versions fail closed to review while readable
detection/OCR evidence remains intact. OCR attempts accumulate across reruns and the selected index is
relative to that cumulative history. Preprocessed evidence is a variant label rather than an immutable
artifact identity, so changing preprocessing output, provider, or settings revokes trust that depended
on it.

Opening a legacy SQLite project idempotently adds/backfills the recognition column and advances project
schema version to 2. A legacy explicit confirmation maps to trusted; ambiguous legacy rows map to
review. The migration also invalidates old translation/inpaint/typeset/export state and removes cached
repair/typeset artifacts created under the former confidence policy. Source, geometry, type, direction,
confidence, provider/language, or recognition provenance changes invalidate trust, while translation,
typography, and ordinary mask edits retain it.
Translation can retain OCR trust while clearing the separate current-content confirmation flag; page
review requires every active region to be both trusted and confirmed after its latest content edit.

Region numbers shown in the UI derive from editable reading order.
`repair.maskEdits` stores a bounded versioned sequence of add/erase strokes, each with a radius and
canonical image-coordinate points. Arbitrary polygon regions are not persisted by the MVP schema.

## Revision

Stores entity type/ID, operation, before/after JSON, and timestamp. It supports audit and durable
recovery; the frontend also keeps a bounded immediate undo/redo stack for fast canvas interaction.
Mutable projects and regions expose monotonically increasing revisions. Update/delete requests carry
the expected revision, and a mismatch fails with a conflict instead of applying a stale write. Autosave
rebases newer queued local mutations onto the acknowledged revision before sending the next mutation.
Stage-review mutations use the image revision guard and append a `stage-review` revision record.

## Job

Stores type, status, provider/options payload, progress counts, error details, and timestamps. Ordered
job-item rows store the requested image/region IDs, per-item state, output metadata, and start/finish
times. A persisted concurrency option bounds parallel item work from one to eight; export normalizes it
to one. Item-level failures do not discard successes. Pause and cancel take effect between item batches
rather than interrupting image processing already running in a worker thread. On restart, interrupted
running work becomes resumable; a running item whose parent was already cancelled becomes cancelled and
is available to explicit retry.

Public job stage outputs expose only an explicit operational/aggregate projection: provider and
size/count metrics, confidence buckets, trust dispositions, and stable reason counts rather than OCR
text, coordinates, region identifiers, internal options, or filesystem paths. Public error messages are
stage-specific but fixed; detailed errors remain private project state. Operational job/item/image IDs
remain in the response so the local workbench can control and label queued work. The authoritative
database and portable project bundle retain the full job state needed for recovery. Text JSON exports are
intentionally user content and additionally include detector/OCR confidence and trust metadata; treat
them as private project data.

Export jobs persist `bundleFinalized=false` when created and do not become terminally completed until the
portable bundle is atomically finalized. Job-scoped owner markers distinguish recoverable partial output
from unrelated content and permit cleanup of recognized temporary database files and SQLite sidecars.
Generated-image export additionally requires explicit page review and accepted, checksum-current visual
stages. A typeset export depends on both inpaint and typeset acceptance. JSON-only export is not gated by
visual review. Each completed export item records the image revision used for its files. A retry requeues
an already completed page if that revision has since changed; replacing such stale output requires the
explicit `overwrite` conflict policy, otherwise the user starts a new export job.

## Path rules

Relative paths use POSIX separators in metadata and are normalized on filesystem access. Absolute,
drive-qualified, empty, dot-dot, NUL-containing, or resolved escaping paths are rejected. Source and
output targets are compared after canonical resolution before any export. Every generated destination,
including the portable manifest/database pair, is checked against all recorded trusted-local originals.
Portable collision keys normalize every path component with Unicode NFKC and `casefold`; import rename
and export rename/skip/overwrite behavior therefore does not depend on the host filesystem's case or
Unicode-normalization sensitivity. Relative custom export roots are resolved against the project root at
job creation and persisted as absolute paths for recovery.
