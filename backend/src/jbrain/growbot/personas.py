"""Personas on the box: their souls, checkpoints, the phones that hold them and the body's rider
(migration 0257, docs/plans/GROWBOT_PLAN.md §2c, wave G4a).

Every soul write bumps `version`. The phone pushes against the version it last pulled, so a
soul the owner changed from the PWA (an edit, a restore, a reset, an import) is never
overwritten by a phone's older cache: that push is refused as `newer`, and the phone reloads.
A push from a phone that no longer holds the persona is refused as `moved`.

The hand-over rules live in `open_on_phone` and `set_rider`, each one statement batch in one
transaction: a phone holds one persona, a persona is on one phone, the body has one rider.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

NAME_MAX = 40
CHECKPOINT_NAME_MAX = 80
# Automatic checkpoints (before an edit, restore, reset, import) are pruned past this many per
# persona, oldest first; the ones the owner named are kept until the persona goes.
AUTO_KEEP = 20
PHONE_LABEL_MAX = 40


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


@dataclass(frozen=True)
class Checkpoint:
    id: str
    persona_id: str
    name: str
    auto: bool
    why: str
    created_at: datetime
    soul: dict[str, Any] | None = None


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


PushResult = int | Literal["moved", "newer", "gone"]


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


class PersonaRepo(Protocol):
    async def list_personas(self, ctx: SessionContext) -> list[Persona]: ...
    async def get_persona(self, ctx: SessionContext, pid: str) -> Persona | None: ...
    async def create(
        self, ctx: SessionContext, name: str, soul: dict[str, Any], made_from: str
    ) -> Persona: ...
    async def write_soul(
        self,
        ctx: SessionContext,
        pid: str,
        soul: dict[str, Any],
        *,
        name: str | None = None,
        before: tuple[str, str] | None = None,
        expect: int | None = None,
    ) -> tuple[Persona, Checkpoint | None] | Literal["stale"] | None: ...
    async def delete(self, ctx: SessionContext, pid: str) -> bool: ...
    async def list_checkpoints(self, ctx: SessionContext, pid: str) -> list[Checkpoint]: ...
    async def get_checkpoint(self, ctx: SessionContext, cid: str) -> Checkpoint | None: ...
    async def save_checkpoint(
        self, ctx: SessionContext, pid: str, name: str, auto: bool = False, why: str = ""
    ) -> Checkpoint | None: ...
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


_PERSONA = (
    "p.id::text AS id, p.name, p.version, p.made_from, p.phone_id, p.last_phone_id,"
    " p.opened_at, p.created_at, p.updated_at,"
    " (SELECT count(*) FROM app.growbot_checkpoints c WHERE c.persona_id = p.id) AS checkpoints,"
    " (SELECT b.code FROM app.growbot_body b WHERE b.persona_id = p.id LIMIT 1) AS wearing"
)
_CHECKPOINT = "id::text AS id, persona_id::text AS persona_id, name, auto, why, created_at"


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
    )


def _json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


class SqlPersonaRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

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
        self, ctx: SessionContext, name: str, soul: dict[str, Any], made_from: str
    ) -> Persona:
        async with scoped_session(self._maker, ctx) as session:
            pid = (
                await session.execute(
                    text(
                        "INSERT INTO app.growbot_personas (name, soul, made_from)"
                        " VALUES (:n, CAST(:s AS jsonb), :f) RETURNING id::text"
                    ),
                    {"n": name, "s": _json(soul), "f": made_from[:120]},
                )
            ).scalar_one()
            made = await self._one(session, pid, soul=True)
            assert made is not None
            return made

    async def _checkpoint_now(
        self, session: AsyncSession, pid: str, name: str, auto: bool, why: str
    ) -> Checkpoint | None:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.growbot_checkpoints (persona_id, name, auto, why, soul)"
                    " SELECT id, :n, :a, :w, soul FROM app.growbot_personas WHERE id = :p"
                    f" RETURNING {_CHECKPOINT}"
                ),
                {"p": pid, "n": name[:CHECKPOINT_NAME_MAX], "a": auto, "w": why[:80]},
            )
        ).first()
        if row is None:
            return None
        if auto:
            await session.execute(
                text(
                    "DELETE FROM app.growbot_checkpoints WHERE id IN ("
                    " SELECT id FROM app.growbot_checkpoints"
                    " WHERE persona_id = :p AND auto"
                    " ORDER BY created_at DESC, id OFFSET :keep)"
                ),
                {"p": pid, "keep": AUTO_KEEP},
            )
        return _checkpoint(row)

    async def write_soul(
        self,
        ctx: SessionContext,
        pid: str,
        soul: dict[str, Any],
        *,
        name: str | None = None,
        before: tuple[str, str] | None = None,
        expect: int | None = None,
    ) -> tuple[Persona, Checkpoint | None] | Literal["stale"] | None:
        """The owner's write: an automatic checkpoint of the soul as it is (`before` = its name
        and why) and then the new soul, in one transaction, bumping the version. With `expect`,
        "stale" when the soul is no longer that version (the phone wrote in between), so an
        edit made from an older read never drops what the creature just wrote."""
        if (key := _uuid(pid)) is None:
            return None
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
            cp = None
            if before is not None:
                cp = await self._checkpoint_now(session, key, before[0], True, before[1])
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
        async with scoped_session(self._maker, ctx) as session:
            gone = await session.execute(
                text("DELETE FROM app.growbot_personas WHERE id = :p RETURNING id"), {"p": key}
            )
            return gone.first() is not None

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
        async with scoped_session(self._maker, ctx) as session:
            return await self._checkpoint_now(session, key, name, auto, why)

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
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('growbot-body', 0))")
        )
        robot = (
            await session.execute(
                text("SELECT 1 FROM app.growbot_robots WHERE code = :c"), {"c": code}
            )
        ).first()
        if robot is None:
            return False
        if pid is not None:
            found = (
                await session.execute(
                    text("SELECT 1 FROM app.growbot_personas WHERE id = :p"), {"p": pid}
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
                return await self._open(session, key, phone_id, label, code)
        except IntegrityError:
            # Two tickets spent on one phone at once: the one-persona-per-phone index let one
            # through, and this one opens nothing.
            return False

    async def _open(
        self, session: AsyncSession, pid: str, phone_id: str, label: str, code: str
    ) -> bool:
        # One open per phone at a time: two tickets spent on one phone would otherwise lock
        # two personas in opposite orders and deadlock.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('growbot-phone:' || :ph, 0))"),
            {"ph": phone_id},
        )
        held = (
            await session.execute(
                text("SELECT phone_id FROM app.growbot_personas WHERE id = :p FOR UPDATE"),
                {"p": pid},
            )
        ).first()
        if held is None:
            return False
        if code:
            robot = (
                await session.execute(
                    text("SELECT 1 FROM app.growbot_robots WHERE code = :c"), {"c": code}
                )
            ).first()
            if robot is None:
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
                        f"SELECT p.version, {'p.soul' if with_soul else 'NULL'} AS soul,"
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
            return Pulled(row.version, dict(row.soul or {}), row.wearing)

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
