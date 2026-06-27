from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.middleware.logging import StructuredLoggingMiddleware
from app.routers import admin, audit, auth, health, ingest, query, review

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
)

app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(query.router)
app.include_router(ingest.router)
app.include_router(review.router)
app.include_router(audit.router)
