---
name: manga-final-review-loop
description: Resume or run Manga Localizer's 199-page final-review and corpus-rebuild Goal across checkpoints. Use only after the user explicitly activates that project Goal; do not use for ordinary image edits, general repository work, or prompt drafting.
---

# Manga final review loop

This skill routes an already-authorized project Goal. It never activates the Goal or grants data mutation, network, Git, service, or export authority by itself.

## Maintenance and standing write permission

A request to inspect or update this skill or Agent permissions is maintenance only, not a request to run or resume the Goal. For maintenance, do not enter the workflow below, discover storage, start services, change `STATE.yaml`, or process pages; preserve the existing Goal checkpoint.

The owner's standing global filesystem-write grant covers Codex and Cursor primary agents and subagents across all projects. Within an authorized task's owned scope, do not ask again for ordinary local writes merely because the project changed or images, OCR, databases, review outputs, or other personal data are real, Git-ignored, or outside the repository. Task scope, assigned writer/reviewer roles, data-integrity constraints, guarded storage routing, and actual runtime restrictions still apply. Filesystem permission does not itself authorize uploads or other external effects; reuse an explicit external-effect grant within its authorized scope without asking for the same permission again.

## Enter or resume

1. Read the repository `AGENTS.md` and [current state](../../../.agent/STATE.yaml). Treat `STATE.yaml` as the sole authority and next-action source.
2. Enter execution only when the current request explicitly activates or continues this Goal. Otherwise do not run its workflow or change its recorded state. When execution is authorized, resume from the current checkpoint.
3. On first activation, a protocol-revision mismatch, or context compaction that lost needed protocol, read the headings in the [reference](../../../.agent/FINAL_CORPUS_REBUILD_REFERENCE.md), then only the sections required by the current checkpoint. Record the loaded protocol revision in `STATE.yaml` after authority and live facts are verified.
4. On an ordinary continuation, do not reload the reference, history, or problem report. Read the compact strict-YAML `STATE.yaml` execution_control/current_work and the exact historical checkpoint referenced for the active page; load reference sections again only under the conditions in step 3. Do not ingest the whole state file.

## Advance one bounded turn

- Before any write, prove there is one control-plane writer (Root) and at most one page writer per image/generation, no conflicting process/handle/Git operation, and an exact effect set. Preserve unrelated dirty-worktree changes. Unique writer is per page, not one writer for the whole database.
- In the same tick, Root orchestrates and dispatches up to `pageSlots` page workers in parallel. Each worker writes exactly one assigned page. Root writes `STATE.yaml` and control-plane only. Pending owner-review pages do not occupy slots. If every remaining issue page is skip-listed or blocked, stop at the batched human-review checkpoint; do not hunt the C cluster. `pageSlots` may cover remask/G4 only. G8 `--execute` is hard-serialized to **1**; never stack a second ingest or a prepare-dir against a live execute. Do not relaunch leftover `sample`/profiler attaches on uvicorn.
- On first activation, a protocol revision change, or a new process after context loss, run `npm run storage:check` and resolve the data root once with `npm run -s storage:data -- --print-real-data`. Use that guarded route and the repository wrappers; never fall back to repo-local project data or a hard-coded volume path. Ordinary continuations do not repeat this discovery while the same verified route remains loaded.
- Advance one evidence-bearing checkpoint. Continue independent safe work after a single-page failure; after two equivalent no-progress attempts, record evidence and change the approach.
- Owner bounce and visual reject are gate-local. Reuse accepted G0–G(n-1) checksums, regions, mask, and clean plate. Do not `restartFromSource` unless the feedback names source/preprocess identity or the accepted quality/mask is actually unusable. A G8 fail keeps G0–G7; a G10 fail keeps accepted G8; a missed box returns to G4/G7 only.
- Do not copy quality/mask/raw into `.agent/audits/` except the checksum-bound `cloud:image --prepare-dir` tree. That tree stays gitignored. Do not write `.py` there.
- Never overwrite immutable originals, user verdicts, history, or valid lineage. A repaired item returns to `pending`; only the user can mark it `approved`. Keep project data on the guarded local storage route and out of Git; external upload still requires explicit authority.
- Update `STATE.yaml` only for a material change, keeping current counts, checkpoint, blocker/evidence, loaded protocol revision, and exact next action concise.
- When all currently actionable issue pages are `pending` or reproducibly blocked, stop at one batched human-review checkpoint. Keep the Goal active and do not poll or run in the background.
- On `pause`, `stop`, or `cancel`, stop new dispatch and effects immediately, persist the safe current checkpoint, and respond.

Complete only after live evidence proves `199 approved / 0 issues / 0 pending`, full database/identity/lineage/checksum consistency, and a verified new-directory final export under the storage-governance route. Otherwise resume from `STATE.yaml` or report the precise owner decision that is required.
