"""Redis Streams between pipeline stages. Each stage is a consumer group, so
workers scale horizontally and a crashed worker's messages are reclaimed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from news.env import env

RAW = "news:raw"  # RawItems from connectors, consumed by the normaliser
DEAD = "news:dead"  # messages that failed MAX_ATTEMPTS times

MAX_ATTEMPTS = 5
RECLAIM_IDLE_MS = 60_000

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def redis_client(url: str | None = None) -> Redis:
    # Longer than the blocking stream reads below
    return Redis.from_url(url or env().redis_url, socket_timeout=30, socket_connect_timeout=10,
                          health_check_interval=30)


async def publish(r: Redis, stream: str, payload: dict[str, Any], maxlen: int = 20_000) -> None:
    await r.xadd(stream, {"d": orjson.dumps(payload)}, maxlen=maxlen, approximate=True)


async def ensure_group(r: Redis, stream: str, group: str) -> None:
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _handle(r: Redis, stream: str, group: str, msg_id: bytes,
                  fields: dict[bytes, bytes], handler: Handler) -> None:
    try:
        await handler(orjson.loads(fields[b"d"]))
    except Exception:
        attempts_key = f"{stream}:attempts:{group}"
        attempts = await r.hincrby(attempts_key, msg_id.decode(), 1)
        log.exception("stream handler failed", extra={"stream": stream, "msg_id": msg_id.decode(), "attempts": attempts})
        if attempts < MAX_ATTEMPTS:
            return  # stays pending; reclaimed after RECLAIM_IDLE_MS
        await r.xadd(DEAD, {"stream": stream, "group": group, "d": fields[b"d"]}, maxlen=10_000, approximate=True)
        await r.hdel(attempts_key, msg_id.decode())
    await r.xack(stream, group, msg_id)


async def consume_once(r: Redis, stream: str, group: str, consumer: str,
                       handler: Handler, count: int = 50, block_ms: int = 5000) -> int:
    """Handle one batch: reclaimed stale messages first, then new ones.
    Returns how many messages were handled."""
    handled = 0
    reclaimed: Any = await r.xautoclaim(stream, group, consumer, RECLAIM_IDLE_MS, "0-0", count=count)
    for msg_id, fields in reclaimed[1]:
        if fields:
            await _handle(r, stream, group, msg_id, fields, handler)
            handled += 1
    resp: Any = await r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
    for _stream, messages in resp or []:
        for msg_id, fields in messages:
            await _handle(r, stream, group, msg_id, fields, handler)
            handled += 1
    return handled


async def consume_forever(r: Redis, stream: str, group: str, consumer: str, handler: Handler) -> None:
    await ensure_group(r, stream, group)
    while True:
        try:
            await consume_once(r, stream, group, consumer, handler)
        except (RedisConnectionError, RedisTimeoutError, OSError):
            log.exception("redis connection problem; retrying")
            await asyncio.sleep(5)
