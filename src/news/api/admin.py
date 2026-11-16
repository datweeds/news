"""Admin API: settings (including the LLM switch), audit log, API keys,
config reload."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select, text

from news.api.deps import Admin, Session, Viewer
from news.api.pagination import decode_cursor, encode_cursor, page
from news.api.schemas import AuditOut, AuditPage, KeyCreated, KeyIn, KeyOut
from news.audit import audit
from news.config import SETTINGS_MODELS, LlmSettings, seed
from news.db.models import ApiKey, AuditLog, Client, Cluster, LlmUsage, Setting
from news.env import env
from news.keys import create_key
from news.llm.budget import gate, spent_today
from news.settings_store import get_settings, invalidate

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ── Settings ─────────────────────────────────────────────────────────────────


async def _current(session: Session, name: str) -> dict[str, Any]:
    row = await session.get(Setting, name)
    return SETTINGS_MODELS[name].model_validate(row.value if row else {}).model_dump()


@router.get("/settings")
async def all_settings(session: Session, _: Viewer) -> dict[str, dict[str, Any]]:
    return {name: await _current(session, name) for name in SETTINGS_MODELS}


@router.get("/settings/{name}")
async def get_setting(name: str, session: Session, _: Viewer) -> dict[str, Any]:
    if name not in SETTINGS_MODELS:
        raise HTTPException(404, f"Unknown settings section (have: {', '.join(SETTINGS_MODELS)})")
    return await _current(session, name)


@router.put("/settings/{name}")
async def put_setting(name: str, session: Session, actor: Admin,
                      body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Change a settings section. Fields not given keep their current value.
    E.g. PUT /v1/admin/settings/llm {"mode": "off"} switches the LLM off."""
    if name not in SETTINGS_MODELS:
        raise HTTPException(404, f"Unknown settings section (have: {', '.join(SETTINGS_MODELS)})")
    before = await _current(session, name)
    try:
        value = SETTINGS_MODELS[name].model_validate({**before, **body}).model_dump()
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False)) from e
    row = await session.get(Setting, name)
    if row is None:
        session.add(Setting(name=name, value=value))
    else:
        row.value = value
    audit(session, actor, "settings.update", "settings", name, before=before, after=value)
    await session.commit()
    invalidate(name)
    return value


@router.post("/config/reload")
async def reload_config(session: Session, actor: Admin, overwrite: bool = False) -> dict[str, int]:
    """Load the YAML config files: adds new sources, settings and topics;
    with overwrite=true, the files also replace what is in the database."""
    try:
        counts = await seed(session, env().config_dir, overwrite=overwrite)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    audit(session, actor, "config.reload", "config", None, after={"overwrite": overwrite, **counts})
    await session.commit()
    invalidate()
    return counts


# ── Audit log ────────────────────────────────────────────────────────────────


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    session: Session,
    _: Viewer,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> AuditPage:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if after := decode_cursor(cursor):
        stmt = stmt.where(AuditLog.id < after)
    rows, more = page((await session.scalars(stmt)).all(), limit)
    return AuditPage(items=[AuditOut.model_validate(r) for r in rows],
                     next_cursor=encode_cursor(rows[-1].id) if more else None)


# ── API keys ─────────────────────────────────────────────────────────────────


@router.get("/api-keys", response_model=list[KeyOut])
async def list_keys(session: Session, _: Admin) -> list[KeyOut]:
    rows = (await session.scalars(select(ApiKey).order_by(ApiKey.created_at))).all()
    return [KeyOut.model_validate(k) for k in rows]


@router.post("/api-keys", response_model=KeyCreated, status_code=201)
async def new_key(body: KeyIn, session: Session, actor: Admin) -> KeyCreated:
    if body.role == "client" and body.client_id is None:
        raise HTTPException(422, "Client keys need a client_id")
    if body.client_id and await session.get(Client, body.client_id) is None:
        raise HTTPException(404, "Client not found")
    row, key = await create_key(session, body.name, body.role, body.client_id)
    audit(session, actor, "api_key.create", "api_key", row.id, after={"name": row.name, "role": row.role})
    await session.commit()
    await session.refresh(row)
    return KeyCreated(**KeyOut.model_validate(row).model_dump(), key=key)


@router.delete("/api-keys/{key_id}", response_model=KeyOut)
async def revoke_key(key_id: uuid.UUID, session: Session, actor: Admin) -> KeyOut:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(404, "Key not found")
    if row.id == actor.key_id:
        raise HTTPException(409, "You can't revoke the key you're using")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        audit(session, actor, "api_key.revoke", "api_key", row.id, before={"name": row.name, "role": row.role})
        await session.commit()
    return KeyOut.model_validate(row)


@router.get("/clients")
async def list_clients(session: Session, _: Admin) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Client).order_by(Client.name))).all()
    return [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in rows]


@router.post("/clients", status_code=201)
async def create_client(session: Session, actor: Admin,
                        name: str = Body(..., embed=True, min_length=1, max_length=200)) -> dict[str, Any]:
    if await session.scalar(select(Client.id).where(Client.name == name)):
        raise HTTPException(409, "A client with that name exists")
    client = Client(name=name)
    session.add(client)
    await session.flush()
    audit(session, actor, "client.create", "client", client.id, after={"name": name})
    await session.commit()
    return {"id": client.id, "name": client.name}


# ── Stories and LLM spend ────────────────────────────────────────────────────


@router.get("/clusters")
async def ranked_clusters(session: Session, _: Viewer, status: str = "active",
                          q: str | None = Query(None, max_length=200),
                          limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Stories ranked by importance, with the score breakdown."""
    stmt = select(Cluster).where(Cluster.status == status).order_by(Cluster.score.desc()).limit(limit)
    if q:
        stmt = stmt.where(Cluster.title.ilike(f"%{q}%"))
    return [{"id": c.id, "title": c.title, "score": c.score, "breakdown": c.score_breakdown,
             "items": c.item_count, "sources": c.source_count, "first_seen": c.first_seen,
             "last_activity": c.last_activity, "summary": c.summary, "image_url": c.image_url}
            for c in (await session.scalars(stmt)).all()]


@router.get("/metrics/llm")
async def llm_metrics(session: Session, _: Viewer, days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
    llm = await get_settings(session, LlmSettings)
    rows = (await session.scalars(select(LlmUsage).where(
        LlmUsage.day >= datetime.now(UTC).date() - timedelta(days=days)).order_by(LlmUsage.day.desc()))).all()
    g = await gate(session, llm)
    by_sub = (await session.execute(text("""
        SELECT s.id, s.external_ref, s.definition->>'name' AS name, s.active,
               COALESCE((s.definition->>'use_llm')::boolean, true) AS use_llm, s.llm_config IS NOT NULL AS own,
               COALESCE(sum(u.cost_usd) FILTER (WHERE u.scope = 'platform' AND u.day = current_date), 0) AS platform_today,
               COALESCE(sum(u.cost_usd) FILTER (WHERE u.scope = 'platform'), 0) AS platform_period,
               COALESCE(sum(u.cost_usd) FILTER (WHERE u.scope = 'own'), 0) AS own_period
        FROM subscriptions s
        LEFT JOIN llm_usage_subscriptions u ON u.subscription_id = s.id AND u.day > current_date - CAST(:d AS integer)
        GROUP BY s.id ORDER BY platform_period DESC, s.created_at
    """), {"d": days})).all()
    return {
        "by_subscription": [{"id": r.id, "external_ref": r.external_ref, "name": r.name, "active": r.active,
                             "use_llm": r.use_llm, "own_provider": r.own,
                             "platform_today_usd": float(r.platform_today), "platform_period_usd": float(r.platform_period),
                             "own_period_usd": float(r.own_period)} for r in by_sub],
        "mode": llm.mode, "daily_budget_usd": llm.daily_budget_usd,
        "spent_today_usd": round(await spent_today(session), 4),
        "llm_available_now": g.allowed, "reason": g.reason,
        "usage": [{"day": u.day, "model": u.model, "task": u.task, "tokens_in": u.tokens_in,
                   "tokens_out": u.tokens_out, "cost_usd": float(u.cost_usd)} for u in rows],
    }
