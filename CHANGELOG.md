# Changelog

All notable changes to PipelineGPT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - unreleased

First self-hostable release.

### Added
- Self-hosted deployment bundle (`deploy/`): Docker Compose stack with Caddy (automatic HTTPS), `install.sh`, backup/restore/upgrade scripts.
- Production container image: non-root, CPU-only torch, models bundled for offline operation; one image for the `api`, `worker`, `migrate` and `create-admin` roles.
- Release workflow publishing images to GHCR and the deploy bundle to GitHub Releases.
- MFA enforced at sign-in for engineers and admins; admin MFA reset; brute-force lockout; failed sign-ins audited.
- `ENVIRONMENT=production` safety checks, security headers, `/health/live`, configurable `CORS_ORIGINS`.
- Integration test suite against real Postgres/pgvector and Redis; CI with lint, tests, shellcheck and image build.
- LICENSE (AGPL-3.0), CONTRIBUTING, SECURITY, Code of Conduct.

### Changed
- **Breaking:** answers are generated and risk-classified before any text is sent; flagged answers are withheld until an engineer approves them (they no longer stream token by token).
- **Breaking:** sessions issued by earlier versions are rejected; everyone signs in again once.
- Semantic cache stores only delivered answers, is scoped by language and filters, and is cleared when documents change.
- PII scrubbing removes personal data only; segment IDs, places and dates are kept for retrieval.
- Demo data loader and demo accounts require `DEMO_MODE=true`.

### Fixed
- Ingest worker failed on every document after the first in a worker process (event-loop-bound connections); retries duplicated chunks.
- Failed queries stayed in PROCESSING forever; they are now marked FAILED.
- Over-budget token usage was never recorded.
- Review queue showed a risk level guessed from confidence; it now uses the stored classification.
- Audit export mislabelled engineer decisions and allowed spreadsheet formula injection.
- Docker image included `.env` and `.venv`; Flower was missing from dependencies.

[Unreleased]: https://github.com/Kubu-Ventures/pipe-line_gpt-backend/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Kubu-Ventures/pipe-line_gpt-backend/releases/tag/v0.2.0
