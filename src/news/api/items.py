"""Admin API: items, including manual items."""

from __future__ import annotations

import csv
import io
import uuid
from typing import Any

import orjson
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import select

from news.api.deps import Moderator, Session, Viewer
from news.api.pagination import decode_cursor, encode_cursor, page
from news.api.schemas import BulkResult, ItemDetailOut, ItemOut, ItemPage, ManualItemIn, ManualItemPatch
from news.audit import Actor, audit
from news.config import AppSettings
from news.connectors.base import RawItem
from news.db.models import Item, SourceInstance
from news.ids import uuid7
from news.pipeline.store import store_raw
from news.pipeline.text import canonical_url, clean_html, simhash64, truncate
from news.settings_store import get_settings

router = APIRouter(prefix="/v1/admin/items", tags=["admin: items"])

BULK_MAX = 500
CSV_LIST_FIELDS = ("topics", "locations", "target_subscriptions")


async def _source_names(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(SourceInstance.id, SourceInstance.name).where(SourceInstance.id.in_(ids)))
    return {r.id: r.name for r in rows}


async def _out(session: Session, items: list[Item]) -> list[ItemOut]:
    names = await _source_names(session, {i.source_instance_id for i in items})
    return [ItemOut.of(i, names.get(i.source_instance_id, "?")) for i in items]


@router.get("", response_model=ItemPage)
async def list_items(
    session: Session,
    _: Viewer,
    source: str | None = None,
    kind: str | None = None,
    q: str | None = Query(None, max_length=200, description="Words in the title"),
    include_duplicates: bool = False,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> ItemPage:
    stmt = select(Item).order_by(Item.id.desc()).limit(limit + 1)
    if source:
        stmt = stmt.join(SourceInstance, SourceInstance.id == Item.source_instance_id).where(SourceInstance.name == source)
    if kind:
        stmt = stmt.where(Item.kind == kind)
    if q:
        stmt = stmt.where(Item.title.ilike(f"%{q}%"))
    if not include_duplicates:
        stmt = stmt.where(Item.duplicate_of.is_(None))
    if after := decode_cursor(cursor):
        stmt = stmt.where(Item.id < after)
    rows, more = page((await session.scalars(stmt)).all(), limit)
    return ItemPage(items=await _out(session, rows), next_cursor=encode_cursor(rows[-1].id) if more else None)


@router.get("/{item_id}", response_model=ItemDetailOut)
async def get_item(item_id: uuid.UUID, session: Session, _: Viewer) -> ItemDetailOut:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    dups = list((await session.scalars(
        select(Item).where(Item.duplicate_of == item.id).order_by(Item.ingested_at).limit(100)
    )).all())
    [out] = await _out(session, [item])
    return ItemDetailOut(**out.model_dump(), duplicates=await _out(session, dups))


async def _manual_source_id(session: Session) -> uuid.UUID:
    src_id = await session.scalar(select(SourceInstance.id).where(SourceInstance.type == "manual").limit(1))
    if src_id is None:
        raise HTTPException(500, "Manual source missing; run `news migrate`")
    return src_id


def _manual_raw(body: ManualItemIn, source_id: uuid.UUID) -> RawItem:
    return RawItem(
        source_instance=str(source_id),
        external_id=f"manual:{uuid7()}",
        url=str(body.url) if body.url else None,
        title=body.title,
        text=body.summary or body.body or "",
        kind=body.kind,
        raw={"manual": body.model_dump(mode="json", exclude={"title", "url", "body"})},
    )


async def _create_manual(session: Session, body: ManualItemIn, actor: Actor, source_id: uuid.UUID) -> Item | None:
    cfg = await get_settings(session, AppSettings)
    raw = _manual_raw(body, source_id)
    # is_manual marks who entered it; kind says what it is
    item = await store_raw(session, raw.model_copy(update={"kind": "manual"}), cfg, created_by=actor.key_id)
    if item is None:
        return None
    if body.kind != "manual":
        item.kind = body.kind
    audit(session, actor, "item.create_manual", "item", item.id, after=body.model_dump(mode="json"))
    await session.commit()
    return item


@router.post("", response_model=ItemOut, status_code=201)
async def create_manual_item(body: ManualItemIn, session: Session, actor: Moderator) -> ItemOut:
    item = await _create_manual(session, body, actor, await _manual_source_id(session))
    if item is None:
        raise HTTPException(409, "Item already exists")
    [out] = await _out(session, [item])
    return out


def _parse_bulk(content_type: str, data: bytes) -> list[dict[str, Any]]:
    if "json" in content_type:
        parsed = orjson.loads(data)
        if not isinstance(parsed, list):
            raise HTTPException(422, "Expected a JSON array of items")
        return [p for p in parsed if isinstance(p, dict)]
    if "csv" in content_type:
        rows = []
        for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
            clean: dict[str, Any] = {k.strip(): v.strip() for k, v in row.items() if k and v and v.strip()}
            for field in CSV_LIST_FIELDS:
                if field in clean:
                    clean[field] = [x.strip() for x in clean[field].split(";") if x.strip()]
            rows.append(clean)
        return rows
    raise HTTPException(415, "Send application/json or text/csv")


@router.post("/bulk", response_model=BulkResult)
async def bulk_manual_items(request: Request, session: Session, actor: Moderator) -> BulkResult:
    """Bulk import: a JSON array of manual items, or CSV with a header row
    (title, url, summary, kind, topics, locations, importance_override,
    pin_until, editor_note; lists separated by semicolons)."""
    rows = _parse_bulk(request.headers.get("content-type", ""), await request.body())
    if len(rows) > BULK_MAX:
        raise HTTPException(413, f"At most {BULK_MAX} items per upload")
    source_id = await _manual_source_id(session)
    result = BulkResult(created=0, skipped=0, errors=[])
    for n, row in enumerate(rows, start=1):
        try:
            body = ManualItemIn.model_validate(row)
        except ValidationError as e:
            result.errors.append(f"Row {n}: {e.errors(include_url=False)[0]['msg']}")
            continue
        if await _create_manual(session, body, actor, source_id):
            result.created += 1
        else:
            result.skipped += 1
    return result


@router.patch("/{item_id}", response_model=ItemOut)
async def update_manual_item(item_id: uuid.UUID, body: ManualItemPatch, session: Session, actor: Moderator) -> ItemOut:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    if not item.is_manual:
        raise HTTPException(409, "Only manual items can be edited")
    cfg = await get_settings(session, AppSettings)
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"reason"})
    before = {"title": item.title, "url": item.url, "summary": item.text_snippet, "manual": item.raw.get("manual")}
    manual = dict(item.raw.get("manual") or {})
    if "title" in changes:
        item.title = truncate(clean_html(changes.pop("title")), 500)
    if "url" in changes:
        item.url = changes.pop("url")
        item.canonical_url = canonical_url(item.url) if item.url else None
    if "summary" in changes:
        manual["summary"] = changes["summary"]
        item.text_snippet = truncate(clean_html(changes.pop("summary") or ""), cfg.snippet_max_chars)
    manual.update(changes)
    item.raw = {**item.raw, "manual": manual}
    item.simhash = simhash64(f"{item.title} {item.text_snippet}")
    item.enrichment = None  # re-enrich with the new text
    after = {"title": item.title, "url": item.url, "summary": item.text_snippet, "manual": manual}
    audit(session, actor, "item.update_manual", "item", item.id, before=before, after=after, reason=body.reason)
    await session.commit()
    [out] = await _out(session, [item])
    return out
