"""HTTP surface: a token-authed command set over the session manager.

Every route except /healthz requires the bearer token (mirrors the supervisor:
the authed routes live on a router carrying the token dependency). Built by a
factory taking settings + a SessionManager so tests inject fakes — no SDK, no
git, no model gateway. Internal-network only; the JBrain api is the sole caller
and proxies these to the owner (Wave J2).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from jcode_ctl.agent import TurnEvent
from jcode_ctl.config import Settings
from jcode_ctl.preview import PreviewError, PreviewManager
from jcode_ctl.sessions import SessionError, SessionManager

# SSE responses must not be buffered by a proxy (Caddy/nginx), or the turn stream
# arrives all-at-once and the live UX is lost — mirrors the supervisor's SSE.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class CreateSessionRequest(BaseModel):
    repo: str = ""
    branch: str = "main"
    work_branch: str = ""


class TurnRequest(BaseModel):
    prompt: str


class PreviewRequest(BaseModel):
    port: int | None = None


def _frame(ev: TurnEvent) -> bytes:
    payload = {"type": ev.type, "text": ev.text, "tool": ev.tool, "data": ev.data}
    return f"data: {json.dumps(payload)}\n\n".encode()


def create_app(
    settings: Settings, sessions: SessionManager, preview: PreviewManager
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Tear down every live tunnel so no preview outlives the server.
        await preview.close_all()

    app = FastAPI(title="jcode control server", lifespan=lifespan)

    expected_header = f"Bearer {settings.token}"

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        # Compare the WHOLE header in constant time (supervisor-style), so a wrong
        # scheme fails identically to a wrong token. Fail-closed on a missing header.
        if not authorization or not hmac.compare_digest(authorization, expected_header):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(SessionError)
    async def _session_error(_: Request, exc: SessionError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(PreviewError)
    async def _preview_error(_: Request, exc: PreviewError) -> JSONResponse:
        # 409: the preview can't be opened right now (disabled, or the tunnel failed).
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    authed = APIRouter(dependencies=[Depends(require_token)])

    @authed.post("/sessions", status_code=201)
    async def create_session(body: CreateSessionRequest) -> dict[str, object]:
        session = await sessions.create(body.repo, body.branch, body.work_branch)
        return session.public()

    @authed.get("/sessions")
    def list_sessions() -> list[dict[str, object]]:
        return [s.public() for s in sessions.list()]

    @authed.get("/sessions/{sid}")
    def get_session(sid: str) -> dict[str, object]:
        return sessions.get(sid).public()

    @authed.post("/sessions/{sid}/turn")
    async def run_turn(sid: str, body: TurnRequest) -> StreamingResponse:
        # Validate the session exists before opening the stream, so a bad id is a
        # clean 404 rather than an error frame.
        sessions.get(sid)

        async def frames() -> AsyncIterator[bytes]:
            # Guarantee a terminal frame even if the agent RAISES mid-turn (vs. yielding
            # an error event), so the client never sees a silently truncated stream.
            try:
                async for ev in sessions.run_turn(sid, body.prompt):
                    yield _frame(ev)
            except Exception as exc:
                yield _frame(TurnEvent("error", text=str(exc)))
                yield _frame(TurnEvent("done"))

        return StreamingResponse(
            frames(), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    @authed.post("/sessions/{sid}/cancel", status_code=202)
    async def cancel(sid: str) -> dict[str, str]:
        await sessions.cancel(sid)
        return {"status": "cancelling"}

    @authed.post("/sessions/{sid}/reset")
    async def reset(sid: str) -> dict[str, object]:
        return (await sessions.reset(sid)).public()

    @authed.delete("/sessions/{sid}", status_code=204)
    async def delete(sid: str) -> None:
        sessions.delete(sid)
        await preview.close(sid)  # a deleted session keeps no live tunnel

    # --- Web preview (Wave J4): an ephemeral tunnel to the sandbox's dev server ---

    @authed.get("/sessions/{sid}/preview")
    def preview_status(sid: str) -> dict[str, object]:
        sessions.get(sid)
        return {"enabled": preview.enabled, "url": preview.url(sid)}

    @authed.post("/sessions/{sid}/preview")
    async def preview_open(sid: str, body: PreviewRequest) -> dict[str, object]:
        sessions.get(sid)
        url = await preview.open(sid, body.port)
        return {"enabled": True, "url": url}

    @authed.delete("/sessions/{sid}/preview", status_code=204)
    async def preview_close(sid: str) -> None:
        await preview.close(sid)

    app.include_router(authed)
    return app
