# Data model

SQLite is authoritative. IDs are UUID strings and timestamps are UTC ISO-8601 values.

## Project

Stores name, input/output roots, source/target languages, provider identifiers, non-secret provider
settings, glossary, character dictionary, export settings, schema version, and timestamps. The JSON
snapshot mirrors portable settings and content but omits credentials and machine-only catalog state.
It is an inspectable companion manifest, not a JSON-only project import format; reopening requires the
adjacent SQLite database.

Custom export roots contain a complete reopenable snapshot, including project-owned `source/`, accepted
checksum-current canonical stage previews, and every append-only G7/G8/G10 lineage raster referenced by
the retained database. Omitted canonical visual stages are marked pending in the portable snapshot so
reopen never advertises a missing preview as current. Its SQLite copy clears original input paths, the
prior project root, cumulative import boundaries, and job `outputPath` options. It enables secure deletion
and runs `VACUUM` before atomic publication; the working project database may retain those local paths for
normal use.

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

Those status fields, `status.stageReviews.inpaint`, and both `generated/inpaint-candidates/` and
`generated/inpainted/` are legacy combined-workflow state only. A strict G8 active generation neither
reads nor writes them and cannot use them as acceptance or downstream-consumer authority.

When typesetting completes, `status.typesetOverflowCount` and `status.typesetOverflowRegionIds` record
which current boxes could not fit. Invalidating typesetting clears those fields. The API projects them
only while `status.typeset` is `done`. They are review hints, not an export hard gate. Vertical layout
maps CJK punctuation to presentation forms at render time and does not rewrite stored translation text.
Adjacent small boxes may share one render-time layout cluster; stored region geometry and text stay
separate. The workbench can rerun typesetting for persisted overflow region IDs without changing other
boxes when the last typeset plate is still on disk. If that plate is missing, the same region-scoped
job redraws every eligible box on the current inpaint plate. Public typeset job output includes
whether the run was a partial overlay, and the job queue card repeats that as overlay vs full-page
counts.

`status.preprocessSuggestion` records a local, non-binding profile hint from source-image size and a
native-resolution sample of contrast/sharpness. Import writes the sampled suggestion; listing an older
image without that record falls back to size only. Changing preprocessing settings or running a job
does not rewrite it, and it is never an automatic book-wide default.

## Page generation and lineage event

`page_generations` creates a new page-processing identity only for a fresh workspace page whose
relative path and bytes exactly match an open project's immutable source image. The record binds the
source project/image/checksum, run ID, page-generation UUID, parameter-set ID and SHA-256, explicit
`restartFromSource=true`, and the initiating actor/task/thread/session. A partial unique index permits
only one active generation per image; historical generations remain durable.

`page_lineage_events` is the append-only evidence stream for that generation. Each monotonically
sequenced row carries the operation/gate/state, actor and operation source, input/output/parent
checksums, provider/model fields, parameter hash, job/item/revision anchors, timestamps, decision and
structured evidence. SQLite triggers reject updates and deletes. The artifact/image-state transaction
also appends a `pending` production event; job-item completion appends a second pending event. A visual
gate cannot be accepted until both matching events exist for the exact checksum. The later explicit
checksum-observing stage review writes the `accepted` event. Human/agent decisions use both image
revision CAS and generation sequence CAS.

All strict artifact production/replay, read, and export paths validate the G0 identity for every
`page_generations` row, including native non-repair generations. Validation requires exactly one creation
G0, a contiguous full event sequence, matching actor and source/target checksums, the exact creation
`Revision`, and exact parameter-set, run, and source-reference bindings; repair-specific evidence is an
additional contract rather than the only strongly validated generation form. SQLite triggers
`revisions_g0_no_update` and `revisions_g0_no_delete` make a G0-linked creation Revision append-only and
recognize the existing generic G0 five-key evidence shape. Canonical
`page_lineage_events_no_update` and `page_lineage_events_no_delete` likewise make event rows append-only.
Together they prevent coordinated event/generation/Revision/actor/target drift, while service validation
still replays exact content and checks all four canonical definitions in `sqlite_master`. Any missing,
altered, or same-name weakened guard fails closed. Generic and final-review repair G0 creation performs this
exact check before every file or database write; failure is a zero-write 4xx, never a 201 followed by later
read-time discovery.

G2 reconstruction decisions observe the exact accepted G1 checksum. A decision that the baseline
preserves the original structure accepts that preprocessed checksum as the quality plate. A decision
that further reconstruction is needed appends a blocked event and does not expose the baseline as an
accepted G2 output; reconstruction candidates remain a separate, still-closed capability. The current
quality-plate resolver requires the latest G1 and G2 events to be ordered, accepted, checksum-current,
and connected by parent checksum.

G3 text-presence decisions observe both the immutable-original checksum and the accepted quality-plate
checksum. `yes` and `no` require explicit original/quality visual evidence; detector or OCR hints alone
cannot authorize either result. `uncertain` remains pending. A `no` decision additionally requires zero
regions, no active downstream job, pending text-processing stages, and no mask/inpaint/typeset artifacts
or provenance, then atomically records `no-text-reviewed` against the exact quality plate. The structured
evidence stays private; public lineage responses expose only non-content decision and checksum anchors,
not source/target relative paths or private evidence-code lists. Any later G2 decision resets the page
review, and final-review creation/staleness checks independently revalidate the latest ordered G2/G3
events before consuming an active generation's no-text quality plate.

A lineaged detect job requires the latest G3 decision to be accepted `yes` and binds its enqueue,
region-set publication, and completion evidence to the exact current quality-plate checksum. The G4
region-set checksum covers only stable region semantics: identity, normalized geometry, type,
direction, reading order, paragraph/ruby relationships, content disposition, and server-owned detector
candidate identity. It intentionally excludes OCR text, translation, confidence, recognition evidence,
repair, style, and row revision so later stages do not invalidate an accepted G4 decision. Every active
generation region create/update/delete/reorder appends a checksum-continuous pending event in the same
transaction as the mutation. G4 acceptance then requires the latest matching detector publication and
completion plus an in-bounds, contiguous-order region set with at least one non-false-positive semantic
region and whose candidates have explicit classification, direction, paragraph grouping, ruby
relationships, and content disposition. A
`false-positive` candidate may retain unknown type and no paragraph. Sound effects cannot use the
ordinary G4 `translate` disposition: localizable art text uses `redraw-art`, still traverses G6/G9 for
trusted source text and semantic review, and is reserved for the G10 art-lettering renderer.

G5 adds an all-null or all-present background evidence bundle to each region:
`backgroundCategory`, `backgroundConfidence`, `backgroundRationaleCodes`, `backgroundReviewer`, and
`backgroundGenerationId`. The category is one of `white-solid`, `black-solid`, `other-solid`,
`simple-gradient`, `screentone`, `complex-lineart`, or `illustration/character`; its controlled rationale
list must contain the matching category anchor. Only non-ruby `translate` and `redraw-art` regions are
eligible. The canonical G5 checksum includes every row's identity, eligibility, classification bundle,
reviewer, and generation binding, so residue on an ineligible row and out-of-band edits fail closed.
Classification writes and the explicit page acceptance/not-applicable decision use region/image/sequence
CAS and commit revisions plus lineage events in the same transaction. Legacy rows remain NULL; additive
SQLite checks and triggers reject malformed partial bundles without inventing historical reviewers.

G6 stores every strict OCR observation as an append-only `region_ocr_attempts` row. A lineaged OCR
job has one whole-page item and, for every non-ruby `translate` or `redraw-art` region, publishes exactly
one crop from the immutable original and one from the accepted quality plate. Each attempt binds the
generation, job/item, input variant and parent checksum, integer crop box and crop checksum, canonical
local provider, provider-observed model version, parameter hash, Japanese direction/language pack, raw
text checksum, and nullable confidence. Confidence zero is valid evidence and never grants or denies
trust. SQLite triggers reject attempt updates/deletes and malformed insertions; failed jobs publish no
attempts. Project reopen replaces the older translate-only validation trigger additively so existing
databases admit the expanded target set without fabricating evidence.

A source review selects one attempt and its same-job original/quality pair, records one of
`original-attempt`, `quality-attempt`, or `manual-correction`, and requires all nine controlled QC
checks. The server owns the reviewer/generation binding and derives warning flags. Review and page-gate
writes use region/image/sequence CAS and append their checksum-continuous lineage events in the same
transaction. A queued or running OCR item blocks both operations, including the interval after attempts
are published but before completion evidence exists. G6 accepts only when every eligible region has a
completed dual attempt and current source review; zero eligible regions require an explicit
`not-applicable` decision. Sequential reruns remain valid append-only history, while the selected review
must still point to one complete same-job pair.

G7 uses three generation-scoped tables rather than mutating the frozen region repair payload.
`page_mask_drafts` is the only mutable table: it stores one canonical recipe for every server-derived
eligible primary plus G6/quality parents, a recipe-state checksum, and a revision. Ruby membership is
derived from accepted G4 relationships and included in every state checksum; ruby has no independent
recipe switch. Floating recipe values are hashed as big-endian IEEE-754 binary64 tokens so Python and
browser replay cannot disagree over JSON spellings such as `1.0` versus `1`. A read-only context may
project revision-zero defaults, but only an explicit CAS mutation persists a draft and its lineage event.

`page_mask_artifacts` is append-only and unique per job item. Each row binds generation, job/item,
G6 and accepted-quality checksums, recipe checksum, deterministic provider/model/parameter identity,
integer render scale, immutable relative path, actual PNG checksum and grid, nonzero pixel count, and
nonzero bounding box. The file is published to a generation/artifact-specific path; its row and
`mask-artifact-produced` event commit together. Completion is a later required event, and recovery reuses
an already published and revalidated artifact without rerunning the rasterizer.

`page_mask_reviews` is append-only. A non-N/A review binds one current completed artifact and stores the
exact five coverage and five collateral `{check, passed}` results plus the server-owned reviewer.
Coverage keys are `body-glyphs-covered`, `punctuation-covered`, `strokes-and-shadows-covered`,
`ruby-covered`, and `antialias-edges-covered`. Collateral keys are `bubble-borders-protected`,
`characters-protected`, `speed-lines-protected`, `screentone-protected`, and `nearby-art-protected`.
Acceptance requires all ten true; rejection requires at least one failed check and a subsequent
draft/new artifact before later acceptance. Zero eligible primaries permit only an artifact-free
`not-applicable` review and reject a nonzero legacy residual mask. The terminal G7 checksum covers the
complete draft, artifact history, primary/ruby mapping, review history, parents, and actual mask
checksum; parameters or a canvas preview alone cannot authorize G8.

G8 uses `page_clean_plate_candidates` and `page_clean_plate_reviews`. Candidate rows are append-only,
unique per whole-page inpaint job item, and sequence-unique within a generation. Each row binds the
terminal G7 checksum, accepted quality/background checksums, exact accepted mask row/checksum, ordered
per-region route manifest, route/parameter checksum, honest origin (`deterministic`, `ai`, `classical`,
or `mixed`), provider/model sets, immutable PNG checksum/path/grid, and anomaly list. The database requires
`outside_mask_change_count = 0` and a generation-owned accepted-mask/job lineage. Service replay also
opens every stored candidate and recomputes RGBA pixel equality wherever the accepted L-mode mask is
zero; the stored metric alone is never trusted. Candidate files live only at
`generated/lineage-clean-plates/<generation-id>/<candidate-id>.png`.

`page_clean_plate_reviews` is append-only and permits one review per candidate plus at most one terminal
accepted/not-applicable review per generation. Non-N/A reviews bind a completed exact candidate and
store all seven unique boolean checks: outside-mask unchanged, source text unreadable, no white/gray
hole, no blur band, no repeated texture, continuous background, and preserved structure. Acceptance
requires all true; rejection requires the exact reason implied by the failed set. Zero eligible regions
permit only a candidate-free N/A review. Page-scoped fallback enable/disable decisions are immutable
lineage events with revision/CAS evidence; enable is allowed only after all applicable AI candidates in
the current prefix are explicitly rejected. G8 candidate/review rows, their lineage events, and the
revisions referenced by those events are protected against update/delete. Active generations reject the
legacy inpaint stage-review endpoint before any review reset or revision write. Allowlisted AI aliases
are normalized to the canonical provider id before route, job, candidate, and runtime identity checks.

G9 uses `region_translation_candidates`, `region_translation_reviews`, and
`page_translation_reviews`. Candidate rows are append-only and revision-ordered per eligible non-ruby
`translate` or `redraw-art` region; `keep-art`, `ignore`, and ruby are excluded. They bind the terminal G8
and clean-plate checksums, trusted source-text/revision checksum, bounded context checksum and policy,
canonical provider/model/parameter identity,
Simplified-Chinese target, translated-text checksum, origin (`model`, `manual`, `agent`, or `dictionary`),
optional whole-page job/item anchors, superseded candidate, and revision/event evidence. Translation text
remains private and is never copied into public lineage events.

Automatic jobs freeze a server-derived provider contract and complete page target set before insertion.
Manual and dictionary providers are revision-only; remote OpenAI-compatible execution requires explicit
authorization and a configured credential. Candidate rows and production events publish atomically after
the provider result, job completion is separate evidence, and recovery reuses an already published set. A
queued or running automatic job blocks revision writes, while any prior candidate history blocks another
automatic publication.

Each candidate has at most one immutable review with all ten controlled checks, computed and reviewer QC
flags, exact rejection reason, source/context/G8 checksums, reviewer, and revision/event anchor.
Acceptance requires all checks true and both flag sets equal to `none`; rejection must match the actual
failed check or flag. `page_translation_reviews` stores the one immutable accepted/not-applicable G9
terminal, its accepted candidate IDs, translation-state checksum, terminal checksum, reviewer, and
revision/event anchor. Strict replay checks the contiguous event grammar, exact row/revision/job/provider/
checksum/actor evidence, one-to-one cardinality, and absence of downstream events before the terminal.
Only accepted candidates may populate the legacy `translated_text` and provider compatibility fields;
all unaccepted regions keep those fields empty.

Old projects gain the tables and nullable job binding additively. No actor, run, generation, or G9
evidence is invented for historical revisions or jobs. While a generation is active, legacy job requests
and untracked page mutations fail closed. The active-generation path currently opens preprocessing,
detection, whole-page OCR, standalone whole-page mask generation, strict whole-page inpainting, and strict
whole-page translation and typesetting; their G1/G4/G6/G7/G8/G9/G10 gates; the G2/G3 decision endpoints;
and the per-region G5 background gate. G11 operates in the separate final-review database rather than as
a project job: strict batch creation, issue-only repair handoff, explicit refresh, and terminal export are
available only through their replay, locking, revision-CAS, and atomic-publication contracts. Later job
kinds and unsupported project mutations remain blocked until their own contracts are implemented.

## Strict G10 typesetting

`page_typeset_candidates` stores one immutable whole-page raster candidate per strict job item. Each row
binds the active generation, exact G9 terminal and translation-state checksums, accepted clean-plate row
and checksum, complete region/route/style/layout manifests and their checksums, renderer/model/parameter
identity, PNG checksum and canonical generation-scoped path, output grid/render scale, overflow region IDs,
anomalies, and its immutable revision. `page_typeset_reviews` stores at most one immutable conclusion per
candidate and at most one accepted terminal per generation. It repeats the observed candidate, parent,
route, style, layout, clean-plate, grid, and scale evidence; freezes the ordered eight visual checks,
reviewer, revision, and terminal state checksum; and accepts only a defect-free candidate.

Routes are derived rather than client-selected: translated dialogue/speech/thought become `bubble`, other
supported translated semantics become `ordinary`, `redraw-art` becomes `art-lettering`, and explicit
`keep-art`/`ignore` dispositions become non-rendering `keep`/`ignore`. Ruby and false positives are excluded,
and the route set must consume the accepted G9 candidate set exactly. Empty or unknown route sets fail
closed. Style manifests use checksummed installed CJK fonts. Ordinary routes do not claim affine or
visual-centre support; art lettering requires a checksummed display font and declares its supported
fill/stroke, combined rotation, non-uniform scale, shear, opacity, visual-centre, alignment, and line-spacing
features. Unsupported curve/AI requests fail instead of silently degrading to ordinary Pillow text.

The enqueue event freezes route/style contracts. Candidate-row, revision, and publication-event writes
are atomic; the immutable PNG is written at the canonical path before commit and deterministically recovered
if publication is retried. Job completion is a later event written in the same transaction as the item
terminal state. Review is blocked until that exact completion exists. Replay re-renders and hashes the PNG,
checks every manifest/row/revision/job/event/actor relation, and permits retries only after the previous
candidate is explicitly rejected. An accepted review is terminal and is the only G10 artifact authority
that G11 may consume.

Portable bundles retain every raster referenced by G7 mask, G8 clean-plate, and G10 typeset rows. They
preserve only catalog-validated public font capability tokens while scrubbing credential-like values, and
hold the project writer lock across asset discovery, copying, verification, and SQLite backup so the
portable database and files represent one writer-excluded snapshot.

## Final-review batch

A final-review batch is deliberately separate from every project database. Its manifest and SQLite
database sit beside revision-scoped frozen evidence and thumbnails. Each item is identified by the pair
`(source_project_id, source_image_id)`, has a stable 1-based position, records whether its final variant
is `typeset` or no-text `preprocess`, and anchors its artifact revision with SHA-256 checksums and an
evidence digest. Format-v2 creation is atomic and strict-only: immutable original, accepted quality plate,
mask, clean plate, and accepted final descriptors must all be frozen. Each descriptor records availability,
grid/checksum, generation, producer, terminal event/revision, and artifact revision. No-text and legitimate
text-present keep/ignore routes use explicit `not-applicable` mask/clean descriptors; `unavailable` is
reserved for absent legacy evidence. Artifact-revision and review-history rows are append-only, and reads
recompute their digests instead of trusting stored JSON. Creation acquires all open project writer locks in
stable resolved store-root order before strict replay and retains them through evidence freezing, final-review
database commit, atomic directory publication, and its successful 201 response.

The current verdict is `pending`, `approved`, or `issues`. Issue records use normalized multi-select
codes and optional free-form feedback; `issues` requires at least one code, and `other` requires
feedback. Every mutation increments `revision`, writes a history row, and uses compare-and-swap so a
stale browser cannot silently replace a newer judgment. Repair creates or idempotently reuses an isolated
image/generation from the immutable source under the source project transaction. Its G0 binds the exact
final-review item revision and a checksum of issue codes/feedback; public lineage excludes the raw feedback.
The client additionally requires the handoff to keep the source project identity, use a repair image distinct
from the source image, and match the derived run/default-parameter contract before it may navigate.
An idempotent repair retry must match the existing generation's parameter ID/hash; the response reads those
stored values and cannot substitute the retry request's metadata. Candidate discovery collects every G0
whose evidence binds the same `(itemId, itemRevision, feedbackChecksum)`; zero permits creation, exactly one
may be reused, and multiple candidates fail closed. Before reuse, the shared repair/strict-refresh validator
requires the complete generation contract, exactly one G0 event, its exact creation `Revision`, a contiguous
event sequence, the immutable source record and bytes, and repair-target metadata, canonical path, checksum,
decoded resolution, and physical distinctness from that source. The G0 evidence object and creation
`Revision` payload require their exact key sets and JSON scalar types: numeric or boolean values that compare
equal in Python, including `2.0 == 2` and `true == 1`, remain invalid substitutes. Both exact objects persist
the generation's `parameterSetId` and `parameterSetHash`, and the post-refresh handoff carries the same
anchors. Any parameter drift among generation, G0, Revision, and handoff causes retry, refresh, and approved
export to fail closed. Any strict item retaining
a repair handoff replays the same validator on refresh in every verdict state, including `pending` and
`approved`, and the stored handoff generation/image must equal the unique validated candidate.
After project commit, the final-review service rechecks item and batch revisions rather than claiming a
cross-database transaction. Strict refresh observes the fixed final-review-then-source-project lock order;
the source writer lock spans terminal-artifact reads, evidence freezing, and the final item/batch CAS, so a
returned success cannot already be stale. Synchronizing accepts only that exact repair handoff with a
current strict terminal, freezes a new evidence revision, keeps old revision URLs readable, writes history,
and resets the item to `pending`. A failed refresh rolls back its lazy legacy schema changes and removes
unpublished files.

The final-review database does not replace or mutate `status.stageReviews`. Batch initialization reads
strict accepted lineage and never authorizes a page from legacy display status. Any missing qualification,
identity collision, copy mismatch, checksum mismatch, or incomplete current artifact revision aborts the
whole format-v2 batch. Format-v1 open/list is read-only and does not silently migrate the database. Its
reviewed approved/issue items are mutation-locked; issue repair plus successful synchronization upgrades
only that item to strict evidence within the hybrid batch. A v1 public final descriptor has a revisioned
URL, frozen checksum, and relative path, while its grid, resolution digest, generation, producer, and
terminal fields remain null; conflict reload accepts only that exact legacy grammar.

Final-review export always requires actor provenance and the expected batch revision, including format-v1
and hybrid batches. It is unavailable until `approved == itemCount > 0` and both pending and issue counts
are zero. Only an item update carrying human actor provenance may transition an item to `approved`; Codex,
Cursor, and system actors cannot create final approval. Export revalidates that terminal count state,
every frozen checksum/evidence digest, and the batch revision again under lock immediately before
publication, namespaces source projects to resolve repeated
relative paths, requires collision-safe renaming rather than skipping any approved item, and creates a new
destination atomically. Its aggregate manifest contains source identity and file/checksum metadata, but no
OCR text, translation text, or review feedback. A mutation whose result cannot be proved authoritative
places the client in a global conflict state; only a non-regressing reload with the exact batch identity,
coherent counts/item membership, strict item grammar, and the active item can clear it.
An export success is authoritative only when the returned resolved output directory matches the requested
target and its manifest path is exactly `manifest.json` inside that directory.
That strict item grammar recomputes the Python-compatible canonical SHA-256 for every available grid and
for the complete stored evidence payload after excluding the public response-only `url` fields.
The exporter likewise acquires all open project writer locks in stable resolved store-root order before its
first currentness read and keeps them through cross-project upstream replay, copying, repeated currentness/
evidence checks, and atomic publication. A source acceptance therefore either precedes the snapshot or waits
until publication ends, even when the upstream source belongs to another open project.

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

G4 adds nullable `paragraphGroupId`, `rubyParentId`, and `contentDisposition` fields. The disposition is
separate from recognition trust and is one of `translate`, `ignore`, `keep-art`, `redraw-art`, or
`false-positive`. Detector-created rows also carry the server-owned pair `detectorJobItemId` and
`detectorCandidateIndex`; clients cannot create or edit that pair. Ruby parents must be non-ruby regions
on the same page, cannot be false-positive, cannot form self/nested relationships, and share the
paragraph group when both groups are present. Project JSON snapshots and portable SQLite bundles retain
these fields. Legacy databases gain nullable columns, indexes, and relationship triggers idempotently,
preserving historical rows as NULL rather than fabricating detector or classification provenance.

Opening a legacy SQLite project idempotently adds/backfills the recognition column and advances project
schema version to 2. A legacy explicit confirmation maps to trusted; ambiguous legacy rows map to
review. The migration also invalidates old translation/inpaint/typeset/export state and removes cached
repair/typeset artifacts created under the former confidence policy. Source, geometry, type, direction,
confidence, provider/language, or recognition provenance changes invalidate trust, while translation,
typography, and ordinary mask edits retain it.
Translation can retain OCR trust while clearing the separate current-content confirmation flag; that
combination marks typesetting stale without discarding a current inpaint plate. Page
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
running work becomes resumable, including a running item whose parent was marked failed by an outer
worker error. Previously failed sibling items are not rerun implicitly. A running item whose parent was
already cancelled becomes cancelled and is available to explicit retry; paused parents remain paused.

`lineage_context` is a private, structured column separate from provider options. A job that targets
active page generations must bind exactly the same run and image-to-generation set and supply the
current operation actor. The worker repeats that validation immediately before every item and again
before export-bundle finalization. Enqueue and terminal item events preserve the job/item IDs; restart
recovery retains the context and does not manufacture duplicate evidence.
Pause/resume/cancel/retry actions on lineage-bound jobs currently fail closed until those requests can
carry a new actor and per-page sequence evidence; they never reuse the creator's actor implicitly.

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
