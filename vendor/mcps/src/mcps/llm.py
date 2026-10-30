"""Async client for the local OpenAI-compatible model server."""

from __future__ import annotations

import os
from typing import Any

import httpx
from openai import AsyncOpenAI

DEFAULT_BASE_URL = "http://127.0.0.1:10001/v1"
DEFAULT_MODEL = "/opt/models/language/Qwen3.8-9B-Q8_0.gguf"
DEFAULT_API_KEY = "sk-local"

BASE_URL = os.environ.get("LOCAL_AI_BASE_URL", DEFAULT_BASE_URL)
MODEL = os.environ.get("LOCAL_AI_MODEL", DEFAULT_MODEL)
API_KEY = os.environ.get("LOCAL_AI_API_KEY", DEFAULT_API_KEY)
TIMEOUT = float(os.environ.get("LOCAL_AI_TIMEOUT", "300"))
MAX_TOKENS = int(os.environ.get("LOCAL_AI_MAX_TOKENS", "1024"))
MAX_RETRIES = int(os.environ.get("LOCAL_AI_MAX_RETRIES", "2"))
CONCURRENCY = int(os.environ.get("LOCAL_AI_CONCURRENCY", "4"))
# This Qwen3 build spends its token budget on reasoning by default and returns
# empty `content` unless thinking is disabled through the chat template.
ENABLE_THINKING = os.environ.get("LOCAL_AI_ENABLE_THINKING", "0") == "1"

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Return a lazily-created, shared client for the local server."""
    global _client
    if _client is None:
        # The endpoint is loopback; the openai SDK otherwise routes it through
        # HTTP(S)_PROXY inherited from the operator's shell, where it 502s.
        _client = AsyncOpenAI(
            base_url=BASE_URL,
            api_key=API_KEY,
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
            http_client=httpx.AsyncClient(trust_env=False),
        )
    return _client


async def complete(
    *,
    system: str,
    user: str,
    max_tokens: int | None = None,
    temperature: float = 0.2,
) -> str:
    """Run one chat completion against the local model and return its text content."""
    kwargs: dict[str, Any] = {}
    if not ENABLE_THINKING:
        kwargs["chat_template_kwargs"] = {"enable_thinking": False}
    for _ in range(2):
        response = await get_client().chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or MAX_TOKENS,
            temperature=temperature,
            extra_body=kwargs,
        )
        content = (response.choices[0].message.content or "").strip()
        if content:
            return content
    raise RuntimeError(
        "local model returned an empty response; it may have exhausted its "
        "token budget on reasoning. Increase LOCAL_AI_MAX_TOKENS or set "
        "LOCAL_AI_ENABLE_THINKING=1."
    )
