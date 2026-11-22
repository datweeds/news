"""Research papers sent to each subscription

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Which papers each subscription has been sent (each paper goes out once),
    # with the section it matched and its plain-English summary
    op.execute("""
    CREATE TABLE subscription_papers (
        subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        item_id         UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        digest_id       UUID REFERENCES digests(id) ON DELETE SET NULL,
        section_key     TEXT,
        similarity      REAL,
        summary         TEXT,
        sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (subscription_id, item_id)
    )""")
    op.execute("CREATE INDEX items_research ON items (published_at) WHERE raw ? 'openalex_id'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS items_research")
    op.execute("DROP TABLE IF EXISTS subscription_papers")
