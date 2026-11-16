"""Turning a subscription into a stored digest, and its search phrases into
embeddings."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import LlmSettings, ScoringSettings
from news.db.models import Delivery, Digest, DigestItem, Subscription, SubscriptionPhrase
from news.delivery.digest import PhraseVectors, add_extras, build, item_payload
from news.delivery.models import ScheduleDef, SubscriptionDef
from news.delivery.schedule import edition_date, window_hours
from news.delivery.writer import write
from news.embed import embed_queries
from news.ids import uuid7
from news.settings_store import get_settings

log = logging.getLogger(__name__)


async def embed_phrases(sub: SubscriptionDef) -> PhraseVectors:
    pairs = [(s.key, p) for s in sub.sections for p in s.phrases]
    vectors = await embed_queries([p for _, p in pairs])
    out: PhraseVectors = {}
    for (key, phrase), v in zip(pairs, vectors, strict=True):
        out.setdefault(key, []).append((phrase, v))
    return out


async def save_phrases(session: AsyncSession, subscription_id: uuid.UUID, phrases: PhraseVectors) -> None:
    await session.execute(delete(SubscriptionPhrase).where(SubscriptionPhrase.subscription_id == subscription_id))
    for key, pairs in phrases.items():
        for phrase, v in pairs:
            session.add(SubscriptionPhrase(subscription_id=subscription_id, section_key=key, phrase=phrase, embedding=v))


async def load_phrases(session: AsyncSession, subscription_id: uuid.UUID) -> PhraseVectors:
    rows = (await session.execute(text("""
        SELECT section_key, phrase, embedding::text AS v FROM subscription_phrases WHERE subscription_id = :s
    """), {"s": subscription_id})).all()
    out: PhraseVectors = {}
    for r in rows:
        out.setdefault(r.section_key, []).append((r.phrase, [float(x) for x in r.v.strip("[]").split(",")]))
    return out


def digest_payload(digest: Digest, sub: Subscription, sub_def: SubscriptionDef, schedule: ScheduleDef,
                   items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(digest.id),
        "subscription_id": str(sub.id),
        "external_ref": sub.external_ref,
        "name": sub_def.name,
        "cadence": schedule.cadence,
        "edition_date": digest.edition_date.isoformat(),
        "window_start": digest.window_start.isoformat(),
        "window_end": digest.window_end.isoformat(),
        "overview": digest.overview,
        "llm_written": digest.llm_written,
        "sections": [{"key": s.key, "heading": s.heading} for s in sub_def.sections],
        "items": items,
        "created_at": datetime.now(UTC).isoformat(),
    }


async def produce(session: AsyncSession, sub: Subscription, now: datetime | None = None,
                  force: bool = False) -> Digest | None:
    """Build, write and store a digest for the subscription, and queue its
    delivery. Returns None when there is nothing to send and the subscription
    doesn't want empty digests (unless `force`)."""
    now = now or datetime.now(UTC)
    sub_def = SubscriptionDef.model_validate(sub.definition)
    schedule = ScheduleDef.model_validate(sub.schedule)
    scoring = await get_settings(session, ScoringSettings)
    llm = await get_settings(session, LlmSettings)

    draft = await build(session, sub_def, scoring, await load_phrases(session, sub.id),
                        window_hours(schedule), subscription_id=sub.id, now=now)
    if not draft.items and not sub_def.send_empty and not force:
        log.info("nothing to send", extra={"subscription": str(sub.id)})
        return None
    headings = {s.key: s.heading for s in sub_def.sections}
    if sub_def.use_llm:
        overview, llm_written, items = await write(session, llm, sub_def.name, draft.items, headings, sub.id, sub)
    else:
        overview, llm_written, items = None, False, draft.items
    await add_extras(session, items)

    digest = Digest(id=uuid7(), subscription_id=sub.id, edition_date=edition_date(schedule, now),
                    window_start=draft.window_start, window_end=draft.window_end, overview=overview,
                    llm_written=llm_written, status="ready", payload={})
    payload_items = [item_payload(c, headings) for c in items]
    digest.payload = digest_payload(digest, sub, sub_def, schedule, payload_items)
    session.add(digest)
    await session.flush()
    for n, c in enumerate(items):
        session.add(DigestItem(digest_id=digest.id, cluster_id=c.cluster_id, position=n, section_key=c.section_key,
                               is_headline=c.is_headline, is_update=c.is_update, score=c.score,
                               relevance=c.relevance, item_count=c.item_count))
    if sub.webhook_url:
        session.add(Delivery(digest_id=digest.id, method="webhook", next_retry_at=now))
    await session.commit()
    log.info("digest produced", extra={"subscription": str(sub.id), "digest": str(digest.id),
                                       "items": len(items), "llm_written": llm_written})
    return digest


async def latest_digests(session: AsyncSession, subscription_id: uuid.UUID, limit: int) -> list[Digest]:
    return list((await session.scalars(
        select(Digest).where(Digest.subscription_id == subscription_id).order_by(Digest.id.desc()).limit(limit)
    )).all())
