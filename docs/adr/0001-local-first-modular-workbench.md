# ADR 0001: Local-first modular workbench

- Status: accepted; pipeline ordering amended 2026-08-25; strict G8-G11 amended 2026-08-26
- Date: 2026-08-06

## Context

Manga localization is a pipeline, not a single OCR or chat request. The product must remain usable
offline, keep source files immutable, survive interruption, preserve nested Unicode paths, and allow
individual ML or service components to be replaced without rewriting product logic.

## Decision

Use a browser-based React editor backed by a local FastAPI process. Keep the processing stages and
their human acceptance boundaries explicit:

```mermaid
flowchart LR
  Import --> Preprocess --> Detection --> Regions[Region disposition and background] --> OCR --> Trust[Source-text trust]
  Trust --> MaskGeneration[Actual-mask generation] --> MaskReview[Coverage and collateral review]
  MaskReview --> Inpainting --> CleanReview[Clean-plate review]
  CleanReview --> Translation --> TranslationReview[Translation confirmation]
  TranslationReview --> Typesetting --> TypesetReview[Typeset review]
  TypesetReview --> FinalReview[Final review] --> Export
```

Provider protocols isolate OCR, translation, and inpainting. A separate typesetting engine owns
layout and high-resolution rendering. FastAPI routes call application services rather than concrete
providers. SQLite is the source of truth; a sanitized JSON snapshot supports inspection and portability
checks, but opening a project still requires the adjacent SQLite database.

The first working OCR provider uses the locally installed Tesseract executable with Japanese
horizontal and vertical language packs. This is lightweight and cross-platform enough for a safe
default. Higher-quality MangaOCR and PaddleOCR adapters are planned rather than included because their
downloads and runtime footprint must never prevent application startup.

OpenCV Telea inpainting is the baseline eraser. Pillow renders horizontal or vertical Chinese text,
with system-font discovery and user font configuration. Both are intentionally replaceable.

An in-process persistent queue stores jobs and item progress in SQLite. One job runner avoids requiring
Redis or a daemon for the MVP, resumes interrupted work on startup, and keeps processing off request
handlers. Detection, OCR, translation, and rendering can process one to eight items concurrently under
the user's limit; export is serialized for conflict safety. Pause/cancel controls are cooperative
between item batches rather than hard interrupts.
A future desktop shell can supervise the same API without changing project data.

## Consequences

- The MVP runs fully locally after installing system Tesseract and project dependencies.
- Browser folder imports use `webkitRelativePath`, strip the selected root folder, and retain its nested
  tree; server-path import is also available for trusted local use. Native folder dialogs are deferred
  to a Tauri/Electron shell.
- OpenCV inpainting and Tesseract detection are practical baselines, not promises of production-grade
  artistic reconstruction or perfect bubble segmentation.
- Persisted text regions are rotatable rectangles; JSON-only project import and arbitrary polygon text
  regions are deferred. Strict G7 mask drafts separately persist bounded polygon recipes plus canonical
  add/erase brush strokes for actual-mask review.
- Strict generation identity is replayed before artifact production/replay, reads, and export for native
  non-repair generations as well as repair generations. One unique creation G0, contiguous event sequence,
  actor and source/target checksums, exact creation Revision, parameter set, run ID, and source references
  must agree; a generation row alone is not authority. SQLite `revisions_g0_no_update`/
  `revisions_g0_no_delete` triggers make G0-linked creation Revisions append-only, including the existing
  five-key generic G0 shape. Canonical `page_lineage_events_no_update`/
  `page_lineage_events_no_delete` triggers protect the event rows, preventing coordinated event/generation/
  Revision/actor/target drift. The validator still replays exact content and verifies all four canonical
  `sqlite_master` definitions; any missing, altered, or same-name weakened guard fails closed. Generic and
  final-review repair G0 creators check the same definitions before any file/database write, returning a
  zero-write 4xx rather than publishing 201 before later read-time discovery.
- Strict G8 clean plates are separate from the legacy mutable inpaint preview: background-routed
  candidates and reviews are append-only, files are generation/candidate scoped, outside-mask RGBA
  equality is recomputed from the accepted quality/mask bytes, and classical fallback requires an
  explicit page-scoped lineage decision after applicable AI rejection.
- Strict G9 translations are append-only candidates and reviews anchored to terminal G8. Whole-page
  automatic jobs freeze and revalidate canonical provider/model/context evidence before publication;
  manual, Agent, and dictionary edits are revision-only. Ten explicit semantic QC checks and exact defect
  reasons govern candidate review, and one immutable page terminal checksum is the only strict authority
  for G10. Non-ruby `redraw-art` text follows the same G6 trusted-source and G9 semantic-review contract as
  `translate`; `keep-art`, `ignore`, and ruby are excluded. Accepted candidates alone project into legacy
  translation fields.
- Strict G10 typesetting is an append-only whole-page candidate/review gate anchored to terminal G9 and
  the accepted clean plate. It freezes exact region, route, style, installed-font, layout, renderer, and
  raster evidence. Bubble/ordinary and art-lettering are distinct routes; art lettering requires an
  explicit display-font/affine capability, while keep/ignore routes preserve pixels. Publication and job
  completion are separate ordered events, review waits for both, and acceptance requires eight visual
  checks with no server-observed overflow or anomalies. Legacy typeset/render paths cannot authorize an
  active page generation. Strict G11 consumes that immutable accepted candidate into revision-scoped
  five-view final-review evidence; versioned reads, explicit repair/refresh, append-only history, and
  item/batch CAS keep frozen review state separate from live project state. Ambiguous mutations globally
  lock review operations until an exact, coherent, non-regressing batch reload is validated, including
  recomputation of canonical grid and stored-evidence digests from the response payload. Repair responses
  must bind the source project to a distinct repair image, and idempotent reuse must match and report the
  persisted generation parameter set. Candidate discovery collects all G0 matches for the item/revision/
  feedback-checksum identity; only one may be reused and ambiguity fails closed. Repair and strict refresh
  share a validator for the complete generation, unique G0, creation Revision, immutable source, target
  metadata/path/checksum/decoded resolution/physical separation, and contiguous event sequence. G0 and
  Revision JSON are field- and type-exact, so Python-equal float/integer or boolean/integer values fail. Both
  persist the exact `parameterSetId`/`parameterSetHash`, which refresh carries into its handoff; drift from
  the generation causes retry, refresh, and approved export to fail closed.
  Every persisted strict repair handoff is revalidated in every verdict state and must name that validated
  generation/image. Refresh locks final review before the source project and holds the source lock through
  artifact read, freeze, and final CAS, preventing an immediately stale success. Export
  responses must bind the resolved requested directory to its
  fixed manifest. Export is a
  terminal operation: only a human actor may transition an item to approved, and every item must be
  approved with pending/issues both zero before the backend
  revalidates the frozen evidence and atomically publishes a new directory. It uses collision-safe renaming
  and may not skip an approved item. Batch creation locks every open project store in stable store-root order
  across strict replay, freeze, database commit, atomic publication, and the 201 response. Terminal export
  takes the same all-open-store lock set through cross-project upstream replay, copy, repeated currentness
  validation, and publication, closing the source-acceptance race.
- Provider settings and catalog-validated public font capability tokens are portable, while credentials
  remain session/environment scoped. Portable snapshots retain every database-referenced G7/G8/G10 raster
  under one writer lock so the copied files and sanitized SQLite rows cannot describe different moments.
- SQLite writes, generated files, and export paths require transaction and traversal checks.

## Rejected alternatives

- A single model endpoint: conflates detection, OCR, translation, and image editing.
- Electron/Tauri first: adds packaging work before the useful editing loop exists.
- Redis/Celery: increases installation burden for a single-user local application.
- Shipping model weights or fonts: creates repository size, licensing, and privacy risks.
