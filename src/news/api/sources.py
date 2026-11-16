"""Admin API: source instances."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import exists, select

from news.api.deps import Admin, HttpDep, Session, Viewer
from news.api.schemas import SourceOut, SourcePatch, SourceTestOut, SourceTypeOut, TestedItem
from news.audit import audit
from news.config import SourceConfig, source_fields, validate_source
from news.connectors.http import FetchError
from news.connectors.registry import build_connector, connector_types
from news.db.models import Item, SourceInstance

router = APIRouter(prefix="/v1/admin/sources", tags=["admin: sources"])

TEST_ITEMS = 10
TEST_TIMEOUT = 45


def _validate(src: SourceConfig) -> None:
    try:
        validate_source(src)
    except ValidationError as e:
        raise HTTPException(422, {"config": e.errors(include_url=False)}) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


async def _get(session: Session, source_id: uuid.UUID) -> SourceInstance:
    src = await session.get(SourceInstance, source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    return src


@router.get("", response_model=list[SourceOut])
async def list_sources(session: Session, _: Viewer) -> list[SourceOut]:
    rows = (await session.scalars(select(SourceInstance).order_by(SourceInstance.name))).all()
    return [SourceOut.of(s) for s in rows]


@router.get("/types", response_model=list[SourceTypeOut])
async def source_types(_: Viewer) -> list[SourceTypeOut]:
    return [
        SourceTypeOut(type=name, mode=cls.mode, config_schema=cls.config_schema().model_json_schema())
        for name, cls in sorted(connector_types().items())
    ]


@router.post("", response_model=SourceOut, status_code=201)
async def create_source(body: SourceConfig, session: Session, actor: Admin) -> SourceOut:
    _validate(body)
    if await session.scalar(select(exists().where(SourceInstance.name == body.name))):
        raise HTTPException(409, f"A source named '{body.name}' already exists")
    src = SourceInstance(name=body.name, **source_fields(body))
    session.add(src)
    await session.flush()
    audit(session, actor, "source.create", "source", src.id, after=body.model_dump())
    await session.commit()
    await session.refresh(src)
    return SourceOut.of(src)


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: uuid.UUID, session: Session, _: Viewer) -> SourceOut:
    return SourceOut.of(await _get(session, source_id))


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(source_id: uuid.UUID, body: SourcePatch, session: Session, actor: Admin) -> SourceOut:
    src = await _get(session, source_id)
    before = SourceOut.of(src).model_dump(mode="json")
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    if "config" in changes:
        _validate(SourceConfig(name=src.name, type=src.type, config=changes["config"]))
    columns = {"poll_interval": "poll_interval_s", "monthly_budget_usd": "budget_usd"}
    for key, value in changes.items():
        setattr(src, columns.get(key, key), value)
    if changes.get("enabled") is True and src.health.get("status") in ("failing", "disabled"):
        src.health = {}  # a fresh start when re-enabled
    if "config" in changes:
        src.cursor = {}
    await session.flush()
    await session.refresh(src)
    after = SourceOut.of(src).model_dump(mode="json")
    audit(session, actor, "source.update", "source", src.id, before=before, after=after, reason=body.reason)
    await session.commit()
    return SourceOut.of(src)


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: uuid.UUID, session: Session, actor: Admin) -> Response:
    src = await _get(session, source_id)
    if src.type == "manual":
        raise HTTPException(409, "The manual source can't be deleted")
    if await session.scalar(select(exists().where(Item.source_instance_id == src.id))):
        raise HTTPException(409, "This source has items; disable it instead")
    audit(session, actor, "source.delete", "source", src.id, before=SourceOut.of(src).model_dump(mode="json"))
    await session.delete(src)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{source_id}/test", response_model=SourceTestOut)
async def test_source(source_id: uuid.UUID, session: Session, http: HttpDep, _: Admin) -> SourceTestOut:
    """Run one fetch without storing anything or moving the source's cursor."""
    src = await _get(session, source_id)
    try:
        connector, _ctx = build_connector(src, http, dry_run=True)
    except (ValidationError, ValueError) as e:
        return SourceTestOut(ok=False, count=0, items=[], error=str(e))
    found: list[TestedItem] = []

    async def collect() -> None:
        async for item in connector.fetch():
            found.append(TestedItem(title=item.title, url=item.url, published_at=item.published_at))
            if len(found) >= TEST_ITEMS:
                break

    error: Any = None
    try:
        await asyncio.wait_for(collect(), TEST_TIMEOUT)
    except TimeoutError:
        error = f"Timed out after {TEST_TIMEOUT} s"
    except FetchError as e:
        error = str(e)
    return SourceTestOut(ok=error is None, count=len(found), items=found, error=error)
