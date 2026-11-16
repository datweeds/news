from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from news.db.models import Item, SourceInstance


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    enabled: bool
    trust_weight: float
    default_topics: list[str]
    poll_interval: int
    rate_limit_per_min: int | None
    monthly_budget_usd: float | None
    config: dict[str, Any]
    health: dict[str, Any]
    last_run_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, s: SourceInstance) -> SourceOut:
        return cls(
            id=s.id, name=s.name, type=s.type, enabled=s.enabled, trust_weight=s.trust_weight,
            default_topics=s.default_topics, poll_interval=s.poll_interval_s,
            rate_limit_per_min=s.rate_limit_per_min,
            monthly_budget_usd=float(s.budget_usd) if s.budget_usd is not None else None,
            config=s.config, health=s.health, last_run_at=s.last_run_at, created_at=s.created_at,
        )


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    trust_weight: float | None = Field(None, ge=0, le=1)
    default_topics: list[str] | None = None
    poll_interval: int | None = Field(None, ge=30)
    rate_limit_per_min: int | None = Field(None, ge=1)
    monthly_budget_usd: float | None = Field(None, ge=0)
    config: dict[str, Any] | None = None
    reason: str | None = None


class SourceTypeOut(BaseModel):
    type: str
    mode: str
    config_schema: dict[str, Any]


class TestedItem(BaseModel):
    title: str
    url: str | None
    published_at: datetime | None


class SourceTestOut(BaseModel):
    ok: bool
    count: int
    items: list[TestedItem]
    error: str | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    source: str
    kind: str
    url: str | None
    canonical_url: str | None
    title: str
    text_snippet: str
    author: str | None
    published_at: datetime | None
    ingested_at: datetime
    lang: str | None
    engagement: dict[str, Any]
    platform: str | None
    paywalled: bool
    is_manual: bool
    duplicate_of: uuid.UUID | None
    enriched: bool
    manual: dict[str, Any] | None

    @classmethod
    def of(cls, i: Item, source_name: str) -> ItemOut:
        return cls(
            id=i.id, source=source_name, kind=i.kind, url=i.url, canonical_url=i.canonical_url,
            title=i.title, text_snippet=i.text_snippet, author=i.author, published_at=i.published_at,
            ingested_at=i.ingested_at, lang=i.lang, engagement=i.engagement, platform=i.platform,
            paywalled=i.paywalled, is_manual=i.is_manual, duplicate_of=i.duplicate_of,
            enriched=i.enrichment is not None, manual=i.raw.get("manual") if i.is_manual else None,
        )


class ItemDetailOut(ItemOut):
    duplicates: list[ItemOut] = []


class ItemPage(BaseModel):
    items: list[ItemOut]
    next_cursor: str | None


class ManualItemIn(BaseModel):
    """A manual item (spec section 8). Topics, locations, importance and
    targeting are kept with the item and used from stage 2 on."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=500)
    url: HttpUrl | None = None
    summary: str | None = Field(None, max_length=2000)
    body: str | None = Field(None, max_length=20000)
    kind: Literal["news", "paper", "preprint", "social", "manual"] = "manual"
    topics: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    importance_override: int | None = Field(None, ge=0, le=100)
    pin_until: datetime | None = None
    editor_note: str | None = Field(None, max_length=1000)
    target_subscriptions: list[uuid.UUID] = Field(default_factory=list)


class ManualItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=3, max_length=500)
    url: HttpUrl | None = None
    summary: str | None = Field(None, max_length=2000)
    topics: list[str] | None = None
    locations: list[str] | None = None
    importance_override: int | None = Field(None, ge=0, le=100)
    pin_until: datetime | None = None
    editor_note: str | None = Field(None, max_length=1000)
    target_subscriptions: list[uuid.UUID] | None = None
    reason: str | None = None


class BulkResult(BaseModel):
    created: int
    skipped: int
    errors: list[str]


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason: str | None


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_cursor: str | None


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "moderator", "client", "viewer"]
    client_id: uuid.UUID | None = None


class KeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    role: str
    client_id: uuid.UUID | None
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class KeyCreated(KeyOut):
    key: str = Field(description="Shown once; store it now")
