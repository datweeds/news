"""The digest writer: one LLM call per digest writes the overview and, for
each story, a title, summary and why it matters. It may drop stories but
never add them. Without the LLM (switched off, no readers, or over budget),
digests use the outlets' own headline and snippet."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from news.config import LlmSettings
from news.delivery.digest import Candidate
from news.db.models import Subscription
from news.llm.budget import run_own, run_task
from news.llm.client import LLMError, parse_json
from news.prompts import data, load

log = logging.getLogger(__name__)


class WrittenStory(BaseModel):
    id: str
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1500)
    why_it_matters: str = Field("", max_length=600)


class WriterOut(BaseModel):
    overview: str = Field("", max_length=1500)
    stories: list[WrittenStory] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)


async def _headlines(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[tuple[str, str, str]]]:
    rows = (await session.execute(text("""
        SELECT ci.cluster_id, COALESCE(i.raw->>'outlet', s.name) AS outlet, i.title, i.text_snippet
        FROM cluster_items ci JOIN items i ON i.id = ci.item_id JOIN source_instances s ON s.id = i.source_instance_id
        WHERE ci.cluster_id = ANY(:ids)
        ORDER BY s.trust_weight DESC, i.published_at NULLS LAST
    """), {"ids": ids})).all()
    out: dict[uuid.UUID, list[tuple[str, str, str]]] = {}
    for r in rows:
        lst = out.setdefault(r.cluster_id, [])
        if len(lst) < 5 and r.outlet not in {x[0] for x in lst}:
            lst.append((r.outlet, r.title, r.text_snippet))
    return out


def build_prompt(edition: str, items: list[Candidate], headings: dict[str, str],
                 headlines: dict[uuid.UUID, list[tuple[str, str, str]]]) -> str:
    parts = []
    for c in items:
        lines = [f'<story id="{c.cluster_id}" section="{data(headings.get(c.section_key, ""))}">']
        for n, (outlet, title, snippet) in enumerate(headlines.get(c.cluster_id, [])):
            lines.append(f'<headline outlet="{data(outlet)}">{data(title)}</headline>')
            if n < 2 and snippet:
                lines.append(f"<snippet>{data(snippet)}</snippet>")
        lines.append("</story>")
        parts.append("\n".join(lines))
    return f'Edition: "{data(edition)}"\n\n' + "\n\n".join(parts)


async def write(session: AsyncSession, llm: LlmSettings, edition: str, items: list[Candidate],
                headings: dict[str, str], subscription_id: uuid.UUID | None,
                sub: Subscription | None = None) -> tuple[str | None, bool, list[Candidate]]:
    """Returns (overview, written by the LLM?, items kept). A subscription
    with its own provider uses only that; otherwise the platform's."""
    if not items:
        return None, False, items
    prompt = load("digest_writer")
    user = build_prompt(edition, items, headings, await _headlines(session, [c.cluster_id for c in items]))
    system = prompt.text.replace("{edition}", edition)
    try:
        if sub is not None and sub.llm_config:
            result = await run_own(session, sub, "digest_writer", system, user, llm.timeout_s)
        else:
            result = await run_task(session, llm, "digest_writer", system, user, subscription_id=subscription_id)
    except LLMError:
        return None, False, items
    if result is None:
        return None, False, items
    try:
        out = WriterOut.model_validate(parse_json(result.text))
    except (LLMError, ValidationError) as e:
        log.warning("invalid digest writer output", extra={"error": str(e)[:300]})
        return None, False, items

    written = {s.id: s for s in out.stories}
    dropped = set(out.dropped)
    kept = []
    now = datetime.now(UTC)
    for c in items:
        if str(c.cluster_id) in dropped and not c.chosen_headline and not c.pinned:
            c.is_headline = False
            continue
        w = written.get(str(c.cluster_id))
        if w is not None:
            if "title" not in c.locked:
                c.title = w.title
            if "summary" not in c.locked:
                c.summary = w.summary
            if "why_it_matters" not in c.locked:
                c.why_it_matters = w.why_it_matters or None
            await session.execute(text("""
                UPDATE clusters SET
                    title = CASE WHEN 'title' = ANY(locked_fields) THEN title ELSE :t END,
                    summary = CASE WHEN 'summary' = ANY(locked_fields) THEN summary ELSE :s END,
                    why_it_matters = CASE WHEN 'why_it_matters' = ANY(locked_fields) THEN why_it_matters ELSE :w END,
                    summary_item_count = :n, summary_written_at = :at
                WHERE id = :id
            """), {"t": c.title, "s": c.summary, "w": c.why_it_matters, "n": c.item_count, "at": now, "id": c.cluster_id})
        kept.append(c)
    if kept and not any(c.is_headline for c in kept):
        kept[0].is_headline = True
    await session.commit()
    return out.overview or None, True, kept
