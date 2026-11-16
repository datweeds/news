"""Normalise a RawItem, check it against recent items, and store it.

Duplicates are stored too, linked to the first copy (duplicate_of), so later
stages can count how many sources carried a story without enriching each
copy again."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import AppSettings
from news.connectors.base import RawItem
from news.db.models import Item
from news.ids import uuid7
from news.pipeline.text import bands, canonical_url, clean_html, detect_lang, simhash64, truncate

_NEAR_DUP_SQL = text("""
    SELECT id FROM items
    WHERE ingested_at > now() - make_interval(days => :days)
      AND duplicate_of IS NULL
      AND (((simhash >> 48) & 65535) = :b0 OR ((simhash >> 32) & 65535) = :b1
           OR ((simhash >> 16) & 65535) = :b2 OR (simhash & 65535) = :b3)
      AND bit_count((simhash # CAST(:h AS bigint))::bit(64)) <= :maxd
    ORDER BY ingested_at
    LIMIT 1
""")

_URL_DUP_SQL = text("""
    SELECT id FROM items
    WHERE canonical_url = :url AND duplicate_of IS NULL
      AND ingested_at > now() - make_interval(days => :days)
    ORDER BY ingested_at
    LIMIT 1
""")


async def find_duplicate(session: AsyncSession, canon: str | None, sh: int, cfg: AppSettings,
                         near: bool = True) -> uuid.UUID | None:
    if canon:
        found = await session.scalar(_URL_DUP_SQL, {"url": canon, "days": cfg.dedup_window_days})
        if found:
            return uuid.UUID(str(found))
    if near:
        b = bands(sh)
        found = await session.scalar(_NEAR_DUP_SQL, {
            "days": cfg.dedup_window_days, "h": sh, "maxd": cfg.simhash_max_distance,
            "b0": b[0], "b1": b[1], "b2": b[2], "b3": b[3],
        })
        if found:
            return uuid.UUID(str(found))
    return None


async def store_raw(session: AsyncSession, raw: RawItem, cfg: AppSettings,
                    created_by: uuid.UUID | None = None) -> Item | None:
    """Store a RawItem. Returns the new Item, or None if this source already
    sent an item with the same external id."""
    source_id = uuid.UUID(raw.source_instance)
    exists = await session.scalar(
        select(Item.id).where(Item.source_instance_id == source_id, Item.external_id == raw.external_id)
    )
    if exists:
        return None

    title = truncate(clean_html(raw.title), 500)
    snippet = truncate(clean_html(raw.text), cfg.snippet_max_chars)
    canon = canonical_url(raw.url) if raw.url else None
    sh = simhash64(f"{title} {snippet}")
    is_manual = raw.kind == "manual"
    # Manual entries are often short; only an identical URL marks them as copies
    duplicate_of = await find_duplicate(session, canon, sh, cfg, near=not is_manual)

    new_id = uuid7()
    stmt = (
        pg_insert(Item)
        .values(
            id=new_id, source_instance_id=source_id, external_id=raw.external_id, kind=raw.kind,
            url=raw.url, canonical_url=canon, title=title, text_snippet=snippet, author=raw.author,
            published_at=raw.published_at, lang=detect_lang(f"{title}. {snippet}", raw.lang),
            engagement=raw.engagement, simhash=sh, is_manual=is_manual, created_by=created_by,
            duplicate_of=duplicate_of, platform=raw.platform, paywalled=raw.paywalled,
            geo_hint=raw.geo_hint, image_url=raw.image_url, raw=raw.raw,
        )
        .on_conflict_do_nothing(index_elements=["source_instance_id", "external_id"])
        .returning(Item.id)
    )
    inserted = await session.scalar(stmt)
    await session.commit()
    if inserted is None:
        return None
    return await session.get(Item, new_id)
