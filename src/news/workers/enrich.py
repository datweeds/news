"""Enrichment worker: sends batches of relevant items to the LLM while the
switch and the daily budget allow; otherwise waits."""

from __future__ import annotations

import asyncio
import logging

from news.config import LlmSettings, ScoringSettings
from news.db.engine import sessionmaker
from news.llm.budget import gate
from news.pipeline.enrich import enrich_batch
from news.settings_store import get_settings

log = logging.getLogger(__name__)


async def run() -> None:
    log.info("enrich worker started")
    last_reason = ""
    while True:
        wait = 60
        try:
            async with sessionmaker()() as session:
                llm = await get_settings(session, LlmSettings)
                scoring = await get_settings(session, ScoringSettings)
                g = await gate(session, llm)
                if not g.allowed:
                    if g.reason != last_reason:
                        log.info("enrichment paused", extra={"reason": g.reason})
                        last_reason = g.reason
                else:
                    last_reason = ""
                    done = await enrich_batch(session, llm, scoring)
                    if done:
                        log.info("items enriched", extra={"items": done})
                        wait = 2
        except Exception:
            log.exception("enrich worker error")
        await asyncio.sleep(wait)
