"""Shared fixtures. Unit tests need nothing; tests marked by using the `db`
fixture need NEWS_TEST_DATABASE_URL and NEWS_TEST_REDIS_URL (set in the
compose file). No test touches the live network."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = os.environ.get("NEWS_TEST_DATABASE_URL")
TEST_REDIS = os.environ.get("NEWS_TEST_REDIS_URL")

# Point the app at the test database, test Redis and test config before any
# app module reads the environment
if TEST_DB:
    os.environ["NEWS_DATABASE_URL"] = TEST_DB
if TEST_REDIS:
    os.environ["NEWS_REDIS_URL"] = TEST_REDIS
os.environ["NEWS_CONFIG_DIR"] = str(FIXTURES / "config")
os.environ["NEWS_FAKE_EMBED"] = "1"
os.environ.setdefault("NEWS_SECRET_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")  # tests only

PUBLIC_IP = "93.184.215.14"


async def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


@pytest.fixture(scope="session")
def migrated() -> Iterator[str]:
    if not TEST_DB or not TEST_REDIS:
        pytest.skip("NEWS_TEST_DATABASE_URL / NEWS_TEST_REDIS_URL not set")
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.attributes["url"] = TEST_DB
    cfg.attributes["skip_logging"] = True
    command.upgrade(cfg, "head")
    yield TEST_DB


@pytest.fixture
async def db(migrated: str) -> AsyncIterator[None]:
    from sqlalchemy import text

    from news.db.engine import dispose_engine, init_engine
    from news.settings_store import invalidate
    from news.streams import redis_client

    eng = init_engine(migrated)
    async with eng.begin() as conn:
        await conn.execute(text(
            "TRUNCATE audit_log, items, api_keys, clients, topics, settings, llm_usage, source_instances, "
            "clusters, entities, subscriptions, geonames CASCADE"
        ))
    r = redis_client()
    await r.flushdb()
    await r.aclose()
    invalidate()
    yield
    await dispose_engine()


@pytest.fixture
async def seeded(db: None) -> None:
    from news.config import seed
    from news.db.engine import sessionmaker

    async with sessionmaker()() as session:
        await seed(session, FIXTURES / "config")
