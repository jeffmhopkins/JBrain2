"""The shadow dispatcher's resolution + diff logic, DB/queue faked out (W1·A2).

Covers the three security-critical pure paths and the tick wiring without
Postgres: event->trigger resolution, shadow-diff equivalence against the recorded
hardcoded baseline (E7a), and the E1 domain-authorization fail-closed check the A3
stamp deferred to this layer. The real SKIP-LOCKED claim, the events insert, and
RLS are integration-tested against Postgres in tests/integration/test_dispatcher_pg.py.

The dispatcher MUST NOT enqueue in shadow mode — these tests assert that it never
calls queue.enqueue and only stamps dispatched_at.
"""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain import queue
from jbrain.workflow import dispatcher
from jbrain.workflow import events as wf_events
from jbrain.workflow.contracts import Pipeline, PipelineStep, TriggerFilter
from jbrain.workflow.registry import ACTION_SPECS, ActionRegistry, build_registry
from jbrain.workflow.scheduler import PURGE_ACTION

NOW = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)
PRINCIPAL = "11111111-1111-1111-1111-111111111111"


def _registry() -> ActionRegistry:
    return build_registry((*ACTION_SPECS, PURGE_ACTION))


def _event(
    *,
    type: str = wf_events.NOTE_CREATED,
    domain: str = "general",
    payload: dict[str, Any] | None = None,
) -> dispatcher._CandidateEvent:
    return dispatcher._CandidateEvent(
        id="ev-1",
        type=type,
        payload=payload or {"note_id": "n-1"},
        domain_code=domain,
        principal_id=PRINCIPAL,
    )


def _ingest_pipeline() -> Pipeline:
    return Pipeline(
        name="event_ingest_note",
        version=1,
        steps=[PipelineStep(action="ingest_note", action_version=1)],
    )


# --- event_matches: type + payload conjunctive filter -----------------------


def test_event_matches_empty_filter_accepts_any_type() -> None:
    assert dispatcher.event_matches(TriggerFilter(), wf_events.NOTE_CREATED, {})


def test_event_matches_requires_listed_type() -> None:
    f = TriggerFilter(event_types=[wf_events.NOTE_CREATED])
    assert dispatcher.event_matches(f, wf_events.NOTE_CREATED, {})
    assert not dispatcher.event_matches(f, wf_events.NOTE_INGESTED, {})


def test_event_matches_payload_equals_is_conjunctive() -> None:
    f = TriggerFilter(payload_equals={"note_id": "n-1"})
    assert dispatcher.event_matches(f, wf_events.NOTE_CREATED, {"note_id": "n-1", "x": 9})
    assert not dispatcher.event_matches(f, wf_events.NOTE_CREATED, {"note_id": "other"})


# --- authorize_domain: the E1 fail-closed check A3 deferred -----------------


def test_authorize_domain_accepts_a_real_domain_for_the_owner() -> None:
    assert dispatcher.authorize_domain(PRINCIPAL, "health") is None


def test_authorize_domain_rejects_unknown_domain() -> None:
    reason = dispatcher.authorize_domain(PRINCIPAL, "nonsense")
    assert reason is not None and "unknown domain" in reason


def test_authorize_domain_rejects_a_partial_stamp_fail_closed() -> None:
    # A principal with no domain (or vice versa) must never earn the all-domains
    # scope: narrowed_context raises ScopeStampError, which authorize_domain surfaces.
    assert dispatcher.authorize_domain(PRINCIPAL, "") is not None
    assert dispatcher.authorize_domain("", "health") is not None


# --- diff_pipeline: E3 registry-only resolution -----------------------------


def test_diff_pipeline_resolves_action_to_handler_kind_and_stamp() -> None:
    would, _ = dispatcher.diff_pipeline(_event(), _ingest_pipeline(), _registry())
    assert len(would) == 1
    assert would[0].kind == "ingest_note"
    assert would[0].payload == {"note_id": "n-1"}
    # The would-be job carries the event's E1 scope stamp.
    assert would[0].principal_id == PRINCIPAL
    assert would[0].domain_code == "general"


def test_diff_pipeline_strips_the_shadow_baseline_from_the_forwarded_payload() -> None:
    ev = _event(payload={"note_id": "n-1", wf_events.SHADOW_ENQUEUED_KEY: {"kind": "x"}})
    would, _ = dispatcher.diff_pipeline(ev, _ingest_pipeline(), _registry())
    assert would[0].payload == {"note_id": "n-1"}


def test_diff_pipeline_rejects_unregistered_action_e3() -> None:
    bad = Pipeline(name="p", version=1, steps=[PipelineStep(action="ghost", action_version=1)])
    with pytest.raises(dispatcher.DispatchResolutionError):
        dispatcher.diff_pipeline(_event(), bad, _registry())


def test_diff_pipeline_rejects_version_drift() -> None:
    bad = Pipeline(
        name="p", version=1, steps=[PipelineStep(action="ingest_note", action_version=99)]
    )
    with pytest.raises(dispatcher.DispatchResolutionError, match="pins action"):
        dispatcher.diff_pipeline(_event(), bad, _registry())


# --- compute_diff: shadow equivalence (E7a) ---------------------------------


def test_compute_diff_matches_when_engine_reproduces_the_hardcoded_enqueue() -> None:
    ev = _event(
        payload={
            "note_id": "n-1",
            wf_events.SHADOW_ENQUEUED_KEY: wf_events.shadow_enqueued(
                "ingest_note", {"note_id": "n-1"}
            ),
        }
    )
    would, _ = dispatcher.diff_pipeline(ev, _ingest_pipeline(), _registry())
    diff = dispatcher.compute_diff(ev, would)
    assert diff.matches
    assert diff.discrepancies == []


def test_compute_diff_flags_a_kind_mismatch() -> None:
    ev = _event(
        payload={
            "note_id": "n-1",
            wf_events.SHADOW_ENQUEUED_KEY: wf_events.shadow_enqueued(
                "integrate_note", {"note_id": "n-1"}
            ),
        }
    )
    would, _ = dispatcher.diff_pipeline(ev, _ingest_pipeline(), _registry())
    diff = dispatcher.compute_diff(ev, would)
    assert not diff.matches
    assert any("kind mismatch" in d for d in diff.discrepancies)


def test_compute_diff_flags_a_payload_mismatch() -> None:
    ev = _event(
        payload={
            "note_id": "n-1",
            wf_events.SHADOW_ENQUEUED_KEY: wf_events.shadow_enqueued(
                "ingest_note", {"note_id": "OTHER"}
            ),
        }
    )
    would, _ = dispatcher.diff_pipeline(ev, _ingest_pipeline(), _registry())
    diff = dispatcher.compute_diff(ev, would)
    assert not diff.matches
    assert any("payload mismatch" in d for d in diff.discrepancies)


def test_compute_diff_without_a_baseline_is_informational_not_a_mismatch() -> None:
    ev = _event(payload={"note_id": "n-1"})  # no _shadow_enqueued
    would, _ = dispatcher.diff_pipeline(ev, _ingest_pipeline(), _registry())
    diff = dispatcher.compute_diff(ev, would)
    assert diff.matches
    assert diff.actual is None


# --- resolve_event: full chain over a faked session -------------------------


class FakeResult:
    def __init__(self, rows: Any) -> None:
        self._rows = rows

    def all(self) -> Any:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Scripted session: each execute pops the next queued result; UPDATEs/inserts
    are recorded so a test can assert nothing was enqueued and dispatched_at set."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[str] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> FakeResult:
        self.executed.append(str(stmt))
        return self._results.pop(0) if self._results else FakeResult([])


class Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _trigger_row(pipeline: str, filter_: dict[str, Any]) -> Row:
    import json

    return Row(id="trig-1", pipeline=pipeline, filter=json.dumps(filter_))


def _pipeline_row(name: str, action: str) -> Row:
    return Row(
        name=name,
        version=1,
        steps=f'[{{"action": "{action}", "action_version": 1, "params": {{}}}}]',
        description="",
    )


async def test_resolve_event_matches_a_seeded_trigger_to_its_pipeline() -> None:
    session = FakeSession(
        [
            FakeResult([_trigger_row("event_ingest_note", {"event_types": ["note.created"]})]),
            FakeResult([_pipeline_row("event_ingest_note", "ingest_note")]),
        ]
    )
    ev = _event(
        payload={
            "note_id": "n-1",
            wf_events.SHADOW_ENQUEUED_KEY: wf_events.shadow_enqueued(
                "ingest_note", {"note_id": "n-1"}
            ),
        }
    )
    diff = await dispatcher.resolve_event(session, _registry(), ev)  # type: ignore[arg-type]
    assert diff.matches
    assert diff.error is None


async def test_resolve_event_fails_closed_on_unentitled_domain() -> None:
    # Authorization runs BEFORE any trigger lookup: a bad domain never touches a
    # pipeline. The session is never queried.
    session = FakeSession([])
    ev = _event(domain="bogus")
    diff = await dispatcher.resolve_event(session, _registry(), ev)  # type: ignore[arg-type]
    assert not diff.matches
    assert diff.error is not None and "unknown domain" in diff.error
    assert session.executed == []


async def test_resolve_event_rejects_a_trigger_that_refuses_the_event_domain_e2() -> None:
    # The trigger pins domains=['finance']; a general-domain event must not be fanned
    # into it (E2 accept-side, fail-closed).
    session = FakeSession(
        [
            FakeResult(
                [
                    _trigger_row(
                        "event_ingest_note",
                        {"event_types": ["note.created"], "domains": ["finance"]},
                    )
                ]
            ),
        ]
    )
    ev = _event(domain="general")
    diff = await dispatcher.resolve_event(session, _registry(), ev)  # type: ignore[arg-type]
    assert not diff.matches
    assert diff.error is not None and "does not accept domain" in diff.error


async def test_resolve_event_surfaces_an_unregistered_action_e3() -> None:
    session = FakeSession(
        [
            FakeResult([_trigger_row("p", {"event_types": ["note.created"]})]),
            FakeResult([_pipeline_row("p", "ghost_action")]),
        ]
    )
    diff = await dispatcher.resolve_event(session, _registry(), _event())  # type: ignore[arg-type]
    assert not diff.matches
    assert diff.error is not None


# --- dispatcher_tick: claim, diff, mark dispatched, NEVER enqueue -----------


class FakeDB:
    def __init__(self, sessions: list[FakeSession]) -> None:
        self._sessions = list(sessions)
        self.used: list[FakeSession] = []

    @asynccontextmanager
    async def scoped(self, maker: Any, ctx: Any):  # noqa: ANN202
        assert ctx is queue.SYSTEM_CTX
        session = self._sessions.pop(0)
        self.used.append(session)
        yield session


@pytest.fixture
def no_enqueue(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Trip-wire: the shadow dispatcher must NEVER enqueue. Any call is a failure."""
    calls: list[Any] = []

    async def fake_enqueue(*args: Any, **kw: Any) -> str:  # pragma: no cover - asserted empty
        calls.append((args, kw))
        return "should-not-happen"

    monkeypatch.setattr(dispatcher.queue, "enqueue", fake_enqueue)
    yield calls


async def test_tick_marks_dispatched_and_does_not_enqueue(
    monkeypatch: pytest.MonkeyPatch, no_enqueue: list[Any]
) -> None:
    import json

    claim_row = Row(
        id="ev-1",
        type="note.created",
        payload=json.dumps(
            {
                "note_id": "n-1",
                wf_events.SHADOW_ENQUEUED_KEY: {
                    "kind": "ingest_note",
                    "payload": {"note_id": "n-1"},
                },
            }
        ),
        domain_code="general",
        principal_id=PRINCIPAL,
    )
    # One claim session: claim event, list triggers, load pipeline, UPDATE dispatched.
    s1 = FakeSession(
        [
            FakeResult([claim_row]),  # _claim_event
            FakeResult([_trigger_row("event_ingest_note", {"event_types": ["note.created"]})]),
            FakeResult([_pipeline_row("event_ingest_note", "ingest_note")]),
            FakeResult([]),  # the UPDATE dispatched_at
        ]
    )
    s2 = FakeSession([FakeResult([])])  # drain: no more events
    db = FakeDB([s1, s2])
    monkeypatch.setattr(dispatcher, "scoped_session", db.scoped)

    diffs = await dispatcher.dispatcher_tick(None, _registry(), now=NOW)  # type: ignore[arg-type]

    assert len(diffs) == 1
    assert diffs[0].matches
    # Shadow: never enqueued.
    assert no_enqueue == []
    # dispatched_at was stamped.
    assert any("UPDATE app.events" in sql for sql in s1.executed)


async def test_tick_returns_empty_when_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB([FakeSession([FakeResult([])])])
    monkeypatch.setattr(dispatcher, "scoped_session", db.scoped)
    assert await dispatcher.dispatcher_tick(None, _registry(), now=NOW) == []  # type: ignore[arg-type]


# --- run_tick_safely: the workflow_dispatch gate + fault swallow ------------


class FakeSettings:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def get(self, ctx: Any, key: str, default: Any = None) -> Any:
        assert key == dispatcher.WORKFLOW_DISPATCH_KEY
        return self._value if self._value is not None else default


async def test_run_tick_safely_skips_when_gate_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[int] = []

    async def fake_tick(*a: Any, **k: Any) -> list[Any]:
        ran.append(1)
        return []

    monkeypatch.setattr(dispatcher, "dispatcher_tick", fake_tick)
    await dispatcher.run_tick_safely(None, _registry(), settings=FakeSettings(False))  # type: ignore[arg-type]
    assert ran == []


async def test_run_tick_safely_runs_when_gate_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[int] = []

    async def fake_tick(*a: Any, **k: Any) -> list[Any]:
        ran.append(1)
        return []

    monkeypatch.setattr(dispatcher, "dispatcher_tick", fake_tick)
    # value None -> the getter returns the default (ON for shadow).
    await dispatcher.run_tick_safely(None, _registry(), settings=FakeSettings(None))  # type: ignore[arg-type]
    assert ran == [1]


async def test_run_tick_safely_swallows_a_tick_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("db blip")

    monkeypatch.setattr(dispatcher, "dispatcher_tick", boom)
    # Must not raise: a dispatcher blip can never kill the worker loop.
    await dispatcher.run_tick_safely(None, _registry(), settings=FakeSettings(True))  # type: ignore[arg-type]


async def test_tick_marks_a_poison_event_dispatched_without_enqueue(
    monkeypatch: pytest.MonkeyPatch, no_enqueue: list[Any]
) -> None:
    import json

    # An event with an unentitled domain: fail-closed shadow error, still marked
    # dispatched (must not wedge the loop), never enqueued.
    poison = Row(
        id="ev-bad",
        type="note.created",
        payload=json.dumps({"note_id": "n-1"}),
        domain_code="bogus",
        principal_id=PRINCIPAL,
    )
    s1 = FakeSession([FakeResult([poison]), FakeResult([])])  # claim, then UPDATE
    s2 = FakeSession([FakeResult([])])
    db = FakeDB([s1, s2])
    monkeypatch.setattr(dispatcher, "scoped_session", db.scoped)

    diffs = await dispatcher.dispatcher_tick(None, _registry(), now=NOW)  # type: ignore[arg-type]
    assert len(diffs) == 1
    assert diffs[0].error is not None
    assert no_enqueue == []
    assert any("UPDATE app.events" in sql for sql in s1.executed)
