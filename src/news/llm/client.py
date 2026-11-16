"""LLM clients: Anthropic, any OpenAI-compatible endpoint (vLLM, Ollama…),
and a deterministic fake for tests. The LLM never calls tools; source text
goes into prompts as data, and outputs are schema-validated by the caller."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from news.config import ModelConfig


@dataclass
class LLMResult:
    text: str
    model: str
    tokens_in: int
    tokens_out: int


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    async def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult: ...


class AnthropicClient:
    def __init__(self, cfg: ModelConfig, timeout_s: int, api_key: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        key = api_key or os.environ.get(cfg.api_key_env)
        if not key:
            raise LLMError(f"{cfg.api_key_env} is not set")
        self._client = AsyncAnthropic(api_key=key, timeout=timeout_s, max_retries=2)
        self._model = cfg.model

    async def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        from anthropic import APIError

        try:
            msg = await self._client.messages.create(
                model=self._model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except APIError as e:
            raise LLMError(str(e)) from e
        text = "".join(block.text for block in msg.content if block.type == "text")
        return LLMResult(text=text, model=msg.model, tokens_in=msg.usage.input_tokens,
                         tokens_out=msg.usage.output_tokens)


class OpenAICompatClient:
    def __init__(self, cfg: ModelConfig, timeout_s: int, api_key: str | None = None) -> None:
        if not cfg.base_url:
            raise LLMError("openai_compat needs base_url")
        self._url = cfg.base_url.rstrip("/") + "/chat/completions"
        self._key = api_key if api_key is not None else os.environ.get(cfg.api_key_env, "")
        self._model = cfg.model
        self._timeout = timeout_s

    async def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        body = {"model": self._model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(str(e)) from e
        usage = data.get("usage") or {}
        return LLMResult(text=data["choices"][0]["message"]["content"], model=self._model,
                         tokens_in=int(usage.get("prompt_tokens", 0)), tokens_out=int(usage.get("completion_tokens", 0)))


class FakeClient:
    """Deterministic answers in the shapes the prompts ask for."""

    def __init__(self, cfg: ModelConfig, timeout_s: int, api_key: str | None = None) -> None:
        self._model = cfg.model

    async def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        if "<item " in prompt:
            ids = re.findall(r'<item id="(\d+)"', prompt)
            out: Any = {"items": [{
                "id": int(i), "topics": ["energy policy"], "entities": [{"name": "Scottish Government", "type": "org"}],
                "locations": [{"name": "Scotland", "country": "GB", "role": "primary"}],
                "significance": 0.7, "novelty": "new", "promotional": False,
            } for i in ids]}
        else:
            ids = re.findall(r'<story id="([^"]+)"', prompt)
            out = {"overview": "A quiet day with a few notable developments.", "stories": [{
                "id": s, "title": f"Story {n + 1}", "summary": "What happened, in two sentences. Written by the fake.",
                "why_it_matters": "It affects members of this Society.",
            } for n, s in enumerate(ids)], "dropped": []}
        text = json.dumps(out)
        return LLMResult(text=text, model=self._model, tokens_in=len(prompt) // 4, tokens_out=len(text) // 4)


def client_for(cfg: ModelConfig, timeout_s: int, api_key: str | None = None) -> LLMClient:
    if cfg.provider == "anthropic":
        return AnthropicClient(cfg, timeout_s, api_key)
    if cfg.provider == "openai_compat":
        return OpenAICompatClient(cfg, timeout_s, api_key)
    return FakeClient(cfg, timeout_s, api_key)


def parse_json(text: str) -> Any:
    """The JSON object in a reply, tolerating code fences or a preamble."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("No JSON object in the reply")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Invalid JSON: {e}") from e
