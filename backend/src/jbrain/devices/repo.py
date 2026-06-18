"""Device provisioning over an RLS-scoped session.

A "device" is a `Subject(kind='device')` with a bound `Principal(kind='device_key')`
that authenticates OwnTracks ingest (Phase 7). Provisioning, rotation, and
revocation are owner-only: every method runs under the caller's owner
`SessionContext`, so the `subjects`/`principals` RLS policies (owner-only writes)
are the enforcement — not application politeness.
"""

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


@dataclass(frozen=True)
class DeviceInfo:
    """A provisioned device. `id` is the device's subject id (its stable identity);
    `revoked` is True once it has no active key."""

    id: str
    label: str
    created_at: datetime
    revoked: bool


@dataclass(frozen=True)
class LinkedPerson:
    """The Person entity an operational device subject is bound to (via the owner-set
    `entities.subject_id` link), identified by entity id and canonical name."""

    entity_id: str
    canonical_name: str


class DeviceRepo(Protocol):
    async def provision(self, ctx: SessionContext, *, label: str, key_hash: str) -> DeviceInfo: ...

    async def list(self, ctx: SessionContext) -> Sequence[DeviceInfo]: ...

    async def rotate(self, ctx: SessionContext, device_id: str, key_hash: str) -> bool: ...

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool: ...


class SqlDeviceRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def provision(self, ctx: SessionContext, *, label: str, key_hash: str) -> DeviceInfo:
        sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        async with scoped_session(self._maker, ctx) as session:
            created_at = (
                await session.execute(
                    text(
                        "INSERT INTO app.subjects (id, display_name, kind)"
                        " VALUES (:sid, :label, 'device') RETURNING created_at"
                    ),
                    {"sid": sid, "label": label},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO app.principals (id, kind, subject_id, key_hash, label)"
                    " VALUES (:pid, 'device_key', :sid, :kh, :label)"
                ),
                {"pid": pid, "sid": sid, "kh": key_hash, "label": label},
            )
        return DeviceInfo(id=sid, label=label, created_at=created_at, revoked=False)

    async def list(self, ctx: SessionContext) -> Sequence[DeviceInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT s.id, s.display_name, s.created_at,"
                        " bool_or(p.id IS NOT NULL) AS has_active_key"
                        " FROM app.subjects s"
                        " LEFT JOIN app.principals p"
                        "   ON p.subject_id = s.id AND p.kind = 'device_key'"
                        "   AND p.revoked_at IS NULL"
                        " WHERE s.kind = 'device'"
                        " GROUP BY s.id, s.display_name, s.created_at"
                        " ORDER BY s.created_at DESC"
                    )
                )
            ).all()
        return [
            DeviceInfo(
                id=str(r.id),
                label=r.display_name,
                created_at=r.created_at,
                revoked=not r.has_active_key,
            )
            for r in rows
        ]

    async def rotate(self, ctx: SessionContext, device_id: str, key_hash: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            if not await self._is_device(session, device_id):
                return False
            await self._revoke_keys(session, device_id)
            await session.execute(
                text(
                    "INSERT INTO app.principals (id, kind, subject_id, key_hash, label)"
                    " SELECT gen_random_uuid(), 'device_key', :sid, :kh, display_name"
                    " FROM app.subjects WHERE id = :sid"
                ),
                {"sid": device_id, "kh": key_hash},
            )
        return True

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            if not await self._is_device(session, device_id):
                return False
            await self._revoke_keys(session, device_id)
        return True

    async def linked_person(self, ctx: SessionContext, subject_id: str) -> LinkedPerson | None:
        """The graph entity bound to a device subject via `entities.subject_id`, or
        None when the device is unlinked. The binding is owner-set and deterministic
        (never LLM-chosen); this only reads it back, RLS-scoped to the caller."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id::text AS eid, canonical_name FROM app.entities"
                        " WHERE subject_id = cast(:sid AS uuid) AND status != 'merged' LIMIT 1"
                    ),
                    {"sid": subject_id},
                )
            ).first()
        if row is None:
            return None
        return LinkedPerson(entity_id=row.eid, canonical_name=row.canonical_name)

    async def subject_for_person(self, ctx: SessionContext, entity_id: str) -> str | None:
        """The device subject id bound to a Person entity (the reverse of
        `linked_person`), or None when the entity has no device link. RLS-scoped."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT subject_id::text AS sid FROM app.entities"
                        " WHERE id = cast(:eid AS uuid) AND subject_id IS NOT NULL"
                        "   AND status != 'merged'"
                    ),
                    {"eid": entity_id},
                )
            ).first()
        return row.sid if row is not None else None

    async def device_subjects_for_entity(
        self, ctx: SessionContext, entity_id: str
    ) -> builtins.list[str]:
        """Every device subject reachable from a named entity, covering BOTH the
        entity itself being a Device bound directly (its `subject_id` is a device
        subject) AND the entity being a Person who OPERATES devices (each operated
        Device entity's `subject_id`). The binding is owner-set/deterministic — the
        L1 reconciler sets `subject_id` only on Device entities carrying an
        `operatedBy`→Person fact — so a Person's own `subject_id` is a person subject,
        never a track; resolution must hop Person→operatedBy→Device→`subject_id`.
        Returns distinct device subject ids, RLS-scoped."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT sid FROM ("
                        "  SELECT e.subject_id::text AS sid"
                        "  FROM app.entities e JOIN app.subjects s ON s.id = e.subject_id"
                        "  WHERE e.id = cast(:eid AS uuid) AND e.status != 'merged'"
                        "    AND e.subject_id IS NOT NULL AND s.kind = 'device'"
                        "  UNION"
                        "  SELECT d.subject_id::text AS sid"
                        "  FROM app.facts f"
                        "  JOIN app.entities d ON d.id = f.entity_id"
                        "  JOIN app.subjects s ON s.id = d.subject_id"
                        "  WHERE f.object_entity_id = cast(:eid AS uuid)"
                        "    AND f.predicate = 'operatedBy' AND f.kind = 'relationship'"
                        "    AND f.status = 'active' AND f.assertion = 'asserted'"
                        "    AND d.subject_id IS NOT NULL AND d.status != 'merged'"
                        "    AND s.kind = 'device'"
                        ") u"
                    ),
                    {"eid": entity_id},
                )
            ).all()
        return [r.sid for r in rows]

    async def owner_device_subjects(self, ctx: SessionContext) -> builtins.list[str]:
        """The owner's own device subjects, resolved DETERMINISTICALLY via the "Me"
        hard-link (`subject_id IS NOT NULL AND lower(canonical_name)='me'`, the same
        anchor `analysis/entities.py::_find_me` uses) → the devices that "Me"
        operates → their `subject_id`s. Never a fuzzy/substring match on "Me".
        Returns distinct device subject ids, RLS-scoped."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "WITH me AS ("
                        "  SELECT id FROM app.entities"
                        "  WHERE subject_id IS NOT NULL AND lower(canonical_name) = 'me'"
                        "    AND status != 'merged' LIMIT 1"
                        ")"
                        " SELECT DISTINCT d.subject_id::text AS sid"
                        " FROM app.facts f"
                        " JOIN me ON f.object_entity_id = me.id"
                        " JOIN app.entities d ON d.id = f.entity_id"
                        " JOIN app.subjects s ON s.id = d.subject_id"
                        " WHERE f.predicate = 'operatedBy' AND f.kind = 'relationship'"
                        "   AND f.status = 'active' AND f.assertion = 'asserted'"
                        "   AND d.subject_id IS NOT NULL AND d.status != 'merged'"
                        "   AND s.kind = 'device'"
                    )
                )
            ).all()
        return [r.sid for r in rows]

    @staticmethod
    async def _is_device(session: AsyncSession, device_id: str) -> bool:
        return (
            await session.execute(
                text("SELECT 1 FROM app.subjects WHERE id = :sid AND kind = 'device'"),
                {"sid": device_id},
            )
        ).first() is not None

    @staticmethod
    async def _revoke_keys(session: AsyncSession, device_id: str) -> None:
        await session.execute(
            text(
                "UPDATE app.principals SET revoked_at = now()"
                " WHERE subject_id = :sid AND kind = 'device_key' AND revoked_at IS NULL"
            ),
            {"sid": device_id},
        )
