"""Connector types, discovered from the `news.connectors` entry points."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import entry_points
from typing import Any

from news.connectors.base import Connector
from news.connectors.context import ConnectorContext, CursorStore, RateLimiter
from news.connectors.http import SafeHttp
from news.db.models import SourceInstance


@lru_cache
def connector_types() -> dict[str, type[Connector]]:
    out: dict[str, type[Connector]] = {}
    for ep in entry_points(group="news.connectors"):
        cls = ep.load()
        out[ep.name] = cls
    return out


def build_connector(src: SourceInstance, http: SafeHttp,
                    dry_run: bool = False) -> tuple[Connector, ConnectorContext]:
    """Instantiate a source instance's connector, with secrets resolved."""
    from news.config import resolve_env

    cls = connector_types()[src.type]
    config: Any = cls.config_schema().model_validate(resolve_env(src.config))
    ctx = ConnectorContext(
        source_id=src.id,
        source_name=src.name,
        http=http,
        cursor=CursorStore(src.cursor, dry_run=dry_run),
        limiter=RateLimiter(src.rate_limit_per_min),
    )
    return cls(ctx, config), ctx
