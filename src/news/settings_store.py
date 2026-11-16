"""Runtime settings, read from the database with a short cache so admin
changes reach every worker within CACHE_SECONDS without a restart."""

from __future__ import annotations

import time
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import SETTINGS_MODELS, resolve_env
from news.db.models import Setting

CACHE_SECONDS = 30
T = TypeVar("T", bound=BaseModel)

_cache: dict[str, tuple[float, BaseModel]] = {}


def setting_name(model: type[BaseModel]) -> str:
    for name, m in SETTINGS_MODELS.items():
        if m is model:
            return name
    raise KeyError(model.__name__)


async def get_settings(session: AsyncSession, model: type[T]) -> T:
    name = setting_name(model)
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS and isinstance(hit[1], model):
        return hit[1]
    row = await session.get(Setting, name)
    value = model.model_validate(resolve_env(row.value)) if row else model()
    _cache[name] = (time.monotonic(), value)
    return value


def invalidate(name: str | None = None) -> None:
    if name is None:
        _cache.clear()
    else:
        _cache.pop(name, None)
