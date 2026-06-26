from __future__ import annotations

import base64
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import RequireOperator, get_db, get_current_user
from app.models.db import Document, User
from app.models.schemas import IngestResponse, IngestStatusResponse
from app.services import audit_log
from app.services.embedder import content_hash
from app.tasks.celery_app import celery_app, ingest_document_task

router = APIRouter(prefix="/ingest", tags=["ingest"])

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/csv",
    "text/plain",
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
    "application/vnd.ms-excel",
}

EXTENSION_TO_SOURCE_TYPE = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".tsv": "phmsa",
    ".txt": "phmsa",
    ".zip": "phmsa_zip",
}


@router.post("", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_file(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IngestResponse:
    """Upload a document for async ingestion. Returns a task_id for status polling."""

    # Validate size
    content = await file.read()
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.upload_max_bytes // 1_048_576} MB limit.",
        )

    # Validate MIME
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}",
        )

    filename = file.filename or "upload"
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    source_type = EXTENSION_TO_SOURCE_TYPE.get(suffix, "pdf")

    # SHA-256 deduplication
    sha256 = content_hash(content)
    existing = await db.execute(select(Document).where(Document.sha256_hash == sha256))
    existing_doc = existing.scalar_one_or_none()
    if existing_doc:
        return IngestResponse(
            task_id="dedup-skip",
            document_id=str(existing_doc.id),
            filename=filename,
            message="File already ingested (SHA-256 match). Skipping re-embedding.",
        )

    # Create document record
    doc_id = uuid.uuid4()
    doc = Document(
        id=doc_id,
        filename=filename,
        source_type=source_type,
        sha256_hash=sha256,
        status="PENDING",
    )
    db.add(doc)
    await db.commit()

    # Dispatch Celery task
    content_b64 = base64.b64encode(content).decode()
    task = ingest_document_task.delay(
        str(doc_id),
        source_type,
        filename,
        content_b64,
    )

    await audit_log.log_event(
        db,
        event_type="INGEST_SUBMITTED",
        actor_id=str(user.id),
        target_id=str(doc_id),
        target_type="document",
        payload={"filename": filename, "source_type": source_type, "task_id": task.id},
        ip_address=request.client.host if request.client else None,
    )

    return IngestResponse(
        task_id=task.id,
        document_id=str(doc_id),
        filename=filename,
        message="Ingestion queued successfully.",
    )


@router.get("/status/{task_id}", response_model=IngestStatusResponse)
async def ingest_status(task_id: str) -> IngestStatusResponse:
    """Poll the status of an ingestion task."""
    from celery.result import AsyncResult

    result = AsyncResult(task_id, app=celery_app)
    task_result = None
    if result.ready() and result.successful():
        task_result = result.get()

    return IngestStatusResponse(
        task_id=task_id,
        status=result.status,
        document_id=task_result.get("document_id") if task_result else None,
        chunk_count=task_result.get("chunk_count") if task_result else None,
    )
