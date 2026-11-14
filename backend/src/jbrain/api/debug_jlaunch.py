"""`/api/debug/jlaunch/*` — the assistant's read-only window on the job launcher.

The owner has no terminal, so a long run on the box (a GrowBot training run, a census)
is followed from a debug token: the runs and their state, the console tail, a running
job's live folder (progress, the latest replay, the latest weights) and the finished
result. Three writes: starting a registered spec (owner, 2026-11-14: the owner went to bed
handing the assistant "start the camera walk and look training"), which runs only what
the code-defined spec registry holds, one job at a time, exactly as the PWA's Start; and
deleting a finished run (owner, 2026-11-14: "delete bad runs ... via debug token"), which
refuses a live one; and the owner's note on a run. Stopping a run stays with the owner.
The bytes come back exactly as the owner's routes serve them (`jlaunch.py`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from jbrain.api import jlaunch
from jbrain.api.deps import DebugDep
from jbrain.db.session import SessionContext
from jbrain.jlaunch import JlaunchError

router = APIRouter(prefix="/debug/jlaunch")

# Rows a debug start writes carry the console's owner identity, as its other writes do.
_OWNER_CTX = SessionContext(principal_id="debug-console", principal_kind="owner")


@router.post("/jobs", status_code=201)
async def jlaunch_start(
    body: jlaunch.StartJobRequest, request: Request, _p: DebugDep
) -> dict[str, Any]:
    """**Start a run** of a registered spec (names from `GET /debug/jlaunch`), as the PWA's
    Start does. 404 for an unknown spec, 409 while another job is running."""
    request.state.debug_detail = f"jlaunch start {body.spec}"
    return await jlaunch.start(request, body.spec, _OWNER_CTX)


@router.delete("/jobs/{jid}", status_code=204)
async def jlaunch_delete(jid: str, request: Request, _p: DebugDep) -> None:
    """**Delete a finished run**: its checkout, scratch, result and share links, as the
    PWA's Delete run does. 409 while the job is still live (stop it first, from the PWA)."""
    jlaunch._valid_id(jid)
    request.state.debug_detail = f"jlaunch delete {jid}"
    await jlaunch.delete(request, jid, _OWNER_CTX)


@router.patch("/jobs/{jid}")
async def jlaunch_note(
    jid: str, body: jlaunch.RunNoteRequest, request: Request, _p: DebugDep
) -> dict[str, Any]:
    """**Set a run's note** (what it was for), as the PWA's run screen does. Returns the
    run as the mirror holds it, `revision` included."""
    jlaunch._valid_id(jid)
    request.state.debug_detail = f"jlaunch note {jid}"
    return await jlaunch.set_note(request, jid, body.note, _OWNER_CTX)


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
