# Contributing to PipelineGPT

Thanks for your interest. PipelineGPT is used for safety-relevant pipeline integrity work, so we value clear, tested, reviewable changes over speed.

> **Code contributions are not accepted yet.** PipelineGPT is dual-licensed (AGPL-3.0 and commercial), which requires the author to hold the rights to all of the code. Until a contributor agreement is in place, please **open an issue** for bugs, questions and feature ideas instead of a pull request. Issues are read and very welcome. The development instructions below are for anyone running or studying the code.

## Ground rules

- Be kind; see the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Security issues go through [private reporting](SECURITY.md)**, never public issues.
- Open an issue before a large change so we can agree on the approach.
- Changes must never let an operator see an answer before it passes engineer review (HITL). If you touch `app/routers/query.py`, `app/services/hitl.py` or the review endpoints, explain why in the PR.

## Development setup

Requirements: Python 3.12, Docker, [uv](https://docs.astral.sh/uv/) (or pip).

```bash
cd backend
uv venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt          # lean: no torch / presidio
docker compose up db redis -d                   # Postgres + pgvector, Redis
cp .env.example .env                            # add ANTHROPIC_API_KEY
alembic -c alembic/alembic.ini upgrade head
uvicorn app.main:app --reload --port 8000
celery -A app.tasks.celery_app.celery_app worker --loglevel=info   # second terminal
```

The frontend lives in [pipe-line_gpt-frontend](https://github.com/Kubu-Ventures/pipe-line_gpt-frontend).

## Tests and checks

```bash
docker compose -f docker-compose.test.yml up -d --wait
export TEST_DATABASE_URL=postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:55432/pipelinegpt_test
export TEST_REDIS_URL=redis://localhost:56379/15
ruff check . && ruff format --check .
pytest --cov                   # CI enforces a 75% coverage floor
```

Integration tests run against real Postgres and Redis; Claude and the embedder are patched, so no API key is needed. Add tests with every behaviour change; bug fixes should include a test that fails without the fix.

Database changes need an Alembic migration in `alembic/versions/`. Hand-write it: autogenerate currently reports unrelated drift and would drop the vector index.

## Commits and pull requests

- Branch from `main`: `feat/…`, `fix/…`, `docs/…`, `chore/…`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages and PR titles, e.g. `fix(review): lock row before deciding`.
- Keep PRs focused; stack them if a feature needs several steps.
- Fill in the PR template: what changed, why, how you tested it, and any breaking or upgrade notes.
- Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- CI (lint, tests, image build) must be green, and a maintainer must approve before merge.

## Releases (maintainers)

1. Move the **Unreleased** changelog entries under the new version.
2. Tag the backend and frontend with the same version: `git tag v0.3.0 && git push origin v0.3.0`.
3. The release workflows publish `ghcr.io/kubu-ventures/pipelinegpt-{backend,frontend}` and a GitHub Release with the deploy bundle.

We follow [Semantic Versioning](https://semver.org/). While on 0.x, minor versions may contain breaking changes; they are always called out in the changelog and release notes.

## License

Copyright (C) 2026 Collins Kubu. PipelineGPT is licensed under the [GNU AGPL-3.0](LICENSE). Commercial licenses (for use outside the AGPL's terms) and support are available from the author.
