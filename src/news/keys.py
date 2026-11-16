"""API keys. Only a SHA-256 hash is stored; the key is shown once."""

from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from news.db.models import ApiKey

ROLES = ("admin", "moderator", "client", "viewer")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_key(session: AsyncSession, name: str, role: str,
                     client_id: uuid.UUID | None = None) -> tuple[ApiKey, str]:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    key = "nws_" + secrets.token_urlsafe(32)
    row = ApiKey(name=name, role=role, client_id=client_id, prefix=key[:10], key_hash=hash_key(key))
    session.add(row)
    await session.flush()
    return row, key
