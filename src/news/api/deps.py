"""Request dependencies: database session, Redis, and API-key auth with
role checks and per-key rate limits."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from news.audit import Actor
from news.config import AppSettings
from news.connectors.http import SafeHttp
from news.db.engine import sessionmaker
from news.db.models import ApiKey
from news.keys import hash_key
from news.settings_store import get_settings

# Console roles, lowest to highest; each includes the ones below it.
# `client` is separate: client keys only use the client API.
ROLE_RANK = {"viewer": 1, "moderator": 2, "admin": 3}


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


def get_redis(request: Request) -> Redis:
    r: Redis = request.app.state.redis
    return r


def get_http(request: Request) -> SafeHttp:
    http: SafeHttp = request.app.state.http
    return http


Session = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
HttpDep = Annotated[SafeHttp, Depends(get_http)]


async def current_actor(request: Request, session: Session, r: RedisDep) -> Actor:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key",
                            headers={"WWW-Authenticate": "Bearer"})
    key = await session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_key(token.strip()), ApiKey.revoked_at.is_(None))
    )
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key",
                            headers={"WWW-Authenticate": "Bearer"})

    cfg = await get_settings(session, AppSettings)
    bucket = f"news:rl:{key.id}:{int(time.time() // 60)}"
    used = await r.incr(bucket)
    if used == 1:
        await r.expire(bucket, 90)
    if used > cfg.api_rate_limit_per_min:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded",
                            headers={"Retry-After": "60"})

    now = datetime.now(UTC)
    if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=5):
        key.last_used_at = now
        await session.commit()
    return Actor(name=key.name, role=key.role, key_id=key.id, client_id=key.client_id)


def require(min_role: str) -> Callable[..., Awaitable[Actor]]:
    needed = ROLE_RANK[min_role]

    async def check(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
        if ROLE_RANK.get(actor.role, 0) < needed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Needs the {min_role} role")
        return actor

    return check


async def client_or_admin(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
    if actor.role not in ("client", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Needs a client or admin key")
    return actor


ClientActor = Annotated[Actor, Depends(client_or_admin)]
Viewer = Annotated[Actor, Depends(require("viewer"))]
Moderator = Annotated[Actor, Depends(require("moderator"))]
Admin = Annotated[Actor, Depends(require("admin"))]
