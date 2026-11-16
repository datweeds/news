"""Versioned prompt files in prompts/. Each starts with a front-matter block
holding `version:`; the version is stored with every enrichment."""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    version: str
    text: str


@lru_cache
def load(name: str) -> Prompt:
    base = Path(os.environ.get("NEWS_PROMPTS_DIR", Path(__file__).resolve().parents[2] / "prompts"))
    raw = (base / f"{name}.md").read_text(encoding="utf-8")
    version = name
    if raw.startswith("---"):
        _, header, raw = raw.split("---", 2)
        for line in header.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "version":
                version = value.strip()
    return Prompt(version=version, text=raw.strip())


def data(text: str | None) -> str:
    """Source text for inside a prompt's tags: escaped so it can't close
    the tag or open new ones."""
    return html.escape(text or "", quote=True)
