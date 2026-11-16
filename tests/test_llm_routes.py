"""A subscription's own LLM provider, AI write-ups off, and spend attribution."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select, text

from news.api.app import create_app
from news.config import ModelConfig
from news.connectors.http import SafeHttp
from news.db.engine import sessionmaker
from news.db.models import Client, LlmUsage, Subscription
from news.delivery.produce import produce
from news.keys import create_key
from news.llm import budget
from news.llm.client import FakeClient, LLMClient
from tests.conftest import public_resolver
from tests.test_delivery import definition, stories
from tests.test_stories import set_llm


@pytest.fixture
async def client_api(seeded: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    # Own providers are reached through the fake client in tests
    seen_keys: list[str | None] = []

    def fake_client_for(cfg: ModelConfig, timeout_s: int, api_key: str | None = None) -> LLMClient:
        seen_keys.append(api_key)
        return FakeClient(cfg, timeout_s)

    async def public_check(url: str, resolver: object = None) -> None:
        if "127.0.0.1" in url or "localhost" in url:
            from news.connectors.http import BlockedUrl
            raise BlockedUrl("private")

    monkeypatch.setattr(budget, "client_for", fake_client_for)
    monkeypatch.setattr("news.connectors.http.check_url", public_check)
    async with sessionmaker()() as s:
        c = Client(name="dr")
        s.add(c)
        await s.flush()
        _, key = await create_key(s, "dr", "client", c.id)
        await s.commit()
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient() as outbound:
            app.state.http = SafeHttp(outbound, "NewsBot/test", resolver=public_resolver)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                api.seen_keys = seen_keys  # type: ignore[attr-defined]
                yield api, {"Authorization": f"Bearer {key}"}


OWN = {"adapter": "openai_compat", "base_url": "https://api.example-llm.com/v1", "model": "small-1",
       "input_usd_per_mtok": 2.0, "output_usd_per_mtok": 6.0, "monthly_budget_usd": 5.0}


async def test_own_provider_key_is_write_only_and_spend_is_own(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await set_llm(mode="off")  # the platform's LLM is off; the Society's own still works
    await stories()
    sub = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition()})).json()
    resp = await api.put(f"/v1/subscriptions/{sub['id']}/llm", headers=auth, json={**OWN, "api_key": "sk-secret-123"})
    assert resp.status_code == 200 and resp.json()["has_key"] and "api_key" not in resp.json()
    got = (await api.get(f"/v1/subscriptions/{sub['id']}", headers=auth)).json()
    assert got["llm"]["model"] == "small-1" and "sk-secret" not in str(got)
    async with sessionmaker()() as s:
        stored = await s.scalar(select(Subscription.llm_secret))
        assert stored and stored.startswith("v1:") and "sk-secret" not in stored

    digest = (await api.post(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)).json()
    assert digest["llm_written"] and digest["overview"]
    assert "sk-secret-123" in api.seen_keys  # type: ignore[attr-defined]
    usage = (await api.get(f"/v1/subscriptions/{sub['id']}/usage", headers=auth)).json()
    assert usage["own"]["month_usd"] > 0 and usage["platform"]["month_usd"] == 0
    async with sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(LlmUsage)) == 0  # nothing on the platform's bill

    # Edit without re-sending the key keeps it
    assert (await api.put(f"/v1/subscriptions/{sub['id']}/llm", headers=auth, json={**OWN, "model": "small-2"})).json()["has_key"]


async def test_own_budget_falls_back_to_no_ai_not_the_platform(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await set_llm(mode="on")
    await stories()
    sub = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition()})).json()
    await api.put(f"/v1/subscriptions/{sub['id']}/llm", headers=auth, json={**OWN, "monthly_budget_usd": 0, "api_key": "k"})
    digest = (await api.post(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)).json()
    assert not digest["llm_written"] and digest["items"]
    async with sessionmaker()() as s:
        assert await s.scalar(text("SELECT count(*) FROM llm_usage")) == 0


async def test_private_provider_address_is_refused(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    resp = await api.post("/v1/llm/test", headers=auth, json={**OWN, "base_url": "http://127.0.0.1:11434/v1", "api_key": "x"})
    assert resp.status_code == 200 and not resp.json()["ok"] and "isn't allowed" in resp.json()["error"]
    ok = (await api.post("/v1/llm/test", headers=auth, json={**OWN, "api_key": "x"})).json()
    assert ok["ok"] and ok["tokens_in"] > 0


async def test_ai_writeups_off_means_no_llm(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await set_llm(mode="on")
    await stories()
    sub = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition(use_llm=False)})).json()
    await api.post(f"/v1/subscriptions/{sub['id']}/read", headers=auth)
    digest = (await api.post(f"/v1/subscriptions/{sub['id']}/digests", headers=auth)).json()
    assert not digest["llm_written"]
    async with sessionmaker()() as s:
        from news.config import LlmSettings
        from news.settings_store import get_settings
        llm = (await get_settings(s, LlmSettings)).model_copy(update={"mode": "auto"})
        assert not (await budget.gate(s, llm)).allowed  # its readers don't count


async def test_enrichment_spend_is_shared_between_matching_subscriptions(client_api: tuple[httpx.AsyncClient, dict[str, str]]) -> None:
    api, auth = client_api
    await set_llm(mode="on")
    await stories()
    a = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition()})).json()
    b = (await api.post("/v1/subscriptions", headers=auth, json={"definition": definition()})).json()
    async with sessionmaker()() as s:
        from news.config import LlmSettings, ScoringSettings
        from news.pipeline.enrich import enrich_batch
        from news.settings_store import get_settings
        assert await enrich_batch(s, await get_settings(s, LlmSettings), await get_settings(s, ScoringSettings))
        shares = dict((await s.execute(text("""
            SELECT subscription_id::text, sum(cost_usd) FROM llm_usage_subscriptions WHERE task = 'extract' GROUP BY 1
        """))).all())
        total = await s.scalar(text("SELECT sum(cost_usd) FROM llm_usage WHERE task = 'extract'"))
    assert set(shares) == {a["id"], b["id"]}
    assert float(shares[a["id"]]) == pytest.approx(float(shares[b["id"]]), rel=0.01)
    assert sum(float(v) for v in shares.values()) <= float(total) + 1e-6
