"""Stage 1 'done when': RSS items are ingested, deduplicated and visible."""

from pathlib import Path

import httpx
import respx
from sqlalchemy import func, select

from news.config import SourceConfig, source_fields
from news.connectors.http import SafeHttp
from news.db.engine import sessionmaker
from news.db.models import Item, Setting, SourceInstance, Topic
from news.streams import RAW, consume_once, ensure_group, redis_client
from news.workers.ingest import run_source
from news.workers.normalise import GROUP, handle_raw
from tests.conftest import public_resolver

FIXTURES = Path(__file__).parent / "fixtures"


async def source_id(name: str) -> object:
    async with sessionmaker()() as s:
        return await s.scalar(select(SourceInstance.id).where(SourceInstance.name == name))


async def drain() -> int:
    r = redis_client()
    await ensure_group(r, RAW, GROUP)
    total = 0
    while handled := await consume_once(r, RAW, GROUP, "test", handle_raw, block_ms=100):
        total += handled
    await r.aclose()
    return total


async def test_seed_loads_sources_settings_topics(seeded: None) -> None:
    async with sessionmaker()() as s:
        names = set((await s.scalars(select(SourceInstance.name))).all())
        assert names == {"test-news", "test-substack", "manual"}
        llm = await s.get(Setting, "llm")
        assert llm is not None and llm.value["mode"] == "auto" and llm.value["daily_budget_usd"] == 1.0
        child = await s.scalar(select(Topic).where(Topic.slug == "heat-pumps"))
        parent = await s.scalar(select(Topic).where(Topic.slug == "energy"))
        assert child is not None and parent is not None and child.parent_id == parent.id


@respx.mock
async def test_ingest_normalise_dedup(seeded: None) -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get("https://other.example.org/robots.txt").respond(404)
    respx.get("https://news.example.com/rss.xml").respond(200, content=(FIXTURES / "feed.xml").read_bytes(),
                                                          headers={"ETag": '"a"'})
    respx.get("https://other.example.org/rss.xml").respond(
        200, content=(FIXTURES / "feed_other_outlet.xml").read_bytes())

    async with sessionmaker()() as s:
        s.add(SourceInstance(name="other", **source_fields(SourceConfig(
            name="other", type="rss", config={"url": "https://other.example.org/rss.xml"}))))
        await s.commit()

    r = redis_client()
    async with httpx.AsyncClient() as client:
        http = SafeHttp(client, "NewsBot/test", resolver=public_resolver)
        assert await run_source(await source_id("test-news"), http, r) == 3  # type: ignore[arg-type]
        assert await drain() == 3
        assert await run_source(await source_id("other"), http, r) == 2  # type: ignore[arg-type]
        assert await drain() == 2
    await r.aclose()

    async with sessionmaker()() as s:
        items = (await s.scalars(select(Item).order_by(Item.ingested_at))).all()
        assert len(items) == 5
        by_title = {i.title: i for i in items}
        original = next(i for i in items if i.external_id.endswith("heat-pump-grants"))
        assert original.duplicate_of is None
        assert original.canonical_url == "https://news.example.com/2026/11/heat-pump-grants"
        assert original.text_snippet.startswith("Households in rural Scotland") and "<p>" not in original.text_snippet
        assert original.lang == "en"
        # Same text from another outlet: near-duplicate
        copy = next(i for i in items if i.external_id == "other-1")
        assert copy.duplicate_of == original.id
        # Different text, same article URL once tracking and www are removed
        syndicated = by_title["Same story, syndicated link"]
        trams = by_title["Edinburgh trams extension approved by council"]
        assert syndicated.duplicate_of == trams.id

        src = await s.scalar(select(SourceInstance).where(SourceInstance.name == "test-news"))
        assert src is not None
        assert src.cursor.get("etag") == '"a"'
        assert src.health["status"] == "ok" and src.health["last_count"] == 3


@respx.mock
async def test_redelivery_is_idempotent(seeded: None) -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get("https://news.example.com/rss.xml").respond(200, content=(FIXTURES / "feed.xml").read_bytes())
    r = redis_client()
    sid = await source_id("test-news")
    async with httpx.AsyncClient() as client:
        http = SafeHttp(client, "NewsBot/test", resolver=public_resolver)
        await run_source(sid, http, r)  # type: ignore[arg-type]
        await run_source(sid, http, r)  # type: ignore[arg-type]
    await r.aclose()
    assert await drain() == 6
    async with sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(Item)) == 3


@respx.mock
async def test_failing_source_backs_off(seeded: None) -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get("https://news.example.com/rss.xml").respond(200, text="<html>not a feed")
    r = redis_client()
    sid = await source_id("test-news")
    async with httpx.AsyncClient() as client:
        http = SafeHttp(client, "NewsBot/test", resolver=public_resolver)
        assert await run_source(sid, http, r) == 0  # type: ignore[arg-type]
    await r.aclose()
    async with sessionmaker()() as s:
        src = await s.get(SourceInstance, sid)
        assert src is not None and src.enabled
        assert src.health["status"] == "failing"
        assert src.health["consecutive_failures"] == 1
        assert "retry_at" in src.health
