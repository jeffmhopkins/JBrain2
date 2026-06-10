"""Shared HTTP POST with bounded retries and exponential backoff.

Retries cover 429, 5xx, and network errors only — auth failures and other
4xx are deterministic, so retrying them just burns rate limit. Error logs
carry status and provider, never request or response bodies: prompts contain
private notes.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from jbrain.llm.errors import (
    LlmAuthError,
    LlmBadResponseError,
    LlmRateLimitError,
    LlmTransientError,
)

log = structlog.get_logger()

MAX_TRIES = 4
BASE_DELAY_SECONDS = 0.5


async def post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
    # Not an asyncio deadline — the httpx per-request timeout (ASYNC109-safe name).
    request_timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """POST and return the parsed JSON body, retrying retryable failures."""
    last_error: Exception = LlmTransientError(f"{provider}: no attempt made")
    for attempt in range(MAX_TRIES):
        if attempt:
            await sleep(BASE_DELAY_SECONDS * 2 ** (attempt - 1))
        try:
            async with httpx.AsyncClient(timeout=request_timeout, transport=transport) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            last_error = LlmTransientError(f"{provider}: network error: {type(exc).__name__}")
            log.warning("llm.retry", provider=provider, reason="network", attempt=attempt + 1)
            continue
        if resp.status_code == 200:
            try:
                body: dict[str, Any] = resp.json()
            except json.JSONDecodeError as exc:
                raise LlmBadResponseError(f"{provider}: 200 with non-JSON body") from exc
            return body
        if resp.status_code in (401, 403):
            raise LlmAuthError(f"{provider}: HTTP {resp.status_code}")
        if resp.status_code == 429:
            last_error = LlmRateLimitError(f"{provider}: HTTP 429")
        elif resp.status_code >= 500:
            last_error = LlmTransientError(f"{provider}: HTTP {resp.status_code}")
        else:
            raise LlmBadResponseError(f"{provider}: HTTP {resp.status_code}")
        log.warning("llm.retry", provider=provider, status=resp.status_code, attempt=attempt + 1)
    raise last_error
