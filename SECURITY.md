# Security policy

## Supported versions

Security fixes are provided for the latest released minor version. Development snapshots may change
without migration guarantees until the first tagged release.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. If it is unavailable, contact the
maintainer privately through the repository owner's GitHub profile. Do not include credentials, private
manga pages, or a complete project database. A generated reproduction is preferred.

Please describe impact, affected version, reproduction, and a proposed mitigation if known. Expect an
acknowledgement within seven days. Do not publish the report until a fix and disclosure plan are agreed.

## Security model

The MVP is a trusted single-user local application. It has no authentication and must bind to loopback.
It is not safe to expose directly to a LAN or the internet. Project and import roots remain security
boundaries even in local mode; traversal, symlink escape, source overwrite, and unsafe artifact access
are rejected.

Remote translation is opt-in and transmits only explicitly trusted current text plus explicitly trusted
bounded preceding/following text by reading order on the same page to the configured service. Pending
and ignored regions are excluded. Use HTTPS for every non-loopback endpoint.
Plain HTTP is appropriate only for a deliberately configured service on trusted loopback:
OpenAI-compatible API keys travel as bearer credentials and are otherwise exposed in transit.
Remote base URLs containing embedded credentials, queries, fragments, control characters, or
non-loopback HTTP are rejected without echoing or persisting the unsafe value.

Credentials are process- or session-scoped and redacted from application logs, project databases, JSON
snapshots, revision history, and frontend responses. A root `.env` file is convenient but remains
user-managed plaintext on disk and must stay Git-ignored. This repository does not accept secrets, model
weights, copyrighted fonts, or user artwork in issues or pull requests.

The working database intentionally retains exact trusted local-import boundaries so export validation can
protect originals even when an image candidate failed or selected paths span drives. Portable export
databases remove those machine paths and run `VACUUM`; the bundled `source/` tree still contains the
user's artwork and must be handled as private content.
