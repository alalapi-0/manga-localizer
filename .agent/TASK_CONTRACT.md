# TASK_CONTRACT v1 — manga-localizer-round61-closeout-2026-08-17

Created: 2026-08-17
Lane: GOVERNED
Governor: 10b45384-e406-4dc5-aa9e-4e8be02a016e
Status: COMPLETED
Governor decide: APPROVE (903b0ad8-d65d-4743-850e-1396d4a9d18b)
Judge: PASS (7014b5e4-5e9f-4e6a-a8d1-ffcad5d4e1ac)
Delivered: `5c3047251cbffe9332f04dbaf68b60c4c875e4c1` on `origin/main`
Remote CI: `32002551102` success (backend, frontend, e2e)

This contract freezes only the four dimensions below. Plans, paths, tests, and
cleanup remain adaptive candidate details.

## 1. User objective

Stop every automatic worker/loop for this repository, complete one whole-project
governance closeout, verify Round 61, and publish all task-owned work that is
not yet on `main` by ordinary fast-forward to `origin/main`.

## 2. Protected boundaries

- Do not commit private manga, OCR text, models, credentials, `tests/real-data/`,
  or `.manga-localizer/` content.
- Do not rewrite OCR, start Round 8 human visual review, add a new product
  feature, or claim App Store / notarized distribution.
- Keep the default API on loopback; LAN access stays explicit.
- Do not overwrite unrelated work, rewrite history, or force-push.
- Do not re-arm `AGENT_LOOP_WAKE_manga_app`, the dynamic `/loop`, or a
  25-minute fallback sleeper.
- Do not change content unrelated to this closeout except the loop-stop record
  in `.agent/STATE.md`.

## 3. Authorized external effects

- Safely stop leftover workers that belong to this project.
- Update `.agent/STATE.md` so the Active loop is recorded as stopped and must
  not restart.
- Commit that state closeout if needed.
- After Root verification and a fresh Judge `PASS` on the exact final
  candidate, fast-forward local `main` and ordinary-push to `origin/main`.
- That push may trigger GitHub CI. Do not force-push, deploy, publish, notarize,
  or make other remote changes.

## 4. Acceptance criteria

- No live project loop/worker remains, and no replacement sleeper or auto-restart
  is armed.
- `.agent/STATE.md` no longer presents the old Active loop prompt as a live
  instruction, and it records the Round 61 closeout accurately.
- The final candidate includes `0376b6c` plus only the necessary loop-stop
  state commit; no private, secret, or unrelated files.
- With a project open and an empty library, the inspector **项目** tab still
  shows project settings including the **翻译** combobox; **文本 / 排版 / 修复**
  still show the import CTA.
- Root re-observes the relevant unit tests, frontend lint/typecheck/test/build,
  release audit, and `git diff --check`. Run Playwright e2e if the local
  Chromium revision is available.
- Judge returns `PASS` on the exact final candidate.
- `origin/main` ordinary-fast-forwards from `e7a7327` to the final candidate,
  and the resulting `main` CI eventually succeeds.
- After delivery the worktree is clean, with no stash, extra worktree, or
  untracked files.

## Implementation intent (not frozen)

Must change: `.agent/STATE.md` to mark the Active loop stopped and forbid
re-arming. Verify and deliver `0376b6c`.

Must not change: product code unless verification shows `0376b6c` fails a
frozen acceptance criterion. No Round 8, OCR rewrite, new feature, distribution
channel, or default-network change.
