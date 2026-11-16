"""Ingest worker: runs each enabled polling source when it is due and puts
its RawItems on the stream. A failing source never blocks the others; it
backs off, and after `source_auto_disable_hours` of continuous failure it is
disabled and an alert is logged and audited."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from news.audit import SYSTEM, audit
from news.config import AppSettings
from news.connectors.http import SafeHttp
from news.connectors.registry import build_connector, connector_types
from news.db.engine import sessionmaker
from news.db.models import SourceInstance
from news.env import env
from news.settings_store import get_settings
from news.streams import RAW, publish, redis_client

log = logging.getLogger(__name__)
TICK_SECONDS = 15
MAX_BACKOFF = timedelta(hours=6)


def now() -> datetime:
    return datetime.now(UTC)


def poll_interval(src: SourceInstance, cfg: AppSettings) -> int:
    interval = max(src.poll_interval_s, cfg.min_poll_interval)
    if src.config.get("platform") == "substack":
        interval = max(interval, cfg.substack_min_poll_interval)
    return interval


def is_due(src: SourceInstance, cfg: AppSettings, at: datetime) -> bool:
    retry_at = src.health.get("retry_at")
    if retry_at and datetime.fromisoformat(retry_at) > at:
        return False
    if src.last_run_at is None:
        return True
    return src.last_run_at + timedelta(seconds=poll_interval(src, cfg)) <= at


async def run_source(source_id: uuid.UUID, http: SafeHttp, r: Redis) -> int:
    """Run one poll of a source. Returns the number of items published."""
    sm = sessionmaker()
    async with sm() as session:
        src = await session.get(SourceInstance, source_id)
        if src is None or not src.enabled:
            return 0
        cfg = await get_settings(session, AppSettings)
        lock_key = f"news:lock:source:{source_id}"
        if not await r.set(lock_key, "1", nx=True, ex=max(poll_interval(src, cfg), 300)):
            return 0  # another ingest worker has it
        count = 0
        started = now()
        try:
            connector, ctx = build_connector(src, http)
            async for item in connector.fetch():
                await publish(r, RAW, item.model_dump(mode="json"))
                count += 1
            src.health = {"status": "ok", "last_ok_at": started.isoformat(), "last_count": count}
            if ctx.cursor.dirty:
                src.cursor = dict(ctx.cursor.data)
            src.last_run_at = started
            await session.commit()
            log.info("source polled", extra={"source": src.name, "items": count})
        except Exception as e:
            await session.rollback()
            await _record_failure(session, source_id, e, cfg)
        finally:
            await r.delete(lock_key)
        return count


async def _record_failure(session: Any, source_id: uuid.UUID, error: Exception, cfg: AppSettings) -> None:
    src = await session.get(SourceInstance, source_id)
    if src is None:
        return
    at = now()
    failures = int(src.health.get("consecutive_failures", 0)) + 1
    failing_since = src.health.get("failing_since") or at.isoformat()
    backoff = min(timedelta(seconds=poll_interval(src, cfg)) * 2 ** (failures - 1), MAX_BACKOFF)
    src.health = {
        **src.health,
        "status": "failing",
        "last_error": f"{type(error).__name__}: {error}"[:500],
        "last_error_at": at.isoformat(),
        "consecutive_failures": failures,
        "failing_since": failing_since,
        "retry_at": (at + backoff).isoformat(),
    }
    src.last_run_at = at
    log.warning("source failed", extra={"source": src.name, "error": str(error)[:300], "failures": failures})
    if at - datetime.fromisoformat(failing_since) >= timedelta(hours=cfg.source_auto_disable_hours):
        src.enabled = False
        src.health = {**src.health, "status": "disabled", "disabled_at": at.isoformat()}
        audit(session, SYSTEM, "source.auto_disable", "source", src.id, after={"enabled": False},
              reason=f"Failing for {cfg.source_auto_disable_hours} h: {src.health['last_error']}")
        log.error("ALERT source auto-disabled", extra={"source": src.name, "alert": True})
    await session.commit()


async def run() -> None:
    r = redis_client()
    types = connector_types()
    running: dict[uuid.UUID, asyncio.Task[int]] = {}
    async with httpx.AsyncClient() as client:
        http = SafeHttp(client, env().user_agent)
        log.info("ingest started", extra={"connector_types": sorted(types)})
        while True:
            try:
                async with sessionmaker()() as session:
                    cfg = await get_settings(session, AppSettings)
                    sources = (await session.scalars(
                        select(SourceInstance).where(SourceInstance.enabled)
                    )).all()
                at = now()
                running = {k: t for k, t in running.items() if not t.done()}
                for src in sources:
                    cls = types.get(src.type)
                    if cls is None or cls.mode != "poll" or src.id in running:
                        continue
                    if len(running) >= cfg.ingest_concurrency:
                        break
                    if is_due(src, cfg, at):
                        running[src.id] = asyncio.create_task(run_source(src.id, http, r))
            except Exception:
                log.exception("ingest scheduler error")
            await asyncio.sleep(TICK_SECONDS)
