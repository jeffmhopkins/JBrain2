"""GrowBot's policy library: walks the Jobs launcher trained, driven from the Body tab's remote.

The owner trains walking policies on the box (the Jobs launcher's GrowBot jobs) and asked
(2026-11-14) to drive them from the Body tab, pulled straight from the finished jobs. A job
leaves `policy_final.json` in its live folder (GrowBot_Training's export); adding it copies the
file into the library (`jbrain.growbot.policies`), so it outlives the job.

- `router` — the owner: the library (list, add from a job, rename, delete) and the finished
  jobs it can add from.
- `debug_router` — the same reads and the add, through the debug token (CLAUDE.md #10).
- `internal_router` — the sidecar (`growbot.token`): one policy's JSON, which the creature
  page loads when the Body tab drives it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field, field_validator

from jbrain.api import jlaunch
from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.api.internal_auth import require_growbot
from jbrain.db.session import SessionContext
from jbrain.growbot.policies import PolicyError, PolicyRepo, PolicyRow, parse
from jbrain.jlaunch import JlaunchError

router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")
internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")

# The file GrowBot_Training's export leaves in a finished job's live folder; the mid-run
# `policy_latest.json` is a snapshot, not a result (training thread, 2026-11-14).
FINAL = "policy_final.json"
# The Jobs specs that train a GrowBot walk (jlaunch_ctl/specs.py).
_SPEC_PREFIX = "growbot_"
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
PolicyId = Annotated[str, Path(pattern=_UUID)]
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_name(v: str) -> str:
    v = " ".join(_CONTROL.sub(" ", v).split())
    if not v:
        raise ValueError("a name is needed")
    return v


class ContractOut(BaseModel):
    facing: str
    speed: bool
    pitch: bool
    control_hz: int


class PolicyOut(BaseModel):
    id: str
    name: str
    source_job: str
    source_spec: str
    bytes: int
    created_at: datetime
    contract: ContractOut


class AddIn(BaseModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=60)

    _name = field_validator("name")(_clean_name)


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)

    _name = field_validator("name")(_clean_name)


class SourceOut(BaseModel):
    """A finished GrowBot training job whose policy can be added."""

    job_id: str
    spec: str
    title: str
    finished_at: str | None
    # Already in the library (by the job it came from): adding again makes a second copy.
    added: bool


def _repo(request: Request) -> PolicyRepo:
    return cast(PolicyRepo, request.app.state.growbot_policies)


def _owner_ctx(owner: OwnerDep) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


def _out(row: PolicyRow) -> PolicyOut:
    c = row.contract
    return PolicyOut(
        id=row.id,
        name=row.name,
        source_job=row.source_job,
        source_spec=row.source_spec,
        bytes=row.bytes,
        created_at=row.created_at,
        contract=ContractOut(
            facing=c.facing, speed=c.speed, pitch=c.pitch, control_hz=c.control_hz
        ),
    )


async def _list(request: Request, ctx: SessionContext) -> list[PolicyOut]:
    return [_out(r) for r in await _repo(request).list(ctx)]


async def _sources(request: Request, ctx: SessionContext) -> list[SourceOut]:
    try:
        jobs = await jlaunch._client(request).list_jobs()
    except JlaunchError:
        # The launcher being off is no error for the Body tab: there is just nothing to add.
        return []
    added = {r.source_job for r in await _repo(request).list(ctx)}
    out: list[SourceOut] = []
    for job in sorted(jobs, key=lambda j: str(j.get("finished_at") or ""), reverse=True):
        jid, spec = str(job.get("id") or ""), str(job.get("spec_name") or "")
        if not spec.startswith(_SPEC_PREFIX) or job.get("state") != "succeeded":
            continue
        try:
            files = await jlaunch._client(request).list_live(jid)
        except JlaunchError:
            continue
        if not any(f.get("name") == FINAL for f in files):
            continue
        out.append(
            SourceOut(
                job_id=jid,
                spec=spec,
                title=str(job.get("title") or spec),
                finished_at=str(job["finished_at"]) if job.get("finished_at") else None,
                added=jid in added,
            )
        )
    return out


async def _add(request: Request, ctx: SessionContext, body: AddIn) -> PolicyOut:
    client = jlaunch._client(request)
    try:
        job = await client.get_job(body.job_id)
    except JlaunchError as exc:
        raise jlaunch._upstream_404(exc, "no such job") from exc
    spec = str(job.get("spec_name") or "")
    if not spec.startswith(_SPEC_PREFIX) or job.get("state") != "succeeded":
        raise HTTPException(status_code=409, detail="that job hasn't finished training a walk")
    try:
        raw, _media = await client.get_live(body.job_id, FINAL)
    except JlaunchError as exc:
        raise jlaunch._upstream_404(exc, "that job left no finished policy") from exc
    try:
        policy, _contract = parse(raw)
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = await _repo(request).add(ctx, body.name, policy, len(raw), body.job_id, spec)
    return _out(row)


@router.get("/policies")
async def list_policies(request: Request, owner: OwnerDep) -> list[PolicyOut]:
    return await _list(request, _owner_ctx(owner))


@router.get("/policies/sources")
async def list_sources(request: Request, owner: OwnerDep) -> list[SourceOut]:
    """Finished GrowBot training jobs that left a policy, newest first."""
    return await _sources(request, _owner_ctx(owner))


@router.post("/policies", status_code=201)
async def add_policy(request: Request, owner: OwnerDep, body: AddIn) -> PolicyOut:
    """Copy a finished job's policy into the library. 409 when the job is not a finished
    GrowBot walk, 422 when its file isn't a policy the pad can steer."""
    return await _add(request, _owner_ctx(owner), body)


@router.patch("/policies/{pid}")
async def rename_policy(
    request: Request, owner: OwnerDep, pid: PolicyId, body: RenameIn
) -> PolicyOut:
    row = await _repo(request).rename(_owner_ctx(owner), pid, body.name)
    if row is None:
        raise HTTPException(status_code=404, detail="no such policy")
    return _out(row)


@router.delete("/policies/{pid}", status_code=204)
async def delete_policy(request: Request, owner: OwnerDep, pid: PolicyId) -> None:
    if not await _repo(request).delete(_owner_ctx(owner), pid):
        raise HTTPException(status_code=404, detail="no such policy")


@debug_router.get("/policies")
async def debug_list_policies(request: Request, _p: DebugDep) -> list[PolicyOut]:
    request.state.debug_detail = "growbot policies"
    return await _list(request, _DEBUG_OWNER)


@debug_router.get("/policies/sources")
async def debug_list_sources(request: Request, _p: DebugDep) -> list[SourceOut]:
    request.state.debug_detail = "growbot policy sources"
    return await _sources(request, _DEBUG_OWNER)


@debug_router.post("/policies", status_code=201)
async def debug_add_policy(request: Request, _p: DebugDep, body: AddIn) -> PolicyOut:
    request.state.debug_detail = f"growbot add policy from {body.job_id}"
    return await _add(request, _DEBUG_OWNER, body)


@internal_router.get("/policies/{pid}")
async def internal_policy(request: Request, pid: PolicyId) -> dict[str, Any]:
    """The policy JSON the creature's runner loads."""
    got = await _repo(request).get_json(_SYSTEM_OWNER, pid)
    if got is None:
        raise HTTPException(status_code=404, detail="no such policy")
    return got
