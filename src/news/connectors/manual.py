"""Manual items entered by admins and moderators through the admin API
(POST /v1/admin/items). Nothing to poll: items arrive as events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from news.connectors.base import HealthStatus, Mode, RawItem
from news.connectors.context import ConnectorContext


class ManualConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualConnector:
    type_name: ClassVar[str] = "manual"
    mode: ClassVar[Mode] = "event"

    def __init__(self, ctx: ConnectorContext, config: ManualConfig) -> None:
        self.ctx = ctx
        self.config = config

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return ManualConfig

    async def fetch(self) -> AsyncIterator[RawItem]:
        nothing: tuple[RawItem, ...] = ()
        for item in nothing:
            yield item

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True)
