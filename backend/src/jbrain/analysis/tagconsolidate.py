"""The `tag_consolidate` engine action (Phase-6 follow-on; docs/HYGIENE_SWEEPS_PLAN.md).

Nightly maintenance, no LLM: fold drift spellings of a note's `tags` to one canonical form
— lowercase, collapse internal whitespace, trim, drop empties, de-duplicate — and rewrite the
array in place where it changed. So "Medication", "medication ", and "MEDICATION" become one
"medication" across the corpus. Deterministic and set-based (one UPDATE); idempotent (a second
run normalizes to the same sorted-distinct array, so nothing changes). Pure SQL, no tokens, no
self-improvement budget. Runs under SYSTEM_CTX; the schedule ships disabled and is Ops-fireable.

Deliberately conservative: only exact-after-normalization duplicates merge. Semantic merging
("med" ↔ "medication") would need an embedding-assisted tag registry that does not exist — a
named deferred follow-on, not built here (tags are not yet surfaced/searched).
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

TAG_CONSOLIDATE_SPEC = ActionSpec(
    name="tag_consolidate",
    version=1,
    handler="tag_consolidate",
    domain_optional=True,
    mutating=True,  # rewrites note_analysis.tags in place
    cost_class="cheap",  # pure SQL, no LLM
    dedup_key_expr=None,
    description="Fold drift spellings of note tags to one canonical (lower/trim/dedupe) form.",
)

# Normalize each tag (lowercase, collapse whitespace, trim), drop empties, and re-aggregate
# to a sorted distinct array; rewrite only the rows whose array actually changed. A note with
# no tags never appears in the subquery (unnest yields no rows), so it is untouched.
_CONSOLIDATE_SQL = (
    "UPDATE app.note_analysis na SET tags = sub.norm"
    " FROM ("
    "   SELECT na2.note_id,"
    "     coalesce("
    "       array_agg(DISTINCT n.norm ORDER BY n.norm) FILTER (WHERE n.norm <> ''), '{}'"
    "     ) AS norm"
    "   FROM app.note_analysis na2"
    "   CROSS JOIN LATERAL unnest(na2.tags) AS raw(t)"
    "   CROSS JOIN LATERAL (SELECT btrim(regexp_replace(lower(raw.t), '\\s+', ' ', 'g')) AS norm) n"
    "   GROUP BY na2.note_id"
    " ) sub"
    " WHERE na.note_id = sub.note_id AND na.tags IS DISTINCT FROM sub.norm"
)


def tag_consolidate_handler(maker: async_sessionmaker[AsyncSession]) -> Any:
    """Worker dispatch entry for `tag_consolidate` (payload-only Handler)."""

    async def run(_payload: dict[str, Any]) -> None:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            result = await session.execute(text(_CONSOLIDATE_SQL))
        rewritten = cast("CursorResult[Any]", result).rowcount
        if rewritten:
            log.info("tag_consolidate_swept", rewritten=rewritten)

    return run
