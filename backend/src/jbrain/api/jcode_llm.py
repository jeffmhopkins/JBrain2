"""Residency-aware, multi-model proxy for the jcode sandbox's grok CLI (live `/model`).

The sandbox's grok CLI lists every installed tool-capable local model and lets the owner
switch between them live with `/model` — plan on the reasoner (gpt-oss-120b), execute on
the coder (qwen3-coder-next). On a single unified-memory box two large models can't
co-reside, and the gateway is a `swap: false` group that never evicts on its own, so a
switch would otherwise STACK both and drive the box into a reclaim-livelock freeze. This
proxy runs the residency evictor (jbrain.llm.residency.ensure_room) before forwarding each
completion, so a switch frees the fewest resident models to hold the free-RAM floor and
the gateway's on-demand load of the switched-to model fits — a safe cold swap.

Every completion is serialized through a single per-box swap lock (app.state), so only ONE
model is ever loading/serving at a time: a request for a DIFFERENT model waits for the
in-flight one to finish, then cold-swaps. That is what makes parallel agents / concurrent
turns safe — without it two requests would each evict the other's model and load both at
once, the exact thrash we're avoiding.

Internal-only: reachable from the jcode sandbox over the `jcode` docker network, Bearer-
authed with the shared jcode gateway token (the sandbox already holds it — compose passes
it as GROK_API_KEY). Contrast `external_llm`, a metered proxy for a REMOTE coder that PINS
the model and refuses an unloaded one; here we honour the caller's choice and trigger the
load — that is the whole point.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from jbrain.llm import local_catalog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

router = APIRouter()

# Long completions: a live `/model` switch cold-loads the model, adding tens of seconds
# before the first token, so the upstream read must not time out under it.
_TIMEOUT = httpx.Timeout(600.0)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _authorize(request: Request) -> None:
    """Fail-closed shared-token auth: the caller must present the jcode gateway secret.
    An empty configured token (code mode not provisioned) refuses every call."""
    token = getattr(request.app.state.settings, "jcode_gateway_token", "") or ""
    if not token or _bearer(request) != token:
        raise HTTPException(status_code=401, detail="unauthorized")


def _models(request: Request) -> tuple[local_catalog.LocalModel, ...]:
    settings = request.app.state.settings
    return local_catalog.jcode_models(
        getattr(settings, "local_llm_enabled", False),
        getattr(settings, "local_models", []),
    )


# Short, unique `/model` handles for the sandbox's grok CLI, keyed by served name. grok's
# config block key — what `/model`, `[models] default`, and `[subagents.models]` reference —
# becomes the alias; the block's `model =` stays the real served name the proxy validates
# and forwards. A served name with no entry keeps itself as the handle (no collision).
_ALIASES: dict[str, str] = {
    "gpt-oss-120b": "oss",
    "qwen3-coder-next": "qwen",
    "qwen3-coder-next-q8": "qwen-q8",
    "qwen3-vl-30b-a3b": "vl",
    "llama-4-scout-int4": "scout",
    "nemotron-3-super-120b": "nemotron",
    "qwen3-235b-a22b": "qwen-235b",
    "qwen3-next-80b-a3b": "qwen-next",
    "qwen3-next-80b-a3b-thinking": "qwen-next-think",
    "glm-4.5-air": "glm",
    "qwen3-30b-a3b": "qwen-30b",
    "qwen3.5-0.8b": "qwen-tiny",
    "qwen3.5-4b": "qwen-4b",
    "llama-3.3-70b": "llama",
}


def _alias(served: str) -> str:
    return _ALIASES.get(served, served)


@router.get("/jcode/llm/v1/models")
async def list_models(request: Request) -> Response:
    """The installed tool-capable models the sandbox offers via grok's `/model`.

    Default: OpenAI `{"object":"list","data":[…]}` (grok and other clients probe this) —
    `id` is the real served name. With `?format=lines`, an `alias|served|label|context_window`
    text block grok-config.sh renders into one `[model."alias"]` entry each (short `/model`
    handles, real served name in `model =`) — no JSON parsing in the shell."""
    _authorize(request)
    models = _models(request)
    if request.query_params.get("format") == "lines":
        body = "".join(
            f"{_alias(m.served_model)}|{m.served_model}|{m.label}|{m.context_window}\n"
            for m in models
        )
        return Response(content=body, media_type="text/plain")
    data = [
        {"id": m.served_model, "object": "model", "created": 0, "owned_by": "jbrain"}
        for m in models
    ]
    return Response(
        content=json.dumps({"object": "list", "data": data}),
        media_type="application/json",
    )


@router.post("/jcode/llm/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Forward an OpenAI chat completion to the on-box gateway for the caller's CHOSEN
    model, first making room for it (evict-to-budget) so a live `/model` switch cold-swaps
    safely. 400 for a model outside the installed tool-capable set — a bad name must never
    drive an eviction."""
    _authorize(request)
    gateway_url = getattr(request.app.state.settings, "local_llm_url", "") or ""
    if not gateway_url:
        raise HTTPException(status_code=503, detail="on-box LLM is not configured")

    try:
        payload = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    served = str(payload.get("model") or "")
    if served not in {m.served_model for m in _models(request)}:
        raise HTTPException(status_code=400, detail=f"unknown or unavailable model: {served!r}")

    residency = getattr(request.app.state, "residency", None)
    # One model loading/serving at a time on the box: hold the swap lock across BOTH the
    # evict-to-budget and the streamed completion, so a concurrent request for a different
    # model waits (then cold-swaps) instead of loading a second model on top. The lock is
    # created on app.state at startup; absent only in unit tests that drive the router
    # directly, where a nullcontext keeps the single-request path working.
    swap_lock = getattr(request.app.state, "jcode_llm_swap_lock", None)
    # The gateway HTTP client is injectable (app.state) so a test can fake the upstream
    # without patching global httpx; defaults to a real client against the gateway.
    factory = getattr(request.app.state, "jcode_llm_client_factory", None) or httpx.AsyncClient
    client = factory(base_url=gateway_url.rstrip("/"), timeout=_TIMEOUT)

    async def relay() -> AsyncIterator[bytes]:
        guard = swap_lock if swap_lock is not None else contextlib.nullcontext()
        try:
            async with guard:
                # Evict-to-budget for the chosen model BEFORE the gateway loads it on the
                # forwarded request; both inside the lock so it can't be swapped out mid-stream.
                # Best-effort — a residency hiccup degrades to the gateway's own load.
                if residency is not None:
                    try:
                        await residency.ensure_room(served)
                    except Exception:  # noqa: BLE001 - housekeeping never fails a completion
                        log.warning("jcode-llm ensure_room failed model=%s", served, exc_info=True)
                # Stream the gateway's response back verbatim (SSE or whole JSON). The
                # gateway is unauthenticated on the internal network — no upstream credential.
                async with client.stream("POST", "/chat/completions", json=payload) as upstream:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
        finally:
            await client.aclose()

    media = "text/event-stream" if payload.get("stream") else "application/json"
    return StreamingResponse(relay(), media_type=media)
