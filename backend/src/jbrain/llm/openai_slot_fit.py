"""Slot pinning and caps for raw OpenAI-shape requests that bypass the router.

The proxies that forward a client's own chat-completions body to the gateway (the jcode
sandbox proxy, the external remote-coder proxy) cannot go through `LlmRouter`, so on a pooled
model they hold the body to a slot's cap and pin it here instead (FLASH_NEXT_ENGINE_PLAN §4a).
The client never chooses: any slot, parallel-choice or generation-length field it sent is
dropped first, or one request could land in jerv's slot or grow past its cap.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Final

from jbrain.llm import local_catalog, slot_roles
from jbrain.llm.slot_roles import SlotCapError, SlotRole

if TYPE_CHECKING:
    from jbrain.llm.kv_pool_guard import KvPoolBusyError, KvPoolGuard

# llama-server fields that choose a slot, bypass `max_tokens`, or ask for parallel choices
# (each choice multiplies what one request holds in the slot past the cap it is fitted to).
_CLIENT_OVERRIDES: Final = ("id_slot", "slot_id", "n_predict", "n", "n_cmpl")

# The budget filled in when a client names none. Charged in full against the pool by the
# guard, so the slot's whole remaining cap would reserve far more than a coding turn writes.
DEFAULT_OUTPUT_TOKENS: Final = 32_768


def strip_client_overrides(payload: dict[str, Any]) -> None:
    for key in _CLIENT_OVERRIDES:
        payload.pop(key, None)


def _text_chars(content: object) -> tuple[int, int]:
    """(characters, images) of one message's `content`: a string, or a list of parts."""
    if isinstance(content, str):
        return len(content), 0
    chars = images = 0
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            images += 1
        elif isinstance(part.get("text"), str):
            chars += len(part["text"])
    return chars, images


def prompt_chars(payload: dict[str, Any]) -> tuple[int, int]:
    """(characters, images) of an OpenAI body, counted the way `slot_roles.prompt_chars`
    counts the router's turns — content text, tool calls' names and arguments, tool schemas —
    so both feed `prefill`'s calibrated ratio the same kind of number. JSON punctuation and
    base64 image data are not text the model reads as tokens one for one, so neither counts;
    images are charged flat instead."""
    chars = images = 0
    messages = payload.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        text, pictures = _text_chars(message.get("content"))
        chars += text
        images += pictures
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                chars += slot_roles.tool_call_chars(
                    str(function.get("name") or ""), function.get("arguments") or ""
                )
    tools = payload.get("tools")
    for tool in tools if isinstance(tools, list) else []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict):
            chars += slot_roles.tool_schema_chars(
                str(function.get("name") or ""),
                str(function.get("description") or ""),
                function.get("parameters") or {},
            )
    return chars, images


def estimate_prompt_tokens(served: str, payload: dict[str, Any]) -> int:
    chars, images = prompt_chars(payload)
    return slot_roles.estimate_prompt_tokens(served, chars=chars, n_images=images)


def _budget(value: object) -> int | None:
    """A client's output budget as a positive int: an int, an integral float, or a numeric
    string all occur in the wild. Anything else (null, a bool, junk) reads as none."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    return value if isinstance(value, int) and value > 0 else None


def _requested_output(payload: dict[str, Any]) -> int | None:
    for key in ("max_tokens", "max_completion_tokens"):
        value = _budget(payload.get(key))
        if value is not None:
            return value
    return None


def _set_budget(payload: dict[str, Any], tokens: int) -> None:
    """Write the fitted budget to every field a server might read. A `max_completion_tokens`
    the client sent stays only as the fitted value, never its own."""
    payload["max_tokens"] = tokens
    if "max_completion_tokens" in payload:
        payload["max_completion_tokens"] = tokens


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
        _set_budget(payload, min(room, DEFAULT_OUTPUT_TOKENS))
        return prompt_tokens
    admission = slot_roles.admit(pool, role, prompt_tokens=prompt_tokens, max_tokens=asked)
    _set_budget(payload, admission.max_tokens)
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


def pool_busy(exc: KvPoolBusyError) -> dict[str, Any]:
    """OpenAI's error body for a call the shared KV pool cannot hold yet: a server-side,
    retryable condition, not the client's fault."""
    return {"error": {"message": str(exc), "type": "server_error", "code": "kv_pool_busy"}}


def error_bytes(body: dict[str, Any], *, stream: bool) -> bytes:
    """An error for a relay whose 200 headers are already sent: on a stream, the one SSE
    `data:` frame OpenAI clients read as an error, then `[DONE]`; otherwise the bare JSON body.
    Either way the client sees why, instead of a cut stream or an empty 200."""
    data = json.dumps(body)
    return f"data: {data}\n\ndata: [DONE]\n\n".encode() if stream else data.encode()


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
        _set_budget(payload, placement.max_tokens)
        if placement.slot is not None:
            payload["id_slot"] = placement.slot
        yield
