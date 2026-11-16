"""Provider-agnostic LLM access with a hard daily budget."""

from news.llm.client import LLMClient, LLMResult, client_for

__all__ = ["LLMClient", "LLMResult", "client_for"]
