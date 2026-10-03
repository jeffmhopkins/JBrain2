"""Slot pinning and caps for raw OpenAI-shape requests that bypass the router.

The proxies that forward a client's own chat-completions body to the gateway (the jcode
sandbox proxy, the external remote-coder proxy) cannot go through `LlmRouter`, so on a pooled
model they hold the body to a slot's cap and pin it here instead (FLASH_NEXT_ENGINE_PLAN §4a).
The client never chooses: any slot or generation-length field it sent is dropped first, or one
request could land in jerv's slot or grow to the whole trained context past its cap.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Final

from jbrain.llm import local_catalog, slot_roles
from jbrain.llm.slot_roles import SlotCapError, SlotRole

if TYPE_CHECKING:
    from jbrain.llm.kv_pool_guard import KvPoolGuard

# llama-server fields that choose a slot or bypass `max_tokens`; only the proxy sets these.
_CLIENT_OVERRIDES: Final = ("id_slot", "slot_id", "n_predict")

# The budget filled in when a client names none. Charged in full against the pool by the
# guard, so the slot's whole remaining cap would reserve far more than a coding turn writes.
DEFAULT_OUTPUT_TOKENS: Final = 32_768


def strip_client_overrides(payload: dict[str, Any]) -> None:
    for key in _CLIENT_OVERRIDES:
        payload.pop(key, None)


def estimate_prompt_tokens(served: str, payload: dict[str, Any]) -> int:
    """The body's messages and tools as sent, with each image charged flat (its base64
    characters would swamp the estimate)."""
    images = 0
    stripped: list[object] = []
    messages = payload.get("messages")
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            text = [
                p for p in content if not (isinstance(p, dict) and p.get("type") == "image_url")
            ]
            images += len(content) - len(text)
            message = {**message, "content": text}
        stripped.append(message)
    chars = len(json.dumps(stripped)) + len(json.dumps(payload.get("tools") or []))
    return slot_roles.estimate_prompt_tokens(served, chars=chars, n_images=images)


def _requested_output(payload: dict[str, Any]) -> int | None:
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def fit_openai_request(served: str, payload: dict[str, Any], role: SlotRole) -> int:
    """Strip the client's slot overrides, then hold a pooled model's request to `role`'s cap,
    rewriting its output budget in `payload`. Returns the prompt estimate for `pinned_request`
    (0 for a model without a pool). Raises `SlotCapError` when the prompt cannot fit."""
    strip_client_overrides(payload)
    pool = local_catalog.pool_of(served)
    if pool is None:
        return 0
    prompt_tokens = estimate_prompt_tokens(served, payload)
    asked = _requested_output(payload)
    cap = pool.cap(role)
    if asked is None:
        room = cap - prompt_tokens
        if room < slot_roles.MIN_CLAMPED_OUTPUT:
            raise SlotCapError(
                role, cap=cap, prompt_tokens=prompt_tokens, max_tokens=slot_roles.MIN_CLAMPED_OUTPUT
            )
        payload["max_tokens"] = min(room, DEFAULT_OUTPUT_TOKENS)
        return prompt_tokens
    admission = slot_roles.admit(pool, role, prompt_tokens=prompt_tokens, max_tokens=asked)
    payload["max_tokens"] = admission.max_tokens
    if "max_completion_tokens" in payload:
        payload["max_completion_tokens"] = admission.max_tokens
    return prompt_tokens


def context_length_exceeded(exc: SlotCapError) -> dict[str, Any]:
    """OpenAI's error body, so a client reports a context overflow (and can compact) rather than
    a broken gateway. Sizes only, never prompt text."""
    return {
        "error": {
            "message": str(exc),
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
    }


@contextlib.asynccontextmanager
async def pinned_request(
    guard: KvPoolGuard | None,
    model: str,
    payload: dict[str, Any],
    prompt_tokens: int,
    role: SlotRole,
) -> AsyncIterator[None]:
    """Pin a fitted request to `role`'s slot for as long as the caller streams it; unpinned it
    lands wherever llama-server picks and evicts a primed role's prefix. A no-op off a pool."""
    pool = local_catalog.pool_of(model)
    if pool is None:
        yield
        return
    if guard is None:
        payload["id_slot"] = pool.slot(role)
        yield
        return
    async with guard.placed(
        model,
        pool,
        role,
        prompt_tokens=prompt_tokens,
        max_tokens=int(payload.get("max_tokens") or 0),
    ) as placement:
        payload["max_tokens"] = placement.max_tokens
        if "max_completion_tokens" in payload:
            payload["max_completion_tokens"] = placement.max_tokens
        if placement.slot is not None:
            payload["id_slot"] = placement.slot
        yield
