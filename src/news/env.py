"""Process environment. Secrets come only from here, never from config files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Env:
    database_url: str
    redis_url: str
    config_dir: Path
    user_agent: str


@lru_cache
def env() -> Env:
    return Env(
        database_url=os.environ["NEWS_DATABASE_URL"],
        redis_url=os.environ.get("NEWS_REDIS_URL", "redis://redis:6379/0"),
        config_dir=Path(os.environ.get("NEWS_CONFIG_DIR", "config")),
        user_agent=os.environ.get(
            "NEWS_USER_AGENT", "NewsBot/0.1 (+https://news.new.vote; contact@new.vote)"
        ),
    )
