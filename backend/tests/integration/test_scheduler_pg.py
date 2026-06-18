"""The scheduler tick + emergency-trigger fire against real Postgres
(docs/WORKFLOW_ENGINE_PLAN.md §5 Track B): a due schedule claimed SKIP-LOCKED
enqueues its bound pipeline's action onto app.jobs and advances next_run_at
app-side, and the seeded nightly sweeps (migration 0037) are fireable on demand.

The clock is injected (a frozen `now`) so the advance is deterministic — no real
timer — exactly as the unit test does, but here against the real claim query and
the real app.jobs insert."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.agent.correctionmine import CORRECTION_MINE_SPEC
from jbrain.agent.predicatereview import PREDICATE_REVIEW_SPEC
from jbrain.agent.promptselfedit import PROMPT_SELF_EDIT_SPEC
from jbrain.agent.skilldistill import SKILL_DISTILL_SPEC
from jbrain.agent.skillsweep import SKILL_SWEEP_SPEC
from jbrain.analysis.hygiene import ENTITY_HYGIENE_SPEC
from jbrain.analysis.reembed import REEMBED_SPEC
from jbrain.analysis.tagconsolidate import TAG_CONSOLIDATE_SPEC
from jbrain.db.session import scoped_session
from jbrain.wiki.actions import WIKI_SPECS
from jbrain.workflow.evalaction import EVAL_RUN_SPEC
from jbrain.workflow.registry import ACTION_SPECS, build_registry
from jbrain.workflow.scheduler import (
    GEOFENCE_SWEEP_ACTION,
    PURGE_ACTION,
    RECONCILE_PENDING_INTEGRATION_ACTION,
    RECONCILE_PENDING_NOTES_ACTION,
    RECONCILE_UNEMBEDDED_NOTES_ACTION,
    ScheduleResolutionError,
    fire_trigger,
    reconcile_pending_integration_handler,
    reconcile_pending_notes_handler,
    reconcile_unembedded_notes_handler,
    scheduler_tick,
)
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NOW = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)


def _registry():  # noqa: ANN202
    return build_registry(
        (
            *ACTION_SPECS,
            PURGE_ACTION,
            RECONCILE_PENDING_NOTES_ACTION,
            RECONCILE_PENDING_INTEGRATION_ACTION,
            RECONCILE_UNEMBEDDED_NOTES_ACTION,
            GEOFENCE_SWEEP_ACTION,
            EVAL_RUN_SPEC,
            SKILL_DISTILL_SPEC,
            SKILL_SWEEP_SPEC,
            PREDICATE_REVIEW_SPEC,
            CORRECTION_MINE_SPEC,
            PROMPT_SELF_EDIT_SPEC,
            ENTITY_HYGIENE_SPEC,
            REEMBED_SPEC,
            TAG_CONSOLIDATE_SPEC,
            *WIKI_SPECS,
        )
    )


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_schedule(
    maker: async_sessionmaker,
    *,
    action: str,
    next_run_at: datetime,
    enabled: bool = True,
    manual: bool = True,
) -> dict[str, str]:
    """A schedule + its bound schedule-trigger + a one-action pipeline. Fresh ids
    per call so tests never collide on the seeded nightly rows."""
    ids = {k: str(uuid.uuid4()) for k in ("schedule", "trigger")}
    pipeline = f"test_pipeline_{ids['schedule'][:8]}"
    ids["pipeline"] = pipeline
    steps = f'[{{"action": "{action}", "action_version": 1, "params": {{}}}}]'
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.pipelines (name, version, steps)"
                " VALUES (:n, 1, cast(:st AS jsonb))"
            ),
            {"n": pipeline, "st": steps},
        )
        await s.execute(
            text(
                "INSERT INTO app.schedules (id, interval_seconds, next_run_at, enabled)"
                " VALUES (:id, 86400, :nr, :en)"
            ),
            {"id": ids["schedule"], "nr": next_run_at, "en": enabled},
        )
        await s.execute(
            text(
                "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
                " VALUES (:id, :sid, :p, :m)"
            ),
            {"id": ids["trigger"], "sid": ids["schedule"], "p": pipeline, "m": manual},
        )
    return ids


async def _jobs_of_kind(maker: async_sessionmaker, kind: str) -> int:
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        return (
            await s.execute(text("SELECT count(*) FROM app.jobs WHERE kind = :k"), {"k": kind})
        ).scalar_one()


async def _total_jobs(maker: async_sessionmaker) -> int:
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        return (await s.execute(text("SELECT count(*) FROM app.jobs"))).scalar_one()


async def _latest_payload_of_kind(maker: async_sessionmaker, kind: str) -> dict:
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        return (
            await s.execute(
                text(
                    "SELECT payload FROM app.jobs WHERE kind = :k ORDER BY created_at DESC LIMIT 1"
                ),
                {"k": kind},
            )
        ).scalar_one()


async def _jobs_for_note(maker: async_sessionmaker, kind: str, note_id: str) -> int:
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = :k AND payload ->> 'note_id' = :n"
                ),
                {"k": kind, "n": note_id},
            )
        ).scalar_one()


async def _seed_note(
    maker: async_sessionmaker, *, ingest_state: str, integration_state: str
) -> str:
    """A bare note in a given state, with NO ingest/integrate job and NO event —
    exactly the residue a dropped best-effort enqueue would leave behind. The
    reconciler must self-heal it off the state columns alone."""
    nid = str(uuid.uuid4())
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body,"
                " ingest_state, integration_state)"
                " VALUES (:i, :c, 'general', 'body', :ing, :int)"
            ),
            {"i": nid, "c": nid[:12], "ing": ingest_state, "int": integration_state},
        )
    return nid


async def _seed_unembedded_note(maker: async_sessionmaker) -> str:
    """A note with one NULL-embedding chunk and NO embed_note job — the residue a
    dropped embed enqueue (or a model-wipe) leaves. The unembedded reconciler must
    self-heal it off the chunk state alone."""
    nid = str(uuid.uuid4())
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body,"
                " ingest_state, integration_state)"
                " VALUES (:i, :c, 'general', 'body', 'indexed', 'integrated')"
            ),
            {"i": nid, "c": nid[:12]},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks"
                " (id, note_id, domain_code, granularity, seq, text, embedding)"
                " VALUES (gen_random_uuid(), :nid, 'general', 'paragraph', 0, 'c',"
                "         cast(NULL AS vector))"
            ),
            {"nid": nid},
        )
    return nid


async def test_due_schedule_enqueues_its_action_and_advances_next_run_at(
    maker: async_sessionmaker,
) -> None:
    # next_run_at one minute in the past relative to the injected NOW -> due.
    ids = await _seed_schedule(
        maker, action="sync_predicates", next_run_at=NOW - timedelta(minutes=1)
    )

    before = await _jobs_of_kind(maker, "sync_predicates")
    fired = await scheduler_tick(maker, _registry(), now=NOW)

    assert [f.pipeline for f in fired] == [ids["pipeline"]]
    assert len(fired[0].job_ids) == 1
    # The bound action was enqueued onto the real queue (kind = handler key).
    assert await _jobs_of_kind(maker, "sync_predicates") == before + 1

    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        row = (
            await s.execute(
                text("SELECT next_run_at, last_run_at FROM app.schedules WHERE id = :id"),
                {"id": ids["schedule"]},
            )
        ).first()
    # Advanced app-side off the INJECTED clock: last_run = NOW, next = NOW + 1 day.
    assert row is not None
    assert row.last_run_at == NOW
    assert row.next_run_at == NOW + timedelta(days=1)


async def test_tick_skips_a_not_yet_due_schedule(maker: async_sessionmaker) -> None:
    ids = await _seed_schedule(
        maker, action="consolidate_predicates", next_run_at=NOW + timedelta(hours=1)
    )
    before = await _jobs_of_kind(maker, "consolidate_predicates")
    fired = await scheduler_tick(maker, _registry(), now=NOW)
    assert all(f.trigger_id != ids["trigger"] for f in fired)
    assert await _jobs_of_kind(maker, "consolidate_predicates") == before


async def test_tick_skips_a_disabled_schedule(maker: async_sessionmaker) -> None:
    await _seed_schedule(
        maker,
        action="purge_deleted_artifacts",
        next_run_at=NOW - timedelta(hours=1),
        enabled=False,
    )
    before = await _jobs_of_kind(maker, "purge_deleted_artifacts")
    await scheduler_tick(maker, _registry(), now=NOW)
    assert await _jobs_of_kind(maker, "purge_deleted_artifacts") == before


async def test_fire_trigger_enqueues_immediately(maker: async_sessionmaker) -> None:
    # next_run_at in the future: fire_trigger ignores schedule timing entirely
    # (the emergency Ops path runs a sweep now regardless of cadence).
    ids = await _seed_schedule(
        maker, action="consolidate_predicates", next_run_at=NOW + timedelta(days=1)
    )
    before = await _jobs_of_kind(maker, "consolidate_predicates")
    fired = await fire_trigger(maker, _registry(), ids["trigger"])
    assert fired.pipeline == ids["pipeline"]
    assert await _jobs_of_kind(maker, "consolidate_predicates") == before + 1


async def test_fire_trigger_require_manual_rejects_a_non_manual_trigger(
    maker: async_sessionmaker,
) -> None:
    # The Ops emergency endpoint passes require_manual=True; a schedule-bound but
    # non-manual trigger must NOT be hand-fireable (the scheduler tick still fires it).
    ids = await _seed_schedule(
        maker,
        action="consolidate_predicates",
        next_run_at=NOW + timedelta(days=1),
        manual=False,
    )
    with pytest.raises(ScheduleResolutionError):
        await fire_trigger(maker, _registry(), ids["trigger"], require_manual=True)
    # ...but the tick path (no require_manual) fires it fine.
    fired = await fire_trigger(maker, _registry(), ids["trigger"])
    assert fired.pipeline == ids["pipeline"]


async def test_seeded_nightly_sweeps_exist_and_are_fireable(maker: async_sessionmaker) -> None:
    """The seed migrations register the nightly sweeps as manual, schedule-bound
    triggers; EVERY one is fireable on demand (the emergency Ops control), and each
    resolves against the worker-equivalent registry — the drift guard for the api/worker
    action-registry lockstep."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT t.id, t.pipeline FROM app.triggers t"
                    " JOIN app.schedules s ON s.id = t.on_schedule_id"
                    " WHERE t.manual AND t.pipeline LIKE 'nightly_%'"
                )
            )
        ).all()
    pipelines = {r.pipeline for r in rows}
    assert pipelines == {
        "nightly_consolidate_predicates",
        "nightly_sync_predicates",
        "nightly_purge_deleted_artifacts",
        "nightly_eval_run",
        "nightly_wiki_refresh",
        "nightly_wiki_prune",
        "nightly_skill_distill",
        "nightly_skill_sweep",
        "nightly_predicate_review",
        "nightly_correction_mine",
        "nightly_prompt_self_edit",
        "nightly_entity_hygiene",
        "nightly_reembed_stale",
        "nightly_tag_consolidate",
    }
    # EVERY seeded nightly trigger resolves + enqueues against the worker-equivalent
    # registry Ops fires against — the lockstep guard that no in-code action is left out
    # of the api/worker registry composition (else POST /ops/triggers/{id}/run would raise
    # ActionRegistryError on registry.get). Firing a manual trigger works even while its
    # schedule is disabled (the whole point of the emergency control).
    registry = _registry()
    before = await _total_jobs(maker)
    for r in rows:
        await fire_trigger(maker, registry, str(r.id))
    assert await _total_jobs(maker) == before + len(rows)


async def test_seeded_nightly_eval_exists_and_carries_its_run_params(
    maker: async_sessionmaker,
) -> None:
    """Migration 0044 seeds the nightly eval as a manual, schedule-bound trigger.
    Firing it enqueues an `eval_run` job whose payload carries the seeded run params
    (`suite`/`version_label`) — a scheduled trigger has no per-fire payload, so the
    step params ARE the payload the handler reads. The action is referenced by name
    through the in-code registry with NO app.actions row (like the sweeps)."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        trigger_id = (
            await s.execute(
                text(
                    "SELECT t.id FROM app.triggers t"
                    " JOIN app.schedules sc ON sc.id = t.on_schedule_id"
                    " WHERE t.manual AND t.pipeline = 'nightly_eval_run'"
                )
            )
        ).scalar_one()

    before = await _jobs_of_kind(maker, "eval_run")
    fired = await fire_trigger(maker, _registry(), str(trigger_id))
    assert fired.pipeline == "nightly_eval_run"
    assert await _jobs_of_kind(maker, "eval_run") == before + 1
    # The run params bound in the migration reach the job verbatim (the handler reads
    # payload["suite"]/["version_label"]); "all" scores the whole curated corpus.
    payload = await _latest_payload_of_kind(maker, "eval_run")
    assert payload == {"suite": "all", "version_label": "nightly"}


async def test_due_eval_schedule_enqueues_eval_run_via_tick(maker: async_sessionmaker) -> None:
    """The scheduled path (not just manual fire) delivers the seeded params: drive the
    real nightly-eval schedule due and tick — an `eval_run` job lands with the params."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        await s.execute(
            text(
                "UPDATE app.schedules SET next_run_at = :nr"
                " WHERE id = '00000000-0000-0000-0000-0000000c0017'"
            ),
            {"nr": NOW - timedelta(minutes=1)},
        )
    before = await _jobs_of_kind(maker, "eval_run")
    await scheduler_tick(maker, _registry(), now=NOW)
    assert await _jobs_of_kind(maker, "eval_run") == before + 1
    assert (await _latest_payload_of_kind(maker, "eval_run"))["version_label"] == "nightly"


# --- the dropped-event safety net (Wave 2 — the whole point of this task) -----


async def test_seeded_reconciler_sweeps_exist_and_are_fireable(maker: async_sessionmaker) -> None:
    """Migrations 0041/0042 seed the three reconcilers as recurring (300s), manual,
    schedule-bound triggers; each is fireable on demand from Ops."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT t.id, t.pipeline, s.interval_seconds FROM app.triggers t"
                    " JOIN app.schedules s ON s.id = t.on_schedule_id"
                    " WHERE t.manual AND t.pipeline LIKE 'reconcile_%'"
                )
            )
        ).all()
    by_pipeline = {r.pipeline: r for r in rows}
    assert set(by_pipeline) == {
        "reconcile_pending_notes",
        "reconcile_pending_integration",
        "reconcile_unembedded_notes",
    }
    # Recurring, not nightly: 5-minute cadence bounds dropped-event staleness.
    assert all(r.interval_seconds == 300 for r in rows)
    # Fire one on demand and confirm the reconcile job lands.
    before = await _jobs_of_kind(maker, "reconcile_pending_notes")
    await fire_trigger(maker, _registry(), str(by_pipeline["reconcile_pending_notes"].id))
    assert await _jobs_of_kind(maker, "reconcile_pending_notes") == before + 1


async def test_seeded_geofence_sweep_exists_and_is_fireable(maker: async_sessionmaker) -> None:
    """Migration 0064 seeds the geofence reconciler as a recurring (900s), manual,
    schedule-bound trigger. The pipeline must resolve through the in-code registry
    (no DispatchResolutionError) and firing it enqueues a geofence_sweep job."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        row = (
            await s.execute(
                text(
                    "SELECT t.id, s.interval_seconds FROM app.triggers t"
                    " JOIN app.schedules s ON s.id = t.on_schedule_id"
                    " WHERE t.manual AND t.pipeline = 'geofence_sweep'"
                )
            )
        ).one()
    assert row.interval_seconds == 900
    before = await _jobs_of_kind(maker, "geofence_sweep")
    fired = await fire_trigger(maker, _registry(), str(row.id))
    assert fired.pipeline == "geofence_sweep"
    assert await _jobs_of_kind(maker, "geofence_sweep") == before + 1


async def test_dropped_ingest_event_self_heals_and_is_idempotent(
    maker: async_sessionmaker,
) -> None:
    """The core guarantee: a note stuck in ingest_state='pending' with NO ingest
    job and NO event (a dropped best-effort enqueue) gets an ingest_note job when
    the reconciler runs — exactly once, and re-running never double-enqueues (E4).

    This drives the reconciler the way the schedule/trigger does: firing the
    trigger enqueues a reconcile_pending_notes job, whose handler is the backfill.
    Here we fire the trigger (proving the wiring) and then run the handler (proving
    the reconciliation), since the worker loop that would claim the reconcile job
    is not running in this test."""
    note_id = await _seed_note(
        maker, ingest_state="pending", integration_state="pending_integration"
    )
    ids = await _seed_schedule(
        maker, action="reconcile_pending_notes", next_run_at=NOW - timedelta(minutes=1)
    )

    # Firing the trigger enqueues exactly one reconcile_pending_notes job (the
    # schedule/Ops path), and no ingest job yet — that is the handler's work.
    fired = await fire_trigger(maker, _registry(), ids["trigger"])
    assert fired.pipeline == ids["pipeline"]
    assert await _jobs_for_note(maker, "ingest_note", note_id) == 0

    # Running the reconciler handler (what the worker would do on claiming that job)
    # self-heals the dropped-event note: exactly one ingest_note job appears.
    handler = reconcile_pending_notes_handler(maker)
    await handler({})
    assert await _jobs_for_note(maker, "ingest_note", note_id) == 1

    # Idempotent: a second run does NOT double-enqueue (the backfill skips notes
    # that already have an active ingest_note job).
    await handler({})
    assert await _jobs_for_note(maker, "ingest_note", note_id) == 1


async def test_dropped_integration_event_self_heals_and_is_idempotent(
    maker: async_sessionmaker,
) -> None:
    """Same guarantee for integration: an indexed-but-unintegrated note with NO
    integrate job and NO event gets exactly one integrate_note job, idempotently."""
    note_id = await _seed_note(
        maker, ingest_state="indexed", integration_state="pending_integration"
    )
    ids = await _seed_schedule(
        maker, action="reconcile_pending_integration", next_run_at=NOW - timedelta(minutes=1)
    )

    fired = await fire_trigger(maker, _registry(), ids["trigger"])
    assert fired.pipeline == ids["pipeline"]
    assert await _jobs_for_note(maker, "integrate_note", note_id) == 0

    handler = reconcile_pending_integration_handler(maker)
    await handler({})
    assert await _jobs_for_note(maker, "integrate_note", note_id) == 1

    await handler({})
    assert await _jobs_for_note(maker, "integrate_note", note_id) == 1


async def test_dropped_embed_self_heals_and_is_idempotent(
    maker: async_sessionmaker,
) -> None:
    """Track S: a note with NULL-embedding chunks and NO embed_note job (a dropped
    embed enqueue) gets exactly one embed_note job when the unembedded reconciler
    runs — and a second run never double-enqueues, because the backfill skips any
    note that already has an active embed_note job (E4)."""
    note_id = await _seed_unembedded_note(maker)
    ids = await _seed_schedule(
        maker, action="reconcile_unembedded_notes", next_run_at=NOW - timedelta(minutes=1)
    )

    # The schedule/Ops path enqueues exactly one reconcile_unembedded_notes job —
    # no embed_note job yet (that is the handler's work).
    fired = await fire_trigger(maker, _registry(), ids["trigger"])
    assert fired.pipeline == ids["pipeline"]
    assert await _jobs_for_note(maker, "embed_note", note_id) == 0

    handler = reconcile_unembedded_notes_handler(maker)
    await handler({})
    assert await _jobs_for_note(maker, "embed_note", note_id) == 1

    # Idempotent: fire twice, still one — the active embed_note job suppresses the
    # second enqueue.
    await handler({})
    assert await _jobs_for_note(maker, "embed_note", note_id) == 1
