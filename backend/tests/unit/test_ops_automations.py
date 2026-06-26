"""The Automations Ops API with a fake reader on app.state — owner-only list +
catalog + the enable/disable toggles. The reader's SQL is integration-tested
against real Postgres (test_automations_reader_rls.py); here the DB is faked so the
test exercises the HTTP surface: auth (owner-only), the payload shapes, and the
toggle / 404 paths."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import keys, service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.workflow.automations import (
    ActionView,
    AutomationsView,
    AutomationView,
    RecentRunView,
    StepView,
)
from tests.unit.fakes import FakeAuthRepo

STARTED = datetime(2026, 6, 16, 2, 0, tzinfo=UTC)


class FakeAutomationsReader:
    """Records toggle calls and returns a fixed two-card view + catalog."""

    def __init__(self) -> None:
        self.trigger_toggles: list[tuple[str, bool]] = []
        self.schedule_toggles: list[tuple[str, bool]] = []
        self.schedule_updates: list[tuple[str, dict[str, object]]] = []
        self._view = AutomationsView(
            automations=[
                AutomationView(
                    trigger_id="e1",
                    kind="on_event",
                    group="event",
                    pipeline="event_ingest_note",
                    enabled=True,
                    manual=False,
                    steps=[
                        StepView(
                            action="ingest_note",
                            cost_class="standard",
                            description="Index a note.",
                            known=True,
                        )
                    ],
                    recent_runs=[
                        RecentRunView(
                            id="r1",
                            status="error",
                            started_at=STARTED,
                            duration_ms=31000,
                            last_error="integrate_note",
                        )
                    ],
                    on_event="note.created",
                ),
                AutomationView(
                    trigger_id="s1",
                    kind="schedule",
                    group="reconcile",
                    pipeline="reconcile_pending_notes",
                    enabled=False,
                    manual=True,
                    steps=[
                        StepView(
                            action="reconcile_pending_notes",
                            cost_class="cheap",
                            description="Re-enqueue ingest.",
                            known=True,
                        )
                    ],
                    recent_runs=[],
                    schedule_id="sched-1",
                    interval_seconds=300,
                    next_run_at=STARTED,
                    last_run_at=None,
                ),
            ],
            actions=[
                ActionView(
                    name="ingest_note",
                    cost_class="standard",
                    domain_optional=True,
                    mutating=True,
                    description="Index a note.",
                    seeded=True,
                ),
                ActionView(
                    name="reconcile_pending_notes",
                    cost_class="cheap",
                    domain_optional=True,
                    mutating=True,
                    description="Re-enqueue ingest.",
                    seeded=False,
                ),
            ],
        )

    async def load(self, ctx: object) -> AutomationsView:
        return self._view

    async def set_trigger_enabled(self, ctx: object, trigger_id: str, enabled: bool) -> bool:
        self.trigger_toggles.append((trigger_id, enabled))
        return trigger_id in {"e1", "s1"}

    async def set_schedule_enabled(self, ctx: object, schedule_id: str, enabled: bool) -> bool:
        self.schedule_toggles.append((schedule_id, enabled))
        return schedule_id == "sched-1"

    async def update_schedule(self, ctx: object, schedule_id: str, **spec: object) -> bool:
        self.schedule_updates.append((schedule_id, spec))
        return schedule_id == "sched-1"


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def reader() -> FakeAutomationsReader:
    return FakeAutomationsReader()


@pytest.fixture
def client(repo: FakeAuthRepo, reader: FakeAutomationsReader) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.automations_reader = reader
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


# --- owner-only (security path, 100%) ---------------------------------------


def test_automations_require_owner(client: TestClient) -> None:
    assert client.get("/api/ops/automations").status_code == 401
    assert client.get("/api/ops/actions").status_code == 401
    assert client.patch("/api/ops/triggers/e1", json={"enabled": False}).status_code == 401
    assert client.patch("/api/ops/schedules/sched-1", json={"enabled": False}).status_code == 401


def test_non_owner_forbidden(client: TestClient, repo: FakeAuthRepo) -> None:
    # A non-owner principal authenticates but the owner_only dep rejects it (403),
    # for both the reads and the mutations — the engine config is owner-only.
    asyncio.run(repo.create_principal("capability_token", "key-h", "agent"))
    principal = repo.principals[-1]
    token = "tok-agent"
    asyncio.run(repo.create_session(principal.id, keys.hash_token(token), "agent"))
    client.cookies.set("jbrain_session", token)
    assert client.get("/api/ops/automations").status_code == 403
    assert client.get("/api/ops/actions").status_code == 403
    assert client.patch("/api/ops/triggers/e1", json={"enabled": False}).status_code == 403
    assert client.patch("/api/ops/schedules/sched-1", json={"enabled": False}).status_code == 403
    assert (
        client.put("/api/ops/schedules/sched-1", json={"schedule_kind": "on_demand"}).status_code
        == 403
    )


# --- payload shapes ----------------------------------------------------------


def test_list_automations_shape(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/ops/automations")
    assert resp.status_code == 200
    body = resp.json()
    autos = body["automations"]
    assert [a["trigger_id"] for a in autos] == ["e1", "s1"]
    event = autos[0]
    assert event["kind"] == "on_event"
    assert event["group"] == "event"
    assert event["on_event"] == "note.created"
    assert event["manual"] is False  # event triggers are not manually fireable
    assert event["steps"][0]["cost_class"] == "standard"
    assert event["recent_runs"][0]["last_error"] == "integrate_note"
    sched = autos[1]
    assert sched["kind"] == "schedule"
    assert sched["manual"] is True
    assert sched["schedule_id"] == "sched-1"
    assert sched["interval_seconds"] == 300
    # The catalog is embedded and flags seeded vs in-code.
    actions = {a["name"]: a for a in body["actions"]}
    assert actions["ingest_note"]["seeded"] is True
    assert actions["reconcile_pending_notes"]["seeded"] is False


def test_actions_catalog_shape(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/ops/actions")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == ["ingest_note", "reconcile_pending_notes"]


# --- enable/disable toggles (security path, 100%) ---------------------------


def test_patch_trigger_toggles(client: TestClient, repo: FakeAuthRepo, reader) -> None:
    login(client, repo)
    resp = client.patch("/api/ops/triggers/e1", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json() == {"trigger_id": "e1", "enabled": False}
    assert reader.trigger_toggles == [("e1", False)]


def test_patch_trigger_unknown_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.patch("/api/ops/triggers/ghost", json={"enabled": True})
    assert resp.status_code == 404


def test_patch_schedule_toggles(client: TestClient, repo: FakeAuthRepo, reader) -> None:
    login(client, repo)
    resp = client.patch("/api/ops/schedules/sched-1", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"schedule_id": "sched-1", "enabled": True}
    assert reader.schedule_toggles == [("sched-1", True)]


def test_patch_schedule_unknown_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.patch("/api/ops/schedules/ghost", json={"enabled": True})
    assert resp.status_code == 404


# --- the schedule-spec editor (PUT /ops/schedules/{id}) ----------------------


def test_put_schedule_requires_owner(client: TestClient) -> None:
    # The spec editor is a real config mutation — owner-only like the toggles.
    assert (
        client.put(
            "/api/ops/schedules/sched-1",
            json={"schedule_kind": "repeat", "schedule_freq": "daily", "schedule_time": "07:00"},
        ).status_code
        == 401
    )


def test_put_schedule_sets_a_repeat_spec(
    client: TestClient, repo: FakeAuthRepo, reader: "FakeAutomationsReader"
) -> None:
    login(client, repo)
    resp = client.put(
        "/api/ops/schedules/sched-1",
        json={"schedule_kind": "repeat", "schedule_freq": "weekdays", "schedule_time": "07:00"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"schedule_id": "sched-1", "schedule_kind": "repeat"}
    sid, spec = reader.schedule_updates[0]
    assert sid == "sched-1"
    assert spec["schedule_kind"] == "repeat"
    assert spec["schedule_freq"] == "weekdays"
    assert spec["schedule_time"] == "07:00"
    # A repeat clears the legacy interval so the stored row is honest.
    assert spec["interval_seconds"] is None


def test_put_schedule_unknown_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.put(
        "/api/ops/schedules/ghost",
        json={"schedule_kind": "on_demand"},
    )
    assert resp.status_code == 404


def test_put_schedule_rejects_incoherent_specs(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    # repeat without a time, weekly without days, once without run_at, interval
    # without a positive interval_seconds — each fails validation (422) at the edge.
    bad_bodies = [
        {"schedule_kind": "repeat", "schedule_freq": "daily"},  # no time
        {"schedule_kind": "repeat", "schedule_freq": "weekly", "schedule_time": "07:00"},  # no days
        {"schedule_kind": "once"},  # no run_at
        {"schedule_kind": "interval", "interval_seconds": 0},  # non-positive
        {"schedule_kind": "bogus"},  # unknown kind
    ]
    for body in bad_bodies:
        assert client.put("/api/ops/schedules/sched-1", json=body).status_code == 422


def test_schedule_body_coerces_by_kind() -> None:
    # Pure validation: on_demand clears every spec field; interval clears the
    # wall-clock spec; the day list is sorted + de-duped and range-checked.
    from jbrain.api.ops import ScheduleBody

    on_demand = ScheduleBody(
        schedule_kind="on_demand", schedule_freq="daily", schedule_time="07:00"
    )
    assert on_demand.schedule_freq is None and on_demand.schedule_time is None

    interval = ScheduleBody(schedule_kind="interval", interval_seconds=300, schedule_time="07:00")
    assert interval.interval_seconds == 300 and interval.schedule_time is None

    weekly = ScheduleBody(
        schedule_kind="repeat",
        schedule_freq="weekly",
        schedule_time="09:00",
        schedule_days=[6, 0, 0],
    )
    assert weekly.schedule_days == [0, 6]

    with pytest.raises(ValueError, match="0..6"):
        ScheduleBody(schedule_kind="on_demand", schedule_days=[7])


# --- reader pure-logic edges (no DB needed) ---------------------------------


def test_resolve_steps_flags_registry_drift() -> None:
    from jbrain.workflow.automations import AutomationsReader
    from jbrain.workflow.registry import build_registry

    reader = AutomationsReader(object(), build_registry(), frozenset())  # type: ignore[arg-type]
    steps = reader._resolve_steps(
        [{"action": "ingest_note"}, {"action": "ghost_action"}],
    )
    assert steps[0].known is True and steps[0].cost_class == "standard"
    # An action the registry does not carry is surfaced as a known-unknown, not hidden.
    assert steps[1].known is False
    assert steps[1].action == "ghost_action"


async def test_toggle_bad_uuid_short_circuits() -> None:
    from jbrain.workflow.automations import AutomationsReader
    from jbrain.workflow.registry import build_registry

    # A non-UUID id returns False before opening a session, so the sentinel maker is
    # never touched (the guard against a malformed id reaching the DB).
    reader = AutomationsReader(object(), build_registry(), frozenset())  # type: ignore[arg-type]
    assert await reader.set_trigger_enabled(object(), "nope", True) is False  # type: ignore[arg-type]
    assert await reader.set_schedule_enabled(object(), "nope", True) is False  # type: ignore[arg-type]
