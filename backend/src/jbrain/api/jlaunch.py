"""Owner-facing REST proxy to the jlaunch control server + the run mirror and share mint.

Every route is owner-gated (`OwnerDep`) and 404s when the launcher isn't configured
(`app.state.jlaunch_client is None`) — the same fail-closed graceful degrade as jcode. The
api proxies the control surface (specs / start / stop / kill / delete), reconciles the live
job state into the durable `jlaunch_runs` mirror (which survives a control-server restart),
and mints/lists/revokes the public results-share links. The live terminal is a separate
WebSocket router (`jlaunch_terminal.py`); the public results page is `jlaunch_share.py`.
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime symbol
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep
from jbrain.db.session import SessionContext, scoped_session
from jbrain.external import jlaunch_shares
from jbrain.jlaunch import JlaunchApi, JlaunchError
from jbrain.models.jlaunch import JlaunchRunRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.storage import BlobStore

router = APIRouter()

_REPO = JlaunchRunRepo()

# Job ids are the control server's hex tokens; mirror its gate so a malformed id can never
# be interpolated into a proxied URL.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StartJobRequest(BaseModel):
    spec: str = Field(min_length=1, max_length=128)


class MintShareRequest(BaseModel):
    label: str = Field(default="shared results", min_length=1, max_length=128)


class MintShareOut(BaseModel):
    id: str
    label: str | None
    # The bearer secret — shown EXACTLY once, never recoverable from the list.
    token: str


class ShareOut(BaseModel):
    id: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int


def _client(request: Request) -> JlaunchApi:
    client = getattr(request.app.state, "jlaunch_client", None)
    if client is None:
        raise HTTPException(status_code=404, detail="the job launcher is not enabled")
    return cast(JlaunchApi, client)


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


def _blobs(request: Request) -> BlobStore:
    return cast("BlobStore", request.app.state.blob_store)


def _owner_ctx(principal_id: str) -> SessionContext:
    return SessionContext(principal_id=principal_id, principal_kind="owner")


def _valid_id(jid: str) -> None:
    if not _ID_RE.match(jid):
        raise HTTPException(status_code=404, detail="unknown job")


async def _live_jobs(request: Request) -> dict[str, dict[str, Any]]:
    """The control server's live jobs keyed by id, or {} when it's off/unreachable — the
    reconcile source of truth for state/phase/snapshots."""
    try:
        jobs = await _client(request).list_jobs()
    except JlaunchError:
        return {}
    return {job["id"]: job for job in jobs}


@router.get("/jlaunch/specs")
async def list_specs(_owner: OwnerDep, request: Request) -> list[dict[str, Any]]:
    try:
        return await _client(request).list_specs()
    except JlaunchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/jlaunch/jobs", status_code=201)
async def start_job(body: StartJobRequest, owner: OwnerDep, request: Request) -> dict[str, Any]:
    client = _client(request)
    try:
        job = await client.start_job(body.spec)
    except JlaunchError as exc:
        if exc.status == 409:
            raise HTTPException(status_code=409, detail="a job is already running") from exc
        if exc.status == 404:
            raise HTTPException(status_code=404, detail="unknown job spec") from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.upsert(db, job)
    return job


@router.get("/jlaunch/jobs")
async def list_jobs(owner: OwnerDep, request: Request) -> list[dict[str, Any]]:
    live = await _live_jobs(request)
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        for job in live.values():
            await _REPO.upsert(db, job)
        rows = await _REPO.list(db)
    return [row.__dict__ for row in rows]


@router.get("/jlaunch/jobs/{jid}")
async def get_job(jid: str, owner: OwnerDep, request: Request) -> dict[str, Any]:
    _valid_id(jid)
    live: dict[str, Any] | None = None
    try:
        live = await _client(request).get_job(jid)
    except JlaunchError:
        live = None
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        if live is not None:
            await _REPO.upsert(db, live)
        row = await _REPO.get(db, jid)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return row.__dict__


@router.post("/jlaunch/jobs/{jid}/stop")
async def stop_job(jid: str, owner: OwnerDep, request: Request) -> dict[str, Any]:
    return await _control_then_mirror(request, owner, jid, "stop")


@router.post("/jlaunch/jobs/{jid}/kill")
async def kill_job(jid: str, owner: OwnerDep, request: Request) -> dict[str, Any]:
    return await _control_then_mirror(request, owner, jid, "kill")


async def _control_then_mirror(
    request: Request, owner: OwnerDep, jid: str, action: str
) -> dict[str, Any]:
    _valid_id(jid)
    client = _client(request)
    try:
        job = await (client.stop(jid) if action == "stop" else client.kill(jid))
    except JlaunchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.upsert(db, job)
    return job


@router.delete("/jlaunch/jobs/{jid}", status_code=204)
async def delete_job(jid: str, owner: OwnerDep, request: Request) -> None:
    _valid_id(jid)
    try:
        await _client(request).delete(jid)  # idempotent (swallows a 404)
    except JlaunchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Drop the mirror row too; the FK cascade removes any share links with it.
    async with scoped_session(_maker(request), _owner_ctx(owner.id)) as db:
        await _REPO.delete(db, jid)


@router.post("/jlaunch/jobs/{jid}/share", status_code=201)
async def mint_share(
    jid: str, body: MintShareRequest, owner: OwnerDep, request: Request
) -> MintShareOut:
    """Mint a public results-share link for a FINISHED, artifact-bearing job: stream the
    artifact into the content-addressed blob store, snapshot the run + artifact pointer, then
    mint the token. 400 if the job hasn't succeeded with an artifact yet."""
    _valid_id(jid)
    client = _client(request)
    try:
        job = await client.get_job(jid)
    except JlaunchError as exc:
        raise HTTPException(status_code=404, detail="unknown job") from exc
    if job.get("state") != "succeeded" or not job.get("artifact_present"):
        raise HTTPException(
            status_code=400, detail="job has not finished successfully with an artifact"
        )
    try:
        sha256 = await _blobs(request).put_stream(client.stream_artifact(jid))
    except JlaunchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    maker = _maker(request)
    ctx = _owner_ctx(owner.id)
    async with scoped_session(maker, ctx) as db:
        await _REPO.upsert(db, job)
        await _REPO.set_artifact(
            db,
            jid,
            sha256=sha256,
            size=int(job.get("artifact_bytes") or 0),
            name=str(job.get("artifact_name") or "artifact"),
        )
    secret, link = await jlaunch_shares.mint_share(maker, ctx, run_id=jid, label=body.label)
    return MintShareOut(id=link.id, label=link.label, token=secret)


@router.get("/jlaunch/jobs/{jid}/shares")
async def list_shares(jid: str, owner: OwnerDep, request: Request) -> list[ShareOut]:
    _valid_id(jid)
    shares = await jlaunch_shares.list_shares(_maker(request), _owner_ctx(owner.id), jid)
    return [
        ShareOut(
            id=s.id,
            label=s.label,
            created_at=s.created_at,
            revoked_at=s.revoked_at,
            last_viewed_at=s.last_viewed_at,
            view_count=s.view_count,
        )
        for s in shares
    ]


@router.delete("/jlaunch/jobs/{jid}/shares/{share_id}", status_code=204)
async def revoke_share(jid: str, share_id: str, owner: OwnerDep, request: Request) -> None:
    _valid_id(jid)
    if not await jlaunch_shares.revoke_share(_maker(request), _owner_ctx(owner.id), share_id):
        raise HTTPException(status_code=404, detail="unknown share link")
