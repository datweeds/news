"""LLM enrichment: topics, entities, places, significance and novelty per
item, in batches. Each item is enriched once and never re-sent.

To keep within the budget, only items that could reach a reader are sent:
not duplicates, at most `enrich_max_items_per_story` per story, and (when
any subscriptions exist) only items similar to some subscription phrase."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import LlmSettings, ScoringSettings
from news.embed import embed_queries, vec
from news.geo import geocode
from news.ids import uuid7
from news.llm.budget import run_task
from news.llm.client import LLMError, parse_json
from news.prompts import data, load

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class ExtractEntity(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["person", "org", "other"] = "other"


class ExtractLocation(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    country: str | None = Field(None, max_length=3)
    role: Literal["primary", "mentioned"] = "mentioned"


class ExtractItem(BaseModel):
    id: int
    topics: list[str] = Field(default_factory=list, max_length=8)
    entities: list[ExtractEntity] = Field(default_factory=list, max_length=15)
    locations: list[ExtractLocation] = Field(default_factory=list, max_length=12)
    significance: float = Field(ge=0, le=1)
    novelty: Literal["new", "update", "rehash"] = "new"
    promotional: bool = False


class ExtractOut(BaseModel):
    items: list[ExtractItem]


async def candidates(session: AsyncSession, llm: LlmSettings, scoring: ScoringSettings) -> list[Any]:
    return list((await session.execute(text("""
        WITH phrases AS (
            SELECT sp.embedding FROM subscription_phrases sp
            JOIN subscriptions s ON s.id = sp.subscription_id
            WHERE s.active AND COALESCE((s.definition->>'use_llm')::boolean, true)
        )
        SELECT i.id, i.title, i.text_snippet, i.kind, COALESCE(i.raw->>'outlet', s.name) AS outlet
        FROM items i
        JOIN cluster_items ci ON ci.item_id = i.id
        JOIN source_instances s ON s.id = i.source_instance_id
        WHERE i.enrichment IS NULL AND i.duplicate_of IS NULL AND i.embedding IS NOT NULL
          AND i.enrich_attempts < :max_attempts
          AND i.ingested_at > now() - make_interval(hours => :age)
          AND (SELECT count(*) FROM cluster_items c2 JOIN items i2 ON i2.id = c2.item_id
               WHERE c2.cluster_id = ci.cluster_id AND i2.enrichment IS NOT NULL) < :per_story
          AND (NOT EXISTS (SELECT 1 FROM phrases)
               OR EXISTS (SELECT 1 FROM phrases p WHERE 1 - (p.embedding <=> i.embedding) >= :gate))
        ORDER BY s.trust_weight DESC, i.ingested_at DESC
        LIMIT :limit
    """), {"max_attempts": MAX_ATTEMPTS, "age": llm.backlog_max_age_hours,
           "per_story": scoring.enrich_max_items_per_story, "gate": scoring.enrich_relevance_gate,
           "limit": llm.extract_batch_size})).all())


def build_prompt(rows: list[Any]) -> str:
    parts = [f'<item id="{n}" outlet="{data(r.outlet)}" kind="{r.kind}">\n'
             f"<title>{data(r.title)}</title>\n<text>{data(r.text_snippet)}</text>\n</item>"
             for n, r in enumerate(rows)]
    return "Tag these items.\n\n" + "\n\n".join(parts)


async def attribution(session: AsyncSession, rows: list[Any], gate: float) -> dict[uuid.UUID, float]:
    """Each item's share of a batch is split evenly among the subscriptions
    (using the LLM) whose phrases it matched."""
    matches = (await session.execute(text("""
        SELECT DISTINCT i.id AS item_id, sp.subscription_id
        FROM items i
        JOIN subscription_phrases sp ON 1 - (sp.embedding <=> i.embedding) >= :gate
        JOIN subscriptions s ON s.id = sp.subscription_id
        WHERE i.id = ANY(:ids) AND s.active AND COALESCE((s.definition->>'use_llm')::boolean, true)
    """), {"ids": [r.id for r in rows], "gate": gate})).all()
    subs_by_item: dict[uuid.UUID, list[uuid.UUID]] = {}
    for m in matches:
        subs_by_item.setdefault(m.item_id, []).append(m.subscription_id)
    shares: dict[uuid.UUID, float] = {}
    for subs in subs_by_item.values():
        for s in subs:
            shares[s] = shares.get(s, 0.0) + 1 / len(rows) / len(subs)
    return shares


async def _map_topics(session: AsyncSession, labels: list[str], threshold: float) -> dict[str, uuid.UUID]:
    """Free-text topic labels → taxonomy topics, by embedding similarity."""
    if not labels or not await session.scalar(text("SELECT EXISTS (SELECT 1 FROM topics WHERE embedding IS NOT NULL)")):
        return {}
    out: dict[str, uuid.UUID] = {}
    for label, v in zip(labels, await embed_queries(labels), strict=True):
        row = (await session.execute(text("""
            SELECT id, 1 - (embedding <=> CAST(:v AS vector)) AS sim FROM topics
            WHERE embedding IS NOT NULL ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1
        """), {"v": vec(v)})).first()
        if row is not None and row.sim >= threshold:
            out[label] = row.id
    return out


async def _save(session: AsyncSession, item_id: uuid.UUID, result: ExtractItem, version: str,
                scoring: ScoringSettings) -> None:
    labels = sorted({t.strip().lower()[:80] for t in result.topics if t.strip()})
    mapped = await _map_topics(session, labels, scoring.taxonomy_match)
    for label in labels:
        await session.execute(text("""
            INSERT INTO item_topics (item_id, label, topic_id) VALUES (:i, :l, :t) ON CONFLICT DO NOTHING
        """), {"i": item_id, "l": label, "t": mapped.get(label)})
    for ent in result.entities:
        entity_id = await session.scalar(text("""
            INSERT INTO entities (id, name, type) VALUES (:id, :n, :t)
            ON CONFLICT (lower(name), type) DO UPDATE SET name = entities.name
            RETURNING id
        """), {"id": uuid7(), "n": ent.name.strip(), "t": ent.type})
        await session.execute(text("INSERT INTO item_entities (item_id, entity_id) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                              {"i": item_id, "e": entity_id})
    for loc in result.locations:
        place = await geocode(session, loc.name, loc.country)
        await session.execute(text("""
            INSERT INTO item_locations (item_id, name, geonames_id, country, admin1, role, geom)
            VALUES (:i, :n, :g, :c, :a, :r,
                    (SELECT geom FROM geonames WHERE geonameid = CAST(:g AS integer)))
            ON CONFLICT (item_id, name) DO NOTHING
        """), {"i": item_id, "n": loc.name.strip()[:200], "g": place.geonames_id if place else None,
               "c": (place.country if place else (loc.country or "")[:2].upper()) or None,
               "a": place.admin1 if place and place.kind != "country" else None, "r": loc.role})
    enrichment = result.model_dump(exclude={"id"})
    await session.execute(text("""
        UPDATE items SET enrichment = CAST(:e AS jsonb), significance = :s, prompt_version = :v WHERE id = :i
    """), {"e": json.dumps(enrichment), "s": result.significance, "v": version, "i": item_id})
    await session.execute(text("""
        UPDATE clusters SET score_dirty = true
        WHERE id = (SELECT cluster_id FROM cluster_items WHERE item_id = :i)
    """), {"i": item_id})


async def enrich_batch(session: AsyncSession, llm: LlmSettings, scoring: ScoringSettings) -> int | None:
    """Enrich one batch. Returns how many items were enriched, 0 if there was
    nothing to do, or None if the LLM may not be used right now."""
    rows = await candidates(session, llm, scoring)
    if not rows:
        return 0
    prompt = load("extract")
    user = build_prompt(rows)
    shares = await attribution(session, rows, scoring.enrich_relevance_gate)
    parsed: ExtractOut | None = None
    for attempt in range(2):  # one retry on invalid output
        try:
            result = await run_task(session, llm, "extract", prompt.text, user, attribution=shares)
        except LLMError:
            break
        if result is None:
            return None
        try:
            parsed = ExtractOut.model_validate(parse_json(result.text))
            break
        except (LLMError, ValidationError) as e:
            log.warning("invalid extract output", extra={"attempt": attempt + 1, "error": str(e)[:300]})
    by_id = {r.id: r for r in (parsed.items if parsed else []) if 0 <= r.id < len(rows)}
    for n, row in enumerate(rows):
        if n in by_id:
            await _save(session, row.id, by_id[n], prompt.version, scoring)
        else:
            await session.execute(text("UPDATE items SET enrich_attempts = enrich_attempts + 1 WHERE id = :i"),
                                  {"i": row.id})
    await session.commit()
    return len(by_id)
