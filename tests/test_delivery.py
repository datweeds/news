"""Stage 3: subscriptions, previews, digests, places, webhooks, schedule."""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select, text

from news.api.app import create_app
from news.connectors.http import SafeHttp
from news.db.engine import sessionmaker
from news.db.models import Client, Cluster, Delivery, Subscription
from news.delivery.models import ScheduleDef
from news.delivery.produce import produce
from news.delivery.schedule import next_run
from news.delivery.webhook import attempt
from news.keys import create_key
from tests.conftest import public_resolver
from tests.test_stories import HEAT, HEAT2, TRAMS, add_item, cluster_of, process, set_llm

SECTIONS = [
    {"key": "energy", "heading": "Energy", "phrases": ["heat pump grants for rural homes"]},
    {"key": "transport", "heading": "Transport", "phrases": ["Edinburgh trams extension approved by council"]},
]


def definition(**extra: object) -> dict[str, object]:
    return {"name": "Test Society Daily News", "sections": SECTIONS, "min_score": 0, **extra}


@pytest.fixture
async def client_api(seeded: None) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    async with sessionmaker()() as s:
        c = Client(name="debate-report")
        s.add(c)
        await s.flush()
        _, key = await create_key(s, "dr", "client", c.id)
        await s.commit()
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient() as outbound:
            app.state.http = SafeHttp(outbound, "NewsBot/test", resolver=public_resolver)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                yield api, {"Authorization": f"Bearer {key}"}


async def stories() -> None:
    await add_item(*HEAT)
    await add_item(*HEAT2, source="test-substack")
    await add_item(*TRAMS)
    await process()


def test_next_run() -> None:
    daily = ScheduleDef(cadence="daily", time="07:00", tz="Europe/London")
    assert next_run(daily, datetime(2026, 11, 16, 6, 0, tzinfo=UTC)) == datetime(2026, 11, 16, 7, 0, tzinfo=UTC)
    assert next_run(daily, datetime(2026, 11, 16, 7, 0, tzinfo=UTC)) == datetime(2026, 11, 17, 7, 0, tzinfo=UTC)
    summer = next_run(daily, datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
    assert summer == datetime(2026, 7, 2, 6, 0, tzinfo=UTC)  # 07:00 BST
    weekly = ScheduleDef(cadence="weekly", time="08:30", weekday=0, tz="Europe/London")
    assert next_run(weekly, datetime(2026, 11, 16, 9, 0, tzinfo=UTC)) == datetime(2026, 11, 23, 8, 30, tzinfo=UTC)


async def test_preview_sections_and_phrase_guidance(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await stories()
    defn = definition(sections=SECTIONS + [{"key": "misc", "heading": "Misc", "phrases": ["Scotland", "volcanoes"]}])
    resp = await api.post("/v1/subscriptions/preview", headers=auth, json={"definition": defn})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["headline"] is not None
    titles = [body["headline"]["title"]] + [i["title"] for s in body["sections"] for i in s["items"]]
    assert any("heat pump" in t for t in titles) and any("trams" in t for t in titles)
    heat = next(i for i in [body["headline"]] + [i for s in body["sections"] for i in s["items"]] if "heat" in i["title"])
    assert heat["section_key"] == "energy" and heat["source_count"] == 2 and heat["top_sources"]
    guidance = {p["phrase"]: p for p in body["phrases"]}
    assert any("vague" in w for w in guidance["Scotland"]["warnings"])
    assert guidance["volcanoes"]["matching_stories"] == 0
    assert guidance["heat pump grants for rural homes"]["matching_stories"] >= 1


async def test_subscription_digest_webhook_and_no_repeat(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await stories()
    created = await api.post("/v1/subscriptions", headers=auth, json={
        "external_ref": "society:1", "definition": definition(), "webhook_url": "https://dr.example.com/api/news/webhook"})
    assert created.status_code == 201, created.text
    sub = created.json()
    secret = sub["webhook_secret"]
    assert secret and sub["next_digest_at"]
    assert (await api.post("/v1/subscriptions", headers=auth, json={
        "external_ref": "society:1", "definition": definition()})).status_code == 409

    # Reading the subscription lets the LLM write digests in auto mode
    assert (await api.post(f"/v1/subscriptions/{sub['id']}/read", headers=auth)).status_code == 204
    resp = await api.post(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)
    assert resp.status_code == 201, resp.text
    digest = resp.json()
    assert digest["llm_written"] and digest["overview"]
    assert digest["items"][0]["is_headline"] and digest["items"][0]["why_it_matters"]
    assert {i["section_key"] for i in digest["items"]} == {"energy", "transport"}
    assert (await api.post(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)).status_code == 429

    received: list[httpx.Request] = []
    async with sessionmaker()() as s:
        delivery = await s.scalar(select(Delivery))
        assert delivery is not None
        with respx.mock:
            respx.post("https://dr.example.com/api/news/webhook").mock(
                side_effect=lambda req: received.append(req) or httpx.Response(200))
            async with httpx.AsyncClient() as client:
                assert await attempt(s, delivery, client, resolver=public_resolver)
        assert delivery.status == "sent"
    sig = dict(p.split("=", 1) for p in received[0].headers["X-News-Signature"].split(","))
    expected = hmac.new(secret.encode(), f"{sig['t']}.".encode() + received[0].content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig["v1"], expected)
    assert json.loads(received[0].content)["digest"]["id"] == digest["id"]

    # The same stories aren't sent again without a material update
    async with sessionmaker()() as s:
        row = await s.get(Subscription, sub["id"])
        assert row is not None
        assert await produce(s, row) is None

    listed = (await api.get(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)).json()
    assert [d["id"] for d in listed["digests"]] == [digest["id"]]


async def test_hide_and_headline_overrides(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await stories()
    sub = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition()})).json()
    preview = (await api.post(f"/v1/subscriptions/{sub['id']}/preview", headers=auth)).json()
    head = preview["headline"]["cluster_id"]
    other = next(i["cluster_id"] for s in preview["sections"] for i in s["items"])
    await api.post(f"/v1/subscriptions/{sub['id']}/overrides", headers=auth, json={"cluster_id": other, "action": "headline"})
    await api.post(f"/v1/subscriptions/{sub['id']}/overrides", headers=auth, json={"cluster_id": head, "action": "hide"})
    after = (await api.post(f"/v1/subscriptions/{sub['id']}/preview", headers=auth)).json()
    assert after["headline"]["cluster_id"] == other
    assert head not in [i["cluster_id"] for s in after["sections"] for i in s["items"]]
    assert any(m["reason"] == "Hidden by a moderator" for m in after["near_misses"])


async def test_location_filter_bristol_uk_not_connecticut(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    uk = await add_item("Bristol council approves new bus lanes on the A38", "The plans cover bus lanes in Bristol.")
    ct = await add_item("Bristol town council approves new bus lanes downtown", "Connecticut town plans bus lanes.")
    await process()
    async with sessionmaker()() as s:
        await s.execute(text("""
            INSERT INTO geonames (geonameid, name, ascii_name, country, admin1, feature_class, feature_code, population, geom)
            VALUES (2654675, 'Bristol', 'Bristol', 'GB', 'ENG', 'P', 'PPLA2', 430000, ST_MakePoint(-2.596, 51.455)::geography),
                   (4830198, 'Bristol', 'Bristol', 'US', 'CT', 'P', 'PPL', 60000, ST_MakePoint(-72.949, 41.672)::geography)
        """))
        for item_id, gid, country, admin1 in ((uk, 2654675, "GB", "ENG"), (ct, 4830198, "US", "CT")):
            await s.execute(text("""
                INSERT INTO item_locations (item_id, name, geonames_id, country, admin1, role, geom)
                VALUES (:i, 'Bristol', :g, :c, :a, 'primary', (SELECT geom FROM geonames WHERE geonameid = :g))
            """), {"i": item_id, "g": gid, "c": country, "a": admin1})
        await s.commit()
    places = (await api.get("/v1/locations/resolve", headers=auth, params={"q": "bristol"})).json()
    gb = next(p for p in places if p["country"] == "GB")
    defn = definition(sections=[{"key": "local", "heading": "Local", "phrases": ["council approves new bus lanes"]}],
                      locations=[{k: gb[k] for k in ("name", "kind", "geonames_id", "country", "admin1", "lat", "lon")}])
    body = (await api.post("/v1/subscriptions/preview", headers=auth, json={"definition": defn})).json()
    chosen = [body["headline"]] + [i for s in body["sections"] for i in s["items"]]
    ids = {i["cluster_id"] for i in chosen if i}
    assert str(await cluster_of(uk)) in ids
    assert str(await cluster_of(ct)) not in ids
    assert any(m["reason"] == "Not about the subscription's places" for m in body["near_misses"])


async def test_llm_off_digest_uses_outlet_headlines(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await set_llm(mode="off")
    await stories()
    sub = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition()})).json()
    await api.post(f"/v1/subscriptions/{sub['id']}/read", headers=auth)
    digest = (await api.post(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)).json()
    assert not digest["llm_written"] and digest["overview"] is None
    assert digest["items"] and all(i["summary"] for i in digest["items"])
    async with sessionmaker()() as s:
        assert await s.scalar(text("SELECT count(*) FROM llm_usage")) == 0
        assert (await s.scalars(select(Cluster.title))).all()
