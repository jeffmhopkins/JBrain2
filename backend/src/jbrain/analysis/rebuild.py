"""The corpus-wide entity-graph rebuild sweep (the `graph_rebuild` action).

The missing instrument: re-derive the WHOLE entity graph from the notes, KEEPING the
notes. The only corpus-wide re-derive the owner had was Ops -> Reset, which drops the
schema and takes the notes with it — useless as an acceptance check on a pipeline
change, and useless as a rollback lever. This is the same job done non-destructively,
composed almost entirely from shipped parts:

    candidate note -> purge_note_artifacts(keep_pinned=True)
                   -> notes.integration_state = 'pending_integration'
                   -> (an EMR note also re-enqueues `emr_parse`)
                   -> backfill_pending_integration drains the re-integration
                   -> wiki_prune + wiki_rebuild('all') + wiki_refresh once it settles

**"The WHOLE graph" has to include the deterministic half.** A health `Records` note's
facts come from TWO producers, not one: the generic LLM extraction (`integrate_note`)
and the EMR parsers (`emr_parse`, migration 0122), which are what turn a lab PDF into
cited analyte readings. Both fan out from one `note.ingested` event at ingest, and only
the first has a re-drive path — so a purge that re-queued integration alone would return
a rebuilt EMR note holding only the LLM's read of a medical record, silently, while the
docstring claimed a whole-graph rebuild. So the sweep re-enqueues `emr_parse` for every
note that still matches stage 2's markers, in the note's own transaction (`_rebuild_one`).

One transaction per note (the `backfill_deleted_note_artifacts` shape), so a crash
resumes from the run's cursor instead of starting over and no note is ever left half
purged. Bounded per fire and self-continuing: each fire enqueues the next batch, so the
corpus drains without a terminal, and a recurring drain schedule picks a run back up if
a self-enqueue is ever lost.

**The wiki chain is not optional.** `wiki_citations.fact_id` is ON DELETE SET NULL
(migration 0046) and `wiki_articles.entity_ref` is a soft ref with no FK (0045), so
re-deriving the graph silently degrades every published revision to chunk-only claims
and points articles at entity ids that no longer exist. So the sweep does not end at
the last purge: it waits for re-integration to drain, queues the three-job wiki repair
(`_finish` — prune the orphans, rebuild the survivors, refresh the newly minted), and
only then marks the run complete.

**RLS.** Runs under SYSTEM_CTX — reconciliation legitimately crosses every domain (E1)
— and is a second consumer of the 0009 DELETE grants. It reaches Postgres only through
`purge_note_artifacts` and plain scoped sessions, so moving those deletes behind a
`SECURITY DEFINER` function later needs no change here. In this repo SECURITY DEFINER
means *bypassing* RLS (0045/0046), so such a function must re-assert
`app.has_domain_scope(domain_code)` on every row it touches: the caller's session scope
would no longer be the firewall.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import Row, bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.analysis.purge import purge_note_artifacts
from jbrain.db.session import scoped_session
from jbrain.ingest.pipeline import PDF_MEDIA_TYPE, ZIP_MEDIA_TYPES
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

# Notes purged per fire. Each is a handful of small deletes plus three projections, and
# a fire with work left self-continues immediately, so the batch bounds only how long
# one job holds the worker — not how fast the sweep drains.
GRAPH_REBUILD_BATCH = 25

# The job kind (== the action's handler key) the sweep self-enqueues to continue.
GRAPH_REBUILD_KIND = "graph_rebuild"

GRAPH_REBUILD_SPEC = ActionSpec(
    name="graph_rebuild",
    version=1,
    handler=GRAPH_REBUILD_KIND,
    domain_optional=True,
    mutating=True,  # purges every note's derived graph and re-drives integration
    cost_class="expensive",  # every note goes back through the Integrator
    dedup_key_expr=None,
    description="Re-derive the whole entity graph from the notes, keeping the notes.",
    category="maintenance",
)

# The candidate corpus: every live, indexed note. A never-analyzed note purges to a
# no-op and re-integrates normally, so no narrower predicate is warranted — and a
# narrower one would silently skip exactly the notes whose analysis is suspect.
_CANDIDATE_WHERE = "n.deleted_at IS NULL AND n.ingest_state = 'indexed'"

# The cursor orders by id, NOT created_at: created_at is the CLIENT's capture time
# (notes/repo.py), so an offline flush can insert a note behind the cursor and a
# created_at cursor would skip it. A uuid cursor is arbitrary but total and stable.
_BATCH_SQL = text(
    f"SELECT n.id FROM app.notes n WHERE {_CANDIDATE_WHERE}"
    " AND (CAST(:cursor AS uuid) IS NULL OR n.id > CAST(:cursor AS uuid))"
    " ORDER BY n.id LIMIT :lim"
)

_RUN_COLUMNS = (
    "SELECT id::text AS id, status, total_notes, notes_done, facts_purged, facts_kept,"
    " cursor_note_id::text AS cursor_note_id,"
    " (now() - started_at) > make_interval(hours => :deadline) AS overdue"
    " FROM app.graph_rebuild_runs"
)

# How long the drain phase waits for re-integration before chaining the wiki rebuild
# anyway. One note that can never integrate (a permanently failing extraction) would
# otherwise hold the chain open forever — and the damage that leaves is the worse
# outcome: every published revision degraded to chunk-only claims and articles pointing
# at entity ids the rebuild replaced. Generous enough that a real corpus drain finishes
# well inside it.
DRAIN_DEADLINE_HOURS = 24


@dataclass(frozen=True)
class RebuildProgress:
    """One fire's view of the run — `note` is what the Ops run log renders."""

    run_id: str | None
    status: str
    total: int
    done: int
    purged: int
    kept: int
    processed_now: int = 0

    @property
    def note(self) -> str:
        if self.run_id is None:
            return "no graph rebuild in progress"
        return (
            f"{self.status}: {self.done}/{self.total} notes rebuilt,"
            f" {self.purged} facts re-derived, {self.kept} pinned facts kept"
        )


IDLE = RebuildProgress(run_id=None, status="idle", total=0, done=0, purged=0, kept=0)


async def _active_run(maker: async_sessionmaker[AsyncSession]) -> Row[Any] | None:
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        return (
            await session.execute(
                text(
                    f"{_RUN_COLUMNS} WHERE status <> 'completed' ORDER BY started_at DESC LIMIT 1"
                ),
                {"deadline": DRAIN_DEADLINE_HOURS},
            )
        ).first()


async def start_run(maker: async_sessionmaker[AsyncSession]) -> str | None:
    """Open a rebuild run, or return None when one is already open.

    The partial unique index on `status <> 'completed'` is the real guard, so two
    simultaneous starts cannot both win — the loser reads the winner's row and
    continues it instead of opening a second run over the same corpus.
    """
    if await _active_run(maker) is not None:
        return None
    run_id = str(uuid.uuid4())
    try:
        async with scoped_session(maker, queue.SYSTEM_CTX) as session:
            total = (
                await session.execute(
                    text(f"SELECT count(*) FROM app.notes n WHERE {_CANDIDATE_WHERE}")
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO app.graph_rebuild_runs (id, status, total_notes)"
                    " VALUES (:id, 'purging', :total)"
                ),
                {"id": run_id, "total": total},
            )
            log.info("graph_rebuild_started", run_id=run_id, total_notes=total)
    except IntegrityError:
        # Lost the race against a concurrent start; the winner's run is the one to run.
        return None
    return run_id


async def _next_batch(
    maker: async_sessionmaker[AsyncSession], cursor: str | None, limit: int
) -> list[uuid.UUID]:
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        rows = await session.execute(_BATCH_SQL, {"cursor": cursor, "lim": limit})
        return [uuid.UUID(str(row[0])) for row in rows]


# Re-enqueue the deterministic EMR parse for a note that still matches stage 2's
# markers (migration 0122's `payload_equals` filter, read off the note itself rather
# than off a synthesized `note.ingested`): a health `Records` note holding a decrypted
# PDF and no archive. Re-emitting the ingest event would have been the other option and
# is worse — it would claim the chunks were rebuilt (they were not), hand integration a
# SECOND producer alongside the sweep's own `pending_integration` drain, and put stage 1
# back in scope. Enqueued directly, in the note's own transaction, so the crash window
# that leaves a note purged-but-unparsed does not exist.
#
# ORDERING. None is promised, and none is needed. The queue claims `ORDER BY run_after`
# and both jobs land in the same instant, exactly as at ingest, where both fan out from
# one event through a trigger query with no ORDER BY — so re-driving reproduces
# ingestion's interleaving rather than inventing a new one. The Layer-2 location
# firewall (ingest/emr/firewall.py) is a property of the PARSER's own lowering, not a
# pass over the graph: re-driving `emr_parse` restores it for exactly the facts it ever
# covered, and it never guarded the generic extraction — at ingest or after a rebuild.
_EMR_REPARSE_SQL = text(
    "INSERT INTO app.jobs (id, kind, payload)"
    " SELECT gen_random_uuid(), 'emr_parse', jsonb_build_object('note_id', n.id)"
    " FROM app.notes n"
    " WHERE n.id = CAST(:note AS uuid) AND n.domain_code = 'health'"
    "   AND n.destination = 'Records'"
    "   AND EXISTS (SELECT 1 FROM app.attachments a"
    "               WHERE a.note_id = n.id AND a.media_type = :pdf)"
    "   AND NOT EXISTS (SELECT 1 FROM app.attachments a"
    "                   WHERE a.note_id = n.id AND a.media_type IN :zips)"
    "   AND NOT EXISTS (SELECT 1 FROM app.jobs j WHERE j.kind = 'emr_parse'"
    "                   AND j.status IN ('queued', 'running')"
    "                   AND j.payload->>'note_id' = n.id::text)"
).bindparams(bindparam("zips", expanding=True))


async def _rebuild_one(
    maker: async_sessionmaker[AsyncSession], run_id: str, note_id: uuid.UUID
) -> None:
    """Rebuild one note in its OWN transaction: purge its derived artifacts, hand it
    back to the Integrator, and advance the run cursor — all or nothing, so a crash
    never leaves a note purged but un-queued and the resume point is always exact.

    An EMR note also gets its deterministic parse re-enqueued here (`_EMR_REPARSE_SQL`),
    inside the same transaction, so "purged but never re-parsed" is not a reachable
    state.

    Only `integration_state` is written to the note row. The wiki's dirty bit is
    `entities.wiki_built`,
    which 0046's triggers flip for us on the purge's fact/mention deletes;
    `notes.wiki_built` is a vestigial column with no reader anywhere in the backend or
    the PWA, so writing it here would look like re-dirtying the wiki while doing
    nothing at all."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        counts = await purge_note_artifacts(session, note_id, keep_pinned=True)
        await session.execute(
            text("UPDATE app.notes SET integration_state = 'pending_integration' WHERE id = :id"),
            {"id": str(note_id)},
        )
        await session.execute(
            _EMR_REPARSE_SQL,
            {"note": str(note_id), "pdf": PDF_MEDIA_TYPE, "zips": list(ZIP_MEDIA_TYPES)},
        )
        await session.execute(
            text(
                "UPDATE app.graph_rebuild_runs"
                " SET notes_done = notes_done + 1,"
                "     facts_purged = facts_purged + :purged,"
                "     facts_kept = facts_kept + :kept,"
                "     cursor_note_id = CAST(:note AS uuid)"
                " WHERE id = :run"
            ),
            # Read-modify-write on the run row. Two workers dragging the same run would
            # double-count these (and race the cursor backwards); the sweep self-enqueues
            # one job at a time, so the queue's own single-flight is what keeps that from
            # happening — the counters are progress display, not an invariant.
            {"purged": counts.purged, "kept": counts.kept, "note": str(note_id), "run": run_id},
        )
        await session.commit()


async def _integration_drained(maker: async_sessionmaker[AsyncSession]) -> bool:
    """Whether every rebuilt note is re-integrated. Both legs matter: a note can be
    un-integrated with no job yet (the reconciler enqueues 100 per call), and a job can
    be in flight for a note whose state has not flipped.

    `emr_parse` counts as in-flight work too: it writes facts and moves the EMR
    projections, and it flips no `integration_state` of its own, so a job leg that
    watched only `integrate_note` would chain the wiki repair over a graph the parser
    was still writing."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        pending = (
            await session.execute(
                text(
                    f"SELECT count(*) FROM app.notes n WHERE {_CANDIDATE_WHERE}"
                    " AND n.integration_state <> 'integrated'"
                )
            )
        ).scalar_one()
        if pending:
            return False
        running = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.jobs"
                    " WHERE kind IN ('integrate_note', 'emr_parse')"
                    " AND status IN ('queued', 'running')"
                )
            )
        ).scalar_one()
    return not running


async def _set_status(maker: async_sessionmaker[AsyncSession], run_id: str, status: str) -> None:
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        await session.execute(
            text("UPDATE app.graph_rebuild_runs SET status = :s WHERE id = CAST(:id AS uuid)"),
            {"s": status, "id": run_id},
        )


async def _finish(maker: async_sessionmaker[AsyncSession], run_id: str) -> None:
    """Close the run and chain into the wiki repair — the step that keeps published
    revisions from silently degrading to chunk-only claims (0046) and articles from
    pointing at entity ids the rebuild replaced (0045).

    THREE jobs, because no single one covers the damage:

    - `wiki_prune` archives articles whose `entity_ref` the sweep orphaned. It must run
      FIRST, and it is the only one that can: `wiki_rebuild('all')` iterates
      `entity_ref FROM wiki_articles WHERE status = 'active'` (wiki/builder.py), i.e.
      the very refs the sweep just killed — it cannot re-point or archive a dead one.
    - `wiki_rebuild('all')` re-derives every surviving article, which is what repairs
      the citations `wiki_citations.fact_id ON DELETE SET NULL` blanked.
    - `wiki_refresh` builds for entities the re-derivation newly MINTED. They have no
      article row, so `rebuild` never visits them; refresh is dirty-bit driven and
      0046's triggers already flipped `entities.wiki_built = false` on the sweep's
      deletes, so it finds them (and would eventually self-heal on its own schedule —
      chaining it here just means the owner does not wait for that window).

    The rebuild job's id is recorded on the run so the chain is auditable and a later
    fire never queues a second one.
    """
    await queue.enqueue(maker, queue.SYSTEM_CTX, "wiki_prune", {})
    job_id = await queue.enqueue(maker, queue.SYSTEM_CTX, "wiki_rebuild", {"target": "all"})
    await queue.enqueue(maker, queue.SYSTEM_CTX, "wiki_refresh", {})
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        await session.execute(
            text(
                "UPDATE app.graph_rebuild_runs"
                " SET status = 'completed', finished_at = now(),"
                "     wiki_rebuild_job_id = CAST(:job AS uuid)"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"job": job_id, "id": run_id},
        )
    log.info("graph_rebuild_complete", run_id=run_id, wiki_rebuild_job_id=job_id)


async def rebuild_batch(
    maker: async_sessionmaker[AsyncSession],
    *,
    start: bool = False,
    limit: int = GRAPH_REBUILD_BATCH,
) -> RebuildProgress:
    """One fire of the sweep. `start=True` opens a run first (the owner's Ops button);
    without it a fire only continues a run that is already open, so the recurring drain
    schedule stays inert until the owner asks for a rebuild.

    Idempotent and resumable: the run row carries the cursor, each note commits on its
    own, and a fire that finds no work returns zero — which the worker reads to reap the
    idle run from the Ops log.
    """
    if start:
        await start_run(maker)
    run = await _active_run(maker)
    if run is None:
        return IDLE

    processed = 0
    if run.status == "purging":
        # One row past the batch is the "more work" probe, so the lookahead costs no
        # second query.
        batch = await _next_batch(maker, run.cursor_note_id, limit + 1)
        more = len(batch) > limit
        for note_id in batch[:limit]:
            await _rebuild_one(maker, run.id, note_id)
            processed += 1
        if more:
            # The purge phase is the long one and every fire does real work, so
            # self-continuing here drains the corpus without busy-looping.
            await queue.enqueue(maker, queue.SYSTEM_CTX, GRAPH_REBUILD_KIND, {})
        else:
            await _set_status(maker, run.id, "draining")
        if processed:
            # Bounded at 100 notes/call: start re-integration alongside the purge
            # rather than waiting for the reconciler's own schedule.
            await queue.backfill_pending_integration(maker, queue.SYSTEM_CTX)

    run = await _active_run(maker)
    if run is None:
        return IDLE
    status = run.status
    if status == "draining":
        await queue.backfill_pending_integration(maker, queue.SYSTEM_CTX)
        if await _integration_drained(maker):
            await _finish(maker, run.id)
            status = "completed"
        elif run.overdue:
            # Past the deadline: a note that can never integrate must not hold the wiki
            # chain open, which is what leaves articles pointing at replaced entity ids.
            log.warning("graph_rebuild_drain_deadline", run_id=run.id, done=run.notes_done)
            await _finish(maker, run.id)
            status = "completed"
    return RebuildProgress(
        run_id=run.id,
        status=status,
        total=run.total_notes,
        done=run.notes_done,
        purged=run.facts_purged,
        kept=run.facts_kept,
        processed_now=processed,
    )


def graph_rebuild_handler(maker: async_sessionmaker[AsyncSession]) -> Any:
    """Worker dispatch entry for `graph_rebuild`.

    Two seeded pipelines drive it: the manual one passes `start: true` (the owner's
    "Run now" in Ops -> Automations), the recurring drain one passes nothing and is
    inert unless a run is open.

    The return value drives the idle reap (scheduler.REAPABLE_IDLE_SWEEPS), so it is
    keyed on WHETHER A RUN EXISTED, not on how many notes this fire moved. Returning
    the note count would reap the most important fire of all — the last one, which
    purges nothing, queues the wiki repair and marks the run complete — leaving the
    owner watching a run that never visibly finishes. It would also erase the whole
    run of a rebuild over an already-clean corpus, which then reads as "nothing
    happened" (CLAUDE.md rule 10: the Ops log is the only place they can see this).
    """

    async def run(
        payload: dict[str, Any], *, progress: Callable[[str], Awaitable[None]] | None = None
    ) -> int:
        result = await rebuild_batch(maker, start=bool(payload.get("start")))
        if result.run_id is None:
            return 0  # a drain poll with no open run: nothing to show the owner
        if progress is not None:
            await progress(result.note)
        return max(result.processed_now, 1)

    return run
