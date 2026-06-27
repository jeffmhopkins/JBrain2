"""HTTP surface: a token-authed command set over the session manager.

Every route except /healthz requires the bearer token (mirrors the supervisor:
the authed routes live on a router carrying the token dependency). Built by a
factory taking settings + a SessionManager so tests inject fakes — no git, no
model gateway. Internal-network only; the JBrain api is the sole caller and
proxies these to the owner (Wave J2).

The session is driven through its interactive terminal (a WebSocket PTY); there is
no headless turn endpoint. Exiting the shell pauses the session, which the launcher
can restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jcode_ctl.config import Settings
from jcode_ctl.preview import PreviewError, PreviewManager
from jcode_ctl.sessions import SessionError, SessionManager
from jcode_ctl.terminal import serve_terminal

_log = logging.getLogger("jcode_ctl")


async def reap_idle(
    sessions: SessionManager, preview: PreviewManager, ttl_seconds: int
) -> list[str]:
    """Delete every session idle past the TTL (and close its tunnel). Returns the
    reaped ids. The unit the GC loop calls — testable with a fake clock + fakes.
    Deliberately-paused (``stopped``) sessions are excluded by ``idle_sessions``."""
    reaped: list[str] = []
    for sid in sessions.idle_sessions(ttl_seconds=ttl_seconds):
        # Close the tunnel FIRST (the DELETE route's N3 invariant): a delete error must
        # never leave a live tunnel behind a reaped session.
        await preview.close(sid)
        # Re-confirm idle after that await suspension: a terminal may have opened (back
        # to active) or the session been stopped/deleted, so only proceed if it still
        # qualifies — its checkout must never be removed out from under a live shell.
        if sid not in sessions.idle_sessions(ttl_seconds=ttl_seconds):
            continue
        await sessions.delete(sid)
        reaped.append(sid)
    if reaped:
        _log.info("reaped %d idle session(s): %s", len(reaped), reaped)
    return reaped


async def _reaper_loop(
    sessions: SessionManager, preview: PreviewManager, settings: Settings
) -> None:
    while True:
        await asyncio.sleep(settings.reap_interval_seconds)
        try:
            await reap_idle(sessions, preview, settings.session_ttl_seconds)
        except Exception:
            # A reap failure (e.g. a workspace removal error) must not kill the loop —
            # but log it, so a recurring failure isn't silently swallowed forever.
            _log.exception("jcode session reaper sweep failed")


class CreateSessionRequest(BaseModel):
    repo: str = ""
    branch: str = "main"
    work_branch: str = ""
    # The served-model id the terminal pins the ``claude`` CLI to. Empty = the server's
    # configured default (settings.model). The api resolves the owner's selection.
    model: str = ""


class PreviewRequest(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)


def create_app(
    settings: Settings, sessions: SessionManager, preview: PreviewManager
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The session GC reaper runs for the life of the server.
        reaper = asyncio.create_task(_reaper_loop(sessions, preview, settings))
        _log.info(
            "jcode control server up: model=%s model_base_url=%s workspace=%s "
            "preview=%s ttl=%ds log_level=%s",
            settings.model,
            settings.model_base_url,
            settings.workspace_root,
            settings.preview_enabled,
            settings.session_ttl_seconds,
            settings.log_level,
        )
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
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
        session = await sessions.create(
            body.repo, body.branch, body.work_branch, model=body.model
        )
        return session.public()

    @authed.get("/sessions")
    def list_sessions() -> list[dict[str, object]]:
        return [s.public() for s in sessions.list()]

    @authed.get("/sessions/{sid}")
    def get_session(sid: str) -> dict[str, object]:
        return sessions.get(sid).public()

    @authed.post("/sessions/{sid}/reset")
    async def reset(sid: str) -> dict[str, object]:
        return (await sessions.reset(sid)).public()

    @authed.post("/sessions/{sid}/stop")
    def stop(sid: str) -> dict[str, object]:
        """Pause a session: kill its processes, keep the checkout. Mirrors the
        shell-exit path so the launcher can stop a session explicitly."""
        return sessions.stop(sid).public()

    @authed.post("/sessions/{sid}/restart")
    def restart(sid: str) -> dict[str, object]:
        """Resume a paused session (the checkout is still on disk)."""
        return sessions.restart(sid).public()

    @authed.delete("/sessions/{sid}", status_code=204)
    async def delete(sid: str) -> None:
        # Close the tunnel FIRST, so it's torn down even if the delete below raises
        # (review N3) — a deleted session keeps no live tunnel. delete() then kills open
        # shells before removing the checkout.
        await preview.close(sid)
        await sessions.delete(sid)

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

    @app.websocket("/sessions/{sid}/terminal")
    async def terminal(websocket: WebSocket, sid: str) -> None:
        # Manual token auth on the upgrade: a Depends that raises HTTPException can't
        # close a WS cleanly, so check the same bearer header here (constant-time) and
        # close 4401 on mismatch — the api forwards the token. 4404 for an unknown id.
        header = websocket.headers.get("authorization", "")
        if not hmac.compare_digest(header, expected_header):
            await websocket.close(code=4401)
            return
        session = sessions.get_or_none(sid)
        if session is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        # Pin the shell's `claude` CLI to this session's model (falling back to the
        # server default) so it doesn't default to a cloud model the on-box gateway has
        # no route for. on_open/on_close register the PTY pid so an open shell counts as
        # activity (the reaper won't reap under it) and delete can kill it.
        # on_shell_exit pauses the session when the user exits the shell (not a socket
        # drop).
        def _on_shell_exit(_pid: int) -> None:
            sessions.stop(sid)

        await serve_terminal(
            websocket,
            session.workspace,
            model=session.model or settings.model,
            on_open=lambda pid: sessions.terminal_opened(sid, pid),
            on_close=lambda pid: sessions.terminal_closed(sid, pid),
            on_shell_exit=_on_shell_exit,
        )

    app.include_router(authed)
    return app
