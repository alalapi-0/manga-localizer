# Architecture

Manga Localizer is a local single-user client/server application. The workbench UI and FastAPI share
one origin in application mode: loopback by default, or one private LAN IPv4 when `npm run app:lan`
or the packaged app `--lan` flag is started explicitly. The browser, Mac application window, or
same-Wi-Fi phone Safari session owns interaction state and immediate canvas history. FastAPI owns
durable state, safe filesystem operations, processing, and exports. `npm run package:app` builds a
local `Manga Localizer.app` that supervises that API, serves the built UI from the bundle, and
selects checksum-verified models copied at package time. `npm run app` remains the source-tree
prototype.

## Components

- **Workbench UI:** React, Zustand, and React Konva. It renders summaries from the API and sends
  debounced region patches. Undo/redo is local-first and each committed mutation produces a revision.
- **Application API:** FastAPI routers validate IDs, paths, settings, and provider inputs. Services,
  not routes, define transactions and processing workflows.
- **Project store:** one SQLite database beneath `output/project/`, plus a sanitized, inspectable JSON
  snapshot. Reopening requires both the manifest and its adjacent database; JSON-only import is not
  implemented. Generated artifacts live under fixed output categories and retain source-relative paths.
- **Page-lineage authority:** normalized page-generation rows anchor a fresh rework page to immutable
  source identity and parameter evidence. Database-enforced append-only events record actors, jobs,
  revisions, checksums, decisions, and gate state without placing private OCR or translation text in
  public responses.
- **Final-review workspace:** an application-owned, cross-project review batch with its own SQLite
  database, manifest, frozen final PNGs, and thumbnails. It keeps `(projectId, imageId)` source links
  for repair without turning the batch into another processing project or changing project-review
  semantics.
- **Provider registry:** exact runtime selection of preprocessing, text detection, OCR, translation,
  and inpainting implementations. Optional model/runtime failures remain isolated health states.
- **Typesetting engine:** deterministic Pillow renderer with font discovery, overflow reporting, and
  persisted per-page overflow IDs for workbench review.
- **Job runner:** persisted job records and one process-local runner. It executes one job at a time,
  with a user-selected one-to-eight-item concurrency bound inside detection/OCR/translation/render
  jobs; filesystem export remains serialized. It converts interrupted `running` work back to
  resumable state at startup, including an item stranded beneath a failed parent, without implicitly
  rerunning already failed siblings.

## Data and request flow

1. Import records each trusted local file/directory selection as a cumulative security boundary before
   decoding candidates, then validates every relative path, image type, dimension, and destination.
2. Optional preprocessing writes a separate PNG plus scale provenance. Detection and OCR consume it,
   but every persisted region is mapped back to immutable source coordinates. Low/empty processed-crop
   OCR is retried on the original crop.
3. Detection stores polygon/box provenance. A completed empty result is authoritative; OCR does not
   silently substitute a second detector. Strict lineaged OCR stores append-only original/quality crop
   attempts, provider/model/checksum provenance, and a separate human source-text/QC decision; reading
   order is independently editable.
4. Strict page generations derive G7 eligibility from non-ruby `translate`/`redraw-art` primaries and
   attach their ruby children server-side. A standalone deterministic mask job consumes the current
   checksum-bound recipe and accepted quality plate, publishes an immutable actual PNG, and stops before
   inpainting. Its five coverage checks cover body glyphs, punctuation, outlines/shadows, linked ruby,
   and antialias edges; its five collateral checks protect bubble borders, characters, speed lines,
   screentone, and nearby artwork. G8 consumes only that accepted mask and the accepted quality plate,
   routes each region by its G5 background class, and publishes generation-scoped immutable clean-plate
   candidates with exact route/provider/model and zero-outside-mask evidence. The legacy combined
   mask/inpaint path is not an authority for strict pages.
5. Strict G9 consumes only terminal G8 evidence. One whole-page automatic job freezes the canonical
   provider/model, target language, bounded same-page context policy, parameter hash, and complete target
   set before execution; manual, Agent, and dictionary edits instead append dedicated revisions. Every
   immutable candidate has an exact ten-check review, and only an accepted latest candidate for each
   eligible non-ruby `translate` or `redraw-art` region permits the immutable page terminal decision.
   The latter remains a semantic translation target here and is routed separately as art lettering in
   G10; `keep-art`, `ignore`, and ruby never enter the G9 target set.
6. Strict G10 consumes only the exact terminal G9 state and accepted clean plate. One whole-page job
   freezes region/route/style manifests, installed-font checksums, renderer identity, and parameters.
   Bubble and ordinary text use the regular renderer; non-ruby `redraw-art` uses a distinct display-font
   art-lettering renderer with declared affine/compositing capabilities; `keep-art` and `ignore` do not
   render. Candidate PNGs are generation-scoped and immutable. A candidate becomes reviewable only after
   its publication and job-completion events, and acceptance requires the exact eight visual checks plus
   zero recorded overflow or anomalies. Rejected styles may seed a new immutable whole-page retry. The
   legacy render and stage-review paths remain unable to authorize an active generation.
7. Final review can freeze accepted typeset results, or accepted preprocess results for explicitly
   reviewed no-text pages, into one cross-project batch. Operator verdicts and issue feedback persist
   independently from the processing-stage reviews.
8. Project export performs a final boundary/overwrite check, then exclusively creates new artifacts or
   uses atomic replacement for an explicitly selected overwrite where supported. Final-review export
   is a separate terminal all-approved operation into a new, non-existing destination.

Region or upstream-provider changes invalidate affected downstream status. Typesetting-only edits keep
the last typeset plate on disk so a region-scoped rerun can overlay selected boxes; preview endpoints
and image export still require a current completed stage, so an old bitmap cannot be silently paired
with newly edited JSON. If that typeset file is missing while the clean plate is still current, the
job redraws every eligible box instead of compositing onto a blank plate. Inpaint invalidation still
removes repair, mask, and typeset artifacts.

The canonical pipeline is:

```text
immutable source
  -> optional preprocess artifact
  -> text detection (canonical coordinates)
  -> dual-crop OCR + explicit source-text trust
  -> editable/reviewed regions
  -> immutable actual mask + explicit coverage/collateral acceptance
  -> background-routed OpenCV or LaMa restoration + clean-plate acceptance
  -> translation + explicit confirmation
  -> Pillow typesetting + acceptance
  -> final review + conflict-safe export
```

## Reliability boundaries

SQLite foreign keys and transactions protect metadata. Project and region mutations use expected
revision guards; stale writes return a conflict. The frontend rebases project settings and newer local
region edits that arrive while an autosave is in flight onto the server revision, while unresolved
external conflicts remain visible to the user.

An active page generation adds a second authority boundary. Jobs must carry an exact run/page binding
and actor context at enqueue, and the worker revalidates immutable source checksum and active-generation
identity immediately before mutation. Every strict artifact production/replay, read, and export first applies
the same generation validator to native and repair generations: exactly one creation G0, contiguous event
sequences, exact actor and source/target checksums, exact creation Revision, and matching parameter set,
run ID, and source references. A valid generation row alone is never sufficient. The artifact and image
status commit together with a pending production event; item completion adds separate pending evidence.
The canonical `revisions_g0_no_update` and `revisions_g0_no_delete` SQLite triggers protect every G0-linked
creation Revision from update or deletion while recognizing the established five-key generic G0 evidence
shape; canonical `page_lineage_events_no_update` and `page_lineage_events_no_delete` protect the event rows.
These database guards prevent coordinated event/generation/Revision/actor/target drift. The validator
independently replays exact content and verifies all four definitions from `sqlite_master`; any missing,
altered, or same-name weakened guard fails closed before strict consumption. Both the generic and
final-review repair G0 creators perform the same exact guard check before any file/database mutation;
failure returns a zero-write 4xx rather than a 201 whose weakness is discovered only on replay.
A checksum-bound stage decision
must observe both records and uses the generation's next sequence as compare-and-swap before it can append
accepted visual-gate evidence. SQLite triggers reject lineage-event update or deletion; older projects
receive the tables and nullable job binding without fabricated history. G2 can accept the exact G1
artifact as the quality plate or record that reconstruction is required without pretending a candidate
exists. G3 then binds a visual text-presence decision to immutable-original and quality-plate checksums;
no-text acceptance also proves that no downstream text state or artifact remains. A later G2 decision
revokes the page-level no-text review, and the final-review service rechecks current ordered lineage
instead of trusting status alone. G4 detection requires the current G3 `text-present` decision, reads the
exact accepted quality plate outside the write transaction, and then revalidates image/region revisions
and the G3/quality anchors before atomically replacing only undecided detector candidates, updating image
state, and appending region-set production evidence. The canonical detector identity must match across
enqueue, runtime publication, completion, and G4 acceptance. A crash before item completion can safely republish
that item without duplicate candidate identities; operator-decided or manual regions survive later
detection. Active-generation region edits use both image revision and lineage sequence CAS and append a
checksum-continuous mutation event. G4 acceptance additionally requires the matching detector completion
and validates geometry, order, classification, paragraph/ruby structure, and content disposition.
G5 begins only from that current accepted G4 checksum. Exactly the non-ruby `translate` and `redraw-art`
regions require one of seven controlled background classes, a finite 0–1 confidence value, a category-
anchored rationale set, the server-derived reviewer actor, and the active generation id. Confidence is
evidence only and has no automatic acceptance threshold. Each classification commits the region/image
revision and checksum-continuous lineage event atomically. The page gate recomputes the full eligibility
and classification projection under image/sequence CAS; it records `accepted` only when every eligible
region is complete, or `not-applicable` only when the authoritative eligible set is empty. Any G5 event
locks later G4 edits and detection. The frontend independently derives the current phase from a stable
generation/events/generation snapshot, keeps uncertain outcomes locked until manual reload, and exposes
G5 boxes as selectable but geometry/mask read-only against only the immutable original and accepted
quality plate.
G6 begins only from that current accepted G5 checksum. One whole-page local OCR item publishes exactly
one immutable-original crop and one accepted-quality crop for every non-ruby `translate` or `redraw-art`
region, with server-observed provider/model, Japanese direction/language, crop and text checksums, and
confidence as evidence only. Attempts and their publication event commit atomically and remain append-only
across sequential reruns; item completion is separate required evidence. A restart after publication
recovers to completion without calling the provider twice. Source review selects one same-job pair, records its
source mode, all nine QC checks, server-derived flags, and the current actor/generation. Review and gate
acceptance are blocked while any OCR item is queued or running. The terminal gate requires every
eligible region to have completed dual evidence and a current review, or an explicit `not-applicable`
decision for an empty eligible set. The frontend replays exact decisions, counts, and checksums
fail-closed and revalidates the authoritative G6 context when opening G7.
G7 begins only from current accepted or explicitly not-applicable G6 evidence. A mutable draft contains
one canonical recipe per eligible primary; linked ruby is derived and rasterized by the server and cannot
be disabled independently. Each whole-page mask job binds the G6 state, accepted quality checksum,
recipe checksum, integer render scale, deterministic algorithm identity, image revision, and lineage
sequence. It writes an immutable generation/artifact path and atomically publishes actual PNG checksum,
grid, nonzero count, bounding box, primary/ruby mapping, and a pending event; completion is separate and
required. Rejected reviews preserve all ten structured results and require a later draft plus new artifact
before acceptance. Zero eligible regions can only record an explicit artifact-free not-applicable event.
The frontend replays the exact event grammar, reloads the authoritative context on cold G7/G8 entry, and
accepts only the browser-observed bytes and grid for the current recipe.
The ten results are five removal-coverage decisions (body glyphs, punctuation, outlines/shadows, linked
ruby, and antialias edges) plus five collateral-protection decisions (bubble borders, characters, speed
lines, screentone, and nearby artwork). A coverage-only defect stays in G7 and requires a revised draft and
new artifact. If the evidence instead shows that accepted G4 geometry, paragraph/ruby ownership, or
disposition was wrong, the rejected generation remains immutable and execution restarts from the
immutable source in a fresh generation/workspace before redoing G4. The G5 lock intentionally forbids an
in-place rewrite of that earlier evidence.

G8 begins only from terminal G7 evidence. A whole-page inpaint enqueue freezes the current background
and quality checksums, accepted mask identity/checksum, and an ordered route manifest. Solid, gradient,
and screentone classes use deterministic routes; complex line art and character/illustration classes use
an allowlisted AI redraw route. Each job item can publish one append-only candidate at
`generated/lineage-clean-plates/<generation>/<candidate>.png`; the row and production event commit
together, and a later completion event is required before review. Replay reopens every candidate PNG,
rechecks its SHA-256/grid, decodes the accepted quality and L-mode mask, and recomputes RGBA differences
outside `mask == 0`; any changed outside pixel locks the page. Duplicate workers serialize publication
and recover the already published candidate instead of overwriting it.

The operator UI binds four simultaneous views—immutable original, accepted quality plate, that quality
plate with the exact context-accepted mask, and the immutable candidate—to the same generation,
sequence, state, route, checksum, and grid identity. Acceptance requires all seven controlled checks;
rejection preserves the candidate and exact failed reason. Zero eligible regions use an artifact-free
not-applicable review. Classical fallback is disabled by default and page-scoped: it can be enabled only
after every applicable same-generation AI candidate is explicitly rejected, and every fallback route is
recorded as classical provenance. Accepted/not-applicable review is terminal and immutable.
Legacy inpaint stage-review writes are rejected whenever a page generation is active, including review
reset requests, so they cannot append an operation outside the strict G8 grammar. Provider aliases such
as `lama` are normalized to the registry identity `lama-onnx` before manifest publication and the same
canonical identity is rechecked at execution.

G9 begins only from the exact terminal G8 checksum and clean-plate identity. A whole-page enqueue freezes
the complete page target set, canonical server-observed provider/model, Simplified-Chinese target,
bounded same-page context policy, and parameter hash. The worker verifies that contract before the first
provider call and publishes the full candidate set plus append-only events atomically; restart recovers a
published item without invoking the provider again. Manual, Agent, and dictionary candidates use the
revision path, cannot interleave with an active automatic job, and preserve superseded history.

Each candidate binds source/context/G8 checksums and receives exactly one immutable review containing all
ten QC results and exact defect flags/reason. Acceptance requires every check to pass and both computed
and reviewer flags to be `none`; rejection must name the actual defect. A single terminal page review
accepts only when every eligible non-ruby `translate` or `redraw-art` region has a current accepted
candidate, or records artifact-free `not-applicable` for an empty set. `redraw-art` carries semantic
translation through G9 but is reserved for G10 art lettering; `keep-art`, `ignore`, and ruby are excluded.
Strict replay validates event order, rows, revisions, jobs, provider metadata, checksums, actors, and the
terminal boundary. Only accepted candidates project into legacy translation fields.

G10 derives an exact route for every reviewed non-ruby, non-false-positive region: bubble, ordinary,
art-lettering, keep, or ignore. The route manifest must consume the accepted G9 candidate set exactly.
The job freezes checksummed installed fonts and route-specific styles; ordinary routes reject affine and
visual-centre overrides, while art lettering requires a display-font capability and supports fill/stroke,
combined region-plus-style rotation, non-uniform scale, shear, opacity, visual centre, alignment, and
line spacing. Unsupported curve or AI-lettering requests fail closed instead of falling back to ordinary
text. Publication stores a deterministic PNG under
`generated/lineage-typesets/<generation>/<candidate>.png` with route/style/layout and raster checksums.
Completion is separate but atomic with the job-item terminal state, and no review can precede it. Exact
replay re-renders the candidate and validates its file, rows, revisions, job status, event grammar, and
terminal checksum. One accepted candidate is immutable and is the only G10 authority available to G11.

Public lineage projections omit source/target relative paths and private text. Active-generation jobs
now open preprocessing, detection, whole-page OCR, standalone mask generation, and strict whole-page G8
inpainting, strict whole-page G9 translation, and strict whole-page G10 typesetting. G11 is not a project
job kind: final-review batch creation, issue-only repair handoff, explicit refresh, and terminal export are
opened only through the strict replay, locking, revision-CAS, and atomic-publication contracts described
below. Other unsupported job and mutation kinds remain fail-closed.

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
current generated previews, every G7/G8/G10 raster referenced by retained lineage rows, a sanitized SQLite
backup, and its JSON manifest. Only catalog-validated public font capability tokens survive credential
scrubbing. The project writer lock spans discovery, copy, verification, and SQLite backup so retained files
and rows form one snapshot. Machine-only original, project, export, and exact import-boundary paths are
removed from that backup, and `VACUUM` rebuilds the database so deleted values do not remain in free pages.
Delivery folders such as `translated/` remain separate so users do not need to share the source-bearing
project snapshot.

Final-review format-v2 batches are strict-only and freeze five revision-scoped evidence views: immutable
original, accepted quality plate, mask, clean plate, and accepted final. Explicit not-applicable states are
distinct from unavailable legacy evidence, and versioned routes never fall back to live project files.
Creation acquires every open project writer lock in stable resolved store-root order and holds the full set
through strict lineage replay, evidence freezing, final-review database commit, atomic directory publication,
and the successful 201 response.
Existing format-v1 batches open and list without schema mutation; reviewed legacy items remain locked, but
an issue item may create an isolated immutable-source repair target. That repair G0 is bound to the exact
item revision and private-feedback checksum without publishing the feedback text. Once its strict G10 is
accepted, explicit synchronization freezes a new artifact revision, retains the old revision, appends
history, and resets the item to pending. Legacy v1 final evidence keeps its unavailable grid/resolution and
producer/terminal fields null; conflict reload validates that exact public shape rather than fabricating v2
proof. The repair handoff is accepted only for the same source project and a distinct repair ImageAsset,
with the exact derived G0 run and parameter contract. Idempotent lookup includes the stored parameter ID
and hash, and responses project those persisted values. The lookup collects all G0 candidates for the
`(itemId, itemRevision, feedbackChecksum)` identity before choosing: no match permits creation, exactly one
permits validation and reuse, and multiple matches fail closed. Repair and strict refresh use the same
validator, which replays the complete generation, its unique G0 event and creation Revision, checks the
immutable source and target metadata/path/checksum/decoded resolution/physical separation, and requires a
contiguous event sequence. G0 evidence and the creation Revision have an exact, type-aware JSON grammar;
Python-equal values such as `2.0`/`2` or `true`/`1` do not satisfy it. Exact G0 evidence and the creation
Revision both persist `parameterSetId` and `parameterSetHash`, and strict refresh projects those anchors
into the resulting handoff. Drift between them and the generation fails closed during retry, refresh, and
approved export. Every strict item with a persisted
repair handoff replays this validator during refresh regardless of current verdict, then requires the
handoff's generation and image to equal the validated candidate. Strict refresh acquires the final-review
lock before the source-project writer lock and retains the source lock across terminal-artifact reads,
freezing, and the final item/batch CAS, preventing a successful response from being immediately stale.
Strict review writes and all repair,
synchronization, and export
operations use item/batch CAS and actor provenance. A transition to approved is backend-authorized only
for a human actor; agent and system actors cannot grant final approval. An ambiguous mutation globally
locks those operations
until a non-regressing reload validates the exact batch identity, coherent counts and item set, and active
item. Strict response validation recomputes canonical grid and frozen-evidence digests, excluding only the
public URLs appended after storage. Terminal export requires the whole batch to be approved with zero
pending/issues, then revalidates
every frozen evidence revision under the batch lock immediately before atomically publishing a new
directory. Its success response must bind the resolved requested directory and that directory's fixed
`manifest.json`. Terminal export likewise holds every open project writer lock in stable store-root order
through cross-project upstream replay, copy, repeated currentness checks, and publication, then repeats the
currentness check immediately before publication. Collision-safe renaming is mandatory so the terminal
manifest cannot omit an approved item.

Remote base URLs must be HTTP(S), cannot embed credentials/query/fragment data, and may use plain HTTP
only on loopback. A remote endpoint or model change invalidates translation, typesetting, and export
status/artifacts. Unsafe legacy endpoint fields are removed when a project is reopened.

See [ADR 0001](adr/0001-local-first-modular-workbench.md) for rationale.
