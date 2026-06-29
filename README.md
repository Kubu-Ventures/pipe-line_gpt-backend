# PipelineGPT Backend

A FastAPI backend that powers a retrieval-augmented generation (RAG) interface for pipeline integrity data. Operators and engineers ask questions in plain English against their own document corpus -- ILI reports, SCADA exports, PHMSA incident records, and compliance schedules -- and receive cited, source-grounded answers in real time.

## How it works

1. **Ingest** -- Documents (PDF, CSV, PHMSA TSV/ZIP) are uploaded and dispatched to an async Celery worker. The worker parses, chunks, and embeds each document using `BAAI/bge-small-en-v1.5` (384-dim, local ONNX inference via `fastembed`) and stores vectors in PostgreSQL via `pgvector`.
2. **Query** -- At query time the user's question is expanded into alternative phrasings via Claude, embedded, and used for cosine similarity search across the vector store. Candidate chunks are reranked with a cross-encoder (`ms-marco-MiniLM-L-6-v2`). The top passages are assembled into a cited context block and streamed back through Claude via Server-Sent Events.
3. **HITL review** -- Any answer that contains operational directives (repair, shut-in, pressure reduction, evacuation) or falls below the confidence threshold is held in a review queue. A qualified engineer must approve, edit, or reject the response before it is delivered to the operator.
4. **Audit** -- Every query, document ingestion, HITL decision, and deletion is written to an append-only audit log with actor identity, timestamp, and IP address.

## Tech stack

| Layer | Technology |
|---|---|
| API | FastAPI 0.115, Python 3.12 |
| ORM | SQLAlchemy 2.0 async + asyncpg |
| Database | PostgreSQL 16 + pgvector |
| Cache / broker | Redis 7 |
| Task queue | Celery 5 |
| Embeddings | fastembed (BAAI/bge-small-en-v1.5, 384-dim, local ONNX) |
| Reranking | sentence-transformers cross-encoder/ms-marco-MiniLM-L-6-v2 |
| LLM | Anthropic Claude (claude-sonnet-4-6) |
| Auth | JWT (python-jose) + bcrypt + TOTP MFA (pyotp) |
| PII scrubbing | Microsoft Presidio |
| Migrations | Alembic |
| Observability | Prometheus (via prometheus-fastapi-instrumentator), Celery Flower |

## Prerequisites

- Docker and Docker Compose, **or**
- Python 3.12, PostgreSQL 16 with the `pgvector` extension, and Redis 7 for local development
- An [Anthropic API key](https://console.anthropic.com/)

## Quick start with Docker Compose

```bash
# 1. Clone and enter the backend directory
git clone https://github.com/Kubu-Ventures/Rosen-backend.git
cd Rosen-backend/backend

# 2. Create your environment file
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY and JWT_SECRET at minimum

# 3. Start all services (Postgres, Redis, API, Celery worker, Flower)
docker compose up --build
```

The API will be available at `http://localhost:8000`. Interactive docs are at `http://localhost:8000/docs`. Celery Flower (task monitor) runs at `http://localhost:5555`.

## Local development (without Docker)

### 1. Install dependencies

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Start Postgres and Redis

```bash
# Quickest path -- just the infrastructure services
docker compose up db redis -d
```

### 3. Configure environment

```bash
cp .env.example .env
```

Set the following values in `.env`:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | asyncpg connection string | `postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:5432/pipelinegpt` |
| `REDIS_URL` | Redis connection string | `redis://localhost:6379/0` |
| `ANTHROPIC_API_KEY` | Anthropic API key | _(required)_ |
| `JWT_SECRET` | Secret used to sign JWT tokens | _(required -- use a long random string)_ |
| `JWT_ALGORITHM` | JWT signing algorithm | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime | `480` |
| `LLM_MODEL` | Claude model ID | `claude-sonnet-4-6` |
| `HITL_CONFIDENCE_THRESHOLD` | Confidence below which HITL is triggered | `0.75` |
| `TOP_K_RETRIEVAL` | Number of chunks fetched from vector store | `12` |
| `TOP_K_RERANK` | Number of chunks kept after reranking | `6` |
| `SEMANTIC_CACHE_SIMILARITY` | Cosine similarity threshold for cache hit | `0.97` |
| `UPLOAD_MAX_BYTES` | Maximum upload size | `52428800` (50 MB) |

### 4. Run migrations

```bash
alembic -c alembic/alembic.ini upgrade head
```

### 5. Create the first admin user

```bash
python create_admin.py
```

### 6. Start the API server

```bash
uvicorn app.main:app --reload --port 8000
```

### 7. Start the Celery worker (separate terminal)

```bash
celery -A app.tasks.celery_app.celery_app worker --loglevel=info --concurrency=4
```

## API overview

All routes require a Bearer JWT unless noted.

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/auth/login` | Public | Authenticate and receive a JWT |
| POST | `/auth/accept-invite` | Public | Complete registration from an invitation token |
| POST | `/auth/mfa/setup` | Any | Generate a TOTP secret and QR URI |
| POST | `/auth/mfa/verify` | Any | Verify a TOTP code and enable MFA |
| POST | `/query` | OPERATOR+ | Submit a question -- streams SSE response |
| GET | `/query/history` | OPERATOR+ | Retrieve the current user's query history |
| POST | `/ingest` | OPERATOR+ | Upload a document for async processing |
| GET | `/ingest/status/{task_id}` | OPERATOR+ | Poll ingestion task status |
| GET | `/ingest/history` | OPERATOR+ | List all ingested documents |
| DELETE | `/ingest/{document_id}` | ENGINEER+ | Remove a document and its chunks |
| POST | `/ingest/phmsa-sync` | OPERATOR+ | Load sample pipeline datasets for demo |
| GET | `/review` | ENGINEER+ | Paginated HITL review queue |
| POST | `/review/{review_id}/decision` | ENGINEER+ | Submit approve / edit / reject |
| GET | `/admin/users` | ADMIN | List all users |
| POST | `/admin/invite` | ADMIN | Send an invitation |
| PATCH | `/admin/users/{id}/status` | ADMIN | Suspend or activate a user |
| GET | `/audit` | ENGINEER+ | Paginated audit event log |
| GET | `/health` | Public | Liveness check |
| GET | `/metrics` | Public | Prometheus metrics |

### SSE query response format

Each `data:` event is a JSON object:

```json
{ "delta": "...token...", "citations": [], "hitl_required": false, "query_id": "uuid" }
```

The final event sets `"done": true` and includes the full `citations` array:

```json
{
  "delta": "",
  "citations": [{ "source_id": "SRC-001", "filename": "ILI_Report.csv", "excerpt": "..." }],
  "hitl_required": true,
  "query_id": "uuid",
  "done": true
}
```

## User roles

| Role | Capabilities |
|---|---|
| `OPERATOR` | Query, view history, upload documents, load demo data |
| `ENGINEER` | All operator capabilities + HITL review queue + document deletion |
| `ADMIN` | All engineer capabilities + user management + invitation management |

Registration is invite-only. Admins send invitations via `POST /admin/invite`. Engineers and admins must complete TOTP-based MFA setup on first login.

## Database migrations

Migrations live in `alembic/versions/`. To create a new migration after changing `app/models/db.py`:

```bash
alembic -c alembic/alembic.ini revision --autogenerate -m "describe the change"
alembic -c alembic/alembic.ini upgrade head
```

To roll back one step:

```bash
alembic -c alembic/alembic.ini downgrade -1
```

## Running tests

```bash
pytest
```

Tests require a running Postgres and Redis instance. The test suite uses `pytest-asyncio` with `asyncio_mode = "auto"`.

## Project structure

```
backend/
├── alembic/
│   ├── versions/          # Migration files
│   └── env.py
├── app/
│   ├── ingest/            # Document parsers (PDF, CSV, PHMSA)
│   ├── middleware/        # Auth, rate limiting, structured logging
│   ├── models/
│   │   ├── db.py          # SQLAlchemy ORM models
│   │   └── schemas.py     # Pydantic request/response schemas
│   ├── routers/           # FastAPI route handlers
│   ├── services/
│   │   ├── embedder.py    # fastembed wrapper + cosine similarity
│   │   ├── retriever.py   # pgvector search + cross-encoder reranking
│   │   ├── llm.py         # Claude streaming, query expansion, citation extraction
│   │   ├── hitl.py        # Risk classification + HITL queue logic
│   │   └── audit_log.py   # Append-only event logging
│   ├── tasks/
│   │   └── celery_app.py  # Celery app + ingest task
│   ├── config.py          # Pydantic settings (reads from .env)
│   └── main.py            # FastAPI app factory, router registration
├── tests/
├── create_admin.py        # One-off admin user creation script
├── seed_demo.py           # Seed realistic sample data
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```
