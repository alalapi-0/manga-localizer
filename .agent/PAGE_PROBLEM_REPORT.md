# Manga Localizer — one-page reprocess problem report

Updated: 2026-08-23

Public operator log for the one-page full-reprocess loop. No private
page images, OCR text, or personal paths.

## Open findings

### P-36 — no strict-AI clean plate candidate is visually acceptable

- Kind: `quality / restoration strategy`
- Recovery checkpoint: sidebar 2, 1166×540
- What happened: after correcting the page to one trusted vertical dialogue
  region and expanding the authoritative support to cover the complete visible
  lettering, five mask/geometry variants were compared. Every direct or derived
  LaMa candidate left visible gray or dark blocks inside the otherwise white
  speech balloon. OpenCV Navier-Stokes and Telea produced a visually clean white
  cavity with zero changes outside the mask, but their honest `classical`
  provenance is correctly rejected by the project's strict AI gate.
- Expected: the accepted clean plate has no readable source lettering or visible
  reconstruction dirt, changes nothing outside the reviewed mask, and satisfies
  an honest persisted provenance policy. A temporary project-setting change,
  relabelling OpenCV as AI, or allowing a later typeset/export to conceal the
  failed clean plate is not acceptable.
- Evidence: the full mask was reviewed on the accepted 4× preprocessing grid;
  the two classical candidates passed visual inspection while no AI-qualified
  candidate did. No inpaint review was accepted and no corrected-page
  translation, typesetting, page review, or export was allowed to proceed.
- Decision boundary: the recommended continuation is a default-off, explicit,
  revocable page approval for an honestly labelled classical fallback after all
  same-generation AI candidates were compared and rejected. Keeping the current
  AI-only meaning instead requires a different allowed AI restoration path that
  produces a visually acceptable result.
- Owner decision (2026-08-23): the owner explicitly authorized the page-scoped
  classical fallback and asked Codex to perform the visual checks and approval
  clicks through the normal user interface instead of requiring the owner to
  operate every page. TASK_CONTRACT v4 freezes honest `classical` provenance,
  AI-first per-generation comparison, default-off page scope, revocation on any
  bound-evidence change, and reuse of one central downstream/export eligibility
  check. Synthetic implementation and review are in progress; real-page writes
  remain stopped until the exact candidate receives fresh Judge and Governor
  approval.

## Cleared findings

### P-38 — short vertical balloon text was pinned to the top of its region

- Kind: `quality / typesetting / replay compatibility`
- What happened: the ordinary vertical renderer always began at the region's top padding. A short
  replacement could therefore match the source glyph size and remain inside the balloon while leaving an
  obviously unbalanced lower cavity. An independent visual review rejected both the smaller candidate and
  the source-sized but top-heavy retry.
- Expected: an explicitly centered short vertical run preserves the source hierarchy and visual center,
  while old frozen candidates continue replaying byte-identically.
- Fix: bubble and ordinary routes now accept an optional bounded `verticalAlign` value (`start`, `center`,
  or `end`). Omitted styles retain the legacy `start` behavior; art-lettering and invalid values fail
  closed. The explicit value is frozen into the candidate style manifest and checksum.
- Verification: the legacy candidate chain replayed after restart; the accepted retry changed no pixels
  outside its frozen text box and passed all eight G10 checks. The centering regression, the end-to-end G10
  persistence test, all 54 imaging tests, all 608 backend tests, Ruff, format checking, and a fresh
  read-only Judge passed.

### P-37 — a committed G4 reorder could omit its lineage event

- Kind: `function / audit durability / transaction ordering`
- What happened: the reorder path retained only a Revision id before the session's final flush. Because
  the id was still unset, the region Revisions committed but the matching append-only G4 event was skipped,
  leaving strict replay unable to advance.
- Expected: every committed reading-order change has one checksum- and Revision-bound event, or the whole
  write rolls back.
- Fix: the writer now keeps the final Revision object, flushes it, and only then constructs the event. A
  narrow append-only recovery recognizes solely the exact historical residue: current sequence/CAS,
  contiguous reorder-only Revision suffix, backward checksum replay, expected image revision, no later
  gate, and the explicitly observed current order must all match.
- Verification: normal event, atomic rollback, recovery, idempotency, post-acceptance, and unrelated-suffix
  rejection tests passed. The real residue recovered one public `recovered=true` event without changing
  project revision or region state; the page-lineage suites, full backend suite, Ruff, and a fresh Judge
  passed.

### P-35 — strict generated-image export omitted the AI provenance gate

- Kind: `security / workflow / export`
- Recovery checkpoint: sidebar 2, discovered during the P-36 strategy audit
- What happened: strict AI provenance was checked when translation, typesetting,
  or render jobs were submitted, but generated-image export validated only page
  review, visual-stage state, and artifact/mask checksums. A previously accepted
  classical clean plate could therefore be exported directly, or through an
  already generated typeset plate, while the project still claimed strict AI
  downstream enforcement.
- Expected: every strict generated-image export reuses the same current accepted
  inpaint provenance gate at submission, immediately before the worker's first
  copy, from direct export-service calls, and again before portable bundle
  publication. JSON-only export and projects with strict mode disabled retain
  their existing behavior.
- Fix: generated export now delegates to the central checksum-, manifest-,
  provider-, and review-digest-bound inpaint gate. Submission performs only the
  strict generated-image policy check, preserving ordinary non-strict queue
  behavior; worker/direct-service readiness and portable finalization recheck the
  same evidence. No classical fallback, provenance relabelling, or weaker export-
  specific predicate was introduced.
- Verification: synthetic regressions cover classical submission rejection with
  no job or destination, queued-then-ineligible failure before the first copy,
  unchanged pre-existing destination bytes, direct-service rejection, actual
  artifact/review/manifest/provenance tampering, portable finalization failure,
  direct AI, validated AI-derived output, zero masks, strict-disabled generated
  export, strict JSON-only export, and typeset/both dependency checks. Root passed
  all 109 job-test instances and the complete 9-launcher / 381-backend /
  197-frontend repository gate. A fresh Judge passed and a fresh Governor
  approved exact combined candidate
  `e65f86cfa9f7fe73c7c3fd131e0a99e31d7557a88eb0a7f82d1dbbb0c97bfa23`;
  its protected restart preserved project revision 4797, sidebar-2 revision 68,
  and zero active jobs.
- Repro: accept an OpenCV clean plate in a non-strict project, enable
  `requireAIInpaintBeforeDownstream`, and request an inpainted-image export. The
  broken path created an export job instead of returning the central
  `ai-inpaint-required` prerequisite conflict.

### P-34 — hydrated full-region autosave invalidated completed page reviews

- Kind: `function / review persistence`
- Recovery checkpoint: sidebar 2, 1166×540
- What happened: on 2026-08-22, the workbench hydrated legacy region records and
  saved complete region snapshots even when the operator changed only one field.
  The snapshots mixed genuinely default-equivalent repair additions with style
  values that differ from the renderer's behavior when style is absent. Those
  unintended style writes were substantive and therefore invalidated typeset
  artifacts and page reviews across previously checked pages. The historical
  `58/130` operator log no longer matched the canonical database, which retained
  only 14 non-pending page reviews.
- Expected: the workbench sends only fields the operator actually changes. The
  backend suppresses actual semantic no-ops, including legacy repair mappings
  that merely acquire ordinary repair defaults, while genuine geometry,
  recognition, repair, or renderer-changing style edits retain their existing
  invalidation behavior.
- Fix: the workbench sends sparse nested patches with explicit
  removal markers, rebases queued edits safely, and canonicalizes only
  default-equivalent repair mappings. Style is compared against stored rendering
  data, so a real style change still invalidates dependent output.
- Same-page verification: a live sparse repair-field removal changed only that
  stored key. The accepted 4× preprocess checksum and completed upstream stages
  remained unchanged, and a full application reload emitted no compensating
  PATCH. The corrected one-region page persisted through the subsequent mask and
  inpaint comparisons. Sidebar 2 is still unfinished because of the independent
  P-36 restoration-policy decision, not because P-34 remains reproducible.
- Checks: focused backend/frontend race, no-op, deletion/undo, and reload
  regressions passed; the complete repository gate passed 9 launcher, 381
  backend, and 197 frontend tests plus formatting, lint, typecheck, production
  build, and `git diff --check`.
- Repro: load a legacy region whose repair/style mapping omits hydrated inspector
  values, edit one unrelated field, and inspect the autosave payload. The broken
  client sends the whole hydrated snapshot instead of only the operator's edit.

### P-33 — the local application service disappeared between page rounds

- Kind: `function / runtime reliability`
- Page: sidebar 56, 1284×559
- What happened: immediately after navigating from the completed previous page,
  the workbench reported that it could not reach the local API and the original
  raster view failed to load. The previously tracked bounded application process
  was no longer present.
- Recovery: restarted the bounded local application session from the current
  verified worktree and reloaded the existing persisted project. No project,
  source, region, review, or generated-artifact data required repair.
- Same-page verification: sidebar 56 reopened at the same persisted position,
  the original raster loaded, and a current-page Real-ESRGAN 4× job completed.
  Original/enhanced comparison succeeded and the improved raster was accepted;
  the local API remained healthy throughout that job and review.
- Repro: finish and reload one page, advance to the next sidebar page, and open
  the original preview after the local application process has exited.

### P-32 — changing only the semantic text type deleted the accepted clean plate

- Kind: `workflow / review safety`
- Page: sidebar 55, 1060×492
- What happened: after the clean plate was accepted, changing the trusted region
  from dialogue to sound effect correctly made page confirmation stale, but also
  withdrew OCR trust and deleted the accepted inpaint artifact, authoritative
  mask, review, provenance, and erased preview.
- Expected: semantic type changes require page reconfirmation and a new typeset,
  while preserving the unchanged accepted clean plate. Geometry, source text,
  trust/ignore disposition, repair settings, mask, and reading order did not
  change.
- Fix: semantic type is no longer treated as an OCR/trust input in either the
  backend invalidation policy or the frontend optimistic trust policy. It remains
  a substantive page-review and typesetting input, so changing it clears
  confirmation and invalidates typeset/export only; direction, geometry, source,
  repair, order, and trust/ignore changes retain their stricter behavior.
- Same-page verification: after rebuilding and accepting the exact AI-derived
  clean plate, the operator changed the region type to dialogue and back to
  sound effect. Both transitions preserved trusted recognition, the accepted
  inpaint review, authoritative mask, erased preview, candidate provenance, and
  an enabled clean-plate view. The final style change likewise left that clean
  plate intact; only typesetting was regenerated. A full application reload kept
  both inpaint and typeset accepted.
- Checks: focused backend invalidation regressions passed 2 cases; the focused
  frontend store regression passed; backend formatting/lint, frontend lint and
  typecheck, and `git diff --check` passed.
- Repro: accept a trusted region's AI clean plate, change only its semantic text
  type, and inspect the inpaint stage, erased artifact, mask, review, and
  provenance before rerunning any job.

### P-31 — the sound-effect translation and first typeset were not publication-ready

- Kind: `quality / translation / typesetting`
- Page: sidebar 55, 1060×492
- What happened: the local translation job replaced the one trusted sound-effect
  region with a shorter fragment that remained in the source writing system,
  then correctly cleared the region's confirmation state. After the operator
  corrected and reconfirmed the translation, the first typeset still treated the
  region as dialogue and auto-fit the one-character sound effect far too small
  for the action cavity.
- Expected: the region contains a concise Simplified Chinese sound effect that
  matches the visible action, is explicitly reconfirmed, and is typeset as a
  legible sound effect with visual weight appropriate to the repaired area.
- Fix: the unusable provider result was replaced locally with a concise operator-
  reviewed translation without disclosing it in the public report. The region
  was classified as a sound effect, changed from auto-fit dialogue styling to a
  fixed 200-pixel layout with a two-pixel outline, and reconfirmed after the
  final style change.
- Same-page verification: the first undersized result was not accepted. The
  second result was inspected against the original at full-page fit and enlarged
  region zoom, had no overflow, restored the intended visual weight without
  covering adjacent artwork, and was accepted. Page review, current-page JSON-
  only export, zero active jobs, and reload persistence all passed after the
  clean plate remained independently accepted.
- Repro: run the configured local translator on the current trusted one-region
  sound-effect page and inspect the saved translation before reconfirming it.

### P-30 — the accepted AI-derived clean plate could not satisfy the strict provenance gate

- Kind: `workflow / AI provenance`
- Page: sidebar 55, 1060×492
- What happened: the accepted clean plate uses a real LaMa overview as its only
  generated source inside the authoritative support, followed by a deterministic
  line-art cleanup. Its manifest currently classifies the selected artifact as
  `deterministic-postprocess`, while the project's strict clean-first gate accepts
  only `direct-ai`. The image is visually clean and accepted, but later automated
  translation or typesetting would correctly fail closed under the current
  provenance contract.
- Expected: an AI-derived artifact is eligible only when immutable internal
  evidence binds its generation, mask, selected checksum, allowed transform, and
  exact direct-AI base candidate checksum/provider. Classical or unbound
  postprocessing must remain ineligible, and display metadata must never unlock
  the gate.
- Why it blocks: turning off the strict gate or relabelling the candidate would
  violate the user's AI-redraw requirement; leaving the contract unchanged makes
  the accepted clean plate unusable for the required later stages.
- Fix: the real direct-LaMa overview is now persisted as an internal-only PNG and
  checksum-bound manifest-v2 record. The public cleanup candidate is classified
  as `ai-derived` and names only one versioned, allowlisted transform over that
  exact base. Server validation binds the base and derived artifact to the same
  generation, authoritative mask, actual provider, selected candidate, manifest
  digest, and accepted-review provenance digest. Internal base records cannot be
  listed or selected through public candidate routes. Candidate selection first
  validates the complete current evidence, so a changed database field, manifest,
  base file, or public status label cannot be used to launder a result.
- Verification: the latest real 4× clean plate was regenerated after the schema
  change, the public candidate list excluded the hidden base, and the intended
  overview-derived candidate was selected in the workbench. Full-resolution
  before/after and actual-mask review found the glyph removed and the motion lines
  continued without a readable remnant; the artifact changes zero pixels outside
  the 818,466-pixel persisted support. The real UI accepted that exact artifact
  and mask, and the live strict prerequisite recomputed and matched its artifact,
  mask, manifest, internal-base, and review-provenance evidence. Translation and
  typesetting remain pending. Focused provenance tests passed 29 cases; the full
  repository check passed 9 launcher, 360 backend, and 181 frontend tests plus
  lint, formatting, typecheck, production build, and `git diff --check`.
- Repro: generate the overview-derived line-art candidate from an allowed LaMa
  base, select and accept it, then evaluate the strict downstream prerequisite.
  It is rejected solely because its origin is deterministic postprocessing.

### P-29 — narrow layouts hid modal controls and overlapped clean-plate review

- Kind: `interaction / responsive layout`
- Page: sidebar 55, 1060×492
- What happened: the mobile top bar retained a fixed 54-pixel flex basis while
  its wrapped children overflowed into the canvas toolbar, placing the shortcuts
  trigger over the `接受` action. The long shortcuts modal also had no viewport
  height limit or scrollable body, so its header and close control could be
  centered outside a short viewport.
- Fix: the mobile top bar now occupies its actual wrapped height. General modals
  are bounded by their padded viewport container, keep header/footer fixed, and
  scroll only the body; their width also follows the safe-area-aware container.
- Verification: in a 319×734 real app viewport the top bar reflowed to 132.5px,
  the canvas toolbar moved below it, the shortcuts and review controls had zero
  overlap, and the review button's center hit the correct element. The shortcuts
  dialog stayed wholly within the viewport, its body scrolled to the last item,
  its close control remained visible, and closing it returned to the accepted
  current-page clean plate with the actual mask still shown.

### P-28 — a large stylized glyph left fragments across every redraw candidate

- Kind: `quality / authoritative mask and AI redraw`
- Page: sidebar 55, 1060×492
- What happened: the initial support followed only the thick strokes, while
  ordinary tiled/component LaMa retained dark fragments and pale bands and the
  classical candidates became blocky.
- Fix: the hard manual support was completed around the entire visible glyph.
  LaMa padding now keeps reflected masked pixels hidden; a global overview pass
  supplies coherent page-edge context before overlapping native-resolution core
  refinement. A conservative overview-only line-art cleanup uses source pixels
  exclusively outside the authoritative support and AI pixels exclusively
  inside it, fails closed outside light monochrome material, and never becomes
  the automatic default.
- Verification: the selected 4× overview-derived candidate changes zero pixels
  outside the persisted mask, removes the readable glyph, gray haze, and block
  artifacts, and continues the motion lines without a visible seam. Full-page,
  enlarged mask-on/mask-off, and persisted-candidate checks passed in the real
  app; that exact clean plate and mask are accepted. Translation and typesetting
  remain pending after P-30 secured its derived-AI provenance.

### P-27 — inpaint could consume an unaccepted or rejected enhancement

- Kind: `workflow / review safety`
- Page: sidebar 54, 1204×1351
- What happened: render source selection checked that preprocessing was `done`
  and readable, but did not require that exact artifact to have a current
  accepted review. An unreviewed, rejected, missing, or changed enhancement
  could therefore become the mask and AI-redraw input.
- Fix: inpaint now uses a preprocessing raster only when its accepted review and
  checksum are current; otherwise it explicitly falls back to the immutable
  original at 1×. If an accepted enhancement was selected, the worker rechecks
  it before publishing. Typeset/render use the accepted clean plate's recorded
  raster lineage instead of reselecting preprocessing, and recheck both that
  lineage and the accepted clean plate before committing. Accepting an
  enhancement after an original-based clean plate was accepted invalidates and
  removes that old clean plate, mask, downstream artifacts, reviews, candidates,
  and provenance so the high-resolution plate must be rebuilt.
- Verification: regressions cover every fallback state, enqueue and worker
  races, the rejected-to-accepted transition with an unchanged enhancement,
  accepted high-resolution lineage, and deletion/tampering failure. The real
  current-page enhancement, clean plate, mask, and final raster still decode at
  the same supported 4× grid and match their accepted checksums. Independent
  read-only reviews found no remaining clean-plate lineage bypass.

### P-26 — valid canonical repair sizes could fail after 4× render scaling

- Kind: `function / high-resolution rendering`
- Page: sidebar 54, 1204×1351
- What happened: render snapshots correctly scaled canonical brush radii, mask
  morphology, provider radius, and context padding, but the imaging validators
  still applied 1× maxima to those runtime values.
- Fix: a validated integer render scale now travels through mask creation,
  OpenCV, LaMa, component/full-context AI repair, and comparison-candidate
  construction. Canonical values are scaled exactly once; runtime validation
  permits the corresponding 2×–4× maxima without clamping, while 1× limits and
  API persistence limits remain unchanged.
- Verification: exact-maximum and maximum-plus-one regressions cover 2×, 3×,
  and 4× mask, brush, OpenCV, and LaMa paths; render snapshots remain immutable
  and prove no double scaling. Independent read-only review passed the complete
  propagation chain. The real current-page 4× artifact and authoritative mask
  grids and accepted checksums remained unchanged.
- Checks: the complete repository check passed with 9 launcher, 343 backend,
  and 181 frontend tests, plus backend lint/format, frontend lint/typecheck, and
  the production build. `git diff --check` passed.

### P-24 — bounded manual support left a fringe and a visible AI fill band

- Kind: `quality / mask calibration`
- Page: sidebar 54, 1204×1351
- What happened: the first real LaMa clean-plate pass removed the main vertical
  glyphs, but a narrow outer annotation/knockout fringe remained and the first
  long support made the continued gray field look like a vertical smoothing
  band. An intermediate per-glyph support still left pinholes and side edges.
- Expected: the authoritative support covers every glyph and knockout remnant
  without touching the panel border or adjacent line art, and the AI result
  continues the local gray field without a readable strip, hole, or seam.
- Fix: the one-point overlay fix made the authoritative mask directly auditable;
  the two manual-only supports were then recentered and widened only over the
  knockout lettering, with the circular intersections overlapped to close every
  pinhole. The 4× LaMa componentwise candidate was regenerated and selected;
  no classical, solid-fill, or deterministic cleanup candidate was accepted.
- Same-page verification: both regions were checked with the actual mask shown,
  then with it hidden in original/result comparison at enlarged zoom, and again
  as an unannotated full-page result. No glyph or knockout remnant remained, the
  panel borders and adjacent line art stayed intact, and the local dark-to-light
  field continued without a visible repair boundary.
- Persistent verification: inpaint is done and accepted, the selected candidate
  is the direct LaMa component candidate, and the accepted clean plate remained
  unchanged through later translation and typesetting. A local high-resolution
  diagnostic also confirmed zero changed pixels outside the authoritative mask;
  private temporary samples were removed after review.

### P-25 — a persisted single-point mask stroke was invisible in the editor

- Kind: `interaction / mask preview`
- Page: sidebar 54, 1204×1351
- What happened: a valid one-point manual mask stroke was persisted and used by
  the backend as a circular repair support, but the pre-generation canvas drew
  every stroke as a polyline, so the one-point stroke had no visible overlay.
- Expected: the editing overlay represents the authoritative backend mask
  semantics; a one-point add or erase stroke appears as a circle with the same
  center, radius, color, and opacity before the page is rebuilt.
- Fix: the canvas now renders a one-point stroke as a Konva circle and retains
  the existing round-cap polyline rendering for multi-point strokes; the test
  environment and component regression cover both branches.
- Same-page verification: rebuilt frontend assets were loaded in the real app;
  the persisted compact add strokes appeared as correctly centered circular
  overlays in both active regions, including the smaller annotation supports.
- Checks: focused canvas tests passed (19 tests), then the complete repository
  check passed (9 launcher, 322 backend, and 181 frontend tests; lint,
  typecheck, formatting, and production build). `git diff --check` also passed.

### P-20 — clean-plate quality is not a hard gate before translation/typesetting

- Kind: `workflow / quality`
- Page: sidebar 53, 771×449
- Clicked: accepted the current AI repair candidate, then proceeded to the
  translated/typeset preview and page-complete actions before obtaining a
  separate user-quality decision on every reconstructed cavity in the clean
  plate
- Expected: the workflow remains in mask-and-AI-redraw review until every
  removed-text cavity is convincingly reconstructed; translation, typesetting,
  page completion, and advancement stay out of scope until that clean plate is
  independently accepted
- What happened: the later-stage artifact was generated and accepted while the
  user still considered the AI reconstruction incomplete.
- Why it blocks: translated text can conceal residual glyphs, broken line art,
  texture discontinuities, or soft AI blocks. A final-page view is therefore not
  evidence that the underlying clean plate is ready.
- Fix: translation, typesetting, and the compatibility render entry point now
  fail closed unless the current clean plate is `done`, independently accepted,
  and still matches both its artifact and authoritative-mask checksums. The
  worker repeats the prerequisite check before committing output, and
  typesetting can no longer rebuild a missing or stale clean plate implicitly.
  A persisted project switch adds the stricter policy required for this real-
  page loop. Every generated candidate now carries internal, non-display
  provenance bound to its generation, candidate file, artifact checksum, mask
  checksum, origin class, and actual providers; acceptance binds a canonical
  digest of that evidence. The complete immutable candidate record set is also
  canonically hashed into an independent database anchor. Candidate selection
  recomputes that anchor before trusting origin/provider metadata, so changing
  only a manifest provenance label fails closed while leaving the accepted
  artifact, mask, revision, and database provenance untouched. Stage acceptance
  and the strict gate also compare the database provenance with the anchored
  manifest's currently selected candidate record, so relabeling database origin
  or provider fields and re-reviewing unchanged pixels cannot legitimize a
  classical result. When the mask is nonempty, only a checksum-current
  `direct-ai` origin from an allowed LaMa provider unlocks downstream work.
  OpenCV, mixed/classical candidates, deterministic manga postprocessing, and
  forged UI status labels remain blocked. A zero mask is the deliberate safe
  no-op exception.
- Same-page verification: enabled the strict AI prerequisite in the real
  workbench and reloaded to confirm persistence, then rebuilt the current 4×
  clean plate under the provenance-aware implementation. The componentwise
  LaMa candidate and same-grid authoritative mask were inspected again at fit
  and enlarged zoom with the mask shown and hidden, then accepted with a bound
  provenance digest. The reconstructed cavities remained clean while the light
  knockout backing, nearby line art, and screentone were preserved; translation
  and typesetting remained pending. Public regressions cover unreviewed,
  rejected, deleted, tampered, non-AI, forged-display-status, deterministic-
  postprocess, provenance-tamper, database-relabel/re-review,
  manifest-metadata-tamper, zero-mask, and worker-race cases. The same live
  prerequisite gate admitted the accepted LaMa result and the page then
  completed translation and typesetting without rebuilding the clean plate.
  Its candidate-manifest anchor and accepted review digest match, and its 4×
  artifact and mask grids agree. A fresh independent Judge passed the exact
  candidate. The final repository check passed: 9 launcher tests, 322 backend
  tests, 180 frontend tests, frontend lint/typecheck, and the production build.
- Repro: generate a typeset artifact immediately after an AI inpaint candidate,
  then compare its apparent completeness with the clean plate while toggling the
  authoritative mask. Do not include private text, page pixels, project names,
  filenames, IDs, or paths.

### P-23 — AI redraw recreates removed glyph shapes after an exact manual mask

- Kind: `quality / AI reconstruction`
- Page: sidebar 53, 771×449
- What happened: the persisted manual support was correct, but the original
  sequential and full-context LaMa results still produced dark or gray glyph-
  shaped blocks inside it.
- Fix: added an explicit `LaMa 逐空缺重绘(局部上下文)` candidate. It processes
  each mask connected component independently and sequentially with local
  context plus an inference-only collar, then composites only through the
  persisted review mask. It is optional, never silently becomes the default,
  and malformed or failed component output cannot fail the page job.
- Same-page verification: rebuilt the accepted 4× plate, selected the new AI
  candidate, confirmed it was bit-exact outside the authoritative mask, and
  inspected the real erased-result canvas at fit and enlarged zoom with the mask
  shown and hidden. The dark glyph remnants were gone while the light knockout
  backing, nearby line art, and screentone remained. The current artifact and
  mask then received a checksum-bound accepted inpaint review; translation and
  typeset remained pending.
- Repro: on a genuine enlarged near-grayscale page, draw exact manual-only
  support over several long outlined glyph groups spanning flat and periodic
  backgrounds, rebuild with local LaMa, and inspect the AI candidates with the
  actual mask shown and hidden. Do not include private text, page pixels,
  project names, filenames, IDs, or paths.

### P-22 — automatic text mask absorbs screentone instead of isolating glyphs

- Kind: `function / mask authority`
- Page: sidebar 53, 771×449
- What happened: automatic both-polarity segmentation treated periodic dots and
  the light inter-dot field as text, creating a dense repair mask.
- Fix: the manual-only strategy has an empty automatic base, ignores automatic
  morphology, derives authority only from persisted strokes, preserves the same
  support for composition/review, and fails closed when no manual add support
  exists.
- Same-page verification: the 4× generated mask was byte-for-byte equal to the
  locally simulated persisted-stroke mask, excluded surrounding retained
  texture, loaded on the same grid as the chosen AI artifact, and was inspected
  in the real workbench at fit and enlarged zoom before acceptance.
- Repro: place outlined text over a regular black-and-white dot field, rebuild
  with a both-polarity automatic text mask, and inspect the generated mask at
  enlarged zoom. Do not include private text, page pixels, project names,
  filenames, IDs, or paths.

### P-21 — 4× AI redraw candidate cannot be loaded in the review canvas

- Kind: `function / review safety`
- Page: sidebar 53, 771×449
- What happened: the selected high-resolution result initially reported an
  image-read failure in the real erased-result pane.
- Fix: generated visual modes and masks accept supported equal integer-scale
  grids while retaining canonical interaction coordinates; inpaint review fails
  closed when artifact and mask grids differ.
- Same-page verification: the real canvas loaded the 3084×1796 selected AI
  artifact and same-grid mask, displayed the candidate label, allowed fit and
  enlarged inspection, and kept acceptance disabled until the reviewer
  explicitly showed the mask.
- Repro: accept a genuine 4× preprocessing artifact, run local LaMa over several
  trusted regions, select a generated candidate, and open the erased-result
  review with the actual mask enabled. Do not include private text, page pixels,
  project names, filenames, IDs, or paths.

### P-19 — AI redraw treats the tight text mask as a glyph contour

- Kind: `quality`
- Page: sidebar 53, 771×449
- Clicked: accepted a genuine local 4× AI super-resolution result, persisted a
  separate bounded add-mask for every visible glyph component, rebuilt all six
  trusted repair regions with LaMa, then compared both the sequential-provider
  and full-context LaMa candidates with the actual mask shown and hidden
- Expected: LaMa receives clean high-resolution context outside the verified
  text support and redraws the erased cavities by continuing the surrounding
  dark field, light knockout plate, line art, and screentone.
- What happened: the render job correctly used the accepted 4× plate and the
  persisted mask followed the intended glyph support, but both LaMa candidates
  produced repeated light glyph-shaped blocks and vertical smears. The tight
  inference boundary still exposes antialiased outline pixels and the glyph
  silhouette as conditioning evidence, so the model reconstructs the removed
  foreground instead of the background.
- Why it blocks: every available AI clean-plate candidate remains visibly
  defective at normal page fit, so the current page cannot be accepted and the
  requested AI cavity-redraw workflow is not satisfied.
- Fix: replace shape-distorting one-shot LaMa resizing with native 512px
  overlapping tiles and cosine blending, keep the wider inference support
  separate from the authoritative review mask, and add a confidence-gated
  **AI manga redraw** candidate that uses the AI result for local dark/light
  structure before snapping it back to the verified monochrome palette. For
  this page the six repair regions use honest full-region review masks so no
  changed pixels are hidden outside the displayed support.
- Same-page verification: rebuilt the accepted 4× plate through the local LaMa
  provider, selected the automatically generated AI manga redraw candidate,
  inspected it at fit and enlarged zoom with the actual mask both visible and
  hidden, and confirmed that all source glyphs disappeared without stretching
  the retained character line art. The clean plate and final typeset were both
  accepted; page review, local export, same-page reload, and zero active jobs
  passed.
- Repro: on an accepted 4× plate, draw bounded persisted masks that closely
  cover multiple outlined glyphs across mixed manga backgrounds, rebuild with
  LaMa, hide the mask overlay, and compare the sequential and full-context AI
  candidates. Do not include private text, page pixels, project names,
  filenames, IDs, or paths.

### P-18 — AI inpainting ignores the accepted super-resolved plate

- Kind: `function`
- Page: sidebar 53, 771×449
- Clicked: requested a real local Real-ESRGAN 4× redraw for the current
  low-resolution page, then prepared to rebuild its trusted repair regions with
  the local LaMa AI provider
- Expected: after the enhanced plate is accepted, text detection, OCR, mask
  construction, AI inpainting, typesetting, preview, and export all use the same
  super-resolved pixels while persisted region geometry remains on the stable
  canonical coordinate grid
- What happened: detection and OCR select the generated preprocessed artifact and
  map coordinates across its scale, but the render pipeline unconditionally
  reopens the original source image. LaMa, repair masks, typesetting, and final
  artifacts therefore remain low resolution even after a genuine 4× AI pass.
- Why it blocks: the erased glyph cavities are being reconstructed from the
  least detailed source instead of the accepted clear plate, so neither the
  clean plate nor the final page meets the requested AI-redraw workflow.
- Fix: render from the accepted preprocessing artifact, scale temporary repair
  geometry, brush/polygon data, pixel-valued repair options, and typesetting
  style into the processed grid without rewriting canonical database
  coordinates. Persist rendered size/scale provenance, accept generated 1×–4×
  grids in the review canvas, require artifact/mask pixel-grid equality, and
  describe canonical versus rendered dimensions in export metadata.
- Same-page verification: the genuine local 4× artifact became the input for
  mask generation, LaMa, candidate creation, typesetting, review, and export;
  the generated clean plate, mask, and final page shared the 4× raster while
  inspector geometry remained on the canonical grid. Both visual stages were
  accepted and remained accepted after the page was reselected.
- Repro: accept a genuine 2×–4× AI preprocessing result, run current-page AI
  inpainting, and compare the provider input and generated mask/image dimensions
  with the enhanced artifact. Do not include private text, page pixels, project
  names, filenames, IDs, or paths.

### P-17 — persisted mask strokes cannot be cleared after reopening a page

- Kind: `interaction`
- Page: sidebar 53, 771×449
- Clicked: reopened the current page after a production backend restart, framed
  one repaired region, and inspected the enlarged clean plate after its earlier
  manual mask experiments had been saved
- Expected: the repair inspector provides an explicit way to clear all persisted
  add/erase strokes for the selected region before rebuilding a precise mask
- What happened: global undo history was empty after reopening, the inspector had
  no clear/reset action, and later add strokes could not supersede an earlier
  erase because erase strokes are always applied last.
- Why it blocks: obsolete wide strokes continue producing visible flat blocks and
  scalloped edges even after the repair algorithm is corrected, while the current
  region cannot be safely redrawn through the visible application.
- Fix: add a selected-region **clear mask strokes** action that writes an empty
  versioned stroke list through the ordinary nested repair patch, while keeping
  manual erase as the final authority for non-cleared edit histories.
- Same-page verification: cleared the persisted experimental strokes through
  the visible repair inspector, switched regions and reopened the page, and
  verified that all six active regions retained zero strokes before the final
  rebuild. The clear action then correctly disabled because there was nothing
  left to remove.
- Repro: save one or more manual mask strokes, reopen the application or page,
  then try to remove all strokes for only that region without splitting or
  recreating its text data. Do not include private text, page pixels, project
  names, filenames, or paths.

### P-16 — every repair method breaks periodic screentone behind text

- Kind: `quality`
- Page: sidebar 53, 771×449
- Clicked: split three outlined text regions at their dark-to-screentone
  background boundary, kept the uniform dark halves on verified solid fills,
  then rebuilt the screentone halves with isolated persisted glyph masks using
  LaMa, OpenCV Telea, OpenCV Navier–Stokes, solid white, line-guided, and
  full-context candidates at enlarged region zoom
- Expected: the complete foreground glyphs disappear while the regular dot
  lattice and the long artwork edges remain visually continuous through each
  small repaired area
- What happened: LaMa retains glyph-shaped dark ghosts; Telea and
  Navier–Stokes create triangular or radial smears; solid white leaves aligned
  capsule-shaped blank areas; the line-guided and full-context candidates retain
  the same defects or add larger structure hallucinations.
- Why it blocks: every available clean-plate result has visible repeated damage
  across the screentone at normal page fit and enlarged zoom, so the repair and
  final page cannot be accepted.
- Fix: add an explicit, non-default screentone repair method with validated
  periodic phase reconstruction and a confidence-gated two-field boundary
  model; reject ambiguous curved/one-sided fields instead of silently replacing
  them with a flat or periodic fill. The final user-requested workflow remains
  local AI redraw rather than deterministic fill.
- Same-page verification: exercised the new explicit method on the isolated
  periodic segments during same-page repair comparison, then selected the
  higher-quality local AI manga redraw for the accepted page. Public hard-mask,
  phase, field-boundary, mask-outside, alpha, and failure-closed regressions
  passed.
- Repro: place several small verified text masks over a regular black-and-white
  screentone crossed by artwork edges, rebuild each available repair method, hide
  the actual-mask overlay, and compare the lattice phase and edge continuity.
  Do not include private text, page pixels, project names, filenames, or paths.

### P-15 — solid-fill color appears saved but reverts after region selection

- Kind: `interaction`
- Page: sidebar 53, 771×449
- Clicked: selected a trusted repair segment, chose **OpenCV → 纯色填充**,
  changed the fill color from white to black, waited until the workbench reported
  **已保存**, selected another segment, then returned to the edited segment
- Expected: the black fill color remains in the region repair settings so the
  next current-page rebuild uses the verified uniform backing color
- What happened: the color control immediately showed black and the global save
  indicator completed, but returning to the same segment restored white.
- Why it blocks: rebuilding with the reverted value creates a conspicuous light
  block instead of removing the light glyphs on the uniform dark backing, so the
  clean plate cannot be accepted.
- Fix: send only the changed partial repair object from the inspector and let
  the store perform its existing nested merge, preventing stale render closures
  from restoring sibling values. Handle the native color input event directly
  so a real UI edit always enters pending persistence.
- Same-page verification: changed the fill color through the visible inspector,
  waited for the dirty/save cycle, switched regions and returned, and verified
  that the persisted value remained. The final accepted page used local AI
  redraw, not a solid-fill substitute.
- Repro: on any trusted repair region, select **OpenCV → 纯色填充**, change the
  fill color, wait for the saved indicator, switch to another region, then return
  and inspect the color control. Do not include private text, page pixels, project
  names, filenames, or paths.

### P-14 — large connected manual text mask leaves glyph-shaped repair remnants

- Kind: `quality`
- Page: sidebar 50, 1064×473
- Clicked: after the explicit light-text safety gate rejected an unsafe automatic
  mask, drew a persisted add-mask over the complete missed sound effect, filled
  the remaining holes, rebuilt the current page, hid the mask-edit overlay, and
  compared all five generated repair candidates at enlarged region zoom
- Expected: the manually verified glyph support disappears while the surrounding
  dark backing, the adjacent light edge, and the lower textured boundary remain
  visually continuous
- What happened: the current-provider and line-guided candidates leave soft
  glyph-shaped light remnants, both OpenCV candidates create large polygonal
  blocks, and the optional full-context candidate leaves broad light patches.
- Why it blocks: every clean-plate candidate remains visibly defective at normal
  page fit and enlarged zoom, so repair and final-page review cannot be accepted.
- Fix: remove the experimental background-prefill candidate after it failed real
  visual review, then avoid the unsafe single connected hole in persisted page
  state. Split the effect into two non-overlapping repair regions, use a verified
  solid fill for the uniform component and local LaMa repair for the textured
  component, and keep each manual add mask inside its own backing area. The
  reading-order primary result remains the canonical page candidate.
- Same-page verification: at enlarged region zoom and normal page fit, both
  repaired components are free of glyph remnants and block artifacts, the tone
  transition is continuous, adjacent line art is unchanged, and the persisted
  actual mask stays inside the intended backing. The primary repair and final
  typeset were accepted, all active regions were reconfirmed without losing the
  accepted artifacts, the page was marked checked, JSON-only export completed,
  and a full reload preserved those states.
- Repro: on a large connected light-on-dark glyph, use a conservative automatic
  text mask plus persisted manual add strokes covering the complete glyph, rebuild,
  hide all mask overlays, and compare every candidate. Do not include private
  text, page pixels, project names, filenames, or paths.

### P-13 — explicit light-text mask absorbs adjacent light artwork

- Kind: `function`
- Page: sidebar 50, 1064×473
- Clicked: created one tight manual region around a large light-on-dark sound
  effect, selected **文本轮廓** with explicit **浅色文字**, rebuilt the current
  page, then framed that region and displayed the persisted actual mask
- Expected: the explicit-polarity mask includes only the light glyph core and
  its narrow configured expansion while preserving the dark backing and nearby
  light artwork
- What happened: the actual mask expands through most of the glyph region and
  includes a connected strip of surrounding artwork. Every generated repair
  candidate therefore leaves a conspicuous flat or block-shaped replacement.
- Why it blocks: the clean plate visibly destroys original art at normal page
  fit and enlarged region zoom, so neither repair nor the final page can be
  accepted.
- Fix: explicit dark/light modes no longer rescue selected-polarity components
  that continue through the analysis guard boundary. Before applying configured
  padding, dilation, and feathering, the text-mask path now measures aggregate
  expansion risk; a dense result is intersected with conservative mixed-background
  evidence and fails closed when it would still approximate the detector region.
  Persisted manual add strokes remain the explicit recovery path.
- Same-page verification: the unsafe automatic mask was rejected instead of
  absorbing adjacent artwork. A bounded manual mask then covered the intended
  glyph support, the rebuilt primary candidate preserved surrounding line art,
  and focused explicit-polarity regressions cover dense texture, sparse fragments
  that merge under expansion, ordinary narrow glyphs, and opposite-polarity
  boundary artwork.
- Repro: on a tight region containing a large light glyph over dark artwork
  beside similarly light line art, use explicit light polarity with a modest
  mask expansion, rebuild, and inspect the persisted actual mask shown and
  hidden. Do not include private text, page pixels, project names, filenames,
  or paths.

### P-12 — confirming unchanged trusted text disables the accepted typeset artifact

- Kind: `interaction`
- Page: sidebar 47, 1282×1708
- Clicked: after accepting the final clean plate and typeset preview, used the
  page-review prompt to confirm each already-trusted active region without
  changing its geometry, source text, translation, or typesetting style
- Expected: confirmation closes the current-content review gate while preserving
  the already-generated and accepted clean plate and typeset artifacts
- What happened: all three confirmation saves returned success, the accepted
  clean plate remained available, but the **成品** preview immediately became
  disabled as though typesetting had never been generated
- Why it blocks: the current page cannot be marked reviewed while its accepted
  final artifact is unavailable, and regenerating it after every confirmation
  would conceal an invalidation bug in the required one-page workflow
- Fix: treat `confirmed` as page-review metadata rather than render eligibility.
  A new human trust decision still invalidates translation, repair, and typeset
  through its `recognition` change, while confirmation-only toggles now invalidate
  export only and preserve current visual artifacts and their accepted reviews.
- Same-page verification: after rebuilding and accepting the byte-identical final
  preview, an already-trusted region was unconfirmed and reconfirmed through the
  production UI. Both saves returned success; **成品** stayed enabled and accepted,
  the accepted clean plate and selected repair candidate were preserved, and the
  focused safety regressions passed.
- Repro: on a page with trusted regions and accepted inpaint and typeset reviews,
  confirm each unchanged region through the page-review prompt, then inspect the
  **成品** preview availability. Do not include private text, page pixels, project
  names, or paths.

### P-11 — preview switching can detach the canvas image and blank the page

- Kind: `interaction`
- Page: sidebar 47, 1282×1708
- Clicked: selected the optional full-context repair candidate, framed a repair
  region at 138%, switched **擦除 → 原图 → 擦除**, enabled region overlays,
  then clicked the canvas with the selection tool.
- Expected: the current preview remains drawable so each repair region can be
  selected and visually reviewed at the same zoom.
- What happened: the canvas became blank and the browser logged
  `InvalidStateError: Failed to execute 'drawImage' ... image source is detached`.
- Why it blocked: the current page cannot complete its required region-by-region
  visual acceptance while the preview disappears during ordinary comparison.
- Fix: keep the last decoded bitmap alive while a replacement is loading, then
  release it only after the replacement commits or the viewport unmounts. This
  prevents a same-URL preview cycle from rendering a previously closed bitmap.
- Same-page verification: the new production bundle completed the same
  **擦除 → 原图 → 擦除** path with the page still visible and zero console
  errors; the focused lifecycle regression, typecheck, lint, and build passed.
- Repro: cycle **擦除 → 原图 → 擦除** on the same generated artifact, then
  select a region. Do not include image bytes, OCR text, private filenames,
  project names, or personal paths in the report.

### P-10 — outlined text across a high-contrast boundary leaves repair blocks

- Kind: `quality`
- Page: sidebar 47, 1282×1708
- Clicked: compared the current-provider, Navier–Stokes, Telea, and
  line-guided clean plates after rebuilding two trusted outlined vertical
  captions; then split the mixed-background caption at its horizontal midpoint,
  reconfirmed both halves, rebuilt, and compared all candidates again with the
  actual mask shown and hidden
- Expected: the complete dark core and light outline disappear while the
  underlying light texture, dark figure edge, and their boundary remain
  visually continuous
- What happened: every candidate leaves a conspicuous solid, jagged, or
  scalloped vertical remnant where the outlined glyphs cross from a light area
  into dark artwork. Splitting by character height reduces the repair area but
  does not reconstruct the mixed-polarity boundary.
- Why it blocks: the defect is visible at normal fit-to-page zoom and becomes
  unmistakable at selected-region zoom, so neither the clean plate nor the
  final page can be accepted.
- Fix: add a conservative mixed-boundary mask refinement and an optional fifth
  LaMa candidate that performs one union-mask pass against the original page with
  wider local context. The existing regional candidates remain available; the
  full-context result is selected only after explicit visual comparison. If that
  optional pass fails, the already-successful four candidates remain usable.
- Same-page verification: the selected full-context candidate was inspected at
  page fit and enlarged region zoom with the actual mask shown and hidden. All
  repaired areas lost the complete outlined glyphs without the previous solid,
  scalloped, or striped remnants; the high-contrast boundary and nearby artwork
  remained visually continuous. The selected candidate persisted through the
  accepted clean plate, exact final-preview rebuild, page review, and current-page
  text-only JSON export with no overflow or active job.
- Repro: on a trusted outlined text region crossing a light/dark artwork
  boundary, use a text-contour mask with zero or narrow dilation, show the
  actual mask, and compare all four clean-plate candidates at fit and enlarged
  zoom. Do not include private text, page pixels, project names, or paths.

### P-9 — text-contour mask absorbs border-connected artwork

- Kind: `function`
- Page: sidebar 45, 516×694
- Clicked: ran current-page LaMa repair after consolidating the visible vertical
  dialogue, then split the same content into eight tight trusted regions and
  rebuilt all four candidates with per-region text-contour masks reduced from
  4/2/2 padding/dilation/feather to 1/0/1
- Expected: the text-contour mask includes only glyph strokes and their narrow
  outline, while dark blade, clothing, and speed-line components touching a
  region boundary remain outside the mask
- What happened: dark artwork connected to the region boundary is classified
  as text. The primary, Navier-Stokes, Telea, and line-guided candidates replace
  large high-contrast structures with blurred or solid patches; the tightest
  masks also leave visible glyph remnants.
- Why it blocks: no current repair candidate preserves the artwork and removes
  the complete text, so the clean plate cannot be accepted and sidebar 45
  cannot proceed to typesetting.
- Fix: segment dark and light text candidates independently through a guard band,
  reject components connected to its real boundary, require local morphology to
  corroborate adaptive thresholds, expose `auto` / `dark` / `light` polarity per
  region, and remove the dense full-region fallback. Explicit polarity never
  writes the opposite-polarity support pixels into the mask.
- Same-page verification: the actual mask was inspected shown and hidden after
  rebuilding the same eight trusted regions. Explicit dark polarity with a
  narrow hard fill removed the complete glyph cores while leaving the outlined
  backing and excluding the connected blade, clothing, and speed-line art. The
  accepted clean plate then completed local translation, reconfirmation, final
  vertical typesetting, page review, and current-page text-only JSON export with
  no current overflow or active job.
- Repro: on a text page where outlined vertical glyphs overlap dark line art,
  use **文本轮廓** masks on tight trusted regions, show the actual mask, then
  compare all four clean-plate candidates with the mask hidden. Do not include
  private text, page pixels, project names, or paths.

### P-8 — text-only repair leaves the terminal long dash

- Kind: `quality`
- Page: sidebar 38, 1106×410
- Clicked: compared the current-provider and line-guided clean plates with the
  review mask hidden at enlarged whole-page zoom
- Expected: the complete short floating dialogue, including its terminal long
  dash, is removed while the nearby face contour and panel art stay intact
- What happened: both inspected candidates removed the main glyphs but left the
  final vertical dash clearly visible
- Why it blocks: the clean plate is visibly incomplete and cannot be accepted or
  used for final typesetting
- Fix: extend the verified region's lower boundary far enough to include the
  complete dash, then re-run OCR, restore the operator text, reconfirm,
  retranslate, and rebuild the same page
- Same-page verification: the rebuilt review mask enclosed the complete long
  dash. All four repair candidates were compared again with the mask hidden;
  the primary LaMa result removed both complete dialogue groups without the
  terminal remnant while preserving the nearby face contour, figure texture,
  panel borders, and drawn effect. The final two-region vertical typeset, page
  review, and current-page JSON export completed with zero overflow or open
  review gates.
- Repro: run text-mask repair on a short vertical floating dialogue whose final
  long dash reaches below the detected region, hide the review mask, and inspect
  the clean plate at enlarged zoom. Do not include private text, page pixels,
  project names, or paths.

### P-7 — text-only repair leaves one terminal ellipsis dot

- Kind: `quality`
- Page: sidebar 35, 1185×384
- Clicked: compared all four current-page repair candidates with the review
  mask hidden at enlarged selected-region zoom
- Expected: the complete short dialogue string, including every terminal
  ellipsis dot, is removed while the small balloon outline stays intact
- What happened: every candidate removed the main glyphs and most punctuation,
  but one final ellipsis dot remained clearly visible inside the balloon
- Why it blocks: the clean plate was visibly incomplete and could not be
  accepted or used for final typesetting
- Fix: extend the verified region's lower boundary beyond the final
  punctuation, re-run OCR, restore the operator text, reconfirm the region,
  rerun local translation, and rebuild the same page
- Same-page verification: the new text mask enclosed every ellipsis dot without
  touching the balloon outline. All four candidates were compared again at
  enlarged selected-region zoom; the remnant was gone from each, and the
  primary LaMa result kept all three balloon borders and nearby art intact.
  After correcting the wide dialogue to vertical flow, the final three-region
  typeset, page review, and current-page JSON export completed with zero
  overflow or open review gates.
- Repro: run text-mask repair on a short vertical dialogue whose ellipsis ends
  close to the lower edge of its verified region, hide the review mask, and
  inspect the clean plate at selected-region zoom. Do not include private text,
  page pixels, project names, or paths.

### P-6 — text-only repair leaves punctuation remnants on the clean plate

- Kind: `quality`
- Page: sidebar 31, 1175×1815
- Clicked: compared the primary current-provider clean plate with the enhanced
  page after current-page LaMa repair
- Expected: both verified dialogue strings, including their punctuation, are
  completely removed while nearby line art remains intact
- What happened: the main glyphs disappeared, but a visible punctuation remnant
  remained at each repaired location at normal workbench zoom
- Why it blocks: the clean plate is visibly incomplete and cannot be accepted or
  used for final typesetting
- Fix: extend both verified regions far enough to include their punctuation,
  then tighten the horizontal bounds back to the actual glyph columns so the
  text mask does not absorb adjacent hair and shadow. Re-run OCR and explicitly
  reconfirm both regions before rebuilding the current page.
- Same-page verification: all four repair candidates were compared at fit and
  enlarged zoom. The primary LaMa result removed both complete strings without
  the original punctuation remnants and preserved the nearby panel art better
  than the OpenCV and line-guided alternatives. The accepted result survived a
  final local-translation refresh, fixed-size typeset, page review, and JSON
  export with zero overflow or open review gates.
- Repro: run text-mask repair on two short vertical exclamations whose punctuation
  sits near the lower edge of the detected regions, hide the review mask, and
  compare the clean plate at fit-to-page zoom. Do not include private text, page
  pixels, project names, or paths.

### P-5 — full-balloon repair mask creates a visible background patch

- Kind: `quality`
- Page: sidebar 28, 1064×628
- Clicked: compared the final clean plate against the original with the review
  mask hidden
- Expected: both balloon interiors remain visually continuous after text removal
- What happened: the large manually drawn left balloon region let LaMa replace
  part of the balloon interior with a white rectangular patch at its lower edge
- Why it blocks: the defect is visible at normal workbench zoom and would remain
  under the translated overlay; the clean plate and final page cannot be accepted
- Fix: keep the manually verified text inside the left region while shortening
  its lower edge before the balloon outline, and route that white-background
  region through a local OpenCV solid text mask. The other balloon remains on
  LaMa, so repair stays region-specific rather than changing project defaults.
- Same-page verification: direct original/clean comparisons showed both text
  groups removed, the left balloon outline preserved, and surrounding artwork
  unchanged. The mixed-provider clean plate and subsequent Pillow typeset were
  accepted with no overflow.
- Repro: create one repair region spanning the full tall balloon, run current-page
  LaMa, hide the review mask, and compare the balloon's lower edge with the
  original. Do not include page pixels, source text, project names, or paths.

### P-4 — reconfirming a translation-only edit invalidates the accepted clean plate

- Kind: `interaction`
- Page: sidebar 28, 1064×628
- Clicked: rejected the first typeset result, edited only translated text,
  reconfirmed that already-trusted region, reran current-page typeset, accepted
  the corrected result, and exported current-page JSON
- Expected: a translation-only correction and its reconfirmation invalidate the
  typeset result, but preserve the unchanged accepted inpaint artifact
- What happened: the reconfirmation marked inpaint pending, so the typeset job
  rebuilt the clean plate and silently cleared its accepted visual review. The
  page and JSON export still showed done, but the persisted inpaint review gate
  was open.
- Why it blocks: sidebar 28 cannot count as passed until the clean plate is
  explicitly accepted after the final typeset run; proceeding would violate the
  one-image gate.
- Fix: reconfirming an already-trusted region after a translation- or style-only
  edit now invalidates typeset/export only; it does not discard an unchanged
  accepted inpaint artifact. Trust-changing confirmations still invalidate the
  clean plate.
- Same-page verification: the new regression passed. In the repaired live app,
  the operator translation correction and reconfirmation preserved the accepted
  inpaint review; full typeset completed without rebuilding the clean plate, and
  all final stage reviews remained accepted through page review and JSON export.
- Repro: on a trusted, confirmed region with accepted inpaint, edit only its
  translated text, reconfirm the region, rerun current-page typeset, and inspect
  the persisted inpaint stage review. Do not include private text, page pixels,
  project names, or paths.

### P-3 — application-mode launcher exits immediately after opening its window

- Kind: `function`
- Page: sidebar 28, 1064×628
- Clicked: started the documented loopback application with `npm run app`
- Expected: the dedicated application window and `127.0.0.1:8000`
  workbench remain available so the current page can be opened and processed
- What happened: the window launcher child exited successfully immediately
  after opening the window, and `scripts/app.mjs` treated that helper exit as
  the application closing, so it terminated the API too
- Why it blocked: the real workbench disappeared before sidebar 28 could be
  opened or visually processed
- Fix: distinguish the bundled window helper, whose lifetime owns the packaged
  application, from one-shot external Chromium / browser launchers. Source-tree
  application mode now keeps the API alive when an external launcher returns,
  while the bundled helper still closes the API with its real window.
- Same-page verification: launcher tests passed; repaired `npm run app` kept
  `127.0.0.1:8000` and the queue running while sidebar 28 completed its full
  quality, text, visual-review, page-review, and JSON-export path.
- Repro: on macOS with a supported browser/application-window launcher,
  start `npm run app` and observe whether the loopback health endpoint remains
  available after the launcher helper returns. Do not include private project
  names, image bytes, OCR text, or personal paths.

### P-2 — tiled detect on a wide 4× plate floods the page with tiny boxes

- Kind: `function`
- Page: sidebar 5, 1110×312
- Clicked: accepted Real-ESRGAN ONNX 4× (enhanced 4440×1248), then
  current-page **文字检测** + **日文 OCR**
- Expected: a small set of balloon / SFX boxes so confirm / ignore
  can finish the text path
- What happened: detect+OCR completed 1/1 with 58 boxes (15 leftover
  ignored). Most new boxes are 3–8 px fragments. **整理本页选框**
  left 55. The inspector asks to confirm dozens of leftovers.
- Why it blocked: confirm / translate / inpaint / typeset could not
  finish while fragment boxes buried the real text.
- Fix: drop boxes smaller than a short-side-scaled minimum on the
  detector plate and after mapping back to the page. Re-detect also
  replaces leftover tiny unconfirmed auto boxes even when OCR filled
  them. Same-page detect+OCR then returned 26 boxes with 0 sub-minimum
  fragments; visual review kept 2 real boxes and ignored the rest.
- Repro: on a wide short photographed page, accept a 4× plate, then
  current-page detect+OCR only. Do not include image bytes, OCR
  text, or personal paths.

### P-1 — enhanced detect+OCR returns zero boxes on a tall page

- Kind: `function`
- Page: sidebar 3, 627×1843
- Clicked: accepted Real-ESRGAN ONNX 4× (enhanced 2508×7372), then
  **批处理与导出** · **当前页** · **文字检测** + **日文 OCR** ·
  **加入队列 · 1 张 · 2 步**
- Expected: PP-OCRv3 boxes when the plate still has detector-sized
  marks, instead of a hard zero
- What happened: the first detect+OCR completed 1/1 with 0 regions
  because PP-OCR stretched the tall plate into 736×736. Letterbox
  alone was still 0; overlapping 736 tiles on the 4× plate produced
  10 boxes.
- Fix: PP-OCR letterbox plus overlapping tiles, then NMS. Same-page
  detect+OCR then returned 10 boxes. Visual review found they sat on
  artwork, not balloons or SFX; all 10 were ignored and
  **确认本页无文字** set `no-text-reviewed`.
- Repro: on a tall narrow photographed page, run current-page AI
  重绘 to a 4× plate, then current-page detect+OCR only. Do not
  include image bytes, OCR text, or personal paths.

## Progress

- Corpus: 130-page full book first, then remaining real books.
- Synthetic catalog leftovers are out of scope.
- Historical page-loop notes: 58 pages visually checked before P-34 was found.
- Canonical recovery baseline: 14/130 first-book pages currently retain a
  non-pending page review; every page must still satisfy the full persisted gate.
- Current recovery page: sidebar 2, the earliest invalidated page.
- Finished: sidebar 1, 1184×701, no-text path after quality pass.
- Finished: sidebar 2, 1166×540, text path after quality pass.
- Finished: sidebar 3, 627×1843, no-text path after quality pass
  (P-1 tiled detect, then ignored artwork false boxes).
- Finished: sidebar 4, 340×594, text path after quality pass.
- Finished: sidebar 5, 1110×312, text path after quality pass
  (P-2 min-size filter, then 2 real boxes).
- Finished: sidebar 6, 1068×811, text path after quality pass
  (11 boxes merged to 1 balloon).
- Finished: sidebar 7, 1084×749, text path after quality pass
  (5 balloons after restoring one ignored empty box).
- Finished: sidebar 8, 1190×666, text path after quality pass
  (4 text clusters from 37 boxes).
- Finished: sidebar 9, 1185×551, no-text path after quality pass
  (0 boxes on the 4× plate).
- Finished: sidebar 10, 1185×1095, text path after quality pass
  (3 text clusters from 9 boxes).
- Finished: sidebar 11, 1188×435, text path after quality pass
  (1 dialogue box from 2 detections).
- Finished: sidebar 12, 1182×751, text path after quality pass
  (2 balloons from 9 boxes).
- Finished: sidebar 13, 1076×515, text path after quality pass
  (1 balloon from 9 boxes).
- Finished: sidebar 14, 1189×777, no-text path after quality pass
  (6 artwork false boxes ignored).
- Finished: sidebar 15, 1058×631, text path after quality pass
  (5 text clusters from 43 boxes).
- Finished: sidebar 16, 332×572, text path after quality pass
  (1 shout balloon from 5 boxes).
- Finished: sidebar 17, 1073×482, text path after quality pass
  (2 balloons from 4 boxes).
- Finished: sidebar 18, 1190×661, text path after quality pass
  (3 floating text clusters from 19 boxes).
- Finished: sidebar 19, 1178×537, no-text path after quality pass
  (3 artwork false boxes ignored).
- Finished: sidebar 20, 1187×1244, text path after quality pass
  (1 shout from 2 boxes; drawn SFX left as artwork).
- Finished: sidebar 21, 958×228, no-text path after quality pass
  (1 artwork false box ignored).
- Finished: sidebar 22, 1284×777, no-text path after quality pass
  (1 drawn-SFX fragment ignored).
- Finished: sidebar 23, 1072×564, no-text path after quality pass
  (1 debris false box ignored).
- Finished: sidebar 24, 1074×358, text path after quality pass
  (3 balloons from 46 boxes; drawn SFX left as artwork).
- Finished: sidebar 25, 1074×793, text path after quality pass
  (2 floating text clusters from 43 boxes).
- Finished: sidebar 26, 1089×334, no-text path after quality pass
  (2 landscape-line false boxes ignored).
- Finished: sidebar 27, 1068×619, text path after quality pass
  (2 balloons from 22 boxes; drawn SFX left as artwork).
- Finished: sidebar 28, 1064×628, text path after quality pass
  (2 balloons from 20 detections; P-3 launcher lifetime, P-4 review
  invalidation, and P-5 clean-plate boundary issues fixed and reverified).
- Finished: sidebar 29, 1174×1161, text path after quality pass
  (1 complete dialogue region; 2 artwork false positives ignored; first
  typeset rejected as too small, then corrected and reverified).
- Finished: sidebar 30, 1189×699, no-text path after quality pass
  (1 shadow-stroke false positive ignored).
- Finished: sidebar 31, 1175×1815, text path after quality pass
  (2 vertical exclamations from 11 detections; 5 artwork false positives
  ignored; P-6 punctuation-mask boundary fixed and reverified).
- Finished: sidebar 32, 441×1827, text path after quality pass
  (3 dialogue regions from 30 detections after cleanup; 13 artwork false
  positives ignored; all repair candidates and the final typeset reverified).
- Finished: sidebar 33, 717×1818, no-text path after quality pass
  (4 detections consolidated to 2; drawn effect lettering and image texture
  explicitly ignored; latest enhancement reaccepted before final export).
- Finished: sidebar 34, 1181×1262, no-text path after quality pass
  (0 detections on the accepted enhanced plate; drawn effects left as art).
- Finished: sidebar 35, 1185×384, text path after quality pass
  (3 vertical dialogue regions from 20 detections after cleanup; 5 false
  positives ignored; P-7 ellipsis-mask boundary fixed and reverified).
- Finished: sidebar 36, 1187×571, text path after quality pass
  (1 complete vertical dialogue region from 21 detections after cleanup; 1
  duplicate and 1 line-art false positive ignored; all repair candidates
  compared; first undersized typeset rejected, then fixed-size vertical
  typeset accepted with zero overflow).
- Finished: sidebar 37, 1178×1267, text path after quality pass
  (1 complete vertical dialogue region from 12 detections after cleanup; 1
  overlapping duplicate ignored; first mask touching the balloon outline was
  rejected, then narrowed and rebuilt on the same page; all repair candidates
  compared and fixed-size three-column typeset accepted with zero overflow).
- Finished: sidebar 38, 1106×410, text path after quality pass
  (2 vertical dialogue regions from 24 detections after cleanup; 10 duplicates,
  drawn effects, or line-art false positives ignored; first unsafe mask rejected;
  P-8 terminal long-dash remnant fixed and reverified before the two-region
  typeset was accepted with zero overflow).
- Finished: sidebar 39, 1185×713, text path after quality pass
  (1 complete three-column floating dialogue from 13 detections after cleanup;
  4 overlapping fragments ignored; first mask touching the cloud line rejected,
  then shortened and rebuilt; all repair candidates compared and fixed-size
  vertical typeset accepted with zero overflow).
- Finished: sidebar 40, 1076×487, no-text path after quality pass
  (1 arm-contour and texture false positive ignored; full-page scan confirmed
  no translatable text).
- Finished: sidebar 41, 1190×765, no-text path after quality pass
  (1 back-shadow and line-art false positive ignored; visible lettering kept as
  drawn effect artwork; full-page scan confirmed no translatable text).
- Finished: sidebar 42, 1109×319, no-text path after quality pass
  (1 large drawn-effect-lettering false positive ignored; punctuation-only
  bubble and effect lettering kept as artwork; full-page scan confirmed no
  translatable text).
- Finished: sidebar 43, 1190×644, no-text path after quality pass
  (1 small drawn-effect-lettering false positive ignored; remaining large marks
  kept as artwork; full-page scan confirmed no translatable text).
- Finished: sidebar 44, 472×1157, no-text path after quality pass
  (Real-ESRGAN result rejected because it removed intended foreground
  screentone; original retained; 2 hair-contour/screentone false positives
  ignored; full-page scan confirmed no translatable text).
- Finished: sidebar 45, 516×694, text path after quality pass
  (P-9 guard-band/polarity text mask fixed and reverified; eight trusted
  fragments translated and typeset; clean plate, final page review, and
  current-page text-only JSON export completed).
- Finished: sidebar 46, 646×447, no-text path after quality pass
  (Real-ESRGAN ONNX 4× accepted; one empty drawn-effect fragment ignored;
  full-page scan confirmed no translatable text).
- Finished: sidebar 47, 1282×1708, text path after quality pass
  (four detected regions reduced to three trusted text regions and one ignored
  false positive; P-10 mixed-boundary repair, P-11 preview lifecycle, and P-12
  confirmation invalidation fixed and reverified; clean plate, final typeset,
  page review, and current-page text-only JSON export completed).
- Finished: sidebar 48, 1175×1189, no-text path after quality pass
  (Real-ESRGAN result rejected for broken contours and oversharpening; original
  retained; 5 artwork-only false positives ignored; full-page scan confirmed no
  translatable text, and no downstream text job or private export was run).
- Finished: sidebar 49, 1166×566, text path after quality pass
  (Real-ESRGAN result rejected; 5 detections reduced to 2 merged real-text
  fragments and 3 ignored artwork false positives; the Telea clean plate and
  fixed-size vertical typeset were accepted, followed by page review and
  current-page text-only JSON export).
- Finished: sidebar 50, 1064×473, text path after quality pass
  (5 regions reduced to 4 trusted text regions and 1 ignored false positive;
  P-13 explicit-polarity mask expansion and P-14 large connected-mask repair
  were fixed and reverified; clean plate, final typeset, page review, and
  current-page text-only JSON export completed).
- Finished: sidebar 51, 1060×721, no-text path after quality pass
  (Real-ESRGAN result rejected because it damaged screentone, gray transitions,
  and line continuity; original retained; 4 artwork-only false positives were
  enlarged and ignored; reload preserved the rejected review, ignore decisions,
  and no-text page confirmation; no downstream text job or private export ran).
- Finished: sidebar 52, 706×491, text path after quality pass
  (10 fragments reduced to 2 merged trusted vertical regions and 6 ignored
  duplicates; Real-ESRGAN, bounded manual-mask LaMa full-context repair, and
  zero-overflow vertical typeset were accepted; page review, current-page
  text-only JSON export, reload persistence, and zero active jobs verified).
- Finished: sidebar 53, 771×449, text path after quality pass
  (4× Real-ESRGAN, exact manual-only support, and componentwise LaMa redraw were
  accepted before downstream work; 6 trusted text boxes were translated and
  reconfirmed; the second single-column typeset pass, page review, text-only JSON
  export, strict provenance gate, and zero active jobs were verified).
- Finished: sidebar 54, 1204×1351, text path after quality pass
  (4× Real-ESRGAN, calibrated manual-only support, and the direct LaMa component
  redraw were accepted before downstream work; 2 trusted text boxes were
  manually corrected and reconfirmed after incomplete local translation; the
  zero-overflow vertical typeset, page review, current-page text-only JSON export,
  and zero active jobs were verified).
- Finished: sidebar 55, 1060×492, text path after quality pass
  (4× Real-ESRGAN, a hard manual-only support, and the checksum-bound AI-derived
  overview redraw were accepted before downstream work; one trusted sound-effect
  region was locally corrected and reconfirmed after an unusable translator
  result; the enlarged fixed-size typeset, page review, current-page text-only
  JSON export, reload persistence, and zero active jobs were verified after
  P-28 through P-32 were cleared).
- Finished: sidebar 56, 1284×559, no-text path after quality pass
  (Real-ESRGAN anime 4× was accepted after full-width and enlarged comparison;
  fresh detection/OCR reduced stale empty proposals to one enlarged clothing/
  shadow false positive, which was explicitly ignored; full-page scan, no-text
  confirmation, reload persistence, zero active jobs, and P-33 recovery were
  verified without any translation, repair, typesetting, or image export).
- Finished: sidebar 57, 611×704, no-text path after quality pass
  (Real-ESRGAN anime 4× was accepted after original/enhanced comparison restored
  the low-resolution line work; two overlapping hand/motion-shading false
  positives were inspected enlarged and ignored; integrated impact lettering
  was retained as artwork, while full-page scan, no-text confirmation, reload
  persistence, and cleared batch selection passed without downstream jobs).
- Finished: sidebar 58, 1177×1133, no-text path after quality pass
  (Real-ESRGAN anime 4× was accepted after original/enhanced comparison sharpened
  the character, arrows, snow contours, and screentone without structural loss;
  fresh detection/OCR replaced twenty-five stale empty proposals with one
  high-zoom false positive on a continuous snowbank contour and its parallel
  motion/shading marks, which was explicitly ignored; the large cropped edge
  marks were retained as artwork, while full-page scan, no-text confirmation,
  reload persistence, zero active jobs, and cleared batch selection passed
  without translation, repair, typesetting, or private image export).
- Next page under the historical sequence was sidebar 59. P-34 supersedes that
  pointer: recovery resumes at sidebar 2 and may advance only after its persisted
  gate passes.
- Earlier realpages-loop “已检查” pages are not a skip; this loop
  reprocesses from the first page.
