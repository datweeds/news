"""OpenAlex: scientific papers and preprints, from the open index at
api.openalex.org (journals plus arXiv, bioRxiv, medRxiv and other preprint
servers; free, no key).

Unlike a feed, there are far too many papers to take them all, so this
connector searches: for each search phrase of the subscriptions that want
research, the most relevant works published in the last `days` days. Each
phrase is searched again at most once every `research_hours`, so a phrase
added to a subscription is searched on the next poll."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from news.connectors.base import HealthStatus, Mode, RawItem
from news.connectors.context import ConnectorContext
from news.connectors.http import FetchError

API = "https://api.openalex.org/works"
ABSTRACT_MAX = 3000


class OpenAlexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(90, ge=1, le=365, description="Only works published in the last this many days")
    per_phrase: int = Field(15, ge=1, le=50, description="Most relevant works taken per phrase")
    research_hours: int = Field(24, ge=1, le=720, description="How often each phrase is searched again")
    types: list[Literal["article", "preprint", "review"]] = ["article", "preprint", "review"]
    language: str | None = "en"
    mailto: str | None = Field(None, description="Contact address: OpenAlex's faster 'polite pool'")
    # A free key from openalex.org keeps search working when OpenAlex pauses
    # anonymous search under load. Written as ${OPENALEX_API_KEY}; never stored.
    api_key: str | None = None
    phrases: list[str] = Field(default_factory=list, description="Extra phrases searched as well as subscriptions'")


def abstract_text(inverted: dict[str, list[int]] | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    if not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for p in positions:
            slots[p] = word
    return " ".join(slots[p] for p in sorted(slots))


def is_preprint(work: dict[str, Any]) -> bool:
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    return work.get("type") == "preprint" or source.get("type") == "repository"


class OpenAlexConnector:
    type_name: ClassVar[str] = "openalex"
    mode: ClassVar[Mode] = "poll"
    uses_subscription_phrases: ClassVar[bool] = True

    def __init__(self, ctx: ConnectorContext, config: OpenAlexConfig) -> None:
        self.ctx = ctx
        self.config = config

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return OpenAlexConfig

    def _phrases(self) -> list[str]:
        out: list[str] = []
        for p in [*self.config.phrases, *self.ctx.phrases]:
            p = " ".join(p.split())
            if p and p.lower() not in {x.lower() for x in out}:
                out.append(p)
        return out

    def _url(self, phrase: str, since: date) -> str:
        filters = [f"from_publication_date:{since.isoformat()}", "has_abstract:true",
                   "type:" + "|".join(self.config.types)]
        if self.config.language:
            filters.append(f"language:{self.config.language}")
        params = {"search": phrase, "filter": ",".join(filters), "sort": "relevance_score:desc",
                  "per_page": str(self.config.per_phrase)}
        if self.config.mailto:
            params["mailto"] = self.config.mailto
        if self.config.api_key and not self.config.api_key.startswith("${"):
            params["api_key"] = self.config.api_key
        return f"{API}?{urlencode(params)}"

    async def search(self, phrase: str, now: datetime | None = None) -> list[RawItem]:
        now = now or datetime.now(UTC)
        await self.ctx.limiter.wait()
        resp = await self.ctx.http.get(self._url(phrase, (now - timedelta(days=self.config.days)).date()))
        if resp.status != 200:
            try:
                detail = json.loads(resp.content).get("message") or ""
            except (ValueError, AttributeError):
                detail = ""
            raise FetchError(f"HTTP {resp.status} from OpenAlex{': ' + str(detail)[:300] if detail else ''}")
        try:
            results = json.loads(resp.content).get("results") or []
        except (ValueError, AttributeError) as e:
            raise FetchError(f"OpenAlex sent something that isn't JSON: {e}") from e
        return [item for w in results if (item := self.to_raw(w, phrase)) is not None]

    async def fetch(self) -> AsyncIterator[RawItem]:
        now = datetime.now(UTC)
        searched: dict[str, str] = dict(self.ctx.cursor.get("searched") or {})
        due = timedelta(hours=self.config.research_hours)
        phrases = self._phrases()
        for phrase in phrases:
            last = searched.get(phrase.lower())
            if last and now - datetime.fromisoformat(last) < due:
                continue
            for item in await self.search(phrase, now):
                yield item
            searched[phrase.lower()] = now.isoformat()
        # Forget phrases no subscription uses any more
        keep = {p.lower() for p in phrases}
        self.ctx.cursor.set("searched", {k: v for k, v in searched.items() if k in keep})

    def to_raw(self, w: dict[str, Any], phrase: str) -> RawItem | None:
        work_id = w.get("id")
        title = (w.get("display_name") or w.get("title") or "").strip()
        if not work_id or not title:
            return None
        loc = w.get("primary_location") or {}
        source = loc.get("source") or {}
        oa = w.get("open_access") or {}
        doi = w.get("doi")
        url = oa.get("oa_url") or loc.get("landing_page_url") or doi or work_id
        published = None
        if pd := w.get("publication_date"):
            try:
                published = datetime.fromisoformat(pd).replace(tzinfo=UTC)
            except ValueError:
                published = None
        authors = [a.get("author", {}).get("display_name") for a in w.get("authorships") or []]
        authors = [a for a in authors if a]
        byline = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "") if authors else None
        abstract = abstract_text(w.get("abstract_inverted_index"))
        preprint = is_preprint(w)
        venue = source.get("display_name")
        return RawItem(
            source_instance=str(self.ctx.source_id),
            external_id=str(work_id)[:2000],
            url=url,
            title=title,
            text=abstract,
            author=byline,
            published_at=published,
            lang=w.get("language"),
            kind="preprint" if preprint else "paper",
            engagement={"citations": int(w.get("cited_by_count") or 0)},
            raw={
                "outlet": venue or ("Preprint" if preprint else "Journal article"),
                "openalex_id": work_id, "doi": doi, "venue": venue, "venue_type": source.get("type"),
                "authors": authors[:20], "author_count": len(authors), "type": w.get("type"),
                "open_access": bool(oa.get("is_oa")), "oa_url": oa.get("oa_url"),
                "landing_url": loc.get("landing_page_url"), "cited_by": int(w.get("cited_by_count") or 0),
                "relevance": w.get("relevance_score"), "found_by": phrase, "abstract": abstract[:ABSTRACT_MAX],
            },
        )

    async def healthcheck(self) -> HealthStatus:
        phrase = (self._phrases() or ["climate"])[0]
        try:
            items = await self.search(phrase)
        except FetchError as e:
            return HealthStatus(ok=False, detail=str(e))
        return HealthStatus(ok=True, detail=f"{len(items)} works for “{phrase}”")
