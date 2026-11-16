from collections.abc import AsyncIterator

import httpx
import pytest

from news.api.app import create_app
from news.connectors.http import SafeHttp
from news.db.engine import sessionmaker
from news.keys import create_key
from tests.conftest import public_resolver


async def make_key(role: str) -> str:
    async with sessionmaker()() as s:
        _, key = await create_key(s, f"test-{role}", role)
        await s.commit()
    return key


@pytest.fixture
async def api(seeded: None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient() as outbound:
            app.state.http = SafeHttp(outbound, "NewsBot/test", resolver=public_resolver)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield client


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_health(api: httpx.AsyncClient) -> None:
    assert (await api.get("/healthz")).json() == {"ok": True}
    ready = await api.get("/readyz")
    assert ready.status_code == 200 and ready.json()["db"] and ready.json()["redis"]


async def test_auth_and_roles(api: httpx.AsyncClient) -> None:
    assert (await api.get("/v1/admin/sources")).status_code == 401
    assert (await api.get("/v1/admin/sources", headers=auth("nws_wrong"))).status_code == 401
    viewer = await make_key("viewer")
    assert (await api.get("/v1/admin/sources", headers=auth(viewer))).status_code == 200
    resp = await api.post("/v1/admin/sources", headers=auth(viewer),
                          json={"name": "x", "type": "rss", "config": {"url": "https://a.example/feed"}})
    assert resp.status_code == 403
    client_key = await make_key("client")
    assert (await api.get("/v1/admin/sources", headers=auth(client_key))).status_code == 403


async def test_sources_crud_and_types(api: httpx.AsyncClient) -> None:
    admin = auth(await make_key("admin"))
    types = {t["type"]: t for t in (await api.get("/v1/admin/sources/types", headers=admin)).json()}
    assert {"rss", "manual"} <= set(types)
    assert "url" in types["rss"]["config_schema"]["properties"]

    bad = await api.post("/v1/admin/sources", headers=admin, json={"name": "bad", "type": "rss", "config": {}})
    assert bad.status_code == 422
    unknown = await api.post("/v1/admin/sources", headers=admin, json={"name": "u", "type": "nope"})
    assert unknown.status_code == 422

    created = await api.post("/v1/admin/sources", headers=admin, json={
        "name": "new-feed", "type": "rss", "trust_weight": 0.6, "config": {"url": "https://a.example/feed"}})
    assert created.status_code == 201
    sid = created.json()["id"]
    assert (await api.post("/v1/admin/sources", headers=admin, json={
        "name": "new-feed", "type": "rss", "config": {"url": "https://a.example/feed"}})).status_code == 409

    patched = await api.patch(f"/v1/admin/sources/{sid}", headers=admin,
                              json={"enabled": False, "reason": "testing"})
    assert patched.status_code == 200 and patched.json()["enabled"] is False

    audit = (await api.get("/v1/admin/audit", headers=admin, params={"target_id": sid})).json()["items"]
    assert [a["action"] for a in audit] == ["source.update", "source.create"]
    assert audit[0]["before"]["enabled"] is True and audit[0]["after"]["enabled"] is False

    assert (await api.delete(f"/v1/admin/sources/{sid}", headers=admin)).status_code == 204


async def test_source_test_endpoint(api: httpx.AsyncClient) -> None:
    import respx

    admin = auth(await make_key("admin"))
    sources = {s["name"]: s for s in (await api.get("/v1/admin/sources", headers=admin)).json()}
    with respx.mock() as mock:
        mock.get("https://news.example.com/robots.txt").respond(404)
        mock.get("https://news.example.com/rss.xml").respond(
            200, content=open("tests/fixtures/feed.xml", "rb").read())
        resp = await api.post(f"/v1/admin/sources/{sources['test-news']['id']}/test", headers=admin)
    body = resp.json()
    assert body["ok"] and body["count"] == 3
    # A dry run stores nothing and leaves the cursor alone
    items = (await api.get("/v1/admin/items", headers=admin)).json()
    assert items["items"] == []


async def test_manual_items_and_pagination(api: httpx.AsyncClient) -> None:
    mod = auth(await make_key("moderator"))
    for n in range(3):
        resp = await api.post("/v1/admin/items", headers=mod, json={
            "title": f"Manual item {n}", "summary": "Entered by a moderator", "topics": ["energy"],
            "editor_note": "Worth a look", "importance_override": 70})
        assert resp.status_code == 201
        assert resp.json()["is_manual"] and resp.json()["manual"]["editor_note"] == "Worth a look"

    first = (await api.get("/v1/admin/items", headers=mod, params={"limit": 2})).json()
    assert [i["title"] for i in first["items"]] == ["Manual item 2", "Manual item 1"]
    second = (await api.get("/v1/admin/items", headers=mod,
                            params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert [i["title"] for i in second["items"]] == ["Manual item 0"] and second["next_cursor"] is None

    item_id = first["items"][0]["id"]
    edited = await api.patch(f"/v1/admin/items/{item_id}", headers=mod,
                             json={"title": "Manual item 2 (edited)", "editor_note": "Updated"})
    assert edited.status_code == 200
    assert edited.json()["title"] == "Manual item 2 (edited)" and edited.json()["manual"]["editor_note"] == "Updated"


async def test_bulk_csv(api: httpx.AsyncClient) -> None:
    mod = auth(await make_key("moderator"))
    csv = ("title,url,summary,topics\n"
           "First bulk item,https://a.example/1,Short summary,energy;heat-pumps\n"
           "x,,too short a title,\n"
           "Second bulk item,https://a.example/2,,\n")
    resp = await api.post("/v1/admin/items/bulk", headers={**mod, "Content-Type": "text/csv"}, content=csv)
    body = resp.json()
    assert body["created"] == 2 and len(body["errors"]) == 1 and body["errors"][0].startswith("Row 2")
    items = (await api.get("/v1/admin/items", headers=mod)).json()["items"]
    assert {"energy", "heat-pumps"} == set(next(i for i in items if i["title"] == "First bulk item")["manual"]["topics"])


async def test_llm_switch(api: httpx.AsyncClient) -> None:
    admin = auth(await make_key("admin"))
    viewer = auth(await make_key("viewer"))
    assert (await api.get("/v1/admin/settings/llm", headers=viewer)).json()["mode"] == "auto"
    assert (await api.put("/v1/admin/settings/llm", headers=viewer, json={"mode": "off"})).status_code == 403
    assert (await api.put("/v1/admin/settings/llm", headers=admin, json={"mode": "sometimes"})).status_code == 422
    resp = await api.put("/v1/admin/settings/llm", headers=admin, json={"mode": "off"})
    body = resp.json()
    assert resp.status_code == 200 and body["mode"] == "off" and body["daily_budget_usd"] == 1.0
    assert body["tasks"]["extract"]["provider"] == "fake"  # other fields keep their values
    assert (await api.get("/v1/admin/settings/llm", headers=viewer)).json()["mode"] == "off"


async def test_key_lifecycle(api: httpx.AsyncClient) -> None:
    admin = auth(await make_key("admin"))
    created = await api.post("/v1/admin/api-keys", headers=admin, json={"name": "temp", "role": "viewer"})
    assert created.status_code == 201
    new_key = created.json()["key"]
    assert (await api.get("/v1/admin/items", headers=auth(new_key))).status_code == 200
    assert (await api.delete(f"/v1/admin/api-keys/{created.json()['id']}", headers=admin)).status_code == 200
    assert (await api.get("/v1/admin/items", headers=auth(new_key))).status_code == 401
    listed = (await api.get("/v1/admin/api-keys", headers=admin)).json()
    assert all("key" not in k and "key_hash" not in k for k in listed)
