"""The LLM switch and the daily budget. Every LLM call goes through
`run_task`, which checks both before calling and records the cost after."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Literal
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import LlmSettings, ModelConfig
from news.db.models import LlmUsage, Subscription
from news.llm.client import LLMError, LLMResult, client_for

__all__ = ["client_for"]  # re-exported, so tests can replace it

log = logging.getLogger(__name__)


@dataclass
class Gate:
    allowed: bool
    reason: str


def cost_usd(cfg: ModelConfig, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * cfg.input_usd_per_mtok + tokens_out * cfg.output_usd_per_mtok) / 1_000_000


async def spent_today(session: AsyncSession) -> float:
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(LlmUsage.day == datetime.now(UTC).date())
    )
    return float(total or 0)


USES_LLM = "COALESCE((definition->>'use_llm')::boolean, true)"


async def readers_active(session: AsyncSession, days: int, subscription_id: object | None = None) -> bool:
    """Has anyone opened the digests of an active subscription that uses the
    LLM recently?"""
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = select(Subscription.id).where(
        Subscription.active, Subscription.last_read_at > since, text(USES_LLM)).limit(1)
    if subscription_id is not None:
        stmt = stmt.where(Subscription.id == subscription_id)
    return await session.scalar(stmt) is not None


async def gate(session: AsyncSession, cfg: LlmSettings, estimate_usd: float = 0.0,
               subscription_id: object | None = None) -> Gate:
    """Whether an LLM call may be made now. For a digest, pass its
    subscription: in auto mode, that subscription must have readers."""
    if cfg.mode == "off":
        return Gate(False, "LLM is switched off")
    if cfg.mode == "auto" and not await readers_active(session, cfg.auto_reader_window_days, subscription_id):
        return Gate(False, f"No readers in the last {cfg.auto_reader_window_days} days")
    spent = await spent_today(session)
    if spent + estimate_usd > cfg.daily_budget_usd:
        return Gate(False, f"Daily budget reached (${spent:.3f} of ${cfg.daily_budget_usd:.2f})")
    return Gate(True, "")


Attribution = dict[uuid.UUID, float]  # subscription → share of a call (shares sum to at most 1)


async def record(session: AsyncSession, task: str, cfg: ModelConfig, result: LLMResult,
                 attribution: Attribution | None = None, scope: str = "platform") -> float:
    """Record a call's cost. Calls on a subscriber's own provider ("own") are
    attributed to it but don't count against the platform's daily budget."""
    cost = cost_usd(cfg, result.tokens_in, result.tokens_out)
    day = datetime.now(UTC).date()
    for sub_id, share in (attribution or {}).items():
        await session.execute(text("""
            INSERT INTO llm_usage_subscriptions (day, subscription_id, task, scope, tokens_in, tokens_out, cost_usd)
            VALUES (:day, :s, :task, :scope, :tin, :tout, :cost)
            ON CONFLICT (day, subscription_id, task, scope) DO UPDATE SET
                tokens_in = llm_usage_subscriptions.tokens_in + EXCLUDED.tokens_in,
                tokens_out = llm_usage_subscriptions.tokens_out + EXCLUDED.tokens_out,
                cost_usd = llm_usage_subscriptions.cost_usd + EXCLUDED.cost_usd
        """), {"day": day, "s": sub_id, "task": task, "scope": scope, "tin": round(result.tokens_in * share),
               "tout": round(result.tokens_out * share), "cost": Decimal(str(round(cost * share, 6)))})
    if scope == "platform":
        await session.execute(text("""
            INSERT INTO llm_usage (day, model, task, tokens_in, tokens_out, cost_usd)
            VALUES (:day, :model, :task, :tin, :tout, :cost)
            ON CONFLICT (day, model, task) DO UPDATE SET
                tokens_in = llm_usage.tokens_in + EXCLUDED.tokens_in,
                tokens_out = llm_usage.tokens_out + EXCLUDED.tokens_out,
                cost_usd = llm_usage.cost_usd + EXCLUDED.cost_usd
        """), {"day": day, "model": result.model, "task": task,
               "tin": result.tokens_in, "tout": result.tokens_out, "cost": Decimal(str(round(cost, 6)))})
    await session.commit()
    return cost


async def run_task(session: AsyncSession, cfg: LlmSettings, task: str, system: str, prompt: str,
                   subscription_id: object | None = None,
                   attribution: Attribution | None = None) -> LLMResult | None:
    """Call the task's model if the switch and budget allow. Returns None
    when not allowed. The estimate assumes the full max_tokens of output, so
    a call never takes spending past the cap."""
    model_cfg = cfg.tasks[task]
    estimate = cost_usd(model_cfg, len(system + prompt) // 3, model_cfg.max_tokens)
    g = await gate(session, cfg, estimate, subscription_id)
    if not g.allowed:
        log.info("llm call skipped", extra={"task": task, "reason": g.reason})
        return None
    client = client_for(model_cfg, cfg.timeout_s)
    try:
        result = await client.complete(system, prompt, model_cfg.max_tokens)
    except LLMError:
        log.exception("llm call failed", extra={"task": task})
        raise
    if attribution is None and isinstance(subscription_id, uuid.UUID):
        attribution = {subscription_id: 1.0}
    cost = await record(session, task, model_cfg, result, attribution)
    log.info("llm call", extra={"task": task, "model": result.model, "tokens_in": result.tokens_in,
                                "tokens_out": result.tokens_out, "cost_usd": round(cost, 5)})
    return result


# ── A subscriber's own provider ──────────────────────────────────────────────


class OwnLlm(BaseModel):
    """A subscriber's own LLM, used only for work done for that subscriber.
    The key is stored separately, encrypted."""

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["anthropic", "openai_compat"]
    base_url: str | None = None
    model: str = Field(min_length=1, max_length=200)
    input_usd_per_mtok: float = Field(0, ge=0)
    output_usd_per_mtok: float = Field(0, ge=0)
    monthly_budget_usd: float | None = Field(None, ge=0)
    max_tokens: int = Field(4000, ge=200, le=16000)

    def model_cfg(self) -> ModelConfig:
        return ModelConfig(provider="anthropic" if self.adapter == "anthropic" else "openai_compat",
                           model=self.model, base_url=self.base_url, input_usd_per_mtok=self.input_usd_per_mtok,
                           output_usd_per_mtok=self.output_usd_per_mtok, max_tokens=self.max_tokens)


async def own_spent_this_month(session: AsyncSession, subscription_id: uuid.UUID) -> float:
    total = await session.scalar(text("""
        SELECT COALESCE(sum(cost_usd), 0) FROM llm_usage_subscriptions
        WHERE subscription_id = :s AND scope = 'own' AND day >= date_trunc('month', now())::date
    """), {"s": subscription_id})
    return float(total or 0)


async def run_own(session: AsyncSession, sub: Subscription, task: str, system: str, prompt: str,
                  timeout_s: int) -> LLMResult | None:
    """Call the subscriber's own provider. Returns None (the caller uses its
    non-LLM fallback) if it is over its monthly budget or fails; it never
    falls back to the platform's provider."""
    from news.connectors.http import FetchError, check_url
    from news.secrets import SecretsUnavailable, decrypt

    if not sub.llm_config or not sub.llm_secret:
        return None
    own = OwnLlm.model_validate(sub.llm_config)
    cfg = own.model_cfg()
    estimate = cost_usd(cfg, len(system + prompt) // 3, cfg.max_tokens)
    if own.monthly_budget_usd is not None and \
            await own_spent_this_month(session, sub.id) + estimate > own.monthly_budget_usd:
        log.info("own llm over budget", extra={"subscription": str(sub.id), "task": task})
        return None
    try:
        if own.base_url and os.environ.get("NEWS_ALLOW_LOCAL_LLM") != "1":
            await check_url(own.base_url)  # no private or internal addresses
        client = client_for(cfg, timeout_s, api_key=decrypt(sub.llm_secret))
        result = await client.complete(system, prompt, cfg.max_tokens)
    except (LLMError, SecretsUnavailable, FetchError, ValueError) as e:
        log.warning("own llm call failed", extra={"subscription": str(sub.id), "task": task, "error": str(e)[:300]})
        return None
    await record(session, task, cfg, result, {sub.id: 1.0}, scope="own")
    return result
