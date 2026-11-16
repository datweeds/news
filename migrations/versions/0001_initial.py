"""Stage 1: sources, items, topics, keys, audit, settings

Revision ID: 0001
Revises:
Create Date: 2026-11-15
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.execute("""
    CREATE TABLE source_instances (
        id                 UUID PRIMARY KEY,
        name               TEXT NOT NULL UNIQUE,
        type               TEXT NOT NULL,
        config             JSONB NOT NULL DEFAULT '{}',
        enabled            BOOLEAN NOT NULL DEFAULT true,
        trust_weight       REAL NOT NULL DEFAULT 0.5 CHECK (trust_weight BETWEEN 0 AND 1),
        default_topics     TEXT[] NOT NULL DEFAULT '{}',
        poll_interval_s    INTEGER NOT NULL DEFAULT 600,
        rate_limit_per_min INTEGER,
        budget_usd         NUMERIC(10, 2),
        cursor             JSONB NOT NULL DEFAULT '{}',
        health             JSONB NOT NULL DEFAULT '{}',
        last_run_at        TIMESTAMPTZ,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE items (
        id                 UUID PRIMARY KEY,
        source_instance_id UUID NOT NULL REFERENCES source_instances(id) ON DELETE RESTRICT,
        external_id        TEXT NOT NULL,
        kind               TEXT NOT NULL CHECK (kind IN ('news', 'paper', 'preprint', 'social', 'manual')),
        url                TEXT,
        canonical_url      TEXT,
        title              TEXT NOT NULL,
        text_snippet       TEXT NOT NULL DEFAULT '',
        author             TEXT,
        published_at       TIMESTAMPTZ,
        ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        lang               TEXT,
        engagement         JSONB NOT NULL DEFAULT '{}',
        simhash            BIGINT NOT NULL,
        enrichment         JSONB,
        prompt_version     TEXT,
        significance       REAL,
        is_manual          BOOLEAN NOT NULL DEFAULT false,
        created_by         UUID,
        duplicate_of       UUID REFERENCES items(id) ON DELETE SET NULL,
        platform           TEXT,
        paywalled          BOOLEAN NOT NULL DEFAULT false,
        geo_hint           TEXT,
        raw                JSONB NOT NULL DEFAULT '{}',
        UNIQUE (source_instance_id, external_id)
    )""")
    op.execute("CREATE INDEX items_ingested_idx ON items (ingested_at DESC)")
    op.execute("CREATE INDEX items_canonical_url_idx ON items (canonical_url) WHERE canonical_url IS NOT NULL")
    # Near-duplicate lookup: two 64-bit SimHashes within 3 bits share at least
    # one of these four 16-bit bands
    for i, shift in enumerate((48, 32, 16, 0)):
        op.execute(f"CREATE INDEX items_simhash_b{i} ON items (((simhash >> {shift}) & 65535))")
    op.execute("CREATE INDEX items_unenriched_idx ON items (ingested_at) WHERE enrichment IS NULL AND duplicate_of IS NULL")
    op.execute("CREATE INDEX items_duplicate_of_idx ON items (duplicate_of) WHERE duplicate_of IS NOT NULL")

    op.execute("""
    CREATE TABLE topics (
        id               UUID PRIMARY KEY,
        slug             TEXT NOT NULL UNIQUE,
        label            TEXT NOT NULL,
        parent_id        UUID REFERENCES topics(id) ON DELETE SET NULL,
        description      TEXT NOT NULL DEFAULT '',
        half_life_h      REAL,
        weight_overrides JSONB NOT NULL DEFAULT '{}',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE clients (
        id         UUID PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE api_keys (
        id           UUID PRIMARY KEY,
        name         TEXT NOT NULL,
        role         TEXT NOT NULL CHECK (role IN ('admin', 'moderator', 'client', 'viewer')),
        client_id    UUID REFERENCES clients(id) ON DELETE CASCADE,
        prefix       TEXT NOT NULL,
        key_hash     TEXT NOT NULL UNIQUE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ,
        revoked_at   TIMESTAMPTZ
    )""")

    op.execute("""
    CREATE TABLE audit_log (
        id           UUID PRIMARY KEY,
        at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor        TEXT NOT NULL,
        actor_key_id UUID,
        action       TEXT NOT NULL,
        target_type  TEXT NOT NULL,
        target_id    TEXT,
        before       JSONB,
        after        JSONB,
        reason       TEXT
    )""")
    op.execute("CREATE INDEX audit_log_at_idx ON audit_log (at DESC)")
    op.execute("CREATE INDEX audit_log_target_idx ON audit_log (target_type, target_id)")

    op.execute("""
    CREATE TABLE settings (
        name       TEXT PRIMARY KEY,
        value      JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE llm_usage (
        day        DATE NOT NULL,
        model      TEXT NOT NULL,
        task       TEXT NOT NULL,
        tokens_in  BIGINT NOT NULL DEFAULT 0,
        tokens_out BIGINT NOT NULL DEFAULT 0,
        cost_usd   NUMERIC(12, 6) NOT NULL DEFAULT 0,
        PRIMARY KEY (day, model, task)
    )""")


def downgrade() -> None:
    for table in ("llm_usage", "settings", "audit_log", "api_keys", "clients", "topics", "items", "source_instances"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
