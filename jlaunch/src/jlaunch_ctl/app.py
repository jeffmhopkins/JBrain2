"""HTTP/WS surface for the jlaunch control server.

Every route except /healthz requires the bearer token (mirrors jcode/the supervisor).
The live terminal is a WebSocket that *attaches* to a job's already-running PTY — it
never creates one, so a browser only ever observes a job the manager started. The app is
built by a factory taking settings, the job manager, and the terminal registry so tests
inject fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import re
from typing import TYPE_CHECKING, Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jlaunch_ctl.jobs import BusyError, JobError, JobManager
from jlaunch_ctl.specs import SPECS
from jlaunch_ctl.terminal import PtyTerminal, TerminalRegistry, _recv_loop

if TYPE_CHECKING:
    from jlaunch_ctl.config import Settings

# Job ids are the manager's hex tokens; gate the shape so a malformed id can never be
# interpolated anywhere downstream.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StartJobRequest(BaseModel):
    spec: str


async def _attach(websocket: WebSocket, term: PtyTerminal) -> None:  # pragma: no cover
    """Attach an accepted WebSocket to a running PTY: replay scrollback then stream live
    output, forwarding keystrokes/resize. Takeover: a new client closes the previous
    one so a single client drives at a time. Detach never kills the job."""
    previous = term.attached
    term.attached = websocket
    if previous is not None and previous is not websocket:
        with contextlib.suppress(Exception):
            await previous.close()
    recv = asyncio.ensure_future(_recv_loop(websocket, term))
    send = asyncio.ensure_future(term.stream_to(websocket))
    try:
        done, pending = await asyncio.wait(
            {recv, send}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if send in done and send.exception() is None and send.result():
            with contextlib.suppress(Exception):
                await websocket.close()
    finally:
        if term.attached is websocket:
            term.attached = None


def create_app(
    settings: Settings, jobs: JobManager, registry: TerminalRegistry
) -> FastAPI:
    app = FastAPI(title="jbrain-jlaunch")
    expected = f"Bearer {settings.token}".encode()

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        provided = (authorization or "").encode()
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    authed = APIRouter(dependencies=[Depends(require_token)])

    @authed.get("/specs")
    def list_specs() -> list[dict[str, object]]:
        return [spec.public() for spec in SPECS.values()]

    @authed.post("/jobs", status_code=201)
    async def start_job(body: StartJobRequest) -> dict[str, object]:
        try:
            job = await jobs.start(body.spec)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return job.public()

    @authed.get("/jobs")
    def list_jobs() -> list[dict[str, object]]:
        return [job.public() for job in jobs.list()]

    @authed.get("/jobs/{jid}")
    def get_job(jid: str) -> dict[str, object]:
        return _require(jobs, jid).public()

    @authed.post("/jobs/{jid}/stop")
    async def stop_job(jid: str) -> dict[str, object]:
        _require(jobs, jid)
        return (await jobs.stop(jid)).public()

    @authed.post("/jobs/{jid}/kill")
    async def kill_job(jid: str) -> dict[str, object]:
        _require(jobs, jid)
        return (await jobs.kill(jid)).public()

    @authed.delete("/jobs/{jid}", status_code=204)
    async def delete_job(jid: str) -> None:
        if not _ID_RE.match(jid):
            raise HTTPException(status_code=404, detail="unknown job")
        await jobs.delete(jid)

    @authed.get("/jobs/{jid}/artifact")
    def get_artifact(jid: str) -> FileResponse:
        job = _require(jobs, jid)
        try:
            path = jobs.artifact_path(jid)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        spec = SPECS.get(job.spec_name)
        media = spec.artifact_media_type if spec else "application/octet-stream"
        return FileResponse(path, media_type=media, filename=jobs.artifact_name(jid))

    app.include_router(authed)

    @app.websocket("/jobs/{jid}/terminal")
    async def terminal(websocket: WebSocket, jid: str) -> None:
        auth = websocket.headers.get("authorization", "").encode()
        if not hmac.compare_digest(auth, expected):
            await websocket.close(code=4401)
            return
        if not _ID_RE.match(jid):
            await websocket.close(code=4404)
            return
        term = registry.get(jid)
        if term is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        await _attach(websocket, term)

    return app


def _require(jobs: JobManager, jid: str):
    if not _ID_RE.match(jid):
        raise HTTPException(status_code=404, detail="unknown job")
    try:
        return jobs.get(jid)
    except JobError as exc:
        raise HTTPException(status_code=404, detail="unknown job") from exc
