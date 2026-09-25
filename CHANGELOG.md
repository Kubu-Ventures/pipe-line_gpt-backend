# Changelog

All notable changes to PipelineGPT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `ops/pilots/provision.sh` sets up a hosted pilot on a fresh VM in one command: installs Docker if needed, deploys the release bundle, runs the installer unattended, schedules daily backups, loads the pilot's documents, and keeps the admin password and a copy of the server's `.env` on your machine. See `ops/pilots/README.md`.
- `install.sh` can run unattended: answers already set as environment variables (domain, AI provider settings, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, …) are not asked for. Run from a terminal, it asks as before.
- `create-admin <email> --password-stdin` reads the password from stdin, for scripts.
- `GET /ingest/documents`: pages through the whole knowledge base (newest first, `limit`/`offset`), searches file names and folder paths (`q`), filters by `status`, and returns totals for every document. `GET /ingest/history` only ever returned the latest 100, so the Documents page could not show or count a large archive. `/ingest/history` is kept for older frontends.
- `bulk-import --skip-summaries` leaves out the per-document Claude summary that feeds the dashboard, saving one AI call per document on large archives. Skipped documents are still fully searchable; the dashboard neither fills in nor refreshes their summaries.
- `bulk-import` command to queue a whole folder of documents (e.g. decades of records) instead of uploading them one at a time. It streams files of any size up to `--max-mb`, skips files already ingested, and can be re-run after an interruption. See "Importing an archive" in `deploy/README.md`.
- Scanned PDFs are now read with OCR (Tesseract, bundled in the image), page by page wherever a page has no text layer. Citations from those pages are labelled "Page N (OCR)" and the ingest audit event records how many pages were OCR'd. `OCR_LANGUAGES` selects the languages (default `eng`; packs for all ten UI languages are bundled), `OCR_ENABLED=false` turns it off.

### Fixed
- `install.sh` wrote secrets through `sed` arguments, which exposed them in the server's process list while it ran and broke on values containing `|`, `&` or `\` (an AWS secret key with `&` was saved corrupted; a `|` aborted the install). It now writes `.env` with shell built-ins.
- A re-analysis whose Claude call failed replaced the document's insights with nothing; the previous insights are now kept.
- A document that took over an hour to process could be picked up by a second worker and processed twice at the same time (Redis redelivers unacknowledged tasks after its visibility timeout). The timeout now exceeds the task time limit.

### Changed
- The dashboard's "Re-analyse" (`POST /dashboard/insights/refresh`) now runs in the background in batches of 25 documents and returns at once (202) with progress; `GET` on the same path reports it. It used to call Claude for every document inside one request, which on a large knowledge base would time out and couldn't be followed. Only one refresh runs at a time.
- Uploaded files now wait for the worker in a shared `uploads` volume instead of inside the Redis queue, so a large backlog no longer fills Redis memory. Uploads queued before upgrading are still processed.
- Ingestion time limit raised from 30 minutes to 2 hours per document, to fit OCR of long scanned reports. A document that exceeds it is marked failed instead of being retried three more times.

## [0.2.0] - 2026-09-25

First self-hostable release.

### Added
- Self-hosted deployment bundle (`deploy/`): Docker Compose stack with Caddy (automatic HTTPS), `install.sh`, backup/restore/upgrade scripts.
- Production container image: non-root, CPU-only torch, models bundled for offline operation; one image for the `api`, `worker`, `migrate` and `create-admin` roles.
- Release workflow publishing images to GHCR and the deploy bundle to GitHub Releases.
- MFA enforced at sign-in for engineers and admins; admin MFA reset; brute-force lockout; failed sign-ins audited.
- `ENVIRONMENT=production` safety checks, security headers, `/health/live`, configurable `CORS_ORIGINS`.
- Integration test suite against real Postgres/pgvector and Redis; CI with lint, tests, shellcheck and image build.
- LICENSE (AGPL-3.0), CONTRIBUTING, SECURITY, Code of Conduct.
- Claude on AWS Bedrock or Google Vertex AI (`LLM_PROVIDER=anthropic|bedrock|vertex`), so questions and document excerpts are processed within the customer's own cloud account. The installer asks which provider to use.
- `llm-check` image role (`docker compose run --rm api llm-check`) that verifies the AI provider answers and prints the reason when it doesn't; the installer runs it.
- `/health` reports the AI provider and model.

### Changed
- **Breaking:** answers are generated and risk-classified before any text is sent; flagged answers are withheld until an engineer approves them (they no longer stream token by token).
- **Breaking:** sessions issued by earlier versions are rejected; everyone signs in again once.
- Semantic cache stores only delivered answers, is scoped by language and filters, and is cleared when documents change.
- PII scrubbing removes personal data only; segment IDs, places and dates are kept for retrieval.
- Demo data loader and demo accounts require `DEMO_MODE=true`.
- The API refuses to start when the chosen provider's settings are missing or the model ID doesn't match the provider (e.g. a Bedrock ID without the `anthropic.` prefix). `ANTHROPIC_API_KEY` is only required with `LLM_PROVIDER=anthropic`.
- Output token caps raised so models that reason before answering (e.g. Claude Sonnet 5, the Sonnet-class model on Bedrock) aren't cut off. Reasoning tokens count toward `MAX_TOKENS_PER_DAY`.

### Fixed
- Ingest worker failed on every document after the first in a worker process (event-loop-bound connections); retries duplicated chunks.
- Failed queries stayed in PROCESSING forever; they are now marked FAILED.
- Over-budget token usage was never recorded.
- Review queue showed a risk level guessed from confidence; it now uses the stored classification.
- Audit export mislabelled engineer decisions and allowed spreadsheet formula injection.
- Docker image included `.env` and `.venv`; Flower was missing from dependencies.
- Document insights and query expansion read only the first content block, which breaks on models that return a thinking block first.
- Document insights created an AI client outside the shared configuration; the worker now builds one per task.
- Missing AWS or Google Cloud credentials now show "The AI service is misconfigured" instead of a generic error.

[Unreleased]: https://github.com/Kubu-Ventures/pipe-line_gpt-backend/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Kubu-Ventures/pipe-line_gpt-backend/releases/tag/v0.2.0
