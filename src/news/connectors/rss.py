"""RSS and Atom feeds: news sites, blogs, Substack publications, journals."""

from __future__ import annotations

import calendar
import hashlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

import feedparser
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from news.connectors.base import HealthStatus, Mode, RawItem
from news.connectors.context import ConnectorContext
from news.connectors.http import FetchError


_IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def entry_image(e: Any) -> str | None:
    """The entry's image: media thumbnail or content, an image enclosure, or
    the first image in its HTML. Only http(s) URLs."""
    candidates: list[str] = []
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key) or []:
            if m.get("url") and (key == "media_thumbnail" or str(m.get("medium", "image")) == "image"
                                 or str(m.get("type", "")).startswith("image/")):
                candidates.append(m["url"])
    for link in e.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/") and link.get("href"):
            candidates.append(link["href"])
    for html_text in [e.get("summary") or ""] + [c.get("value", "") for c in e.get("content") or []]:
        if m := _IMG_SRC.search(html_text):
            candidates.append(m.group(1))
    for url in candidates:
        url = url.strip()
        if url.startswith(("https://", "http://")) and len(url) < 2000:
            return url
    return None


class RssConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    kind: Literal["news", "paper", "preprint"] = "news"
    # substack: polled no more than every 30 minutes (app.substack_min_poll_interval)
    platform: Literal["substack"] | None = None
    max_items: int = Field(100, ge=1, le=1000)


class RssConnector:
    type_name: ClassVar[str] = "rss"
    mode: ClassVar[Mode] = "poll"

    def __init__(self, ctx: ConnectorContext, config: RssConfig) -> None:
        self.ctx = ctx
        self.config = config

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return RssConfig

    async def _get_feed(self, conditional: bool) -> Any | None:
        headers: dict[str, str] = {}
        if conditional:
            if etag := self.ctx.cursor.get("etag"):
                headers["If-None-Match"] = etag
            if modified := self.ctx.cursor.get("last_modified"):
                headers["If-Modified-Since"] = modified
        await self.ctx.limiter.wait()
        resp = await self.ctx.http.get(str(self.config.url), headers)
        if resp.status == 304:
            return None
        if resp.status != 200:
            raise FetchError(f"HTTP {resp.status} from {self.config.url}")
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise FetchError(f"Not a valid feed: {feed.get('bozo_exception')}")
        if conditional:
            self.ctx.cursor.set("etag", resp.headers.get("etag"))
            self.ctx.cursor.set("last_modified", resp.headers.get("last-modified"))
        return feed

    async def fetch(self) -> AsyncIterator[RawItem]:
        feed = await self._get_feed(conditional=True)
        if feed is None:
            return
        lang = feed.feed.get("language")
        outlet = (feed.feed.get("title") or "").strip()[:120] or self.ctx.source_name
        for entry in feed.entries[: self.config.max_items]:
            item = self._to_raw(entry, lang, outlet)
            if item is not None:
                yield item

    def _to_raw(self, e: Any, lang: str | None, outlet: str) -> RawItem | None:
        title = (e.get("title") or "").strip()
        link = e.get("link")
        if not title and not link:
            return None
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime.fromtimestamp(calendar.timegm(e[key]), UTC)
                break
        external_id = e.get("id") or link or hashlib.sha1(f"{title}|{published}".encode()).hexdigest()
        # The summary, not the full content: many feeds (Substack, WordPress)
        # put the whole article and page furniture in content
        text = e.get("summary") or ""
        if not text and e.get("content"):
            text = e["content"][0].get("value", "")
        return RawItem(
            source_instance=str(self.ctx.source_id),
            external_id=str(external_id)[:2000],
            url=link,
            title=title or link,
            text=text,
            author=e.get("author"),
            published_at=published,
            lang=lang,
            kind=self.config.kind,
            image_url=entry_image(e),
            platform=self.config.platform,
            raw={"outlet": outlet, "feed_entry_id": e.get("id"),
                 "tags": [t.get("term") for t in e.get("tags", []) if t.get("term")]},
        )

    async def healthcheck(self) -> HealthStatus:
        try:
            feed = await self._get_feed(conditional=False)
        except FetchError as e:
            return HealthStatus(ok=False, detail=str(e))
        count = len(feed.entries) if feed is not None else 0
        return HealthStatus(ok=True, detail=f"{count} entries")
