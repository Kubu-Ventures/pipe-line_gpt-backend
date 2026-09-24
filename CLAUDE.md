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
docker compose up --build                 # full stack; API is exposed on host port 8001, not 8000
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
1. langdetect → Presidio PII scrub (runs per request; the scrub silently falls back to raw text on error)
2. fastembed embedding of the question (`services/embedder.py`: BAAI/bge-small-en-v1.5, **384-dim**, cached with `lru_cache`)
3. Redis semantic cache: linear `KEYS query_cache:*` scan comparing cosine similarity against `SEMANTIC_CACHE_SIMILARITY`
4. Claude query expansion (`services/llm.expand_query`) → embed variants → `retriever.retrieve_chunks` per variant (pgvector `cosine_distance` joined to `Document`, with optional segment/commodity/year filters) → dedup by chunk id
5. Cross-encoder rerank (`ms-marco-MiniLM-L-6-v2`, loaded on every call; falls back to similarity order)
6. `build_context_block` assigns `SRC-001…` ids by **mutating the chunk dicts** (`_source_id`), which `extract_citations` then relies on
7. Stream Claude tokens as `data: {delta, citations, hitl_required, query_id}` events; the final event has `done: true` plus the full citations
8. Post-hoc: `estimate_confidence` (fraction of chunks cited) → `hitl.classify_risk` (regex on the answer: HIGH-risk verbs/fatality terms or MEDIUM keywords or confidence < threshold ⇒ HITL) → persist `Response`, queue `HITLReview`, consume token budget, write the cache, and write the audit event

Many steps use best-effort `try/except: pass`. Failures there are silent, so check them when debugging retrieval-quality problems.

**Ingestion** (`routers/ingest.py` → `tasks/celery_app.py`): upload creates a `Document` row, then dispatches `tasks.ingest_document` with the file content **base64-encoded** in the Celery JSON payload. The worker runs async code with `asyncio.run` and its own lazily created engine (it does not use `app.main`'s engine). It dispatches on `source_type` to `ingest/pdf_loader`, `csv_loader`, or `phmsa_loader` (TSV/ZIP). Each loader returns `(chunks, meta)`, where chunks are dicts with `chunk_index, text_content, token_count, page_ref?, section_label?`. The worker embeds in batches of 100, then calls `services/insights.extract_document_insights` (Claude → JSON of deadlines/anomalies/alarms stored in `Document.insights_json`), which feeds `/dashboard/insights` in `routers/health.py`.

**Auth/RBAC** (`middleware/auth.py`): HS256 JWT with `sub`=user id and `role`. `get_db` lazily imports `async_session_factory` from `app.main` to avoid a circular import. Role guards are the dependencies `RequireOperator` / `RequireEngineer` / `RequireAdmin` (the hierarchy is OPERATOR < ENGINEER < ADMIN). Registration is invite-only (`/admin/invite` → `/auth/accept-invite`), with TOTP MFA via pyotp. The per-user daily token budget lives in Redis (`middleware/rate_limit.py`).

**HITL**: `services/hitl.py` owns the state transitions. Query status moves PROCESSING → DELIVERED | UNDER_REVIEW → DELIVERED/REJECTED. Review decisions are APPROVE / EDIT (requires `final_text`) / REJECT, posted to `POST /review/{query_id}`. The README's `/review/{id}/decision` path is outdated.

**Data model** (`models/db.py`): User, Invitation, Document, Chunk (`Vector(384)`), Query → Response (1:1) → HITLReview (1:1), plus an append-only AuditEvent written via `services/audit_log.log_event`. The schema is Alembic-only; the app lifespan never calls `create_all`. If you change the embedding model or dimension, you need a migration (see `0003_embedding_dim_384.py`) plus re-ingestion.

## Gotchas

- CORS origins are hard-coded in `app/main.py` (localhost:3000 and pipelinegpt.xyz).
- Unauthenticated requests get **401** (`HTTPBearer(auto_error=False)`), and wrong-role requests get 403.
- `.gitattributes` enforces LF. A Windows-side editor has previously re-saved files as CRLF, which shows up as whole-file diffs with no content change.
- `requirements-core.txt` (lean runtime) and `pyproject.toml` (full) both list runtime deps. Keep them in sync.
