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
    """Scripts both gates: the master `workflow_dispatch` bool (via `get`) and the
    `workflow_dispatch_mode` typed getter the dispatcher reads to pick shadow/live/off."""

    def __init__(self, value: Any, *, mode: str = "shadow") -> None:
        self._value = value
        self._mode = mode

    async def get(self, ctx: Any, key: str, default: Any = None) -> Any:
        assert key == dispatcher.WORKFLOW_DISPATCH_KEY
        return self._value if self._value is not None else default

    async def workflow_dispatch_mode(self, ctx: Any) -> str:
        return self._mode


class FakeRunLog:
    """Records each pipeline run the live path writes (no DB)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, ctx: Any, **kw: Any) -> str:
        self.records.append(kw)
        return "run-1"


async def test_run_tick_safely_skips_when_gate_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[dict[str, Any]] = []

    async def fake_tick(*a: Any, **k: Any) -> list[Any]:
        ran.append(k)
        return []

    monkeypatch.setattr(dispatcher, "dispatcher_tick", fake_tick)
    await dispatcher.run_tick_safely(
        None,  # type: ignore[arg-type]
        _registry(),
        settings=FakeSettings(False),  # type: ignore[arg-type]
        run_log=FakeRunLog(),  # type: ignore[arg-type]
    )
    assert ran == []


async def test_run_tick_safely_skips_when_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Master switch on, mode "off": the tick is skipped entirely.
    ran: list[dict[str, Any]] = []

    async def fake_tick(*a: Any, **k: Any) -> list[Any]:
        ran.append(k)
        return []

    monkeypatch.setattr(dispatcher, "dispatcher_tick", fake_tick)
    await dispatcher.run_tick_safely(
        None,  # type: ignore[arg-type]
        _registry(),
        settings=FakeSettings(True, mode="off"),  # type: ignore[arg-type]
        run_log=FakeRunLog(),  # type: ignore[arg-type]
    )
    assert ran == []


async def test_run_tick_safely_runs_shadow_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default mode shadow: the tick runs with live=False (prod stays shadow).
    ran: list[dict[str, Any]] = []

    async def fake_tick(*a: Any, **k: Any) -> list[Any]:
        ran.append(k)
        return []

    monkeypatch.setattr(dispatcher, "dispatcher_tick", fake_tick)
    await dispatcher.run_tick_safely(
        None,  # type: ignore[arg-type]
        _registry(),
        settings=FakeSettings(None, mode="shadow"),  # type: ignore[arg-type]
        run_log=FakeRunLog(),  # type: ignore[arg-type]
    )
    assert len(ran) == 1
    assert ran[0]["live"] is False


async def test_run_tick_safely_runs_live_when_mode_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mode "live": the tick runs with live=True (the Wave-2 cutover flip).
    ran: list[dict[str, Any]] = []

    async def fake_tick(*a: Any, **k: Any) -> list[Any]:
        ran.append(k)
        return []

    monkeypatch.setattr(dispatcher, "dispatcher_tick", fake_tick)
    await dispatcher.run_tick_safely(
        None,  # type: ignore[arg-type]
        _registry(),
        settings=FakeSettings(True, mode="live"),  # type: ignore[arg-type]
        run_log=FakeRunLog(),  # type: ignore[arg-type]
    )
    assert len(ran) == 1
    assert ran[0]["live"] is True


async def test_run_tick_safely_swallows_a_tick_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("db blip")

    monkeypatch.setattr(dispatcher, "dispatcher_tick", boom)
    # Must not raise: a dispatcher blip can never kill the worker loop.
    await dispatcher.run_tick_safely(
        None,  # type: ignore[arg-type]
        _registry(),
        settings=FakeSettings(True),  # type: ignore[arg-type]
        run_log=FakeRunLog(),  # type: ignore[arg-type]
    )


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


# --- mode resolution: SqlSettingsStore.workflow_dispatch_mode ----------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, "live"),  # absent -> the cutover default (engine owns the path)
        ("shadow", "shadow"),
        ("live", "live"),
        ("off", "off"),
        ("garbage", "shadow"),  # junk fails closed to shadow, never live
        (True, "shadow"),  # wrong type -> shadow
    ],
)
async def test_workflow_dispatch_mode_resolves_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, stored: Any, expected: str
) -> None:
    from jbrain.settings_store import SqlSettingsStore

    store = SqlSettingsStore(maker=None)  # type: ignore[arg-type]

    async def fake_get(ctx: Any, key: str, default: Any = None) -> Any:
        return default if stored is None else stored

    monkeypatch.setattr(store, "get", fake_get)
    assert await store.workflow_dispatch_mode(queue.SYSTEM_CTX) == expected


# --- LIVE mode: dedup-skip, stamped enqueue, run-logging (W2·B) --------------


def _would(
    *, kind: str = "integrate_note", note_id: str | None = "n-1", scoped: bool = True
) -> dispatcher.WouldEnqueue:
    return dispatcher.WouldEnqueue(
        kind=kind,
        payload={"note_id": note_id} if note_id is not None else {},
        principal_id=PRINCIPAL if scoped else "",
        domain_code="general" if scoped else "",
        trigger_id="trig-1",
        pipeline=f"event_{kind}",
    )


@pytest.fixture
def captured_enqueue(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every queue.enqueue call (args + stamp) and hand back a job id."""
    calls: list[dict[str, Any]] = []

    async def fake_enqueue(
        maker: Any,
        ctx: Any,
        kind: str,
        payload: dict[str, Any],
        *,
        principal_id: str | None = None,
        domain_code: str | None = None,
    ) -> str:
        calls.append(
            {
                "kind": kind,
                "payload": payload,
                "principal_id": principal_id,
                "domain_code": domain_code,
            }
        )
        return f"job-{len(calls)}"

    monkeypatch.setattr(dispatcher.queue, "enqueue", fake_enqueue)
    yield calls


async def test_already_active_skips_an_integrate_with_a_queued_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    async def fake_has_active_analysis(
        maker: Any, ctx: Any, note_id: str, *, statuses: tuple[str, ...] = ()
    ) -> bool:
        seen.append((note_id, statuses))
        return True

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", fake_has_active_analysis)
    assert await dispatcher._already_active(None, _would(kind="integrate_note"))  # type: ignore[arg-type]
    # The guard mirrors the hardcoded path: note-keyed and QUEUED-only.
    assert seen == [("n-1", ("queued",))]


async def test_already_active_skips_an_ingest_with_a_queued_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_has_active(
        maker: Any,
        ctx: Any,
        kind: str,
        *,
        payload_field: str,
        value: str,
        statuses: tuple[str, ...],
    ) -> bool:
        seen.append({"kind": kind, "field": payload_field, "value": value, "statuses": statuses})
        return True

    monkeypatch.setattr(dispatcher.queue, "has_active", fake_has_active)
    assert await dispatcher._already_active(None, _would(kind="ingest_note"))  # type: ignore[arg-type]
    assert seen == [
        {"kind": "ingest_note", "field": "note_id", "value": "n-1", "statuses": ("queued",)}
    ]


def _consolidate_would() -> dispatcher.WouldEnqueue:
    # A payload-keyless sweep: no note_id, deduped kind-only (_KIND_DEDUP_KINDS).
    return dispatcher.WouldEnqueue(
        kind="consolidate_predicates",
        payload={},
        principal_id=PRINCIPAL,
        domain_code="general",
        trigger_id="trig-1",
        pipeline="p",
    )


async def test_already_active_false_for_a_kind_without_any_dedup_guard() -> None:
    # A kind in neither dedup set, with no note_id, is never suppressed here (its own
    # action owns dedup).
    w = dispatcher.WouldEnqueue(
        kind="some_other_sweep",
        payload={},
        principal_id=PRINCIPAL,
        domain_code="general",
        trigger_id="trig-1",
        pipeline="p",
    )
    assert not await dispatcher._already_active(None, w)  # type: ignore[arg-type]


async def test_already_active_kind_only_suppresses_a_sweep_with_an_active_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # N1: consolidate_predicates carries no per-target key, so it is deduped kind-only
    # — suppressed while one is queued OR running (the running sweep already covers
    # the change a re-delivered resolution.changed event reflects).
    seen: list[tuple[str, tuple[str, ...]]] = []

    # The fake mirrors the real helper's default statuses (ACTIVE_STATUSES = queued +
    # running), so this asserts the dispatcher leans on that default — it does NOT
    # narrow to queued-only as the note-keyed guard does.
    async def fake_has_active_kind(
        maker: Any, ctx: Any, kind: str, *, statuses: tuple[str, ...] = ("queued", "running")
    ) -> bool:
        seen.append((kind, statuses))
        return True

    monkeypatch.setattr(dispatcher.queue, "has_active_kind", fake_has_active_kind)
    assert await dispatcher._already_active(None, _consolidate_would())  # type: ignore[arg-type]
    # Kind-only check, leaning on the helper's default (queued+running) statuses.
    assert seen == [("consolidate_predicates", ("queued", "running"))]


async def test_already_active_kind_only_allows_a_sweep_with_no_active_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No queued/running sweep of that kind — the would-be enqueue proceeds.
    async def no_twin(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active_kind", no_twin)
    assert not await dispatcher._already_active(None, _consolidate_would())  # type: ignore[arg-type]


# --- _already_active: state-based dedup hardening (W2·C) ----------------------
# Under LIVE the queued-twin check is not enough: a re-delivered/duplicate event
# for a note whose work already finished (no queued twin survives) must not be
# re-processed. _already_active also skips exactly what the reconcilers would NOT
# re-enqueue — ingest past 'pending', integration already 'integrated'.


def _patch_state(monkeypatch: pytest.MonkeyPatch, state: tuple[str, str] | None) -> list[str]:
    """Stub dispatcher._note_state to return `state` and record the note_id it read.
    The real SELECT is integration-tested (test_dispatcher_pg); here we drive the
    skip decision off the returned state."""
    seen: list[str] = []

    async def fake_state(maker: Any, note_id: str) -> tuple[str, str] | None:
        seen.append(note_id)
        return state

    monkeypatch.setattr(dispatcher, "_note_state", fake_state)
    return seen


async def test_already_active_ingest_skips_a_note_past_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No queued twin, but the note is already 'indexed' — the pending reconciler
    # would not re-enqueue it, so neither does a live dispatch.
    async def no_twin(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active", no_twin)
    seen = _patch_state(monkeypatch, ("indexed", "pending_integration"))
    assert await dispatcher._already_active(None, _would(kind="ingest_note"))  # type: ignore[arg-type]
    assert seen == ["n-1"]


async def test_already_active_ingest_allows_a_still_pending_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No queued twin AND the note is still 'pending' — the pending reconciler WOULD
    # re-enqueue it, so the dispatcher must too: not suppressed.
    async def no_twin(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active", no_twin)
    _patch_state(monkeypatch, ("pending", "pending_integration"))
    assert not await dispatcher._already_active(None, _would(kind="ingest_note"))  # type: ignore[arg-type]


async def test_already_active_ingest_allows_a_missing_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An absent note (state None) is left to the handler's own missing-note no-op,
    # never suppressed on absence (which could strand a real, racing insert).
    async def no_twin(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active", no_twin)
    _patch_state(monkeypatch, None)
    assert not await dispatcher._already_active(None, _would(kind="ingest_note"))  # type: ignore[arg-type]


async def test_already_active_integrate_skips_an_already_integrated_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No queued twin, but integration already 'integrated' — past the integration
    # reconciler's `integration_state <> 'integrated'`, so suppressed.
    async def no_twin(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", no_twin)
    seen = _patch_state(monkeypatch, ("indexed", "integrated"))
    assert await dispatcher._already_active(None, _would(kind="integrate_note"))  # type: ignore[arg-type]
    assert seen == ["n-1"]


async def test_already_active_integrate_allows_a_not_yet_integrated_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No queued twin and not yet integrated — the reconciler WOULD re-enqueue, so
    # the dispatcher must too.
    async def no_twin(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", no_twin)
    _patch_state(monkeypatch, ("indexed", "pending_integration"))
    assert not await dispatcher._already_active(None, _would(kind="integrate_note"))  # type: ignore[arg-type]


async def test_already_active_queued_twin_short_circuits_before_state_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cheap queued-twin check fires first: a queued twin suppresses without ever
    # reading note state (one fewer query in the common back-to-back case).
    async def has_twin(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", has_twin)
    seen = _patch_state(monkeypatch, ("indexed", "pending_integration"))
    assert await dispatcher._already_active(None, _would(kind="integrate_note"))  # type: ignore[arg-type]
    assert seen == []  # state never read — the twin check short-circuited


def _allow_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub _note_state to a not-yet-finished state so the state-skip guard never
    suppresses — isolating these tests to the dedup/stamp/run-log path. The state
    skip itself is covered by the _already_active state tests above."""

    async def open_state(maker: Any, note_id: str) -> tuple[str, str]:
        # ingest_state 'pending' (the pending reconciler WOULD re-enqueue) and
        # integration_state not 'integrated' — neither guard suppresses.
        return ("pending", "pending_integration")

    monkeypatch.setattr(dispatcher, "_note_state", open_state)


async def test_live_enqueue_stamps_the_event_scope_and_runlogs(
    monkeypatch: pytest.MonkeyPatch, captured_enqueue: list[dict[str, Any]]
) -> None:
    async def no_active(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", no_active)
    _allow_state(monkeypatch)
    run_log = FakeRunLog()
    diff = dispatcher.ShadowDiff(
        event_id="ev-1",
        event_type="note.ingested",
        matches=True,
        enqueues=[_would(kind="integrate_note")],
    )
    await dispatcher.live_enqueue(None, diff, run_log=run_log)  # type: ignore[arg-type]

    # Exactly one enqueue, carrying the event's E1 stamp.
    assert len(captured_enqueue) == 1
    assert captured_enqueue[0]["kind"] == "integrate_note"
    assert captured_enqueue[0]["principal_id"] == PRINCIPAL
    assert captured_enqueue[0]["domain_code"] == "general"
    # One pipeline run row, kind-discriminated 'pipeline', referencing the job id.
    assert len(run_log.records) == 1
    rec = run_log.records[0]
    assert rec["pipeline"] == "event_integrate_note"
    assert rec["trigger_id"] == "trig-1"
    assert rec["ran_as"] == "scoped"
    assert rec["domain_code"] == "general"
    assert rec["principal_id"] == PRINCIPAL
    assert [s.job_id for s in rec["steps"]] == ["job-1"]


async def test_live_enqueue_skips_a_deduped_target_no_enqueue_no_runlog(
    monkeypatch: pytest.MonkeyPatch, captured_enqueue: list[dict[str, Any]]
) -> None:
    async def already(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", already)
    run_log = FakeRunLog()
    diff = dispatcher.ShadowDiff(
        event_id="ev-1",
        event_type="note.ingested",
        matches=True,
        enqueues=[_would(kind="integrate_note")],
    )
    await dispatcher.live_enqueue(None, diff, run_log=run_log)  # type: ignore[arg-type]
    # The target already has an active job: nothing enqueued, no run logged.
    assert captured_enqueue == []
    assert run_log.records == []


async def test_live_enqueue_system_event_runs_as_system_unstamped(
    monkeypatch: pytest.MonkeyPatch, captured_enqueue: list[dict[str, Any]]
) -> None:
    # A would-be enqueue with no principal/domain is a system enqueue: NULL stamp,
    # ran_as 'system', no domain/principal recorded on the run.
    async def no_active(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active", no_active)
    _allow_state(monkeypatch)
    run_log = FakeRunLog()
    diff = dispatcher.ShadowDiff(
        event_id="ev-1",
        event_type="note.created",
        matches=True,
        enqueues=[_would(kind="ingest_note", scoped=False)],
    )
    await dispatcher.live_enqueue(None, diff, run_log=run_log)  # type: ignore[arg-type]
    assert captured_enqueue[0]["principal_id"] is None
    assert captured_enqueue[0]["domain_code"] is None
    rec = run_log.records[0]
    assert rec["ran_as"] == "system"
    assert rec["domain_code"] is None
    assert rec["principal_id"] is None


async def test_tick_live_enqueues_exactly_once_via_diff(
    monkeypatch: pytest.MonkeyPatch, captured_enqueue: list[dict[str, Any]]
) -> None:
    import json

    async def no_active(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(dispatcher.queue, "has_active_analysis", no_active)
    _allow_state(monkeypatch)
    claim_row = Row(
        id="ev-1",
        type="note.ingested",
        payload=json.dumps(
            {
                "note_id": "n-1",
                wf_events.SHADOW_ENQUEUED_KEY: {
                    "kind": "integrate_note",
                    "payload": {"note_id": "n-1"},
                },
            }
        ),
        domain_code="general",
        principal_id=PRINCIPAL,
    )
    s1 = FakeSession(
        [
            FakeResult([claim_row]),
            FakeResult([_trigger_row("event_integrate_note", {"event_types": ["note.ingested"]})]),
            FakeResult([_pipeline_row("event_integrate_note", "integrate_note")]),
            FakeResult([]),  # UPDATE dispatched_at
        ]
    )
    s2 = FakeSession([FakeResult([])])  # drain
    db = FakeDB([s1, s2])
    monkeypatch.setattr(dispatcher, "scoped_session", db.scoped)
    run_log = FakeRunLog()

    diffs = await dispatcher.dispatcher_tick(
        None,  # type: ignore[arg-type]
        _registry(),
        now=NOW,
        live=True,
        run_log=run_log,  # type: ignore[arg-type]
    )

    assert len(diffs) == 1 and diffs[0].matches
    # LIVE: enqueued exactly once with the event's stamp, and one run logged.
    assert len(captured_enqueue) == 1
    assert captured_enqueue[0]["principal_id"] == PRINCIPAL
    assert len(run_log.records) == 1
    assert run_log.records[0]["steps"][0].job_id == "job-1"
