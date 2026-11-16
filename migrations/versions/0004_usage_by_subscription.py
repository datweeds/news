"""LLM spend per subscription; a subscription's own LLM provider

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Each subscription's share of LLM spend: its digest write-ups in full, and
    # an even share of enriching the items that matched its phrases. `scope`
    # says who paid: the platform, or the subscriber's own provider.
    op.execute("""
    CREATE TABLE llm_usage_subscriptions (
        day             DATE NOT NULL,
        subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        task            TEXT NOT NULL,
        scope           TEXT NOT NULL DEFAULT 'platform' CHECK (scope IN ('platform', 'own')),
        tokens_in       BIGINT NOT NULL DEFAULT 0,
        tokens_out      BIGINT NOT NULL DEFAULT 0,
        cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
        PRIMARY KEY (day, subscription_id, task, scope)
    )""")
    # A subscriber's own provider for work done only for it (digest writing).
    # The key is stored encrypted (AES-GCM, NEWS_SECRET_KEY); config holds the rest.
    op.execute("ALTER TABLE subscriptions ADD COLUMN llm_config JSONB, ADD COLUMN llm_secret TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS llm_config, DROP COLUMN IF EXISTS llm_secret")
    op.execute("DROP TABLE IF EXISTS llm_usage_subscriptions")
