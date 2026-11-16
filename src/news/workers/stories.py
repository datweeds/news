"""Stories worker: embeds new items (locally, free), groups them into
stories, scores stories, and periodically merges near-identical stories and
ages quiet ones."""

from __future__ import annotations

import asyncio
import logging
import time

from news.config import ScoringSettings
from news.db.engine import sessionmaker
from news.pipeline.score import score_clusters
from news.pipeline.stories import age, cluster_pending, embed_pending, embed_topics, merge_similar
from news.settings_store import get_settings

log = logging.getLogger(__name__)
SCORE_EVERY_S = 300
MERGE_EVERY_S = 6 * 3600


async def run() -> None:
    log.info("stories worker started")
    last_score = last_merge = 0.0
    while True:
        busy = False
        try:
            async with sessionmaker()() as session:
                cfg = await get_settings(session, ScoringSettings)
                await embed_topics(session)
                embedded = await embed_pending(session)
                clustered = await cluster_pending(session, cfg)
                busy = bool(embedded or clustered)
                if clustered:
                    await score_clusters(session, cfg, all_active=False)
                if time.monotonic() - last_merge > MERGE_EVERY_S:
                    merged = await merge_similar(session, cfg)
                    await age(session, cfg)
                    last_merge = time.monotonic()
                    log.info("stories merged and aged", extra={"merged": merged})
                if time.monotonic() - last_score > SCORE_EVERY_S:
                    n = await score_clusters(session, cfg, all_active=True)
                    last_score = time.monotonic()
                    log.info("stories scored", extra={"stories": n})
                if busy:
                    log.info("items processed", extra={"embedded": embedded, "clustered": clustered})
        except Exception:
            log.exception("stories worker error")
        await asyncio.sleep(1 if busy else 15)
