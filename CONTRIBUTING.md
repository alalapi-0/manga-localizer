# Contributing

Thank you for improving Manga Localizer. Contributions should preserve its local-first behavior,
source immutability, replaceable providers, and honest capability reporting.

## Before opening an issue

- Search existing issues and the [roadmap](ROADMAP.md).
- Use generated fixtures or images you created and may redistribute. Never upload commercial manga.
- Remove text, filenames, paths, logs, or manifests that disclose private project information.
- For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development workflow

1. Fork and create a focused branch.
2. Install Node.js 22.22.2 or newer and Python 3.12, then run `npm install` and `npm run setup`.
3. Add tests for behavior changes. Remote-provider tests must use a local fake server or mock transport;
   do not send repository fixtures or contributor data to a third party.
4. Before browser tests, run `npm run setup:test`. Then run `npm run check`, relevant Playwright tests,
   and `npm run audit:release`.
5. Update documentation and `CHANGELOG.md` when user-facing behavior changes.
6. Open a pull request describing behavior, verification, privacy impact, and screenshots when relevant.

Do not commit credentials, `.env`, databases, model weights, fonts, user images, outputs, caches, or
machine-specific absolute paths. Avoid adding large optional ML dependencies to the default install;
providers that need them should use an optional extra and report unavailable health cleanly.
The release audit is a heuristic safety net, not a substitute for reviewing staged files and media for
credentials, personal paths, copyrighted material, local databases, and generated output. Dependency or
license changes must also update [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) when applicable.

When adding a remote provider, require HTTPS for non-loopback endpoints. Plain HTTP is acceptable only
for a deliberately configured service on trusted loopback because API keys are sent as bearer credentials.

## Code expectations

- Python is formatted/linted with Ruff and covered by pytest.
- TypeScript must pass ESLint, TypeScript, Vitest, and the production Vite build.
- Filesystem operations require traversal and source-overwrite tests.
- UI behavior must remain keyboard accessible, dense, and usable at 1280×720.
- A baseline algorithm should be described as baseline; do not market mock or heuristic output as AI
  quality that the implementation does not provide.

By submitting a contribution, you agree that it is licensed under Apache-2.0.
