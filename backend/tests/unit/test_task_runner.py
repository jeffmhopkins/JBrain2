"""The TaskRunner (tasks/runner.py) with fakes on every collaborator — the orchestration
(session → run → transcript → run record), the persona firewall, and the fail-closed
error path, with no DB and no LLM."""

from datetime import UTC, datetime

import pytest

from jbrain.agent.loop import AgentResult
from jbrain.agent.session import AgentSessionInfo
from jbrain.db.session import SessionContext
from jbrain.tasks.repo import TaskInfo
from jbrain.tasks.runner import ExecutedTurn, TaskRunner

NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)
OWNER = SessionContext(principal_id="11111111-1111-1111-1111-111111111111", principal_kind="owner")


def _task(**over: object) -> TaskInfo:
    base: dict[str, object] = dict(
        id="task-1",
        principal_id=OWNER.principal_id,
        group_id=None,
        position=0,
        name="Morning brief",
        prompt="Give me the news.",
        agent="jerv",
        domain_scopes=(),
        schedule_kind="repeat",
        schedule_freq="daily",
        schedule_days=(),
        schedule_time="07:00",
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


class FakeSessions:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.touched: list[str] = []

    async def create(self, ctx, *, domain_scopes, subject_ids=(), title="", agent="curator"):  # type: ignore[no-untyped-def]
        self.created.append({"scopes": tuple(domain_scopes), "title": title, "agent": agent})
        return AgentSessionInfo(
            id="sess-1",
            title=title,
            status="active",
            domain_scopes=tuple(domain_scopes),
            subject_ids=(),
            created_at=NOW,
            last_active_at=NOW,
            agent=agent,
        )

    async def touch(self, ctx, session_id):  # type: ignore[no-untyped-def]
        self.touched.append(session_id)


class FakeRunlog:
    def __init__(self) -> None:
        self.finished: list[dict] = []

    async def start(self, ctx, *, session_id, prompt_version):  # type: ignore[no-untyped-def]
        return "run-1"

    def bound(self, ctx, run_id):  # type: ignore[no-untyped-def]
        return object()

    async def finish(self, ctx, run_id, *, status, stop_reason, step_count, cost_tokens):  # type: ignore[no-untyped-def]
        self.finished.append({"status": status, "steps": step_count, "cost": cost_tokens})


class FakeTranscript:
    def __init__(self) -> None:
        self.recorded: list[dict] = []

    async def record_exchange(  # type: ignore[no-untyped-def]
        self, ctx, *, session_id, run_id, user_text, assistant_text, tools, reasoning=""
    ):
        self.recorded.append(
            {"user": user_text, "assistant": assistant_text, "tools": tools, "reasoning": reasoning}
        )
        return "turn-1"


class FakeRuns:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.finished: list[dict] = []

    async def start(self, ctx, *, task_id, principal_id, session_id, run_id, trigger):  # type: ignore[no-untyped-def]
        self.started.append({"task_id": task_id, "trigger": trigger, "session_id": session_id})
        return "trun-1"

    async def finish(self, ctx, run_id, *, status, summary, error, step_count, cost_tokens):  # type: ignore[no-untyped-def]
        self.finished.append({"status": status, "summary": summary, "error": error})


class FakeExecutor:
    def __init__(self, executed: ExecutedTurn | None = None, boom: bool = False) -> None:
        self._executed = executed or ExecutedTurn(
            result=AgentResult(
                text="Here is the news.", stop_reason="end_turn", steps=2, cost_tokens=10
            ),
            tools=[{"id": "t1", "name": "web_search", "ok": True}],
            reasoning="Let me check the headlines.",
        )
        self._boom = boom
        self.calls: list[dict] = []

    async def run_turn(self, *, read_scopes, agent_session_id, **_):  # type: ignore[no-untyped-def]
        self.calls.append({"scopes": tuple(read_scopes), "session": agent_session_id})
        if self._boom:
            raise RuntimeError("model exploded")
        return self._executed


class FakePush:
    def __init__(self) -> None:
        self.pokes: list[list[str]] = []

    async def poke(self, tokens):  # type: ignore[no-untyped-def]
        self.pokes.append(list(tokens))


def _runner(  # type: ignore[no-untyped-def]
    *, sessions=None, runlog=None, transcript=None, runs=None, executor=None, push=None, tokens=()
) -> TaskRunner:
    return TaskRunner(
        sessions=sessions or FakeSessions(),  # type: ignore[arg-type]
        runlog=runlog or FakeRunlog(),  # type: ignore[arg-type]
        transcript=transcript or FakeTranscript(),  # type: ignore[arg-type]
        runs=runs or FakeRuns(),  # type: ignore[arg-type]
        executor=executor or FakeExecutor(),
        push=push,
        push_tokens=tokens,
    )


@pytest.mark.asyncio
async def test_successful_run_records_session_transcript_and_run() -> None:
    sessions, runlog, transcript, runs = FakeSessions(), FakeRunlog(), FakeTranscript(), FakeRuns()
    runner = _runner(sessions=sessions, runlog=runlog, transcript=transcript, runs=runs)
    info = await runner.run(OWNER, _task(), trigger="manual")

    assert info.status == "done"
    assert info.summary == "Here is the news."
    assert info.session_id == "sess-1"
    assert runs.started[0]["trigger"] == "manual"
    assert runs.finished[0]["status"] == "done"
    assert runlog.finished[0] == {"status": "done", "steps": 2, "cost": 10}
    assert transcript.recorded[0]["assistant"] == "Here is the news."
    # The turn's tool steps and reasoning trace are persisted, so the session a task
    # produces replays its "Worked" / "Thought for Ns" disclosures on reopen — a
    # scheduled task used to record neither (tools=[] and no reasoning).
    assert transcript.recorded[0]["tools"] == [{"id": "t1", "name": "web_search", "ok": True}]
    assert transcript.recorded[0]["reasoning"] == "Let me check the headlines."
    assert sessions.touched == ["sess-1"]


@pytest.mark.asyncio
async def test_jerv_runs_with_empty_read_scopes() -> None:
    sessions, executor = FakeSessions(), FakeExecutor()
    runner = _runner(sessions=sessions, executor=executor)
    # The task carries a scope, but a non-KB persona must read nothing — the firewall.
    await runner.run(OWNER, _task(agent="jerv", domain_scopes=("health",)), trigger="schedule")
    assert executor.calls[0]["scopes"] == ()
    assert sessions.created[0]["scopes"] == ()


@pytest.mark.asyncio
async def test_curator_keeps_its_selected_scope() -> None:
    sessions, executor = FakeSessions(), FakeExecutor()
    runner = _runner(sessions=sessions, executor=executor)
    await runner.run(OWNER, _task(agent="curator", domain_scopes=("health",)), trigger="schedule")
    assert executor.calls[0]["scopes"] == ("health",)
    assert sessions.created[0]["scopes"] == ("health",)


@pytest.mark.asyncio
async def test_a_failing_turn_is_a_recorded_error_not_a_raise() -> None:
    runs, runlog, transcript = FakeRuns(), FakeRunlog(), FakeTranscript()
    runner = _runner(
        runlog=runlog, transcript=transcript, runs=runs, executor=FakeExecutor(boom=True)
    )
    info = await runner.run(OWNER, _task(), trigger="schedule")
    assert info.status == "error"
    assert "model exploded" in (info.error or "")
    assert runs.finished[0]["status"] == "error"
    assert runlog.finished[0]["status"] == "error"
    # A failed turn writes no transcript exchange.
    assert transcript.recorded == []


@pytest.mark.asyncio
async def test_push_pokes_only_when_enabled_with_tokens() -> None:
    push = FakePush()
    runner = _runner(push=push, tokens=("tok-a",))
    await runner.run(OWNER, _task(notify_push=True), trigger="schedule")
    assert push.pokes == [["tok-a"]]

    push2 = FakePush()
    runner2 = _runner(push=push2, tokens=("tok-a",))
    await runner2.run(OWNER, _task(notify_push=False), trigger="schedule")
    assert push2.pokes == []
