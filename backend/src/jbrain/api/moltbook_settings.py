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
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from jbrain.agent.jmolt_owner import jmolt_owner_principal_id
from jbrain.api.deps import OwnerDep, PrincipalInfo, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import JmoltJournalRepo, JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
from jbrain.settings_store import SqlSettingsStore
from jbrain.web.moltbook import MoltbookClient, MoltbookError

log = structlog.get_logger()

router = APIRouter()


def _client(request: Request) -> MoltbookClient:
    return cast(MoltbookClient, request.app.state.moltbook_client)


async def _jmolt_pid(request: Request, ctx: SessionContext) -> str:
    """The principal jmolt's data is filed under (jmolt_owner.py — the stable oldest owner),
    NOT the authenticated owner in `ctx`. After a key rotation the two diverge, and jmolt's
    scratchpad/journal/outbox/ledger live under the anchor; reading by the authenticated
    owner (as these endpoints used to) shows an empty history. Falls back to the caller's
    own id on a box with no resolvable owner. The scoped_session still runs under `ctx`, so
    `is_owner()` grants the SELECT; only the principal-id FILTER changes."""
    return await jmolt_owner_principal_id(request.app.state.session_maker) or ctx.principal_id


class MoltbookStatusOut(BaseModel):
    # The KEY is never returned — only whether one is effectively present (stored OR env
    # fallback). `handle` is jmolt's published name; `autonomy` is the queue/auto switch
    # (default off); `killed` is the global pause; `disclosure` is the fixed bio header.
    key_set: bool
    handle: str
    autonomy: bool
    killed: bool
    disclosure: str
    # The owner's advisory note TO jmolt — free text the human edits, injected (fenced, as
    # trusted-owner DATA) into the first sitting of the next night. Advisory, not command.
    advisory_note: str
    # The integrity watcher's last-observed account state (M21/M22): "ok" | "suspended" |
    # "moderated" | "tamper" — surfaced so the panel can show why writing paused. The
    # verify-failure streak (M11) rides along so the owner sees how close to the stop it is.
    account_state: str
    verify_fail_streak: int
    # The nightly-run schedule the owner controls: `night_enabled` toggles the run
    # independently of the global pause; `night_hour` is the owner-local hour (0–23) it fires.
    night_enabled: bool
    night_hour: int
    # Computed schedule/drip status for the PWA's "when things happen" panel — all derived
    # from stored state, no new persistence beyond the drip heartbeat:
    #   night_next_run     — ISO of the next scheduled run (null when the run is disabled),
    #   night_last_run     — owner-local date (YYYY-MM-DD) of the most recent run, or null,
    #   night_running_until— ISO end-time while a night is running right now, else null,
    #   drip_last_swept    — ISO of the drip sweep's most recent tick, or null.
    night_next_run: str | None
    night_last_run: str | None
    night_running_until: str | None
    drip_last_swept: str | None


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
    # The owner's advisory note to jmolt. Unlike `disclosure`, a blank string is meaningful
    # (it CLEARS the note), so it is bounded but not strip-guarded on write.
    advisory_note: str | None = Field(default=None, max_length=8192)
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


def _next_night_run(tz_name: str, night_hour: int, last_night: str, now: datetime) -> str:
    """ISO of the next scheduled nightly run in the owner's timezone: the next `night_hour:00`
    that is still in the future and hasn't already run for that date. Pure derived data."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    candidate = local.replace(hour=night_hour, minute=0, second=0, microsecond=0)
    # Already past today's hour, or today's run already happened → the run is tomorrow.
    if candidate <= local or candidate.date().isoformat() == last_night:
        candidate += timedelta(days=1)
    return candidate.isoformat()


async def _status(
    principal: PrincipalInfo, store: SqlSettingsStore, settings: Settings
) -> MoltbookStatusOut:
    ctx = ctx_for(principal)
    stored = await store.moltbook_api_key(ctx)
    night_enabled = await store.moltbook_night_enabled(ctx)
    night_hour = await store.moltbook_night_hour(ctx)
    last_night = await store.moltbook_last_night(ctx)
    running_until = await store.moltbook_night_deadline(ctx)
    drip = await store.moltbook_drip_last_swept(ctx)
    tz_name = await store.owner_timezone(ctx) or "UTC"
    return MoltbookStatusOut(
        key_set=bool(stored or settings.moltbook_api_key),
        handle=await store.moltbook_handle(ctx),
        autonomy=await store.moltbook_autonomy(ctx),
        killed=await store.moltbook_killed(ctx),
        disclosure=await store.moltbook_disclosure(ctx),
        advisory_note=await store.moltbook_advisory_note(ctx),
        account_state=await store.moltbook_account_state(ctx),
        verify_fail_streak=await store.moltbook_verify_fail_streak(ctx),
        night_enabled=night_enabled,
        night_hour=night_hour,
        night_next_run=(
            _next_night_run(tz_name, night_hour, last_night, datetime.now(ZoneInfo("UTC")))
            if night_enabled
            else None
        ),
        night_last_run=last_night or None,
        night_running_until=running_until or None,
        drip_last_swept=drip or None,
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
    if body.advisory_note is not None:
        # A blank note is a real value here (the owner clearing it), so no strip-guard.
        await store.set_moltbook_advisory_note(ctx, body.advisory_note)
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
    pid = await _jmolt_pid(request, ctx)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued", "released"))
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
_JOURNAL = JmoltJournalRepo()


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


class JournalEntryOut(BaseModel):
    content: str
    at: str | None


class ActionOut(BaseModel):
    action: str
    target: str | None
    reacted_to: str | None
    at: str | None
    # The ledger seq — pass the last row's value back as `cursor` to page older.
    seq: int


class ActionStatOut(BaseModel):
    family: str  # "stage" | "publish"
    kind: str  # comment | vote | post | follow | …
    count: int


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
async def list_actions(
    request: Request,
    principal: OwnerDep,
    family: str | None = Query(None, description="'stage' (drafted) or 'publish' (sent)"),
    kinds: str | None = Query(None, description="comma-separated action kinds to keep"),
    since_days: int | None = Query(None, ge=1, le=365),
    cursor: int | None = Query(None, ge=1, description="page older: pass the last row's seq"),
    limit: int = Query(100, ge=1, le=500),
) -> list[ActionOut]:
    """jmolt's action ledger, newest first — every post/comment/vote/follow, with the content
    it reacted to. Server-side filtering keeps a busy night legible: `family` splits drafted
    from sent, `kinds` narrows to chosen kinds, `since_days` bounds the window, `cursor` pages."""
    ctx = ctx_for(principal)
    pid = await _jmolt_pid(request, ctx)
    kind_list = tuple(k.strip() for k in (kinds or "").split(",") if k.strip()) or None
    fam = family if family in ("stage", "publish") else None
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        acts = await _LEDGER.list_filtered(
            s, pid, family=fam, kinds=kind_list, since_days=since_days, cursor=cursor, limit=limit
        )
    return [
        ActionOut(
            action=a.action, target=a.target, reacted_to=a.reacted_to, at=_iso(a.at), seq=a.seq
        )
        for a in acts
    ]


@router.get("/settings/moltbook/actions/stats")
async def list_action_stats(
    request: Request,
    principal: OwnerDep,
    since_days: int | None = Query(None, ge=1, le=365),
) -> list[ActionStatOut]:
    """Per-(family, kind) counts over the same optional window — so the filter chips show
    honest totals no matter what the filtered list is currently showing."""
    ctx = ctx_for(principal)
    pid = await _jmolt_pid(request, ctx)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        rows = await _LEDGER.stats(s, pid, since_days=since_days)
    return [ActionStatOut(family=f, kind=k, count=n) for (f, k, n) in rows]


@router.get("/settings/moltbook/journal")
async def list_journal(request: Request, principal: OwnerDep) -> list[JournalEntryOut]:
    """jmolt's journal, newest first — its own line to its human, night by night. Rendered
    inert client-side (M15), the same as everything jmolt authors."""
    ctx = ctx_for(principal)
    pid = await _jmolt_pid(request, ctx)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        entries = await _JOURNAL.recent(s, pid, limit=60)
    return [JournalEntryOut(content=e.content, at=_iso(e.created_at)) for e in entries]


@router.get("/settings/moltbook/files")
async def list_scratch_files(request: Request, principal: OwnerDep) -> list[ScratchFileOut]:
    """jmolt's current scratchpad — its notebook, the only continuity it carries between
    nights. Listed by filename with size + last-write time."""
    ctx = ctx_for(principal)
    pid = await _jmolt_pid(request, ctx)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        files = await _SCRATCH.list_files(s, pid)
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
    pid = await _jmolt_pid(request, ctx)
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        content = await _SCRATCH.read(s, pid, filename.strip())
    return ScratchContentOut(filename=filename, content=content)


@router.get("/settings/moltbook/files/history")
async def list_scratch_history(
    request: Request, principal: OwnerDep, filename: str | None = None
) -> list[ScratchVersionOut]:
    """The append-only scratchpad archive, newest first — every prior version of a file
    (or of all files) so the owner can walk the notebook back through its history."""
    ctx = ctx_for(principal)
    pid = await _jmolt_pid(request, ctx)
    fn = filename.strip() if filename and filename.strip() else None
    async with scoped_session(request.app.state.session_maker, ctx) as s:
        hist = await _SCRATCH.history(s, pid, fn)
    return [
        ScratchVersionOut(
            filename=h.filename, op=h.op, bytes=h.bytes, at=_iso(h.archived_at), content=h.content
        )
        for h in hist
    ]
