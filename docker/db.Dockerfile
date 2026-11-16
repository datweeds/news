# Postgres 16 with pgvector (from the base image) and PostGIS. The server is
# upgraded to the latest 16.x too, since PostGIS is built against it.
FROM pgvector/pgvector:0.8.0-pg16

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16 postgresql-16-postgis-3 \
 && rm -rf /var/lib/apt/lists/*
