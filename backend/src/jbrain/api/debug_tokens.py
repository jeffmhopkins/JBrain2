"""Owner-gated lifecycle for debug-console capability tokens.

The owner mints a time-boxed, revocable token here (and copies its self-contained
`payload` to hand to an external assistant); the token then authenticates the
`/api/debug/*` surface (api/debug.py). Minting is refused when the feature flag is
off, but listing and revoking always work so a token can be cleaned up even after
the surface is turned back off. The secret is shown exactly once, at mint.
"""

import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.auth import service
from jbrain.config import Settings

router = APIRouter(prefix="/settings/debug-tokens")

# Bump if the payload shape changes so an older reader can reject a newer payload.
DEBUG_PAYLOAD_VERSION = 1

_MIN_TTL_HOURS, _MAX_TTL_HOURS = 0.25, 24.0 * 30


def build_debug_payload(server_base: str, key: str) -> str:
    """A single opaque string the owner copies and hands to the assistant: it
    embeds the server URL alongside the bearer key, so the assistant learns both
    where to connect and how to authenticate from the one token. base64url(JSON),
    mirroring the OwnTracks pairing payload (jbrain.locations.pairing)."""
    raw = json.dumps(
        {"v": DEBUG_PAYLOAD_VERSION, "u": server_base.rstrip("/"), "k": key},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _public_base(request: Request, settings: Settings) -> str:
    """The base URL to embed in the payload, for the EXTERNAL clients that use it
    (an assistant over the internet) — the LAN-only console ignores it and calls
    same-origin. Prefer the explicit public host so a token minted from the LAN PWA
    still points off-box clients at the public tunnel; then dashboard_url; else fall
    back to the origin the owner's browser actually hit."""
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    if settings.dashboard_url:
        return settings.dashboard_url.rstrip("/")
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    host = request.headers.get("host", request.url.netloc)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{host}".rstrip("/")


class MintRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    ttl_hours: float = Field(default=24.0, ge=_MIN_TTL_HOURS, le=_MAX_TTL_HOURS)


class MintOut(BaseModel):
    id: str
    label: str
    expires_at: datetime | None
    # The self-contained token to hand to the assistant (server URL + key). Shown
    # ONCE — it is never recoverable from the management list.
    payload: str


class TokenOut(BaseModel):
    id: str
    label: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    suspended_at: datetime | None


@router.post("", status_code=201)
async def mint_token(
    body: MintRequest,
    request: Request,
    _owner: OwnerDep,
    repo: AuthRepoDep,
    settings: SettingsDep,
) -> MintOut:
    if not settings.debug_access_enabled:
        raise HTTPException(status_code=409, detail="debug access is not enabled")
    key, record = await service.mint_capability(repo, body.label, body.ttl_hours)
    payload = build_debug_payload(_public_base(request, settings), key)
    return MintOut(id=record.id, label=record.label, expires_at=record.expires_at, payload=payload)


@router.get("")
async def list_tokens(_owner: OwnerDep, repo: AuthRepoDep) -> list[TokenOut]:
    return [
        TokenOut(
            id=t.id,
            label=t.label,
            created_at=t.created_at,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            revoked_at=t.revoked_at,
            suspended_at=t.suspended_at,
        )
        for t in await repo.list_capabilities()
    ]


@router.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: Annotated[str, ...], _owner: OwnerDep, repo: AuthRepoDep) -> None:
    if not await repo.revoke_capability(token_id):
        raise HTTPException(status_code=404, detail="unknown or already-revoked token")


@router.post("/{token_id}/suspend", status_code=204)
async def suspend_token(token_id: Annotated[str, ...], _owner: OwnerDep, repo: AuthRepoDep) -> None:
    """Pause a live token: it stops authenticating until the owner resumes it."""
    if not await repo.suspend_capability(token_id):
        raise HTTPException(status_code=404, detail="unknown, revoked, or already-suspended token")


@router.post("/{token_id}/resume", status_code=204)
async def resume_token(token_id: Annotated[str, ...], _owner: OwnerDep, repo: AuthRepoDep) -> None:
    """Wake a suspended token so it authenticates again (owner-only — a suspended
    token cannot reach the debug surface to un-suspend itself)."""
    if not await repo.resume_capability(token_id):
        raise HTTPException(status_code=404, detail="unknown, revoked, or not-suspended token")
