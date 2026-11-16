"""The connector interface. Every source is a connector producing RawItems;
no source-specific logic lives outside its connector."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, Field

from news.connectors.context import ConnectorContext

ItemKind = Literal["news", "paper", "preprint", "social", "manual"]
Mode = Literal["poll", "stream", "event"]


class RawItem(BaseModel):
    source_instance: str  # source_instances.id
    external_id: str = Field(min_length=1, max_length=2000)
    url: str | None = None
    title: str
    text: str = ""  # snippet or post body; trimmed further by the normaliser
    author: str | None = None
    published_at: datetime | None = None
    lang: str | None = None
    kind: ItemKind
    engagement: dict[str, int] = {}  # likes, reposts, replies, points
    geo_hint: str | None = None
    image_url: str | None = None  # the outlet's own image; linked, never copied
    platform: str | None = None  # e.g. substack
    paywalled: bool = False
    raw: dict[str, Any] = {}


class HealthStatus(BaseModel):
    ok: bool
    detail: str = ""


class Connector(Protocol):
    type_name: ClassVar[str]
    mode: ClassVar[Mode]  # poll: fetch() is one poll; stream: runs forever; event: pushed via the API

    def __init__(self, ctx: ConnectorContext, config: Any) -> None: ...

    def fetch(self) -> AsyncIterator[RawItem]: ...

    async def healthcheck(self) -> HealthStatus: ...

    @classmethod
    def config_schema(cls) -> type[BaseModel]: ...
