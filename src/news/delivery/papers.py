"""The research part of an edition: scientific papers and preprints found by
the OpenAlex connector, published in the last `papers_days` days, matched to
the subscription's sections by meaning, and each sent to a subscription only
once. One LLM call per edition writes a plain-English line for each."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import LlmSettings, ScoringSettings
from news.db.models import Subscription
from news.delivery.digest import PhraseVectors
from news.delivery.models import SubscriptionDef
from news.llm.budget import run_own, run_task
from news.llm.client import LLMError, parse_json
from news.prompts import data, load

log = logging.getLogger(__name__)
POOL = 3000  # most recent research items considered per edition


@dataclass
class Paper:
    item_id: uuid.UUID
    title: str
    url: str | None
    published_at: datetime | None
    kind: str  # paper | preprint
    raw: dict[str, Any]
    section_key: str = ""
    similarity: float = 0.0
    rank: float = 0.0
    summary: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def abstract(self) -> str:
        return str(self.raw.get("abstract") or "")


_POOL_SQL = text("""
    SELECT i.id, i.title, i.url, i.published_at, i.kind, i.raw, i.embedding::text AS emb
    FROM items i
    WHERE i.raw ? 'openalex_id' AND i.duplicate_of IS NULL AND i.embedding IS NOT NULL
      AND i.published_at >= :since
      AND (CAST(:sub AS uuid) IS NULL OR NOT EXISTS (
            SELECT 1 FROM subscription_papers sp WHERE sp.subscription_id = CAST(:sub AS uuid) AND sp.item_id = i.id))
    ORDER BY i.published_at DESC
    LIMIT :n
""")


async def choose(session: AsyncSession, sub: SubscriptionDef, phrases: PhraseVectors, cfg: ScoringSettings,
                 subscription_id: uuid.UUID | None = None, now: datetime | None = None) -> list[Paper]:
    """The papers for this edition, best first. Empty unless the subscription includes papers."""
    if not sub.include_papers:
        return []
    now = now or datetime.now(UTC)
    rows = (await session.execute(_POOL_SQL, {
        "since": now - timedelta(days=cfg.papers_days), "sub": str(subscription_id) if subscription_id else None,
        "n": POOL})).all()
    if not rows:
        return []
    vecs = np.array([[float(x) for x in r.emb.strip("[]").split(",")] for r in rows], dtype=np.float32)
    best_sim = np.full(len(rows), -1.0, dtype=np.float32)
    best_key = [""] * len(rows)
    section_phrases: dict[str, set[str]] = {}
    for section in sub.sections:
        pv = phrases.get(section.key, [])
        section_phrases[section.key] = {p.lower() for p, _ in pv}
        if not pv:
            continue
        sims = (vecs @ np.array([v for _, v in pv], dtype=np.float32).T).max(axis=1)
        for n, s in enumerate(sims):
            if s > best_sim[n]:
                best_sim[n], best_key[n] = float(s), section.key

    by_key = {s.key: s for s in sub.sections}
    out: list[Paper] = []
    for n, r in enumerate(rows):
        raw = dict(r.raw or {})
        key, sim = best_key[n], float(best_sim[n])
        # Found by one of this subscription's own phrases: a little more lenient
        found_here = str(raw.get("found_by", "")).lower() in section_phrases.get(key, set())
        if not key or sim < (cfg.paper_floor - (0.05 if found_here else 0.0)):
            continue
        section = by_key[key]
        body = f"{r.title} {raw.get('abstract', '')}".lower()
        if section.must_include and not any(w.lower() in body for w in section.must_include):
            continue
        if section.must_exclude and any(w.lower() in body for w in section.must_exclude):
            continue
        age_days = (now - r.published_at).total_seconds() / 86400 if r.published_at else cfg.papers_days
        freshness = max(0.0, 1 - age_days / cfg.papers_days)
        citations = min(int(raw.get("cited_by") or 0), 50) / 50
        rank = 0.7 * sim + 0.2 * freshness + 0.1 * citations
        out.append(Paper(item_id=r.id, title=r.title, url=r.url, published_at=r.published_at, kind=r.kind,
                         raw=raw, section_key=key, similarity=round(sim, 3), rank=round(rank, 4)))
    out.sort(key=lambda p: p.rank, reverse=True)
    return out[: sub.max_papers or cfg.papers_max]


class _Summary(BaseModel):
    id: str
    summary: str = Field(min_length=1, max_length=600)


class _SummaryOut(BaseModel):
    papers: list[_Summary] = Field(default_factory=list)


def build_prompt(papers: list[Paper]) -> str:
    parts = []
    for n, p in enumerate(papers):
        kind = "preprint, not yet peer reviewed" if p.kind == "preprint" else "peer-reviewed paper"
        parts.append(f'<paper id="{n}" published_in="{data(p.raw.get("venue") or "")}" kind="{kind}">\n'
                     f"<title>{data(p.title)}</title>\n<abstract>{data(p.abstract[:2000])}</abstract>\n</paper>")
    return "\n\n".join(parts)


async def summarise(session: AsyncSession, llm: LlmSettings, edition: str, papers: list[Paper],
                    sub: Subscription | None) -> bool:
    """Write each paper's plain-English line in one call. False (papers keep
    no summary; editions show the abstract's opening) if not allowed or it fails."""
    if not papers:
        return False
    system = load("paper_summary").text.replace("{edition}", edition)
    user = build_prompt(papers)
    try:
        if sub is not None and sub.llm_config:
            result = await run_own(session, sub, "paper_summary", system, user, llm.timeout_s)
        else:
            result = await run_task(session, llm, "paper_summary", system, user,
                                    subscription_id=sub.id if sub is not None else None)
    except LLMError:
        return False
    if result is None:
        return False
    try:
        out = _SummaryOut.model_validate(parse_json(result.text))
    except (LLMError, ValidationError) as e:
        log.warning("invalid paper summary output", extra={"error": str(e)[:300]})
        return False
    for s in out.papers:
        if s.id.isdigit() and int(s.id) < len(papers):
            papers[int(s.id)].summary = s.summary.strip()
    return any(p.summary for p in papers)


def opening(abstract: str, limit: int = 300) -> str:
    """The abstract's first sentences, for editions without an AI summary."""
    if len(abstract) <= limit:
        return abstract
    cut = abstract[:limit]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…"


def payload(p: Paper, headings: dict[str, str]) -> dict[str, Any]:
    raw = p.raw
    authors = list(raw.get("authors") or [])
    return {
        "item_id": str(p.item_id),
        "kind": p.kind,
        "preprint": p.kind == "preprint",
        "title": p.title,
        "summary": p.summary,
        "abstract_opening": opening(p.abstract),
        "url": p.url,
        "doi": raw.get("doi"),
        "venue": raw.get("venue"),
        "authors": authors[:3],
        "author_count": int(raw.get("author_count") or len(authors)),
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "open_access": bool(raw.get("open_access")),
        "cited_by": int(raw.get("cited_by") or 0),
        "section_key": p.section_key,
        "section_heading": headings.get(p.section_key, ""),
        "similarity": p.similarity,
    }


async def record_sent(session: AsyncSession, subscription_id: uuid.UUID, digest_id: uuid.UUID,
                      papers: list[Paper]) -> None:
    for p in papers:
        await session.execute(text("""
            INSERT INTO subscription_papers (subscription_id, item_id, digest_id, section_key, similarity, summary)
            VALUES (:s, :i, :d, :k, :sim, :sum) ON CONFLICT DO NOTHING
        """), {"s": subscription_id, "i": p.item_id, "d": digest_id, "k": p.section_key,
               "sim": p.similarity, "sum": p.summary})
