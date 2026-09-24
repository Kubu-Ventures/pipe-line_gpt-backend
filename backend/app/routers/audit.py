from __future__ import annotations

import csv
import io
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import RequireEngineer, get_db
from app.models.db import AuditEvent, User
from app.models.schemas import AuditEventOut, AuditListResponse

router = APIRouter(prefix="/audit", tags=["audit"])

# Identity / security events — only ADMIN should see these
ADMIN_ONLY_EVENT_TYPES = {
    "USER_LOGIN",
    "USER_LOGIN_FAILED",
    "USER_MFA_RESET",
    "USER_INVITED",
    "USER_INVITED_ACCEPTED",
    "USER_MFA_ENROLLED",
    "CONFIG_CHANGE",
}

# Per-event-type metadata used in the CSV export
# (category, human label, regulatory reference)
_EVENT_META: dict[str, tuple[str, str, str]] = {
    "HITL_APPROVED": ("Engineer Decision", "HITL Approval", "49 CFR §192.911 / ASME B31.8S §6"),
    "HITL_REJECTED": ("Engineer Decision", "HITL Rejection", "49 CFR §192.911 / ASME B31.8S §6"),
    "HITL_EDITED": ("Engineer Decision", "HITL Edit & Approval", "49 CFR §192.911 / ASME B31.8S §6"),
    "ANOMALY_ESCALATED": ("Integrity Alert", "Anomaly Escalation", "ASME B31.8S §4 / 49 CFR §192.933"),
    "COMPLIANCE_FLAG": ("Compliance Alert", "IMP Deadline Flag", "49 CFR §192.945 / §192.947"),
    "QUERY_COMPLETED": ("AI Query", "Query Answered", "49 CFR §192.911"),
    "INGEST_COMPLETED": ("Document Management", "Document Ingested", "49 CFR §192.911 (records)"),
    "INGEST_FAILED": ("Document Management", "Document Ingestion Failed", "49 CFR §192.911 (records)"),
    "DOCUMENT_DELETED": ("Document Management", "Document Deleted", "49 CFR §192.911 (records)"),
    "QUERY_FAILED": ("AI Query", "Query Failed", "49 CFR §192.911"),
    "USER_LOGIN": ("Security", "User Login", "49 CFR §192.911 (access control)"),
    "USER_LOGIN_FAILED": ("Security", "Failed Sign-in", "49 CFR §192.911 (access control)"),
    "USER_MFA_RESET": ("Security", "MFA Reset by Admin", "49 CFR §192.911 (access control)"),
    "USER_STATUS_CHANGED": ("Security", "Account Status Changed", "49 CFR §192.911 (access control)"),
    "USER_INVITED": ("Security", "Invitation Sent", "49 CFR §192.911 (access control)"),
    "USER_INVITED_ACCEPTED": ("Security", "Invitation Accepted", "49 CFR §192.911 (access control)"),
    "USER_MFA_ENROLLED": ("Security", "MFA Enrolled", "49 CFR §192.911 (access control)"),
    "CONFIG_CHANGE": ("System", "Configuration Changed", "49 CFR §192.911"),
}


# Event names written by earlier releases. Audit rows are append-only, so they are
# normalized when read instead of being rewritten.
LEGACY_EVENT_ALIASES = {
    "HITL_APPROVE": "HITL_APPROVED",
    "HITL_EDIT": "HITL_EDITED",
    "HITL_REJECT": "HITL_REJECTED",
}


def canonical_event_type(event_type: str) -> str:
    return LEGACY_EVENT_ALIASES.get(event_type, event_type)


def _stored_names(event_type: str) -> list[str]:
    return [event_type, *(old for old, new in LEGACY_EVENT_ALIASES.items() if new == event_type)]


def _csv_safe(value: object) -> str:
    """Neutralize spreadsheet formula injection in user-controlled cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def _derive_csv_row(
    e: AuditEvent,
    actor_email: str,
    category: str,
    event_label: str,
    reg_ref: str,
) -> list[str]:
    p = e.payload_json or {}

    actor = p.get("email") or actor_email or str(e.actor_id or "system")

    segment = p.get("segment") or p.get("filename") or p.get("obligation") or "—"

    conf_raw = p.get("confidence")
    confidence = f"{round(float(conf_raw) * 100, 1)}%" if conf_raw is not None else "—"

    risk = p.get("risk_level") or "—"

    etype = canonical_event_type(e.event_type)
    if etype == "HITL_APPROVED":
        decision = p.get("final_action") or "Approved"
    elif etype == "HITL_REJECTED":
        decision = f"Rejected — {p.get('reason', 'no reason recorded')}"
    elif etype == "HITL_EDITED":
        decision = p.get("final_action") or "Edited and approved"
    elif etype == "ANOMALY_ESCALATED":
        wl = p.get("wall_loss")
        decision = p.get("action_required") or (
            f"{wl}% wall loss exceeds {p.get('threshold', 40)}% threshold" if wl else "—"
        )
    elif etype == "COMPLIANCE_FLAG":
        decision = f"Due {p.get('due_date', '—')} — {p.get('days_remaining', '—')} days remaining"
    elif etype == "QUERY_COMPLETED":
        hitl = p.get("hitl_required", False)
        decision = f"Answered — HITL required: {'Yes' if hitl else 'No'}"
    elif etype == "INGEST_COMPLETED":
        chunks = p.get("chunks")
        decision = f"{chunks:,} chunks indexed" if isinstance(chunks, int) else "Indexed"
    elif etype in ("USER_LOGIN", "USER_INVITED", "USER_INVITED_ACCEPTED"):
        role = p.get("role")
        decision = f"Role: {role}" if role else "Authenticated"
    else:
        decision = "—"

    notes = p.get("engineer_note") or p.get("question_summary") or p.get("anomaly") or p.get("change_summary") or ""

    return [
        str(e.id),
        e.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if e.created_at else "—",
        category,
        event_label,
        actor,
        segment,
        confidence,
        risk,
        decision,
        reg_ref,
        e.ip_address or "—",
        notes,
    ]


@router.get("", response_model=AuditListResponse)
async def list_audit_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    event_type: str | None = None,
    actor_id: str | None = None,
    user: Annotated[User, Depends(RequireEngineer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> AuditListResponse:
    """Paginated, filterable audit trail. Engineers see operational events only; admins see all."""
    base_stmt = select(AuditEvent)

    # Engineers cannot see identity / security events
    if user.role == "ENGINEER":
        base_stmt = base_stmt.where(AuditEvent.event_type.not_in(ADMIN_ONLY_EVENT_TYPES))

    # Caller-supplied filters
    if event_type:
        # Silently ignore if engineer requests an admin-only type
        if user.role == "ENGINEER" and event_type in ADMIN_ONLY_EVENT_TYPES:
            return AuditListResponse(total=0, page=page, page_size=page_size, items=[])
        base_stmt = base_stmt.where(AuditEvent.event_type.in_(_stored_names(event_type)))
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
        items=[
            AuditEventOut.model_validate(e).model_copy(update={"event_type": canonical_event_type(e.event_type)})
            for e in events
        ],
    )


@router.get("/export")
async def export_audit_csv(
    event_type: str | None = None,
    user: Annotated[User, Depends(RequireEngineer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> StreamingResponse:
    """
    Download a PHMSA-formatted CSV of audit events.
    Engineers receive operational events only; admins receive the full log.
    Suitable as supporting documentation for IMP compliance reviews under
    49 CFR §192.911 and §192.945.
    """
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.asc())

    if user.role == "ENGINEER":
        stmt = stmt.where(AuditEvent.event_type.not_in(ADMIN_ONLY_EVENT_TYPES))
        if event_type and event_type in ADMIN_ONLY_EVENT_TYPES:
            event_type = None  # ignore the filter silently

    if event_type:
        stmt = stmt.where(AuditEvent.event_type.in_(_stored_names(event_type)))

    result = await db.execute(stmt)
    events = result.scalars().all()

    # Bulk-resolve actor UUIDs → emails to avoid N+1 queries
    actor_uuids = {uuid.UUID(e.actor_id) for e in events if e.actor_id and _is_valid_uuid(e.actor_id)}
    actor_emails: dict[str, str] = {}
    if actor_uuids:
        rows = await db.execute(select(User.id, User.email).where(User.id.in_(actor_uuids)))
        for uid, email in rows:
            actor_emails[str(uid)] = email

    # Build CSV in memory — audit logs are bounded in size
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Event ID",
            "Timestamp (UTC)",
            "Category",
            "Event Type",
            "Actor",
            "Segment / Context",
            "AI Confidence",
            "Risk Level",
            "Decision / Outcome",
            "Regulatory Reference",
            "IP Address",
            "Notes",
        ]
    )

    for e in events:
        etype = canonical_event_type(e.event_type)
        category, label, reg_ref = _EVENT_META.get(etype, ("Unknown", etype, "—"))
        actor_email = actor_emails.get(str(e.actor_id), "")
        writer.writerow([_csv_safe(cell) for cell in _derive_csv_row(e, actor_email, category, label, reg_ref)])

    csv_bytes = output.getvalue().encode("utf-8-sig")  # utf-8-sig adds BOM for Excel compatibility

    role_label = "operational" if user.role == "ENGINEER" else "full"
    filename = f"pipelinegpt_audit_{role_label}_{user.email.split('@')[0]}.csv"

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
