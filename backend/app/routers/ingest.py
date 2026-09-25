from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ingest.file_types import EXTENSION_TO_SOURCE_TYPE, has_valid_magic, source_type_for
from app.middleware.auth import RequireEngineer, RequireOperator, get_db
from app.middleware.rate_limit import get_redis
from app.models.db import Document, User
from app.models.schemas import IngestResponse, IngestStatusResponse
from app.services import audit_log, semantic_cache, upload_store
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


@router.post("", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_file(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IngestResponse:
    """Upload a document for async ingestion. Returns a task_id for status polling."""

    # Read in bounded chunks so an oversized upload is rejected without buffering all of it.
    buf = bytearray()
    while chunk := await file.read(1024 * 1024):
        buf.extend(chunk)
        if len(buf) > settings.upload_max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds {settings.upload_max_bytes // 1_048_576} MB limit.",
            )
    content = bytes(buf)
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    # Validate MIME
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}",
        )

    filename = (file.filename or "upload").replace("\\", "/").rsplit("/", 1)[-1][:255]
    source_type = source_type_for(filename)
    if source_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file extension. Allowed: {', '.join(sorted(EXTENSION_TO_SOURCE_TYPE))}",
        )
    if not has_valid_magic(source_type, content):
        kind = "PDF" if source_type == "pdf" else "ZIP"
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"File is not a valid {kind}.")

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

    # Stage the file for the worker, then create the document record
    doc_id = uuid.uuid4()
    await asyncio.to_thread(upload_store.save, str(doc_id), content)
    doc = Document(
        id=doc_id,
        filename=filename,
        source_type=source_type,
        sha256_hash=sha256,
        status="PENDING",
        operator_id=user.email,
    )
    db.add(doc)
    await db.commit()

    # Dispatch Celery task (the file travels via upload_store, not the queue)
    task = ingest_document_task.delay(str(doc_id), source_type, filename)

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
async def ingest_status(
    task_id: str,
    user: Annotated[User, Depends(RequireOperator)],
) -> IngestStatusResponse:
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


@router.get("/history")
async def ingest_history(
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """Return all ingested documents ordered by most recent first."""
    result = await db.execute(select(Document).order_by(Document.ingest_date.desc()).limit(100))
    docs = result.scalars().all()
    return [
        {
            "id": str(doc.id),
            "filename": doc.filename,
            "source_type": doc.source_type,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "ingest_date": doc.ingest_date.isoformat() if doc.ingest_date else None,
            "segment_id": doc.segment_id,
            "commodity": doc.commodity,
            "uploaded_by": doc.operator_id,
        }
        for doc in docs
    ]


@router.get("/chunk/{document_id}/{chunk_index}")
async def get_chunk_text(
    document_id: uuid.UUID,
    chunk_index: int,
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Return the full text of a specific chunk for display in the citation panel."""
    from app.models.db import Chunk

    result = await db.execute(
        select(Chunk).where(
            Chunk.document_id == document_id,
            Chunk.chunk_index == chunk_index,
        )
    )
    chunk = result.scalar_one_or_none()
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return {
        "document_id": str(document_id),
        "chunk_index": chunk_index,
        "text_content": chunk.text_content,
        "section_label": chunk.section_label,
        "page_ref": chunk.page_ref,
    }


@router.delete("/{document_id}", status_code=status.HTTP_200_OK)
async def delete_document(
    document_id: str,
    request: Request,
    user: Annotated[User, Depends(RequireEngineer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Remove a document and all its chunks from the knowledge base.
    Restricted to engineers and admins — deletion affects all users' query results.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid document ID.") from None

    result = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    filename = doc.filename
    chunk_count = doc.chunk_count or 0

    await db.delete(doc)
    await db.commit()
    upload_store.remove(document_id)  # only still there if the document was never processed
    await semantic_cache.invalidate(get_redis())

    await audit_log.log_event(
        db,
        event_type="DOCUMENT_DELETED",
        actor_id=str(user.id),
        target_id=document_id,
        target_type="document",
        payload={"filename": filename, "chunk_count": chunk_count, "deleted_by_role": user.role},
        ip_address=request.client.host if request.client else None,
    )

    return {"deleted": True, "document_id": document_id, "filename": filename, "chunks_removed": chunk_count}


def _build_demo_datasets() -> list[tuple[str, str, bytes]]:
    """Generate realistic sample pipeline datasets for demo/testing."""
    import csv
    import io

    # ── ILI inspection report ────────────────────────────────────────────────
    ili_rows = [
        [
            "segment_id",
            "inspection_date",
            "feature_type",
            "odometer_m",
            "wall_loss_pct",
            "depth_mm",
            "length_mm",
            "clock_position",
            "action_required",
            "commodity",
        ],
        [
            "SEG-TX-4B",
            "2024-03-15",
            "Metal Loss - External Corrosion",
            1240.5,
            32.4,
            4.1,
            85,
            "6:00",
            "Monitor - Re-inspect within 12 months",
            "Crude Oil",
        ],
        [
            "SEG-TX-4B",
            "2024-03-15",
            "Metal Loss - External Corrosion",
            2890.2,
            41.8,
            5.3,
            120,
            "4:30",
            "IMMEDIATE ACTION - Wall loss exceeds 40% ASME B31.8S threshold",
            "Crude Oil",
        ],
        ["SEG-TX-4B", "2024-03-15", "Dent", 4510.0, 0, 12.2, 200, "12:00", "Monitor", "Crude Oil"],
        [
            "SEG-TX-4B",
            "2024-03-15",
            "Metal Loss - Internal Corrosion",
            6720.8,
            18.2,
            2.3,
            45,
            "6:00",
            "No action required",
            "Crude Oil",
        ],
        [
            "SEG-TX-4B",
            "2024-03-15",
            "Metal Loss - External Corrosion",
            9340.1,
            55.1,
            7.0,
            160,
            "3:00",
            "IMMEDIATE ACTION - Critical wall loss",
            "Crude Oil",
        ],
        [
            "SEG-TX-4B",
            "2024-03-15",
            "Crack - Seam Weld",
            11200.4,
            0,
            0,
            95,
            "9:00",
            "Pressure reduction required",
            "Crude Oil",
        ],
        [
            "SEG-TX-4B",
            "2024-03-15",
            "Metal Loss - External Corrosion",
            14800.7,
            28.9,
            3.7,
            70,
            "6:30",
            "Monitor - Re-inspect within 18 months",
            "Crude Oil",
        ],
        [
            "SEG-TX-7A",
            "2023-11-02",
            "Metal Loss - External Corrosion",
            550.3,
            38.5,
            4.9,
            110,
            "6:00",
            "Repair within 60 days",
            "Natural Gas",
        ],
        [
            "SEG-TX-7A",
            "2023-11-02",
            "Metal Loss - External Corrosion",
            2100.6,
            22.1,
            2.8,
            55,
            "5:00",
            "Monitor",
            "Natural Gas",
        ],
        [
            "SEG-TX-7A",
            "2023-11-02",
            "Dent with Metal Loss",
            3850.9,
            15.3,
            1.9,
            40,
            "3:00",
            "Monitor closely",
            "Natural Gas",
        ],
    ]
    ili_buf = io.StringIO()
    csv.writer(ili_buf).writerows(ili_rows)

    # ── SCADA export ─────────────────────────────────────────────────────────
    scada_rows = [
        ["timestamp", "segment_id", "station", "pressure_psi", "flow_rate_mcfd", "temperature_f", "status", "alarm"],
        ["2024-06-01 00:00", "SEG-TX-4B", "PUMP-01", 812, 45200, 72.3, "NORMAL", ""],
        ["2024-06-01 01:00", "SEG-TX-4B", "PUMP-01", 815, 45100, 71.8, "NORMAL", ""],
        ["2024-06-01 06:00", "SEG-TX-4B", "PUMP-01", 798, 46300, 70.1, "NORMAL", ""],
        ["2024-06-01 12:00", "SEG-TX-4B", "PUMP-01", 821, 44800, 79.5, "NORMAL", ""],
        ["2024-06-01 18:00", "SEG-TX-4B", "PUMP-01", 834, 43900, 83.2, "WARNING", "HIGH_PRESSURE"],
        ["2024-06-02 00:00", "SEG-TX-4B", "PUMP-01", 809, 45500, 71.6, "NORMAL", ""],
        [
            "2024-06-02 06:00",
            "SEG-TX-4B",
            "PUMP-01",
            756,
            41200,
            69.8,
            "ALARM",
            "PRESSURE_DROP - Possible leak at MP 9.34",
        ],
        ["2024-06-02 07:00", "SEG-TX-4B", "PUMP-01", 748, 40100, 69.5, "ALARM", "PRESSURE_DROP"],
        ["2024-06-02 08:00", "SEG-TX-4B", "PUMP-01", 812, 45000, 70.2, "NORMAL", "RESOLVED"],
        ["2024-06-03 00:00", "SEG-TX-7A", "COMP-03", 920, 82000, 65.4, "NORMAL", ""],
        ["2024-06-03 12:00", "SEG-TX-7A", "COMP-03", 935, 81500, 71.2, "NORMAL", ""],
    ]
    scada_buf = io.StringIO()
    csv.writer(scada_buf).writerows(scada_rows)

    # ── PHMSA-style incident report ──────────────────────────────────────────
    phmsa_rows = [
        [
            "REPORT_NUMBER",
            "ACCIDENT_DATE",
            "OPERATOR_NAME",
            "SYSTEM_TYPE",
            "COMMODITY",
            "STATE",
            "COUNTY",
            "CAUSE_CATEGORY",
            "CAUSE_SUBCATEGORY",
            "TOTAL_COST_CURRENT",
            "FATALITIES",
            "INJURIES",
            "VOLUME_LOST_BBL",
            "NARRATIVE",
        ],
        [
            "20240001",
            "2024-01-14",
            "Gulf Coast Pipeline LLC",
            "HVL AND OTHER FLAMMABLE/TOXIC GAS",
            "Crude Oil",
            "TX",
            "Harris",
            "CORROSION",
            "EXTERNAL CORROSION",
            285000,
            0,
            0,
            42.5,
            "External corrosion failure on 12-inch crude oil line at MP 9.34. Wall loss exceeded 40% threshold identified in prior ILI. Repair completed within 48 hours.",
        ],
        [
            "20240002",
            "2024-02-28",
            "Midland Gas Transmission Co",
            "NATURAL GAS TRANSMISSION",
            "Natural Gas",
            "TX",
            "Midland",
            "EXCAVATION DAMAGE",
            "THIRD-PARTY EXCAVATION",
            142000,
            0,
            1,
            0,
            "Third-party contractor struck 6-inch gas line during road construction. One worker received minor burns. Line isolated and repaired within 6 hours.",
        ],
        [
            "20240003",
            "2024-03-10",
            "Permian Basin Pipeline Inc",
            "HVL AND OTHER FLAMMABLE/TOXIC GAS",
            "NGL",
            "NM",
            "Lea",
            "MATERIAL/WELD/EQUIP FAILURE",
            "PIPE BODY FAILURE",
            890000,
            0,
            0,
            218.3,
            "Longitudinal seam weld failure on 16-inch NGL line. Cause attributed to stress corrosion cracking. Line segment replaced with upgraded material.",
        ],
        [
            "20240004",
            "2024-04-05",
            "Southern Gas Distribution LLC",
            "NATURAL GAS DISTRIBUTION",
            "Natural Gas",
            "LA",
            "Orleans",
            "CORROSION",
            "INTERNAL CORROSION",
            67000,
            0,
            0,
            0,
            "Internal corrosion pinhole leak on 4-inch gas distribution line in residential area. Area evacuated, leak isolated, section replaced.",
        ],
        [
            "20240005",
            "2024-05-20",
            "Rocky Mountain Crude Transport",
            "HVL AND OTHER FLAMMABLE/TOXIC GAS",
            "Crude Oil",
            "WY",
            "Sweetwater",
            "INCORRECT OPERATION",
            "INCORRECT OPERATION",
            195000,
            0,
            0,
            31.7,
            "Operator error during valve maintenance resulted in overpressure condition and fitting failure. Revised operating procedures implemented.",
        ],
        [
            "20240006",
            "2024-06-03",
            "Gulf Coast Pipeline LLC",
            "HVL AND OTHER FLAMMABLE/TOXIC GAS",
            "Crude Oil",
            "TX",
            "Brazoria",
            "CORROSION",
            "EXTERNAL CORROSION",
            412000,
            0,
            0,
            88.4,
            "External corrosion failure adjacent to coating holiday. ILI had flagged feature for monitoring. Inspection interval to be reduced.",
        ],
    ]
    phmsa_buf = io.StringIO()
    csv.writer(phmsa_buf).writerows(phmsa_rows)

    # ── Compliance & integrity management schedule ───────────────────────────
    imp_rows = [
        ["segment_id", "regulation", "obligation", "due_date", "status", "last_assessment", "notes"],
        [
            "SEG-TX-4B",
            "49 CFR §195.452",
            "ILI reassessment - High Consequence Area",
            "2024-09-30",
            "OVERDUE",
            "2019-08-14",
            "HCA segment exceeds 7-year reassessment interval. Schedule immediately.",
        ],
        [
            "SEG-TX-4B",
            "49 CFR §195.452(j)(3)",
            "Pressure test - post-repair",
            "2024-07-15",
            "DUE",
            "N/A",
            "Required following June corrosion repair at MP 9.34.",
        ],
        [
            "SEG-TX-7A",
            "49 CFR §192.919",
            "Baseline assessment - ILI",
            "2024-08-01",
            "IN PROGRESS",
            "Never",
            "New segment added to IMP. Baseline ILI tool run scheduled July 2024.",
        ],
        [
            "SEG-NM-2C",
            "49 CFR §195.452(j)(1)",
            "Integrity assessment",
            "2024-12-31",
            "UPCOMING",
            "2019-12-10",
            "5-year reassessment due. ILI vendor contract in place.",
        ],
        [
            "SEG-NM-2C",
            "ASME B31.8S §5",
            "Engineering critical assessment",
            "2024-10-15",
            "UPCOMING",
            "2020-03-22",
            "Required after crack-like feature identified in 2020 assessment.",
        ],
        [
            "SEG-LA-9D",
            "49 CFR §192.723",
            "Leakage survey - Grade 1",
            "2024-07-01",
            "DUE",
            "2023-07-05",
            "Annual leakage survey required for Class 3 location.",
        ],
    ]
    imp_buf = io.StringIO()
    csv.writer(imp_buf).writerows(imp_rows)

    return [
        ("ILI_Report_SEG-TX-4B_and_7A_2024.csv", "csv", ili_buf.getvalue().encode()),
        ("SCADA_Export_SEG-TX-4B_7A_June2024.csv", "csv", scada_buf.getvalue().encode()),
        ("PHMSA_Incident_Report_2024_Sample.csv", "csv", phmsa_buf.getvalue().encode()),
        ("Integrity_Management_Schedule_2024.csv", "csv", imp_buf.getvalue().encode()),
    ]


@router.post("/phmsa-sync", status_code=status.HTTP_202_ACCEPTED)
async def phmsa_sync(
    request: Request,
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Load realistic sample pipeline datasets for demo and testing (DEMO_MODE only)."""
    if not settings.demo_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    datasets = _build_demo_datasets()
    task_ids = []

    for fname, source_type, content in datasets:
        sha256 = content_hash(content)
        existing = await db.execute(select(Document).where(Document.sha256_hash == sha256))
        if existing.scalar_one_or_none():
            continue

        doc_id = uuid.uuid4()
        upload_store.save(str(doc_id), content)
        doc = Document(
            id=doc_id,
            filename=fname,
            source_type=source_type,
            sha256_hash=sha256,
            status="PENDING",
            operator_id=user.email,
        )
        db.add(doc)
        await db.commit()

        task = ingest_document_task.delay(str(doc_id), source_type, fname)
        task_ids.append(task.id)

        await audit_log.log_event(
            db,
            event_type="DEMO_DATA_SYNC",
            actor_id=str(user.id),
            target_id=str(doc_id),
            target_type="document",
            payload={"filename": fname},
            ip_address=request.client.host if request.client else None,
        )

    return {"task_ids": task_ids, "queued": len(task_ids)}
