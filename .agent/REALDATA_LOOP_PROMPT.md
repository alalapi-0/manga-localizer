# Manga Localizer — combined real-data process/fix loop (superseded)

**Superseded on 2026-08-17.** Do not use this file as a live `/loop` prompt.
Do not re-arm `AGENT_LOOP_WAKE_manga_realdata`.

The live loop is `.agent/UI_LOOP_PROMPT.md` with sentinel
`AGENT_LOOP_WAKE_manga_ui`.

This loop completed a full combined 199-page process/fix pass and stopped for
the user's unified visual check. Late wakes skip rewrite and do not resume.
