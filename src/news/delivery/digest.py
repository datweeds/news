"""Choosing a digest's stories.

relevance = importance × topic match × place match, per section. A story goes
in the section it matches best. Only stories at or above min_score are used
(pinned ones and a moderator's chosen headline always are), within the caps
per section and per digest. A story already sent is only sent again as an
update, after its importance rose by resend_rise and new coverage arrived.
The headline is the moderator's choice if any, else the most relevant story.

The same code produces previews (nothing stored, no LLM) and real digests."""

from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import ScoringSettings
from news.delivery.models import LocationDef, SubscriptionDef

PhraseVectors = dict[str, list[tuple[str, list[float]]]]  # section key → [(phrase, vector)]


@dataclass
class Candidate:
    cluster_id: uuid.UUID
    title: str
    summary: str | None
    why_it_matters: str | None
    image_url: str | None
    score: float
    first_seen: datetime
    last_activity: datetime
    item_count: int
    source_count: int
    pinned: bool
    locked: list[str]
    lead_snippet: str
    kind: str
    section_key: str = ""
    topic_similarity: float = 0.0
    topic_factor: float = 0.0
    location_factor: float = 1.0
    relevance: float = 0.0
    is_update: bool = False
    is_headline: bool = False
    chosen_headline: bool = False  # a moderator's choice; the writer can't drop it
    reason: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Draft:
    items: list[Candidate]
    near_misses: list[Candidate]
    window_start: datetime
    window_end: datetime


_CANDIDATES = text("""
    SELECT c.id, c.title, c.summary, c.why_it_matters, c.image_url, c.score, c.first_seen, c.last_activity,
           c.item_count, c.source_count, (c.pinned_until IS NOT NULL AND c.pinned_until > now()) AS pinned,
           c.locked_fields, c.centroid::text AS centroid,
           (SELECT i.text_snippet FROM cluster_items ci JOIN items i ON i.id = ci.item_id
              JOIN source_instances s ON s.id = i.source_instance_id
            WHERE ci.cluster_id = c.id ORDER BY s.trust_weight DESC, i.ingested_at LIMIT 1) AS lead_snippet,
           (SELECT mode() WITHIN GROUP (ORDER BY i.kind) FROM cluster_items ci JOIN items i ON i.id = ci.item_id
            WHERE ci.cluster_id = c.id) AS kind
    FROM clusters c
    WHERE c.kind = 'story' AND c.status = 'active' AND c.item_count > 0
      AND (c.last_activity > CAST(:since AS timestamptz) OR (c.pinned_until IS NOT NULL AND c.pinned_until > now())
           OR c.id = ANY(CAST(:extra AS uuid[])))
""")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _place_match(story: Any, want: LocationDef) -> str:
    """'full', 'partial' (the story is about a wider area containing the
    place wanted, e.g. the UK for Scotland) or 'none'."""
    if story.country != want.country:
        return "none"
    story_is_country = story.admin1 is None and story.lat is None
    if want.kind == "country":
        return "full"
    if want.kind == "admin1":
        if story.admin1 == want.admin1:
            return "full"
        return "partial" if story_is_country else "none"
    # A town or city
    if story.lat is not None and want.lat is not None and want.lon is not None:
        if _haversine_km(story.lat, story.lon, want.lat, want.lon) <= want.radius_km:
            return "full"
    if story_is_country or (story.lat is None and story.admin1 is not None and story.admin1 == want.admin1):
        return "partial"
    return "none"


async def _location_factors(session: AsyncSession, ids: list[uuid.UUID], wanted: list[LocationDef],
                            cfg: ScoringSettings) -> dict[uuid.UUID, float]:
    if not wanted:
        return {}
    rows = (await session.execute(text("""
        SELECT ci.cluster_id, il.country, il.admin1, il.role,
               ST_Y(il.geom::geometry) AS lat, ST_X(il.geom::geometry) AS lon
        FROM item_locations il JOIN cluster_items ci ON ci.item_id = il.item_id
        WHERE ci.cluster_id = ANY(:ids) AND il.country IS NOT NULL
    """), {"ids": ids})).all()
    by_cluster: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for r in rows:
        by_cluster[r.cluster_id].append(r)
    out: dict[uuid.UUID, float] = {}
    for cid in ids:
        locs = by_cluster.get(cid)
        if not locs:
            out[cid] = cfg.unknown_location
            continue
        best = 0.0
        for loc in locs:
            role = 1.0 if loc.role == "primary" else cfg.mentioned_location
            for want in wanted:
                match = _place_match(loc, want)
                value = 1.0 if match == "full" else cfg.mentioned_location if match == "partial" else 0.0
                best = max(best, value * role)
        out[cid] = best
    return out


async def _texts(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    rows = (await session.execute(text("""
        SELECT ci.cluster_id, lower(string_agg(i.title || ' ' || i.text_snippet, ' ')) AS words
        FROM cluster_items ci JOIN items i ON i.id = ci.item_id
        WHERE ci.cluster_id = ANY(:ids) GROUP BY 1
    """), {"ids": ids})).all()
    return {r.cluster_id: str(r.words or "") for r in rows}


async def build(session: AsyncSession, sub: SubscriptionDef, cfg: ScoringSettings, phrases: PhraseVectors,
                window_h: int, subscription_id: uuid.UUID | None = None, now: datetime | None = None) -> Draft:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=window_h)
    min_score = sub.min_score if sub.min_score is not None else cfg.min_score
    max_items = sub.max_items or cfg.max_items
    max_per_section = sub.max_per_section or cfg.max_per_section

    hidden: set[uuid.UUID] = set()
    headline_choice: uuid.UUID | None = None
    sent: dict[uuid.UUID, tuple[float, int]] = {}
    if subscription_id is not None:
        for r in (await session.execute(text("""
            SELECT cluster_id, action FROM subscription_overrides
            WHERE subscription_id = :s AND (until IS NULL OR until > now()) ORDER BY created_at
        """), {"s": subscription_id})).all():
            if r.action == "hide":
                hidden.add(r.cluster_id)
            else:
                headline_choice = r.cluster_id
        for r in (await session.execute(text("""
            SELECT di.cluster_id, max(di.score) AS score, max(di.item_count) AS n
            FROM digest_items di JOIN digests d ON d.id = di.digest_id
            WHERE d.subscription_id = :s GROUP BY 1
        """), {"s": subscription_id})).all():
            sent[r.cluster_id] = (float(r.score), int(r.n))

    extra = [str(headline_choice)] if headline_choice else []
    rows = (await session.execute(_CANDIDATES, {"since": since, "extra": extra})).all()
    cands = [Candidate(
        cluster_id=r.id, title=r.title, summary=r.summary, why_it_matters=r.why_it_matters, image_url=r.image_url,
        score=float(r.score), first_seen=r.first_seen, last_activity=r.last_activity, item_count=r.item_count,
        source_count=r.source_count, pinned=r.pinned, locked=list(r.locked_fields or []),
        lead_snippet=r.lead_snippet or "", kind=r.kind or "news",
    ) for r in rows]
    if not cands:
        return Draft([], [], since, now)

    ids = [c.cluster_id for c in cands]
    centroids = np.array([[float(x) for x in r.centroid.strip("[]").split(",")] for r in rows], dtype=np.float32)
    floor, full = cfg.topic_floor, cfg.topic_full
    section_sims: dict[str, np.ndarray] = {}
    for section in sub.sections:
        vectors = [v for _, v in phrases.get(section.key, [])]
        if vectors:
            section_sims[section.key] = (centroids @ np.array(vectors, dtype=np.float32).T).max(axis=1)
        else:
            section_sims[section.key] = np.zeros(len(cands), dtype=np.float32)

    needs_text = any(s.must_include or s.must_exclude for s in sub.sections)
    texts = await _texts(session, ids) if needs_text else {}
    loc_factors = await _location_factors(session, ids, sub.locations, cfg)

    eligible: list[Candidate] = []
    misses: list[Candidate] = []
    for n, c in enumerate(cands):
        best_factor, best_sim, best_key = 0.0, -1.0, ""
        for section in sub.sections:
            sim = float(section_sims[section.key][n])
            factor = min(1.0, max(0.0, (sim - floor) / (full - floor))) if full > floor else float(sim >= floor)
            if factor > 0 and needs_text:
                t = texts.get(c.cluster_id, "")
                if section.must_include and not any(w.lower() in t for w in section.must_include):
                    factor = 0.0
                if section.must_exclude and any(w.lower() in t for w in section.must_exclude):
                    factor = 0.0
            if factor > best_factor or (best_factor == 0 and sim > best_sim):
                best_factor, best_sim, best_key = factor, sim, section.key
        c.section_key, c.topic_similarity, c.topic_factor = best_key, round(best_sim, 3), round(best_factor, 3)
        c.location_factor = loc_factors.get(c.cluster_id, 1.0)
        c.relevance = round(c.score * c.topic_factor * c.location_factor, 2)
        chosen = c.cluster_id == headline_choice
        c.chosen_headline = chosen

        if c.cluster_id in hidden:
            c.reason = "Hidden by a moderator"
        elif c.topic_factor == 0 and not chosen:
            c.reason = "Doesn't match any section's phrases"
        elif c.location_factor == 0 and not chosen:
            c.reason = "Not about the subscription's places"
        elif c.cluster_id in sent and not chosen:
            prev_score, prev_n = sent[c.cluster_id]
            if c.score - prev_score >= cfg.resend_rise and c.item_count > prev_n:
                c.is_update = True
            else:
                c.reason = "Already in an earlier edition"
        if not c.reason and c.relevance < min_score and not c.pinned and not chosen:
            c.reason = f"Below the minimum score ({c.relevance:.0f} < {min_score})"
        if c.reason:
            if c.topic_factor > 0 or c.topic_similarity >= floor - 0.05:
                misses.append(c)
        else:
            eligible.append(c)

    def rank(c: Candidate) -> tuple[bool, bool, float]:
        return (c.cluster_id == headline_choice, c.pinned, c.relevance)

    eligible.sort(key=rank, reverse=True)
    kinds = Counter(c.kind for c in eligible)
    share_cap = math.ceil(cfg.max_source_type_share * max_items) if len(kinds) > 1 else max_items
    per_section: Counter[str] = Counter()
    per_kind: Counter[str] = Counter()
    chosen_items: list[Candidate] = []
    for c in eligible:
        forced = c.cluster_id == headline_choice or c.pinned
        if not forced:
            if len(chosen_items) >= max_items:
                c.reason = "Edition is full"
            elif per_section[c.section_key] >= max_per_section:
                c.reason = "Section is full"
            elif per_kind[c.kind] >= share_cap:
                c.reason = f"Too many {c.kind} items"
            if c.reason:
                misses.append(c)
                continue
        chosen_items.append(c)
        per_section[c.section_key] += 1
        per_kind[c.kind] += 1

    if chosen_items:
        head = next((c for c in chosen_items if c.cluster_id == headline_choice), chosen_items[0])
        head.is_headline = True
    order = {s.key: n for n, s in enumerate(sub.sections)}
    chosen_items.sort(key=lambda c: (not c.is_headline, order.get(c.section_key, 99), -c.relevance))
    misses.sort(key=lambda c: c.relevance, reverse=True)
    return Draft(chosen_items, misses[:10], since, now)


async def add_extras(session: AsyncSession, items: list[Candidate]) -> None:
    """Sources, topics, places and any editor's note for the chosen stories."""
    if not items:
        return
    ids = [c.cluster_id for c in items]
    by_id = {c.cluster_id: c for c in items}
    for c in items:
        c.extras = {"top_sources": [], "topics": [], "locations": [], "editor_note": None}
    rows = (await session.execute(text("""
        SELECT ci.cluster_id, i.title, i.url, COALESCE(i.raw->>'outlet', s.name) AS outlet, i.published_at,
               i.is_manual, i.raw->'manual'->>'editor_note' AS note, i.kind
        FROM cluster_items ci JOIN items i ON i.id = ci.item_id JOIN source_instances s ON s.id = i.source_instance_id
        WHERE ci.cluster_id = ANY(:ids)
        ORDER BY s.trust_weight DESC, i.published_at NULLS LAST
    """), {"ids": ids})).all()
    seen: dict[uuid.UUID, set[str]] = defaultdict(set)
    for r in rows:
        ex = by_id[r.cluster_id].extras
        if r.note and not ex["editor_note"]:
            ex["editor_note"] = r.note
        key = r.url or r.outlet
        if len(ex["top_sources"]) < 5 and key not in seen[r.cluster_id] and r.outlet not in seen[r.cluster_id]:
            seen[r.cluster_id].update({key, r.outlet})
            ex["top_sources"].append({"title": r.title, "url": r.url, "outlet": r.outlet,
                                      "published_at": r.published_at.isoformat() if r.published_at else None,
                                      "kind": r.kind})
    for r in (await session.execute(text("""
        SELECT ci.cluster_id, it.label, count(*) AS n FROM item_topics it
        JOIN cluster_items ci ON ci.item_id = it.item_id WHERE ci.cluster_id = ANY(:ids)
        GROUP BY 1, 2 ORDER BY 3 DESC, 2
    """), {"ids": ids})).all():
        if len(by_id[r.cluster_id].extras["topics"]) < 4:
            by_id[r.cluster_id].extras["topics"].append(r.label)
    for r in (await session.execute(text("""
        SELECT ci.cluster_id, il.name, bool_or(il.role = 'primary') AS primary_, count(*) AS n FROM item_locations il
        JOIN cluster_items ci ON ci.item_id = il.item_id WHERE ci.cluster_id = ANY(:ids)
        GROUP BY 1, 2 ORDER BY 3 DESC, 4 DESC
    """), {"ids": ids})).all():
        if len(by_id[r.cluster_id].extras["locations"]) < 3:
            by_id[r.cluster_id].extras["locations"].append(r.name)


def item_payload(c: Candidate, headings: dict[str, str]) -> dict[str, Any]:
    return {
        "cluster_id": str(c.cluster_id),
        "kind": "story",
        "title": c.title,
        "summary": c.summary or c.lead_snippet,
        "why_it_matters": c.why_it_matters,
        "image_url": c.image_url,
        "section_key": c.section_key,
        "section_heading": headings.get(c.section_key, ""),
        "is_headline": c.is_headline,
        "is_update": c.is_update,
        "score": round(c.score, 1),
        "relevance": round(c.relevance, 1),
        "first_seen": c.first_seen.isoformat(),
        "last_activity": c.last_activity.isoformat(),
        "item_count": c.item_count,
        "source_count": c.source_count,
        "topics": c.extras.get("topics", []),
        "locations": c.extras.get("locations", []),
        "top_sources": c.extras.get("top_sources", []),
        "papers": [s for s in c.extras.get("top_sources", []) if s.get("kind") in ("paper", "preprint")],
        "manual": bool(c.extras.get("editor_note")),
        "editor_note": c.extras.get("editor_note"),
    }
