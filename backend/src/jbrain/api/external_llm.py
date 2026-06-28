"""External LLM sessions: a token-gated public proxy to the on-box coder.

The owner mints an external session (owner-gated CRUD here) and hands its URL +
secret to a remote coder. Two wire protocols are served off the same endpoint:
a remote Claude points ANTHROPIC_BASE_URL at ``/api/ext/llm/<id>`` (the SDK then
hits ``/v1/messages``); an OpenAI-compatible client (grok-cli, etc.) points
OPENAI_BASE_URL at ``/api/ext/llm/<id>/v1`` (the client then hits
``/v1/chat/completions``). Both set their auth token to the secret. The public
proxy (no owner gate — the bearer IS the credential) authenticates the secret,
refuses when the session is toggled off or the coder isn't resident on the box,
pins every request to the on-box coder, forwards to the LiteLLM shim (which
serves both ``/v1/messages`` and ``/v1/chat/completions``), and meters the token
usage back onto the session so the owner's screen shows consumption.

The credential reuses the capability-token machinery (a `principals` row, kind
``external_llm``; the on/off toggle is the suspend flag). The model is the
configured coder (settings.jcode_model) — pinned, never the caller's choice.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.auth import service
from jbrain.llm import local_catalog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

router = APIRouter()

# Principal ids are UUIDs; validate the shape so a caller-supplied id can't carry a
# `/` or `..` anywhere it's interpolated.
_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


class MintRequest(BaseModel):
    label: str = Field(default="external session", min_length=1, max_length=128)
    # Optional time-box (hours). None / 0 = no expiry — an external endpoint is long-lived
    # and controlled via the on/off toggle and revoke instead.
    ttl_hours: float | None = Field(default=None, ge=0.25, le=24 * 365)


class MintOut(BaseModel):
    id: str
    label: str
    expires_at: str | None
    # The bearer secret + the base URL the remote points ANTHROPIC_BASE_URL at. Shown
    # EXACTLY once — never recoverable from the list.
    token: str
    url: str


class ExternalOut(BaseModel):
    id: str
    label: str
    enabled: bool
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    in_tokens: int
    out_tokens: int
    requests: int


class EnabledRequest(BaseModel):
    enabled: bool


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _public_base(request: Request, settings: object) -> str:
    """The internet-reachable base the remote points at: the configured public base URL,
    else the origin this request arrived on (LAN/dev)."""
    configured = getattr(settings, "public_base_url", "") or ""
    return configured.rstrip("/") or str(request.base_url).rstrip("/")


@router.post("/jcode/external", status_code=201)
async def mint_external(
    body: MintRequest, request: Request, _owner: OwnerDep, repo: AuthRepoDep, settings: SettingsDep
) -> MintOut:
    """Mint an external-LLM session (owner only). Returns the secret + endpoint URL once."""
    key, record = await service.mint_external_llm(repo, body.label, body.ttl_hours)
    return MintOut(
        id=record.id,
        label=record.label,
        expires_at=_iso(record.expires_at),
        token=key,
        url=f"{_public_base(request, settings)}/api/ext/llm/{record.id}",
    )


@router.get("/jcode/external")
async def list_external(_owner: OwnerDep, repo: AuthRepoDep) -> list[ExternalOut]:
    """The live (non-revoked) external sessions with cumulative usage (owner only)."""
    return [
        ExternalOut(
            id=s.id,
            label=s.label,
            enabled=s.enabled,
            created_at=_iso(s.created_at) or "",
            expires_at=_iso(s.expires_at),
            last_used_at=_iso(s.last_used_at),
            in_tokens=s.in_tokens,
            out_tokens=s.out_tokens,
            requests=s.requests,
        )
        for s in await repo.list_external_llm()
    ]


@router.post("/jcode/external/{sid}/enabled")
async def set_enabled(
    sid: str, body: EnabledRequest, _owner: OwnerDep, repo: AuthRepoDep
) -> dict[str, bool]:
    """Flip the on/off toggle (owner only). 404 on an unknown / revoked id."""
    if not _ID_RE.match(sid) or not await repo.set_external_llm_enabled(sid, body.enabled):
        raise HTTPException(status_code=404, detail="unknown external session")
    return {"enabled": body.enabled}


@router.delete("/jcode/external/{sid}", status_code=204)
async def revoke_external(sid: str, _owner: OwnerDep, repo: AuthRepoDep) -> None:
    """Revoke (delete) an external session (owner only). 404 on unknown / already-gone."""
    if not _ID_RE.match(sid) or not await repo.revoke_external_llm(sid):
        raise HTTPException(status_code=404, detail="unknown external session")


# --- The public, token-gated proxy (NO owner gate — the bearer is the credential) ---


def _served_model(model_id: str) -> str:
    """The gateway's served-model name for a catalog id (they match for the coder, but
    resolve via the catalog to be correct)."""
    m = local_catalog.get(model_id)
    return m.served_model if m else model_id


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    # Claude Code sends a Bearer token (ANTHROPIC_AUTH_TOKEN); also accept x-api-key.
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _tokens(usage: dict) -> tuple[int, int]:
    """(input, output) from a usage object in either dialect: Anthropic names them
    ``input_tokens``/``output_tokens``, OpenAI ``prompt_tokens``/``completion_tokens``."""
    in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return in_tok, out_tok


def _usage_from_chunks(chunks: list[bytes]) -> tuple[int, int]:
    """Best-effort (input, output) token counts from a buffered upstream response —
    Anthropic or OpenAI, streaming (SSE) or whole JSON. Returns (0, 0) when usage can't
    be parsed; metering must never break the proxy."""
    in_tok = out_tok = 0
    try:
        body = b"".join(chunks)
        # Non-streaming: a single JSON object with a top-level `usage`.
        try:
            obj = json.loads(body)
            usage = obj.get("usage") if isinstance(obj, dict) else None
            if isinstance(usage, dict):
                return _tokens(usage)
        except (json.JSONDecodeError, ValueError):
            pass
        # Streaming: scan every `data:` line for a usage object. Anthropic carries input
        # on message_start and a running output on message_delta; OpenAI carries the full
        # usage on the final chunk (with stream_options.include_usage). Take the max
        # across all of them, in either dialect.
        for raw in body.split(b"\n"):
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(evt, dict):
                continue
            msg = evt.get("message")
            for usage in (evt.get("usage"), msg.get("usage") if isinstance(msg, dict) else None):
                if isinstance(usage, dict):
                    i, o = _tokens(usage)
                    in_tok, out_tok = max(in_tok, i), max(out_tok, o)
    except Exception:  # noqa: BLE001 - metering is best-effort, never fatal
        log.debug("external-llm usage parse failed", exc_info=True)
    return in_tok, out_tok


async def _proxy(request: Request, sid: str, upstream_path: str, *, meter: bool) -> Response:
    settings = request.app.state.settings
    repo = request.app.state.auth_repo
    if not _ID_RE.match(sid):
        raise HTTPException(status_code=404, detail="unknown external session")
    # 1. Authenticate the bearer secret. None covers unknown / revoked / OFF / expired.
    principal = await service.authenticate_external_llm(repo, _bearer(request))
    if principal is None or principal.id != sid:
        raise HTTPException(status_code=401, detail="invalid or disabled external session")

    # 2. The coder must be resident — never trigger an on-demand load for a remote caller.
    served = _served_model(getattr(settings, "jcode_model", ""))
    gateway = getattr(request.app.state, "local_gateway", None)
    shim_url = getattr(settings, "jcode_shim_url", "")
    shim_key = getattr(settings, "jcode_gateway_token", "")
    if gateway is None or not shim_url or not shim_key:
        raise HTTPException(status_code=503, detail="on-box LLM is not configured")
    try:
        resident = await gateway.running()
    except Exception:  # noqa: BLE001 - a gateway hiccup reads as "not loaded"
        resident = set()
    if served not in resident:
        raise HTTPException(status_code=503, detail="the coder model is not loaded")

    # 3. Pin the model and forward to the shim, streaming the response back verbatim while
    #    teeing it for usage metering.
    try:
        payload = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    payload["model"] = served  # pin to the on-box coder; ignore the caller's choice
    headers = {"Authorization": f"Bearer {shim_key}", "Content-Type": "application/json"}
    client = httpx.AsyncClient(base_url=shim_url.rstrip("/"), timeout=httpx.Timeout(600.0))

    captured: list[bytes] = []

    async def relay() -> AsyncIterator[bytes]:
        try:
            async with client.stream(
                "POST", upstream_path, json=payload, headers=headers
            ) as upstream:
                async for chunk in upstream.aiter_raw():
                    if meter:
                        captured.append(chunk)
                    yield chunk
        finally:
            await client.aclose()
            if meter:
                in_tok, out_tok = _usage_from_chunks(captured)
                if in_tok or out_tok:
                    try:
                        await service.record_external_usage(repo, sid, in_tok, out_tok)
                    except Exception:  # noqa: BLE001 - metering must not fail the call
                        log.warning("external-llm usage record failed sid=%s", sid, exc_info=True)

    media = "text/event-stream" if payload.get("stream") else "application/json"
    return StreamingResponse(relay(), media_type=media)


@router.post("/ext/llm/{sid}/v1/messages")
async def proxy_messages(sid: str, request: Request) -> Response:
    """Proxy an Anthropic Messages call to the on-box coder (token-gated, metered)."""
    return await _proxy(request, sid, "/v1/messages", meter=True)


@router.post("/ext/llm/{sid}/v1/messages/count_tokens")
async def proxy_count_tokens(sid: str, request: Request) -> Response:
    """Proxy a token-count call (token-gated, not metered — it runs no completion)."""
    return await _proxy(request, sid, "/v1/messages/count_tokens", meter=False)


@router.post("/ext/llm/{sid}/v1/chat/completions")
async def proxy_chat_completions(sid: str, request: Request) -> Response:
    """Proxy an OpenAI Chat Completions call to the on-box coder (token-gated, metered).

    Lets an OpenAI-compatible client (grok-cli, etc.) target the coder the same way a
    remote Claude targets ``/v1/messages`` — same auth, same model pin, same metering."""
    return await _proxy(request, sid, "/v1/chat/completions", meter=True)


@router.get("/ext/llm/{sid}/v1/models")
async def proxy_models(sid: str, request: Request) -> dict[str, object]:
    """List the served model (token-gated, not metered). OpenAI clients hit this to
    discover/validate the model before a completion; we advertise only the pinned on-box
    coder, since that's all the proxy will ever run."""
    settings = request.app.state.settings
    repo = request.app.state.auth_repo
    if not _ID_RE.match(sid):
        raise HTTPException(status_code=404, detail="unknown external session")
    principal = await service.authenticate_external_llm(repo, _bearer(request))
    if principal is None or principal.id != sid:
        raise HTTPException(status_code=401, detail="invalid or disabled external session")
    served = _served_model(getattr(settings, "jcode_model", ""))
    return {
        "object": "list",
        "data": [{"id": served, "object": "model", "created": 0, "owned_by": "jbrain"}],
    }
