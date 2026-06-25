"""Owner-gated proxy to the jcode control server (code mode, Wave J2).

The api owns no coding agent — it proxies an owner's sandboxed session to the
internal control server and keeps a durable owner-only index (`jcode_sessions`)
for the launcher. The turn endpoint mirrors the `/chat` SSE contract: a detached
drive task feeds an in-process frame buffer (`_JcodeTurn`) that both the original
response and a reconnecting client follow to completion; an explicit cancel stops
it. Every route is `owner_only` — non-owner principals never reach code mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep
from jbrain.db import SessionContext, scoped_session
from jbrain.jcode import JcodeApi, JcodeError
from jbrain.models.jcode import JcodeSessionRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SSE_HEARTBEAT_SECONDS = 20.0
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_TURN_DONE = object()

# Every route declares `owner: OwnerDep`, which runs `owner_only` and 403s a
# non-owner — so code mode is owner-only without a router-level dependency.
router = APIRouter()


class _JcodeTurn:
    """In-flight turn frame buffer + live fan-out (adapted from the `/chat` `_LiveTurn`,
    docs/ASSISTANT.md). Keyed by run_id; the detached drive task feeds it via emit/finish."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.frames: list[bytes] = []
        self.done = False
        self._subs: set[asyncio.Queue[bytes | object]] = set()
        self.task: asyncio.Task[None] | None = None

    def emit(self, frame: bytes) -> None:
        self.frames.append(frame)
        for q in self._subs:
            q.put_nowait(frame)

    def finish(self) -> None:
        self.done = True
        for q in self._subs:
            q.put_nowait(_TURN_DONE)
        self._subs.clear()

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    async def stream(self, after: int = 0) -> AsyncIterator[bytes]:
        q: asyncio.Queue[bytes | object] = asyncio.Queue()
        for frame in self.frames[max(after, 0) :]:
            q.put_nowait(frame)
        if self.done:
            q.put_nowait(_TURN_DONE)
        else:
            self._subs.add(q)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if item is _TURN_DONE:
                    return
                yield cast(bytes, item)
        finally:
            self._subs.discard(q)


def _frame(type_: str, *, text: str = "", tool: str = "") -> bytes:
    payload = {"type": type_, "text": text, "tool": tool, "data": {}}
    return f"data: {json.dumps(payload)}\n\n".encode()


def _client(request: Request) -> JcodeApi:
    client = getattr(request.app.state, "jcode_client", None)
    if client is None:
        raise HTTPException(status_code=404, detail="code mode is not enabled")
    return cast(JcodeApi, client)


def _turns(request: Request) -> dict[str, _JcodeTurn]:
    return cast("dict[str, _JcodeTurn]", request.app.state.jcode_turns)


def _owner_ctx(principal_id: str) -> SessionContext:
    return SessionContext(principal_id=principal_id, principal_kind="owner")


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


_REPO = JcodeSessionRepo()


class CreateSessionBody(BaseModel):
    repo: str = ""
    branch: str = "main"
    work_branch: str = ""


class TurnBody(BaseModel):
    prompt: str = Field(min_length=1)


@router.post("/jcode/sessions", status_code=201)
async def create_session(
    body: CreateSessionBody, owner: OwnerDep, request: Request
) -> dict[str, object]:
    try:
        session = await _client(request).create_session(body.repo, body.branch, body.work_branch)
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
    return session


@router.get("/jcode/sessions")
async def list_sessions(owner: OwnerDep, request: Request) -> list[dict[str, object]]:
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        rows = await _REPO.list(db)
    return [row.__dict__ for row in rows]


@router.get("/jcode/sessions/{sid}")
async def get_session(sid: str, owner: OwnerDep, request: Request) -> dict[str, object]:
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        row = await _REPO.get(db, sid)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return row.__dict__


@router.delete("/jcode/sessions/{sid}", status_code=204)
async def delete_session(sid: str, owner: OwnerDep, request: Request) -> None:
    try:
        await _client(request).delete(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.delete(db, sid)


@router.post("/jcode/sessions/{sid}/reset")
async def reset_session(sid: str, owner: OwnerDep, request: Request) -> dict[str, object]:
    try:
        session = await _client(request).reset(sid)
    except JcodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.touch(db, sid, status="ready")
    return session


async def _set_status(
    maker: async_sessionmaker[AsyncSession], owner_id: str, sid: str, status: str
) -> None:
    async with scoped_session(maker, _owner_ctx(owner_id)) as db:
        await _REPO.touch(db, sid, status=status)


async def _drive(
    turn: _JcodeTurn,
    client: JcodeApi,
    sid: str,
    prompt: str,
    maker: async_sessionmaker[AsyncSession],
    owner_id: str,
    turns: dict[str, _JcodeTurn],
    run_id: str,
) -> None:
    """Detached: pump control-server frames into the buffer, keep the index status
    honest, and always emit a terminal frame — even on error or cancel."""
    await _set_status(maker, owner_id, sid, "running")
    try:
        async for frame in client.stream_turn(sid, prompt):
            turn.emit(frame)
    except JcodeError as exc:
        turn.emit(_frame("error", text=str(exc)))
        turn.emit(_frame("done"))
    except asyncio.CancelledError:
        turn.emit(_frame("done"))
        raise
    finally:
        # Settle the index status BEFORE finish() unblocks the client's stream, so a
        # read right after the stream ends sees "ready" — never a lingering "running".
        # finish() still runs even if the status write fails, so the client never hangs.
        try:
            await _set_status(maker, owner_id, sid, "ready")
        finally:
            turn.finish()
            turns.pop(run_id, None)


@router.post("/jcode/sessions/{sid}/turn")
async def run_turn(
    sid: str, body: TurnBody, owner: OwnerDep, request: Request
) -> StreamingResponse:
    client = _client(request)
    turns = _turns(request)
    run_id = uuid.uuid4().hex
    turn = _JcodeTurn(sid)
    turns[run_id] = turn
    turn.task = asyncio.create_task(
        _drive(turn, client, sid, body.prompt, _maker(request), owner.id, turns, run_id)
    )
    headers = {**_SSE_HEADERS, "X-Jcode-Run-Id": run_id}
    return StreamingResponse(turn.stream(), media_type="text/event-stream", headers=headers)


@router.get("/jcode/runs/{run_id}/stream")
async def reconnect(
    run_id: str, owner: OwnerDep, request: Request, after: int = 0
) -> StreamingResponse:
    turn = _turns(request).get(run_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="run is no longer live")
    return StreamingResponse(
        turn.stream(after), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/jcode/runs/{run_id}/cancel", status_code=202)
async def cancel_turn(run_id: str, owner: OwnerDep, request: Request) -> dict[str, str]:
    turn = _turns(request).get(run_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="run is no longer live")
    turn.cancel()
    # The control server may have already finished; cancel is best-effort.
    with contextlib.suppress(JcodeError):
        await _client(request).cancel(turn.session_id)
    return {"status": "cancelling"}
