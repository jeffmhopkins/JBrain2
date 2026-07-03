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
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jcode_ctl.config import Settings
from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.preview import PreviewError
from jcode_ctl.preview_proxy import proxy_http, proxy_ws
from jcode_ctl.sessions import SessionError, SessionManager
from jcode_ctl.terminal import TerminalRegistry, serve_terminal

_log = logging.getLogger("jcode_ctl")


async def reap_idle(
    sessions: SessionManager,
    ttl_seconds: int,
    host_preview: HostPreviewManager,
) -> list[str]:
    """Delete every session idle past the TTL (releasing its preview reservation).
    Returns the reaped ids. The unit the GC loop calls — testable with a fake clock.
    Deliberately-paused (``stopped``) sessions are excluded by ``idle_sessions``."""
    reaped: list[str] = []
    for sid in sessions.idle_sessions(ttl_seconds=ttl_seconds):
        # Re-confirm idle after the prior iteration's delete await: a terminal may have
        # opened (back to active) or the session been stopped/deleted, so proceed only
        # while it still qualifies — never remove a checkout under a live shell.
        if sid not in sessions.idle_sessions(ttl_seconds=ttl_seconds):
            continue
        # The reservation is in-memory (no crash-survival concern), so release it only
        # once the reap is confirmed — never out from under a session that re-activated
        # since the idle snapshot (which would orphan its live dev server).
        host_preview.release(sid)
        await sessions.delete(sid)
        reaped.append(sid)
    if reaped:
        _log.info("reaped %d idle session(s): %s", len(reaped), reaped)
    return reaped


async def _reaper_loop(
    sessions: SessionManager,
    settings: Settings,
    host_preview: HostPreviewManager,
) -> None:
    while True:
        await asyncio.sleep(settings.reap_interval_seconds)
        try:
            await reap_idle(sessions, settings.session_ttl_seconds, host_preview)
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
    settings: Settings,
    sessions: SessionManager,
    host_preview: HostPreviewManager,
) -> FastAPI:
    # host_preview is the per-session port + hostname allocator fronting each session's
    # dev server. It fail-closes (.enabled is False) when no base host is configured.
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The session GC reaper runs for the life of the server.
        reaper = asyncio.create_task(_reaper_loop(sessions, settings, host_preview))
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
            # Kill every persistent shell so none outlives the server, then drop every
            # preview reservation.
            terminals.close_all()
            host_preview.release_all()

    app = FastAPI(title="jcode control server", lifespan=lifespan)

    @app.middleware("http")
    async def _log_requests(request: Request, call_next: Any) -> Any:
        # Per-request trace at DEBUG — verbose only when debug access is on (the level
        # is forced to DEBUG then), so the owner debug console shows every control call
        # the api made. Cheap and global; nothing logged at the default INFO level.
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("→ %s %s", request.method, request.url.path)
            response = await call_next(request)
            _log.debug(
                "← %s %s %d", request.method, request.url.path, response.status_code
            )
            return response
        return await call_next(request)

    # The live persistent shells, keyed by session id. A shell outlives any one terminal
    # socket (you leave the app, it keeps running) and is reattached on reconnect; the
    # registry is torn down with the server.
    terminals = TerminalRegistry()

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
        # 409: preview can't be served right now (web preview disabled / no base host).
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
        # The port/slug reservation is kept across a reset: the session lives on (ready
        # after reset), so its preview URL stays stable for the next dev server.
        return (await sessions.reset(sid)).public()

    @authed.post("/sessions/{sid}/stop")
    async def stop(sid: str) -> dict[str, object]:
        """Pause a session: kill its processes, keep the checkout. Mirrors the
        shell-exit path so the launcher can stop a session explicitly."""
        # The port/slug reservation persists across a pause (the proxy gates a stopped
        # session), so a restart resumes the same preview URL — nothing to tear down.
        return sessions.stop(sid).public()

    @authed.post("/sessions/{sid}/restart")
    def restart(sid: str) -> dict[str, object]:
        """Resume a paused session (the checkout is still on disk)."""
        return sessions.restart(sid).public()

    @authed.delete("/sessions/{sid}", status_code=204)
    async def delete(sid: str) -> None:
        # Drop the reservation FIRST so it's gone even if the delete below raises
        # (review N3) — a deleted session keeps no reachable preview. delete() then
        # kills open shells before removing the checkout.
        host_preview.release(sid)
        await sessions.delete(sid)

    # --- Web preview: a per-session hostname under the box's own tunnel, fronted by the
    # api↔jcode proxy below (docs/archive/JCODE_PREVIEW_HOST_PLAN.md). ---

    @authed.get("/sessions/{sid}/preview")
    def preview_status(sid: str) -> dict[str, object]:
        sessions.get(sid)
        # The reserved `port` lets the GUI show "run your dev server on :<port>". `mode`
        # is retained for the GUI that reads it; there's nothing to "open" or "stop".
        return {
            "enabled": host_preview.enabled,
            "url": host_preview.url(sid),
            "mode": "host",
            "port": host_preview.port_for(sid),
        }

    @authed.post("/sessions/{sid}/preview")
    async def preview_open(sid: str, _body: PreviewRequest) -> dict[str, object]:
        sessions.get(sid)
        # Nothing to spin up — the hostname is reserved once and reported; the session's
        # dev server appears at it when it starts (the proxy probes live). 409 if off.
        a = host_preview.ensure(sid)
        return {"enabled": True, "url": a.url, "mode": "host", "port": a.port}

    @authed.delete("/sessions/{sid}/preview", status_code=204)
    async def preview_close(sid: str) -> None:
        # No-op, kept for API compatibility: the reservation is kept (released on
        # delete/reap) and a stable URL is the point — there's nothing to tear down.
        sessions.get(sid)

    @authed.api_route(
        "/preview/{slug}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    @authed.api_route(
        "/preview/{slug}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def preview_proxy_route(
        request: Request, slug: str, path: str = ""
    ) -> Response:
        # The api forwards <slug>-preview.<host> here by slug; resolve it to the live
        # session's reserved dev port and reverse-proxy. Unknown slug or a session that
        # isn't running both refuse — a paused/absent session is never reachable.
        sid = host_preview.resolve(slug)
        session = sessions.get_or_none(sid) if sid is not None else None
        port = host_preview.port_for(sid) if sid is not None else None
        if session is None or session.status == "stopped" or port is None:
            return Response("preview not available", status_code=404)
        return await proxy_http(port, path, request)

    @app.websocket("/preview/{slug}/{path:path}")
    @app.websocket("/preview/{slug}")
    async def preview_ws_route(websocket: WebSocket, slug: str, path: str = "") -> None:
        # The HMR live-reload channel: the api forwards the preview WS upgrade here by
        # slug (bearer-authed on the handshake, like the terminal). Same guard as the
        # HTTP proxy — unknown slug or a non-running session refuses before any upstream
        # connect; proxy_ws accepts the browser socket only after the dev WS is reached.
        header = websocket.headers.get("authorization", "")
        if not hmac.compare_digest(header, expected_header):
            await websocket.close(code=4401)
            return
        sid = host_preview.resolve(slug)
        session = sessions.get_or_none(sid) if sid is not None else None
        port = host_preview.port_for(sid) if sid is not None else None
        if session is None or session.status == "stopped" or port is None:
            await websocket.close(code=4404)
            return
        await proxy_ws(websocket, port, path, websocket.url.query)

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
        # no route for. The shell is persistent — it outlives this socket (leaving the
        # app keeps it running) and is reattached on reconnect; on_open registers its
        # pid once so an open shell counts as activity (the reaper won't reap it) and
        # stop/delete can kill it. on_shell_exit pauses the session when the user exits
        # the shell itself (not a socket drop). It can race a delete (the session's
        # already gone), so the pause is best-effort.
        def _on_shell_exit(_pid: int) -> None:
            # Exiting the shell is the common pause path. The preview reservation is
            # kept across a pause (the proxy gates a stopped session), so there's
            # nothing to tear down here.
            with contextlib.suppress(SessionError):
                sessions.stop(sid)

        # The dev server binds the session's reserved port via $PORT — a per-session
        # port (so concurrent previews don't collide), or the default if preview is off.
        preview_port = settings.preview_default_port
        if host_preview.enabled:
            preview_port = host_preview.ensure(sid).port

        await serve_terminal(
            websocket,
            sid,
            terminals,
            session.workspace,
            model=session.model or settings.model,
            preview_port=preview_port,
            home=str(sessions.home_for(sid)),
            on_open=lambda pid: sessions.terminal_opened(sid, pid),
            on_close=lambda pid: sessions.terminal_closed(sid, pid),
            on_shell_exit=_on_shell_exit,
        )

    app.include_router(authed)
    return app
