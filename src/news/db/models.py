"""ORM models. The schema itself is defined by the Alembic migrations."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from news.ids import uuid7

Json = dict[str, Any]
DIM = 384  # embedding size of BAAI/bge-small-en-v1.5


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceInstance(Base):
    __tablename__ = "source_instances"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    # Stored as written: ${ENV} references stay unresolved
    config: Mapped[Json] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trust_weight: Mapped[float] = mapped_column(REAL, default=0.5)
    default_topics: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    poll_interval_s: Mapped[int] = mapped_column(Integer, default=600)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer)
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    cursor: Mapped[Json] = mapped_column(JSONB, default=dict)
    health: Mapped[Json] = mapped_column(JSONB, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Item(Base):
    __tablename__ = "items"

    id: Mapped[uuid.UUID] = _id()
    source_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_instances.id")
    )
    external_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text_snippet: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = _now()
    lang: Mapped[str | None] = mapped_column(Text)
    engagement: Mapped[Json] = mapped_column(JSONB, default=dict)
    simhash: Mapped[int] = mapped_column(BigInteger)
    enrichment: Mapped[Json | None] = mapped_column(JSONB)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    significance: Mapped[float | None] = mapped_column(REAL)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id")
    )
    platform: Mapped[str | None] = mapped_column(Text)
    paywalled: Mapped[bool] = mapped_column(Boolean, default=False)
    geo_hint: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[Json] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIM))
    image_url: Mapped[str | None] = mapped_column(Text)
    enrich_attempts: Mapped[int] = mapped_column(Integer, default=0)


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = _id()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id")
    )
    description: Mapped[str] = mapped_column(Text, default="")
    half_life_h: Mapped[float | None] = mapped_column(REAL)
    weight_overrides: Mapped[Json] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIM))
    created_at: Mapped[datetime] = _now()


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _now()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id")
    )
    prefix: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _id()
    at: Mapped[datetime] = _now()
    actor: Mapped[str] = mapped_column(Text)
    actor_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[Json | None] = mapped_column(JSONB)
    after: Mapped[Json | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Json] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    task: Mapped[str] = mapped_column(Text, primary_key=True)
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)


# ── Stage 2: enrichment and stories ──────────────────────────────────────────
# Geography columns are not mapped; places are read and written with SQL.


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)


class ItemTopic(Base):
    __tablename__ = "item_topics"

    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True)
    label: Mapped[str] = mapped_column(Text, primary_key=True)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("topics.id"))
    confidence: Mapped[float | None] = mapped_column(REAL)


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = _id()
    kind: Mapped[str] = mapped_column(Text, default="story")
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    centroid: Mapped[list[float]] = mapped_column(Vector(DIM))
    status: Mapped[str] = mapped_column(Text, default="active")
    first_seen: Mapped[datetime] = _now()
    last_activity: Mapped[datetime] = _now()
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(REAL, default=0)
    score_breakdown: Mapped[Json] = mapped_column(JSONB, default=dict)
    score_dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    velocity: Mapped[float | None] = mapped_column(REAL)
    acceleration: Mapped[float | None] = mapped_column(REAL)
    breadth: Mapped[int | None] = mapped_column(Integer)
    is_hot: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    pinned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    boost: Mapped[int] = mapped_column(Integer, default=0)
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    summary_item_count: Mapped[int | None] = mapped_column(Integer)
    summary_written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClusterItem(Base):
    __tablename__ = "cluster_items"

    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clusters.id"), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True)
    added_at: Mapped[datetime] = _now()
    weight: Mapped[float] = mapped_column(REAL, default=1)


# ── Stage 3: subscriptions and delivery ──────────────────────────────────────


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = _id()
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clients.id"))
    external_ref: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[Json] = mapped_column(JSONB)
    schedule: Mapped[Json] = mapped_column(JSONB)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret: Mapped[str | None] = mapped_column(Text)
    feed_token: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    llm_config: Mapped[Json | None] = mapped_column(JSONB)
    llm_secret: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SubscriptionPhrase(Base):
    __tablename__ = "subscription_phrases"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), primary_key=True)
    section_key: Mapped[str] = mapped_column(Text, primary_key=True)
    phrase: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(DIM))


class SubscriptionOverride(Base):
    __tablename__ = "subscription_overrides"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), primary_key=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clusters.id"), primary_key=True)
    action: Mapped[str] = mapped_column(Text, primary_key=True)
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[uuid.UUID] = _id()
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subscriptions.id"))
    edition_date: Mapped[date] = mapped_column(Date)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    overview: Mapped[str | None] = mapped_column(Text)
    llm_written: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(Text, default="ready")
    payload: Mapped[Json] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DigestItem(Base):
    __tablename__ = "digest_items"

    digest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("digests.id"), primary_key=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clusters.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    section_key: Mapped[str] = mapped_column(Text)
    is_headline: Mapped[bool] = mapped_column(Boolean, default=False)
    is_update: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float] = mapped_column(REAL)
    relevance: Mapped[float] = mapped_column(REAL)
    item_count: Mapped[int] = mapped_column(Integer)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[uuid.UUID] = _id()
    digest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("digests.id"))
    method: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text, default="pending")
    response_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = _id()
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subscriptions.id"))
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clusters.id"))
    value: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = _now()
