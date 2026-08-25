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

import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from jbrain.api.deps import OwnerDep, PrincipalInfo, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
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
    # The nightly-run schedule the owner controls: `night_enabled` toggles the run
    # independently of the global pause; `night_hour` is the owner-local hour (0–23) it fires.
    night_enabled: bool
    night_hour: int


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
    # The nightly-run schedule: toggle the run and set its owner-local hour (0–23).
    night_enabled: bool | None = None
    night_hour: int | None = Field(default=None, ge=0, le=23)


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
        night_enabled=await store.moltbook_night_enabled(ctx),
        night_hour=await store.moltbook_night_hour(ctx),
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
    if body.night_enabled is not None:
        await store.set_moltbook_night_enabled(ctx, body.night_enabled)
    if body.night_hour is not None:
        await store.set_moltbook_night_hour(ctx, body.night_hour)
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


# ── jmolt's history: nights, transcripts, the action ledger, and the scratchpad ──────
# Everything only the debug token could reach before, surfaced owner-only in the PWA so
# the owner can walk jmolt's history without a terminal. All reads are plain SELECTs under
# the owner context: `is_owner()` satisfies both the owner-only night tables
# (agent_sessions/agent_turns/runs) and the jmolt-scoped tables' SELECT policy
# (`has_domain_scope('jmolt')`), while there is no write surface here at all. jmolt-authored
# and third-party text (transcript bodies, scratch content, ledger targets) is returned
# verbatim and rendered INERT client-side (M15, `moltbookSafe.inertText`) — one hop from
# attacker-authorable Moltbook text, never trusted as markup.

_SCRATCH = JmoltScratchRepo()
_LEDGER = ActionLedgerRepo()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


class NightOut(BaseModel):
    session_id: str
    title: str
    at: str | None
    status: str | None
    stop_reason: str | None
    # A night is a sequence of sittings (docs/proposed/JMOLT_SITTINGS_PLAN.md), each its own
    # run under the one session; `steps`/`cost_tokens` are summed across them and `sittings`
    # counts them. `status` is "done" if any sitting completed, else "error"/None.
    steps: int | None
    cost_tokens: int | None
    sittings: int


class NightTurnOut(BaseModel):
    role: str
    content: str
    reasoning: str
    tools: Any
    at: str | None


class ScratchFileOut(BaseModel):
    filename: str
    bytes: int
    updated_at: str | None


class ScratchContentOut(BaseModel):
    filename: str
    content: str | None


class ScratchVersionOut(BaseModel):
    filename: str
    op: str
    bytes: int
    at: str | None
    content: str


class ActionOut(BaseModel):
    action: str
    target: str | None
    reacted_to: str | None
    at: str | None


@router.get("/settings/moltbook/nights")
async def list_nights(request: Request, principal: OwnerDep) -> list[NightOut]:
    """jmolt's nights, newest first — one row per nightly session with its run outcome
    (status, stop reason, step count, token cost). The date spine of the history browser."""
    ctx = ctx_for(principal)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT se.id, se.title, se.created_at,"
                    " count(r.id) AS sittings,"
                    " coalesce(sum(r.step_count), 0) AS steps,"
                    " coalesce(sum(r.cost_tokens), 0) AS cost_tokens,"
                    " bool_or(r.status = 'done') AS any_done,"
                    " bool_or(r.status = 'error') AS any_error"
                    " FROM app.agent_sessions se"
                    " LEFT JOIN app.runs r ON r.session_id = se.id AND r.kind = 'agent'"
                    " WHERE se.agent = 'jmolt'"
                    " GROUP BY se.id, se.title, se.created_at"
                    " ORDER BY se.created_at DESC LIMIT 90"
                )
            )
        ).all()
    return [
        NightOut(
            session_id=str(r.id),
            title=r.title or "",
            at=_iso(r.created_at),
            # "done" if any sitting finished; a night with a failed sitting but a good one is
            # still a productive night. stop_reason varies per sitting, so it's per-night null.
            status="done" if r.any_done else ("error" if r.any_error else None),
            stop_reason=None,
            steps=int(r.steps) if r.sittings else None,
            cost_tokens=int(r.cost_tokens) if r.sittings else None,
            sittings=int(r.sittings),
        )
        for r in rows
    ]


@router.get("/settings/moltbook/nights/{session_id}/transcript")
async def read_night_transcript(
    session_id: str, request: Request, principal: OwnerDep
) -> list[NightTurnOut]:
    """One night's turn-by-turn transcript — what jmolt thought (reasoning) and said
    (content) that hour. A non-uuid id resolves to an empty transcript rather than a 500
    from casting it to uuid in Postgres."""
    try:
        uuid.UUID(session_id)
    except ValueError:
        return []
    ctx = ctx_for(principal)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT t.role, t.content, t.reasoning, t.tools, t.created_at"
                    " FROM app.agent_turns t JOIN app.agent_sessions se"
                    " ON se.id = t.session_id WHERE t.session_id = :sid"
                    " AND se.agent = 'jmolt' ORDER BY t.seq ASC LIMIT 400"
                ),
                {"sid": session_id},
            )
        ).all()
    return [
        NightTurnOut(
            role=r.role,
            content=r.content or "",
            reasoning=r.reasoning or "",
            tools=r.tools,
            at=_iso(r.created_at),
        )
        for r in rows
    ]


@router.get("/settings/moltbook/actions")
async def list_actions(request: Request, principal: OwnerDep) -> list[ActionOut]:
    """jmolt's action ledger, newest first — every post/comment/vote/follow and each
    web fetch/search, with the content it reacted to. What jmolt actually did."""
    ctx = ctx_for(principal)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        acts = await _LEDGER.recent(s, ctx.principal_id, limit=200)
    return [
        ActionOut(action=a.action, target=a.target, reacted_to=a.reacted_to, at=_iso(a.at))
        for a in acts
    ]


@router.get("/settings/moltbook/files")
async def list_scratch_files(request: Request, principal: OwnerDep) -> list[ScratchFileOut]:
    """jmolt's current scratchpad — its notebook, the only continuity it carries between
    nights. Listed by filename with size + last-write time."""
    ctx = ctx_for(principal)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        files = await _SCRATCH.list_files(s, ctx.principal_id)
    return [
        ScratchFileOut(filename=f.filename, bytes=f.bytes, updated_at=_iso(f.updated_at))
        for f in files
    ]


@router.get("/settings/moltbook/files/content")
async def read_scratch_file(
    request: Request, principal: OwnerDep, filename: str
) -> ScratchContentOut:
    """The current contents of one scratchpad file (rendered inert client-side, M15)."""
    ctx = ctx_for(principal)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        content = await _SCRATCH.read(s, ctx.principal_id, filename.strip())
    return ScratchContentOut(filename=filename, content=content)


@router.get("/settings/moltbook/files/history")
async def list_scratch_history(
    request: Request, principal: OwnerDep, filename: str | None = None
) -> list[ScratchVersionOut]:
    """The append-only scratchpad archive, newest first — every prior version of a file
    (or of all files) so the owner can walk the notebook back through its history."""
    ctx = ctx_for(principal)
    fn = filename.strip() if filename and filename.strip() else None
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        hist = await _SCRATCH.history(s, ctx.principal_id, fn)
    return [
        ScratchVersionOut(
            filename=h.filename, op=h.op, bytes=h.bytes, at=_iso(h.archived_at), content=h.content
        )
        for h in hist
    ]
