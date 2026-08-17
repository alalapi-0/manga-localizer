# Manga Localizer — combined real-data process/fix loop

Use this file as the live `/loop` prompt. Dynamic schedule. Sentinel:
`AGENT_LOOP_WAKE_manga_realdata`. Do not re-arm `AGENT_LOOP_WAKE_manga_app`.

This is not Goal Mode. Persistence is `/loop` only.

## Objective

Process the combined private books as real material while verifying product
behavior. Each round writes a problem report, then fixes the highest-priority
software defect the report supports. Continue until a full pass over both books
finds no new software defects. Then stop for the user's unified visual check.
Do not treat that human review as done, and do not auto-accept empty pages.

## Datasets (ignored, never commit)

- `tests/real-data/manga01/input/` — previous book, 130 pages
- `tests/real-data/manga02/input/` — new book, 69 pages (68 JPEG, 1 PNG), copied
  from the user's local `manga02` folder on 2026-08-17
- Combined target: 199 pages
- Outputs, workspaces, evaluator reports, exports, and contact sheets stay under
  `tests/real-data/` or `.manga-localizer/`

Never hard-code a Downloads path. Never `git add` `tests/real-data/`.

## Privacy

- Do not open private JPEG/PNG with vision/Read.
- Public docs store only anonymous page IDs and aggregates. No OCR text, no
  image names, no checksums, no absolute paths, no generated artwork.
- Do not send images remotely. Text leaves the machine only if the user already
  selected a remote translator; this loop uses local/mock/Argos only.
- Do not force-push, rewrite history, or claim App Store / notarized release.

## Product constraints

- Processing stays on the Mac. Do not rewrite OCR/inpaint onto iOS.
- Default API remains loopback. LAN companion stays explicit (`npm run app:lan`).
- Do not wait for reversible packaging/UI naming choices. The user verifies in
  the workbench/app panel and will do one unified visual pass at the end.

## Round procedure

1. Read `.agent/STATE.md`, this prompt, `git status`, and the latest private
   round report under `tests/real-data/`.
2. Pick the highest-priority unblocked slice:
   - New `manga02` pages that have not completed import/process/report
   - Then remaining `manga01` pages or previously reported defects
   - Prefer a bounded slice (one chapter, a failing stage, or a representative
     handful) over an unattended 199-page run when debugging
3. Process through the existing Mac pipeline (`evaluate_real_data.py` and/or the
   workbench/app). Record stage failures, UI/API bugs, source-checksum breaks,
   mask leaks, empty-page handling, overflow, and trust-gate mistakes.
4. Write a **private** round report in
   `tests/real-data/reports/round-NN.md` (aggregates + anonymous IDs only).
   Update public `.agent/STATE.md` / `docs/real-data-iteration-status.md` with
   counts and defect classes only.
5. If the report contains a software defect: implement the smallest complete
   public fix with regression coverage. Run the closest gates, then
   `npm run check` / audit as appropriate. Commit **public** files only.
   Do not push unless the current user or STATE explicitly authorizes it.
6. If the report is only visual/quality judgment that needs a human: record it
   as a human gate, do not `--accept` empty pages, do not fake green.
7. After a **full combined pass** with no new software defects, stop the loop
   and leave a NEEDS_USER note for the unified visual check. Do not re-arm.

## Wake / stop

- Primary wake: CI completing after a public-fix push, or a local process job
  finishing. Use `AGENT_LOOP_WAKE_manga_realdata` and `json.loads(..., strict=False)`.
- Do not arm a 25-minute fallback sleeper.
- Late wakes for `AGENT_LOOP_WAKE_manga_app` or Round 35–61 app-packaging CI:
  skip rewrite, do not resume that old loop.
- On user stop: kill watcher PIDs, do not re-arm.

## Done for this loop

A full pass over manga01+manga02 produced no new software defects; private
reports exist; public aggregates are updated; the user still owes the unified
visual check (including remaining manga01 clean-plate review). Until that
human pass, Round 8 is not complete.
