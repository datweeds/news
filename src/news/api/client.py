"""Client API: subscriptions, previews, digests, feedback, stories, places."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from xml.sax.saxutils import escape

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select, text

from news.api.deps import ClientActor, Session
from news.api.pagination import decode_cursor, encode_cursor, page
from news.audit import Actor, audit
from news.config import ScoringSettings
from news.db.models import Cluster, Digest, Feedback, Subscription, SubscriptionOverride
from news.delivery import papers as research
from news.delivery.digest import PhraseVectors, add_extras, build, item_payload
from news.delivery.models import ScheduleDef, SubscriptionDef
from news.delivery.produce import embed_phrases, load_phrases, produce, save_phrases
from news.delivery.schedule import next_run, window_hours
from news.embed import vec
from news.geo import geocode, resolve
from news.llm.budget import OwnLlm
from news.settings_store import get_settings

router = APIRouter(prefix="/v1", tags=["client"])

# ── Schemas ──────────────────────────────────────────────────────────────────


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_ref: str | None = Field(None, max_length=200, description="Your own id for it, e.g. 'society:12'")
    definition: SubscriptionDef
    schedule: ScheduleDef = Field(default_factory=ScheduleDef)
    webhook_url: HttpUrl | None = None
    active: bool = True
    client_id: uuid.UUID | None = Field(None, description="Admin keys only: the client it belongs to")


class SubscriptionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: SubscriptionDef | None = None
    schedule: ScheduleDef | None = None
    webhook_url: HttpUrl | None = None
    active: bool | None = None


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    external_ref: str | None
    definition: dict[str, Any]
    schedule: dict[str, Any]
    webhook_url: str | None
    active: bool
    last_read_at: datetime | None
    last_digest_at: datetime | None
    next_digest_at: datetime | None
    created_at: datetime
    feed_path: str
    llm: dict[str, Any] | None = Field(None, description="Own LLM provider, if any (the key is never returned)")

    @classmethod
    def of(cls, s: Subscription) -> SubscriptionOut:
        return cls(id=s.id, client_id=s.client_id, external_ref=s.external_ref, definition=s.definition,
                   schedule=s.schedule, webhook_url=s.webhook_url, active=s.active, last_read_at=s.last_read_at,
                   last_digest_at=s.last_digest_at, next_digest_at=s.next_digest_at, created_at=s.created_at,
                   feed_path=f"/v1/subscriptions/{s.id}/feed.xml?token={s.feed_token}",
                   llm={**s.llm_config, "has_key": bool(s.llm_secret)} if s.llm_config else None)


class SubscriptionCreated(SubscriptionOut):
    webhook_secret: str | None = Field(None, description="For checking X-News-Signature; shown once")


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: SubscriptionDef
    schedule: ScheduleDef = Field(default_factory=ScheduleDef)


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: uuid.UUID
    action: Literal["hide", "headline"]
    until: datetime | None = None


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: uuid.UUID
    cluster_id: uuid.UUID
    value: Literal["useful", "not_relevant", "seen_elsewhere"]


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _own(session: Session, actor: Actor, subscription_id: uuid.UUID) -> Subscription:
    sub = await session.get(Subscription, subscription_id)
    if sub is None or (actor.role != "admin" and sub.client_id != actor.client_id):
        raise HTTPException(404, "Subscription not found")
    return sub


_WORD = re.compile(r"[\w'’-]+")


async def phrase_feedback(session: Session, sub: SubscriptionDef, phrases: PhraseVectors,
                          window_h: int, cfg: ScoringSettings) -> list[dict[str, Any]]:
    """Guidance on each search phrase: length, places named in it, the
    closest taxonomy topics, and how many recent stories it matches."""
    rows = (await session.execute(text("""
        SELECT title, centroid::text AS c FROM clusters
        WHERE kind = 'story' AND status = 'active' AND item_count > 0
          AND last_activity > now() - make_interval(hours => :h)
    """), {"h": window_h})).all()
    centroids = (np.array([[float(x) for x in r.c.strip("[]").split(",")] for r in rows], dtype=np.float32)
                 if rows else None)
    out = []
    for section in sub.sections:
        for phrase, v in phrases.get(section.key, []):
            words = _WORD.findall(phrase)
            warnings = []
            if len(words) == 1:
                warnings.append("One word is usually too vague. Describe the subject in 3 to 10 words.")
            elif len(words) > 15:
                warnings.append("Long phrases blur together. Split it into separate, shorter phrases.")
            for w in words[1:] if len(words) > 1 else words:
                if w[:1].isupper() and len(w) > 2:
                    place = await geocode(session, w)
                    if place and (place.kind != "place" or (place.population or 0) >= 250_000):
                        warnings.append(f"Mentions a place ({w}). Add places under Locations instead, "
                                        "so the phrase describes the subject only.")
                        break
            topics = (await session.execute(text("""
                SELECT slug, label, round(CAST(1 - (embedding <=> CAST(:v AS vector)) AS numeric), 3) AS sim
                FROM topics WHERE embedding IS NOT NULL ORDER BY embedding <=> CAST(:v AS vector) LIMIT 3
            """), {"v": vec(v)})).all()
            matches, example = 0, None
            if centroids is not None:
                sims = centroids @ np.array(v, dtype=np.float32)
                hits = np.where(sims >= cfg.topic_floor)[0]
                matches = int(len(hits))
                if matches:
                    example = rows[int(hits[np.argmax(sims[hits])])].title
            if not matches:
                warnings.append("No stories in this period match it yet.")
            out.append({"section_key": section.key, "phrase": phrase, "words": len(words), "warnings": warnings,
                        "closest_topics": [{"slug": t.slug, "label": t.label, "similarity": float(t.sim)} for t in topics],
                        "matching_stories": matches, "example": example})
    return out


async def _preview(session: Session, sub_def: SubscriptionDef, schedule: ScheduleDef,
                   phrases: PhraseVectors, subscription_id: uuid.UUID | None) -> dict[str, Any]:
    cfg = await get_settings(session, ScoringSettings)
    window_h = window_hours(schedule)
    draft = await build(session, sub_def, cfg, phrases, window_h, subscription_id=subscription_id)
    await add_extras(session, draft.items)
    headings = {s.key: s.heading for s in sub_def.sections}

    def explain(c: Any) -> dict[str, Any]:
        return {**item_payload(c, headings), "topic_similarity": c.topic_similarity,
                "topic_factor": c.topic_factor, "location_factor": round(c.location_factor, 2)}

    items = [explain(c) for c in draft.items]
    papers = await research.choose(session, sub_def, phrases, cfg, subscription_id=subscription_id)
    return {
        "window_hours": window_h,
        "min_score": sub_def.min_score if sub_def.min_score is not None else cfg.min_score,
        "headline": next((i for i in items if i["is_headline"]), None),
        "sections": [{"key": s.key, "heading": s.heading,
                      "items": [i for i in items if i["section_key"] == s.key and not i["is_headline"]]}
                     for s in sub_def.sections],
        "near_misses": [{"cluster_id": str(c.cluster_id), "title": c.title, "section_heading": headings.get(c.section_key, ""),
                         "score": round(c.score, 1), "relevance": c.relevance, "topic_similarity": c.topic_similarity,
                         "location_factor": round(c.location_factor, 2), "reason": c.reason} for c in draft.near_misses],
        "phrases": await phrase_feedback(session, sub_def, phrases, window_h, cfg),
        # Research already found for any subscription; papers for new phrases are
        # searched within the hour after the subscription is saved
        "papers": [research.payload(p, headings) for p in papers],
        "note": "A preview uses the outlets' own headlines and snippets. Real editions are written up by the "
                "LLM when it is switched on.",
    }


# ── Subscriptions ────────────────────────────────────────────────────────────


@router.post("/subscriptions", response_model=SubscriptionCreated, status_code=201)
async def create_subscription(body: SubscriptionIn, session: Session, actor: ClientActor) -> SubscriptionCreated:
    client_id = body.client_id if actor.role == "admin" else actor.client_id
    if client_id is None:
        raise HTTPException(422, "client_id is required")
    if body.external_ref and await session.scalar(select(Subscription.id).where(
            Subscription.client_id == client_id, Subscription.external_ref == body.external_ref)):
        raise HTTPException(409, f"A subscription with external_ref '{body.external_ref}' exists")
    secret = secrets.token_urlsafe(32) if body.webhook_url else None
    sub = Subscription(client_id=client_id, external_ref=body.external_ref,
                       definition=body.definition.model_dump(), schedule=body.schedule.model_dump(),
                       webhook_url=str(body.webhook_url) if body.webhook_url else None, webhook_secret=secret,
                       feed_token=secrets.token_urlsafe(24), active=body.active,
                       next_digest_at=next_run(body.schedule, datetime.now(UTC)))
    session.add(sub)
    await session.flush()
    await save_phrases(session, sub.id, await embed_phrases(body.definition))
    audit(session, actor, "subscription.create", "subscription", sub.id, after=body.model_dump(mode="json"))
    await session.commit()
    await session.refresh(sub)
    return SubscriptionCreated(**SubscriptionOut.of(sub).model_dump(), webhook_secret=secret)


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(session: Session, actor: ClientActor, external_ref: str | None = None) -> list[SubscriptionOut]:
    stmt = select(Subscription).order_by(Subscription.created_at)
    if actor.role != "admin":
        stmt = stmt.where(Subscription.client_id == actor.client_id)
    if external_ref:
        stmt = stmt.where(Subscription.external_ref == external_ref)
    return [SubscriptionOut.of(s) for s in (await session.scalars(stmt)).all()]


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def get_subscription(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> SubscriptionOut:
    return SubscriptionOut.of(await _own(session, actor, subscription_id))


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(subscription_id: uuid.UUID, body: SubscriptionPatch, session: Session,
                              actor: ClientActor) -> SubscriptionOut:
    sub = await _own(session, actor, subscription_id)
    before = SubscriptionOut.of(sub).model_dump(mode="json")
    if body.definition is not None:
        sub.definition = body.definition.model_dump()
        await save_phrases(session, sub.id, await embed_phrases(body.definition))
    if body.schedule is not None:
        sub.schedule = body.schedule.model_dump()
        sub.next_digest_at = next_run(body.schedule, datetime.now(UTC))
    if "webhook_url" in body.model_fields_set:
        sub.webhook_url = str(body.webhook_url) if body.webhook_url else None
        if sub.webhook_url and not sub.webhook_secret:
            sub.webhook_secret = secrets.token_urlsafe(32)
    if body.active is not None:
        sub.active = body.active
        if body.active and sub.next_digest_at is None:
            sub.next_digest_at = next_run(ScheduleDef.model_validate(sub.schedule), datetime.now(UTC))
    await session.flush()
    audit(session, actor, "subscription.update", "subscription", sub.id, before=before,
          after=SubscriptionOut.of(sub).model_dump(mode="json"))
    await session.commit()
    await session.refresh(sub)
    return SubscriptionOut.of(sub)


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> Response:
    sub = await _own(session, actor, subscription_id)
    audit(session, actor, "subscription.delete", "subscription", sub.id, before=SubscriptionOut.of(sub).model_dump(mode="json"))
    await session.delete(sub)
    await session.commit()
    return Response(status_code=204)


@router.post("/subscriptions/{subscription_id}/webhook-secret")
async def rotate_webhook_secret(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> dict[str, str]:
    sub = await _own(session, actor, subscription_id)
    sub.webhook_secret = secrets.token_urlsafe(32)
    audit(session, actor, "subscription.rotate_secret", "subscription", sub.id)
    await session.commit()
    return {"webhook_secret": sub.webhook_secret}


@router.post("/subscriptions/preview")
async def preview_unsaved(body: PreviewIn, session: Session, _: ClientActor) -> dict[str, Any]:
    """Test a definition before saving it: what an edition would contain now,
    with scores and reasons, and guidance on each phrase. Nothing is stored
    and the LLM is not used, so it costs nothing."""
    return await _preview(session, body.definition, body.schedule, await embed_phrases(body.definition), None)


@router.post("/subscriptions/{subscription_id}/preview")
async def preview_saved(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> dict[str, Any]:
    sub = await _own(session, actor, subscription_id)
    return await _preview(session, SubscriptionDef.model_validate(sub.definition),
                          ScheduleDef.model_validate(sub.schedule), await load_phrases(session, sub.id), sub.id)


@router.post("/subscriptions/{subscription_id}/digests", status_code=201)
async def publish_now(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> dict[str, Any]:
    """Produce an edition now, outside the schedule (at most every 10 minutes)."""
    sub = await _own(session, actor, subscription_id)
    latest = await session.scalar(select(Digest.created_at).where(Digest.subscription_id == sub.id)
                                  .order_by(Digest.id.desc()).limit(1))
    if latest and datetime.now(UTC) - latest < timedelta(minutes=10):
        raise HTTPException(429, "An edition was produced in the last 10 minutes")
    digest = await produce(session, sub, force=True)
    assert digest is not None
    audit(session, actor, "digest.publish_now", "subscription", sub.id, after={"digest": str(digest.id)})
    await session.commit()
    return digest.payload


@router.get("/subscriptions/{subscription_id}/digests")
async def list_digests(subscription_id: uuid.UUID, session: Session, actor: ClientActor,
                       since: datetime | None = None, cursor: str | None = None,
                       limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    sub = await _own(session, actor, subscription_id)
    stmt = select(Digest).where(Digest.subscription_id == sub.id).order_by(Digest.id.desc()).limit(limit + 1)
    if since:
        stmt = stmt.where(Digest.created_at > since)
    if after := decode_cursor(cursor):
        stmt = stmt.where(Digest.id < after)
    rows, more = page((await session.scalars(stmt)).all(), limit)
    return {"digests": [d.payload for d in rows], "next_cursor": encode_cursor(rows[-1].id) if more else None}


@router.get("/digests/{digest_id}")
async def get_digest(digest_id: uuid.UUID, session: Session, actor: ClientActor) -> dict[str, Any]:
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise HTTPException(404, "Digest not found")
    await _own(session, actor, digest.subscription_id)
    return digest.payload


@router.post("/subscriptions/{subscription_id}/read", status_code=204)
async def mark_read(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> Response:
    """Someone opened this subscription's digests. In `auto` mode the LLM
    only runs while subscriptions are being read."""
    sub = await _own(session, actor, subscription_id)
    sub.last_read_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)


@router.post("/subscriptions/{subscription_id}/overrides", status_code=201)
async def add_override(subscription_id: uuid.UUID, body: OverrideIn, session: Session, actor: ClientActor) -> dict[str, Any]:
    """Hide a story from this subscription, or make it the next headline."""
    sub = await _own(session, actor, subscription_id)
    if await session.get(Cluster, body.cluster_id) is None:
        raise HTTPException(404, "Story not found")
    if body.action == "headline":
        await session.execute(text("DELETE FROM subscription_overrides WHERE subscription_id = :s AND action = 'headline'"),
                              {"s": sub.id})
    await session.merge(SubscriptionOverride(subscription_id=sub.id, cluster_id=body.cluster_id,
                                             action=body.action, until=body.until))
    audit(session, actor, f"subscription.{body.action}", "subscription", sub.id, after=body.model_dump(mode="json"))
    await session.commit()
    return body.model_dump(mode="json")


@router.delete("/subscriptions/{subscription_id}/overrides/{cluster_id}/{action}", status_code=204)
async def remove_override(subscription_id: uuid.UUID, cluster_id: uuid.UUID, action: Literal["hide", "headline"],
                          session: Session, actor: ClientActor) -> Response:
    sub = await _own(session, actor, subscription_id)
    await session.execute(text("""
        DELETE FROM subscription_overrides WHERE subscription_id = :s AND cluster_id = :c AND action = :a
    """), {"s": sub.id, "c": cluster_id, "a": action})
    audit(session, actor, f"subscription.un{action}", "subscription", sub.id, after={"cluster_id": str(cluster_id)})
    await session.commit()
    return Response(status_code=204)


@router.post("/feedback", status_code=201)
async def feedback(body: FeedbackIn, session: Session, actor: ClientActor) -> dict[str, str]:
    """useful, not_relevant or seen_elsewhere, from a reader. Kept for tuning
    weights and source trust; hiding a story is a separate override."""
    sub = await _own(session, actor, body.subscription_id)
    if await session.get(Cluster, body.cluster_id) is None:
        raise HTTPException(404, "Story not found")
    session.add(Feedback(subscription_id=sub.id, cluster_id=body.cluster_id, value=body.value))
    await session.commit()
    return {"status": "recorded"}


@router.get("/subscriptions/{subscription_id}/feed.xml", include_in_schema=True)
async def atom_feed(subscription_id: uuid.UUID, session: Session, token: str) -> Response:
    """Private Atom feed of the subscription's recent digests (no API key;
    the token in the subscription's feed_path)."""
    sub = await session.get(Subscription, subscription_id)
    if sub is None or not hmac.compare_digest(sub.feed_token, token):
        raise HTTPException(404, "Feed not found")
    digests = (await session.scalars(select(Digest).where(Digest.subscription_id == sub.id)
                                     .order_by(Digest.id.desc()).limit(10))).all()
    name = escape(SubscriptionDef.model_validate(sub.definition).name)
    entries = []
    for d in digests:
        for item in d.payload.get("items", []):
            link = next((s["url"] for s in item.get("top_sources", []) if s.get("url")), "")
            entries.append(
                f"<entry><id>urn:news:{d.id}:{item['cluster_id']}</id><title>{escape(item['title'])}</title>"
                f"<updated>{d.created_at.isoformat()}</updated>"
                f"<link href=\"{escape(link)}\"/><summary>{escape(item.get('summary') or '')}</summary></entry>")
    updated = digests[0].created_at.isoformat() if digests else datetime.now(UTC).isoformat()
    xml = (f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
           f"<id>urn:news:subscription:{sub.id}</id><title>{name}</title><updated>{updated}</updated>"
           + "".join(entries) + "</feed>")
    return Response(xml, media_type="application/atom+xml")


# ── Stories, topics, places ──────────────────────────────────────────────────


@router.get("/clusters/{cluster_id}")
async def get_cluster(cluster_id: uuid.UUID, session: Session, _: ClientActor) -> dict[str, Any]:
    """A story with all its sources and its timeline."""
    c = await session.get(Cluster, cluster_id)
    if c is None:
        raise HTTPException(404, "Story not found")
    items = (await session.execute(text("""
        SELECT i.id, i.title, i.url, COALESCE(i.raw->>'outlet', s.name) AS outlet, i.published_at, i.kind,
               i.duplicate_of IS NOT NULL AS duplicate, i.image_url
        FROM cluster_items ci JOIN items i ON i.id = ci.item_id JOIN source_instances s ON s.id = i.source_instance_id
        WHERE ci.cluster_id = :c ORDER BY i.published_at DESC NULLS LAST LIMIT 100
    """), {"c": cluster_id})).all()
    timeline = (await session.execute(text("""
        SELECT bucket_start, items FROM cluster_timeseries WHERE cluster_id = :c ORDER BY bucket_start
    """), {"c": cluster_id})).all()
    return {
        "cluster_id": str(c.id), "kind": c.kind, "title": c.title, "summary": c.summary,
        "why_it_matters": c.why_it_matters, "image_url": c.image_url, "status": c.status, "score": c.score,
        "score_breakdown": c.score_breakdown, "first_seen": c.first_seen, "last_activity": c.last_activity,
        "item_count": c.item_count, "source_count": c.source_count,
        "sources": [{"title": i.title, "url": i.url, "outlet": i.outlet, "published_at": i.published_at,
                     "kind": i.kind, "duplicate": i.duplicate, "image_url": i.image_url} for i in items],
        "timeline": [{"at": t.bucket_start, "items": t.items} for t in timeline],
    }


@router.get("/topics")
async def topics(session: Session, _: ClientActor, q: str | None = Query(None, max_length=100)) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT t.slug, t.label, t.description, p.slug AS parent FROM topics t LEFT JOIN topics p ON p.id = t.parent_id
        WHERE CAST(:q AS text) IS NULL OR t.label ILIKE '%' || CAST(:q AS text) || '%' OR t.slug ILIKE '%' || CAST(:q AS text) || '%'
        ORDER BY t.slug
    """), {"q": q})).all()
    return [{"slug": r.slug, "label": r.label, "description": r.description, "parent": r.parent} for r in rows]


@router.get("/locations/resolve")
async def resolve_location(session: Session, _: ClientActor, q: str = Query(min_length=2, max_length=100)) -> list[dict[str, Any]]:
    """Place name → candidates, ready to use as a subscription location."""
    out = []
    for p in await resolve(session, q):
        out.append({"name": p.name, "kind": p.kind, "geonames_id": p.geonames_id, "country": p.country,
                    "admin1": p.admin1, "lat": p.lat, "lon": p.lon, "population": p.population})
    return out


# ── A subscription's own LLM, and its usage ──────────────────────────────────


class OwnLlmIn(OwnLlm):
    api_key: str | None = Field(None, max_length=500, description="Write-only; omit to keep the stored key")


class LlmTestIn(OwnLlm):
    api_key: str | None = Field(None, max_length=500)
    subscription_id: uuid.UUID | None = Field(None, description="Use this subscription's stored key")


def _own_out(sub: Subscription) -> dict[str, Any] | None:
    if not sub.llm_config:
        return None
    return {**sub.llm_config, "has_key": bool(sub.llm_secret)}


@router.put("/subscriptions/{subscription_id}/llm")
async def set_own_llm(subscription_id: uuid.UUID, body: OwnLlmIn, session: Session, actor: ClientActor) -> dict[str, Any]:
    """Use the subscriber's own provider for work done only for it (writing
    its digests). Keys are stored encrypted and never returned."""
    from news.secrets import SecretsUnavailable, encrypt

    sub = await _own(session, actor, subscription_id)
    if body.adapter == "openai_compat" and not body.base_url:
        raise HTTPException(422, "An OpenAI-compatible provider needs its base URL")
    if body.api_key:
        try:
            sub.llm_secret = encrypt(body.api_key.strip())
        except SecretsUnavailable as e:
            raise HTTPException(503, str(e)) from e
    elif not sub.llm_secret and body.adapter == "anthropic":
        raise HTTPException(422, "An API key is needed")
    sub.llm_config = body.model_dump(exclude={"api_key"})
    audit(session, actor, "subscription.set_llm", "subscription", sub.id, after=sub.llm_config)
    await session.commit()
    return _own_out(sub) or {}


@router.delete("/subscriptions/{subscription_id}/llm", status_code=204)
async def clear_own_llm(subscription_id: uuid.UUID, session: Session, actor: ClientActor) -> Response:
    sub = await _own(session, actor, subscription_id)
    sub.llm_config, sub.llm_secret = None, None
    audit(session, actor, "subscription.clear_llm", "subscription", sub.id)
    await session.commit()
    return Response(status_code=204)


_TEST_ITEMS = [
    ("Scottish Government announces new heat pump grants for rural homes",
     "Households in rural Scotland will be able to claim up to £9,000 towards a heat pump from January."),
    ("Edinburgh trams extension approved by council",
     "The City of Edinburgh Council approved plans to extend the tram line north to Granton by 2028."),
]


@router.post("/llm/test")
async def test_llm(body: LlmTestIn, session: Session, actor: ClientActor) -> dict[str, Any]:
    """Test a provider and model: run the item-tagging task on two sample
    items and check the reply is valid. Costs a fraction of a cent on paid
    providers."""
    import time as _time
    from types import SimpleNamespace

    from pydantic import ValidationError

    from news.connectors.http import FetchError, check_url
    from news.llm.budget import client_for, cost_usd
    from news.llm.client import LLMError, parse_json
    from news.pipeline.enrich import ExtractOut, build_prompt
    from news.prompts import load
    from news.secrets import SecretsUnavailable, decrypt

    key = body.api_key
    if not key and body.subscription_id:
        sub = await _own(session, actor, body.subscription_id)
        if sub.llm_secret:
            try:
                key = decrypt(sub.llm_secret)
            except SecretsUnavailable as e:
                return {"ok": False, "error": str(e)}
    cfg = OwnLlm.model_validate(body.model_dump(exclude={"api_key", "subscription_id"})).model_cfg()
    rows = [SimpleNamespace(outlet="Test", kind="news", title=t, text_snippet=s) for t, s in _TEST_ITEMS]
    started = _time.monotonic()
    try:
        if cfg.base_url and os.environ.get("NEWS_ALLOW_LOCAL_LLM") != "1":
            await check_url(cfg.base_url)
        client = client_for(cfg, 60, api_key=key or "")
        result = await client.complete(load("extract").text, build_prompt(rows), 1500)
    except FetchError as e:
        return {"ok": False, "error": f"That address isn't allowed or can't be reached: {e}"}
    except LLMError as e:
        return {"ok": False, "error": f"The provider refused or failed: {e}"[:500]}
    latency_ms = round((_time.monotonic() - started) * 1000)
    try:
        parsed = ExtractOut.model_validate(parse_json(result.text))
        valid = len({i.id for i in parsed.items} & {0, 1}) == 2
    except (LLMError, ValidationError):
        valid = False
    return {
        "ok": valid, "model": result.model, "latency_ms": latency_ms,
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
        "cost_usd": round(cost_usd(cfg, result.tokens_in, result.tokens_out), 5),
        "error": None if valid else "Connected, but the reply wasn't valid structured output. Try a larger or more capable model.",
    }


@router.get("/subscriptions/{subscription_id}/usage")
async def subscription_usage(subscription_id: uuid.UUID, session: Session, actor: ClientActor,
                             days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """This subscription's LLM spend: its share of the platform's, and its
    own provider's; plus whether the platform's LLM is available to it now."""
    from news.config import LlmSettings
    from news.llm.budget import gate, own_spent_this_month

    sub = await _own(session, actor, subscription_id)
    rows = (await session.execute(text("""
        SELECT day, task, scope, tokens_in, tokens_out, cost_usd FROM llm_usage_subscriptions
        WHERE subscription_id = :s AND day > current_date - CAST(:d AS integer) ORDER BY day DESC, task
    """), {"s": sub.id, "d": days})).all()
    llm = await get_settings(session, LlmSettings)
    g = await gate(session, llm, subscription_id=sub.id)
    today = datetime.now(UTC).date()
    month = today.replace(day=1)

    def total(scope: str, since: Any) -> float:
        return round(sum(float(r.cost_usd) for r in rows if r.scope == scope and r.day >= since), 4)

    return {
        "days": [{"day": r.day, "task": r.task, "scope": r.scope, "tokens_in": r.tokens_in,
                  "tokens_out": r.tokens_out, "cost_usd": float(r.cost_usd)} for r in rows],
        "platform": {"today_usd": total("platform", today), "month_usd": total("platform", month),
                     "mode": llm.mode, "available": g.allowed, "reason": g.reason},
        "own": {"configured": bool(sub.llm_config), "config": _own_out(sub),
                "month_usd": round(await own_spent_this_month(session, sub.id), 4)},
    }
