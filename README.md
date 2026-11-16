# News

A self-hosted service that collects news, science and social posts, works
out which developments matter, and delivers short ranked digests per
subscription. debate-report uses it for each Society's News tab.

Status: **stages 1–3**. Collecting, deduplication, embeddings, LLM
enrichment, stories, importance scores, subscriptions with sections and
places, previews, digests, webhook/pull/Atom delivery and feedback. Science
and social sources, themes and hot-theme alerts come in stage 4; the admin
console and moderation tools in stage 5.

## How it works

1. **ingest** polls each source and puts raw items on a Redis stream.
2. **normalise** cleans them, removes tracking from URLs and marks
   duplicates (same URL, or near-identical text).
3. **stories** embeds items locally (BAAI/bge-small-en-v1.5, free) and
   groups them into stories: very similar items, or similar items sharing
   names that are rare in recent news. It scores every story 0–100 from
   corroboration, velocity, breadth, novelty, evidence, LLM significance and
   decay ([config/scoring.yaml](config/scoring.yaml)).
4. **enrich** asks the LLM to tag items with topics, places, entities,
   significance and novelty, in batches, only while the LLM switch and the
   daily budget allow, and only for items relevant to some subscription.
5. **deliver** builds each subscription's digest on its schedule. Relevance
   = importance × topic match × place match per section; the most relevant
   story is the headline (or a moderator's choice). One LLM call writes the
   overview, titles, summaries and "why it matters". Digests go out by
   signed webhook, and can be pulled or read as an Atom feed.

## Running

Everything runs in Docker Compose: Postgres 16 (pgvector + PostGIS), Redis,
a one-shot `migrate` job, the API and five workers. Places come from
GeoNames, loaded once:

```sh
docker compose run --rm api news geonames load
```

```sh
cd ~/news
docker compose up -d --build        # first start runs migrations and seeds from config/
docker compose logs -f ingest normalise
```

`.env` holds `POSTGRES_PASSWORD` (generated at setup, never committed). The
API listens on `127.0.0.1:3012`; nginx serves it at https://news.new.vote.
Interactive API docs: https://news.new.vote/v1/docs.

### API keys

Admin endpoints need `Authorization: Bearer <key>`. Roles: `admin`
(everything), `moderator` (items and moderation), `viewer` (read-only),
`client` (client API only, from stage 3).

```sh
docker compose run --rm api news keys create --name david --role admin --out /app/config/.admin-key
```

Keys are stored hashed and shown once. Create more with
`POST /v1/admin/api-keys`.

## Adding a source

Add an entry to `config/sources.yaml` and run
`docker compose run --rm api news config reload`, or `POST /v1/admin/sources`:

```yaml
  - name: carbon-brief          # lower case, digits, - _ .
    type: rss
    trust_weight: 0.8           # 0-1, how much to trust it
    poll_interval: 1800         # seconds
    config:
      url: https://www.carbonbrief.org/feed/
      # platform: substack     # for Substack publications (polled at most every 30 min)
```

`POST /v1/admin/sources/{id}/test` runs one fetch without storing anything.
A source that keeps failing backs off, and is disabled after 24 hours of
failure (logged as an alert and recorded in the audit log).

## Adding a connector type

A connector is a class implementing `news.connectors.base.Connector`:
`type_name`, `mode` (poll, stream or event), `config_schema()` (a Pydantic
model), `fetch()` yielding `RawItem`s, and `healthcheck()`. Register it under
the `news.connectors` entry-point group, in this package's `pyproject.toml`
or in a separate package:

```toml
[project.entry-points."news.connectors"]
mysource = "news_mysource:MySourceConnector"
```

Its config schema then appears in `GET /v1/admin/sources/types`. Keep all
source-specific logic inside the connector, and use `ctx.http` for requests
(retries, robots.txt, SSRF protection) and `ctx.cursor` for resume state.

## Subscriptions (client API)

A client key (`news keys create --role client --client NAME`) manages its
subscriptions under `/v1/subscriptions`. A subscription has a name, up to 12
sections (a heading plus search phrases, optional word filters), places
(from `GET /v1/locations/resolve`), a schedule, `min_score` and `max_items`.

- `POST /v1/subscriptions/preview` tests a definition before saving: the
  edition it would produce now, with scores and reasons, near misses, and
  guidance on each phrase. No LLM, nothing stored.
- `POST /v1/subscriptions/{id}/digests` publishes an edition now.
- `POST /v1/subscriptions/{id}/read` reports a reader (drives `auto` mode).
- `POST /v1/subscriptions/{id}/overrides` hides a story or makes it the
  headline; `POST /v1/feedback` records reader feedback.

Webhooks carry `X-News-Signature: t=<unix>,v1=<hmac>` (HMAC-SHA256 of
`<t>.<body>` with the subscription's secret); reject timestamps older than
5 minutes.

## Switching the LLM off

LLM use has three modes in `config/llm.yaml`, changeable at runtime:

```sh
curl -X PUT https://news.new.vote/v1/admin/settings/llm \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"mode": "off"}'
```

`off` never calls the LLM; `on` always does, within `daily_budget_usd`;
`auto` (the default) only does while someone is reading digests. Collecting,
deduplication, stories and scoring carry on regardless and cost nothing;
without the LLM, digests use the outlets' own headlines.
`GET /v1/admin/metrics/llm` shows today's spend and whether the LLM is
available. Each model's price is in `llm.yaml`; keep it current.

## Tuning noise

- Too many near-duplicates: lower `simhash_max_distance` / check sources.
- Stories split that should be one: lower `story_two_names_similarity`;
  unrelated items merged: raise `story_similarity`.
- Thin editions: lower the subscription's `min_score`, or `topic_floor`.
- Unimportant stories ranking high: adjust `weights`, or a source's
  `trust_weight`. `news eval precision --labels file.csv` reports
  precision@10 against hand labels.

All settings: [docs/config.md](docs/config.md). Places: GeoNames
(geonames.org), CC BY 4.0.

## Development

```sh
docker compose run --rm api pytest -q
docker compose run --rm api mypy src
```

Tests use recorded fixtures and never call the live network; database tests
use the `news_test` database.

## Licence

Proprietary. Copyright (c) 2026 Debate Report Limited. All rights reserved. See [LICENSE](LICENSE).
