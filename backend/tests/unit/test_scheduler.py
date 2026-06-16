"""Scheduler tick + trigger-fire logic with the DB and queue faked out.

The clock is injected (no real timer): a frozen `now` proves `next_run_at`
advances deterministically (N3) and the right job kind is enqueued. Real SQL
(the SKIP-LOCKED claim, the actual advance write) is integration-tested against
Postgres in tests/integration/test_scheduler_pg.py.
"""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jbrain import queue
from jbrain.workflow import scheduler
from jbrain.workflow.contracts import Pipeline, PipelineStep
from jbrain.workflow.registry import ACTION_SPECS, ActionRegistry, build_registry

NOW = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)


def _registry() -> ActionRegistry:
    # The worker's composed registry: the shipped six plus the in-code purge action
    # and the three reconciler actions (the dropped-event safety net, Wave 2 + Track S).
    return build_registry(
        (
            *ACTION_SPECS,
            scheduler.PURGE_ACTION,
            scheduler.RECONCILE_PENDING_NOTES_ACTION,
            scheduler.RECONCILE_PENDING_INTEGRATION_ACTION,
            scheduler.RECONCILE_UNEMBEDDED_NOTES_ACTION,
        )
    )


class FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class FakeSession:
    """Scripted session: each `execute` returns the next queued result; UPDATEs
    are recorded so the test can assert the advance write."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params or {}))
        return FakeResult(self._results.pop(0) if self._results else None)


class FakeDB:
    """Hands out a fresh scripted session per `scoped_session` call, in order."""

    def __init__(self, sessions: list[FakeSession]) -> None:
        self._sessions = list(sessions)
        self.used: list[FakeSession] = []

    @asynccontextmanager
    async def scoped(self, maker: Any, ctx: Any):  # noqa: ANN202
        assert ctx is queue.SYSTEM_CTX
        session = self._sessions.pop(0)
        self.used.append(session)
        yield session


class Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(maker: Any, ctx: Any, kind: str, payload: dict[str, Any]) -> str:
        assert ctx is queue.SYSTEM_CTX
        calls.append((kind, payload))
        return f"job-{len(calls)}"

    monkeypatch.setattr(scheduler.queue, "enqueue", fake_enqueue)
    yield calls


# --- the pure advance helper (the clock contract) ---------------------------


def test_advance_is_one_interval_out_from_injected_now() -> None:
    assert scheduler.advance(NOW, 86400) == NOW + timedelta(days=1)
    assert scheduler.advance(NOW, 30) == NOW + timedelta(seconds=30)


def test_advance_does_not_catch_up_missed_runs() -> None:
    # Three days late: the next run is still ONE interval from now, not a backlog.
    late = NOW + timedelta(days=3)
    assert scheduler.advance(late, 86400) == late + timedelta(days=1)


# --- fire_trigger -----------------------------------------------------------


async def test_fire_trigger_enqueues_the_pipeline_action(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[tuple[str, dict[str, Any]]]
) -> None:
    steps = '[{"action": "sync_predicates", "action_version": 1, "params": {}}]'
    db = FakeDB(
        [
            FakeSession([Row(pipeline="nightly_sync", enabled=True)]),
            FakeSession([Row(name="nightly_sync", version=1, steps=steps, description="")]),
        ]
    )
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)

    fired = await scheduler.fire_trigger(None, _registry(), "trig-1")  # type: ignore[arg-type]

    assert fired.pipeline == "nightly_sync"
    assert fired.job_ids == ["job-1"]
    # The enqueued job kind is the action's handler key — identical to a hardcoded
    # trigger's enqueue.
    assert enqueued == [("sync_predicates", {})]


async def test_fire_trigger_rejects_unknown_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB([FakeSession([None])])
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)
    with pytest.raises(scheduler.ScheduleResolutionError, match="no trigger"):
        await scheduler.fire_trigger(None, _registry(), "ghost")  # type: ignore[arg-type]


async def test_fire_trigger_rejects_disabled_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB([FakeSession([Row(pipeline="p", enabled=False)])])
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)
    with pytest.raises(scheduler.ScheduleResolutionError, match="disabled"):
        await scheduler.fire_trigger(None, _registry(), "trig-off")  # type: ignore[arg-type]


async def test_fire_trigger_rejects_missing_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(
        [
            FakeSession([Row(pipeline="gone", enabled=True)]),
            FakeSession([None]),  # pipeline lookup misses
        ]
    )
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)
    with pytest.raises(scheduler.ScheduleResolutionError, match="no pipeline"):
        await scheduler.fire_trigger(None, _registry(), "trig-2")  # type: ignore[arg-type]


# --- _enqueue_pipeline action resolution ------------------------------------


async def test_enqueue_pipeline_rejects_unregistered_action(
    enqueued: list[tuple[str, dict[str, Any]]],
) -> None:
    bad = Pipeline(
        name="p",
        version=1,
        steps=[PipelineStep(action="not_an_action", action_version=1)],
    )
    from jbrain.workflow.registry import ActionRegistryError

    with pytest.raises(ActionRegistryError):
        await scheduler._enqueue_pipeline(None, _registry(), bad)  # type: ignore[arg-type]
    assert enqueued == []  # all-or-nothing: nothing enqueued on a bad step


async def test_enqueue_pipeline_rejects_version_mismatch(
    enqueued: list[tuple[str, dict[str, Any]]],
) -> None:
    bad = Pipeline(
        name="p",
        version=1,
        steps=[PipelineStep(action="sync_predicates", action_version=99)],
    )
    with pytest.raises(scheduler.ScheduleResolutionError, match="pins action"):
        await scheduler._enqueue_pipeline(None, _registry(), bad)  # type: ignore[arg-type]
    assert enqueued == []


# --- scheduler_tick: claim + advance + fire ---------------------------------


async def test_tick_advances_next_run_at_app_side_off_injected_clock(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[tuple[str, dict[str, Any]]]
) -> None:
    steps = '[{"action": "consolidate_predicates", "action_version": 1, "params": {}}]'
    # 1: claim due schedule + advance, 2: empty re-query (drain), then fire_trigger's
    # two sessions (trigger lookup, pipeline lookup).
    claim = FakeSession(
        [Row(id="sch-1", interval_seconds=86400, trigger_id="trig-1", pipeline="p")]
    )
    drain = FakeSession([None])
    trig = FakeSession([Row(pipeline="p", enabled=True)])
    pipe = FakeSession([Row(name="p", version=1, steps=steps, description="")])
    db = FakeDB([claim, trig, pipe, drain])
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)

    fired = await scheduler.scheduler_tick(None, _registry(), now=NOW)  # type: ignore[arg-type]

    assert [f.pipeline for f in fired] == ["p"]
    assert enqueued == [("consolidate_predicates", {})]
    # The advance UPDATE wrote next_run_at = now + interval and last_run_at = now,
    # both off the INJECTED clock (no SQL now()).
    update_sql, update_params = next(
        (s, p) for s, p in claim.executed if "UPDATE app.schedules" in s
    )
    assert update_params["now"] == NOW
    assert update_params["next"] == NOW + timedelta(days=1)


async def test_tick_returns_empty_when_nothing_due(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB([FakeSession([None])])
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)
    assert await scheduler.scheduler_tick(None, _registry(), now=NOW) == []  # type: ignore[arg-type]


async def test_tick_advances_then_skips_a_schedule_with_no_trigger(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[tuple[str, dict[str, Any]]]
) -> None:
    # A dangling schedule (no enabled trigger) is advanced and skipped, not looped
    # on forever: the drain re-query returns nothing because it was advanced.
    claim = FakeSession([Row(id="sch-x", interval_seconds=3600, trigger_id=None, pipeline=None)])
    drain = FakeSession([None])
    db = FakeDB([claim, drain])
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)

    fired = await scheduler.scheduler_tick(None, _registry(), now=NOW)  # type: ignore[arg-type]
    assert fired == []
    assert enqueued == []
    assert any("UPDATE app.schedules" in s for s, _ in claim.executed)


# --- the two reconciler actions (the dropped-event safety net, Wave 2) -------


def test_reconciler_actions_resolve_in_the_composed_registry() -> None:
    # The worker composes both reconcilers into its registry; a pipeline that names
    # one must resolve to its handler key (and not be confused with the others).
    registry = _registry()
    for action in (
        "reconcile_pending_notes",
        "reconcile_pending_integration",
        "reconcile_unembedded_notes",
    ):
        spec = registry.get(action)
        assert spec.version == 1
        assert spec.handler == action
        assert spec.cost_class == "cheap"


async def test_reconcile_pending_notes_handler_calls_the_pending_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The handler is the ingest reconciler: it calls queue.backfill_pending_notes
    # under SYSTEM_CTX (reconciliation crosses every domain) and takes no payload.
    calls: list[Any] = []

    async def fake_backfill(maker: Any, ctx: Any) -> int:
        assert ctx is queue.SYSTEM_CTX
        calls.append(maker)
        return 0

    monkeypatch.setattr(scheduler.queue, "backfill_pending_notes", fake_backfill)
    sentinel = object()
    handler = scheduler.reconcile_pending_notes_handler(sentinel)  # type: ignore[arg-type]
    await handler({})
    assert calls == [sentinel]


async def test_reconcile_pending_integration_handler_calls_the_integration_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def fake_backfill(maker: Any, ctx: Any) -> int:
        assert ctx is queue.SYSTEM_CTX
        calls.append(maker)
        return 0

    monkeypatch.setattr(scheduler.queue, "backfill_pending_integration", fake_backfill)
    sentinel = object()
    handler = scheduler.reconcile_pending_integration_handler(sentinel)  # type: ignore[arg-type]
    await handler({})
    assert calls == [sentinel]


async def test_reconcile_unembedded_notes_handler_calls_the_unembedded_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The handler is the unembedded-notes reconciler (Track S): it calls
    # queue.backfill_unembedded_notes under SYSTEM_CTX and takes no payload.
    calls: list[Any] = []

    async def fake_backfill(maker: Any, ctx: Any) -> int:
        assert ctx is queue.SYSTEM_CTX
        calls.append(maker)
        return 0

    monkeypatch.setattr(scheduler.queue, "backfill_unembedded_notes", fake_backfill)
    sentinel = object()
    handler = scheduler.reconcile_unembedded_notes_handler(sentinel)  # type: ignore[arg-type]
    await handler({})
    assert calls == [sentinel]


async def test_fire_reconciler_trigger_enqueues_the_reconcile_action(
    monkeypatch: pytest.MonkeyPatch, enqueued: list[tuple[str, dict[str, Any]]]
) -> None:
    # End-to-end through fire_trigger: a trigger bound to the pending-notes
    # reconciler pipeline enqueues a reconcile_pending_notes job (kind = handler).
    steps = '[{"action": "reconcile_pending_notes", "action_version": 1, "params": {}}]'
    db = FakeDB(
        [
            FakeSession([Row(pipeline="reconcile_pending_notes", enabled=True, manual=True)]),
            FakeSession(
                [Row(name="reconcile_pending_notes", version=1, steps=steps, description="")]
            ),
        ]
    )
    monkeypatch.setattr(scheduler, "scoped_session", db.scoped)

    fired = await scheduler.fire_trigger(None, _registry(), "trig-recon")  # type: ignore[arg-type]

    assert fired.pipeline == "reconcile_pending_notes"
    assert enqueued == [("reconcile_pending_notes", {})]
