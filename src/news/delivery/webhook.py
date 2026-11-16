"""Webhook delivery: POST JSON signed with HMAC-SHA256. Headers:
X-News-Signature: t=<unix>,v1=<hex hmac of "<t>.<body>">, X-News-Event-Id,
Idempotency-Key (the digest id). Receivers should reject timestamps older
than 5 minutes. Failures retry with backoff for 24 hours, then the delivery
is marked dead and an alert logged."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from news.connectors.http import FetchError, Resolver, check_url, system_resolver
from news.db.models import Delivery, Digest, Subscription
from news.env import env

log = logging.getLogger(__name__)
BACKOFF_S = (60, 300, 1800, 7200, 21600)
GIVE_UP_AFTER = timedelta(hours=24)


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


async def attempt(session: AsyncSession, delivery: Delivery, client: httpx.AsyncClient,
                  resolver: Resolver = system_resolver) -> bool:
    digest = await session.get(Digest, delivery.digest_id)
    sub = await session.get(Subscription, digest.subscription_id) if digest else None
    if digest is None or sub is None or not sub.webhook_url or not sub.webhook_secret:
        delivery.status, delivery.last_error = "dead", "Subscription has no webhook"
        await session.commit()
        return False
    body = orjson.dumps({"event": "digest.ready", "digest": digest.payload})
    now = datetime.now(UTC)
    delivery.attempt += 1
    try:
        await check_url(sub.webhook_url, resolver)
        resp = await client.post(sub.webhook_url, content=body, timeout=15, follow_redirects=False, headers={
            "Content-Type": "application/json",
            "User-Agent": env().user_agent,
            "X-News-Signature": sign(sub.webhook_secret, body),
            "X-News-Event-Id": str(delivery.id),
            "Idempotency-Key": str(digest.id),
        })
        delivery.response_code = resp.status_code
        ok = 200 <= resp.status_code < 300
        delivery.last_error = None if ok else f"HTTP {resp.status_code}"
    except (httpx.HTTPError, FetchError) as e:
        ok = False
        delivery.last_error = f"{type(e).__name__}: {e}"[:500]
    if ok:
        delivery.status, delivery.next_retry_at = "sent", None
        digest.status, digest.sent_at = "sent", now
    elif now - delivery.created_at >= GIVE_UP_AFTER:
        delivery.status, delivery.next_retry_at = "dead", None
        digest.status = "failed"
        log.error("ALERT webhook delivery failed for 24 hours",
                  extra={"alert": True, "digest": str(digest.id), "error": delivery.last_error})
    else:
        delivery.next_retry_at = now + timedelta(seconds=BACKOFF_S[min(delivery.attempt - 1, len(BACKOFF_S) - 1)])
    await session.commit()
    return ok


async def deliver_due(session: AsyncSession, client: httpx.AsyncClient) -> int:
    due = (await session.scalars(
        select(Delivery).where(Delivery.status == "pending", Delivery.next_retry_at <= datetime.now(UTC))
        .order_by(Delivery.next_retry_at).limit(20)
    )).all()
    for d in due:
        await attempt(session, d, client)
    return len(due)
