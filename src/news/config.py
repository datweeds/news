"""YAML configuration: models, loading, seeding the database, exporting back.

YAML files in the config directory seed the database. After that the database
wins: admin API changes stick, and `news config export` writes them back to
YAML. Secrets are written as ${ENV_NAME}; they are stored unresolved and only
resolved when used, so they never reach the database or an export.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from news.db.models import Setting, SourceInstance, Topic

# ── Settings sections (one row each in `settings`) ───────────────────────────


class ModelConfig(BaseModel):
    """One LLM task's model. Prices are used to enforce the daily budget."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic", "openai_compat", "fake"] = "anthropic"
    model: str = "claude-haiku-5-5"
    input_usd_per_mtok: float = Field(1.0, ge=0)
    output_usd_per_mtok: float = Field(5.0, ge=0)
    base_url: str | None = None  # openai_compat only
    api_key_env: str = "ANTHROPIC_API_KEY"  # name of the environment variable, not the key
    max_tokens: int = Field(3000, ge=100, le=16000)


LLM_TASKS = ("extract", "digest_writer")


def _default_tasks() -> dict[str, ModelConfig]:
    return {task: ModelConfig() for task in LLM_TASKS}


class LlmSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "on", "auto"] = "auto"
    daily_budget_usd: float = Field(1.0, ge=0)
    auto_reader_window_days: int = Field(7, ge=1)
    backlog_max_age_hours: int = Field(48, ge=1)
    tasks: dict[str, ModelConfig] = Field(default_factory=_default_tasks)
    extract_batch_size: int = Field(10, ge=1, le=50)
    timeout_s: int = Field(90, ge=10, le=600)

    @field_validator("tasks")
    @classmethod
    def _all_tasks(cls, v: dict[str, ModelConfig]) -> dict[str, ModelConfig]:
        unknown = set(v) - set(LLM_TASKS)
        if unknown:
            raise ValueError(f"Unknown LLM tasks: {', '.join(sorted(unknown))} (have: {', '.join(LLM_TASKS)})")
        return {**_default_tasks(), **v}


class ScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    significance: float = 0.25
    corroboration: float = 0.20
    velocity: float = 0.15
    breadth: float = 0.10
    novelty: float = 0.15
    evidence: float = 0.15


def _default_evidence() -> dict[str, float]:
    return {"paper": 1.0, "preprint": 0.7, "news": 0.8, "social": 0.4, "manual": 0.8}


class ScoringSettings(BaseModel):
    """Stories, importance scores, relevance and digest rules."""

    model_config = ConfigDict(extra="forbid")

    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    half_life_h: float = Field(18, gt=0)
    science_half_life_h: float = Field(72, gt=0)
    evidence: dict[str, float] = Field(default_factory=_default_evidence)
    promotional_penalty: float = Field(0.5, ge=0, le=1)
    single_source_social_penalty: float = Field(0.6, ge=0, le=1)
    # Stories. Embeddings alone measure topic more than event, so an item
    # joins a story on very high similarity, or on lower similarity plus
    # shared names (capitalised words) that are rare in recent news.
    story_similarity: float = Field(0.88, ge=0.5, le=1)
    story_one_name_similarity: float = Field(0.80, ge=0.3, le=1)
    story_two_names_similarity: float = Field(0.70, ge=0.3, le=1)
    rare_name_max_share: float = Field(0.015, gt=0, le=1)
    story_window_h: int = Field(72, ge=1)
    merge_similarity: float = Field(0.90, ge=0.5, le=1)
    dormant_after_h: int = Field(72, ge=1)
    archive_after_days: int = Field(14, ge=1)
    # Relevance: topic similarity below topic_floor counts 0, at topic_full counts 1
    topic_floor: float = Field(0.55, ge=0, le=1)
    topic_full: float = Field(0.75, ge=0, le=1)
    mentioned_location: float = Field(0.6, ge=0, le=1)
    unknown_location: float = Field(0.5, ge=0, le=1)
    taxonomy_match: float = Field(0.75, ge=0, le=1)
    # Digest defaults (a subscription can override min_score, max_items, max_per_section)
    min_score: int = Field(35, ge=0, le=100)
    max_items: int = Field(12, ge=1, le=50)
    max_per_section: int = Field(4, ge=1, le=20)
    max_source_type_share: float = Field(0.4, gt=0, le=1)
    resend_rise: int = Field(15, ge=0, le=100)
    # Enrichment cost control
    enrich_relevance_gate: float = Field(0.6, ge=0, le=1)
    enrich_max_items_per_story: int = Field(3, ge=1, le=50)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dedup_window_days: int = Field(7, ge=1)
    # The banded index lookup is exact up to 3 bits
    simhash_max_distance: int = Field(3, ge=0, le=3)
    snippet_max_chars: int = Field(500, ge=50, le=2000)
    min_poll_interval: int = Field(60, ge=30)
    substack_min_poll_interval: int = Field(1800, ge=60)
    ingest_concurrency: int = Field(5, ge=1, le=50)
    source_auto_disable_hours: int = Field(24, ge=1)
    api_rate_limit_per_min: int = Field(120, ge=1)


SETTINGS_MODELS: dict[str, type[BaseModel]] = {"llm": LlmSettings, "app": AppSettings, "scoring": ScoringSettings}

# ── Sources and topics ───────────────────────────────────────────────────────


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,62}$")
    type: str
    enabled: bool = True
    trust_weight: float = Field(0.5, ge=0, le=1)
    default_topics: list[str] = []
    poll_interval: int = Field(600, ge=30)
    rate_limit_per_min: int | None = Field(None, ge=1)
    monthly_budget_usd: float | None = Field(None, ge=0)
    config: dict[str, Any] = {}


class TopicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=120)
    description: str = ""
    half_life_h: float | None = Field(None, gt=0)
    children: list[TopicConfig] = []


# ── Loading ──────────────────────────────────────────────────────────────────

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def resolve_env(value: Any) -> Any:
    """Replace ${ENV_NAME} references with their values, recursively."""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ValueError(f"Environment variable {name} is not set")
            return os.environ[name]
        return _ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env(v) for v in value]
    return value


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def load_sources(config_dir: Path) -> list[SourceConfig]:
    data = load_yaml(config_dir / "sources.yaml") or {}
    return [SourceConfig.model_validate(s) for s in data.get("sources") or []]


def load_topics(config_dir: Path) -> list[TopicConfig]:
    data = load_yaml(config_dir / "taxonomy.yaml") or {}
    return [TopicConfig.model_validate(t) for t in data.get("topics") or []]


def load_settings(config_dir: Path) -> dict[str, BaseModel]:
    out: dict[str, BaseModel] = {}
    for name, model in SETTINGS_MODELS.items():
        data = load_yaml(config_dir / f"{name}.yaml")
        if data is not None:
            out[name] = model.model_validate(data)
    return out


# ── Seeding ──────────────────────────────────────────────────────────────────


def validate_source(src: SourceConfig) -> None:
    """Check the connector type exists and the config matches its schema."""
    from news.connectors.registry import connector_types

    types = connector_types()
    if src.type not in types:
        raise ValueError(f"Unknown connector type '{src.type}' (installed: {', '.join(sorted(types))})")
    # Validate with secrets left as placeholders: only the shape matters here
    types[src.type].config_schema().model_validate(src.config)


def source_fields(src: SourceConfig) -> dict[str, Any]:
    return {
        "type": src.type,
        "enabled": src.enabled,
        "trust_weight": src.trust_weight,
        "default_topics": src.default_topics,
        "poll_interval_s": src.poll_interval,
        "rate_limit_per_min": src.rate_limit_per_min,
        "budget_usd": src.monthly_budget_usd,
        "config": src.config,
    }


async def seed(session: AsyncSession, config_dir: Path, overwrite: bool = False) -> dict[str, int]:
    """Load YAML into the database. New entries are added; existing ones are
    only changed when `overwrite` is set. Returns counts of rows written."""
    counts = {"sources": 0, "settings": 0, "topics": 0}

    sources = load_sources(config_dir)
    sources.append(SourceConfig(name="manual", type="manual", trust_weight=1.0))
    for src in sources:
        validate_source(src)
        row = await session.scalar(select(SourceInstance).where(SourceInstance.name == src.name))
        if row is None:
            session.add(SourceInstance(name=src.name, **source_fields(src)))
            counts["sources"] += 1
        elif overwrite and src.name != "manual":
            for key, value in source_fields(src).items():
                setattr(row, key, value)
            counts["sources"] += 1

    for name, value in load_settings(config_dir).items():
        setting = await session.get(Setting, name)
        if setting is None:
            session.add(Setting(name=name, value=value.model_dump()))
            counts["settings"] += 1
        elif overwrite:
            setting.value = value.model_dump()
            counts["settings"] += 1

    async def add_topics(topics: list[TopicConfig], parent: Topic | None) -> None:
        for t in topics:
            row = await session.scalar(select(Topic).where(Topic.slug == t.slug))
            if row is None:
                row = Topic(slug=t.slug, label=t.label, description=t.description,
                            half_life_h=t.half_life_h, parent_id=parent.id if parent else None)
                session.add(row)
                await session.flush()
                counts["topics"] += 1
            elif overwrite:
                row.label, row.description, row.half_life_h = t.label, t.description, t.half_life_h
                row.parent_id = parent.id if parent else None
                counts["topics"] += 1
            await add_topics(t.children, row)

    await add_topics(load_topics(config_dir), None)
    await session.commit()
    return counts


# ── Export ───────────────────────────────────────────────────────────────────


async def export(session: AsyncSession, out_dir: Path) -> list[Path]:
    """Write the database's sources, settings and topics back to YAML."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def dump(name: str, data: Any) -> None:
        path = out_dir / name
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        written.append(path)

    rows = (await session.scalars(
        select(SourceInstance).where(SourceInstance.type != "manual").order_by(SourceInstance.name)
    )).all()
    sources = []
    for r in rows:
        src: dict[str, Any] = {"name": r.name, "type": r.type, "enabled": r.enabled,
                               "trust_weight": round(r.trust_weight, 3), "poll_interval": r.poll_interval_s}
        if r.default_topics:
            src["default_topics"] = r.default_topics
        if r.rate_limit_per_min:
            src["rate_limit_per_min"] = r.rate_limit_per_min
        if r.budget_usd is not None:
            src["monthly_budget_usd"] = float(r.budget_usd)
        src["config"] = r.config
        sources.append(src)
    dump("sources.yaml", {"sources": sources})

    for setting in (await session.scalars(select(Setting))).all():
        dump(f"{setting.name}.yaml", setting.value)

    topics = (await session.scalars(select(Topic).order_by(Topic.slug))).all()
    children: dict[Any, list[Topic]] = {}
    for t in topics:
        children.setdefault(t.parent_id, []).append(t)

    def tree(parent_id: Any) -> list[dict[str, Any]]:
        out = []
        for t in children.get(parent_id, []):
            node: dict[str, Any] = {"slug": t.slug, "label": t.label, "description": t.description}
            if t.half_life_h:
                node["half_life_h"] = t.half_life_h
            if kids := tree(t.id):
                node["children"] = kids
            out.append(node)
        return out

    dump("taxonomy.yaml", {"topics": tree(None)})
    return written
