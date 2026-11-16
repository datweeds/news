"""Importance: a 0-100 score per story, recomputed as it changes and as it
ages. See config/scoring.yaml for the signals and their weights."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import ScoringSettings

_AGG = text("""
    SELECT c.id, c.last_activity, c.boost,
           count(i.id) AS n_items,
           count(DISTINCT i.source_instance_id) AS n_sources,
           count(DISTINCT COALESCE(split_part(split_part(i.canonical_url, '://', 2), '/', 1), s.name)) AS n_domains,
           avg(s.trust_weight) AS trust,
           sum(i.significance * s.trust_weight) FILTER (WHERE i.significance IS NOT NULL)
             / NULLIF(sum(s.trust_weight) FILTER (WHERE i.significance IS NOT NULL), 0) AS significance,
           avg(CASE i.enrichment->>'novelty' WHEN 'new' THEN 1.0 WHEN 'update' THEN 0.5 WHEN 'rehash' THEN 0.1 END) AS novelty,
           avg(CASE WHEN (i.enrichment->>'promotional')::boolean THEN 1.0 ELSE 0.0 END)
             FILTER (WHERE i.enrichment IS NOT NULL) AS promotional,
           bool_and(i.kind = 'social') AS social_only,
           bool_or(i.kind IN ('paper', 'preprint')) AS science,
           max((i.raw->'manual'->>'importance_override')::int) AS override,
           array_agg(i.kind) AS kinds,
           count(*) FILTER (WHERE ci.added_at > now() - interval '3 hours') AS c3,
           count(*) FILTER (WHERE ci.added_at <= now() - interval '3 hours'
                              AND ci.added_at > now() - interval '24 hours') AS c21
    FROM clusters c
    JOIN cluster_items ci ON ci.cluster_id = c.id
    JOIN items i ON i.id = ci.item_id
    JOIN source_instances s ON s.id = i.source_instance_id
    WHERE c.status = 'active' AND (:all OR c.score_dirty)
    GROUP BY c.id
""")


def _log_scaled(n: int, cap: int = 10) -> float:
    return min(1.0, math.log1p(n) / math.log1p(cap))


def compute(row: Any, cfg: ScoringSettings, now: datetime) -> tuple[float, dict[str, Any]]:
    w = cfg.weights
    trust = float(row.trust or 0.5)
    significance = float(row.significance) if row.significance is not None else 0.4 + 0.2 * trust
    corroboration = _log_scaled(int(row.n_domains))
    baseline = int(row.c21) / 7  # items per 3 hours over the previous 21 hours
    z = (int(row.c3) - baseline) / math.sqrt(baseline + 1)
    velocity = 1 / (1 + math.exp(-(z - 1)))
    breadth = _log_scaled(int(row.n_sources))
    novelty = float(row.novelty) if row.novelty is not None else 0.7
    kinds = list(row.kinds or [])
    evidence = sum(cfg.evidence.get(k, 0.6) for k in kinds) / max(len(kinds), 1)

    total_w = w.significance + w.corroboration + w.velocity + w.breadth + w.novelty + w.evidence
    base = (w.significance * significance + w.corroboration * corroboration + w.velocity * velocity
            + w.breadth * breadth + w.novelty * novelty + w.evidence * evidence) / (total_w or 1)

    half_life = cfg.science_half_life_h if row.science else cfg.half_life_h
    age_h = max((now - row.last_activity).total_seconds() / 3600, 0)
    decay = 0.5 ** (age_h / half_life)

    penalty = 1.0
    if row.promotional is not None and float(row.promotional) >= 0.5:
        penalty *= cfg.promotional_penalty
    if row.social_only and int(row.n_sources) == 1:
        penalty *= cfg.single_source_social_penalty

    score = 100 * base * decay * penalty + int(row.boost or 0)
    if row.override is not None:
        score = max(score, float(row.override))
    score = max(0.0, min(100.0, score))
    breakdown = {
        "significance": round(significance, 3), "corroboration": round(corroboration, 3),
        "velocity": round(velocity, 3), "breadth": round(breadth, 3), "novelty": round(novelty, 3),
        "evidence": round(evidence, 3), "base": round(base, 3), "decay": round(decay, 3),
        "penalty": round(penalty, 3), "boost": int(row.boost or 0), "override": row.override,
        "items": int(row.n_items), "sources": int(row.n_sources), "acceleration_z": round(z, 2),
        "enriched": row.significance is not None,
    }
    return round(score, 2), breakdown


async def score_clusters(session: AsyncSession, cfg: ScoringSettings, all_active: bool = True) -> int:
    now = datetime.now(UTC)
    rows = (await session.execute(_AGG, {"all": all_active})).all()
    updates = []
    for row in rows:
        score, breakdown = compute(row, cfg, now)
        updates.append({"id": row.id, "score": score, "b": breakdown, "n": int(row.n_items),
                        "s": int(row.n_sources), "v": int(row.c3) / 3, "z": breakdown["acceleration_z"]})
    if updates:
        await session.execute(text("""
            UPDATE clusters SET score = :score, score_breakdown = CAST(:b AS jsonb), item_count = :n,
                   source_count = :s, breadth = :s, velocity = :v, acceleration = :z, score_dirty = false
            WHERE id = :id
        """), [{**u, "b": json.dumps(u["b"])} for u in updates])
        await session.commit()
    return len(updates)
