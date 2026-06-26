from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AuditEvent


async def log_event(
    db: AsyncSession,
    event_type: str,
    actor_id: str | None = None,
    target_id: str | None = None,
    target_type: str | None = None,
    payload: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditEvent:
    """Append an immutable audit event. Never updates or deletes existing records."""
    event = AuditEvent(
        id=uuid.uuid4(),
        event_type=event_type,
        actor_id=actor_id,
        target_id=target_id,
        target_type=target_type,
        payload_json=payload,
        ip_address=ip_address,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event
