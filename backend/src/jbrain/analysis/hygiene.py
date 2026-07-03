"""The `entity_hygiene` engine action (Phase-6 follow-on; docs/archive/HYGIENE_SWEEPS_PLAN.md).

Nightly maintenance, no LLM: hard-delete provisional orphan entities — those with no
mentions, no facts (as subject or object), no distinct_from edge, and no merge tombstone
pointing at them — that a fact retraction or supersession stranded. The per-note purge
(`analysis.purge`) only visits entities tied to a just-deleted note, so a provisional
entity left empty by a *retraction* (not a note deletion) is never cleaned up; this sweep
catches them, reusing the EXACT same safe criteria (`purge._orphan_conditions`).

Pure SQL (no tokens), so no self-improvement budget — like the reconcilers, it is core-data
maintenance, not self-improvement. Runs under SYSTEM_CTX; the schedule ships disabled and is
Ops-fireable. Nothing citable is ever touched: a confirmed/subject-linked entity, anything
mentioned or cited by a surviving fact, and merge tombstones are all excluded.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.purge import sweep_orphaned_entities
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

ENTITY_HYGIENE_SPEC = ActionSpec(
    name="entity_hygiene",
    version=1,
    handler="entity_hygiene",
    domain_optional=True,
    mutating=True,  # deletes provisional orphan entities
    cost_class="cheap",  # pure SQL, no LLM
    dedup_key_expr=None,
    description="Delete provisional orphan entities stranded by retraction/supersession.",
    category="maintenance",
)


def entity_hygiene_handler(maker: async_sessionmaker[AsyncSession]) -> Any:
    """Worker dispatch entry for `entity_hygiene` (payload-only Handler)."""

    async def run(_payload: dict[str, Any]) -> None:
        deleted = await sweep_orphaned_entities(maker)
        if deleted:
            log.info("entity_hygiene_swept", deleted=deleted)

    return run
