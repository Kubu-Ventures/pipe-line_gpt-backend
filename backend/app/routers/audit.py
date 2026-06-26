from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import RequireAdmin, RequireEngineer, get_db, get_current_user
from app.models.db import AuditEvent, User
from app.models.schemas import AuditEventOut, AuditListResponse

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
async def list_audit_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    event_type: str | None = None,
    actor_id: str | None = None,
    user: Annotated[User, Depends(RequireEngineer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> AuditListResponse:
    """Return paginated, filterable audit trail. Read-only; no row can be deleted via API."""
    base_stmt = select(AuditEvent)

    if event_type:
        base_stmt = base_stmt.where(AuditEvent.event_type == event_type)
    if actor_id:
        base_stmt = base_stmt.where(AuditEvent.actor_id == actor_id)

    count_result = await db.execute(select(func.count()).select_from(base_stmt.subquery()))
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    stmt = base_stmt.order_by(AuditEvent.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(stmt)
    events = result.scalars().all()

    return AuditListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[AuditEventOut.model_validate(e) for e in events],
    )


@router.get("/export")
async def export_audit_csv(
    event_type: str | None = None,
    user: Annotated[User, Depends(RequireAdmin)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> StreamingResponse:
    """Stream all audit events as a CSV download for regulatory reporting."""
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.asc())
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)

    result = await db.execute(stmt)
    events = result.scalars().all()

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "event_type", "actor_id", "target_id", "target_type", "ip_address", "created_at"])
        for e in events:
            writer.writerow(
                [str(e.id), e.event_type, e.actor_id, e.target_id, e.target_type, e.ip_address, e.created_at.isoformat()]
            )
            yield output.getvalue()
            output.truncate(0)
            output.seek(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_export.csv"},
    )
