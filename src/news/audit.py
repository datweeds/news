"""Audit log: who did what, before and after, and when."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from news.db.models import AuditLog


@dataclass(frozen=True)
class Actor:
    name: str
    role: str
    key_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None


SYSTEM = Actor(name="system", role="admin")


def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: object | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """Add an audit entry to the session; it commits with the change itself."""
    session.add(AuditLog(
        actor=actor.name, actor_key_id=actor.key_id, action=action, target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        before=before, after=after, reason=reason,
    ))
