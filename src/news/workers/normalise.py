"""Normaliser worker: RawItems from the stream → deduplicated items rows."""

from __future__ import annotations

import logging
import socket
from typing import Any

from news.config import AppSettings
from news.connectors.base import RawItem
from news.db.engine import sessionmaker
from news.pipeline.store import store_raw
from news.settings_store import get_settings
from news.streams import RAW, consume_forever, redis_client

log = logging.getLogger(__name__)
GROUP = "normalise"


async def handle_raw(payload: dict[str, Any]) -> None:
    raw = RawItem.model_validate(payload)
    async with sessionmaker()() as session:
        cfg = await get_settings(session, AppSettings)
        item = await store_raw(session, raw, cfg)
    if item is not None:
        log.debug("stored item", extra={"item_id": str(item.id), "duplicate_of": str(item.duplicate_of)})


async def run() -> None:
    consumer = f"{socket.gethostname()}"
    log.info("normaliser started", extra={"consumer": consumer})
    await consume_forever(redis_client(), RAW, GROUP, consumer, handle_raw)
