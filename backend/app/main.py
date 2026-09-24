from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.middleware.logging import StructuredLoggingMiddleware
from app.routers import admin, audit, auth, health, ingest, query, review

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic — no create_all here
    yield
    await engine.dispose()


app = FastAPI(
    title="PipelineGPT API",
    description="AI-powered natural language interface for pipeline integrity data.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response


app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(query.router)
app.include_router(ingest.router)
app.include_router(review.router)
app.include_router(audit.router)
