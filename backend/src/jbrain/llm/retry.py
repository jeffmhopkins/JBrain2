"""Shared HTTP POST with bounded retries and exponential backoff.

Retries cover 429, 5xx, and network errors only — auth failures and other
4xx are deterministic, so retrying them just burns rate limit. Error logs
carry status and provider, never request or response bodies: prompts contain
private notes.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import structlog

from jbrain.llm.errors import (
    LlmAuthError,
    LlmBadResponseError,
    LlmContextOverflowError,
    LlmRateLimitError,
    LlmTransientError,
)

log = structlog.get_logger()

MAX_TRIES = 4
BASE_DELAY_SECONDS = 0.5


def _looks_like_context_overflow(body_text: str) -> bool:
    """Whether a rejected request's body says the prompt outgrew the context window.
    llama.cpp/llama-swap answers 400 with "the request exceeds the available context
    size ... n_ctx = ..."; other OpenAI servers phrase it around n_ctx / context
    length. Matched tolerantly (wording shifts across builds) but narrowly enough not
    to swallow unrelated 400s. Only the CLASSIFICATION reads the body — the raised
    error carries none of it (bodies can echo the private prompt)."""
    t = body_text.lower()
    if "n_ctx" in t or "context size" in t or "context window" in t or "context length" in t:
        return True
    return "context" in t and (
        "exceed" in t or "too long" in t or "too large" in t or "larger than" in t
    )


def _terminal_4xx(provider: str, status_code: int, body_text: str) -> LlmBadResponseError:
    """The exception for a non-retryable 4xx: a context-overflow subtype when the body
    says so (so callers can tell the owner the model ran out of context), else a plain
    bad-response. Neither carries the body — just the status and provider."""
    if _looks_like_context_overflow(body_text):
        return LlmContextOverflowError(f"{provider}: context window exceeded")
    return LlmBadResponseError(f"{provider}: HTTP {status_code}")


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
            raise _terminal_4xx(provider, resp.status_code, resp.text)
        log.warning("llm.retry", provider=provider, status=resp.status_code, attempt=attempt + 1)
    raise last_error


async def _iter_sse_data(resp: httpx.Response, provider: str) -> AsyncIterator[dict[str, Any]]:
    """Yield the parsed JSON of each SSE `data:` line. `[DONE]` ends the stream
    (OpenAI's sentinel); Anthropic carries the same shape on every event's data,
    so the event: line is ignored and the payload's own `type` is the switch."""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            if data == "[DONE]":
                return
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError as exc:
            raise LlmBadResponseError(f"{provider}: malformed SSE data line") from exc


async def stream_sse(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
    request_timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[dict[str, Any]]:
    """POST a streaming request and yield each parsed SSE event.

    Retries the same failures `post_json` does — but only *until the response is
    accepted* (a 200). Once accepted, the generation has begun server-side, so a
    drop while reading the body fails hard rather than replaying: retrying would
    re-bill a non-idempotent generation, and SSE has no resume. Same body-free
    error logs (prompts are private)."""
    last_error: Exception = LlmTransientError(f"{provider}: no attempt made")
    for attempt in range(MAX_TRIES):
        if attempt:
            await sleep(BASE_DELAY_SECONDS * 2 ** (attempt - 1))
        committed = False
        try:
            # The stream depends on the client, so these can't collapse to one
            # `with` (SIM117) — the second context references the first's value.
            async with httpx.AsyncClient(  # noqa: SIM117
                timeout=request_timeout, transport=transport
            ) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        if resp.status_code in (401, 403):
                            raise LlmAuthError(f"{provider}: HTTP {resp.status_code}")
                        if resp.status_code == 429:
                            last_error = LlmRateLimitError(f"{provider}: HTTP 429")
                        elif resp.status_code >= 500:
                            last_error = LlmTransientError(f"{provider}: HTTP {resp.status_code}")
                        else:
                            # Body already pulled by aread() above; classify (context
                            # overflow vs generic) without logging it.
                            raise _terminal_4xx(provider, resp.status_code, resp.text)
                        log.warning(
                            "llm.retry",
                            provider=provider,
                            status=resp.status_code,
                            attempt=attempt + 1,
                        )
                        continue
                    # 200: the request is accepted and the model is generating —
                    # no failure past here may retry (it would re-bill a turn).
                    committed = True
                    async for event in _iter_sse_data(resp, provider):
                        yield event
                    return
        except httpx.HTTPError as exc:
            if committed:
                raise LlmTransientError(
                    f"{provider}: stream interrupted: {type(exc).__name__}"
                ) from exc
            last_error = LlmTransientError(f"{provider}: network error: {type(exc).__name__}")
            log.warning("llm.retry", provider=provider, reason="network", attempt=attempt + 1)
            continue
    raise last_error
