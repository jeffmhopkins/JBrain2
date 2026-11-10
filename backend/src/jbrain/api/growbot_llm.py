"""The GrowBot creature's model lane: an OpenAI chat proxy to the box's own engine.

docs/plans/GROWBOT_PLAN.md §2. The sidecar's `/api/llm[-stream]?target=openrouter` shim lands
here, so the stock app's think lane (text, and camera frames as `image_url` parts) runs on the
box and nothing leaves the house. The `jcode_llm.py` shape, not its routes or token: residency
makes room for the model before the gateway would load it on demand (the sidecar never talks
to llama-swap itself), the request is held to the GrowBot slot's cap on a pooled engine and
pinned there.

The box, not the page, picks the model. Upstream's app names a different model per lane (fast,
active, dream) and runs on a LAN phone, so honouring its names would let vendored page code
cold-swap the box many times a minute. Every turn runs on one creature model (the first
installed vision model of the serving engine, else its first model), and `/models` lists only
that one, so the sidecar's lanes all agree. Where a turn could swap (an engine with no slot
pool) it takes jcode's swap lock, the box-wide one-model-at-a-time rule; on a pooled engine
nothing swaps, so its own lock keeps a robot's one-line reply from queueing behind a long
coding turn.

Authenticated by `growbot.token` (api/internal_auth.py) and mounted under `/internal`, which
Caddy never routes. Unlike jcode, the box picks the reasoning level: the creature answers in
a sentence many times a minute, and a hybrid reasoner left at its template default thinks for
thousands of tokens first.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Final

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from jbrain.api.internal_auth import require_growbot
from jbrain.llm import engine as engines
from jbrain.llm import gpu_guard, local_catalog
from jbrain.llm.kv_pool_guard import KvPoolBusyError
from jbrain.llm.openai_compat import apply_local_reasoning
from jbrain.llm.openai_slot_fit import (
    context_length_exceeded,
    error_bytes,
    fit_openai_request,
    pinned_request,
    pool_busy,
    prompt_chars,
)
from jbrain.llm.residency import ResidencyError
from jbrain.llm.slot_roles import GROWBOT_ROLE, SlotCapError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

router = APIRouter(prefix="/growbot/llm/v1", dependencies=[Depends(require_growbot)])

# A cold swap to the creature's model adds tens of seconds before the first token.
_TIMEOUT = httpx.Timeout(600.0)
# Upstream's measured turn is ~10-25 reply tokens; this leaves a long answer room without
# booking a coder's budget against the pool. Also the ceiling: on an engine with no pool
# nothing else would bound what the page asks for.
DEFAULT_MAX_TOKENS: Final = 1024
_BUDGET_KEYS: Final = ("max_tokens", "max_completion_tokens")
# The fields an OpenAI chat turn needs. Everything else llama-server would honour (grammars,
# `n_probs`, `logit_bias`, `cache_prompt`, sampler chains, tools) costs the box work the
# creature never asks for, or defeats the pinned prefix, so it is dropped.
_FIELDS: Final = frozenset(
    {
        "model",
        "messages",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "reasoning_effort",
        "reasoning",
    }
)
# A camera frame is tens of KiB; this bounds one inline image's base64 text.
MAX_IMAGE_CHARS: Final = 4 * 1024 * 1024
# The levels a client may ask for; anything else, or none, runs with thinking off.
_LEVELS: Final = frozenset({"none", "low", "medium", "high"})
_DEFAULT_LEVEL: Final = "none"
# An image must arrive inline. llama-server fetches a remote `image_url` itself, so a URL here
# would let the creature's page make the box request any address it names (SSRF from the box).
_INLINE_IMAGE: Final = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,")
_PART_TYPES: Final = frozenset({"text", "image_url"})


def _unsafe_part(messages: list[Any]) -> str | None:
    """Why a message content part may not be forwarded, or None when every part is plain text
    or an inline image. Anything else (a remote image, a video or file part) is refused."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind not in _PART_TYPES:
                return f"content part {kind!r} is not accepted"
            if kind != "image_url":
                continue
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str) or not _INLINE_IMAGE.match(url):
                return "images must be inline data:image/...;base64, URLs"
            if len(url) > MAX_IMAGE_CHARS:
                return "image is too large"
    return None


async def _engine(request: Request) -> engines.Engine:
    active = getattr(request.app.state, "active_engine", None)
    return await active.get() if active is not None else engines.DEFAULT_ENGINE


async def _creature(request: Request) -> local_catalog.LocalModel | None:
    """The one model every creature turn runs on, or None when the engine has none installed.
    Whatever the page names is ignored, so no request can choose what loads on the box."""
    settings = request.app.state.settings
    models = local_catalog.growbot_models(
        getattr(settings, "local_llm_enabled", False),
        getattr(settings, "local_models", []),
        await _engine(request),
    )
    return next((m for m in models if m.supports_vision), models[0] if models else None)


def _client_level(payload: dict[str, Any]) -> str:
    """The reasoning level the app asked for (OpenAI's `reasoning_effort`, or OpenRouter's
    `reasoning.effort`), else thinking off."""
    asked = payload.get("reasoning_effort")
    reasoning = payload.get("reasoning")
    if not isinstance(asked, str) and isinstance(reasoning, dict):
        asked = reasoning.get("effort")
    return asked if isinstance(asked, str) and asked in _LEVELS else _DEFAULT_LEVEL


def _apply_level(payload: dict[str, Any], level: str, served: str) -> None:
    """Put the level on the wire the way the adapter encodes it for the model actually sent,
    after clearing the client's own fields: OpenRouter's `reasoning` object means nothing to
    llama-server."""
    for key in ("reasoning_effort", "reasoning", "chat_template_kwargs"):
        payload.pop(key, None)
    model = local_catalog.get_by_served(served)
    if model is not None and model.supports_reasoning:
        apply_local_reasoning(payload, level)


def _openai_error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "invalid_request_error", "code": code}},
    )


def _server_error(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "server_error", "code": code}}


@router.get("/models")
async def list_models(request: Request) -> Response:
    """The creature's model (one, or none), with whether it can see images."""
    model = await _creature(request)
    data = [
        {
            "id": m.served_model,
            "object": "model",
            "created": 0,
            "owned_by": "jbrain",
            "label": m.label,
            "vision": m.supports_vision,
        }
        for m in ((model,) if model is not None else ())
    ]
    return Response(
        content=json.dumps({"object": "list", "data": data}), media_type="application/json"
    )


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Forward the app's chat completion to the on-box gateway, admitted by residency, held
    to the GrowBot slot and streamed back verbatim (SSE when the body sets `stream`)."""
    gateway_url = getattr(request.app.state.settings, "local_llm_url", "") or ""
    if not gateway_url:
        raise HTTPException(status_code=503, detail="on-box LLM is not configured")
    try:
        payload = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise HTTPException(status_code=400, detail="body must be a chat completion request")

    creature = await _creature(request)
    if creature is None:
        return _openai_error(400, "no on-box model is installed", "model_not_found")
    served = creature.served_model
    unsafe = _unsafe_part(payload["messages"])
    if unsafe is not None:
        return _openai_error(400, unsafe, "invalid_content")
    payload = {k: v for k, v in payload.items() if k in _FIELDS}
    _chars, images = prompt_chars(payload)
    if images and not local_catalog.supports_vision(served):
        return _openai_error(400, f"{served} cannot see images", "vision_not_supported")
    payload["model"] = served
    level = _client_level(payload)
    for key in ("reasoning_effort", "reasoning"):
        payload.pop(key, None)
    budgets = [b for b in (payload.get(k) for k in _BUDGET_KEYS) if type(b) is int]
    for key in _BUDGET_KEYS:
        payload.pop(key, None)
    payload["max_tokens"] = min([DEFAULT_MAX_TOKENS, *(b for b in budgets if b > 0)])
    # Before the lock: a request too long for its slot must not wait a turn to be refused.
    try:
        prompt_tokens = fit_openai_request(served, payload, GROWBOT_ROLE)
    except SlotCapError as exc:
        return JSONResponse(status_code=400, content=context_length_exceeded(exc))

    state = request.app.state
    residency = getattr(state, "residency", None)
    pool_guard = getattr(state, "kv_pool_guard", None)
    # A pooled engine serves one model, so nothing swaps; anywhere else this turn may load a
    # model, and the box loads one at a time under jcode's swap lock.
    pooled = local_catalog.pool_of(served) is not None
    lock = getattr(state, "growbot_llm_lock" if pooled else "jcode_llm_swap_lock", None)
    factory = getattr(state, "growbot_llm_client_factory", None) or httpx.AsyncClient
    stream = bool(payload.get("stream"))

    async def relay() -> AsyncIterator[bytes]:
        nonlocal prompt_tokens
        # Made here, not before the response: a turn cancelled before its body starts never
        # runs this generator, and a client made outside it would never be closed.
        client = factory(base_url=gateway_url.rstrip("/"), timeout=_TIMEOUT)
        guard = lock if lock is not None else contextlib.nullcontext()
        # The headers are out by now, so every refusal is written into the body as an OpenAI
        # error: the app shows a cut stream as a broken brain and retries blind.
        try:
            async with guard:
                if residency is not None:
                    try:
                        admitted = await residency.ensure_room(served)
                    except (ResidencyError, gpu_guard.GpuBudgetError) as exc:
                        # A deliberate refusal ends the turn here, before the POST that would
                        # make the gateway load the model unadmitted.
                        log.warning("growbot-llm refused model=%s: %s", served, exc)
                        yield error_bytes(
                            _server_error(str(exc), "model_unavailable"), stream=stream
                        )
                        return
                    except Exception:  # noqa: BLE001 - housekeeping never fails a turn
                        log.warning(
                            "growbot-llm ensure_room failed model=%s", served, exc_info=True
                        )
                        admitted = None
                    if admitted and admitted != served:
                        payload["model"] = admitted
                        prompt_tokens = fit_openai_request(admitted, payload, GROWBOT_ROLE)
                model = str(payload["model"])
                # Judged on the model actually sent, after admission, as jcode's proxy does.
                _apply_level(payload, level, model)
                async with (
                    pinned_request(pool_guard, model, payload, prompt_tokens, GROWBOT_ROLE),
                    client.stream("POST", "/chat/completions", json=payload) as upstream,
                ):
                    status = getattr(upstream, "status_code", 200)
                    if status >= 400:
                        # The 200 is already out; say it in the body, which the sidecar and
                        # the app read as a failed turn rather than as the creature's words.
                        log.warning("growbot-llm gateway answered %s model=%s", status, model)
                        body = _server_error("the on-box model refused the turn", "gateway_error")
                        yield error_bytes(body, stream=stream)
                        return
                    async for chunk in upstream.aiter_raw():
                        yield chunk
        except SlotCapError as exc:
            yield error_bytes(context_length_exceeded(exc), stream=stream)
        except KvPoolBusyError as exc:
            yield error_bytes(pool_busy(exc), stream=stream)
        except httpx.HTTPError as exc:
            log.warning("growbot-llm gateway unreachable: %s", exc)
            body = _server_error("the on-box model did not answer", "gateway_unavailable")
            yield error_bytes(body, stream=stream)
        finally:
            await client.aclose()

    media = "text/event-stream" if stream else "application/json"
    return StreamingResponse(relay(), media_type=media, headers={"Cache-Control": "no-cache"})
