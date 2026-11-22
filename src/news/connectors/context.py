"""What a connector gets from the service: HTTP, a cursor that survives
restarts, a rate limiter and a logger."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from news.connectors.http import SafeHttp


class CursorStore:
    """A connector's resume state (ETags, last-seen ids…). Saved to the
    source instance after a successful run; never saved in dry runs."""

    def __init__(self, initial: dict[str, Any] | None = None, dry_run: bool = False) -> None:
        self.data: dict[str, Any] = dict(initial or {})
        self.dry_run = dry_run
        self.dirty = False

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self.data.get(key) != value:
            self.data[key] = value
            self.dirty = True


class RateLimiter:
    """Spaces requests evenly to at most `per_minute`."""

    def __init__(self, per_minute: int | None) -> None:
        self._interval = 60.0 / per_minute if per_minute else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


@dataclass
class ConnectorContext:
    source_id: uuid.UUID
    source_name: str
    http: SafeHttp
    cursor: CursorStore = field(default_factory=CursorStore)
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(None))
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("news.connector"))
    # Search phrases of the subscriptions that want this source's items, for
    # connectors that search rather than read a feed (uses_subscription_phrases)
    phrases: list[str] = field(default_factory=list)
