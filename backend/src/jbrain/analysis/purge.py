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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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

    await _repair_chains(session, note_id, doomed_links, doomed_close)
    await _delete_review_items(session, note_id, set(doomed_links))

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


async def _repair_chains(
    session: AsyncSession,
    note_id: uuid.UUID,
    doomed_links: dict[uuid.UUID, uuid.UUID | None],
    doomed_close: dict[uuid.UUID, datetime | None],
) -> None:
    """Fix surviving facts whose supersessor is about to vanish.

    A survivor whose chain re-attaches to a NON-doomed fact deeper down stays
    superseded — the world still moved past it, just with one less link of
    evidence — so only the dangling pointer is repaired. A survivor whose
    chain dies inside the doomed set is restored: the only evidence it was
    ever superseded is being deleted.
    """
    if not doomed_links:
        return
    survivors = (
        await session.execute(
            select(Fact.id, Fact.superseded_by, Fact.status, Fact.valid_to).where(
                Fact.superseded_by.in_(doomed_links), Fact.note_id != note_id
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
            await session.execute(
                update(Fact).where(Fact.id == s.id).values(superseded_by=target)
            )
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


async def _delete_review_items(
    session: AsyncSession, note_id: uuid.UUID, doomed_ids: set[uuid.UUID]
) -> None:
    """Delete review items derived from the note, in ANY status — resolved
    history is derived data too, and its frozen display snippets quote the
    note's text. Items created by another note but referencing a doomed fact
    go as well, including the one-doomed-one-surviving case: such a card is
    unservable (one side's evidence is gone) and its choice labels quote the
    doomed fact's statement."""
    await session.execute(
        text("DELETE FROM app.review_items WHERE payload->>'note_id' = :note"),
        {"note": str(note_id)},
    )
    if not doomed_ids:
        return
    ids = [str(d) for d in doomed_ids]
    stmt = text(
        "DELETE FROM app.review_items"
        " WHERE payload->>'fact_id' IN :doomed_id"
        " OR payload->>'fact_a' IN :doomed_a"
        " OR payload->>'fact_b' IN :doomed_b"
    ).bindparams(
        bindparam("doomed_id", expanding=True),
        bindparam("doomed_a", expanding=True),
        bindparam("doomed_b", expanding=True),
    )
    await session.execute(stmt, {"doomed_id": ids, "doomed_a": ids, "doomed_b": ids})


async def _delete_orphaned_entities(session: AsyncSession, candidates: set[uuid.UUID]) -> None:
    """Provisional entities that existed only because of this note vanish;
    their aliases cascade. Never deleted: confirmed or subject-linked
    entities (the "Me" entity is sacrosanct), merge tombstones (status
    'merged' — un-merge needs them), anything still mentioned or cited by a
    surviving fact, and anything held by a distinct_from edge or pointed at
    by a tombstone — that knowledge outlives the note."""
    if not candidates:
        return
    tombstone = aliased(Entity)
    await session.execute(
        delete(Entity).where(
            Entity.id.in_(candidates),
            Entity.status == "provisional",
            Entity.subject_id.is_(None),
            ~select(EntityMention.id).where(EntityMention.entity_id == Entity.id).exists(),
            ~select(Fact.id).where(Fact.entity_id == Entity.id).exists(),
            ~select(Fact.id).where(Fact.object_entity_id == Entity.id).exists(),
            ~select(EntityDistinction.id)
            .where(
                (EntityDistinction.entity_a == Entity.id)
                | (EntityDistinction.entity_b == Entity.id)
            )
            .exists(),
            ~select(tombstone.id).where(tombstone.merged_into_id == Entity.id).exists(),
        )
    )
