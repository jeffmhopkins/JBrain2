"""Note-deletion purge: hard-delete everything derived from a note.

docs/ANALYSIS.md doctrine says nothing derived is ever deleted — the
supersession chain is the revision history. This module is the one
sanctioned exception [decided]: notes are the sole sources of truth, so
deleting a note is a privacy promise that everything derived from it goes
too — facts, entity mentions, temporal tokens, review items in ANY status
(resolved history carries frozen snippets of the note's text), the
note_analysis row, and provisional entities no surviving note references.
The note row itself stays soft-deleted (the settled Phase 2 behavior); only
the derived graph purges, and the pipeline skips deleted notes, so nothing
re-creates these artifacts afterward.

Runs on the caller's session, inside the SAME transaction as the soft
delete, so a failed purge never leaves a half-purged graph. This is a
sibling of the review-reopen effects-unwind (analysis/repo.py): both repair
the graph when a write that shaped it is taken back — reopen replays
recorded effects, the purge re-derives chain repairs from what survives.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from jbrain.analysis.appointment_projection import project_appointments
from jbrain.analysis.geofence_projection import project_place_geofences
from jbrain.models.agent import AgentEpisode, AgentEpisodeRef
from jbrain.models.analysis import (
    Entity,
    EntityDistinction,
    EntityMention,
    Fact,
    NoteAnalysis,
    TemporalToken,
)


def chain_repair_target(
    start: uuid.UUID | None, doomed_links: dict[uuid.UUID, uuid.UUID | None]
) -> uuid.UUID | None:
    """First transitive supersessor of `start` that is NOT doomed; None when
    the chain dies inside the doomed set.

    `doomed_links` maps each doomed fact to its own superseded_by, so a
    survivor pointing into the doomed set can re-attach to whatever surviving
    fact sits deeper in the chain — multiple consecutive doomed links are
    walked through. A cycle would mean corrupted chain data; bail to None
    (treat as chain-dead) rather than loop forever.
    """
    seen: set[uuid.UUID] = set()
    current = start
    while current is not None and current in doomed_links:
        if current in seen:
            return None
        seen.add(current)
        current = doomed_links[current]
    return current


async def purge_note_artifacts(session: AsyncSession, note_id: uuid.UUID) -> None:
    """Purge every artifact derived from `note_id`, repairing supersession
    chains first. A never-analyzed note has nothing here and is a no-op."""
    doomed = (
        await session.execute(
            select(
                Fact.id, Fact.superseded_by, Fact.valid_from, Fact.entity_id, Fact.object_entity_id
            ).where(Fact.note_id == note_id)
        )
    ).all()
    doomed_links: dict[uuid.UUID, uuid.UUID | None] = {f.id: f.superseded_by for f in doomed}
    doomed_close: dict[uuid.UUID, datetime | None] = {f.id: f.valid_from for f in doomed}

    # Entity ids must be collected BEFORE their referencing rows go: after
    # the deletes there is no way to know which entities this note touched.
    candidates: set[uuid.UUID] = set()
    for f in doomed:
        candidates.add(f.entity_id)
        if f.object_entity_id is not None:
            candidates.add(f.object_entity_id)
    candidates.update(
        (
            await session.execute(
                select(EntityMention.entity_id).where(EntityMention.note_id == note_id).distinct()
            )
        ).scalars()
    )

    await repair_chains(session, doomed_links, doomed_close)
    await delete_review_items(session, set(doomed_links), note_id=note_id)

    await session.execute(delete(Fact).where(Fact.note_id == note_id))
    # Tokens are per-note by construction (the pipeline only mints them for
    # the note being analyzed), so no other note's fact should cite one — but
    # the FK would abort the whole delete if a stray citation ever appeared,
    # so unhook defensively rather than trust the invariant. Runs after the
    # fact delete: only other notes' facts can still match.
    await session.execute(
        update(Fact)
        .where(
            Fact.temporal_token_id.in_(
                select(TemporalToken.id).where(TemporalToken.note_id == note_id)
            )
        )
        .values(temporal_token_id=None)
    )
    await session.execute(delete(TemporalToken).where(TemporalToken.note_id == note_id))
    await session.execute(delete(EntityMention).where(EntityMention.note_id == note_id))
    await session.execute(delete(NoteAnalysis).where(NoteAnalysis.note_id == note_id))
    await _delete_orphaned_entities(session, candidates)
    # An orphaned appointment entity cascaded out with its row; a SURVIVING one
    # (still mentioned elsewhere) may have lost this note's scheduledTime, so
    # re-derive its projection — the row is removed when no live time remains.
    await project_appointments(session, candidates)
    await project_place_geofences(session, candidates)
    await _purge_episodes(session, note_id)


async def _purge_episodes(session: AsyncSession, note_id: uuid.UUID) -> None:
    """Purge is total (invariant #11): an episodic trace derived from this note is
    deleted WHOLE — the episode row, not merely its pointer — so no agent-memory
    row retains content derived from a deleted note. Refs cascade with the episode
    (the note FK can't: notes soft-delete, so its ON DELETE CASCADE never fires)."""
    await session.execute(
        delete(AgentEpisode).where(
            AgentEpisode.id.in_(
                select(AgentEpisodeRef.episode_id).where(AgentEpisodeRef.note_id == note_id)
            )
        )
    )


async def repair_chains(
    session: AsyncSession,
    doomed_links: dict[uuid.UUID, uuid.UUID | None],
    doomed_close: dict[uuid.UUID, datetime | None],
) -> None:
    """Fix surviving facts whose supersessor is doomed — purged by note
    deletion, or retracted by a re-extraction sweep (analysis/pipeline.py),
    which is why the survivor filter is "not itself doomed" rather than
    "other note": intra-note chains exist, and a same-note fact the sweep
    left alone still deserves repair.

    A survivor whose chain re-attaches to a NON-doomed fact deeper down stays
    superseded — the world still moved past it, just with one less link of
    evidence — so only the dangling pointer is repaired. A survivor whose
    chain dies inside the doomed set is restored: the only evidence it was
    ever superseded is being deleted (or retracted).
    """
    if not doomed_links:
        return
    survivors = (
        await session.execute(
            select(Fact.id, Fact.superseded_by, Fact.status, Fact.valid_to).where(
                Fact.superseded_by.in_(doomed_links), Fact.id.not_in(doomed_links)
            )
        )
    ).all()
    for s in survivors:
        target = chain_repair_target(s.superseded_by, doomed_links)
        if target is not None:
            # valid_to is left alone: the close may have copied the doomed
            # middle link's valid_from, but the surviving supersessor still
            # bounds the interval and inventing a new close here would be
            # guessing.
            await session.execute(update(Fact).where(Fact.id == s.id).values(superseded_by=target))
            continue
        values: dict[str, Any] = {"superseded_by": None}
        # Only a supersession is undone. retracted/pending_review are
        # verdicts about THIS fact (a human's, or re-extraction's), not
        # consequences of the doomed fact, so they survive with the dangling
        # link cleared.
        if s.status == "superseded":
            values["status"] = "active"
        # Honest approximation: the SCD-2 close copies the dooming fact's
        # valid_from, so equality is our only evidence the close came FROM
        # that fact rather than from the survivor's own note. On a match the
        # interval reopens; a coincidentally equal independent close would
        # reopen too, and we accept that over keeping an unevidenced close.
        if s.valid_to is not None and s.valid_to == doomed_close.get(s.superseded_by):
            values["valid_to"] = None
        await session.execute(update(Fact).where(Fact.id == s.id).values(**values))


async def delete_review_items(
    session: AsyncSession,
    doomed_ids: set[uuid.UUID],
    *,
    note_id: uuid.UUID | None = None,
    statuses: tuple[str, ...] | None = None,
) -> None:
    """Delete review items referencing doomed facts (payload fact_id /
    fact_a / fact_b), plus — when `note_id` is given — everything filed for
    the note itself.

    The purge passes note_id and no status filter: resolved history is
    derived data too, and its frozen display snippets quote the note's text.
    Items created by another note but referencing a doomed fact go as well,
    including the one-doomed-one-surviving case: such a card is unservable
    (one side's evidence is gone) and its choice labels quote the doomed
    fact's statement. The re-extraction sweep instead passes
    statuses=('open',) and no note_id: resolved/dismissed items are HUMAN
    history and survive a re-run.
    """
    status_clause = " AND status IN :statuses" if statuses is not None else ""
    if note_id is not None:
        stmt = text(
            f"DELETE FROM app.review_items WHERE payload->>'note_id' = :note{status_clause}"
        )
        params: dict[str, Any] = {"note": str(note_id)}
        if statuses is not None:
            stmt = stmt.bindparams(bindparam("statuses", expanding=True))
            params["statuses"] = list(statuses)
        await session.execute(stmt, params)
    if not doomed_ids:
        return
    ids = [str(d) for d in doomed_ids]
    stmt = text(
        "DELETE FROM app.review_items"
        " WHERE (payload->>'fact_id' IN :doomed_id"
        " OR payload->>'fact_a' IN :doomed_a"
        " OR payload->>'fact_b' IN :doomed_b)" + status_clause
    ).bindparams(
        bindparam("doomed_id", expanding=True),
        bindparam("doomed_a", expanding=True),
        bindparam("doomed_b", expanding=True),
    )
    params = {"doomed_id": ids, "doomed_a": ids, "doomed_b": ids}
    if statuses is not None:
        stmt = stmt.bindparams(bindparam("statuses", expanding=True))
        params["statuses"] = list(statuses)
    await session.execute(stmt, params)


def _orphan_conditions() -> list[Any]:
    """The criteria for a provisional entity that no surviving knowledge references —
    safe to hard-delete. Shared by the per-note purge (`_delete_orphaned_entities`)
    and the periodic global sweep (`sweep_orphaned_entities`), so they can never
    diverge: never an entity that is confirmed/subject-linked (the "Me" entity is
    sacrosanct), still mentioned or cited by a surviving fact, held by a distinct_from
    edge, or pointed at by a merge tombstone — that knowledge outlives any one note."""
    tombstone = aliased(Entity)
    return [
        Entity.status == "provisional",
        Entity.subject_id.is_(None),
        ~select(EntityMention.id).where(EntityMention.entity_id == Entity.id).exists(),
        ~select(Fact.id).where(Fact.entity_id == Entity.id).exists(),
        ~select(Fact.id).where(Fact.object_entity_id == Entity.id).exists(),
        ~select(EntityDistinction.id)
        .where(
            (EntityDistinction.entity_a == Entity.id) | (EntityDistinction.entity_b == Entity.id)
        )
        .exists(),
        ~select(tombstone.id).where(tombstone.merged_into_id == Entity.id).exists(),
    ]


async def _delete_orphaned_entities(session: AsyncSession, candidates: set[uuid.UUID]) -> None:
    """Provisional entities that existed only because of this note vanish;
    their aliases cascade. Restricts the shared orphan criteria to this note's
    candidate entities (`_orphan_conditions`)."""
    if not candidates:
        return
    await session.execute(delete(Entity).where(Entity.id.in_(candidates), *_orphan_conditions()))


async def sweep_orphaned_entities(
    maker: async_sessionmaker[AsyncSession], *, min_age_hours: int = 1, ctx: Any = None
) -> int:
    """The periodic global orphan sweep (the `entity_hygiene` action): delete EVERY
    provisional entity matching the shared orphan criteria, not only those tied to a
    just-deleted note. Closes the gap where a fact retraction or supersession strands a
    provisional entity (zero mentions/facts/edges) that the per-note purge never visits.

    Unlike the per-note purge (which acts on a just-deleted note's candidates, never racing
    live extraction), this runs corpus-wide and can fire — manually from Ops — *during* an
    extraction. So it adds an **age guard**: only entities older than `min_age_hours` are
    eligible, so a provisional entity an in-flight extraction just inserted but has not yet
    linked to its mention/fact is never deleted out from under it. Runs under SYSTEM_CTX;
    returns the count deleted. Idempotent — a second run finds nothing once the backlog
    clears."""
    from datetime import UTC, timedelta
    from typing import cast

    from sqlalchemy.engine import CursorResult

    from jbrain.db.session import scoped_session
    from jbrain.queue import SYSTEM_CTX

    cutoff = datetime.now(UTC) - timedelta(hours=min_age_hours)
    # Default SYSTEM_CTX (all domains); a narrowed ctx is firewalled by RLS to its scope,
    # so a domain-scoped sweep can only ever delete in-scope orphans (the firewall test).
    async with scoped_session(maker, ctx or SYSTEM_CTX) as session:
        result = await session.execute(
            delete(Entity).where(*_orphan_conditions(), Entity.created_at < cutoff)
        )
    return cast("CursorResult[Any]", result).rowcount or 0


async def backfill_deleted_note_artifacts(
    maker: async_sessionmaker[AsyncSession],
) -> int:
    """One-shot startup sweep: purge artifacts of notes deleted BEFORE the
    cascade existed. Idempotent — a fully purged note matches no candidate
    predicate — so running it every worker boot costs one cheap query once
    the backlog is clear."""
    from jbrain.db.session import scoped_session
    from jbrain.queue import SYSTEM_CTX

    candidate_sql = text(
        """
        SELECT n.id FROM app.notes n
        WHERE n.deleted_at IS NOT NULL AND (
            EXISTS (SELECT 1 FROM app.facts f WHERE f.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.entity_mentions m WHERE m.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.temporal_tokens t WHERE t.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.note_analysis a WHERE a.note_id = n.id)
            OR EXISTS (
                SELECT 1 FROM app.review_items r
                WHERE r.payload->>'note_id' = n.id::text
            )
        )
        """
    )
    async with scoped_session(maker, SYSTEM_CTX) as session:
        candidates = [row[0] for row in (await session.execute(candidate_sql)).all()]
    for note_id in candidates:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            await purge_note_artifacts(session, uuid.UUID(str(note_id)))
            await session.commit()
    return len(candidates)
