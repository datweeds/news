import uuid
from pathlib import Path

import httpx
import pytest
import respx

from news.connectors.context import ConnectorContext, CursorStore
from news.connectors.http import BlockedUrl, RobotsDisallowed, SafeHttp, check_url
from news.connectors.rss import RssConfig, RssConnector
from tests.conftest import public_resolver

FEED = (Path(__file__).parent / "fixtures" / "feed.xml").read_bytes()
FEED_URL = "https://news.example.com/rss.xml"


def connector(client: httpx.AsyncClient, cursor: CursorStore | None = None, **cfg: object) -> RssConnector:
    http = SafeHttp(client, "NewsBot/test", resolver=public_resolver, retries=2)
    ctx = ConnectorContext(source_id=uuid.uuid4(), source_name="test", http=http, cursor=cursor or CursorStore())
    return RssConnector(ctx, RssConfig.model_validate({"url": FEED_URL, **cfg}))


async def test_check_url_blocks_private_addresses() -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    with pytest.raises(BlockedUrl):
        await check_url("http://127.0.0.1/admin")
    with pytest.raises(BlockedUrl):
        await check_url("http://[::1]/")
    with pytest.raises(BlockedUrl):
        await check_url("http://intranet.example/", resolver=private)
    with pytest.raises(BlockedUrl):
        await check_url("file:///etc/passwd")
    await check_url("https://news.example.com/", resolver=public_resolver)


@respx.mock
async def test_rss_fetch_parses_and_uses_etag() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    route = respx.get(FEED_URL)
    route.side_effect = [
        httpx.Response(200, content=FEED, headers={"ETag": '"v1"'}),
        httpx.Response(304),
    ]
    cursor = CursorStore()
    async with httpx.AsyncClient() as client:
        conn = connector(client, cursor)
        items = [i async for i in conn.fetch()]
        assert len(items) == 3
        first = items[0]
        assert first.title.startswith("Scottish Government announces")
        assert first.external_id == "https://news.example.com/2026/11/heat-pump-grants"
        assert first.lang == "en-gb" and first.kind == "news"
        assert first.published_at is not None and first.published_at.year == 2026
        assert cursor.get("etag") == '"v1"' and cursor.dirty

        again = [i async for i in conn.fetch()]
        assert again == []
        assert route.calls[1].request.headers["If-None-Match"] == '"v1"'


@respx.mock
async def test_robots_disallow_is_respected() -> None:
    respx.get("https://news.example.com/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    async with httpx.AsyncClient() as client:
        with pytest.raises(RobotsDisallowed):
            [i async for i in connector(client).fetch()]


@respx.mock
async def test_redirect_to_private_address_is_blocked() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get(FEED_URL).respond(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(BlockedUrl):
            [i async for i in connector(client).fetch()]


@respx.mock
async def test_server_errors_are_retried() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get(FEED_URL).side_effect = [httpx.Response(503), httpx.Response(200, content=FEED)]
    async with httpx.AsyncClient() as client:
        items = [i async for i in connector(client).fetch()]
    assert len(items) == 3


@respx.mock
async def test_substack_platform_is_carried() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get(FEED_URL).respond(200, content=FEED)
    async with httpx.AsyncClient() as client:
        items = [i async for i in connector(client, platform="substack").fetch()]
    assert {i.platform for i in items} == {"substack"}
