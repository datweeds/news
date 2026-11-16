"""Delivery worker: produces each subscription's digest when it is due, and
sends (and retries) webhook deliveries."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from news.db.engine import sessionmaker
from news.db.models import Subscription
from news.delivery.models import ScheduleDef
from news.delivery.produce import produce
from news.delivery.schedule import next_run
from news.delivery.webhook import deliver_due

log = logging.getLogger(__name__)


async def produce_due(now: datetime) -> int:
    async with sessionmaker()() as session:
        due = (await session.scalars(
            select(Subscription.id).where(Subscription.active, Subscription.next_digest_at <= now)
        )).all()
    for sub_id in due:
        async with sessionmaker()() as session:
            sub = await session.get(Subscription, sub_id, with_for_update=True)
            if sub is None or sub.next_digest_at is None or sub.next_digest_at > now:
                continue
            # Move the schedule on first, so a failure doesn't retry every tick
            sub.next_digest_at = next_run(ScheduleDef.model_validate(sub.schedule), now)
            sub.last_digest_at = now
            await session.commit()
            try:
                await produce(session, sub, now)
            except Exception:
                log.exception("digest failed", extra={"subscription": str(sub_id)})
    return len(due)


async def run() -> None:
    log.info("deliver worker started")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await produce_due(datetime.now(UTC))
                async with sessionmaker()() as session:
                    await deliver_due(session, client)
            except Exception:
                log.exception("deliver worker error")
            await asyncio.sleep(30)
