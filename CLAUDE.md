# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

PipelineGPT backend: a FastAPI RAG service for pipeline-integrity data (ILI reports, SCADA exports, PHMSA incidents, IMP schedules). Answers are grounded in ingested documents with `[SRC-NNN]` citations. Answers that recommend operational actions are held for engineer review (HITL) before they reach the operator.

All Python code lives in `backend/`. Run every command below from `backend/`, because `config.py` reads `.env` relative to the current working directory.

The companion Next.js 14 frontend is a separate repo, `Kubu-Ventures/pipe-line_gpt-frontend` (NextAuth v5, next-intl with 10 locales, TanStack Query). It calls this API through `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`) and consumes the `/query` SSE stream directly. Any change to response schemas or SSE event shape must be mirrored there.

## Commands

```bash
cd backend
source .venv/bin/activate                 # uv-managed venv at backend/.venv
uv pip install -r requirements-dev.txt    # lean (what CI uses); `-e ".[dev]"` adds torch/presidio

docker compose up db redis -d             # infra only (pgvector/pg16 + redis 7)
docker compose up --build                 # full dev stack from source (API on :8000)
alembic -c alembic/alembic.ini upgrade head
alembic -c alembic/alembic.ini revision --autogenerate -m "msg"   # after editing app/models/db.py

uvicorn app.main:app --reload --port 8000
celery -A app.tasks.celery_app.celery_app worker --loglevel=info --concurrency=4

python create_admin.py                    # bootstrap first ADMIN
python seed_demo.py                       # demo users/data

ruff check . && ruff format --check .     # lint (CI-enforced)

pytest tests/unit                         # no services
docker compose -f docker-compose.test.yml up -d --wait     # tmpfs pg on :55432, redis on :56379
export TEST_DATABASE_URL=postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:55432/pipelinegpt_test
export TEST_REDIS_URL=redis://localhost:56379/15
pytest --cov                              # full suite; coverage floor 75%
pytest tests/integration/test_query_flow.py::test_semantic_cache_hit -q   # single test
```

`make help` wraps these, but `make` isn't installed on the dev WSL box by default.

### Test harness

- `tests/conftest.py` sets env vars **before** `app` is imported (DATABASE_URL/REDIS_URL from the TEST_ vars, a fake ANTHROPIC_API_KEY). Integration tests are auto-skipped without `TEST_DATABASE_URL`.
- `tests/integration/conftest.py` drops the `public` schema and runs `alembic upgrade head` once per session (it refuses to run on a DB name without "test"), then DELETEs all rows and flushes Redis after each test. Add new tables to `TABLES` there, children first.
- Tests use `httpx.AsyncClient(ASGITransport)` on one session-scoped event loop. A sync `TestClient` breaks the shared asyncpg pool across loops.
- Claude/embedder/reranker are patched at the **import site** (`app.routers.query.stream_answer`, etc.), not at their defining module. Celery `.delay` is patched in `app.routers.ingest`, and the worker body `_ingest_async` is called directly.
- Don't touch ORM attributes after `session.expire_all()` in async tests (MissingGreenlet). Use `session.get(..., populate_existing=True)`.

## Architecture

**Request path for `POST /query`** (`app/routers/query.py`): the whole pipeline runs inside the SSE generator `event_stream()`:
1. langdetect → Presidio PII scrub (engines cached; no-op if Presidio isn't installed)
2. fastembed embedding of the question (`services/embedder.py`: BAAI/bge-small-en-v1.5, **384-dim**)
3. The `Query` row is persisted as PROCESSING, then `services/semantic_cache.lookup`. Entries are partitioned by a hash of (language, filters), and **only DELIVERED answers are ever stored**, so a cache hit skips HITL safely. The cache is invalidated whenever a document is ingested or deleted.
4. Claude query expansion → embed variants → `retriever.retrieve_chunks` per variant (pgvector cosine) → dedup
5. Cross-encoder rerank (model cached, run via `asyncio.to_thread`; falls back to similarity order)
6. `build_context_block` assigns `SRC-001…` ids by **mutating the chunk dicts** (`_source_id`), which `extract_citations` then relies on
7. **HITL hold:** the full answer is generated server-side first (`: ping` SSE comments every 10s), then `hitl.classify_risk` runs. Low-risk answers are replayed as `delta` events. Flagged answers are **never sent**: the client gets `HELD_MESSAGE` with `hitl_required: true`. `/query/history` also redacts UNDER_REVIEW/REJECTED answers for the asker. Do not reintroduce token-by-token streaming of unclassified text.
8. Persist `Response` (with `risk_level` and real token usage from `get_final_message()`), queue `HITLReview`, record usage, cache (if delivered), and audit. Any exception marks the query FAILED, logs `QUERY_FAILED`, and sends a sanitized error (`llm.user_facing_llm_error`).

**Ingestion** (`routers/ingest.py` → `tasks/celery_app.py`): upload validates the size while streaming, the extension allow-list, and PDF/ZIP magic bytes. It stages the file in `services/upload_store` (`UPLOAD_DIR`, a volume shared by API and worker), creates a `Document` row and dispatches `tasks.ingest_document` with the document ID only; never put file bytes in the Celery payload (the optional `content_b64` argument only serves messages queued before 0.3). The task removes the staged file on success, on `DocumentParseError`, and after the last retry. `bulk_import.py` (image role `bulk-import`, logic in `services/bulk_import.py`) walks a folder and queues files the same way, streaming hashes and copies. Each task runs in a fresh `asyncio.run` loop, so `_ingest_async` builds a **per-task NullPool engine** (never cache an engine or async client across tasks). Retries first delete the document's existing chunks. `DocumentParseError` (bad file) fails without retrying, while other errors retry with backoff. Loaders return `(chunks, meta)`. `pdf_loader` OCRs pages with under 25 characters of text layer by piping a rendered PNG to the `tesseract` binary (subprocess, not a Python wrapper; `OCR_LANGUAGES`); without the binary it only logs a warning. Celery's Redis `visibility_timeout` must stay above `task_time_limit`, or a long task is redelivered to a second worker while still running. Afterwards come `services/insights.extract_document_insights` (Claude → `Document.insights_json`, feeding `/dashboard/insights`), the audit events `INGEST_COMPLETED`/`INGEST_FAILED`, and cache invalidation.

**Auth** (`middleware/auth.py`, `routers/auth.py`): JWTs carry `scope`: `full`, or `mfa_setup` (15 min). A `mfa_setup` token is issued to ENGINEER/ADMIN users who haven't enrolled TOTP, and is accepted **only** by `get_user_allow_mfa_setup` (`/auth/me`, `/auth/mfa/*`). Everything else uses `get_current_user`, which also rejects non-ACTIVE users on every request. Login flow: per-email/IP lockout in Redis (`rate_limit.ensure_login_allowed`) → password check (timing-equalized) → if enrolled, `totp_code` is required (401 `{"detail": {"code": "mfa_required" | "mfa_invalid"}}`; codes are single-use via Redis). "Enrolled" means `mfa_enabled AND mfa_secret`. Legacy/demo rows with `mfa_enabled` but no secret skip MFA **only when `DEMO_MODE=true`**. Admins can `POST /admin/users/{id}/reset-mfa`. Role guards: `RequireOperator` / `RequireEngineer` / `RequireAdmin` (OPERATOR < ENGINEER < ADMIN). The daily token budget is checked before `/query` (`token_budget_dependency`) and recorded after with real usage.

**HITL**: `services/hitl.py` owns the state transitions. Query status moves PROCESSING → DELIVERED | UNDER_REVIEW → DELIVERED/REJECTED, or → FAILED. Decisions are APPROVE / EDIT (requires `final_text`) / REJECT (requires `reason`), posted to `POST /review/{query_id}` under a row lock. They're audited as `HITL_APPROVED` / `HITL_EDITED` / `HITL_REJECTED`; older rows used `HITL_APPROVE` etc. and are normalized on read (`audit.LEGACY_EVENT_ALIASES`), because audit rows are never rewritten.

**Config** (`app/config.py`): `ENVIRONMENT=production` fails startup on a weak `JWT_SECRET` or (with `LLM_PROVIDER=anthropic`) a missing `ANTHROPIC_API_KEY`, hides `/docs`, and adds HSTS. `DEMO_MODE` gates `seed_demo.py`, `/ingest/phmsa-sync`, and the demo MFA bypass (the frontend's matching flag is `NEXT_PUBLIC_DEMO_MODE`). `CORS_ORIGINS` is comma-separated.

**LLM providers** (`services/llm.py`): `LLM_PROVIDER=anthropic|bedrock|vertex` picks the client in `make_client()` (`AsyncAnthropic` / `AsyncAnthropicBedrockMantle` / `AsyncAnthropicVertex`; same messages API). The API process uses the cached `get_client()`; Celery tasks must use `make_client()` (fresh per task, own event loop). Bedrock model IDs take an `anthropic.` prefix and Bedrock (Mantle) has no Sonnet 4.6, so its default is `anthropic.claude-sonnet-5`, which thinks by default: read responses with `response_text()`, never `content[0].text`. Cloud credentials come from the AWS / Google standard chains, not app settings. `python check_llm.py` (image role `llm-check`) smoke-tests the provider.

**Data model** (`models/db.py`): User, Invitation, Document, Chunk (`Vector(384)`), Query → Response (1:1) → HITLReview (1:1), plus an append-only AuditEvent written via `services/audit_log.log_event`. The schema is Alembic-only; the app lifespan never calls `create_all`. If you change the embedding model or dimension, you need a migration (see `0003_embedding_dim_384.py`) plus re-ingestion.

## Distribution (self-hosted, AGPL-3.0)

- **One backend image, many roles:** `backend/Dockerfile` builds a single image; `docker-entrypoint.sh` dispatches `api` (migrates, then uvicorn with proxy headers), `worker`, `flower`, `migrate`, `create-admin <email>`, `llm-check`. It runs as a non-root user with CPU-only torch. The fastembed, cross-encoder and spaCy `en_core_web_sm` models are baked in and `HF_HUB_OFFLINE=1`, so **nothing may download models at runtime**.
- **`deploy/` is what customers run:** a compose stack with Caddy (TLS) routing `/` to the frontend and `/backend/*` to the API, so the frontend image is domain-agnostic. Postgres and Redis sit on an `internal` network. It also holds the `install.sh`, `backup.sh`, `restore.sh` and `upgrade.sh` scripts, and `README.md`, the operator guide.
- **Releases:** pushing a `vX.Y.Z` tag runs `.github/workflows/release.yml`, which pushes `ghcr.io/kubu-ventures/pipelinegpt-backend` and creates a GitHub Release with the deploy tarball. The frontend repo tags the same version for its image.
- **Copyright holder:** Collins Kubu, sole author. No company is named anywhere in licensing or builds; commercial licenses and support are offered by the author directly. Outside code contributions are not accepted yet (no CLA). `CHANGELOG.md` follows Keep a Changelog; PRs use conventional-commit titles and the PR template.
- **PII scrubbing** is limited to `PII_ENTITIES` in `routers/query.py`. Presidio's defaults would redact segment IDs, places and dates.

## Gotchas

- `alembic revision --autogenerate` shows pre-existing model/migration drift (JSON vs JSONB, nullability) and would **drop the pgvector index `ix_chunks_embedding_ivfflat`**, which isn't declared on the model. Hand-write migrations or prune autogenerate output.
- Adding a Postgres enum value: use `op.get_context().autocommit_block()` (see `0005_hardening.py`).
- `/metrics` is unauthenticated. Block it at the reverse proxy in deployments.
- Unauthenticated requests get **401** (`HTTPBearer(auto_error=False)`), and wrong-role requests get 403.
- `.gitattributes` enforces LF. A Windows-side editor has previously re-saved files as CRLF, which shows up as whole-file diffs with no content change.
- `requirements-core.txt` (lean runtime) and `pyproject.toml` (full) both list runtime deps. Keep them in sync.
