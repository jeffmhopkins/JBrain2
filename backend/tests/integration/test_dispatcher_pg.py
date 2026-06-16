"""The shadow dispatcher against real Postgres (W1·A2, docs/WORKFLOW_ENGINE_PLAN §E7a).

Proves end-to-end on real RLS + the real claim query that (post W2·C cutover):

- the note flow emits `app.events` rows (note.ingested) and indexes the note, but
  no longer enqueues integrate directly — the engine owns that path;
- a LIVE dispatcher tick claims the undispatched event, resolves it to the seeded
  event-bound trigger -> pipeline -> action, and enqueues EXACTLY ONE job, stamping
  `dispatched_at` and writing a pipeline run; the state/queued dedup skips a
  re-delivered or already-handled event (no double-processing);
- SHADOW (a rollback mode) still never enqueues;
- an unentitled-domain event fails closed (a shadow error) but is still marked
  dispatched, never enqueued;
- `app.events` is domain-firewalled: a health event is invisible to a
  general-scoped reader (the per-table RLS isolation test migration 0036 owes).
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from jbrain.workflow import dispatcher
from jbrain.workflow import events as wf_events
from jbrain.workflow.registry import ACTION_SPECS, build_registry
from jbrain.workflow.runlog import PipelineRunLog
from jbrain.workflow.scheduler import PURGE_ACTION
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


def _registry():  # noqa: ANN202
    return build_registry((*ACTION_SPECS, PURGE_ACTION))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


async def _seed_owner_principal(maker: async_sessionmaker[AsyncSession]) -> str:
    """A real owner principal so the worker-side emit (which has no per-content
    principal) can resolve one and satisfy the events FK. Returns its id."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (:id, 'owner', :kh) ON CONFLICT DO NOTHING"
            ),
            {"id": pid, "kh": f"disp-{pid}"},
        )
    return pid


async def _make_note(maker: async_sessionmaker[AsyncSession], *, domain: str, body: str) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"disp-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    return note.id


async def _undispatched_events(
    maker: async_sessionmaker[AsyncSession], *, type: str, note_id: str
) -> list[dict]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text AS id, domain_code, dispatched_at, payload::text AS payload"
                    " FROM app.events WHERE type = :t AND payload->>'note_id' = :nid"
                ),
                {"t": type, "nid": note_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def _count_jobs(maker: async_sessionmaker[AsyncSession], *, kind: str, note_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = :k AND payload->>'note_id' = :nid"
                ),
                {"k": kind, "nid": note_id},
            )
        ).scalar_one()


async def test_ingest_emits_event_and_indexes_without_a_direct_integrate(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """W2·C: ingest indexes the note and EMITS note.ingested, but no longer enqueues
    integrate directly — the engine owns that path now. So after ingest there is ZERO
    integrate job; the undispatched event is what will drive integration."""
    await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="health", body="blood pressure 120/80")

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as s:
        state = (
            await s.execute(
                text("SELECT ingest_state FROM app.notes WHERE id = :id"), {"id": note_id}
            )
        ).scalar_one()
    assert state == "indexed"
    # The direct integrate enqueue is gone: ingest enqueues no integrate job.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 0

    # The note.ingested event was emitted, carrying the note's domain (E2) and the
    # baseline the dispatcher diffs for observability.
    events = await _undispatched_events(maker, type=wf_events.NOTE_INGESTED, note_id=note_id)
    assert len(events) == 1
    assert events[0]["domain_code"] == "health"
    assert events[0]["dispatched_at"] is None
    assert wf_events.SHADOW_ENQUEUED_KEY in events[0]["payload"]


async def test_live_tick_drives_integration_from_an_ingest_event(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The cutover crux for note.ingested: a real ingest emits the event (no direct
    enqueue), and a LIVE dispatcher tick resolves it to the integrate pipeline and
    enqueues EXACTLY ONE integrate job — the engine is the integration trigger."""
    await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="dinner with sam")
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    # No integrate job yet — ingest only emitted the event.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 0

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_INGESTED]
    assert mine and all(d.error is None for d in mine)
    assert any(d.matches for d in mine)

    # LIVE: the engine enqueued exactly one integrate job for this note.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 1
    # And the event is drained from the undispatched set.
    events = await _undispatched_events(maker, type=wf_events.NOTE_INGESTED, note_id=note_id)
    assert len(events) == 1
    assert events[0]["dispatched_at"] is not None


async def test_dispatcher_drains_an_unbound_event_type_without_enqueue(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An event whose type binds no enabled trigger resolves to zero would-be
    enqueues: it is marked dispatched (drained, never left perpetually claimable)
    and nothing is enqueued. The events FK to app.domains already enforces a real
    domain at the DB; the dispatcher's own authorize_domain guard is unit-covered."""
    pid = await _seed_owner_principal(maker)
    event_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                " VALUES (:id, 'unbound.type', '{}'::jsonb, 'finance', :pid)"
            ),
            {"id": event_id, "pid": pid},
        )

    await dispatcher.dispatcher_tick(maker, _registry())

    async with scoped_session(maker, OWNER) as s:
        dispatched = (
            await s.execute(
                text("SELECT dispatched_at FROM app.events WHERE id = :id"), {"id": event_id}
            )
        ).scalar_one()
    assert dispatched is not None


async def test_resolution_emits_event_and_dispatcher_diffs_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The third trigger point: a predicate remap resolution emits a
    resolution.changed event (alongside the hardcoded consolidate enqueue), and the
    dispatcher resolves it to the seeded consolidate pipeline and diffs clean — with
    no second consolidate job (shadow)."""
    import json
    from datetime import UTC, datetime

    from jbrain.analysis.repo import SqlAnalysisRepo

    pid = await _seed_owner_principal(maker)
    # A ctx whose principal is the real owner row (so the emit's FK is satisfied).
    owner_ctx = SessionContext(principal_id=pid, principal_kind="owner")

    async with scoped_session(maker, owner_ctx) as s:
        await s.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind)"
                " VALUES ('spouse', 'd', 'ref', 'relationship')"
                " ON CONFLICT (canonical_name) DO NOTHING"
            )
        )
        ent = (
            await s.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                    " VALUES (gen_random_uuid(), 'Person', :n, 'confirmed', 'general')"
                    " RETURNING id::text"
                ),
                {"n": f"E-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()
        note = (
            await s.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'general', 'x') RETURNING id::text"
                ),
                {"cid": f"disp-{uuid.uuid4()}"},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO app.facts"
                " (id, entity_id, predicate, qualifier, kind, statement, assertion, status,"
                " reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'zzqMapMe', '', 'relationship', 's', 'asserted',"
                " 'active', :ts, :nid, 'test', 'v', 'general')"
            ),
            {"e": ent, "ts": datetime.now(UTC), "nid": uuid.UUID(note)},
        )
        card = (
            await s.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                    " VALUES (gen_random_uuid(), 'new_predicate', cast(:p AS jsonb), 'general')"
                    " RETURNING id::text"
                ),
                {
                    "p": json.dumps(
                        {"predicate": "zzqMapMe", "fact_kind": "relationship", "statement": "x"}
                    )
                },
            )
        ).scalar_one()

    await SqlAnalysisRepo(maker).resolve_review(
        owner_ctx, card, "map_to_existing", {"canonical_name": "spouse"}
    )

    # The resolution emitted a resolution.changed event for the dispatcher.
    async with scoped_session(maker, owner_ctx) as s:
        events = (
            await s.execute(
                text(
                    "SELECT domain_code FROM app.events WHERE type = :t AND dispatched_at IS NULL"
                ),
                {"t": wf_events.RESOLUTION_CHANGED},
            )
        ).all()
    assert any(e.domain_code == "general" for e in events)

    consolidate_before = await _count_consolidate(maker)
    diffs = await dispatcher.dispatcher_tick(maker, _registry())
    mine = [d for d in diffs if d.event_type == wf_events.RESOLUTION_CHANGED]
    assert mine and all(d.error is None for d in mine)
    assert any(d.matches for d in mine)
    # SHADOW: the dispatcher enqueued no additional consolidate job.
    assert await _count_consolidate(maker) == consolidate_before


async def _count_consolidate(maker: async_sessionmaker[AsyncSession]) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.jobs WHERE kind = 'consolidate_predicates'")
            )
        ).scalar_one()


async def _insert_event(
    maker: async_sessionmaker[AsyncSession],
    *,
    type: str,
    domain: str,
    principal_id: str,
    payload: dict,
) -> str:
    """Insert one undispatched event directly (no hardcoded enqueue alongside), so a
    LIVE tick is the ONLY thing that enqueues for it — the clean once-only path."""
    import json

    event_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                " VALUES (:id, :t, cast(:p AS jsonb), :d, :pid)"
            ),
            {"id": event_id, "t": type, "p": json.dumps(payload), "d": domain, "pid": principal_id},
        )
    return event_id


async def _pipeline_runs_for(
    maker: async_sessionmaker[AsyncSession], *, pipeline: str, note_id: str
) -> list[dict]:
    """Pipeline runs for `pipeline` whose enqueued job targets `note_id` — scoped to
    one note so a run written by a sibling test (the testcontainer is shared across
    the module) never leaks into this assertion."""
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT r.id::text AS id, r.kind, r.ran_as, r.domain_code,"
                    " r.principal_id::text AS principal_id, r.trigger_id::text AS trigger_id,"
                    " r.step_count, r.status"
                    " FROM app.runs r"
                    " JOIN app.run_steps rs ON rs.run_id = r.id"
                    " JOIN app.jobs j ON j.id = rs.job_id"
                    " WHERE r.kind = 'pipeline' AND r.pipeline = :p"
                    " AND j.payload->>'note_id' = :nid"
                ),
                {"p": pipeline, "nid": note_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def _run_step_job_ids(maker: async_sessionmaker[AsyncSession], *, run_id: str) -> list[str]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT job_id::text AS job_id, kind, name FROM app.run_steps"
                    " WHERE run_id = :rid ORDER BY idx"
                ),
                {"rid": run_id},
            )
        ).all()
    return [r.job_id for r in rows]


async def test_live_tick_enqueues_exactly_once_and_writes_a_pipeline_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """LIVE crux: an undispatched note.ingested event drives the seeded integrate
    pipeline to EXACTLY ONE integrate_note job, stamped with the event's scope, and
    a runs(kind='pipeline') + run_steps(job_id) row records the dispatch (§8)."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="live dispatch")
    # A synthetic event with NO hardcoded enqueue alongside, so the live tick owns
    # the only enqueue for this note.
    await _insert_event(
        maker,
        type=wf_events.NOTE_INGESTED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )

    before = await _count_jobs(maker, kind="integrate_note", note_id=note_id)
    assert before == 0

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_INGESTED]
    assert mine and all(d.error is None for d in mine)

    # Enqueued EXACTLY once, carrying the event's E1 stamp.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 1
    async with scoped_session(maker, OWNER) as s:
        stamp = (
            await s.execute(
                text(
                    "SELECT principal_id::text AS principal_id, domain_code FROM app.jobs"
                    " WHERE kind = 'integrate_note' AND payload->>'note_id' = :nid"
                ),
                {"nid": note_id},
            )
        ).first()
    assert stamp is not None and stamp.principal_id == pid and stamp.domain_code == "general"

    # A pipeline run + a step referencing the enqueued job were written.
    runs = await _pipeline_runs_for(maker, pipeline="event_integrate_note", note_id=note_id)
    assert len(runs) == 1
    run = runs[0]
    assert run["kind"] == "pipeline"
    assert run["ran_as"] == "scoped"
    assert run["domain_code"] == "general"
    assert run["principal_id"] == pid
    assert run["step_count"] == 1
    job_ids = await _run_step_job_ids(maker, run_id=run["id"])
    assert len(job_ids) == 1 and job_ids[0] is not None


async def test_live_tick_skips_a_target_with_an_active_job_no_double_enqueue(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """Idempotency under the cutover: a note already has a QUEUED integrate job (e.g.
    an on-demand /analyze, or a re-delivered event whose first dispatch is still
    queued) AND a note.ingested event is pending; the LIVE tick must SKIP its would-be
    integrate (the queued twin) — no double-enqueue, no run logged (E4)."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="dedup me")
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    # No integrate job from ingest (the direct enqueue is gone). Stand up a queued
    # integrate twin directly — the state the dispatcher's _already_active must honor.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload)"
                " VALUES (gen_random_uuid(), 'integrate_note',"
                " jsonb_build_object('note_id', cast(:nid AS text)))"
            ),
            {"nid": note_id},
        )
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 1
    # A note.ingested event for the same note (the ingest emit is best-effort and may
    # already be present; insert one explicitly so the tick has an event to resolve).
    await _insert_event(
        maker,
        type=wf_events.NOTE_INGESTED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_INGESTED]
    assert mine and all(d.error is None for d in mine)

    # SKIPPED on the queued twin: still exactly one integrate job — never doubled.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 1
    # And no pipeline run was written for the deduped (zero-enqueue) dispatch.
    assert await _pipeline_runs_for(maker, pipeline="event_integrate_note", note_id=note_id) == []


async def test_shadow_tick_never_enqueues_even_after_the_live_capability(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """SHADOW (the operator rollback mode, no longer the default) still never
    enqueues: the same synthetic event, dispatched with live=False, marks dispatched
    + diffs but submits nothing and writes no pipeline run."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="shadow only")
    await _insert_event(
        maker,
        type=wf_events.NOTE_INGESTED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )

    await dispatcher.dispatcher_tick(maker, _registry(), live=False, run_log=PipelineRunLog(maker))

    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 0
    assert await _pipeline_runs_for(maker, pipeline="event_integrate_note", note_id=note_id) == []
    # The event is still drained from the undispatched set.
    events = await _undispatched_events(maker, type=wf_events.NOTE_INGESTED, note_id=note_id)
    assert len(events) == 1 and events[0]["dispatched_at"] is not None


async def _set_note_state(
    maker: async_sessionmaker[AsyncSession],
    *,
    note_id: str,
    ingest_state: str,
    integration_state: str,
) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET ingest_state = :i, integration_state = :g WHERE id = :id"),
            {"i": ingest_state, "g": integration_state, "id": note_id},
        )


async def test_live_tick_state_skips_an_already_integrated_note_no_enqueue(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The W2·C state-based dedup: a re-delivered note.ingested event for a note that
    is ALREADY integrated (no queued twin survives) must NOT re-enqueue — the engine
    skips exactly what the integration reconciler (integration_state <> 'integrated')
    would not re-enqueue. No job, no pipeline run."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="already integrated")
    await _set_note_state(
        maker, note_id=note_id, ingest_state="indexed", integration_state="integrated"
    )
    await _insert_event(
        maker,
        type=wf_events.NOTE_INGESTED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_INGESTED]
    assert mine and all(d.error is None for d in mine)

    # State-skipped: no integrate job and no pipeline run for the skipped dispatch.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 0
    assert await _pipeline_runs_for(maker, pipeline="event_integrate_note", note_id=note_id) == []


async def test_live_tick_state_skips_a_note_past_pending_ingest_no_enqueue(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The ingest side of the W2·C state dedup: a re-delivered note.created event for
    a note already 'indexed' (past 'pending') must NOT re-ingest — the pending
    reconciler keys on ingest_state='pending', so the engine skips it too."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="already indexed")
    await _set_note_state(
        maker, note_id=note_id, ingest_state="indexed", integration_state="pending_integration"
    )
    await _insert_event(
        maker,
        type=wf_events.NOTE_CREATED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_CREATED]
    assert mine and all(d.error is None for d in mine)

    assert await _count_jobs(maker, kind="ingest_note", note_id=note_id) == 0
    assert await _pipeline_runs_for(maker, pipeline="event_ingest_note", note_id=note_id) == []


async def test_e2e_note_created_event_drives_ingest_with_a_logged_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """E2E for the note.created transition (W2·C, §5 Wave 2 bullet 3): an undispatched
    note.created event drives the seeded ingest pipeline to EXACTLY ONE ingest_note
    job + a logged runs(kind='pipeline') row whose step references that job. (The API
    emit is integration-tested via test_notes_rls; here the event is the input.)"""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="created -> ingest")
    await _insert_event(
        maker,
        type=wf_events.NOTE_CREATED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )
    assert await _count_jobs(maker, kind="ingest_note", note_id=note_id) == 0

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_CREATED]
    assert mine and all(d.error is None for d in mine)

    # Exactly one ingest_note job, and a pipeline run referencing it.
    assert await _count_jobs(maker, kind="ingest_note", note_id=note_id) == 1
    runs = await _pipeline_runs_for(maker, pipeline="event_ingest_note", note_id=note_id)
    assert len(runs) == 1 and runs[0]["step_count"] == 1
    job_ids = await _run_step_job_ids(maker, run_id=runs[0]["id"])
    assert len(job_ids) == 1 and job_ids[0] is not None


async def test_e2e_redelivered_event_is_exactly_once_under_live(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly-once under live (§5 Wave 2 bullet 3): two note.created events for the
    SAME note (a re-delivery) drive only ONE ingest_note job — the first dispatch
    enqueues, the second is suppressed by the queued-twin dedup (_already_active)."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="exactly once")
    for _ in range(2):
        await _insert_event(
            maker,
            type=wf_events.NOTE_CREATED,
            domain="general",
            principal_id=pid,
            payload={"note_id": note_id},
        )

    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))

    # Both events were dispatched, but only ONE ingest_note job exists.
    assert await _count_jobs(maker, kind="ingest_note", note_id=note_id) == 1
    runs = await _pipeline_runs_for(maker, pipeline="event_ingest_note", note_id=note_id)
    assert len(runs) == 1  # exactly one run logged for the one enqueue


async def test_e2e_a_failed_step_backs_off_and_surfaces_in_the_run_log(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A failed step backs off and is diagnosable from the run log (§5 Wave 2 bullet
    3): the live tick enqueues integrate + logs the run/step (job_id); when the
    executor FAILS that job (queue.fail), the failure surfaces THROUGH the run-step's
    job_id FK as last_error + a backed-off run_after — the run log drills straight to
    the failing job, no separate failure record needed."""
    pid = await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="will fail")
    await _insert_event(
        maker,
        type=wf_events.NOTE_INGESTED,
        domain="general",
        principal_id=pid,
        payload={"note_id": note_id},
    )

    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))
    runs = await _pipeline_runs_for(maker, pipeline="event_integrate_note", note_id=note_id)
    assert len(runs) == 1
    [job_id] = await _run_step_job_ids(maker, run_id=runs[0]["id"])
    assert job_id is not None

    # The executor claims and FAILS the enqueued integrate step (a retryable failure).
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.jobs SET status = 'running', locked_at = now() WHERE id = :id"),
            {"id": job_id},
        )
    exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job_id, "integrate boom")
    assert exhausted is False  # first attempt — backed off, not permanently failed

    # The failure is diagnosable from the run log: the run-step's job_id FK reaches
    # the job carrying last_error + a future run_after (backoff) + requeued status.
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    "SELECT j.last_error, j.status, j.attempts, j.run_after > now() AS backed_off"
                    " FROM app.run_steps rs JOIN app.jobs j ON j.id = rs.job_id"
                    " WHERE rs.run_id = :rid"
                ),
                {"rid": runs[0]["id"]},
            )
        ).first()
    assert row is not None
    assert row.last_error == "integrate boom"
    assert row.status == "queued"  # requeued for retry, not 'failed'
    assert row.attempts == 1
    assert row.backed_off is True  # exponential backoff pushed run_after into the future


async def test_e2e_resolution_event_drives_consolidate_with_a_logged_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """E2E for the resolution.changed transition: a synthetic resolution.changed event
    drives the seeded consolidate pipeline to a consolidate_predicates job + a logged
    pipeline run. (The resolution->emit path is covered in test_predicate_resolve_pg;
    here the event drives the engine to the sweep + run log.)"""
    pid = await _seed_owner_principal(maker)
    # No queued/running consolidate must pre-exist, else N1's kind-only dedup would
    # (correctly) suppress this enqueue — park any leftover from a sibling test.
    await _quiesce_consolidate_jobs(maker)
    await _insert_event(
        maker,
        type=wf_events.RESOLUTION_CHANGED,
        domain="general",
        principal_id=pid,
        payload={"item_id": str(uuid.uuid4())},
    )
    before = await _count_consolidate(maker)

    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))

    # The sweep was enqueued and a pipeline run recorded it (consolidate carries no
    # note_id, so assert on the run row + the job count directly).
    assert await _count_consolidate(maker) == before + 1
    async with scoped_session(maker, OWNER) as s:
        runs = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.runs"
                    " WHERE kind = 'pipeline' AND pipeline = 'event_consolidate_predicates'"
                )
            )
        ).scalar_one()
    assert runs >= 1


async def _quiesce_consolidate_jobs(maker: async_sessionmaker[AsyncSession]) -> None:
    """Park every consolidate job as 'done' (the module shares one testcontainer; a
    leftover queued/running sweep would suppress a sibling's expected enqueue via N1's
    kind-only dedup). DELETE is denied on app.jobs even under OWNER, so we park (the
    same trick test_queue_pg.quiesce_jobs uses)."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.jobs SET status = 'done' WHERE kind = 'consolidate_predicates'")
        )


async def _count_active_consolidate(maker: async_sessionmaker[AsyncSession]) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'consolidate_predicates'"
                    " AND status IN ('queued', 'running')"
                )
            )
        ).scalar_one()


async def test_live_tick_suppresses_a_second_consolidate_while_one_is_active(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """N1: consolidate_predicates is a payload-keyless idempotent sweep enqueued off
    resolution.changed on every remapping resolution. With a queued sweep already
    present, a fresh resolution.changed event must NOT pile up a second one — the
    kind-only dedup (_already_active -> has_active_kind) suppresses it."""
    pid = await _seed_owner_principal(maker)
    await _quiesce_consolidate_jobs(maker)
    # Stand up a QUEUED consolidate twin directly — the active sweep the dedup honors.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload, status)"
                " VALUES (gen_random_uuid(), 'consolidate_predicates', '{}'::jsonb, 'queued')"
            )
        )
    assert await _count_active_consolidate(maker) == 1

    await _insert_event(
        maker,
        type=wf_events.RESOLUTION_CHANGED,
        domain="general",
        principal_id=pid,
        payload={"item_id": str(uuid.uuid4())},
    )

    diffs = await dispatcher.dispatcher_tick(
        maker, _registry(), live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.RESOLUTION_CHANGED]
    assert mine and all(d.error is None for d in mine)

    # SUPPRESSED: still exactly one active consolidate — no second enqueued.
    assert await _count_active_consolidate(maker) == 1
    await _quiesce_consolidate_jobs(maker)


async def test_live_tick_suppresses_consolidate_while_one_is_running(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """N1 (running half): the kind-only dedup is queued OR running — a RUNNING sweep
    already covers the change a re-delivered resolution.changed event reflects, so a
    fresh dispatch is suppressed (unlike the note-keyed guard, which is queued-only)."""
    pid = await _seed_owner_principal(maker)
    await _quiesce_consolidate_jobs(maker)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload, status, locked_at)"
                " VALUES (gen_random_uuid(), 'consolidate_predicates', '{}'::jsonb,"
                " 'running', now())"
            )
        )
    assert await _count_active_consolidate(maker) == 1

    await _insert_event(
        maker,
        type=wf_events.RESOLUTION_CHANGED,
        domain="general",
        principal_id=pid,
        payload={"item_id": str(uuid.uuid4())},
    )

    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))

    assert await _count_active_consolidate(maker) == 1
    await _quiesce_consolidate_jobs(maker)


async def test_events_rls_isolates_by_domain(maker: async_sessionmaker[AsyncSession]) -> None:
    """events is domain-firewalled (migration 0036 RLS isolation, CLAUDE.md rule 3):
    a health event is invisible to a general-only reader and visible to a health
    reader. The per-table isolation test the events table owes for this wave's
    additive writes."""
    pid = await _seed_owner_principal(maker)
    health_event = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                " VALUES (:id, 'note.ingested', '{}'::jsonb, 'health', :pid)"
            ),
            {"id": health_event, "pid": pid},
        )

    async def visible(ctx: SessionContext) -> int:
        async with scoped_session(maker, ctx) as s:
            return (
                await s.execute(
                    text("SELECT count(*) FROM app.events WHERE id = :id"), {"id": health_event}
                )
            ).scalar_one()

    assert await visible(GENERAL_ONLY) == 0  # firewalled out
    assert await visible(HEALTH_ONLY) == 1  # in-scope reader sees it
    assert await visible(OWNER) == 1  # the owner crosses every firewall
