"""jmolt's obligation ledger — what it left open, and the verbatim evidence for it.

The store behind the second night engine (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2). The
shipped engine keeps jmolt's continuity in a free-text scratchpad it re-reads each night as
trusted context, which is how a night becomes a rewrite of the night before: mode collapse
does not need a memory failure to produce repetition, it only needs the same prose in front of
the same model. These are typed rows instead, and the composer prints them — the model never
re-reads a sentence it wrote.

`open_` returns what is owed, ordered by `touched_at` so an obligation worked on last night
outranks one opened a fortnight ago and left alone. That ordering IS the identity claim: the
agent is the sum of its unfinished business, most recently disturbed first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# What an obligation can be. Three kinds, one table: they share a lifecycle and the composer
# reads them together.
KINDS = ("question", "commitment", "person")
MAX_SUBJECT = 200
MAX_QUOTE = 2000


@dataclass(frozen=True)
class Evidence:
    """One verbatim quote. Never a paraphrase — a paraphrase is prose the next night would
    paraphrase again, and two hops from the source is where a claim stops being checkable."""

    quote: str
    source: str
    at: datetime


@dataclass(frozen=True)
class Obligation:
    id: str
    kind: str
    subject: str
    status: str
    resolution: str
    opened_at: datetime
    touched_at: datetime
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def nights_open(self) -> int:
        """Whole days since it was opened. The composer shows this because an obligation open
        for a week without progress is a different fact from one opened last night, and the
        difference is exactly what "follow-through" means."""
        return max(0, (self.touched_at.date() - self.opened_at.date()).days)


def _to_obligation(row: Any, evidence: list[Evidence] | None = None) -> Obligation:
    return Obligation(
        id=str(row.id),
        kind=row.kind,
        subject=row.subject,
        status=row.status,
        resolution=row.resolution,
        opened_at=row.opened_at,
        touched_at=row.touched_at,
        evidence=evidence or [],
    )


class ObligationRepo:
    async def open(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        kind: str,
        subject: str,
        quote: str = "",
        source: str = "",
    ) -> str | None:
        """Open an obligation, or touch the existing one with the same (kind, subject).

        Returns its id — the same id either way, so a caller cannot tell whether it created
        the row, and does not need to. Promise extraction runs every night over text the model
        may or may not remember writing, so "open this, again, possibly" is the ONLY sane
        contract; making the caller check first would put a race and a branch in the one path
        that has to be dull.

        Returns None when the subject is blank or over-long: an obligation whose handle is
        empty prints as a blank line in tomorrow's brief, and one that is a whole paragraph
        is the prose this table exists to avoid."""
        subject = subject.strip()
        if not subject or len(subject) > MAX_SUBJECT or kind not in KINDS:
            return None
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.jmolt_obligation (principal_id, kind, subject)"
                    " VALUES (:pid, :kind, :subject)"
                    " ON CONFLICT (principal_id, kind, subject) DO UPDATE"
                    # Re-encountering something abandoned or discharged REOPENS it: the world
                    # brought it back, and a closed row that stays closed is how a promise
                    # someone chased up disappears.
                    "   SET touched_at = clock_timestamp(), status = 'open', closed_at = NULL"
                    " RETURNING id"
                ),
                {"pid": principal_id, "kind": kind, "subject": subject},
            )
        ).scalar_one()
        obligation_id = str(row)
        if quote:
            await self.evidence(session, principal_id, obligation_id, quote=quote, source=source)
        return obligation_id

    async def evidence(
        self,
        session: AsyncSession,
        principal_id: str,
        obligation_id: str,
        *,
        quote: str,
        source: str = "",
    ) -> bool:
        """Attach one verbatim quote. A duplicate quote on the same obligation is a no-op, so
        re-reading the same thread twice in a night does not double the evidence — but it DOES
        touch the obligation, because encountering something again is itself a fact about
        what is live.

        Returns whether a new quote landed."""
        quote = quote.strip()[:MAX_QUOTE]
        if not quote:
            return False
        landed = (
            await session.execute(
                text(
                    "INSERT INTO app.jmolt_evidence"
                    " (principal_id, obligation_id, quote, source)"
                    " VALUES (:pid, cast(:oid AS uuid), :quote, :source)"
                    " ON CONFLICT (obligation_id, quote) DO NOTHING"
                    " RETURNING id"
                ),
                {"pid": principal_id, "oid": obligation_id, "quote": quote, "source": source},
            )
        ).scalar_one_or_none()
        await self.touch(session, principal_id, obligation_id)
        return landed is not None

    async def touch(self, session: AsyncSession, principal_id: str, obligation_id: str) -> None:
        await session.execute(
            text(
                "UPDATE app.jmolt_obligation SET touched_at = clock_timestamp()"
                " WHERE id = cast(:oid AS uuid) AND principal_id = :pid"
            ),
            {"oid": obligation_id, "pid": principal_id},
        )

    async def close(
        self,
        session: AsyncSession,
        principal_id: str,
        obligation_id: str,
        *,
        resolution: str,
        abandoned: bool = False,
    ) -> bool:
        """Discharge an obligation (or abandon it), with jmolt's own account of what closed it.

        Abandoning is a first-class outcome, not a failure state. An agent that can only ever
        discharge accumulates a brief it can never finish reading, and the pressure that puts
        on a night is indistinguishable from the pressure to post."""
        status = "abandoned" if abandoned else "discharged"
        result = await session.execute(
            text(
                "UPDATE app.jmolt_obligation"
                " SET status = :st, resolution = :res,"
                "     closed_at = clock_timestamp(), touched_at = clock_timestamp()"
                " WHERE id = cast(:oid AS uuid) AND principal_id = :pid AND status = 'open'"
            ),
            {
                "st": status,
                "res": resolution.strip()[:MAX_QUOTE],
                "oid": obligation_id,
                "pid": principal_id,
            },
        )
        return bool(result.rowcount)

    async def open_(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        kinds: tuple[str, ...] = KINDS,
        limit: int = 12,
        evidence_each: int = 3,
    ) -> list[Obligation]:
        """What is owed, most recently disturbed first, with the newest evidence on each.

        Bounded twice on purpose. The brief this feeds is the whole context a sitting gets,
        and an unbounded ledger would reproduce the failure it replaces: a context that grows
        until the model reads its own past instead of the world.

        The ordering only works because `touched_at` is written with `clock_timestamp()`
        rather than `now()`: `now()` is transaction time, so everything one sitting touched
        would tie and this would quietly become insertion order."""
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM app.jmolt_obligation"
                    " WHERE principal_id = :pid AND status = 'open' AND kind = ANY(:kinds)"
                    " ORDER BY touched_at DESC, seq DESC LIMIT :lim"
                ),
                {"pid": principal_id, "kinds": list(kinds), "lim": limit},
            )
        ).all()
        out = []
        for row in rows:
            quotes = (
                await session.execute(
                    text(
                        "SELECT quote, source, at FROM app.jmolt_evidence"
                        " WHERE obligation_id = :oid ORDER BY at DESC, seq DESC LIMIT :lim"
                    ),
                    {"oid": row.id, "lim": evidence_each},
                )
            ).all()
            out.append(
                _to_obligation(
                    row, [Evidence(quote=q.quote, source=q.source, at=q.at) for q in quotes]
                )
            )
        return out

    async def closed_since(
        self, session: AsyncSession, principal_id: str, *, since: datetime
    ) -> list[Obligation]:
        """What was discharged or abandoned since `since` — the follow-through numerator, and
        the one part of the brief that is allowed to be good news."""
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM app.jmolt_obligation"
                    " WHERE principal_id = :pid AND status <> 'open' AND closed_at >= :since"
                    " ORDER BY closed_at DESC"
                ),
                {"pid": principal_id, "since": since},
            )
        ).all()
        return [_to_obligation(r) for r in rows]

    async def counts(self, session: AsyncSession, principal_id: str) -> dict[str, int]:
        """Open obligations per kind. For the observer and the scorer — never shown to jmolt.
        A cumulative number the agent can only move by acting is a slot machine."""
        rows = (
            await session.execute(
                text(
                    "SELECT kind, count(*) AS n FROM app.jmolt_obligation"
                    " WHERE principal_id = :pid AND status = 'open' GROUP BY kind"
                ),
                {"pid": principal_id},
            )
        ).all()
        return {r.kind: int(r.n) for r in rows}
