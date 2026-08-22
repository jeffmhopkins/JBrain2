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

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from jbrain.ingest.imageprep import pdf_page_images
from jbrain.llm import gpu_guard, local_catalog
from jbrain.llm.residency import ResidencyError
from jbrain.vision import OcrServiceError
from jbrain.web.fetch import JS_SHELL_MESSAGE, JS_SHELL_NOTE, WebFetchError
from jbrain.web.search import WebSearchError

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
    "nemotron-3.5-lightning-30b": "nemotron-lightning",
    "qwen3.8-27b": "qwen38",
    "qwen3.8-27b-q4": "qwen38-q4",
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
                # Best-effort — a residency hiccup degrades to the gateway's own load. But a
                # deliberate over-box refusal (the model can't fit RAM) propagates: better to
                # fail this swap than crash the box loading a model that can't fit.
                if residency is not None:
                    try:
                        await residency.ensure_room(served)
                    except (ResidencyError, gpu_guard.GpuBudgetError):
                        # GpuBudgetError joins ResidencyError here, and the omission was the
                        # worst kind: it is not a subclass, so it fell to the blanket arm,
                        # was logged as a "housekeeping" hiccup, and execution CONTINUED to
                        # the POST below — which makes llama-swap load the model on demand,
                        # with no admission, no device pre-flight and no watchdog. A DEVICE
                        # REFUSAL was converted into precisely the unguarded load the guard
                        # exists to prevent, on the box whose failure mode is a power cycle.
                        raise
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


# --- SearXNG search bridge (docs/plans/JCODE_GROK_INTERNET_PLAN.md) -----------------------
#
# The sandbox's grok/claude reach the box's SearXNG through here: the sandbox sits on the
# `jcode` network and can't touch searxng (on `internal`), but this api is the one peer on
# both, so it bridges. Shell helpers on PATH (web-search / web-fetch) POST to these routes
# with the same shared bearer the model calls already use. Only the model-supplied query /
# URL leaves — through the owner's own searxng — the same no-owner-data posture as jerv's
# web tools. Responses are pre-rendered text so the helper just prints them to the shell.

_SEARCH_MAX = 10
_SEARCH_DEFAULT = 6


def _json_body(raw: bytes) -> dict:
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return payload


@router.post("/jcode/llm/v1/web_search")
async def web_search(request: Request) -> Response:
    """SearXNG-backed web search for the in-sandbox CLIs. 503 when searxng isn't configured,
    400 for an empty query, 502 when the search service is unreachable."""
    _authorize(request)
    client = getattr(request.app.state, "searxng", None)
    if client is None or not (getattr(request.app.state.settings, "searxng_url", "") or ""):
        raise HTTPException(status_code=503, detail="web search is not configured")
    payload = _json_body(await request.body())
    query = str(payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    try:
        limit = int(payload.get("limit") or _SEARCH_DEFAULT)
    except (TypeError, ValueError):
        limit = _SEARCH_DEFAULT
    limit = max(1, min(limit, _SEARCH_MAX))
    try:
        hits = (await client.search(query, limit)).hits
    except WebSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not hits:
        return Response(content=f"No web results for '{query}'.\n", media_type="text/plain")
    body = "Web results:\n" + "\n".join(f"- {h.title}\n  {h.url}\n  {h.snippet}" for h in hits)
    return Response(content=body + "\n", media_type="text/plain")


@router.post("/jcode/llm/v1/web_fetch")
async def web_fetch(request: Request) -> Response:
    """Fetch-and-extract a URL for the in-sandbox CLIs, mirroring jerv's web_fetch. 503 when
    the fetcher isn't configured, 400 for a missing url, 502 when the fetch fails."""
    _authorize(request)
    fetcher = getattr(request.app.state, "web_fetcher", None)
    if fetcher is None:
        raise HTTPException(status_code=503, detail="web fetch is not configured")
    payload = _json_body(await request.body())
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        result = await fetcher.fetch(url)
    except WebFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not result.text:
        # An un-rendered JS app is not an empty page — the in-sandbox CLI gets the same
        # diagnosis jerv's web_fetch does, or it will burn its turns re-fetching the URL.
        empty = (
            JS_SHELL_MESSAGE.format(url=url)
            if result.js_shell
            else (f"That page ({url}) had no readable text.")
        )
        return Response(content=f"{empty}\n", media_type="text/plain")
    header = f"# {result.title}\n{result.url}\n\n" if result.title else f"{result.url}\n\n"
    body = header + result.text
    if result.js_shell:
        body += f"\n\n{JS_SHELL_NOTE}"
    if result.links:
        links = "\n".join(f"- {u}" for u in result.links)
        body += f"\n\nLinks on this page (web-fetch any of these to follow it):\n{links}"
    return Response(content=body + "\n", media_type="text/plain")


async def _ocr_pdf_bytes(client: object, data: bytes) -> str:
    """OCR a PDF page by page (rasterize in the api, one image per page to the sidecar)."""
    images = await asyncio.to_thread(pdf_page_images, data)
    parts: list[str] = []
    for number, png in enumerate(images, start=1):
        page = (await client.ocr(png, "image/png")).text.strip()  # type: ignore[attr-defined]
        if page:
            parts.append(f"[page {number}]\n{page}")
    return "\n\n".join(parts)


@router.post("/jcode/llm/v1/ocr")
async def ocr(request: Request) -> Response:
    """Deterministic OCR for the sandbox CLIs via the on-box RapidOCR sidecar — the sandbox
    can't reach it directly (it's on `internal`), so this api bridges. Raw image/PDF bytes in
    the body; a PDF is rasterized and read page by page. Unlike web-search this has no
    per-session gate — it's an offline, local read (docs/plans/RAPIDOCR_PLAN.md)."""
    _authorize(request)
    client = getattr(request.app.state, "rapidocr", None)
    if client is None:
        raise HTTPException(status_code=503, detail="OCR is not configured")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    content_type = request.headers.get("content-type", "")
    is_pdf = content_type.startswith("application/pdf") or data[:5] == b"%PDF-"
    try:
        text = await _ocr_pdf_bytes(client, data) if is_pdf else (await client.ocr(data)).text
    except OcrServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    text = text.strip()
    return Response(
        content=(text + "\n") if text else "No legible text was found.\n",
        media_type="text/plain",
    )
