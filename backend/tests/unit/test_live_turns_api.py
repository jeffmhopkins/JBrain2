"""GET /api/ops/turns — the roster behind the vitals detail surface.

The list comes from the runs TABLE, not the in-process live-turn registry: that
registry holds only parent /chat turns, so a deep-research fan would collapse to one
row and a workflow run would never appear. These pin that, the call stamp's optional
shape, and the owner-only gate.
"""

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.prompt_capture import forget, record_prompt
from jbrain.agent.runlog import LiveTurnRow, RunDetail, RunStepView
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 8, 16, 7, 41, 56, tzinfo=UTC)


def row(**over: object) -> LiveTurnRow:
    base: dict[str, object] = {
        "id": "run_parent",
        "kind": "agent",
        "status": "running",
        "name": "agent",
        "started_at": NOW,
        "elapsed_ms": 252_000,
        "step_count": 9,
        "cost_tokens": 38_200,
        "progress_note": "synthesising 6 sources",
        "parent_run_id": None,
        "session_id": "sess_4b1e",
        "domain_code": None,
        "ran_as": "scoped",
        "prompt_version": "fullbrain@v14",
        "trigger_pipeline": None,
        "call_stamp": {
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "reasoning_effort": "high",
            "context_window": 200_000,
            "vision": True,
            "persona": "jerv",
            "tools": ["notes.search", "web.fetch"],
            "user_message": "Dig into heat pump sizing.",
            "user_message_truncated": False,
        },
    }
    base.update(over)
    return LiveTurnRow(**base)  # type: ignore[arg-type]


class FakeRunReader:
    def __init__(self) -> None:
        self.rows: list[LiveTurnRow] = []
        self.detail: RunDetail | None = None

    async def list_live(self, ctx: object, *, limit: int = 50) -> list[LiveTurnRow]:
        return self.rows

    async def load(self, ctx: object, run_id: str) -> RunDetail | None:
        return self.detail


class FakeTranscript:
    def __init__(self) -> None:
        self.turns: list[object] = []

    async def load(self, ctx: object, session_id: str) -> list[object]:
        return self.turns


class StoredTurn:
    def __init__(self, role: str, content: str, reasoning: str = "") -> None:
        self.role = role
        self.content = content
        self.reasoning = reasoning


def detail(steps: list[RunStepView], session_id: str | None = "sess_4b1e") -> RunDetail:
    return RunDetail(
        id="run_parent",
        kind="agent",
        status="running",
        name="agent",
        started_at=NOW,
        duration_ms=None,
        step_count=len(steps),
        cost_tokens=0,
        stop_reason=None,
        progress_note=None,
        session_id=session_id,
        steps=steps,
    )


def step(idx: int, *, ok: bool = True, error: str | None = None) -> RunStepView:
    return RunStepView(
        idx=idx,
        kind="web.fetch",
        name=f"step {idx}",
        ok=ok,
        cost_tokens=100,
        job_id=None,
        error=error,
        detail=None,
    )


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def reader() -> FakeRunReader:
    return FakeRunReader()


@pytest.fixture
def transcript() -> FakeTranscript:
    return FakeTranscript()


@pytest.fixture
def client(
    repo: FakeAuthRepo, reader: FakeRunReader, transcript: FakeTranscript
) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.run_reader = reader
        app.state.agent_transcript = transcript
        app.state.live_turns = {}
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_turns_require_owner(client: TestClient) -> None:
    assert client.get("/api/ops/turns").status_code == 401


def test_lists_a_turn_with_its_call(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    reader.rows = [row()]
    login(client, repo)

    body = client.get("/api/ops/turns").json()

    assert len(body["turns"]) == 1
    turn = body["turns"][0]
    assert turn["kind"] == "agent"
    assert turn["progress_note"] == "synthesising 6 sources"
    assert turn["call"]["model"] == "claude-opus-4-6"
    assert turn["call"]["persona"] == "jerv"
    # The verbatim prompt is stored nowhere, so the payload must not imply otherwise.
    assert "prompt" not in turn["call"]


def test_nests_a_fan_by_parent(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """A deep-research fan is a parent plus its children — the whole reason this reads
    the table instead of the registry, which only knows the parent."""
    reader.rows = [
        row(),
        row(id="run_child_a", kind="subagent", parent_run_id="run_parent"),
        row(id="run_child_b", kind="subagent", parent_run_id="run_parent"),
    ]
    login(client, repo)

    turns = client.get("/api/ops/turns").json()["turns"]

    assert [t["id"] for t in turns] == ["run_parent", "run_child_a", "run_child_b"]
    assert [t["parent_run_id"] for t in turns[1:]] == ["run_parent", "run_parent"]


def test_renders_a_run_with_no_stamp(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """Runs predating migration 0166, and drivers that don't stamp, still list — the
    surface shows what it has rather than hiding the row."""
    reader.rows = [row(call_stamp=None, kind="pipeline", trigger_pipeline="nightly-reconcile")]
    login(client, repo)

    turn = client.get("/api/ops/turns").json()["turns"][0]

    assert turn["call"] is None
    assert turn["trigger_pipeline"] == "nightly-reconcile"


def test_keeps_a_partial_stamp(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    reader.rows = [row(call_stamp={"model": "gpt-oss-120b"})]
    login(client, repo)

    call = client.get("/api/ops/turns").json()["turns"][0]["call"]

    assert call["model"] == "gpt-oss-120b"
    assert call["provider"] is None


def test_distinguishes_a_wildcard_toolset_from_none(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """null tools is the registry WILDCARD ("every in-scope tool"); [] is "no tools".
    Flattening them would tell the owner a jerv turn holds nothing."""
    reader.rows = [
        row(id="wild", call_stamp={"tools": None}),
        row(id="none", call_stamp={"tools": []}),
    ]
    login(client, repo)

    turns = client.get("/api/ops/turns").json()["turns"]

    assert turns[0]["call"]["tools"] is None
    assert turns[1]["call"]["tools"] == []


def test_names_a_child_by_its_label_not_its_kind(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """A fan of children all named "subagent" is useless. The reader prefers the call
    stamp's label, so this pins the shape the API hands the roster."""
    reader.rows = [
        row(id="kid", kind="subagent", name="source sweep — manufacturer specs"),
    ]
    login(client, repo)

    assert client.get("/api/ops/turns").json()["turns"][0]["name"] != "subagent"


def test_carries_the_gauge_next_to_an_empty_roster(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPU busy covers the whole box — image generation included — so a high reading
    with nothing running is legitimate, and the surface needs both figures at once to
    say so instead of looking broken."""
    from jbrain.api import ops

    monkeypatch.setattr(ops, "read_gpu_busy_percent", lambda: 94.0)
    reader.rows = []
    login(client, repo)

    body = client.get("/api/ops/turns").json()

    assert body["turns"] == []
    assert body["gpu_busy_percent"] == 94.0


# --- GET /ops/turns/{run_id} — the step trail + raw output (level 2) ---------


def test_turn_detail_requires_owner(client: TestClient) -> None:
    assert client.get("/api/ops/turns/run_parent").status_code == 401


def test_returns_the_step_trail(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    reader.detail = detail([step(0), step(1, ok=False, error="timeout")])
    login(client, repo)

    steps = client.get("/api/ops/turns/run_parent").json()["steps"]

    assert [s["idx"] for s in steps] == [0, 1]
    assert steps[1]["ok"] is False
    assert steps[1]["error"] == "timeout"


def test_falls_back_to_the_transcript_for_a_settled_sub_agent(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader, transcript: FakeTranscript
) -> None:
    """A sub-agent never registers a live handle, so without this a fan's children would
    read as silent even after they finish."""
    settled = detail([])
    reader.detail = RunDetail(**{**vars(settled), "status": "done"})
    transcript.turns = [
        StoredTurn("user", "sweep the spec sheets"),
        StoredTurn("assistant", "MXZ-SM36: rated 36,000 BTU.", reasoning="extracting capacity"),
    ]
    login(client, repo)

    output = client.get("/api/ops/turns/run_parent").json()["output"]

    assert output["live"] is False
    assert output["answer"] == "MXZ-SM36: rated 36,000 BTU."
    assert output["reasoning"] == "extracting capacity"


def test_never_shows_another_turn_s_answer_as_this_one_s(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader, transcript: FakeTranscript
) -> None:
    """A /chat turn's run row exists for several awaits before its live handle does.
    Reading the session transcript in that window would show the PREVIOUS exchange's
    answer under this turn's header, badged as if it were its own."""
    reader.detail = detail([])  # status="running", and no live handle registered yet
    transcript.turns = [StoredTurn("assistant", "the answer to the LAST question")]
    login(client, repo)

    output = client.get("/api/ops/turns/run_parent").json()["output"]

    assert output is None


def test_reads_the_transcript_once_the_run_has_settled(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader, transcript: FakeTranscript
) -> None:
    settled = detail([])
    reader.detail = RunDetail(**{**vars(settled), "status": "done"})
    transcript.turns = [StoredTurn("assistant", "MXZ-SM36: rated 36,000 BTU.")]
    login(client, repo)

    output = client.get("/api/ops/turns/run_parent").json()["output"]

    assert output["live"] is False
    assert output["answer"] == "MXZ-SM36: rated 36,000 BTU."


def test_prefers_the_live_accumulator_over_the_transcript(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader, transcript: FakeTranscript
) -> None:
    """A turn mid-answer has nothing in the transcript yet — only the live accumulator
    can show what it has produced so far."""

    class FakeAcc:
        @staticmethod
        def render_snapshot() -> dict[str, object]:
            return {"content": "streaming so far", "reasoning": "thinking", "tools": []}

    class FakeLive:
        acc = FakeAcc()
        done = False

    reader.detail = detail([])
    transcript.turns = [StoredTurn("assistant", "a stale earlier answer")]
    login(client, repo)
    client.app.state.live_turns["run_parent"] = FakeLive()  # type: ignore[attr-defined]

    try:
        output = client.get("/api/ops/turns/run_parent").json()["output"]
    finally:
        # App shutdown cancels every live turn's driving task; this stand-in has none,
        # so it must not still be in the registry at teardown.
        client.app.state.live_turns.clear()  # type: ignore[attr-defined]

    assert output["live"] is True
    assert output["answer"] == "streaming so far"


def test_reports_no_output_for_a_turn_that_has_produced_none(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    reader.detail = detail([], session_id=None)
    login(client, repo)

    assert client.get("/api/ops/turns/run_parent").json()["output"] is None


def test_shows_the_prompt_the_model_actually_received(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """The one thing the surface previously could not show, because it was assembled
    per call and thrown away."""
    reader.detail = detail([])
    record_prompt(
        "run_parent",
        system="you are jerv",
        messages=[{"role": "user", "content": "size the heat pump"}],
        tools=["web.fetch"],
    )
    login(client, repo)

    try:
        prompt = client.get("/api/ops/turns/run_parent").json()["prompt"]
    finally:
        forget("run_parent")

    assert prompt["system"] == "you are jerv"
    assert prompt["messages"][0]["content"] == "size the heat pump"
    assert prompt["tools"] == ["web.fetch"]
    assert prompt["round_index"] == 1


def test_a_run_whose_prompt_was_never_captured_reports_none(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """It predates a restart, was evicted, or never reached a model call — the surface
    says nothing rather than inventing one."""
    reader.detail = detail([])
    login(client, repo)

    assert client.get("/api/ops/turns/run_never_prompted").json()["prompt"] is None


# --- GET /ops/vitals/history — the graph's past ------------------------------


def test_vitals_history_requires_owner(client: TestClient) -> None:
    assert client.get("/api/ops/vitals/history").status_code == 401


def test_serves_the_recorded_history(client: TestClient, repo: FakeAuthRepo) -> None:
    ring = client.app.state.vitals_ring  # type: ignore[attr-defined]
    ring.record(time.time(), 61.0)
    login(client, repo)

    samples = client.get("/api/ops/vitals/history?seconds=600").json()["samples"]

    assert samples[-1]["gpu"] == 61.0


def test_history_window_is_clamped_to_the_ring(client: TestClient, repo: FakeAuthRepo) -> None:
    # A wider request is answered with what exists rather than rejected.
    login(client, repo)

    assert client.get("/api/ops/vitals/history?seconds=99999").status_code == 200
