"""The GrowBot creature's model lane: an OpenAI chat proxy to the box's own engine.

docs/plans/GROWBOT_PLAN.md §2. The sidecar's `/api/llm[-stream]?target=openrouter` shim lands
here, so the stock app's think lane (text, and camera frames as `image_url` parts) runs on the
box and nothing leaves the house. The `jcode_llm.py` shape, not its routes or token: residency
makes room for the model before the gateway would load it on demand (the sidecar never talks
to llama-swap itself), the request is held to the GrowBot slot's cap on a pooled engine and
pinned there, and a per-box lock serializes the creature's turns so two phones never each
evict the other's model.

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
# booking a coder's budget against the pool.
DEFAULT_MAX_TOKENS: Final = 1024
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
    return None


async def _engine(request: Request) -> engines.Engine:
    active = getattr(request.app.state, "active_engine", None)
    return await active.get() if active is not None else engines.DEFAULT_ENGINE


async def _models(request: Request) -> tuple[local_catalog.LocalModel, ...]:
    settings = request.app.state.settings
    return local_catalog.growbot_models(
        getattr(settings, "local_llm_enabled", False),
        getattr(settings, "local_models", []),
        await _engine(request),
    )


async def _served(
    request: Request, requested: str, models: tuple[local_catalog.LocalModel, ...]
) -> str | None:
    """The served name a request runs on: its own when installed on the active engine, a
    catalog id's served name, or the active engine's sole model (Flash-Next serves every local
    call). None for anything else — an unknown name must never drive an eviction."""
    by_name = {m.served_model: m.served_model for m in models} | {
        m.id: m.served_model for m in models
    }
    if requested in by_name:
        return by_name[requested]
    known = local_catalog.get_by_served(requested) or local_catalog.get(requested)
    if known is None:
        return None
    mapped = local_catalog.remap_for_engine(known.served_model, await _engine(request))
    return mapped if mapped in by_name.values() else None


def _client_level(payload: dict[str, Any]) -> str:
    """The reasoning level the app asked for (OpenAI's `reasoning_effort`, or OpenRouter's
    `reasoning.effort`), else thinking off."""
    asked = payload.get("reasoning_effort")
    reasoning = payload.get("reasoning")
    if not isinstance(asked, str) and isinstance(reasoning, dict):
        asked = reasoning.get("effort")
    return asked if isinstance(asked, str) and asked in _LEVELS else _DEFAULT_LEVEL


def _apply_level(payload: dict[str, Any], served: str) -> None:
    """Put the level on the wire the way the adapter encodes it, after clearing every field the
    client may have set: OpenRouter's `reasoning` object means nothing to llama-server."""
    level = _client_level(payload)
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
    """The installed models of the serving engine, each with whether it can see images."""
    data = [
        {
            "id": m.served_model,
            "object": "model",
            "created": 0,
            "owned_by": "jbrain",
            "label": m.label,
            "vision": m.supports_vision,
        }
        for m in await _models(request)
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

    models = await _models(request)
    requested = str(payload.get("model") or "")
    served = await _served(request, requested, models)
    if served is None:
        return _openai_error(400, f"unknown or unavailable model: {requested!r}", "model_not_found")
    unsafe = _unsafe_part(payload["messages"])
    if unsafe is not None:
        return _openai_error(400, unsafe, "invalid_content")
    _chars, images = prompt_chars(payload)
    if images and not local_catalog.supports_vision(served):
        return _openai_error(400, f"{served} cannot see images", "vision_not_supported")
    payload["model"] = served
    _apply_level(payload, served)
    if "max_tokens" not in payload and "max_completion_tokens" not in payload:
        payload["max_tokens"] = DEFAULT_MAX_TOKENS
    # Before the lock: a request too long for its slot must not wait a turn to be refused.
    try:
        prompt_tokens = fit_openai_request(served, payload, GROWBOT_ROLE)
    except SlotCapError as exc:
        return JSONResponse(status_code=400, content=context_length_exceeded(exc))

    state = request.app.state
    residency = getattr(state, "residency", None)
    pool_guard = getattr(state, "kv_pool_guard", None)
    lock = getattr(state, "growbot_llm_lock", None)
    factory = getattr(state, "growbot_llm_client_factory", None) or httpx.AsyncClient
    client = factory(base_url=gateway_url.rstrip("/"), timeout=_TIMEOUT)
    stream = bool(payload.get("stream"))

    async def relay() -> AsyncIterator[bytes]:
        nonlocal prompt_tokens
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
                async with (
                    pinned_request(pool_guard, model, payload, prompt_tokens, GROWBOT_ROLE),
                    client.stream("POST", "/chat/completions", json=payload) as upstream,
                ):
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
