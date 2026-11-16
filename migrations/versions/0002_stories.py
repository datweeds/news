"""Stage 2: embeddings, enrichment, places, stories

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DIM = 384  # BAAI/bge-small-en-v1.5


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE items
        ADD COLUMN embedding       vector({DIM}),
        ADD COLUMN image_url       TEXT,
        ADD COLUMN enrich_attempts INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("CREATE INDEX items_unembedded_idx ON items (ingested_at) WHERE embedding IS NULL AND duplicate_of IS NULL")
    op.execute(f"ALTER TABLE topics ADD COLUMN embedding vector({DIM})")

    # GeoNames: places over 15,000 people, first-level regions and countries
    op.execute("""
    CREATE TABLE geonames (
        geonameid     INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        ascii_name    TEXT NOT NULL,
        alt_names     TEXT[] NOT NULL DEFAULT '{}',   -- lower case
        country       CHAR(2),
        admin1        TEXT,
        feature_class CHAR(1),
        feature_code  TEXT,
        population    BIGINT,
        geom          GEOGRAPHY(Point, 4326)
    )""")
    op.execute("CREATE INDEX geonames_name_idx ON geonames (lower(name))")
    op.execute("CREATE INDEX geonames_ascii_idx ON geonames (lower(ascii_name))")
    op.execute("CREATE INDEX geonames_alt_idx ON geonames USING gin (alt_names)")
    op.execute("CREATE INDEX geonames_geom_idx ON geonames USING gist (geom)")

    op.execute("""
    CREATE TABLE entities (
        id   UUID PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL
    )""")
    op.execute("CREATE UNIQUE INDEX entities_name_type_idx ON entities (lower(name), type)")
    op.execute("""
    CREATE TABLE item_entities (
        item_id   UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        PRIMARY KEY (item_id, entity_id)
    )""")
    op.execute("""
    CREATE TABLE item_topics (
        item_id    UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        label      TEXT NOT NULL,
        topic_id   UUID REFERENCES topics(id) ON DELETE SET NULL,
        confidence REAL,
        PRIMARY KEY (item_id, label)
    )""")
    op.execute("CREATE INDEX item_topics_unmapped_idx ON item_topics (lower(label)) WHERE topic_id IS NULL")
    op.execute("""
    CREATE TABLE item_locations (
        item_id     UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        geonames_id INTEGER REFERENCES geonames(geonameid) ON DELETE SET NULL,
        country     CHAR(2),
        admin1      TEXT,
        role        TEXT NOT NULL CHECK (role IN ('primary', 'mentioned')),
        confidence  REAL,
        geom        GEOGRAPHY(Point, 4326),
        PRIMARY KEY (item_id, name)
    )""")

    op.execute(f"""
    CREATE TABLE clusters (
        id                 UUID PRIMARY KEY,
        kind               TEXT NOT NULL DEFAULT 'story' CHECK (kind IN ('story', 'theme')),
        title              TEXT NOT NULL,
        summary            TEXT,
        why_it_matters     TEXT,
        image_url          TEXT,
        centroid           vector({DIM}) NOT NULL,
        status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dormant', 'archived', 'suppressed')),
        first_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_activity      TIMESTAMPTZ NOT NULL DEFAULT now(),
        item_count         INTEGER NOT NULL DEFAULT 0,
        source_count       INTEGER NOT NULL DEFAULT 0,
        score              REAL NOT NULL DEFAULT 0,
        score_breakdown    JSONB NOT NULL DEFAULT '{{}}',
        score_dirty        BOOLEAN NOT NULL DEFAULT true,
        velocity           REAL,
        acceleration       REAL,
        breadth            INTEGER,
        is_hot             BOOLEAN NOT NULL DEFAULT false,
        locked_fields      TEXT[] NOT NULL DEFAULT '{{}}',
        pinned_until       TIMESTAMPTZ,
        boost              INTEGER NOT NULL DEFAULT 0 CHECK (boost BETWEEN -30 AND 30),
        suppressed_reason  TEXT,
        summary_item_count INTEGER,          -- items in the story when the summary was written
        summary_written_at TIMESTAMPTZ
    )""")
    op.execute("CREATE INDEX clusters_centroid_idx ON clusters USING hnsw (centroid vector_cosine_ops)")
    op.execute("CREATE INDEX clusters_active_idx ON clusters (last_activity DESC) WHERE status = 'active'")
    op.execute("""
    CREATE TABLE cluster_items (
        cluster_id UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        item_id    UUID NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
        added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        weight     REAL NOT NULL DEFAULT 1,
        PRIMARY KEY (cluster_id, item_id)
    )""")
    op.execute("""
    CREATE TABLE cluster_timeseries (
        cluster_id   UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        bucket_start TIMESTAMPTZ NOT NULL,
        items        INTEGER NOT NULL DEFAULT 0,
        authors      INTEGER NOT NULL DEFAULT 0,
        engagement   REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (cluster_id, bucket_start)
    )""")


def downgrade() -> None:
    for table in ("cluster_timeseries", "cluster_items", "clusters", "item_locations", "item_topics",
                  "item_entities", "entities", "geonames"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("ALTER TABLE topics DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE items DROP COLUMN IF EXISTS embedding, DROP COLUMN IF EXISTS image_url, DROP COLUMN IF EXISTS enrich_attempts")
