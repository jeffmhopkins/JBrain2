"""The /api/tasks router: payload validation (TaskBody) and the thin endpoint
handlers, driven directly with fakes on a stand-in request — no app, no DB."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from jbrain.api import tasks as tasks_api
from jbrain.api.tasks import GroupBody, ReorderBody, TaskBody
from jbrain.tasks.repo import TaskGroupInfo, TaskInfo, TaskRunInfo

NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)
PID = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = SimpleNamespace(id=PID, kind="owner")


def _task(**over: object) -> TaskInfo:
    base: dict[str, object] = dict(
        id="task-1",
        principal_id=PID,
        group_id=None,
        position=0,
        name="Brief",
        prompt="news",
        agent="jerv",
        domain_scopes=(),
        schedule_kind="on_demand",
        schedule_freq=None,
        schedule_days=(),
        schedule_time=None,
        run_at=None,
        timezone="UTC",
        enabled=True,
        notify_push=True,
        home_card=True,
        next_run_at=None,
        last_run_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(over)
    return TaskInfo(**base)  # type: ignore[arg-type]


# ---- TaskBody validation ----


def test_repeat_requires_freq_and_time() -> None:
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="repeat")
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="repeat", schedule_freq="weekly", schedule_time="07:00")
    ok = TaskBody(prompt="x", schedule_kind="repeat", schedule_freq="daily", schedule_time="07:00")
    assert ok.schedule_freq == "daily"


def test_once_requires_run_at() -> None:
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="once")
    ok = TaskBody(prompt="x", schedule_kind="once", run_at=datetime(2026, 7, 1, 9, tzinfo=UTC))
    assert ok.run_at is not None


def test_on_demand_clears_schedule_fields() -> None:
    body = TaskBody(
        prompt="x",
        schedule_kind="on_demand",
        schedule_freq="daily",
        schedule_time="07:00",
        schedule_days=[1, 2],
    )
    assert body.schedule_freq is None and body.schedule_time is None and body.schedule_days == []


def test_non_curator_drops_domain_scopes() -> None:
    body = TaskBody(prompt="x", agent="jerv", domain_scopes=["health"])
    assert body.domain_scopes == []
    curator = TaskBody(prompt="x", agent="curator", domain_scopes=["health"])
    assert curator.domain_scopes == ["health"]


def test_rejects_unknown_enums_and_days() -> None:
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", agent="rogue")
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="whenever")
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", agent="curator", domain_scopes=["secret"])
    with pytest.raises(ValidationError):
        TaskBody(
            prompt="x",
            schedule_kind="repeat",
            schedule_freq="weekly",
            schedule_time="07:00",
            schedule_days=[9],
        )


# ---- endpoint handlers ----


class FakeRepo:
    def __init__(self) -> None:
        self.tasks = {"task-1": _task()}
        self.deleted: list[str] = []
        self.marked: list[str] = []

    async def list(self, ctx):  # type: ignore[no-untyped-def]
        return list(self.tasks.values())

    async def create(self, ctx, **fields):  # type: ignore[no-untyped-def]
        return _task(id="task-2", name=fields.get("name", ""))

    async def get(self, ctx, task_id):  # type: ignore[no-untyped-def]
        return self.tasks.get(task_id)

    async def update(self, ctx, task_id, **fields):  # type: ignore[no-untyped-def]
        if task_id not in self.tasks:
            return None
        return _task(id=task_id, **{k: v for k, v in fields.items() if k == "enabled"})

    async def delete(self, ctx, task_id):  # type: ignore[no-untyped-def]
        self.deleted.append(task_id)

    async def mark_ran(self, ctx, task_id, *, at):  # type: ignore[no-untyped-def]
        self.marked.append(task_id)

    async def reorder(self, ctx, *, group_id, task_ids):  # type: ignore[no-untyped-def]
        self.reordered = (group_id, list(task_ids))
        return [_task(id=tid, group_id=group_id, position=i) for i, tid in enumerate(task_ids)]


class FakeGroups:
    def __init__(self) -> None:
        self.groups = {"g1": TaskGroupInfo(id="g1", name="Money", position=0)}
        self.deleted: list[str] = []

    async def list(self, ctx):  # type: ignore[no-untyped-def]
        return list(self.groups.values())

    async def create(self, ctx, *, name):  # type: ignore[no-untyped-def]
        return TaskGroupInfo(id="g2", name=name, position=1)

    async def rename(self, ctx, group_id, *, name):  # type: ignore[no-untyped-def]
        if group_id not in self.groups:
            return None
        return TaskGroupInfo(id=group_id, name=name, position=0)

    async def delete(self, ctx, group_id):  # type: ignore[no-untyped-def]
        self.deleted.append(group_id)


class FakeRunner:
    async def run(self, ctx, task, *, trigger):  # type: ignore[no-untyped-def]
        return TaskRunInfo(
            id="trun-1",
            task_id=task.id,
            session_id="sess-1",
            run_id="run-1",
            status="done",
            trigger=trigger,
            summary="ok",
            error=None,
            step_count=1,
            cost_tokens=5,
            started_at=NOW,
            ended_at=NOW,
        )


def _run(task_id: str) -> TaskRunInfo:
    return TaskRunInfo(
        id="lr",
        task_id=task_id,
        session_id="sess-latest",
        run_id=None,
        status="done",
        trigger="schedule",
        summary="latest",
        error=None,
        step_count=2,
        cost_tokens=0,
        started_at=NOW,
        ended_at=NOW,
    )


class FakeRuns:
    async def list_for_task(self, ctx, task_id, *, limit=20):  # type: ignore[no-untyped-def]
        return []

    async def latest_per_task(self, ctx, task_ids):  # type: ignore[no-untyped-def]
        return {tid: _run(tid) for tid in task_ids}


def _request(repo: FakeRepo, groups: "FakeGroups | None" = None) -> SimpleNamespace:
    state = SimpleNamespace(
        task_repo=repo,
        task_runner=FakeRunner(),
        task_runs=FakeRuns(),
        task_groups=groups or FakeGroups(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_list_and_create() -> None:
    repo = FakeRepo()
    req = _request(repo)
    out = await tasks_api.list_tasks(req, PRINCIPAL)  # type: ignore[arg-type]
    assert out[0].id == "task-1"
    # The latest run is embedded so the card's band renders without a per-card fetch.
    assert out[0].latest_run is not None and out[0].latest_run.session_id == "sess-latest"
    created = await tasks_api.create_task(req, PRINCIPAL, TaskBody(prompt="hi", name="New"))  # type: ignore[arg-type]
    assert created.name == "New"
    assert created.latest_run is None  # a brand-new task has never run


@pytest.mark.asyncio
async def test_patch_enabled_and_404() -> None:
    repo = FakeRepo()
    req = _request(repo)
    patch = tasks_api.EnabledPatch(enabled=False)
    out = await tasks_api.set_enabled(req, PRINCIPAL, "task-1", patch)  # type: ignore[arg-type]
    assert out.enabled is False
    assert out.latest_run is not None  # a toggle preserves the embedded latest run
    with pytest.raises(HTTPException):
        miss = tasks_api.EnabledPatch(enabled=True)
        await tasks_api.set_enabled(req, PRINCIPAL, "missing", miss)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_now_executes_and_marks_ran() -> None:
    repo = FakeRepo()
    req = _request(repo)
    run = await tasks_api.run_task(req, PRINCIPAL, "task-1")  # type: ignore[arg-type]
    assert run.status == "done" and run.trigger == "manual"
    assert repo.marked == ["task-1"]
    with pytest.raises(HTTPException):
        await tasks_api.run_task(req, PRINCIPAL, "missing")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delete_and_runs() -> None:
    repo = FakeRepo()
    req = _request(repo)
    await tasks_api.delete_task(req, PRINCIPAL, "task-1")  # type: ignore[arg-type]
    assert repo.deleted == ["task-1"]
    assert await tasks_api.task_runs(req, PRINCIPAL, "task-1") == []  # type: ignore[arg-type]


# ---- groups + reorder (Direction B) ----


@pytest.mark.asyncio
async def test_group_crud() -> None:
    groups = FakeGroups()
    req = _request(FakeRepo(), groups)
    listed = await tasks_api.list_task_groups(req, PRINCIPAL)  # type: ignore[arg-type]
    assert [g.name for g in listed] == ["Money"]
    created = await tasks_api.create_task_group(req, PRINCIPAL, GroupBody(name="  Health  "))  # type: ignore[arg-type]
    assert created.name == "Health"  # the name is trimmed at the edge
    renamed = await tasks_api.rename_task_group(req, PRINCIPAL, "g1", GroupBody(name="Finance"))  # type: ignore[arg-type]
    assert renamed.name == "Finance"
    with pytest.raises(HTTPException):
        await tasks_api.rename_task_group(req, PRINCIPAL, "missing", GroupBody(name="x"))  # type: ignore[arg-type]
    await tasks_api.delete_task_group(req, PRINCIPAL, "g1")  # type: ignore[arg-type]
    assert groups.deleted == ["g1"]


@pytest.mark.asyncio
async def test_reorder_sets_group_and_position() -> None:
    repo = FakeRepo()
    req = _request(repo)
    body = ReorderBody(group_id="g1", task_ids=["task-3", "task-1"])
    out = await tasks_api.reorder_tasks(req, PRINCIPAL, body)  # type: ignore[arg-type]
    assert repo.reordered == ("g1", ["task-3", "task-1"])
    # Order returned reflects the sent sequence, each stamped with its list index.
    assert [(t.id, t.group_id, t.position) for t in out] == [
        ("task-3", "g1", 0),
        ("task-1", "g1", 1),
    ]

    # A NULL target moves tasks back to the Ungrouped bucket.
    back = ReorderBody(group_id=None, task_ids=["task-1"])
    ungrouped = await tasks_api.reorder_tasks(req, PRINCIPAL, back)  # type: ignore[arg-type]
    assert ungrouped[0].group_id is None


# ---- radio commands (APRS_CONTROL_PLAN.md P4) ----------------------------------------
# The fourth kind carries a credential, so the validation here is a security boundary
# rather than a convenience: what the owner may configure, what the wire may not carry,
# and what an edit does to a key that a truck is already set up with.


def _command_body(**over: object) -> TaskBody:
    fields: dict[str, object] = {
        "prompt": "open the gate",
        "schedule_kind": "on_command",
        "command_word": "gate",
        **over,
    }
    return TaskBody(**fields)  # type: ignore[arg-type]


def test_a_command_needs_a_word() -> None:
    # Without one there is nothing to match, so the task would be armed against nothing
    # while looking, in the list, exactly like a command that works.
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="on_command")


def test_the_word_is_normalised_to_what_a_radio_head_can_send() -> None:
    assert _command_body(command_word=" gate ").command_word == "GATE"
    for bad in ("ga te", "gate!", "gaté"):
        with pytest.raises(ValidationError):
            _command_body(command_word=bad)


def test_a_callsign_is_accepted_with_or_without_an_ssid() -> None:
    assert _command_body(command_callsign="ke8xyz-9").command_callsign == "KE8XYZ-9"
    assert _command_body(command_callsign="  ").command_callsign is None
    with pytest.raises(ValidationError):
        _command_body(command_callsign="KE8 XYZ")


def test_a_window_needs_both_ends() -> None:
    # One end alone reads as a narrowing the box does not actually apply — the owner
    # would believe the command was armed for two hours a day when it is armed always.
    with pytest.raises(ValidationError):
        _command_body(command_from="07:00")
    ok = _command_body(command_from="7:00", command_until="09:00")
    assert (ok.command_from, ok.command_until) == ("07:00", "09:00")


def test_a_malformed_window_time_is_refused() -> None:
    for bad in ("25:00", "07:60", "seven", "07:00:00"):
        with pytest.raises(ValidationError):
            _command_body(command_from=bad, command_until="09:00")


def test_a_radio_command_may_hold_a_firewalled_scope_with_a_warning() -> None:
    """The decided mock warns rather than blocks, and that is right.

    Blocking reads like defence in depth and is not: location is exactly the domain a
    radio command wants — "what am I due at next" asked from the truck — and the box
    never transmits, so a fired task cannot answer over the air. Only a VERIFIED command
    fires anything, which is the cap that actually holds. The editor carries the warning
    (docs/mocks/aprs/b-trigger-editor.html)."""
    for scope in ("health", "finance", "location"):
        assert _command_body(agent="curator", domain_scopes=[scope]).domain_scopes == [scope]


def test_a_key_can_never_arrive_over_the_wire() -> None:
    # Not a field at all: the box generates the secret, so a weak or shared one cannot
    # be chosen, and a key in a request body cannot end up in a log or a proxy trace.
    body = _command_body(command_key="AAAAAAAA")  # type: ignore[call-arg]
    assert not hasattr(body, "command_key")


def test_changing_the_kind_disarms_the_command() -> None:
    # The word is what the verify path matches on, so clearing it is what actually
    # disarms it — a task flipped to on-demand must not still answer the radio.
    body = TaskBody(prompt="x", schedule_kind="on_demand", command_word="GATE")
    assert body.command_word is None


def test_command_state_is_visible_but_the_key_is_not() -> None:
    out = tasks_api.TaskOut.of(
        _task(
            schedule_kind="on_command",
            command_word="GATE",
            command_counter=7,
            command_failures=5,
        )
    )

    assert (out.command_word, out.command_counter) == ("GATE", 7)
    assert out.command_locked is True  # five failures is the lockout
    assert "command_key" not in out.model_dump()


def test_a_command_that_stays_a_command_keeps_its_key() -> None:
    # Editing the window months later must not re-key the truck. This is the case that
    # decides whether the feature is usable at all.
    kept = tasks_api._key_for_edit(_task(schedule_kind="on_command"), _command_body())

    assert kept == {}


def test_a_task_becoming_a_command_gets_a_fresh_key() -> None:
    made = tasks_api._key_for_edit(_task(schedule_kind="on_demand"), _command_body())

    assert made["command_key"]
    assert made["command_counter"] == 0


def test_a_command_that_stops_being_one_loses_its_key() -> None:
    # So switching it back on cannot resurrect a credential that may have been copied
    # while the command was off.
    dropped = tasks_api._key_for_edit(
        _task(schedule_kind="on_command"), TaskBody(prompt="x", schedule_kind="on_demand")
    )

    assert dropped == {"command_key": None, "command_counter": 0, "command_failures": 0}


class _CommandRepo(FakeRepo):
    """A repo that remembers what was written, which is what the key routes are about."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks = {"task-1": _task(schedule_kind="on_command", command_word="GATE")}
        self.wrote: dict[str, object] = {}

    async def update(self, ctx, task_id, **fields):  # type: ignore[no-untyped-def]
        if task_id not in self.tasks:
            return None
        self.wrote = dict(fields)
        # `command_key` never reaches a DTO — TaskInfo does not carry the secret — so
        # the fake drops it here exactly as the real repo's row-to-info mapping does.
        shown = {k: v for k, v in fields.items() if k != "command_key"}
        return _task(id=task_id, schedule_kind="on_command", command_word="GATE", **shown)


@pytest.mark.asyncio
async def test_rotating_shows_the_key_once_and_resets_the_counter() -> None:
    repo = _CommandRepo()

    out = await tasks_api.rotate_command_key(_request(repo), PRINCIPAL, "task-1")  # type: ignore[arg-type]

    assert out.word == "GATE"
    assert len(out.key) >= 32  # a 32-byte secret in base32; the CODE is what is short
    # Rotating is also revoking: the old key stops verifying, and a counter is
    # meaningless against a different key.
    assert repo.wrote["command_key"] == out.key
    assert repo.wrote["command_counter"] == 0 and repo.wrote["command_failures"] == 0


@pytest.mark.asyncio
async def test_rotating_a_task_that_is_not_a_command_is_a_404() -> None:
    repo = _CommandRepo()
    repo.tasks = {"task-1": _task(schedule_kind="repeat")}

    with pytest.raises(HTTPException):
        await tasks_api.rotate_command_key(_request(repo), PRINCIPAL, "task-1")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unlocking_clears_the_failures_and_nothing_else() -> None:
    # The owner runs this box with no terminal, so the way out of a lockout is a button.
    # It must not re-key the truck: most lockouts are a mis-keyed digit.
    repo = _CommandRepo()

    await tasks_api.unlock_command(_request(repo), PRINCIPAL, "task-1")  # type: ignore[arg-type]

    assert repo.wrote == {"command_failures": 0}
