"""`/api/debug/jlaunch/*` — the assistant's read-only window on the job launcher.

The owner has no terminal, so a long run on the box (a GrowBot training run, a census)
is followed from a debug token: the runs and their state, the console tail, a running
job's live folder (progress, the latest replay, the latest weights) and the finished
result. One write: starting a registered spec (owner, 2026-11-14: the owner went to bed
handing the assistant "start the camera walk and look training"). It runs only what the
code-defined spec registry holds, one job at a time, exactly as the PWA's Start; stopping
and deleting a run stay with the owner. The bytes come back exactly as the owner's routes
serve them (`jlaunch.py`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from jbrain.api import jlaunch
from jbrain.api.deps import DebugDep
from jbrain.db.session import SessionContext
from jbrain.jlaunch import JlaunchError

router = APIRouter(prefix="/debug/jlaunch")

# Rows a debug start writes carry the console's owner identity, as its other writes do.
_OWNER_CTX = SessionContext(principal_id="debug-console", principal_kind="owner")


class StartIn(BaseModel):
    spec: str


@router.post("/jobs", status_code=201)
async def jlaunch_start(body: StartIn, request: Request, _p: DebugDep) -> dict[str, Any]:
    """**Start a run** of a registered spec (names from `GET /debug/jlaunch`), as the PWA's
    Start does. 404 for an unknown spec, 409 while another job is running."""
    request.state.debug_detail = f"jlaunch start {body.spec}"
    return await jlaunch.start(request, body.spec, _OWNER_CTX)


@router.get("")
async def jlaunch_overview(request: Request, _p: DebugDep) -> dict[str, Any]:
    """The runnable specs and every job the control server knows, newest first. A job here
    carries its state, phase, headline and finish reason; `GET /jobs/{id}` adds the
    console tail."""
    request.state.debug_detail = "jlaunch overview"
    client = jlaunch._client(request)
    try:
        specs = await client.list_specs()
        jobs = await client.list_jobs()
    except JlaunchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    jobs = sorted(jobs, key=lambda j: str(j.get("started_at") or ""), reverse=True)
    return {"specs": specs, "jobs": jobs}


@router.get("/jobs/{jid}")
async def jlaunch_job(jid: str, request: Request, _p: DebugDep) -> dict[str, Any]:
    request.state.debug_detail = f"jlaunch job {jid}"
    jlaunch._valid_id(jid)
    try:
        return await jlaunch._client(request).get_job(jid)
    except JlaunchError as exc:
        raise jlaunch._upstream_404(exc, "unknown job") from exc


@router.get("/jobs/{jid}/live")
async def jlaunch_live(jid: str, request: Request, _p: DebugDep) -> list[dict[str, Any]]:
    """The files in a job's live folder. `progress.json` is the one to read first."""
    request.state.debug_detail = f"jlaunch live {jid}"
    return await jlaunch.live_listing(jlaunch._client(request), jid)


@router.get("/jobs/{jid}/live/{name}")
async def jlaunch_live_file(
    jid: str,
    name: str,
    request: Request,
    _p: DebugDep,
    download: Annotated[bool, Query()] = False,
) -> Response:
    request.state.debug_detail = f"jlaunch live {jid}/{name}"
    return await jlaunch.live_file(jlaunch._client(request), jid, name, download=download)


@router.get("/jobs/{jid}/artifact")
async def jlaunch_artifact(jid: str, request: Request, _p: DebugDep) -> StreamingResponse:
    """A finished job's result (the spec's tarball), streamed. 404 until it succeeded."""
    request.state.debug_detail = f"jlaunch artifact {jid}"
    return await jlaunch.artifact_download(jlaunch._client(request), jid)
