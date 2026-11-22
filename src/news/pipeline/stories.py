"""Stories: items about the same event, grouped by embedding similarity.

Online: each new item joins the most similar active story (centroid
similarity >= story_similarity within story_window_h), or starts one.
Duplicates join their original's story directly, so they count as
corroboration. Periodically, near-identical stories are merged and quiet ones
go dormant, then archived."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import ScoringSettings
from news.db.models import Cluster
from news.embed import embed_passages, item_text, normalise, vec
from news.ids import uuid7

log = logging.getLogger(__name__)


def _parse_vec(s: str) -> list[float]:
    return [float(x) for x in json.loads(s)]


_NAME = re.compile(r"\b[A-Z][\w-]*[A-Za-z0-9]|\b[A-Z]{2,}\d*\b")


def names(text_: str) -> set[str]:
    """Capitalised words, possessives stripped: a cheap stand-in for the
    people, places and organisations an item is about."""
    out = set()
    for m in _NAME.finditer(text_.replace("’", "'")):
        word = m.group(0)
        if len(word) > 2:
            out.add(word.removesuffix("'s"))
    return out


class NameStats:
    """How many recent items mention each name, so only rare names count."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.total = 0
        self.loaded_at = 0.0

    async def refresh(self, session: AsyncSession, window_h: int) -> None:
        if time.monotonic() - self.loaded_at < 600:
            return
        rows = (await session.execute(text("""
            SELECT title, text_snippet FROM items
            WHERE ingested_at > now() - make_interval(hours => :h) AND duplicate_of IS NULL
        """), {"h": window_h})).all()
        self.counts = Counter(n for r in rows for n in names(f"{r.title} {r.text_snippet}"))
        self.total = len(rows)
        self.loaded_at = time.monotonic()

    def rare(self, found: set[str], max_share: float) -> set[str]:
        limit = max(3.0, max_share * self.total)
        return {n for n in found if self.counts.get(n, 0) <= limit}


_stats = NameStats()


async def _cluster_names(session: AsyncSession, cluster_id: uuid.UUID) -> set[str]:
    rows = (await session.execute(text("""
        SELECT i.title, i.text_snippet FROM cluster_items ci JOIN items i ON i.id = ci.item_id
        WHERE ci.cluster_id = :c AND i.duplicate_of IS NULL ORDER BY ci.added_at DESC LIMIT 10
    """), {"c": cluster_id})).all()
    return set().union(*(names(f"{r.title} {r.text_snippet}") for r in rows)) if rows else set()


async def choose_story(session: AsyncSession, v: list[float], item_names: set[str], at: datetime,
                       cfg: ScoringSettings) -> uuid.UUID | None:
    """The story an item belongs to, if any: very similar, or similar and
    sharing rare names."""
    cands = (await session.execute(text("""
        SELECT id, 1 - (centroid <=> CAST(:v AS vector)) AS sim
        FROM clusters
        WHERE status = 'active' AND kind = 'story'
          AND last_activity > CAST(:at AS timestamptz) - make_interval(hours => :h)
        ORDER BY centroid <=> CAST(:v AS vector)
        LIMIT 5
    """), {"v": vec(v), "at": at, "h": cfg.story_window_h})).all()
    rare = _stats.rare(item_names, cfg.rare_name_max_share)
    for c in cands:  # most similar first
        if c.sim >= cfg.story_similarity:
            return uuid.UUID(str(c.id))
        if c.sim < cfg.story_two_names_similarity or not rare:
            continue
        shared = len(rare & await _cluster_names(session, c.id))
        if shared >= 2 or (shared == 1 and c.sim >= cfg.story_one_name_similarity):
            return uuid.UUID(str(c.id))
    return None


async def embed_pending(session: AsyncSession, batch: int = 64) -> int:
    rows = (await session.execute(text("""
        SELECT id, title, text_snippet FROM items
        WHERE embedding IS NULL AND duplicate_of IS NULL
        ORDER BY ingested_at LIMIT :n
    """), {"n": batch})).all()
    if not rows:
        return 0
    vectors = await embed_passages([item_text(r.title, r.text_snippet) for r in rows])
    await session.execute(text("UPDATE items SET embedding = CAST(:v AS vector) WHERE id = :id"),
                          [{"id": r.id, "v": vec(v)} for r, v in zip(rows, vectors, strict=True)])
    await session.commit()
    return len(rows)


async def embed_topics(session: AsyncSession) -> int:
    rows = (await session.execute(text("SELECT id, label, description FROM topics WHERE embedding IS NULL"))).all()
    if not rows:
        return 0
    vectors = await embed_passages([f"{r.label}. {r.description}" for r in rows])
    await session.execute(text("UPDATE topics SET embedding = CAST(:v AS vector) WHERE id = :id"),
                          [{"id": r.id, "v": vec(v)} for r, v in zip(rows, vectors, strict=True)])
    await session.commit()
    return len(rows)


async def _bucket(session: AsyncSession, cluster_id: uuid.UUID, at: datetime, engagement: dict[str, Any]) -> None:
    weight = sum(float(v) for v in engagement.values() if isinstance(v, int | float))
    await session.execute(text("""
        INSERT INTO cluster_timeseries (cluster_id, bucket_start, items, authors, engagement)
        VALUES (:c, to_timestamp(floor(extract(epoch FROM CAST(:at AS timestamptz)) / 900) * 900), 1, 1, :e)
        ON CONFLICT (cluster_id, bucket_start) DO UPDATE SET
            items = cluster_timeseries.items + 1, engagement = cluster_timeseries.engagement + EXCLUDED.engagement
    """), {"c": cluster_id, "at": at, "e": weight})


async def _attach(session: AsyncSession, cluster_id: uuid.UUID, item: Any, v: list[float] | None, at: datetime) -> None:
    await session.execute(text("INSERT INTO cluster_items (cluster_id, item_id) VALUES (:c, :i) ON CONFLICT DO NOTHING"),
                          {"c": cluster_id, "i": item.id})
    row = (await session.execute(text("SELECT centroid::text AS c, item_count FROM clusters WHERE id = :c FOR UPDATE"),
                                 {"c": cluster_id})).one()
    params: dict[str, Any] = {"c": cluster_id, "at": at, "img": item.image_url}
    centroid_sql = ""
    if v is not None:
        n = max(int(row.item_count), 1)
        old = _parse_vec(row.c)
        params["v"] = vec(normalise([(o * n + x) / (n + 1) for o, x in zip(old, v, strict=True)]))
        centroid_sql = "centroid = CAST(:v AS vector),"
    await session.execute(text(f"""
        UPDATE clusters SET {centroid_sql}
            item_count = item_count + 1,
            last_activity = GREATEST(last_activity, :at),
            image_url = COALESCE(image_url, :img),
            score_dirty = true
        WHERE id = :c
    """), params)


async def cluster_pending(session: AsyncSession, cfg: ScoringSettings, batch: int = 200) -> int:
    await _stats.refresh(session, cfg.story_window_h)
    rows = (await session.execute(text("""
        SELECT i.id, i.embedding::text AS emb, i.duplicate_of, i.title, i.text_snippet, i.image_url, i.engagement,
               LEAST(COALESCE(i.published_at, i.ingested_at), now()) AS at,
               (SELECT ci.cluster_id FROM cluster_items ci WHERE ci.item_id = i.duplicate_of) AS original_cluster
        FROM items i
        WHERE NOT EXISTS (SELECT 1 FROM cluster_items ci WHERE ci.item_id = i.id)
          AND (i.embedding IS NOT NULL OR i.duplicate_of IS NOT NULL)
          -- Research papers found by search go to editions' research section, not into stories
          AND NOT (i.raw ? 'openalex_id')
        ORDER BY i.ingested_at
        LIMIT :n
    """), {"n": batch})).all()
    done = 0
    for item in rows:
        if item.duplicate_of is not None:
            if item.original_cluster is None:
                continue  # the original isn't in a story yet; next round
            await _attach(session, item.original_cluster, item, None, item.at)
            await _bucket(session, item.original_cluster, item.at, item.engagement)
        else:
            v = _parse_vec(item.emb)
            best = await choose_story(session, v, names(f"{item.title} {item.text_snippet}"), item.at, cfg)
            if best is not None:
                await _attach(session, best, item, v, item.at)
                cluster_id = best
            else:
                cluster = Cluster(id=uuid7(), title=item.title, centroid=v, first_seen=item.at,
                                  last_activity=item.at, item_count=1, image_url=item.image_url)
                session.add(cluster)
                await session.flush()
                await session.execute(text("INSERT INTO cluster_items (cluster_id, item_id) VALUES (:c, :i)"),
                                      {"c": cluster.id, "i": item.id})
                cluster_id = cluster.id
            await _bucket(session, cluster_id, item.at, item.engagement)
        done += 1
        await session.commit()
    return done


async def merge_similar(session: AsyncSession, cfg: ScoringSettings) -> int:
    """Merge active stories whose centroids are near-identical: the smaller
    story's items move to the larger, and the smaller is archived."""
    pairs = (await session.execute(text("""
        SELECT a.id AS a, b.id AS b, a.item_count AS na, b.item_count AS nb
        FROM clusters a
        JOIN LATERAL (
            SELECT id, item_count, centroid FROM clusters b
            WHERE b.status = 'active' AND b.id <> a.id AND b.kind = a.kind
            ORDER BY b.centroid <=> a.centroid LIMIT 3
        ) b ON 1 - (a.centroid <=> b.centroid) >= :t
        WHERE a.status = 'active' AND cardinality(a.locked_fields) = 0
    """), {"t": cfg.merge_similarity})).all()
    merged: set[uuid.UUID] = set()
    for p in pairs:
        if p.a in merged or p.b in merged:
            continue
        keep, drop = (p.a, p.b) if (p.na, str(p.a)) >= (p.nb, str(p.b)) else (p.b, p.a)
        await session.execute(text("UPDATE cluster_items SET cluster_id = :k WHERE cluster_id = :d"), {"k": keep, "d": drop})
        await session.execute(text("""
            INSERT INTO cluster_timeseries (cluster_id, bucket_start, items, authors, engagement)
            SELECT :k, bucket_start, items, authors, engagement FROM cluster_timeseries WHERE cluster_id = :d
            ON CONFLICT (cluster_id, bucket_start) DO UPDATE SET
                items = cluster_timeseries.items + EXCLUDED.items,
                engagement = cluster_timeseries.engagement + EXCLUDED.engagement
        """), {"k": keep, "d": drop})
        await session.execute(text("DELETE FROM cluster_timeseries WHERE cluster_id = :d"), {"d": drop})
        await session.execute(text("""
            UPDATE clusters k SET
                centroid = (SELECT l2_normalize(avg(i.embedding)) FROM cluster_items ci JOIN items i ON i.id = ci.item_id
                            WHERE ci.cluster_id = k.id AND i.embedding IS NOT NULL),
                item_count = (SELECT count(*) FROM cluster_items WHERE cluster_id = k.id),
                first_seen = LEAST(k.first_seen, (SELECT first_seen FROM clusters WHERE id = :d)),
                last_activity = GREATEST(k.last_activity, (SELECT last_activity FROM clusters WHERE id = :d)),
                summary_item_count = NULL,
                score_dirty = true
            WHERE k.id = :k
        """), {"k": keep, "d": drop})
        await session.execute(text("UPDATE clusters SET status = 'archived', item_count = 0 WHERE id = :d"), {"d": drop})
        await session.commit()
        merged.update((keep, drop))
        log.info("stories merged", extra={"kept": str(keep), "merged": str(drop)})
    return len(merged) // 2


async def age(session: AsyncSession, cfg: ScoringSettings) -> None:
    now = datetime.now(UTC)
    await session.execute(text("""
        UPDATE clusters SET status = 'dormant'
        WHERE status = 'active' AND last_activity < CAST(:now AS timestamptz) - make_interval(hours => :h)
          AND (pinned_until IS NULL OR pinned_until < CAST(:now AS timestamptz))
    """), {"now": now, "h": cfg.dormant_after_h})
    await session.execute(text("""
        UPDATE clusters SET status = 'archived'
        WHERE status = 'dormant' AND last_activity < CAST(:now AS timestamptz) - make_interval(days => :d)
    """), {"now": now, "d": cfg.archive_after_days})
    # Items collected before their source reported its own name get it from
    # the source's newer items
    await session.execute(text("""
        UPDATE items i SET raw = i.raw || jsonb_build_object('outlet', o.outlet)
        FROM (SELECT DISTINCT ON (source_instance_id) source_instance_id, raw->>'outlet' AS outlet
              FROM items WHERE raw ? 'outlet' ORDER BY source_instance_id, ingested_at DESC) o
        WHERE i.source_instance_id = o.source_instance_id AND NOT i.raw ? 'outlet'
    """))
    await session.commit()
