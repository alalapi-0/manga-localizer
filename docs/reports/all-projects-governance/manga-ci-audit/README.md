# Manga CI delivery repair

Reader: the next Root, independent reviewer and Hub governance reader. Update on
candidate, validation or delivery changes. Contract: `contract.md`; preserved
preimages and baseline failures: `baseline.json`. This unit belongs to the existing
all-projects Goal and does not resume the paused image workflow.

The prior remote run reached 910 passing backend tests and two failures. One
large-drift fixture reached different valid geometric rejection guards on macOS
and Linux. The other exposed a real capability error: a generic fallback could
return a display-class font even when the explicit display capability collection
was empty. The minimal production repair requires the explicit collection. Its
API regression verifies 409 and unchanged job, revision, event and candidate
counts. Reinstating the old assignment inside an isolated test process reproduces
the erroneous 202 response. Production geometry rules and thresholds are unchanged.

Normal CI now verifies explicit full base/head identities and the effective
candidate. Personal paths are checked in base-to-working-tree net additions,
HEAD-to-index and index-to-working-tree additions, and all untracked text. An
uncommitted forward removal can repair current-tip evidence; a newly staged bad
path cannot hide behind a clean unstaged copy. Missing/incomplete comparisons
fail closed. Git replacement objects cannot substitute for original historical
content; grafted history is rejected.

Both modes retain candidate/index/reachable-history secret and artifact checks.
Secret-pattern coverage remains the existing text-extension and size policy;
binary artifact checks remain independent. Approved public raster pins remain
exact path/size/hash identities. No path or finding allowlist was introduced.
Committed objects are inspected through Git; changed live Cursor resources stop
the audit on metadata, and unchanged committed counterparts are read as inert
Git objects. No Cursor filesystem content, process, API or managed resource is
accessed by the audit.

Default and explicit release modes remain equivalent and strict. The observed
repository has protected historical personal-path findings and remains
`release_not_ready`. A normal-CI pass is not approval for a product release.
The repaired current-tip handoff baseline and service receipt use typed
repository/registry-relative references; their image identities and quality
evidence remain unchanged. Original evidence is retained in published history.

Validation uses temporary Git repositories and generated application fixtures.
One initial non-ancestor fixture allowed identical same-second root commits;
its distinct tree now makes the intended unrelated-history condition deterministic.
That interrupted test run is not counted as passing. Final outcomes and exact
candidate identity are registered alongside this file. Remote main ancestry and
the delivered candidate's complete CI are required before delivery is closed.

Unresolved image work remains as recorded in `../manga-retry/live-recovery.json`:
the three real attempt-2 handoffs are reusable, while image quality and pending
owner review remain unaccepted. No real image, database, generation or review
status was changed in this CI unit.
