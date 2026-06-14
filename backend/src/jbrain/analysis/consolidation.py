"""Retroactive predicate consolidation: rewrite already-stored drift spellings
to the registry's canonical predicate (the `renamed_from` attractor).

This is the nightly counterpart to the parse-time normalization slice 1 applies
to NEW facts (docs/entity.md "The vocabulary invariant"): facts written under an
older prompt/model keep their drift spelling (`legalName`) until this sweep
moves them onto the canonical address (`name.legal`), so the supersession chain
for a property is not forked across spellings.

Conservative by construction. A drift fact is renamed IN PLACE — same row, same
id, citations intact — only when the canonical identity key
`(subject, entity, predicate, qualifier)` is otherwise empty for that entity.
When a canonical fact already exists there, renaming would entangle two
independent supersession chains, so the drift fact is LEFT ALONE and counted as
a collision for the merge machinery to resolve — never auto-merged here.

Trigger is deferred: today this runs as an enqueued action (boot self-heal);
recurring nightly + on-demand scheduling lands with the Phase-5 workflow engine
(docs/ROADMAP.md "Scheduled-task migration").
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.schema import SchemaRegistry, get_registry

log = structlog.get_logger()


def plan_renames(stored: set[str], registry: SchemaRegistry) -> dict[str, str]:
    """For each distinct stored predicate, the canonical it should become —
    only the ones that actually differ (the drift spellings)."""
    plan: dict[str, str] = {}
    for predicate in stored:
        canonical = registry.normalize_predicate(predicate)
        if canonical != predicate:
            plan[predicate] = canonical
    return plan


async def rewrite_predicate(session: AsyncSession, old: str, canonical: str) -> list[str]:
    """Move every drift fact under `old` onto `canonical` IN PLACE (same row, id,
    citations) where the canonical identity key is free, and return the rewritten
    fact ids. The one guarded rewrite shared by the nightly sweep and the
    new_predicate map_to_existing resolution, so their safety rules can't drift:

    - Pinned facts are human history and never move (CLAUDE.md #7); a retracted
      row is dead and not worth an address.
    - Only a LIVE canonical twin blocks the move. A superseded/dead tombstone on
      the canonical address must NOT strand the active drift chain there — that
      would fork the property's history, the exact harm this exists to prevent.
    """
    result = await session.execute(
        text(
            "UPDATE app.facts f SET predicate = :canon"
            " WHERE f.predicate = :old"
            "   AND f.pinned = false"
            "   AND f.status <> 'retracted'"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM app.facts g"
            "     WHERE g.entity_id = f.entity_id"
            "       AND g.subject_id IS NOT DISTINCT FROM f.subject_id"
            "       AND g.qualifier = f.qualifier"
            "       AND g.predicate = :canon"
            "       AND g.status IN ('active', 'pending_review')"
            "   )"
            " RETURNING f.id::text"
        ),
        {"canon": canonical, "old": old},
    )
    return list(result.scalars().all())


async def consolidate_predicates(session: AsyncSession) -> dict[str, int]:
    """Rename every drift predicate onto its canonical address where the target
    key is free. Returns counts of rows renamed and collisions left in place."""
    rows = (await session.execute(text("SELECT DISTINCT predicate FROM app.facts"))).all()
    plan = plan_renames({r.predicate for r in rows}, get_registry())

    renamed = 0
    # Sorted for determinism: when two drift spellings map to the SAME canonical
    # (legalName + legal_name -> name.legal), the first claims the key and the
    # second is left as a collision rather than merging two chains silently.
    for old, canonical in sorted(plan.items()):
        renamed += len(await rewrite_predicate(session, old, canonical))

    # A collision is a LIVE, non-pinned drift row that could not move because a
    # live canonical twin holds its address — the merge machinery's job. Counted
    # after all moves so a row blocked only by a sibling drift spelling that
    # sorted first is still surfaced, but retracted/pinned rows never inflate it.
    collisions = 0
    for old in plan:
        collisions += (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.facts"
                    " WHERE predicate = :old AND pinned = false AND status <> 'retracted'"
                ),
                {"old": old},
            )
        ).scalar_one()

    if renamed or collisions:
        log.info("analysis.consolidate_predicates", renamed=renamed, collisions=collisions)
    return {"renamed": renamed, "collisions": collisions}


class Consolidator:
    """The `consolidate_predicates` job handler (one sweep per run)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def run(self, payload: dict[str, Any]) -> None:
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await consolidate_predicates(session)
