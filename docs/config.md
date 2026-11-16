# Configuration

YAML files in `config/` seed the database on first start. After that the
database wins: changes through the admin API stick, and
`news config export` writes the database back to YAML.
`news config reload` adds new entries from the files;
`news config reload --overwrite` makes the files win.

Secrets are never written in these files. Write `${ENV_NAME}` and set the
variable in `.env`; the reference is stored as written and resolved only when
used.

## llm.yaml — `PUT /v1/admin/settings/llm`

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `auto` | `off`: never call the LLM. `on`: enrich within the budget. `auto`: enrich only while an active subscription's digests were opened in the last `auto_reader_window_days` |
| `daily_budget_usd` | `1.00` | Hard cap per UTC day across all LLM tasks |
| `auto_reader_window_days` | `7` | How recently someone must have read a digest for `auto` to enrich |
| `backlog_max_age_hours` | `48` | Unenriched items older than this are skipped when the LLM comes back on |
| `tasks` | Haiku for both | Model per task: `extract` (per item, batched) and `digest_writer` (once per digest). Each has `provider` (anthropic, openai_compat, fake), `model`, `input_usd_per_mtok`, `output_usd_per_mtok` (used for the budget), `base_url` (openai_compat), `api_key_env` (the variable's name), `max_tokens` |
| `extract_batch_size` | `10` | Items per extraction call |
| `timeout_s` | `90` | Per LLM call |

## app.yaml — `PUT /v1/admin/settings/app`

| Setting | Default | Meaning |
|---|---|---|
| `dedup_window_days` | `7` | How far back to look for duplicates |
| `simhash_max_distance` | `3` | Max differing bits (0–3) for a near-duplicate |
| `snippet_max_chars` | `500` | Stored text per item; full articles are never stored |
| `min_poll_interval` | `60` | Floor on any source's poll interval, seconds |
| `substack_min_poll_interval` | `1800` | Floor for Substack feeds |
| `ingest_concurrency` | `5` | Sources polled at once per ingest worker |
| `source_auto_disable_hours` | `24` | Continuous failure before a source is disabled |
| `api_rate_limit_per_min` | `120` | Requests per minute per API key |

## scoring.yaml — `PUT /v1/admin/settings/scoring`

| Setting | Default | Meaning |
|---|---|---|
| `weights` | see file | Importance signals: significance, corroboration, velocity, breadth, novelty, evidence |
| `half_life_h` / `science_half_life_h` | `18` / `72` | Importance halves after this long without new coverage |
| `evidence` | paper 1.0 … social 0.4 | Evidence quality per item kind |
| `promotional_penalty`, `single_source_social_penalty` | `0.5`, `0.6` | Multipliers |
| `story_similarity` | `0.88` | Join a story on embedding similarity alone |
| `story_one_name_similarity` / `story_two_names_similarity` | `0.80` / `0.70` | Join with one / two shared rare names |
| `rare_name_max_share` | `0.015` | A name is rare if in at most this share of recent items |
| `story_window_h`, `merge_similarity` | `72`, `0.90` | Stories considered for joining; merging near-identical stories |
| `dormant_after_h`, `archive_after_days` | `72`, `14` | Story lifecycle |
| `topic_floor` / `topic_full` | `0.55` / `0.75` | Phrase similarity counting nothing / fully |
| `mentioned_location`, `unknown_location` | `0.6`, `0.5` | Place match when only mentioned, or unknown |
| `taxonomy_match` | `0.75` | Mapping LLM topics onto the taxonomy |
| `min_score`, `max_items`, `max_per_section` | `35`, `12`, `4` | Digest defaults (per subscription overridable) |
| `max_source_type_share` | `0.4` | Diversity across news, papers, social (when several are present) |
| `resend_rise` | `15` | A story is re-sent only as an update after rising this much |
| `enrich_relevance_gate`, `enrich_max_items_per_story` | `0.6`, `3` | Enrichment cost control |

## sources.yaml

One entry per source instance: `name`, `type`, `enabled`, `trust_weight`
(0–1), `default_topics`, `poll_interval` (seconds), `rate_limit_per_min`,
`monthly_budget_usd` (paid sources), and `config`, validated against the
connector type's schema (`GET /v1/admin/sources/types`).

### rss

| Field | Default | Meaning |
|---|---|---|
| `url` | — | Feed URL (RSS or Atom) |
| `kind` | `news` | `news`, `paper` or `preprint` |
| `platform` | — | `substack` for Substack publications |
| `max_items` | `100` | Entries read per poll |

## taxonomy.yaml

Hierarchical topics: `slug`, `label`, `description` (one or two sentences on
what belongs in the topic and what doesn't; used to match stories from stage
2), optional `half_life_h`, and `children`.
