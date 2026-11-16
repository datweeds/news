"""Stage 2: stories, scoring, enrichment and the LLM switch and budget."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from news.config import AppSettings, LlmSettings, ScoringSettings
from news.connectors.base import RawItem
from news.db.engine import sessionmaker
from news.db.models import Cluster, ClusterItem, Item, LlmUsage, SourceInstance
from news.llm.budget import gate, run_task
from news.pipeline.enrich import enrich_batch
from news.pipeline.score import score_clusters
from news.pipeline.stories import cluster_pending, embed_pending
from news.pipeline.store import store_raw
from news.settings_store import get_settings, invalidate

HEAT = ("Scottish Government announces new heat pump grants for rural homes",
        "Households in rural Scotland can claim up to nine thousand pounds towards a heat pump.")
HEAT2 = ("Scottish Government announces heat pump grants for rural homes",
         "Households in rural Scotland can claim up to nine thousand pounds towards a new heat pump.")
TRAMS = ("Edinburgh trams extension approved by council",
         "The council approved plans to extend the tram line north to Granton by 2028.")


async def add_item(title: str, snippet: str, source: str = "test-news", kind: str = "news",
                   url: str | None = None) -> uuid.UUID:
    async with sessionmaker()() as s:
        src_id = await s.scalar(select(SourceInstance.id).where(SourceInstance.name == source))
        cfg = await get_settings(s, AppSettings)
        item = await store_raw(s, RawItem(source_instance=str(src_id), external_id=str(uuid.uuid4()), url=url,
                                          title=title, text=snippet, kind=kind, published_at=datetime.now(UTC)), cfg)
        assert item is not None
        return item.id


async def process() -> None:
    async with sessionmaker()() as s:
        cfg = await get_settings(s, ScoringSettings)
        while await embed_pending(s):
            pass
        while await cluster_pending(s, cfg):
            pass
        await score_clusters(s, cfg)


async def cluster_of(item_id: uuid.UUID) -> uuid.UUID | None:
    async with sessionmaker()() as s:
        return await s.scalar(select(ClusterItem.cluster_id).where(ClusterItem.item_id == item_id))


async def set_llm(**changes: object) -> None:
    async with sessionmaker()() as s:
        current = (await get_settings(s, LlmSettings)).model_dump()
        await s.execute(text("UPDATE settings SET value = CAST(:v AS jsonb) WHERE name = 'llm'"),
                        {"v": json.dumps({**current, **changes})})
        await s.commit()
    invalidate()


async def test_similar_items_form_one_story(seeded: None) -> None:
    a = await add_item(*HEAT)
    b = await add_item(*HEAT2, source="test-substack")
    c = await add_item(*TRAMS)
    await process()
    assert await cluster_of(a) == await cluster_of(b) is not None
    assert await cluster_of(c) not in (None, await cluster_of(a))
    async with sessionmaker()() as s:
        story = await s.get(Cluster, await cluster_of(a))
        assert story is not None and story.item_count == 2 and story.source_count == 2
        assert 0 < story.score <= 100
        assert {"significance", "corroboration", "velocity", "decay"} <= set(story.score_breakdown)
        single = await s.get(Cluster, await cluster_of(c))
        assert single is not None and story.score > single.score  # corroborated beats single-source


async def test_duplicates_join_their_originals_story(seeded: None) -> None:
    a = await add_item(*HEAT, url="https://news.example.com/a")
    dup = await add_item("Different words entirely", "But the same article URL.", source="test-substack",
                         url="https://www.news.example.com/a?utm_source=x")
    await process()
    assert await cluster_of(dup) == await cluster_of(a)
    async with sessionmaker()() as s:
        item = await s.get(Item, dup)
        assert item is not None and item.duplicate_of == a and item.embedding is None  # never embedded or enriched


async def test_llm_switch_and_budget(seeded: None) -> None:
    async with sessionmaker()() as s:
        llm = await get_settings(s, LlmSettings)
        assert not (await gate(s, llm)).allowed  # auto, and nobody is reading
        assert await run_task(s, llm, "extract", "sys", "hello") is None
        off = llm.model_copy(update={"mode": "off"})
        assert (await gate(s, off)).reason == "LLM is switched off"
        on = llm.model_copy(update={"mode": "on"})
        assert (await gate(s, on)).allowed
        assert await run_task(s, on, "extract", "sys", "hello") is not None
        spent = await s.scalar(select(func.sum(LlmUsage.cost_usd)))
        assert spent is not None and spent > 0
        broke = on.model_copy(update={"daily_budget_usd": float(spent)})
        g = await gate(s, broke, estimate_usd=0.001)
        assert not g.allowed and "budget" in g.reason


async def test_enrichment_with_fake_llm(seeded: None) -> None:
    await set_llm(mode="on")
    ids = [await add_item(*HEAT), await add_item(*TRAMS)]
    await process()
    async with sessionmaker()() as s:
        llm = await get_settings(s, LlmSettings)
        scoring = await get_settings(s, ScoringSettings)
        assert await enrich_batch(s, llm, scoring) == 2
        assert await enrich_batch(s, llm, scoring) == 0  # each item once
        item = await s.get(Item, ids[0])
        assert item is not None and item.significance == pytest.approx(0.7) and item.prompt_version == "extract-v2"
        loc = (await s.execute(text("SELECT name, country, role FROM item_locations WHERE item_id = :i"),
                               {"i": ids[0]})).one()
        assert (loc.name, loc.country, loc.role) == ("Scotland", "GB", "primary")
        assert await s.scalar(text("SELECT count(*) FROM item_topics WHERE item_id = :i"), {"i": ids[0]}) == 1


async def test_enrichment_waits_when_switched_off(seeded: None) -> None:
    await set_llm(mode="off")
    await add_item(*HEAT)
    await process()
    async with sessionmaker()() as s:
        assert await enrich_batch(s, await get_settings(s, LlmSettings), await get_settings(s, ScoringSettings)) is None


async def test_old_backlog_is_skipped(seeded: None) -> None:
    await set_llm(mode="on", backlog_max_age_hours=1)
    item_id = await add_item(*HEAT)
    async with sessionmaker()() as s:
        await s.execute(text("UPDATE items SET ingested_at = :t WHERE id = :i"),
                        {"t": datetime.now(UTC) - timedelta(hours=3), "i": item_id})
        await s.commit()
    await process()
    async with sessionmaker()() as s:
        assert await enrich_batch(s, await get_settings(s, LlmSettings), await get_settings(s, ScoringSettings)) == 0
