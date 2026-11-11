"""GrowBot personas on the box (docs/plans/GROWBOT_PLAN.md §2c, wave G4a): the GrowBot screen's
Personas, Body and Phones tabs and each persona's page (DESIGN.md "GrowBot — the desk robot's
screen").

- `router` — the owner (`OwnerDep`): list everything the three tabs show in one call; add a
  persona from a seed; rename, duplicate, reset, delete; edit one config region of its soul;
  export and import a soul file; save, list and restore checkpoints; and pick who wears the
  body (B's *Who wears Fish*, for a persona open on any phone).
- `debug_router` — a debug token (`DebugDep`): read everything, save a checkpoint and restore
  one, so a persona can be recovered with no terminal (CLAUDE.md #10).
- `internal_router` — the GrowBot sidecar (`growbot.token`): pull and push the soul of the
  persona a phone was opened as and sync its visual memories, and read a robot's rider, all
  keyed by the phone id the sidecar signed into that phone's session, so a phone reaches only
  its own persona.

Visual memories (wave G4d) name people the camera saw, so they never leave the box: the owner
sees and deletes them here, a checkpoint keeps them, and an export leaves them out.

Every edit, restore, reset and import makes an automatic checkpoint first, so each one can be
undone (a rename changes only the name, so it makes none). Every write bumps the persona's
version, so the phone holding it reloads instead of overwriting it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.api.growbot import PHONE_RE
from jbrain.api.internal_auth import require_growbot
from jbrain.db.session import SessionContext
from jbrain.growbot import memories, souls
from jbrain.growbot.personas import (
    NAME_MAX,
    Checkpoint,
    Face,
    MemoriesDo,
    Memory,
    Persona,
    PersonaRepo,
)
from jbrain.growbot.souls import obj

router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")
internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_CODE = r"^gb-[A-Za-z0-9]{1,32}$"

Pid = Annotated[str, Path(pattern=_UUID)]
Cid = Annotated[str, Path(pattern=_UUID)]
Mid = Annotated[str, Path(pattern="^r[0-9a-f]{1,32}$")]
View = Annotated[int, Path(ge=0, lt=memories.MAX_VIEWS)]
Code = Annotated[str, Path(pattern=_CODE)]
Region = Annotated[str, Path(pattern="^(" + "|".join(souls.REGIONS) + ")$")]

# What the region labels read as in an automatic checkpoint's name (the screen's words).
_REGION_LABEL = {
    "persona": "personality",
    "expression": "expression",
    "meta": "name and description",
    "dream": "dreams",
    "harness": "thinking",
    "body": "body",
}


def _repo(request: Request) -> PersonaRepo:
    return cast(PersonaRepo, request.app.state.growbot_personas)


def _owner_ctx(owner: OwnerDep) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


def _name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise HTTPException(status_code=422, detail="Give it a name.")
    return name[:NAME_MAX]


# -- shapes ---------------------------------------------------------------------------------


class PersonaOut(BaseModel):
    id: str
    name: str
    version: int
    made_from: str
    # The phone holding it now (None: not open) and the last one that did.
    phone: str | None
    last_phone: str | None
    opened_at: datetime | None
    created_at: datetime
    updated_at: datetime
    checkpoints: int
    # The robot it wears, or None.
    wearing: str | None
    # How many visual memories it has.
    memories: int
    # The face its creature wears.
    face: str


class MemoryOut(BaseModel):
    id: str
    kind: str
    name: str
    desc: str
    saved_at: datetime | None
    views: int
    # "human": the owner told it what this is; "agent": it decided itself.
    confirmed: str


class MemoriesOut(BaseModel):
    memories: list[MemoryOut]


class CheckpointOut(BaseModel):
    id: str
    name: str
    auto: bool
    why: str
    created_at: datetime
    # How many visual memories it keeps.
    memories: int


class PersonaDetailOut(PersonaOut):
    soul: dict[str, Any]
    checkpoint_list: list[CheckpointOut]


class PhoneOut(BaseModel):
    id: str
    label: str
    created_at: datetime
    last_seen_at: datetime


class RiderOut(BaseModel):
    code: str
    persona: str | None
    since: datetime


class OverviewOut(BaseModel):
    personas: list[PersonaOut]
    phones: list[PhoneOut]
    body: list[RiderOut]


class WrittenOut(BaseModel):
    persona: PersonaDetailOut
    # The automatic checkpoint made before the write: its id is the toast's Undo.
    checkpoint: CheckpointOut | None


def _persona_out(p: Persona) -> PersonaOut:
    return PersonaOut(
        id=p.id,
        name=p.name,
        version=p.version,
        made_from=p.made_from,
        phone=p.phone_id,
        last_phone=p.last_phone_id,
        opened_at=p.opened_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
        checkpoints=p.checkpoints,
        wearing=p.wearing,
        memories=p.memories,
        face=p.face,
    )


def _cp_out(c: Checkpoint) -> CheckpointOut:
    return CheckpointOut(
        id=c.id, name=c.name, auto=c.auto, why=c.why, created_at=c.created_at, memories=c.memories
    )


def _saved_at(t: object) -> datetime | None:
    # The creature's own clock, in milliseconds; 0 (or nonsense) is "not known".
    if not isinstance(t, int | float) or isinstance(t, bool) or not 0 < t < 4e12:
        return None
    return datetime.fromtimestamp(t / 1000, UTC)


def _memory_out(m: Memory) -> MemoryOut:
    kind = str(m.meta.get("kind") or "object")
    return MemoryOut(
        id=m.id,
        kind="object" if kind == "thing" else kind,
        name=str(m.meta.get("name") or ""),
        desc=str(m.meta.get("desc") or ""),
        saved_at=_saved_at(m.meta.get("t")),
        views=len(m.views),
        confirmed="human" if m.meta.get("confirmed") == "human" else "agent",
    )


async def _memories(request: Request, ctx: SessionContext, pid: str) -> MemoriesOut:
    got = await _repo(request).memories(ctx, pid)
    if got is None:
        raise HTTPException(status_code=404, detail="no such persona")
    return MemoriesOut(memories=[_memory_out(m) for m in got])


def _jpeg(data: bytes | None) -> Response:
    if data is None:
        raise HTTPException(status_code=404, detail="no such view")
    # A face: kept out of every shared cache, and never sniffed as anything but a picture.
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


async def _detail(request: Request, ctx: SessionContext, p: Persona) -> PersonaDetailOut:
    cps = await _repo(request).list_checkpoints(ctx, p.id)
    return PersonaDetailOut(
        **_persona_out(p).model_dump(),
        soul=p.soul or {},
        checkpoint_list=[_cp_out(c) for c in cps],
    )


async def _get(request: Request, ctx: SessionContext, pid: str) -> Persona:
    p = await _repo(request).get_persona(ctx, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="no such persona")
    return p


def _soul_error(exc: souls.SoulError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def _overview(request: Request, ctx: SessionContext) -> OverviewOut:
    repo = _repo(request)
    return OverviewOut(
        personas=[_persona_out(p) for p in await repo.list_personas(ctx)],
        phones=[
            PhoneOut(id=f.id, label=f.label, created_at=f.created_at, last_seen_at=f.last_seen_at)
            for f in await repo.list_phones(ctx)
        ],
        body=[
            RiderOut(code=r.code, persona=r.persona_id, since=r.since)
            for r in await repo.riders(ctx)
        ],
    )


async def _written(
    request: Request,
    ctx: SessionContext,
    pid: str,
    soul: dict[str, Any],
    before: tuple[str, str] | None,
    name: str | None = None,
    expect: int | None = None,
    mems: MemoriesDo = "keep",
) -> WrittenOut:
    done = await _repo(request).write_soul(
        ctx, pid, soul, name=name, before=before, expect=expect, memories=mems
    )
    if done is None:
        raise HTTPException(status_code=404, detail="no such persona")
    if done == "stale":
        raise HTTPException(
            status_code=409,
            detail="It changed on its phone while you were editing. Nothing was saved — try again.",
        )
    persona, cp = done
    return WrittenOut(
        persona=await _detail(request, ctx, persona),
        checkpoint=_cp_out(cp) if cp is not None else None,
    )


async def _restore(request: Request, ctx: SessionContext, pid: str, cid: str) -> WrittenOut:
    cp = await _repo(request).get_checkpoint(ctx, cid)
    if cp is None or cp.persona_id != pid.lower() or cp.soul is None:
        raise HTTPException(status_code=404, detail="no such checkpoint")
    before = (f"Before restoring “{cp.name}”"[:80], "made before a restore")
    return await _written(request, ctx, pid, cp.soul, before, mems=("from", cp.id))


async def _save_cp(request: Request, ctx: SessionContext, pid: str, name: str) -> CheckpointOut:
    label = " ".join(name.split())
    if not label:
        raise HTTPException(status_code=422, detail="Name the checkpoint.")
    cp = await _repo(request).save_checkpoint(ctx, pid, label)
    if cp is None:
        raise HTTPException(status_code=404, detail="no such persona")
    return _cp_out(cp)


# -- the owner ------------------------------------------------------------------------------


class NewPersonaIn(BaseModel):
    name: str = Field(max_length=200)
    # A checkpoint whose personality the new persona starts from; None is a fresh creature.
    seed: str | None = Field(default=None, pattern=_UUID)


class RenameIn(BaseModel):
    # Either or both: a new name, a new face.
    name: str | None = Field(default=None, max_length=200)
    face: Face | None = None


class RegionIn(BaseModel):
    # Text for persona and dream, {name, description} for meta, an object for the rest.
    value: Any


class CheckpointIn(BaseModel):
    name: str = Field(max_length=200)


class ImportIn(BaseModel):
    soul: Any
    # Import as a new persona with this name, or over persona `over` (a checkpoint first).
    name: str | None = Field(default=None, max_length=200)
    over: str | None = Field(default=None, pattern=_UUID)


class RiderIn(BaseModel):
    persona: str | None = Field(default=None, pattern=_UUID)


@router.get("/personas")
async def overview(request: Request, owner: OwnerDep) -> OverviewOut:
    """Every persona, every phone that has opened GrowBot, and who wears each robot."""
    return await _overview(request, _owner_ctx(owner))


@router.post("/personas")
async def new_persona(request: Request, owner: OwnerDep, body: NewPersonaIn) -> PersonaDetailOut:
    ctx, repo = _owner_ctx(owner), _repo(request)
    name = _name(body.name)
    if body.seed is None:
        soul, made_from = souls.fresh(name), "a fresh creature"
    else:
        cp = await repo.get_checkpoint(ctx, body.seed)
        src = await repo.get_persona(ctx, cp.persona_id) if cp is not None else None
        if cp is None or cp.soul is None or src is None:
            raise HTTPException(status_code=404, detail="no such checkpoint")
        made_from = f"{src.name} — {cp.name}"
        soul = souls.seeded(name, cp.soul, f"{src.name} / {cp.name}")
    return await _detail(request, ctx, await repo.create(ctx, name, soul, made_from))


@router.get("/personas/{pid}")
async def persona_detail(request: Request, owner: OwnerDep, pid: Pid) -> PersonaDetailOut:
    ctx = _owner_ctx(owner)
    return await _detail(request, ctx, await _get(request, ctx, pid))


@router.patch("/personas/{pid}")
async def rename_persona(request: Request, owner: OwnerDep, pid: Pid, body: RenameIn) -> WrittenOut:
    """A new name (a soul write, so its phone reloads on it), a new face (not a soul write: the
    phone's poll carries it and the creature changes face where it stands), or both."""
    ctx = _owner_ctx(owner)
    if body.face is not None and not await _repo(request).set_face(ctx, pid, body.face):
        raise HTTPException(status_code=404, detail="no such persona")
    p = await _get(request, ctx, pid)
    if body.name is None:
        return WrittenOut(persona=await _detail(request, ctx, p), checkpoint=None)
    name = _name(body.name)
    return await _written(
        request, ctx, pid, souls.renamed(p.soul or {}, name), None, name, p.version
    )


@router.put("/personas/{pid}/regions/{region}")
async def edit_region(
    request: Request, owner: OwnerDep, pid: Pid, region: Region, body: RegionIn
) -> WrittenOut:
    """Replace one config region; the soul as it was is checkpointed first."""
    ctx = _owner_ctx(owner)
    p = await _get(request, ctx, pid)
    try:
        soul = souls.edit_region(p.soul or {}, region, body.value)
    except souls.SoulError as exc:
        raise _soul_error(exc) from exc
    name = None
    if region == "meta":
        name = _name(str(soul["meta"]["name"]))
    before = (f"Before editing {_REGION_LABEL[region]}", "made before an edit")
    return await _written(request, ctx, pid, soul, before, name, p.version)


@router.post("/personas/{pid}/duplicate")
async def duplicate_persona(request: Request, owner: OwnerDep, pid: Pid) -> PersonaDetailOut:
    """A copy of the whole creature, memories and visual memories, without its checkpoints."""
    ctx, repo = _owner_ctx(owner), _repo(request)
    p = await _get(request, ctx, pid)
    name = f"{p.name} copy"[:NAME_MAX]
    made = await repo.create(
        ctx,
        name,
        souls.renamed(p.soul or {}, name),
        f"copy of {p.name}",
        memories_from=p.id,
        face=cast(Face, p.face),
    )
    return await _detail(request, ctx, made)


@router.post("/personas/{pid}/reset")
async def reset_persona(request: Request, owner: OwnerDep, pid: Pid) -> WrittenOut:
    """A fresh creature, which has met nobody: its visual memories go too (upstream forgets
    them when its soul is replaced), kept by the checkpoint saved first."""
    ctx = _owner_ctx(owner)
    p = await _get(request, ctx, pid)
    return await _written(
        request,
        ctx,
        pid,
        souls.fresh(p.name),
        ("Before reset", "made before a reset"),
        mems="clear",
    )


@router.delete("/personas/{pid}", status_code=204)
async def delete_persona(request: Request, owner: OwnerDep, pid: Pid) -> Response:
    """Erases the persona, its memories and its checkpoints; a body it wore stands still."""
    if not await _repo(request).delete(_owner_ctx(owner), pid):
        raise HTTPException(status_code=404, detail="no such persona")
    return Response(status_code=204)


@router.get("/personas/{pid}/export")
async def export_persona(request: Request, owner: OwnerDep, pid: Pid) -> Response:
    """The soul file, in upstream's snapshot shape. Visual memories stay on the box."""
    p = await _get(request, _owner_ctx(owner), pid)
    doc = souls.for_export(p.soul or {}, p.name)
    # ASCII only: the header is Latin-1, and a name in another script must still download.
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in p.name.lower())
    slug = "-".join(part for part in slug.split("-") if part) or "persona"
    return Response(
        content=json.dumps(doc, ensure_ascii=False, indent=1).encode(),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{slug[:40]}-soul.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/personas/import")
async def import_persona(request: Request, owner: OwnerDep, body: ImportIn) -> WrittenOut:
    """A soul file, checked before anything changes: as a new persona, or over one."""
    ctx, repo = _owner_ctx(owner), _repo(request)
    try:
        soul = souls.from_file(body.soul)
    except souls.SoulError as exc:
        raise _soul_error(exc) from exc
    if body.over is not None:
        await _get(request, ctx, body.over)
        # Another creature's soul has not met the people this one saved.
        return await _written(
            request,
            ctx,
            body.over,
            soul,
            ("Before import", "made before an import"),
            mems="clear",
        )
    meta = obj(soul.get("meta"))
    name = _name(body.name or str(meta.get("name") or "") or "Imported")
    made = await repo.create(ctx, name, souls.renamed(soul, name), "a soul file")
    return WrittenOut(persona=await _detail(request, ctx, made), checkpoint=None)


@router.post("/personas/{pid}/checkpoints")
async def save_checkpoint(
    request: Request, owner: OwnerDep, pid: Pid, body: CheckpointIn
) -> CheckpointOut:
    return await _save_cp(request, _owner_ctx(owner), pid, body.name)


@router.post("/personas/{pid}/checkpoints/{cid}/restore")
async def restore_checkpoint(request: Request, owner: OwnerDep, pid: Pid, cid: Cid) -> WrittenOut:
    """The checkpoint becomes the persona; the soul as it was is checkpointed first."""
    return await _restore(request, _owner_ctx(owner), pid, cid)


@router.get("/personas/{pid}/memories")
async def list_memories(request: Request, owner: OwnerDep, pid: Pid) -> MemoriesOut:
    """Its visual memories, newest first (the persona page's Memories tab)."""
    return await _memories(request, _owner_ctx(owner), pid)


@router.get("/personas/{pid}/memories/{mid}/views/{i}")
async def memory_view(request: Request, owner: OwnerDep, pid: Pid, mid: Mid, i: View) -> Response:
    return _jpeg(await _repo(request).memory_view(_owner_ctx(owner), pid, mid, i))


@router.delete("/personas/{pid}/memories/{mid}", status_code=204)
async def forget_memory(request: Request, owner: OwnerDep, pid: Pid, mid: Mid) -> Response:
    """It forgets that face or thing; the phone holding it forgets it at its next sync. A
    checkpoint made before still has it."""
    if not await _repo(request).forget_memory(_owner_ctx(owner), pid, mid):
        raise HTTPException(status_code=404, detail="no such memory")
    return Response(status_code=204)


@router.put("/body/{code}")
async def set_rider(request: Request, owner: OwnerDep, code: Code, body: RiderIn) -> OverviewOut:
    """Who wears robot `code` (B's *Who wears Fish*). A persona open on a phone takes the legs
    there at that phone's next check; the one it replaces carries on alone. The owner's session
    only: a phone takes the body solely by being opened wearing it."""
    ctx = _owner_ctx(owner)
    if not await _repo(request).set_rider(ctx, code, body.persona):
        raise HTTPException(status_code=404, detail="no such robot or persona")
    return await _overview(request, ctx)


# -- the debug console ----------------------------------------------------------------------


@debug_router.get("/personas")
async def debug_overview(request: Request, _p: DebugDep) -> OverviewOut:
    request.state.debug_detail = "growbot personas"
    return await _overview(request, _DEBUG_OWNER)


@debug_router.get("/personas/{pid}")
async def debug_persona(request: Request, _p: DebugDep, pid: Pid) -> PersonaDetailOut:
    request.state.debug_detail = f"growbot persona {pid}"
    return await _detail(request, _DEBUG_OWNER, await _get(request, _DEBUG_OWNER, pid))


@debug_router.get("/personas/{pid}/memories")
async def debug_memories(request: Request, _p: DebugDep, pid: Pid) -> MemoriesOut:
    # What it has saved, never the pictures: the console is for recovery, not for looking.
    request.state.debug_detail = f"growbot memories {pid}"
    return await _memories(request, _DEBUG_OWNER, pid)


@debug_router.post("/personas/{pid}/checkpoints")
async def debug_save_checkpoint(
    request: Request, _p: DebugDep, pid: Pid, body: CheckpointIn
) -> CheckpointOut:
    request.state.debug_detail = f"growbot checkpoint {pid}"
    return await _save_cp(request, _DEBUG_OWNER, pid, body.name)


@debug_router.post("/personas/{pid}/checkpoints/{cid}/restore")
async def debug_restore(request: Request, _p: DebugDep, pid: Pid, cid: Cid) -> WrittenOut:
    request.state.debug_detail = f"growbot restore {pid} {cid}"
    return await _restore(request, _DEBUG_OWNER, pid, cid)


# -- the sidecar ----------------------------------------------------------------------------

# Lenient bodies, as api/growbot.py's: any JSON object short of a right answer is refused with
# a status the sidecar can act on, never a 422 it would have to tell apart.


class PhoneIn(BaseModel):
    persona: str = ""
    phone: str = ""


class PushIn(PhoneIn):
    base: int = 0
    soul: Any = None


class PulledOut(BaseModel):
    version: int
    soul: dict[str, Any]
    # The robot this persona wears now, or None: the phone drives the legs only while it is.
    wearing: str | None
    face: str


class PushedOut(BaseModel):
    version: int


class BodyOut(BaseModel):
    persona: str | None
    phone: str | None


def _away(why: str) -> HTTPException:
    """409 `moved` (another phone holds it: the creature shows that it has moved) or 404
    `gone` (deleted: the creature shows that)."""
    return HTTPException(status_code=404 if why == "gone" else 409, detail=why)


def _phone_ok(body: PhoneIn) -> bool:
    return bool(PHONE_RE.fullmatch(body.phone)) and len(body.persona) <= 64


@internal_router.post("/soul/pull")
async def pull_soul(request: Request, body: PhoneIn) -> PulledOut:
    """The soul of the persona this phone holds. 409 `moved` when another phone has it now,
    404 `gone` when it was deleted, 401 for a body that names no phone."""
    if not _phone_ok(body):
        raise HTTPException(status_code=401, detail="unauthorized")
    got = await _repo(request).pull(_SYSTEM_OWNER, body.persona, body.phone)
    if isinstance(got, str):
        raise _away(got)
    return PulledOut(version=got.version, soul=got.soul, wearing=got.wearing, face=got.face)


class StateOut(BaseModel):
    version: int
    wearing: str | None
    # Bumped by every change to its visual memories: the phone syncs when it moves.
    memories: int
    # The face it wears: the creature changes to it where it stands.
    face: str


@internal_router.post("/soul/state")
async def soul_state(request: Request, body: PhoneIn) -> StateOut:
    """The phone's poll, without the soul: its version (a newer one means the owner changed it;
    pull and reload) and the robot it wears. 409 `moved` and 404 `gone` as the pull."""
    if not _phone_ok(body):
        raise HTTPException(status_code=401, detail="unauthorized")
    got = await _repo(request).pull(_SYSTEM_OWNER, body.persona, body.phone, with_soul=False)
    if isinstance(got, str):
        raise _away(got)
    return StateOut(version=got.version, wearing=got.wearing, memories=got.memories, face=got.face)


@internal_router.post("/soul/push")
async def push_soul(request: Request, body: PushIn) -> PushedOut:
    """The creature's own write. 409 `moved` (another phone holds it), 409 `newer` (the owner
    changed it; pull and reload), 404 `gone` (deleted), 413 over the soul cap, 400 for a body
    that is not a soul."""
    if not _phone_ok(body):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        soul = souls.from_phone(body.soul)
    except souls.SoulError as exc:
        status = 413 if isinstance(exc, souls.SoulTooBig) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    result = await _repo(request).push(_SYSTEM_OWNER, body.persona, body.phone, body.base, soul)
    if result == "gone":
        raise HTTPException(status_code=404, detail="gone")
    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)
    return PushedOut(version=result)


@internal_router.get("/body/{code}")
async def body_rider(request: Request, code: Code) -> BodyOut:
    """Who wears robot `code` and from which phone: the relay lets only that phone drive it."""
    repo = _repo(request)
    for rider in await repo.riders(_SYSTEM_OWNER):
        if rider.code == code:
            if rider.persona_id is None:
                return BodyOut(persona=None, phone=None)
            p = await repo.get_persona(_SYSTEM_OWNER, rider.persona_id)
            return BodyOut(persona=rider.persona_id, phone=p.phone_id if p else None)
    return BodyOut(persona=None, phone=None)


class MemoryViewIn(PhoneIn):
    id: str = ""
    i: int = 0


class KeepIn(PhoneIn):
    memory: Any = None


class ForgetIn(PhoneIn):
    ids: Any = None


class PhoneMemoryOut(BaseModel):
    # The creature's own record without its pictures, field for field as it wrote it.
    id: str
    kind: Any
    name: Any
    desc: Any
    t: Any
    confirmed: Any
    namedAt: Any  # noqa: N815 — the creature's own field name
    # [{"t": when}], one per view; each picture is fetched on its own.
    views: list[dict[str, Any]]


class PhoneMemoriesOut(BaseModel):
    version: int
    memories: list[PhoneMemoryOut]


class MemoriesVersionOut(BaseModel):
    version: int


def _for_phone(m: Memory) -> PhoneMemoryOut:
    return PhoneMemoryOut(
        id=m.id,
        kind=m.meta.get("kind"),
        name=m.meta.get("name"),
        desc=m.meta.get("desc"),
        t=m.meta.get("t"),
        confirmed=m.meta.get("confirmed"),
        namedAt=m.meta.get("namedAt"),
        views=[{"t": v.get("t")} for v in m.views],
    )


@internal_router.post("/memories/list")
async def phone_memories(request: Request, body: PhoneIn) -> PhoneMemoriesOut:
    """The visual memories the box holds for the persona this phone holds, to compare with
    what the phone holds. 409 `moved` and 404 `gone` as the soul's pull."""
    if not _phone_ok(body):
        raise HTTPException(status_code=401, detail="unauthorized")
    got = await _repo(request).phone_memories(_SYSTEM_OWNER, body.persona, body.phone)
    if isinstance(got, str):
        raise _away(got)
    return PhoneMemoriesOut(version=got[0], memories=[_for_phone(m) for m in got[1]])


@internal_router.post("/memories/view")
async def phone_memory_view(request: Request, body: MemoryViewIn) -> Response:
    """One saved view's JPEG, for a phone that lacks it. 404 `gone` when the persona was
    deleted, 404 when the view is not there."""
    if not _phone_ok(body) or not memories.ID_RE.fullmatch(body.id):
        raise HTTPException(status_code=401, detail="unauthorized")
    got = await _repo(request).phone_view(_SYSTEM_OWNER, body.persona, body.phone, body.id, body.i)
    if isinstance(got, str):
        raise _away(got)
    return _jpeg(got)


@internal_router.post("/memories/keep")
async def phone_keep(request: Request, body: KeepIn) -> MemoriesVersionOut:
    """A record the creature saved, renamed or gave another view, kept whole. 400 for one the
    box will not keep (the phone stops sending it), 413 when the persona already has
    `MAX_MEMORIES`; 409 `moved` and 404 `gone` as the soul's push."""
    if not _phone_ok(body):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        saved = memories.from_phone(body.memory)
    except memories.MemoryRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    got = await _repo(request).phone_keep(_SYSTEM_OWNER, body.persona, body.phone, saved)
    if got == "full":
        raise HTTPException(status_code=413, detail="full")
    if isinstance(got, str):
        raise _away(got)
    return MemoriesVersionOut(version=got)


@internal_router.post("/memories/forget")
async def phone_forget(request: Request, body: ForgetIn) -> MemoriesVersionOut:
    """Records the creature forgot (one, or all of them), gone from the box too."""
    if not _phone_ok(body):
        raise HTTPException(status_code=401, detail="unauthorized")
    ids = body.ids
    if (
        not isinstance(ids, list)
        or len(ids) > memories.MAX_MEMORIES
        or not all(isinstance(i, str) and memories.ID_RE.fullmatch(i) for i in ids)
    ):
        raise HTTPException(status_code=400, detail="not visual memory ids")
    got = await _repo(request).phone_forget(_SYSTEM_OWNER, body.persona, body.phone, ids)
    if isinstance(got, str):
        raise _away(got)
    return MemoriesVersionOut(version=got)
