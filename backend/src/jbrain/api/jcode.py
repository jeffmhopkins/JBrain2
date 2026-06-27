"""Owner-gated proxy to the jcode control server (code mode, Wave J2).

The api owns no coding agent — it proxies an owner's sandboxed session to the
internal control server and keeps a durable owner-only index (`jcode_sessions`)
for the launcher. The session is driven through its interactive terminal (a
WebSocket PTY, see `api.jcode_terminal`); there is no turn/SSE surface. Every
route is `owner_only` — non-owner principals never reach code mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import JcodeAccessDep, OwnerDep
from jbrain.db import SessionContext, scoped_session
from jbrain.jcode import JcodeApi, JcodeError
from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGatewayError
from jbrain.models.jcode import JcodeSessionRepo
from jbrain.settings_store import SqlSettingsStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.config import Settings
    from jbrain.llm.local_gateway import LocalGateway

log = logging.getLogger(__name__)

# Every route declares `owner: OwnerDep`, which runs `owner_only` and 403s a
# non-owner — so code mode is owner-only without a router-level dependency.
router = APIRouter()


# The control server mints opaque ids (hex / uuid). Validate the shape at the api
# boundary so a caller-supplied sid can never carry a `/` or `..` into the control
# server's URL path (review S2) — even though the base host is pinned.
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_sid(sid: str) -> None:
    if not _SID_RE.match(sid):
        raise HTTPException(status_code=404, detail="unknown session")


def _client(request: Request) -> JcodeApi:
    client = getattr(request.app.state, "jcode_client", None)
    if client is None:
        raise HTTPException(status_code=404, detail="code mode is not enabled")
    return cast(JcodeApi, client)


def _owner_ctx(principal_id: str) -> SessionContext:
    return SessionContext(principal_id=principal_id, principal_kind="owner")


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


def _store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


async def _resolve_model(request: Request, owner_id: str) -> str:
    """The model a new session runs: the owner's stored selection (Settings → LLM),
    else the JBRAIN_JCODE_MODEL config default. Read here rather than via a Depends
    so the unconfigured/owner gating runs before any settings access.

    The stored id is taken as-is — not re-validated against the currently-installed
    set. If the owner picked a model and later uninstalled it, the turn fails at the
    gateway (same as the default before its weights are provisioned); the settings
    screen surfaces that via the dropdown's "(not installed)" option."""
    settings = cast("Settings", request.app.state.settings)
    return (await _store(request).jcode_model(_owner_ctx(owner_id))) or settings.jcode_model


def _served_model(model_id: str) -> str:
    """The gateway's served-model name for a catalog id (they match for the coder, but
    resolve via the catalog to be correct)."""
    m = local_catalog.get(model_id)
    return m.served_model if m else model_id


def _warming_models(request: Request) -> Counter[str]:
    """In-flight warm tasks per served-model name — the readiness signal the loading bar
    polls. Tied to the warm task's lifecycle (not gateway `running()`, which lists a model
    as soon as a load is *requested*, before its weights finish reading in). A Counter, not
    a set: concurrent creates warm the SAME coder, so the flag must stay up until the LAST
    of them finishes (a set would let the first done-callback clear it early)."""
    state = request.app.state
    warming = getattr(state, "jcode_warming", None)
    if warming is None:
        warming = Counter()
        state.jcode_warming = warming
    return cast("Counter[str]", warming)


def _warm_tasks(request: Request) -> set[asyncio.Task[None]]:
    state = request.app.state
    tasks = getattr(state, "jcode_warm_tasks", None)
    if tasks is None:
        tasks = set()
        state.jcode_warm_tasks = tasks
    return cast("set[asyncio.Task[None]]", tasks)


async def _warm_model(gateway: LocalGateway, served: str) -> None:
    # Give the coder the whole box: evict every OTHER resident model, then load it. A
    # cold 80B load reads tens of GB (blocks up to ~2 min), so this runs in the
    # background — never blocking session creation. NOT unloaded later: the coder stays
    # resident until another JBrain task loads a different model (the gateway swaps it
    # then). All best-effort: a gateway hiccup must never break a session.
    with contextlib.suppress(Exception):
        resident = await gateway.running()
        # Already loaded → no-op: don't evict anything and don't re-probe a load (which
        # could force the gateway to re-read the weights). Switching to the coder when
        # it's already on the box must be instant.
        if served in resident:
            return
        for other in resident:
            with contextlib.suppress(LocalGatewayError):
                await gateway.unload(other)
        await gateway.load(served)


def _warm_coder(request: Request, model_id: str) -> None:
    """Fire-and-forget warm of the coder: evict the other resident models and load it.
    Triggered ONLY by the explicit warm route, after the session screen has confirmed the
    swap with the owner — never automatically on session create, so we never evict a model
    the owner is using (and didn't ask to replace) just by opening code mode."""
    settings = cast("Settings", request.app.state.settings)
    if not settings.local_llm_enabled:
        return
    gateway = getattr(request.app.state, "local_gateway", None)
    if gateway is None:
        return
    served = _served_model(model_id)
    # Mark warming BEFORE the task starts so the very first status poll (which can race
    # the task) already sees it — and keep it up until the task finishes (the blocking
    # health-gated load is the true readiness window the bar should span). Refcounted so
    # overlapping creates of the same coder don't clear each other early.
    warming = _warming_models(request)
    warming[served] += 1
    task = asyncio.create_task(_warm_model(gateway, served))
    tasks = _warm_tasks(request)
    tasks.add(task)

    def _done(t: asyncio.Task[None]) -> None:
        tasks.discard(t)
        warming[served] -= 1
        if warming[served] <= 0:
            del warming[served]

    task.add_done_callback(_done)


_REPO = JcodeSessionRepo()


class CreateSessionBody(BaseModel):
    repo: str = ""
    branch: str = "main"
    work_branch: str = ""


class RenameBody(BaseModel):
    title: str = ""


class PreviewBody(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


@router.post("/jcode/sessions", status_code=201)
async def create_session(
    body: CreateSessionBody, owner: OwnerDep, request: Request
) -> dict[str, object]:
    # 404 first when code mode is unconfigured, before any settings read. The model
    # is fixed at create (the owner's selection, else the default) so a mid-session
    # settings change never re-points a live session.
    client = _client(request)
    model = await _resolve_model(request, owner.id)
    try:
        session = await client.create_session(body.repo, body.branch, body.work_branch, model)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.upsert(
            db,
            id=session["id"],
            repo=session.get("repo", ""),
            branch=session.get("branch", "main"),
            work_branch=session.get("work_branch", ""),
            status=session.get("status", "ready"),
        )
    # The coder is NOT warmed here: a session opening must not silently evict whatever
    # model is resident. The session screen polls /jcode/model, and — when the coder isn't
    # already loaded — asks the owner before POSTing /jcode/model/warm to do the swap.
    return session


async def _model_payload(request: Request, owner_id: str) -> dict[str, object]:
    """The code-mode model status: the configured coder, whether it's resident, whether a
    warm is in flight, and what else is currently on the box (so the screen can tell the
    owner which model a swap would evict)."""
    settings = cast("Settings", request.app.state.settings)
    model_id = await _resolve_model(request, owner_id)
    served = _served_model(model_id)
    cat = local_catalog.get(model_id)
    running: set[str] = set()
    gateway = getattr(request.app.state, "local_gateway", None)
    if settings.local_llm_enabled and gateway is not None:
        running = await cast("LocalGateway", gateway).running()
    # `warming` is the bar's primary signal: true while the warm task runs (eviction +
    # the up-to-2-min health-gated load). `loaded` alone races true early, so the bar
    # keys off `warming` to stay up for the whole real load.
    warming = _warming_models(request)[served] > 0
    return {
        "model": model_id,
        "served": served,
        "loaded": served in running,
        "warming": warming,
        "hosting": settings.local_llm_enabled,
        "size_gb": cat.size_gb if cat else 0.0,
        # The served context window the coder runs with — full native (262144) for the
        # coder, so the terminal's `claude` gets the whole window. Surfaced so the screen
        # can show it ("256k") next to the model.
        "context_window": cat.context_window if cat else 0,
        "resident": sorted(running),
    }


@router.get("/jcode/model")
async def model_status(owner: OwnerDep, request: Request) -> dict[str, object]:
    """Whether the code-mode coder is resident in the gateway — the session screen's
    load prompt + loading-bar poll. Owner-gated; `hosting` is false when local hosting is
    off. Unlike the session routes it does NOT 404 when code mode is disabled — it reports
    residency of the configured model regardless, and the poll only runs from the session
    screen."""
    return await _model_payload(request, owner.id)


@router.post("/jcode/model/warm")
async def warm_model(owner: OwnerDep, request: Request) -> dict[str, object]:
    """Explicitly warm the coder onto the box (evict the other resident models, then load
    it), and report the fresh status. The session screen calls this only after the owner
    confirms the swap — so the eviction is never a surprise."""
    model_id = await _resolve_model(request, owner.id)
    _warm_coder(request, model_id)
    return await _model_payload(request, owner.id)


@router.get("/jcode/sessions")
async def list_sessions(owner: OwnerDep, request: Request) -> list[dict[str, object]]:
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        rows = await _REPO.list(db)
    return [row.__dict__ for row in rows]


@router.get("/jcode/sessions/{sid}")
async def get_session(sid: str, principal: JcodeAccessDep, request: Request) -> dict[str, object]:
    _valid_sid(sid)
    # The owner reads its launcher mirror; a share principal (no owner-RLS access to that
    # table) gets the same session straight from the control server, the source of truth.
    if principal.kind == "owner":
        async with scoped_session(_maker(request), _owner_ctx(principal.id)) as db:
            row = await _REPO.get(db, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return row.__dict__
    try:
        return await _client(request).get_session(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc


@router.delete("/jcode/sessions/{sid}", status_code=204)
async def delete_session(sid: str, owner: OwnerDep, request: Request) -> None:
    _valid_sid(sid)
    # The control server's delete kills open shells (and anything running in the
    # checkout) before removing it, so nothing is stranded on a vanished cwd.
    try:
        await _client(request).delete(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.delete(db, sid)


@router.patch("/jcode/sessions/{sid}", status_code=204)
async def rename_session(sid: str, body: RenameBody, owner: OwnerDep, request: Request) -> None:
    # Launcher-only metadata: rename/archive touch the owner's index, never the control
    # server (the sandbox doesn't care about the label). 204 even for an unknown sid —
    # the UPDATE is a no-op, mirroring the agent-sessions manager.
    _valid_sid(sid)
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.rename(db, sid, body.title)


@router.post("/jcode/sessions/{sid}/archive", status_code=204)
async def archive_session(sid: str, owner: OwnerDep, request: Request) -> None:
    """Tidy a session out of the live list without deleting it (archived → true)."""
    _valid_sid(sid)
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.set_archived(db, sid, True)


@router.post("/jcode/sessions/{sid}/unarchive", status_code=204)
async def unarchive_session(sid: str, owner: OwnerDep, request: Request) -> None:
    """Restore an archived session to the live list (archived → false)."""
    _valid_sid(sid)
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.set_archived(db, sid, False)


@router.post("/jcode/sessions/{sid}/reset")
async def reset_session(sid: str, owner: OwnerDep, request: Request) -> dict[str, object]:
    _valid_sid(sid)
    try:
        session = await _client(request).reset(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.touch(db, sid, status="ready")
    return session


@router.post("/jcode/sessions/{sid}/stop")
async def stop_session(sid: str, owner: OwnerDep, request: Request) -> dict[str, object]:
    """Pause a session: the control server kills its processes but keeps the checkout, so
    it can be restarted. Mirrors the shell-exit pause for the launcher."""
    _valid_sid(sid)
    try:
        session = await _client(request).stop(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.touch(db, sid, status=str(session.get("status", "stopped")))
    return session


@router.post("/jcode/sessions/{sid}/restart")
async def restart_session(sid: str, owner: OwnerDep, request: Request) -> dict[str, object]:
    """Resume a paused session (its checkout is still on disk)."""
    _valid_sid(sid)
    try:
        session = await _client(request).restart(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.touch(db, sid, status=str(session.get("status", "ready")))
    return session


# --- Web preview (Wave J4): proxy the control server's ephemeral-tunnel surface ---


@router.get("/jcode/sessions/{sid}/preview")
async def preview_status(sid: str, _access: JcodeAccessDep, request: Request) -> dict[str, object]:
    _valid_sid(sid)
    try:
        return await _client(request).preview_status(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/jcode/sessions/{sid}/preview")
async def preview_open(
    sid: str, body: PreviewBody, _access: JcodeAccessDep, request: Request
) -> dict[str, object]:
    _valid_sid(sid)
    try:
        return await _client(request).preview_open(sid, body.port)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/jcode/sessions/{sid}/preview", status_code=204)
async def preview_close(sid: str, _access: JcodeAccessDep, request: Request) -> None:
    _valid_sid(sid)
    with contextlib.suppress(JcodeError):
        await _client(request).preview_close(sid)
