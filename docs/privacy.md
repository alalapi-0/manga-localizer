# Privacy

Manga Localizer is local-first. Imported image bytes, masks, previews, OCR databases, and exports remain
on the machine unless the user moves them with another tool. The application does not include telemetry.

Remote translation is the only MVP feature designed to make an outbound content request. It is disabled
until the user selects a remote provider and supplies configuration. The optional `argos-ja-zh` provider
translates trusted text locally through an English pivot and does not open a network connection at
translate time. Remote requests contain only an explicitly
trusted current text, explicitly trusted bounded preceding/following text regions by reading order on the
same page, optional character names, and relevant glossary entries. They do not contain pending/ignored
regions, adjacent pages, the entire book, project paths, or image bytes.
Review the remote provider's retention policy before enabling it.

Use HTTPS for non-loopback translation endpoints. Plain HTTP is suitable only for a trusted service
bound to loopback; otherwise the text and Bearer API credential can be observed in transit. The
configured Base URL and model are portable project settings, so do not place credentials in either
field. Validation rejects embedded URL credentials, query strings, fragments, and non-loopback HTTP
without persisting or echoing the unsafe value. Reopening sanitizes invalid legacy endpoint fields.
Changing the endpoint or model invalidates translation, typesetting, and export output until those
stages are rerun.

The optional same-LAN companion (`npm run app:lan`) serves the unauthenticated workbench over plain
HTTP on one private IPv4. Use it only on a trusted Wi-Fi. Default startup remains loopback.

API keys come from the process environment or volatile session configuration. The development launcher
can populate the process environment from the user's Git-ignored `.env`, which is a local plaintext file
managed by the user. The application does not write keys to SQLite, project JSON, revision history,
frontend persistence, or normal logs. Health/error output is redacted.

Public bug reports must use generated fixtures or artwork the reporter has permission to redistribute.
Remove project manifests and logs if their filenames or translated text are sensitive.

Project manifests, SQLite databases, and text JSON may contain OCR/translation text, provider IDs,
confidence values, OCR-attempt provenance, and human trust decisions even when they contain no image
bytes. Treat all of them as private. Public job responses omit internal options and filesystem paths;
their stage output is an operational/aggregate projection without OCR text, coordinates, or region
identifiers, and public errors use fixed stage messages. Operational envelope IDs remain available for
queue control and page labels. That does not make the surrounding project or an exported portable bundle
public-safe.

The working SQLite database records cumulative exact local-import file/directory boundaries, including
selections whose candidate files fail image validation, to guarantee that later exports cannot overwrite
them. It may therefore contain machine-specific source paths even though imported bytes are copied into
the project. On multi-drive imports, the public `inputRoot` summary can be empty while those exact rows
remain authoritative.

Custom export directories are reopenable project snapshots and therefore contain project-owned copies
of every imported source image, not just translated output. Treat the whole directory as private manga
content. If you only intend to deliver finished pages, share files from `translated/` rather than the
project root, `source/`, `generated/`, or `project/`.

Portable bundle databases remove the working root, input root, exact import boundaries, per-image input
paths, and job `outputPath` options. They then run SQLite `VACUUM` before atomic placement so removed local
paths or secret-bearing historical text do not remain in free pages. This sanitization does not make
bundled source artwork public-safe.
