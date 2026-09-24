.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test test-unit test-integration test-services test-services-down migrate run worker

VENV ?= .venv
BIN := $(VENV)/bin
export TEST_DATABASE_URL ?= postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:55432/pipelinegpt_test
export TEST_REDIS_URL ?= redis://localhost:56379/15

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

install: ## Create venv and install app + dev tools
	uv venv $(VENV) --python 3.12 --allow-existing
	uv pip install --python $(BIN)/python -e ".[dev]"

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-fix lint and format
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck: ## mypy (not yet enforced in CI)
	$(BIN)/mypy app

test-unit: ## Unit tests, no services needed
	$(BIN)/pytest tests/unit

test-services: ## Start throwaway Postgres + Redis for integration tests
	docker compose -f docker-compose.test.yml up -d --wait

test-services-down: ## Stop test services
	docker compose -f docker-compose.test.yml down

test-integration: test-services ## Integration tests against test services
	$(BIN)/pytest tests/integration

test: test-services ## Full suite with coverage
	$(BIN)/pytest --cov --cov-report=term-missing:skip-covered

migrate: ## Apply migrations to DATABASE_URL
	$(BIN)/alembic -c alembic/alembic.ini upgrade head

run: ## API with autoreload on :8000
	$(BIN)/uvicorn app.main:app --reload --port 8000

worker: ## Celery ingest worker
	$(BIN)/celery -A app.tasks.celery_app.celery_app worker --loglevel=info --concurrency=4
