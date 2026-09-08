# Manga retry handoff repair

Task `ALL-PROJECTS-CODEX-GOVERNANCE-V1`; lane REVIEWED; Root owns code, evidence, state and Git. This unit repairs the final-review handoff. It does not accept the whole manga project, complete the corpus Goal, or change any image verdict.

## Defect and affected set

The backend returns `repairAttempt` and `retryFromGenerationId`, and uses `final-review-<item-prefix>-r<revision>-a<N>` for attempts 2+. The frontend required the attempt-1 run ID even when reopening a valid later head. The real metadata copies for issue items `7975117b-c953-4ac4-847e-5249d9521a2c`, `8db3827e-72e3-48b5-b787-98379ffc6ed7`, and `e764b906-1c88-4ac1-94ca-a870c8ed8c43` reproduced rejection (3 failures before the fix). Positions are 134, 167 and 192; these are existing legacy snapshots, item r2/artifact r1, with active attempt-2 repair heads.

The first unmet transition is **authoritative repair response → frontend repair context/navigation**. This is not evidence of a stuck worker. The frontend now verifies the exact attempt suffix, safe integer attempt, parent presence and non-self parent, while preserving item/batch/artifact revisions, source/target identity, sequence, parameter identity and duplicate-operation checks. The entry says “进入修复工作台”; reopening an existing repair does not promise a new G0.

The observed denominator remains 199 = 140 approved / 56 issues / 3 pending (batch r485). Earlier read-only matching found 54/56 issues with a current feedback/revision-matched head; 51 were attempt 1, these 3 were attempt 2. The other 2 had no current matching head. That does not mean all 54 suffered this defect, and does not authorize fresh generation for any item.

## Evidence and limits

- `live-recovery.json` binds the exact metadata, real supported service responses, duplicate reopen results and before/after fingerprints. `verify_final_review_retry_data.py` executes the production `FinalReviewStore.repair` method. It uses read-only SQL connections plus a guard that rejects creation; it never fabricates a response. It validates exact manifests/IDs/counts and applicable evidence before the call. It opens only one project and one batch; it does not migrate, load the full catalog, start a worker or listener, or evaluate all 199 snapshots.
- Both the preserved runtime and the isolated candidate service code returned the exact three expected heads twice each, with unchanged review/image revision/generation/event-count fingerprints. The final receipt binds the isolated candidate service SHA. Precise source/attempt image integrity checks remain in the production service; no image bytes are included in this evidence.
- `verify-final-review-retry-ui.mjs` renders the real final-review component with metadata copies and synthetic images. It substitutes the API and project/image navigation, so it proves the UI handoff and session reload, **not an actual full-workbench session**. Six browser paths (three cases × desktop/mobile) passed with keyboard activation, unchanged verdict, zero console errors, zero unexpected network requests and no horizontal overflow. Root visually inspected representative screenshots. Outputs are reproducible under `.agent/audits/manga-retry-ui/` and are not committed.
- The existing backend tests exercise isolated G0→G10 repair/refresh-to-pending, linear a1→a2 replay, a3 parameter variants and old-history preservation. These are test fixtures, not newly regenerated private manga pages.
- Existing Page91 evidence separately shows G7 accepted → G8 events 208 enqueue / 209 candidate / 210 completed / 211 rejected → 212 enqueue / 213 candidate / 214 completed / 215 rejected. The native provider ingestion records these events sequentially; its claim field is provider provenance, not proof of a separate asynchronous worker claim. `multiple-visual-failures` and its stop on further attempts remain valid. No old failed window was retried here.
- The normal loopback API was not listening. A full TestClient lifespan was interrupted before any completed case because catalog loading expands unrelated entries. It is not counted as a successful recovery. The bounded service route above is the verified route. Original app execution and the underlying owner quality review remain separate.

## Reproduce

First run `npm run storage:check`. The supported external runtime is reused; no model, account, credential, or plugin setting changes are needed. In an isolated worktree, `PYTHONPATH=backend/src` is necessary because the shared editable Python installation otherwise resolves the original checkout.

```sh
npm run check:frontend
PYTHONPATH=backend/src node scripts/external-uv.mjs run --frozen --offline --no-sync python -m pytest backend/tests/test_final_reviews.py -q -k 'issue_repair_isolated_g0_idempotent_and_verdict_unchanged or issue_repair_explicit_retry_is_linear_audited_and_idempotent or issue_repair_parameter_variant_retry_preserves_history_and_exact_replay'
PYTHONPATH=backend/src node scripts/external-uv.mjs run --frozen --offline --no-sync python scripts/verify_final_review_retry_data.py --project-root /Volumes/AI_WORK_SSD/ProjectData/manga-localizer/real-data/manga02/runs/rd-r04-ppocr-lama-json/workspace --review-root /Volumes/AI_WORK_SSD/ProjectData/manga-localizer/real-data/final-review/all-199-pages --receipt docs/reports/all-projects-governance/manga-retry/live-recovery.json
node scripts/verify-final-review-retry-ui.mjs
```

The browser script accepts the existing `MANGA_LOCALIZER_E2E_BROWSER_EXECUTABLE` override when the default bundled browser is unavailable. The data probe refuses drift from its observed receipt; a future change requires fresh scoped observation, not force or resetting counts.

## Remaining and delivery

All original 56 quality blockers and 3 pending owner reviews remain. No fresh image generation or approval is claimed. Whole-project Agent/state-template onboarding is still pending in the parent execution plan. Fresh Judge review and remote-main verification are recorded separately from this semantic candidate. Preserve all unrelated original dirty work; do not merge its contents to deliver this unit.

The original accepted handoff candidate is preserved at Git commit `9320e60`.
The subsequent CI audit unit replaces current-tip machine-specific source references
with repository/registry-relative references; recorded image identities, service
hash, before/after fingerprints and unaccepted quality status are unchanged.
See `../manga-ci-audit/contract.md` for the governed delivery repair and release limit.
