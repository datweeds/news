"""Stage 3: subscriptions, digests, delivery, feedback

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DIM = 384


def upgrade() -> None:
    op.execute("""
    CREATE TABLE subscriptions (
        id              UUID PRIMARY KEY,
        client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        external_ref    TEXT,
        definition      JSONB NOT NULL,
        schedule        JSONB NOT NULL,
        webhook_url     TEXT,
        webhook_secret  TEXT,
        feed_token      TEXT NOT NULL,
        active          BOOLEAN NOT NULL DEFAULT true,
        last_read_at    TIMESTAMPTZ,
        last_digest_at  TIMESTAMPTZ,
        next_digest_at  TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (client_id, external_ref)
    )""")
    op.execute("CREATE INDEX subscriptions_due_idx ON subscriptions (next_digest_at) WHERE active")

    # Each section's search phrases, embedded when the subscription is saved
    op.execute(f"""
    CREATE TABLE subscription_phrases (
        subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        section_key     TEXT NOT NULL,
        phrase          TEXT NOT NULL,
        embedding       vector({DIM}) NOT NULL,
        PRIMARY KEY (subscription_id, section_key, phrase)
    )""")

    # Per-subscription moderator choices: hide a story, or make it the headline
    op.execute("""
    CREATE TABLE subscription_overrides (
        subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        cluster_id      UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        action          TEXT NOT NULL CHECK (action IN ('hide', 'headline')),
        until           TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (subscription_id, cluster_id, action)
    )""")

    op.execute("""
    CREATE TABLE digests (
        id              UUID PRIMARY KEY,
        subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        edition_date    DATE NOT NULL,
        window_start    TIMESTAMPTZ NOT NULL,
        window_end      TIMESTAMPTZ NOT NULL,
        overview        TEXT,
        llm_written     BOOLEAN NOT NULL DEFAULT false,
        status          TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'held', 'sent', 'failed')),
        payload         JSONB NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        sent_at         TIMESTAMPTZ
    )""")
    op.execute("CREATE INDEX digests_subscription_idx ON digests (subscription_id, id DESC)")
    op.execute("""
    CREATE TABLE digest_items (
        digest_id   UUID NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
        cluster_id  UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        section_key TEXT NOT NULL,
        is_headline BOOLEAN NOT NULL DEFAULT false,
        is_update   BOOLEAN NOT NULL DEFAULT false,
        score       REAL NOT NULL,
        relevance   REAL NOT NULL,
        item_count  INTEGER NOT NULL,
        PRIMARY KEY (digest_id, cluster_id)
    )""")
    op.execute("CREATE INDEX digest_items_cluster_idx ON digest_items (cluster_id)")

    op.execute("""
    CREATE TABLE deliveries (
        id            UUID PRIMARY KEY,
        digest_id     UUID NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
        method        TEXT NOT NULL,
        attempt       INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
        response_code INTEGER,
        last_error    TEXT,
        next_retry_at TIMESTAMPTZ,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX deliveries_pending_idx ON deliveries (next_retry_at) WHERE status = 'pending'")

    op.execute("""
    CREATE TABLE feedback (
        id              UUID PRIMARY KEY,
        subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        cluster_id      UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        value           TEXT NOT NULL CHECK (value IN ('useful', 'not_relevant', 'seen_elsewhere')),
        at              TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")


def downgrade() -> None:
    for table in ("feedback", "deliveries", "digest_items", "digests", "subscription_overrides",
                  "subscription_phrases", "subscriptions"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
