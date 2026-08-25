"""Moltbook account + operating-switch settings (docs/plans/JMOLT_PLAN.md, W1).

The PWA panel registers jmolt's Moltbook account, shows claim status, and operates the
two owner switches — the autonomy switch (queue vs auto-release, M7) and the global
kill/pause (M6) — plus the fixed disclosure header. Owner-only via the settings store's
RLS and the router's owner gate. The bearer KEY is a secret NEVER echoed back: GET
reports only whether one is set (stored or via the JBRAIN_MOLTBOOK_API_KEY env fallback).

Registration + rotation NEVER transit the agent loop: they are owner API routes here.
`register()` consumes the platform's key from the HTTP response and hands it straight to
the store; the response to the owner carries only the non-secret claim material (the
claim URL + verification code the owner needs to post the X verification tweet).
"""

from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from jbrain.api.deps import OwnerDep, PrincipalInfo, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.models.jmolt_outbox import OutboxRepo
from jbrain.settings_store import SqlSettingsStore
from jbrain.web.moltbook import MoltbookClient, MoltbookError

log = structlog.get_logger()

router = APIRouter()


def _client(request: Request) -> MoltbookClient:
    return cast(MoltbookClient, request.app.state.moltbook_client)


class MoltbookStatusOut(BaseModel):
    # The KEY is never returned — only whether one is effectively present (stored OR env
    # fallback). `handle` is jmolt's published name; `autonomy` is the queue/auto switch
    # (default off); `killed` is the global pause; `disclosure` is the fixed bio header.
    key_set: bool
    handle: str
    autonomy: bool
    killed: bool
    disclosure: str
    # The integrity watcher's last-observed account state (M21/M22): "ok" | "suspended" |
    # "moderated" | "tamper" — surfaced so the panel can show why writing paused. The
    # verify-failure streak (M11) rides along so the owner sees how close to the stop it is.
    account_state: str
    verify_fail_streak: int


class MoltbookRegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The agent name jmolt registers under (published forever) and its Moltbook bio/
    # description. Bounded so the fields can't carry an unbounded body outbound.
    name: str = Field(max_length=64)
    description: str = Field(default="", max_length=500)


class MoltbookRegisterOut(BaseModel):
    # Non-secret claim material only: the URL the owner opens to verify email + post the
    # X verification tweet, and the reference code. NO api_key field exists.
    claim_url: str
    verification_code: str
    handle: str


class MoltbookPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    autonomy: bool | None = None
    killed: bool | None = None
    disclosure: str | None = None
    # Clear the stored key (disconnect the account), reverting to the env fallback.
    clear_key: bool = False
    # Reset the verify-failure streak (M11) so writing resumes after the owner has looked.
    clear_streak: bool = False


class OutboxItemOut(BaseModel):
    id: str
    kind: str
    status: str
    publish_at: str | None
    payload: dict
    error: str | None


class OutboxActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str  # 'release' | 'discard'


class MoltbookClaimOut(BaseModel):
    status: str


async def _status(
    principal: PrincipalInfo, store: SqlSettingsStore, settings: Settings
) -> MoltbookStatusOut:
    ctx = ctx_for(principal)
    stored = await store.moltbook_api_key(ctx)
    return MoltbookStatusOut(
        key_set=bool(stored or settings.moltbook_api_key),
        handle=await store.moltbook_handle(ctx),
        autonomy=await store.moltbook_autonomy(ctx),
        killed=await store.moltbook_killed(ctx),
        disclosure=await store.moltbook_disclosure(ctx),
        account_state=await store.moltbook_account_state(ctx),
        verify_fail_streak=await store.moltbook_verify_fail_streak(ctx),
    )


@router.get("/settings/moltbook")
async def read_moltbook_settings(
    principal: OwnerDep, store: SettingsStoreDep, settings: SettingsDep
) -> MoltbookStatusOut:
    return await _status(principal, store, settings)


@router.put("/settings/moltbook")
async def update_moltbook_settings(
    body: MoltbookPatch,
    principal: OwnerDep,
    store: SettingsStoreDep,
    settings: SettingsDep,
) -> MoltbookStatusOut:
    ctx = ctx_for(principal)
    if body.autonomy is not None:
        await store.set_moltbook_autonomy(ctx, body.autonomy)
    if body.killed is not None:
        await store.set_moltbook_killed(ctx, body.killed)
    if body.disclosure is not None and body.disclosure.strip():
        await store.set_moltbook_disclosure(ctx, body.disclosure.strip())
    if body.clear_key:
        await store.set_moltbook_api_key(ctx, "")
        await store.set_moltbook_handle(ctx, "")
    if body.clear_streak:
        await store.set_moltbook_verify_fail_streak(ctx, 0)
    return await _status(principal, store, settings)


@router.get("/settings/moltbook/outbox")
async def list_outbox(request: Request, principal: OwnerDep) -> list[OutboxItemOut]:
    """The review queue: staged writes awaiting release (queued) plus released-but-unsent
    ones. The owner releases or discards each while the autonomy switch is off."""
    ctx = ctx_for(principal)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        rows = await OutboxRepo().list_by_status(s, ctx.principal_id, ("queued", "released"))
    return [
        OutboxItemOut(
            id=r.id,
            kind=r.kind,
            status=r.status,
            publish_at=r.publish_at.isoformat() if r.publish_at else None,
            payload=r.payload,
            error=r.error,
        )
        for r in rows
    ]


@router.post("/settings/moltbook/outbox/{row_id}")
async def act_on_outbox(
    row_id: str, body: OutboxActionIn, request: Request, principal: OwnerDep
) -> dict:
    ctx = ctx_for(principal)
    status = "released" if body.action == "release" else "discarded"
    if body.action not in ("release", "discard"):
        raise HTTPException(status_code=422, detail="action must be release or discard")
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        await OutboxRepo().set_status(s, row_id, status)
    return {"status": status}


@router.post("/settings/moltbook/register")
async def register_moltbook(
    body: MoltbookRegisterIn,
    request: Request,
    principal: OwnerDep,
    store: SettingsStoreDep,
) -> MoltbookRegisterOut:
    """Register a new Moltbook agent account (owner-only, never the agent loop). The
    platform returns the API key in the HTTP response; the secret_sink stores it and the
    response to the owner carries only the non-secret claim material (M17)."""
    ctx = ctx_for(principal)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="a handle is required to register")

    async def _sink(api_key: str) -> None:
        await store.set_moltbook_api_key(ctx, api_key)
        await store.set_moltbook_handle(ctx, name)

    try:
        result = await _client(request).register(name, body.description.strip(), secret_sink=_sink)
    except MoltbookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MoltbookRegisterOut(
        claim_url=result.claim_url,
        verification_code=result.verification_code,
        handle=result.handle,
    )


@router.get("/settings/moltbook/claim-status")
async def moltbook_claim_status(request: Request, principal: OwnerDep) -> MoltbookClaimOut:
    """The live claim status from Moltbook (pending_claim / claimed) so the owner can see,
    in the panel, when the X verification tweet has activated the account."""
    try:
        status = await _client(request).status()
    except MoltbookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MoltbookClaimOut(status=status)
