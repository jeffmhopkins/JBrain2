"""GET /api/ops/turns — the roster behind the vitals detail surface.

The list comes from the runs TABLE, not the in-process live-turn registry: that
registry holds only parent /chat turns, so a deep-research fan would collapse to one
row and a workflow run would never appear. These pin that, the call stamp's optional
shape, and the owner-only gate.
"""

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.prompt_capture import forget, record_prompt
from jbrain.agent.runlog import LiveTurnRow, RunDetail, RunStepView
from jbrain.api import ops
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
        "ended_at": None,
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
        # What the route asked for, so a test can assert the window reached the reader.
        self.since: datetime | None = None

    async def list_live(
        self, ctx: object, *, limit: int = 50, since: datetime | None = None
    ) -> list[LiveTurnRow]:
        self.since = since
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
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """GPU busy covers the whole box — image generation included — so a high reading
    with nothing running is legitimate, and the surface needs both figures at once to
    say so instead of looking broken."""
    reader.rows = []
    login(client, repo)
    client.app.state.vitals_ring.record(time.time(), 94.0)  # type: ignore[attr-defined]

    body = client.get("/api/ops/turns").json()

    assert body["turns"] == []
    assert body["gpu_busy_percent"] == 94.0


# --- the window: turns that RAN, not only turns still running ----------------


def test_defaults_to_running_turns_only(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """No `seconds` is the top bar's question — what is on the box right now — so it must
    not silently widen into a history read."""
    login(client, repo)

    client.get("/api/ops/turns")

    assert reader.since is None


def test_asks_for_the_window_the_graph_is_showing(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """The roster emptied seconds after a turn finished, so the 5- and 15-minute ranges
    showed a graph full of history above a list with nothing in it."""
    login(client, repo)
    before = datetime.now(tz=UTC)

    client.get("/api/ops/turns?seconds=900")

    assert reader.since is not None
    elapsed = before - reader.since
    assert timedelta(seconds=899) <= elapsed <= timedelta(seconds=901)


def test_clamps_the_window_to_the_graph_s_ceiling(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """This route returns every field of every row, so a hand-written `seconds` must not
    turn it into an unbounded run-log export."""
    login(client, repo)
    before = datetime.now(tz=UTC)

    client.get("/api/ops/turns?seconds=86400")

    assert reader.since is not None
    assert before - reader.since <= timedelta(seconds=901)


def test_a_negative_window_reads_as_running_only(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    login(client, repo)

    client.get("/api/ops/turns?seconds=-60")

    assert reader.since is None


def test_reports_when_a_turn_ended(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """The client splits running from settled on this field alone; without it every
    finished turn in the window rendered as though it were still going."""
    ended = NOW + timedelta(seconds=42)
    reader.rows = [row(id="run_done", status="ok", ended_at=ended, elapsed_ms=42_000)]
    login(client, repo)

    turn = client.get("/api/ops/turns?seconds=300").json()["turns"][0]

    assert turn["ended_at"] is not None
    assert turn["elapsed_ms"] == 42_000


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


# --- GET /ops/vitals/events — what the box was DOING --------------------------
# The graph and the roster together cannot explain the box's commonest busy minute: GPU
# pinned, roster empty. These pin the route that supplies the missing half.


def test_vitals_events_require_owner(client: TestClient) -> None:
    assert client.get("/api/ops/vitals/events").status_code == 401


def _serve_events(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, object]]) -> list[float]:
    """Stub the reader (a real one needs Postgres) and report the windows it was asked for."""
    windows: list[float] = []

    async def fake_recent(maker: object, ctx: object, *, seconds: float) -> list[dict[str, object]]:
        windows.append(seconds)
        return events

    monkeypatch.setattr(ops.box_events, "recent", fake_recent)
    return windows


def test_reports_a_load_that_is_still_happening(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ended_ms` null is what lets the surface say "loading gpt-oss-120b…" during the
    spike rather than accounting for it a minute later."""
    _serve_events(
        monkeypatch,
        [
            {
                "at_ms": 1_760_000_000_000,
                "ended_ms": None,
                "kind": "model_load",
                "subject": "gpt-oss-120b",
                "detail": "",
                "status": "running",
                "source": "worker",
            }
        ],
    )
    login(client, repo)

    events = client.get("/api/ops/vitals/events?seconds=300").json()["events"]

    assert events[0]["subject"] == "gpt-oss-120b"
    assert events[0]["ended_ms"] is None
    assert events[0]["source"] == "worker"  # a load nothing on screen asked for


def test_carries_why_a_model_was_evicted(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_events(
        monkeypatch,
        [
            {
                "at_ms": 1_760_000_000_000,
                "ended_ms": 1_760_000_001_000,
                "kind": "model_unload",
                "subject": "qwen35",
                "detail": "to make room for gpt-oss-120b",
                "status": "ok",
                "source": "api",
            }
        ],
    )
    login(client, repo)

    events = client.get("/api/ops/vitals/events").json()["events"]

    assert events[0]["detail"] == "to make room for gpt-oss-120b"


def _load_row(
    subject: str = "gpt-oss-120b",
    status: str = "running",
    progress: float | None = None,
) -> dict[str, object]:
    return {
        "at_ms": 1_760_000_000_000,
        "ended_ms": None if status == "running" else 1_760_000_090_000,
        "kind": "model_load",
        "subject": subject,
        "detail": "",
        "status": status,
        "source": "api",
        "progress": progress,
    }


def test_a_running_load_carries_how_far_in_it_is(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The elapsed count beside the row says how long this has been going; only the
    fraction says how much longer, which on a load that reads tens of GB is the question
    the owner is actually asking."""
    _serve_events(monkeypatch, [_load_row(progress=0.43)])
    login(client, repo)

    events = client.get("/api/ops/vitals/events").json()["events"]

    assert events[0]["percent"] == pytest.approx(0.43)


def test_a_load_with_no_measurement_still_reports_itself(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box with no device-memory probe, or a load whose first sample has not landed, has
    no fraction — and must still say it is loading. The percentage is the embellishment;
    the row is the reading."""
    _serve_events(monkeypatch, [_load_row()])
    login(client, repo)

    event = client.get("/api/ops/vitals/events").json()["events"][0]

    assert event["subject"] == "gpt-oss-120b"
    assert event["status"] == "running"
    assert event["percent"] is None


def test_each_load_carries_its_own_fraction(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled row for one model must not wear the progress of the load that replaced it.
    The figure rides its own row, so there is nothing to match up and nothing to get wrong —
    which is the point of having moved it there. (`box_events.recent` is what blanks the
    fraction on a settled row; that rule is covered against a real database.)"""
    _serve_events(
        monkeypatch,
        [_load_row(subject="qwen35"), _load_row(progress=0.43)],
    )
    login(client, repo)

    events = client.get("/api/ops/vitals/events").json()["events"]

    assert [(e["subject"], e["percent"]) for e in events] == [
        ("qwen35", None),
        ("gpt-oss-120b", pytest.approx(0.43)),
    ]


def test_events_window_is_clamped_like_the_graph(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = _serve_events(monkeypatch, [])
    login(client, repo)

    assert client.get("/api/ops/vitals/events?seconds=99999").status_code == 200
    assert windows == [900.0]  # the widest window the surface offers, not what was asked


# --- the SSE stream's anti-buffering headers ---------------------------------


async def test_vitals_stream_asks_proxies_not_to_buffer() -> None:
    """An intermediary that buffers text/event-stream turns this into a socket that
    connects, reports itself healthy, and delivers nothing — a failure EventSource raises
    no error for, so the client's reconnect never fires and the meter just sits blank.

    Asserted on the response object, NOT through `client.stream`: TestClient never delivers
    an ASGI `http.disconnect`, so consuming this route's body through it hangs the suite
    forever — which is exactly what it did the first time this was written."""
    from types import SimpleNamespace

    from jbrain.api import ops

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(vitals_ring=None)),
        is_disconnected=None,
    )
    response = await ops.vitals_stream(request)  # type: ignore[arg-type]

    assert response.media_type == "text/event-stream"
    assert "no-cache" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"


# --- one gauge, every surface -------------------------------------------------


def test_every_gpu_surface_answers_from_one_sample(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """The probe and the roster must report the SAME figure as the ring behind them.

    They each used to take their own `read_gpu_busy_percent()` at their own instant, which
    is how two surfaces showing "the GPU" could disagree on screen — one blank while
    another read 94%. ONE fake reading feeds all of them here, so a future change that
    reintroduces a second source fails this test instead of shipping."""
    reader.rows = []
    login(client, repo)
    ring = client.app.state.vitals_ring  # type: ignore[attr-defined]
    ring.record(time.time(), 73.0)

    probe = client.get("/api/ops/vitals").json()["gpu_busy_percent"]
    roster = client.get("/api/ops/turns").json()["gpu_busy_percent"]

    assert probe == roster == ring.latest() == 73.0


def test_the_roster_gauge_goes_quiet_when_the_sampler_does(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """One source is also one point of failure: a stopped sampler must read as no reading,
    not as a confident frozen number."""
    reader.rows = []
    login(client, repo)
    client.app.state.vitals_ring.record(time.time() - 3600, 99.0)  # type: ignore[attr-defined]

    assert client.get("/api/ops/turns").json()["gpu_busy_percent"] is None


# --- the browser's own account of the stream ---------------------------------


def test_client_vitals_round_trip(client: TestClient, repo: FakeAuthRepo) -> None:
    """The box cannot see any of this. A connection the browser declines to open leaves no
    server-side trace, which is the state that had the top bar blank while the box was
    serving 97% quite happily — so the browser reports what it saw and the debug console
    reads it back."""
    login(client, repo)

    assert (
        client.post("/api/ops/client-vitals", json={"frames": 0, "readyState": 1}).status_code
        == 204
    )

    stored = client.app.state.client_vitals  # type: ignore[attr-defined]
    assert stored["frames"] == 0
    assert stored["readyState"] == 1
    assert "at" in stored  # stamped server-side, so a wrong device clock cannot mislead


def test_client_vitals_accepts_unknown_keys(client: TestClient, repo: FakeAuthRepo) -> None:
    """A diagnostic must never 422 because a newer client learned to report one more
    thing — the report is evidence, not an API."""
    login(client, repo)

    response = client.post("/api/ops/client-vitals", json={"somethingNew": "x"})

    assert response.status_code == 204


def test_client_vitals_requires_owner(client: TestClient) -> None:
    assert client.post("/api/ops/client-vitals", json={}).status_code == 401
