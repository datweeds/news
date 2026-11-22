"""Research papers: the OpenAlex connector, and the research part of editions."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from sqlalchemy import select, text

from news.config import AppSettings
from news.connectors.base import RawItem
from news.connectors.context import ConnectorContext, CursorStore
from news.connectors.http import SafeHttp
from news.connectors.openalex import OpenAlexConfig, OpenAlexConnector, abstract_text
from news.db.engine import sessionmaker
from news.db.models import SourceInstance, Subscription
from news.delivery.papers import opening
from news.delivery.produce import produce
from news.pipeline.store import store_raw
from news.settings_store import get_settings
from tests.conftest import public_resolver
from tests.test_delivery import client_api  # noqa: F401  (fixture)
from tests.test_stories import cluster_of, process

API = "https://api.openalex.org/works"


def work(n: int, title: str, abstract: str, preprint: bool = False, days_old: int = 10) -> dict[str, object]:
    words: dict[str, list[int]] = {}
    for i, w in enumerate(abstract.split()):
        words.setdefault(w, []).append(i)
    return {
        "id": f"https://openalex.org/W{n}", "display_name": title, "doi": f"https://doi.org/10.1/x{n}",
        "publication_date": (datetime.now(UTC) - timedelta(days=days_old)).date().isoformat(),
        "type": "preprint" if preprint else "article", "language": "en", "cited_by_count": n,
        "abstract_inverted_index": words,
        "authorships": [{"author": {"display_name": f"Author {i}"}} for i in range(5)],
        "primary_location": {"landing_page_url": f"https://journal.example/{n}",
                             "source": {"display_name": "bioRxiv" if preprint else "Nature Energy",
                                        "type": "repository" if preprint else "journal"}},
        "open_access": {"is_oa": True, "oa_url": f"https://oa.example/{n}.pdf"},
        "relevance_score": 10.0,
    }


def connector(client: httpx.AsyncClient, phrases: list[str], cursor: CursorStore | None = None) -> OpenAlexConnector:
    http = SafeHttp(client, "NewsBot/test", resolver=public_resolver, retries=1)
    ctx = ConnectorContext(source_id=uuid.uuid4(), source_name="openalex", http=http,
                           cursor=cursor or CursorStore(), phrases=phrases)
    return OpenAlexConnector(ctx, OpenAlexConfig(mailto="contact@new.vote"))


def test_abstract_and_opening() -> None:
    assert abstract_text({"heat": [0, 3], "pumps": [1], "save": [2]}) == "heat pumps save heat"
    assert abstract_text(None) == ""
    long = "First sentence about heat pumps in rural homes and how they work. " * 10
    assert opening(long).endswith(".") and len(opening(long)) <= 300


@respx.mock
async def test_search_maps_works_and_searches_each_phrase_once_a_day() -> None:
    respx.get("https://api.openalex.org/robots.txt").respond(404)
    calls: list[dict[str, list[str]]] = []

    def answer(req: httpx.Request) -> httpx.Response:
        calls.append(parse_qs(urlsplit(str(req.url)).query))
        return httpx.Response(200, json={"results": [
            work(1, "Heat pumps in cold climates", "We measured heat pump efficiency in rural homes"),
            work(2, "A preprint on heat pumps", "Early results on heat pumps", preprint=True)]})

    respx.get(url__startswith=API).mock(side_effect=answer)
    cursor = CursorStore()
    async with httpx.AsyncClient() as client:
        items = [i async for i in connector(client, ["heat pumps", "Heat  Pumps", "trams"], cursor).fetch()]
        # Same phrases again within a day: not searched again
        again = [i async for i in connector(client, ["heat pumps", "trams"], CursorStore(cursor.data)).fetch()]
    assert len(calls) == 2 and not again  # duplicate phrase (case, spaces) searched once
    q = calls[0]
    assert q["search"] == ["heat pumps"] and q["mailto"] == ["contact@new.vote"]
    assert "from_publication_date:" in q["filter"][0] and "has_abstract:true" in q["filter"][0]
    paper, preprint = items[0], items[1]
    assert paper.kind == "paper" and preprint.kind == "preprint"
    assert paper.url == "https://oa.example/1.pdf" and paper.author == "Author 0, Author 1, Author 2 et al."
    assert paper.text.startswith("We measured heat pump") and paper.raw["found_by"] == "heat pumps"
    assert paper.raw["venue"] == "Nature Energy" and preprint.raw["outlet"] == "bioRxiv"


async def add_paper(title: str, abstract: str, days_old: int = 10, kind: str = "paper",
                    found_by: str = "") -> uuid.UUID:
    async with sessionmaker()() as s:
        src_id = await s.scalar(select(SourceInstance.id).where(SourceInstance.name == "test-news"))
        cfg = await get_settings(s, AppSettings)
        wid = uuid.uuid4().hex
        item = await store_raw(s, RawItem(
            source_instance=str(src_id), external_id=f"W{wid}", url=f"https://oa.example/{wid}.pdf",
            title=title, text=abstract, kind=kind, published_at=datetime.now(UTC) - timedelta(days=days_old),
            raw={"openalex_id": "https://openalex.org/W1", "abstract": abstract, "venue": "Nature Energy",
                 "authors": ["A. Researcher"], "found_by": found_by, "cited_by": 3, "open_access": True}), cfg)
        assert item is not None
        return item.id


async def test_editions_carry_matching_recent_papers_once(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:  # noqa: F811
    api, auth = client_api
    match = await add_paper("Heat pump grants for rural homes cut bills",
                            "Rural homes with heat pump grants saw lower heating bills", found_by="heat pump grants for rural homes")
    preprint = await add_paper("Heat pump grants for rural homes: early evidence",
                               "Heat pump grants for rural homes in a pilot", kind="preprint")
    old = await add_paper("Heat pump grants for rural homes, a decade on",
                          "Heat pump grants for rural homes long term", days_old=120)
    await add_paper("Deep sea octopus behaviour", "Octopuses at depth were observed hunting")
    await process()
    assert await cluster_of(match) is None  # research papers are not news stories

    created = await api.post("/v1/subscriptions", headers=auth, json={
        "external_ref": "society:9", "definition": {
            "name": "Test Society Daily News", "min_score": 0, "include_papers": True,
            "sections": [{"key": "energy", "heading": "Energy", "phrases": ["heat pump grants for rural homes"]}]}})
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]
    assert (await api.post(f"/v1/subscriptions/{sub_id}/read", headers=auth)).status_code == 204

    preview = (await api.post(f"/v1/subscriptions/{sub_id}/preview", headers=auth)).json()
    assert {p["item_id"] for p in preview["papers"]} == {str(match), str(preprint)}

    digest = (await api.post(f"/v1/subscriptions/{sub_id}/digests", headers=auth)).json()
    papers = digest["papers"]
    assert {p["item_id"] for p in papers} == {str(match), str(preprint)}  # not the old one, not the octopus
    assert str(old) not in {p["item_id"] for p in papers}
    assert all(p["summary"] and p["section_heading"] == "Energy" for p in papers)
    assert next(p for p in papers if p["item_id"] == str(preprint))["preprint"] is True

    # The same papers are not sent again, and with nothing new there is no edition
    async with sessionmaker()() as s:
        row = await s.get(Subscription, sub_id)
        assert row is not None
        assert await produce(s, row) is None
        sent = await s.scalar(text("SELECT count(*) FROM subscription_papers WHERE subscription_id = :s"), {"s": sub_id})
    assert sent == 2


async def test_papers_off_by_default(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:  # noqa: F811
    api, auth = client_api
    await add_paper("Heat pump grants for rural homes cut bills", "Rural homes with heat pump grants")
    await process()
    resp = await api.post("/v1/subscriptions/preview", headers=auth, json={"definition": {
        "name": "T", "min_score": 0,
        "sections": [{"key": "energy", "heading": "Energy", "phrases": ["heat pump grants for rural homes"]}]}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["papers"] == []
    json.dumps(resp.json())
