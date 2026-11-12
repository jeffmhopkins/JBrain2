"""Personas on the box: their souls, checkpoints, the phones that hold them and the body's rider
(migration 0257, docs/plans/GROWBOT_PLAN.md §2c, wave G4a).

Every soul write bumps `version`. The phone pushes against the version it last pulled, so a
soul the owner changed from the PWA (an edit, a restore, a reset, an import) is never
overwritten by a phone's older cache: that push is refused as `newer`, and the phone reloads.
A push from a phone that no longer holds the persona is refused as `moved`.

The hand-over rules live in `open_on_phone` and `set_rider`, each one statement batch in one
transaction: a phone holds one persona, a persona is on one phone, the body has one rider.

Visual memories (wave G4d) sync per record, not per version: the phone compares what it holds
with what the box holds and sends what differs. `memories_version` is only the phone's signal
that the owner changed them. Their pictures are blobs in the memories' own namespace; a
checkpoint names the same digests, so a blob is deleted (`_sweep`) only once neither table
does, under one lock that a phone's write holds from its first blob to its commit.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.growbot.memories import MAX_MEMORIES, Saved
from jbrain.growbot.prefs import OWNER_ONLY, Patch, for_phone, merged
from jbrain.storage import BlobStore

NAME_MAX = 40
CHECKPOINT_NAME_MAX = 80
# Automatic checkpoints (before an edit, restore, reset, import) are pruned past this many per
# persona, oldest first; the ones the owner named are kept until the persona goes.
AUTO_KEEP = 20
PHONE_LABEL_MAX = 40
_SYSTEM_OWNER = SessionContext(principal_kind="owner")
# Below this much free space the box keeps no new picture.
FREE_FLOOR_BYTES = 1 << 30
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    version: int
    made_from: str
    phone_id: str | None
    last_phone_id: str | None
    opened_at: datetime | None
    created_at: datetime
    updated_at: datetime
    checkpoints: int
    wearing: str | None
    soul: dict[str, Any] | None = None
    memories: int = 0
    face: str = "lantern"
    voice: str = ""
    # The creature's settings (growbot/prefs.py); None until its phone first sent them.
    prefs: dict[str, str] | None = None
    explore: bool = True


@dataclass(frozen=True)
class Checkpoint:
    id: str
    persona_id: str
    name: str
    auto: bool
    why: str
    created_at: datetime
    soul: dict[str, Any] | None = None
    memories: int = 0


@dataclass(frozen=True)
class Memory:
    id: str
    # The creature's own fields, exactly as it wrote them: kind, name, desc, t, confirmed,
    # namedAt.
    meta: dict[str, Any]
    # [{"sha": digest, "t": when}], one per saved view.
    views: list[dict[str, Any]]


@dataclass(frozen=True)
class Phone:
    id: str
    label: str
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class Rider:
    code: str
    persona_id: str | None
    since: datetime


@dataclass(frozen=True)
class Pulled:
    version: int
    soul: dict[str, Any]
    wearing: str | None
    memories: int = 0
    face: str = "lantern"
    voice: str = ""
    # Bumped by every change to its settings: the phone pulls them when it moves.
    prefs: int = 0
    explore: bool = True


PushResult = int | Literal["moved", "newer", "gone"]
# The faces growbot-face.js draws (migration 0264's CHECK); lantern is the default.
Face = Literal["lantern", "ostrich", "robot", "ember", "pixel"]
# A tts-stt voice id (migration 0265's CHECK, and the shape the sidecar's /api/fish-tts
# accepts), or "" for the box's default voice.
VOICE_RE: Final = r"^(kokoro-[a-z0-9_]{1,40})?$"
Away = Literal["moved", "gone"]
# How a restore, reset or import treats the memories: keep them, clear them, or put back the
# ones checkpoint `str` kept.
MemoriesDo = Literal["keep", "clear"] | tuple[Literal["from"], str]


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


class PersonaRepo(Protocol):
    async def list_personas(self, ctx: SessionContext) -> list[Persona]: ...
    async def get_persona(self, ctx: SessionContext, pid: str) -> Persona | None: ...
    async def create(
        self,
        ctx: SessionContext,
        name: str,
        soul: dict[str, Any],
        made_from: str,
        memories_from: str | None = None,
        face: Face = "lantern",
        voice: str = "",
        explore: bool = True,
    ) -> Persona: ...
    async def set_face(self, ctx: SessionContext, pid: str, face: Face) -> bool: ...
    async def set_voice(self, ctx: SessionContext, pid: str, voice: str) -> bool: ...
    async def set_explore(self, ctx: SessionContext, pid: str, explore: bool) -> bool: ...
    async def write_soul(
        self,
        ctx: SessionContext,
        pid: str,
        soul: dict[str, Any],
        *,
        name: str | None = None,
        before: tuple[str, str] | None = None,
        expect: int | None = None,
        memories: MemoriesDo = "keep",
    ) -> tuple[Persona, Checkpoint | None] | Literal["stale"] | None: ...
    async def delete(self, ctx: SessionContext, pid: str) -> bool: ...
    async def list_checkpoints(self, ctx: SessionContext, pid: str) -> list[Checkpoint]: ...
    async def get_checkpoint(self, ctx: SessionContext, cid: str) -> Checkpoint | None: ...
    async def save_checkpoint(
        self, ctx: SessionContext, pid: str, name: str, auto: bool = False, why: str = ""
    ) -> Checkpoint | None: ...
    async def delete_checkpoint(self, ctx: SessionContext, pid: str, cid: str) -> bool: ...
    async def delete_automatic(self, ctx: SessionContext, pid: str) -> int | None: ...
    async def list_phones(self, ctx: SessionContext) -> list[Phone]: ...
    async def riders(self, ctx: SessionContext) -> list[Rider]: ...
    async def set_rider(self, ctx: SessionContext, code: str, pid: str | None) -> bool: ...
    async def open_on_phone(
        self, ctx: SessionContext, pid: str, phone_id: str, label: str, code: str
    ) -> bool: ...
    async def pull(
        self, ctx: SessionContext, pid: str, phone_id: str, with_soul: bool = True
    ) -> Pulled | Literal["moved", "gone"]: ...
    async def push(
        self, ctx: SessionContext, pid: str, phone_id: str, base: int, soul: dict[str, Any]
    ) -> PushResult: ...
    async def memories(self, ctx: SessionContext, pid: str) -> list[Memory] | None: ...
    async def memory_view(
        self, ctx: SessionContext, pid: str, mid: str, i: int
    ) -> bytes | None: ...
    async def forget_memory(self, ctx: SessionContext, pid: str, mid: str) -> bool: ...
    async def phone_memories(
        self, ctx: SessionContext, pid: str, phone_id: str
    ) -> tuple[int, list[Memory]] | Away: ...
    async def phone_view(
        self, ctx: SessionContext, pid: str, phone_id: str, mid: str, i: int
    ) -> bytes | None | Away: ...
    async def phone_keep(
        self, ctx: SessionContext, pid: str, phone_id: str, saved: Saved
    ) -> int | Away | Literal["full"]: ...
    async def phone_forget(
        self, ctx: SessionContext, pid: str, phone_id: str, ids: list[str]
    ) -> int | Away: ...
    async def patch_prefs(self, ctx: SessionContext, pid: str, patch: Patch) -> int | None: ...
    async def phone_prefs(
        self, ctx: SessionContext, pid: str, phone_id: str
    ) -> tuple[int, dict[str, str] | None, bool] | Away: ...
    async def phone_patch_prefs(
        self, ctx: SessionContext, pid: str, phone_id: str, patch: Patch
    ) -> int | Away: ...


_PERSONA = (
    "p.id::text AS id, p.name, p.version, p.made_from, p.phone_id, p.last_phone_id,"
    " p.opened_at, p.created_at, p.updated_at, p.face, p.voice, p.prefs, p.explore,"
    " (SELECT count(*) FROM app.growbot_checkpoints c WHERE c.persona_id = p.id) AS checkpoints,"
    " (SELECT b.code FROM app.growbot_body b WHERE b.persona_id = p.id LIMIT 1) AS wearing,"
    " (SELECT count(*) FROM app.growbot_memories m WHERE m.persona_id = p.id) AS memories"
)
_CHECKPOINT = (
    "id::text AS id, persona_id::text AS persona_id, name, auto, why, created_at,"
    " coalesce(jsonb_array_length(memories), 0) AS memories"
)
# A persona's memories as a checkpoint keeps them.
_SNAPSHOT = (
    "(SELECT coalesce(jsonb_agg(jsonb_build_object('id', m.id, 'meta', m.meta, 'views', m.views)"
    " ORDER BY m.id), '[]'::jsonb) FROM app.growbot_memories m WHERE m.persona_id = :p)"
)


def _persona(r: Any, soul: bool = False) -> Persona:
    return Persona(
        id=r.id,
        name=r.name,
        version=r.version,
        made_from=r.made_from,
        phone_id=r.phone_id,
        last_phone_id=r.last_phone_id,
        opened_at=r.opened_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
        checkpoints=int(r.checkpoints),
        wearing=r.wearing,
        soul=dict(r.soul) if soul else None,
        memories=int(r.memories),
        face=r.face,
        voice=r.voice,
        prefs=for_phone(r.prefs)[0],
        explore=r.explore,
    )


def _checkpoint(r: Any, soul: bool = False) -> Checkpoint:
    return Checkpoint(
        id=r.id,
        persona_id=r.persona_id,
        name=r.name,
        auto=r.auto,
        why=r.why,
        created_at=r.created_at,
        soul=dict(r.soul) if soul else None,
        memories=int(r.memories),
    )


def _shas(views: Any) -> set[str]:
    return {
        v["sha"] for v in (views or []) if isinstance(v, dict) and isinstance(v.get("sha"), str)
    }


def _kept_shas(memories: Any) -> set[str]:
    out: set[str] = set()
    for m in memories or []:
        if isinstance(m, dict):
            out |= _shas(m.get("views"))
    return out


def _json(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


class _Refused(Exception):
    """An open found something gone after it had begun writing."""


async def _lock_bodies(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('growbot-body', 0))"))


async def _lock_blobs(session: AsyncSession) -> None:
    # A phone's write holds this from its first blob to its commit, and a sweep from its check
    # to its unlink, so a sweep never takes a blob a write is about to name.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('growbot-memory-blobs', 0))")
    )


async def _robot_held(session: AsyncSession, code: str) -> bool:
    # FOR KEY SHARE keeps the robot from being forgotten until this transaction ends.
    row = await session.execute(
        text("SELECT 1 FROM app.growbot_robots WHERE code = :c FOR KEY SHARE"), {"c": code}
    )
    return row.first() is not None


class SqlPersonaRepo:
    def __init__(
        self, maker: async_sessionmaker[AsyncSession], blobs: BlobStore | None = None
    ) -> None:
        self._maker = maker
        self._blobs = blobs

    async def _one(self, session: AsyncSession, pid: str, soul: bool) -> Persona | None:
        cols = _PERSONA + (", p.soul" if soul else "")
        row = (
            await session.execute(
                text(f"SELECT {cols} FROM app.growbot_personas p WHERE p.id = :id"), {"id": pid}
            )
        ).first()
        return _persona(row, soul) if row is not None else None

    async def list_personas(self, ctx: SessionContext) -> list[Persona]:
        async with scoped_session(self._maker, ctx) as session:
            rows = await session.execute(
                text(f"SELECT {_PERSONA} FROM app.growbot_personas p ORDER BY p.created_at, p.id")
            )
            return [_persona(r) for r in rows]

    async def get_persona(self, ctx: SessionContext, pid: str) -> Persona | None:
        if (key := _uuid(pid)) is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            return await self._one(session, key, soul=True)

    async def create(
        self,
        ctx: SessionContext,
        name: str,
        soul: dict[str, Any],
        made_from: str,
        memories_from: str | None = None,
        face: Face = "lantern",
        voice: str = "",
        explore: bool = True,
    ) -> Persona:
        """A new persona; with `memories_from`, holding a copy of that persona's memories
        (a duplicate), whose pictures it shares."""
        async with scoped_session(self._maker, ctx) as session:
            pid = (
                await session.execute(
                    text(
                        "INSERT INTO app.growbot_personas"
                        " (name, soul, made_from, face, voice, explore)"
                        " VALUES (:n, CAST(:s AS jsonb), :f, :face, :voice, :x)"
                        " RETURNING id::text"
                    ),
                    {
                        "n": name,
                        "s": _json(soul),
                        "f": made_from[:120],
                        "face": face,
                        "voice": voice,
                        "x": explore,
                    },
                )
            ).scalar_one()
            if memories_from is not None and (src := _uuid(memories_from)) is not None:
                # Its pictures are named only by the source's rows until this commits.
                await _lock_blobs(session)
                await session.execute(
                    text(
                        "INSERT INTO app.growbot_memories (persona_id, id, meta, views)"
                        " SELECT :p, id, meta, views FROM app.growbot_memories"
                        " WHERE persona_id = :src"
                    ),
                    {"p": pid, "src": src},
                )
                # A duplicate is the same creature, so it keeps the same settings, but not
                # the diary id: two creatures writing one diary would muddle both.
                await session.execute(
                    text(
                        "UPDATE app.growbot_personas SET prefs ="
                        " (SELECT prefs - 'gb_nx_sid' FROM app.growbot_personas WHERE id = :src)"
                        " WHERE id = :p"
                    ),
                    {"p": pid, "src": src},
                )
            made = await self._one(session, pid, soul=True)
            assert made is not None
            return made

    async def set_face(self, ctx: SessionContext, pid: str, face: Face) -> bool:
        """Not a soul write: the version stays, so its phone does not reload, and its poll
        carries the face to the creature."""
        if (key := _uuid(pid)) is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            done = await session.execute(
                text(
                    "UPDATE app.growbot_personas SET face = :f, updated_at = now()"
                    " WHERE id = :p RETURNING 1"
                ),
                {"p": key, "f": face},
            )
            return done.first() is not None

    async def set_voice(self, ctx: SessionContext, pid: str, voice: str) -> bool:
        """Like a face, not a soul write: its poll carries the voice to the creature."""
        if (key := _uuid(pid)) is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            done = await session.execute(
                text(
                    "UPDATE app.growbot_personas SET voice = :v, updated_at = now()"
                    " WHERE id = :p RETURNING 1"
                ),
                {"p": key, "v": voice},
            )
            return done.first() is not None

    async def set_explore(self, ctx: SessionContext, pid: str, explore: bool) -> bool:
        """Like a face, not a soul write: its poll carries the setting to the creature."""
        if (key := _uuid(pid)) is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            done = await session.execute(
                text(
                    "UPDATE app.growbot_personas SET explore = :x, updated_at = now()"
                    " WHERE id = :p RETURNING 1"
                ),
                {"p": key, "x": explore},
            )
            return done.first() is not None

    async def _checkpoint_now(
        self, session: AsyncSession, pid: str, name: str, auto: bool, why: str, freed: set[str]
    ) -> Checkpoint | None:
        """A checkpoint of the soul and memories as they are; the pictures of any automatic
        checkpoint it prunes go into `freed`, for a sweep once this commits. The caller holds
        the persona's row, and the blob lock is taken here: until this commits, the snapshot
        is the only thing naming a picture a racing forget lets go of."""
        await _lock_blobs(session)
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.growbot_checkpoints"
                    " (persona_id, name, auto, why, soul, memories)"
                    f" SELECT id, :n, :a, :w, soul, {_SNAPSHOT}"
                    " FROM app.growbot_personas WHERE id = :p"
                    f" RETURNING {_CHECKPOINT}"
                ),
                {"p": pid, "n": name[:CHECKPOINT_NAME_MAX], "a": auto, "w": why[:80]},
            )
        ).first()
        if row is None:
            return None
        if auto:
            pruned = await session.execute(
                text(
                    "DELETE FROM app.growbot_checkpoints WHERE id IN ("
                    " SELECT id FROM app.growbot_checkpoints"
                    " WHERE persona_id = :p AND auto"
                    " ORDER BY created_at DESC, id OFFSET :keep) RETURNING memories"
                ),
                {"p": pid, "keep": AUTO_KEEP},
            )
            for gone in pruned:
                freed |= _kept_shas(gone.memories)
        return _checkpoint(row)

    async def _sweep(self, freed: set[str]) -> None:
        """Delete each picture in `freed` that no memory or checkpoint names any more. Run
        after the transaction that let go of them has committed."""
        if not freed or self._blobs is None:
            return
        async with scoped_session(self._maker, _SYSTEM_OWNER) as session:
            await _lock_blobs(session)
            # One set-based pass, so a big forget holds the lock every write waits on briefly.
            loose = (
                await session.execute(
                    text(
                        "SELECT s FROM unnest(CAST(:shas AS text[])) AS s WHERE NOT EXISTS ("
                        "  SELECT 1 FROM app.growbot_memories m"
                        "  WHERE m.views @> jsonb_build_array(jsonb_build_object('sha', s)))"
                        " AND NOT EXISTS (SELECT 1 FROM app.growbot_checkpoints c"
                        "  WHERE c.memories @> jsonb_build_array(jsonb_build_object('views',"
                        "  jsonb_build_array(jsonb_build_object('sha', s)))))"
                    ),
                    {"shas": sorted(freed)},
                )
            ).scalars()
            for sha in loose:
                try:
                    await self._blobs.delete(sha)
                except ValueError:
                    # Not a digest: never one this repo wrote, so not one to unlink.
                    continue

    async def _bump_memories(self, session: AsyncSession, pid: str) -> int:
        return int(
            (
                await session.execute(
                    text(
                        "UPDATE app.growbot_personas SET memories_version = memories_version + 1"
                        " WHERE id = :p RETURNING memories_version"
                    ),
                    {"p": pid},
                )
            ).scalar_one()
        )

    async def write_soul(
        self,
        ctx: SessionContext,
        pid: str,
        soul: dict[str, Any],
        *,
        name: str | None = None,
        before: tuple[str, str] | None = None,
        expect: int | None = None,
        memories: MemoriesDo = "keep",
    ) -> tuple[Persona, Checkpoint | None] | Literal["stale"] | None:
        """The owner's write: an automatic checkpoint of the soul and memories as they are
        (`before` = its name and why) and then the new soul, in one transaction, bumping the
        version. With `expect`, "stale" when the soul is no longer that version (the phone
        wrote in between), so an edit made from an older read never drops what the creature
        just wrote. `memories` says what becomes of the visual memories (a reset and an import
        clear them, a restore puts back the checkpoint's)."""
        if (key := _uuid(pid)) is None:
            return None
        freed: set[str] = set()
        done = await self._write_soul(ctx, key, soul, name, before, expect, memories, freed)
        await self._sweep(freed)
        return done

    async def _write_soul(
        self,
        ctx: SessionContext,
        key: str,
        soul: dict[str, Any],
        name: str | None,
        before: tuple[str, str] | None,
        expect: int | None,
        memories: MemoriesDo,
        freed: set[str],
    ) -> tuple[Persona, Checkpoint | None] | Literal["stale"] | None:
        async with scoped_session(self._maker, ctx) as session:
            # Locks the row, so two writes in flight cannot both checkpoint the same soul.
            found = (
                await session.execute(
                    text("SELECT version FROM app.growbot_personas WHERE id = :p FOR UPDATE"),
                    {"p": key},
                )
            ).first()
            if found is None:
                return None
            if expect is not None and found.version != expect:
                return "stale"
            kept: Any = None
            if isinstance(memories, tuple):
                # Read before the checkpoint below is made: making it may prune this one.
                kept = (
                    await session.execute(
                        text(
                            "SELECT memories FROM app.growbot_checkpoints"
                            " WHERE id = :c AND persona_id = :p"
                        ),
                        {"c": _uuid(memories[1]), "p": key},
                    )
                ).scalar_one_or_none()
            cp = None
            if before is not None:
                cp = await self._checkpoint_now(session, key, before[0], True, before[1], freed)
            if memories == "clear" or kept is not None:
                gone = await session.execute(
                    text("DELETE FROM app.growbot_memories WHERE persona_id = :p RETURNING views"),
                    {"p": key},
                )
                for row in gone:
                    freed |= _shas(row.views)
                if kept:
                    await _lock_blobs(session)
                for m in kept or []:
                    await session.execute(
                        text(
                            "INSERT INTO app.growbot_memories (persona_id, id, meta, views)"
                            " VALUES (:p, :id, CAST(:m AS jsonb), CAST(:v AS jsonb))"
                        ),
                        {"p": key, "id": m["id"], "m": _json(m["meta"]), "v": _json(m["views"])},
                    )
                await self._bump_memories(session, key)
            await session.execute(
                text(
                    "UPDATE app.growbot_personas SET soul = CAST(:s AS jsonb),"
                    " version = version + 1, name = coalesce(:n, name), updated_at = now()"
                    " WHERE id = :p"
                ),
                {"p": key, "s": _json(soul), "n": name},
            )
            done = await self._one(session, key, soul=True)
            assert done is not None
            return done, cp

    async def delete(self, ctx: SessionContext, pid: str) -> bool:
        if (key := _uuid(pid)) is None:
            return False
        freed: set[str] = set()
        async with scoped_session(self._maker, ctx) as session:
            held = await session.execute(
                text(
                    "SELECT views AS v FROM app.growbot_memories WHERE persona_id = :p"
                    " UNION ALL SELECT memories AS v FROM app.growbot_checkpoints"
                    " WHERE persona_id = :p"
                ),
                {"p": key},
            )
            for row in held:
                freed |= _shas(row.v) | _kept_shas(row.v)
            gone = await session.execute(
                text("DELETE FROM app.growbot_personas WHERE id = :p RETURNING id"), {"p": key}
            )
            deleted = gone.first() is not None
        await self._sweep(freed)
        return deleted

    async def list_checkpoints(self, ctx: SessionContext, pid: str) -> list[Checkpoint]:
        if (key := _uuid(pid)) is None:
            return []
        async with scoped_session(self._maker, ctx) as session:
            rows = await session.execute(
                text(
                    f"SELECT {_CHECKPOINT} FROM app.growbot_checkpoints WHERE persona_id = :p"
                    " ORDER BY created_at DESC, id DESC"
                ),
                {"p": key},
            )
            return [_checkpoint(r) for r in rows]

    async def get_checkpoint(self, ctx: SessionContext, cid: str) -> Checkpoint | None:
        if (key := _uuid(cid)) is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(f"SELECT {_CHECKPOINT}, soul FROM app.growbot_checkpoints WHERE id = :c"),
                    {"c": key},
                )
            ).first()
            return _checkpoint(row, soul=True) if row is not None else None

    async def save_checkpoint(
        self, ctx: SessionContext, pid: str, name: str, auto: bool = False, why: str = ""
    ) -> Checkpoint | None:
        if (key := _uuid(pid)) is None:
            return None
        freed: set[str] = set()
        async with scoped_session(self._maker, ctx) as session:
            # The row before the blob lock, the order a phone's keep takes them in.
            held = await session.execute(
                text("SELECT 1 FROM app.growbot_personas WHERE id = :p FOR UPDATE"), {"p": key}
            )
            if held.first() is None:
                return None
            cp = await self._checkpoint_now(session, key, name, auto, why, freed)
        await self._sweep(freed)
        return cp

    async def _drop_checkpoints(
        self, ctx: SessionContext, pid: str, where: str, params: dict[str, Any]
    ) -> int | None:
        """Delete the persona's checkpoints matching `where`, sweeping the pictures only they
        named; None when there is no such persona."""
        if (key := _uuid(pid)) is None:
            return None
        freed: set[str] = set()
        async with scoped_session(self._maker, ctx) as session:
            # The row, then the blob lock, the order every write that frees pictures takes them.
            held = await session.execute(
                text("SELECT 1 FROM app.growbot_personas WHERE id = :p FOR UPDATE"), {"p": key}
            )
            if held.first() is None:
                return None
            await _lock_blobs(session)
            gone = (
                await session.execute(
                    text(
                        f"DELETE FROM app.growbot_checkpoints WHERE persona_id = :p AND {where}"
                        " RETURNING memories"
                    ),
                    {"p": key, **params},
                )
            ).all()
            for row in gone:
                freed |= _kept_shas(row.memories)
        await self._sweep(freed)
        return len(gone)

    async def delete_checkpoint(self, ctx: SessionContext, pid: str, cid: str) -> bool:
        """Forget one checkpoint, named or automatic."""
        if (ckey := _uuid(cid)) is None:
            return False
        return bool(await self._drop_checkpoints(ctx, pid, "id = :c", {"c": ckey}))

    async def delete_automatic(self, ctx: SessionContext, pid: str) -> int | None:
        """Forget every automatic checkpoint; the ones the owner named stay."""
        return await self._drop_checkpoints(ctx, pid, "auto", {})

    async def list_phones(self, ctx: SessionContext) -> list[Phone]:
        async with scoped_session(self._maker, ctx) as session:
            rows = await session.execute(
                text(
                    "SELECT id, label, created_at, last_seen_at FROM app.growbot_phones"
                    " ORDER BY last_seen_at DESC, id"
                )
            )
            return [Phone(r.id, r.label, r.created_at, r.last_seen_at) for r in rows]

    async def riders(self, ctx: SessionContext) -> list[Rider]:
        async with scoped_session(self._maker, ctx) as session:
            rows = await session.execute(
                text(
                    "SELECT r.code, b.persona_id::text AS persona_id, b.since"
                    " FROM app.growbot_robots r JOIN app.growbot_body b ON b.code = r.code"
                    " ORDER BY r.code"
                )
            )
            return [Rider(r.code, r.persona_id, r.since) for r in rows]

    async def set_rider(self, ctx: SessionContext, code: str, pid: str | None) -> bool:
        """Put persona `pid` in robot `code` (None: nobody). The persona keeps the phone it is
        on, which takes the legs; the persona it replaces carries on alone on its own phone.
        False when the robot or the persona is not there."""
        key = None
        if pid is not None and (key := _uuid(pid)) is None:
            return False
        try:
            async with scoped_session(self._maker, ctx) as session:
                return await self._ride(session, code, key)
        except IntegrityError:
            # Another pick put this persona in another robot at the same moment.
            return False

    async def _ride(self, session: AsyncSession, code: str, pid: str | None) -> bool:
        # Rider changes are rare and touch several robots' rows, so they take one lock and run
        # one at a time instead of deadlocking.
        await _lock_bodies(session)
        if not await _robot_held(session, code):
            return False
        if pid is not None:
            found = (
                await session.execute(
                    # FOR KEY SHARE: a delete of this persona racing the pick waits here
                    # instead of locking its body first.
                    text("SELECT 1 FROM app.growbot_personas WHERE id = :p FOR KEY SHARE"),
                    {"p": pid},
                )
            ).first()
            if found is None:
                return False
            # One body per persona: wearing this one leaves any other.
            await session.execute(
                text(
                    "UPDATE app.growbot_body SET persona_id = NULL, since = now()"
                    " WHERE persona_id = :p AND code <> :c"
                ),
                {"p": pid, "c": code},
            )
        await session.execute(
            text(
                "INSERT INTO app.growbot_body (code, persona_id) VALUES (:c, :p)"
                " ON CONFLICT (code) DO UPDATE SET persona_id = EXCLUDED.persona_id,"
                " since = CASE WHEN app.growbot_body.persona_id IS DISTINCT FROM"
                " EXCLUDED.persona_id THEN now() ELSE app.growbot_body.since END"
            ),
            {"c": code, "p": pid},
        )
        return True

    async def open_on_phone(
        self, ctx: SessionContext, pid: str, phone_id: str, label: str, code: str
    ) -> bool:
        """A ticket for persona `pid` was spent on `phone_id`: the phone now holds it (and only
        it), and with `code` it wears that robot. Opened alone, it leaves a body it wore on
        another phone, which stands still until a persona connects. False, with nothing
        changed, when the persona or the robot is gone or another open raced this one."""
        if (key := _uuid(pid)) is None:
            return False
        try:
            async with scoped_session(self._maker, ctx) as session:
                if not await self._open(session, key, phone_id, label, code):
                    # Raised, not returned, so the transaction rolls back whatever this open
                    # already wrote.
                    raise _Refused
        except (_Refused, IntegrityError):
            # IntegrityError: a bad phone id, or the persona went at the same moment.
            return False
        return True

    async def _open(
        self, session: AsyncSession, pid: str, phone_id: str, label: str, code: str
    ) -> bool:
        # Opens and rider picks run one at a time under the body lock, taken before any row
        # lock: an open touches two personas (and a body), and two phones swapping personas,
        # or an open racing a Who-wears pick, would otherwise lock them in opposite orders.
        await _lock_bodies(session)
        # Both personas this open moves (the one it opens and the one the phone held) are
        # locked before the phone's row, the order a push takes them in.
        rows = (
            await session.execute(
                text(
                    "SELECT id::text AS id, phone_id FROM app.growbot_personas"
                    " WHERE id = :p OR phone_id = :ph ORDER BY id FOR UPDATE"
                ),
                {"p": pid, "ph": phone_id},
            )
        ).all()
        held = next((r for r in rows if r.id == pid), None)
        if held is None:
            return False
        if code and not await _robot_held(session, code):
            return False
        await session.execute(
            text(
                "INSERT INTO app.growbot_phones (id, label) VALUES (:id, :l)"
                " ON CONFLICT (id) DO UPDATE SET last_seen_at = now(),"
                " label = CASE WHEN app.growbot_phones.label = '' THEN EXCLUDED.label"
                " ELSE app.growbot_phones.label END"
            ),
            {"id": phone_id, "l": label[:PHONE_LABEL_MAX]},
        )
        moved = held.phone_id is not None and held.phone_id != phone_id
        # The persona this phone held closes here; it is kept on the box.
        await session.execute(
            text(
                "UPDATE app.growbot_personas SET phone_id = NULL WHERE phone_id = :ph AND id <> :p"
            ),
            {"ph": phone_id, "p": pid},
        )
        await session.execute(
            text(
                "UPDATE app.growbot_personas SET phone_id = :ph, last_phone_id = :ph,"
                " opened_at = now() WHERE id = :p"
            ),
            {"ph": phone_id, "p": pid},
        )
        if code:
            return await self._ride(session, code, pid)
        if moved:
            await session.execute(
                text(
                    "UPDATE app.growbot_body SET persona_id = NULL, since = now()"
                    " WHERE persona_id = :p"
                ),
                {"p": pid},
            )
        return True

    async def pull(
        self, ctx: SessionContext, pid: str, phone_id: str, with_soul: bool = True
    ) -> Pulled | Literal["moved", "gone"]:
        """The soul for the phone that holds `pid`: "moved" when another phone holds it now,
        "gone" when it was deleted. Without the soul it is the phone's cheap poll: has the
        owner changed it, has the body moved."""
        if (key := _uuid(pid)) is None:
            return "gone"
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT p.version, p.memories_version, p.face, p.voice,"
                        f" p.prefs_version, p.explore,"
                        f" {'p.soul' if with_soul else 'NULL'} AS soul,"
                        " (SELECT b.code FROM app.growbot_body b WHERE b.persona_id = p.id"
                        "  LIMIT 1) AS wearing"
                        ", p.phone_id FROM app.growbot_personas p WHERE p.id = :p"
                    ),
                    {"p": key},
                )
            ).first()
            if row is None:
                return "gone"
            if row.phone_id != phone_id:
                return "moved"
            await session.execute(
                text("UPDATE app.growbot_phones SET last_seen_at = now() WHERE id = :ph"),
                {"ph": phone_id},
            )
            return Pulled(
                row.version,
                dict(row.soul or {}),
                row.wearing,
                row.memories_version,
                row.face,
                row.voice,
                row.prefs_version,
                row.explore,
            )

    async def push(
        self, ctx: SessionContext, pid: str, phone_id: str, base: int, soul: dict[str, Any]
    ) -> PushResult:
        """The phone's soul, written only if it holds `pid` and pulled the version it names."""
        if (key := _uuid(pid)) is None:
            return "gone"
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT phone_id, version FROM app.growbot_personas"
                        " WHERE id = :p FOR UPDATE"
                    ),
                    {"p": key},
                )
            ).first()
            if row is None:
                return "gone"
            if row.phone_id != phone_id:
                return "moved"
            if row.version != base:
                return "newer"
            version = (
                await session.execute(
                    text(
                        "UPDATE app.growbot_personas SET soul = CAST(:s AS jsonb),"
                        " version = version + 1, updated_at = now() WHERE id = :p"
                        " RETURNING version"
                    ),
                    {"p": key, "s": _json(soul)},
                )
            ).scalar_one()
            await session.execute(
                text("UPDATE app.growbot_phones SET last_seen_at = now() WHERE id = :ph"),
                {"ph": phone_id},
            )
            return int(version)

    # -- visual memories ---------------------------------------------------------------------

    async def _held(
        self, session: AsyncSession, pid: str, phone_id: str, lock: bool
    ) -> Away | None:
        row = (
            await session.execute(
                text(
                    "SELECT phone_id FROM app.growbot_personas WHERE id = :p"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"p": pid},
            )
        ).first()
        if row is None:
            return "gone"
        return "moved" if row.phone_id != phone_id else None

    async def _list(self, session: AsyncSession, pid: str) -> list[Memory]:
        rows = await session.execute(
            text(
                "SELECT id, meta, views FROM app.growbot_memories WHERE persona_id = :p"
                " ORDER BY (meta->>'t')::double precision DESC NULLS LAST, id"
            ),
            {"p": pid},
        )
        return [Memory(r.id, dict(r.meta), list(r.views)) for r in rows]

    async def _view(self, session: AsyncSession, pid: str, mid: str, i: int) -> bytes | None:
        views = (
            await session.execute(
                text("SELECT views FROM app.growbot_memories WHERE persona_id = :p AND id = :m"),
                {"p": pid, "m": mid},
            )
        ).scalar_one_or_none()
        if not isinstance(views, list) or not 0 <= i < len(views) or self._blobs is None:
            return None
        sha = views[i].get("sha") if isinstance(views[i], dict) else None
        # Only ever a digest this repo wrote; checked anyway, since a path is built from it.
        if not isinstance(sha, str) or not _DIGEST_RE.fullmatch(sha):
            return None
        try:
            return await self._blobs.get(sha)
        except (FileNotFoundError, KeyError, TypeError):
            return None

    async def memories(self, ctx: SessionContext, pid: str) -> list[Memory] | None:
        """The persona's visual memories, newest first; None when the persona is gone."""
        if (key := _uuid(pid)) is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            found = await session.execute(
                text("SELECT 1 FROM app.growbot_personas WHERE id = :p"), {"p": key}
            )
            if found.first() is None:
                return None
            return await self._list(session, key)

    async def memory_view(self, ctx: SessionContext, pid: str, mid: str, i: int) -> bytes | None:
        if (key := _uuid(pid)) is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            return await self._view(session, key, mid, i)

    async def forget_memory(self, ctx: SessionContext, pid: str, mid: str) -> bool:
        """The owner's delete. The phone holding the persona forgets it at its next sync."""
        if (key := _uuid(pid)) is None:
            return False
        freed: set[str] = set()
        async with scoped_session(self._maker, ctx) as session:
            # The persona's row first, the order every other memory write takes its locks in.
            await session.execute(
                text("SELECT 1 FROM app.growbot_personas WHERE id = :p FOR UPDATE"), {"p": key}
            )
            gone = (
                await session.execute(
                    text(
                        "DELETE FROM app.growbot_memories WHERE persona_id = :p AND id = :m"
                        " RETURNING views"
                    ),
                    {"p": key, "m": mid},
                )
            ).first()
            if gone is not None:
                freed = _shas(gone.views)
                await self._bump_memories(session, key)
        await self._sweep(freed)
        return gone is not None

    async def phone_memories(
        self, ctx: SessionContext, pid: str, phone_id: str
    ) -> tuple[int, list[Memory]] | Away:
        """What the box holds for the phone holding `pid`, to compare with its own."""
        if (key := _uuid(pid)) is None:
            return "gone"
        async with scoped_session(self._maker, ctx) as session:
            if (away := await self._held(session, key, phone_id, lock=False)) is not None:
                return away
            version = (
                await session.execute(
                    text("SELECT memories_version FROM app.growbot_personas WHERE id = :p"),
                    {"p": key},
                )
            ).scalar_one()
            return int(version), await self._list(session, key)

    async def phone_view(
        self, ctx: SessionContext, pid: str, phone_id: str, mid: str, i: int
    ) -> bytes | None | Away:
        if (key := _uuid(pid)) is None:
            return "gone"
        async with scoped_session(self._maker, ctx) as session:
            if (away := await self._held(session, key, phone_id, lock=False)) is not None:
                return away
            return await self._view(session, key, mid, i)

    async def phone_keep(
        self, ctx: SessionContext, pid: str, phone_id: str, saved: Saved
    ) -> int | Away | Literal["full"]:
        """A record the creature saved or renamed, kept whole (its pictures replaced) only
        while this phone holds the persona."""
        if (key := _uuid(pid)) is None:
            return "gone"
        if self._blobs is None:
            raise RuntimeError("no store for visual memories")
        freed: set[str] = set()
        async with scoped_session(self._maker, ctx) as session:
            if (away := await self._held(session, key, phone_id, lock=True)) is not None:
                return away
            had = (
                await session.execute(
                    text(
                        "SELECT views FROM app.growbot_memories WHERE persona_id = :p AND id = :m"
                    ),
                    {"p": key, "m": saved.id},
                )
            ).first()
            if had is None:
                count = (
                    await session.execute(
                        text("SELECT count(*) FROM app.growbot_memories WHERE persona_id = :p"),
                        {"p": key},
                    )
                ).scalar_one()
                if count >= MAX_MEMORIES:
                    return "full"
            # A phone left saving faces must never be what fills the box's disk.
            if self._blobs.free_bytes() < FREE_FLOOR_BYTES:
                return "full"
            await _lock_blobs(session)
            views = [{"sha": await self._blobs.put(data), "t": t} for data, t in saved.views]
            if had is not None:
                freed = _shas(had.views) - _shas(views)
            await session.execute(
                text(
                    "INSERT INTO app.growbot_memories (persona_id, id, meta, views)"
                    " VALUES (:p, :m, CAST(:meta AS jsonb), CAST(:v AS jsonb))"
                    " ON CONFLICT (persona_id, id) DO UPDATE SET meta = EXCLUDED.meta,"
                    " views = EXCLUDED.views, updated_at = now()"
                ),
                {"p": key, "m": saved.id, "meta": _json(saved.meta), "v": _json(views)},
            )
            version = await self._bump_memories(session, key)
        await self._sweep(freed)
        return version

    async def phone_forget(
        self, ctx: SessionContext, pid: str, phone_id: str, ids: list[str]
    ) -> int | Away:
        """Records the creature forgot, gone from the box too, while this phone holds it."""
        if (key := _uuid(pid)) is None:
            return "gone"
        freed: set[str] = set()
        async with scoped_session(self._maker, ctx) as session:
            if (away := await self._held(session, key, phone_id, lock=True)) is not None:
                return away
            gone = (
                await session.execute(
                    text(
                        "DELETE FROM app.growbot_memories WHERE persona_id = :p"
                        " AND id = ANY(:ids) RETURNING views"
                    ),
                    {"p": key, "ids": ids},
                )
            ).all()
            for row in gone:
                freed |= _shas(row.views)
            if gone:
                version = await self._bump_memories(session, key)
            else:
                version = int(
                    (
                        await session.execute(
                            text("SELECT memories_version FROM app.growbot_personas WHERE id = :p"),
                            {"p": key},
                        )
                    ).scalar_one()
                )
        await self._sweep(freed)
        return version

    # -- settings -----------------------------------------------------------------------------

    async def _patch(self, session: AsyncSession, pid: str, patch: Patch, phone: bool) -> int:
        # The row is locked by the caller, so two patches merge one after the other.
        had = (
            await session.execute(
                text("SELECT prefs FROM app.growbot_personas WHERE id = :p"), {"p": pid}
            )
        ).scalar_one()
        if phone:
            if had is not None and OWNER_ONLY in had:
                # The owner's settings came first: a phone fills in around them, never over
                # them, even one that pulled before the owner's write (growbot-box.js adopt).
                patch = Patch(
                    {k: v for k, v in patch.set.items() if k not in had},
                    [k for k in patch.drop if k not in had],
                )
            had = for_phone(had)[0]
        elif had is None:
            had = {OWNER_ONLY: "1"}
        return int(
            (
                await session.execute(
                    text(
                        "UPDATE app.growbot_personas SET prefs = CAST(:s AS jsonb),"
                        " prefs_version = prefs_version + 1 WHERE id = :p"
                        " RETURNING prefs_version"
                    ),
                    {"p": pid, "s": _json(merged(had, patch))},
                )
            ).scalar_one()
        )

    async def patch_prefs(self, ctx: SessionContext, pid: str, patch: Patch) -> int | None:
        """The owner's edit of the creature's settings; its phone's poll carries it there.
        Raises PrefsTooBig when the merged settings would pass the caps."""
        if (key := _uuid(pid)) is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text("SELECT 1 FROM app.growbot_personas WHERE id = :p FOR UPDATE"),
                    {"p": key},
                )
            ).first()
            if row is None:
                return None
            return await self._patch(session, key, patch, phone=False)

    async def phone_prefs(
        self, ctx: SessionContext, pid: str, phone_id: str
    ) -> tuple[int, dict[str, str] | None, bool] | Away:
        """The settings for the phone holding `pid`: None when nobody set any yet, and whether
        no phone has sent its own (the owner's are there, and the phone keeps its own under)."""
        if (key := _uuid(pid)) is None:
            return "gone"
        async with scoped_session(self._maker, ctx) as session:
            if (away := await self._held(session, key, phone_id, lock=False)) is not None:
                return away
            row = (
                await session.execute(
                    text("SELECT prefs, prefs_version FROM app.growbot_personas WHERE id = :p"),
                    {"p": key},
                )
            ).one()
            shown, adopt = for_phone(row.prefs)
            return int(row.prefs_version), shown, adopt

    async def phone_patch_prefs(
        self, ctx: SessionContext, pid: str, phone_id: str, patch: Patch
    ) -> int | Away:
        """The creature's own change to its settings, kept only while this phone holds it."""
        if (key := _uuid(pid)) is None:
            return "gone"
        async with scoped_session(self._maker, ctx) as session:
            if (away := await self._held(session, key, phone_id, lock=True)) is not None:
                return away
            return await self._patch(session, key, patch, phone=True)
