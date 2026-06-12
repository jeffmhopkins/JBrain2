"""The /chat SSE endpoint and the sessions API, with fakes on app.state — a real
turn-loop driven by the fake adapter, no database. Asserts the loop's ChatEvents
serialize as `data:`-framed SSE and that the run log is opened and closed."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.session import AgentSessionInfo
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 6, 12, tzinfo=UTC)


class FakeAgentSessions:
    def __init__(self) -> None:
        self.touched: list[str] = []
        self._by_id: dict[str, AgentSessionInfo] = {}

    def add(self, info: AgentSessionInfo) -> None:
        self._by_id[info.id] = info

    async def create(self, ctx, *, domain_scopes, subject_ids=(), title=""):  # type: ignore[no-untyped-def]
        info = AgentSessionInfo(
            id=f"sess-{len(self._by_id) + 1}",
            title=title,
            status="active",
            domain_scopes=tuple(domain_scopes),
            subject_ids=tuple(subject_ids),
            created_at=NOW,
            last_active_at=NOW,
        )
        self.add(info)
        return info

    async def list(self, ctx):  # type: ignore[no-untyped-def]
        return list(self._by_id.values())

    async def get(self, ctx, session_id):  # type: ignore[no-untyped-def]
        return self._by_id.get(session_id)

    async def touch(self, ctx, session_id):  # type: ignore[no-untyped-def]
        self.touched.append(session_id)


class FakeRunLog:
    """Doubles as both the run-log writer and the bound step recorder."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.steps: list[tuple[str, str]] = []
        self.finished: list[dict] = []

    async def start(self, ctx, *, session_id, prompt_version):  # type: ignore[no-untyped-def]
        self.started.append((session_id, prompt_version))
        return "run-1"

    def bound(self, ctx, run_id):  # type: ignore[no-untyped-def]
        return self

    async def step(self, *, idx, kind, name, ok, cost_tokens):  # type: ignore[no-untyped-def]
        self.steps.append((kind, name))

    async def finish(self, ctx, run_id, *, status, stop_reason, step_count, cost_tokens):  # type: ignore[no-untyped-def]
        self.finished.append(
            {
                "status": status,
                "stop_reason": stop_reason,
                "step_count": step_count,
                "cost_tokens": cost_tokens,
            }
        )


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def sessions_store() -> FakeAgentSessions:
    return FakeAgentSessions()


@pytest.fixture
def runlog() -> FakeRunLog:
    return FakeRunLog()


def stream_router(turns: list[LlmTurn], stream_chunks: list[list[str]]) -> LlmRouter:
    fake = FakeLlmClient(turns=turns, stream_chunks=stream_chunks)
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")})


class BoomStreamClient:
    """A client whose stream raises mid-turn, to exercise the /chat error path."""

    async def converse_stream(self, **_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("model exploded")
        yield  # unreachable; makes this an async generator


@pytest.fixture
def client(
    repo: FakeAuthRepo, sessions_store: FakeAgentSessions, runlog: FakeRunLog
) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.agent_sessions = sessions_store
        app.state.agent_runlog = runlog
        app.state.agent_registry = ToolRegistry([])  # no tools: the model answers directly
        app.state.llm_router = stream_router(
            [LlmTurn("hi there", (), "end_turn", LlmUsage(7, 3))],
            stream_chunks=[["hi ", "there"]],
        )
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def sse_events(body: str) -> list[dict]:
    return [
        json.loads(block[len("data: ") :])
        for block in body.strip().split("\n\n")
        if block.startswith("data: ")
    ]


def test_chat_requires_owner(client: TestClient) -> None:
    assert client.post("/api/chat", json={"session_id": "x", "message": "hi"}).status_code == 401


def test_chat_unknown_session_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.post("/api/chat", json={"session_id": "ghost", "message": "hi"})
    assert resp.status_code == 404


def test_chat_streams_text_then_done(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "what do I know?"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = sse_events(resp.text)
    assert events == [
        {"type": "text_delta", "text": "hi "},
        {"type": "text_delta", "text": "there"},
        {"type": "done", "stop_reason": "end_turn"},
    ]
    # The run was opened, the session touched, and the run closed with its summary.
    assert runlog.started == [("sess-1", "agent-system-v1")]
    assert sessions_store.touched == ["sess-1"]
    assert runlog.finished == [
        {"status": "ended", "stop_reason": "end_turn", "step_count": 1, "cost_tokens": 10}
    ]


def test_chat_history_is_replayed_into_the_turn(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = router._clients["xai"]  # type: ignore[attr-defined]

    resp = client.post(
        "/api/chat",
        json={
            "session_id": "sess-1",
            "message": "and the second?",
            "history": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
            ],
        },
    )
    assert resp.status_code == 200
    # The loop received prior turns plus the new user message, in order.
    sent = fake.stream_calls[0]["messages"]
    assert [type(m).__name__ for m in sent] == [
        "UserMessage",
        "AssistantMessage",
        "UserMessage",
    ]
    assert sent[-1].text == "and the second?"


def test_chat_model_failure_emits_error_done_and_marks_run_failed(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": BoomStreamClient()}, {"agent.turn": ("xai", "grok-4.3")}
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    # The failure surfaces as a terminal event, never a 500, and the run is closed.
    assert resp.status_code == 200
    assert sse_events(resp.text)[-1] == {"type": "done", "stop_reason": "error"}
    assert runlog.finished[-1]["status"] == "failed"
    assert runlog.finished[-1]["stop_reason"] == "error"


def test_create_and_list_sessions(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    created = client.post(
        "/api/sessions", json={"domain_scopes": ["general", "health"], "title": "labs"}
    )
    assert created.status_code == 200
    assert created.json()["domain_scopes"] == ["general", "health"]

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert [s["title"] for s in listed.json()] == ["labs"]


def test_sessions_require_owner(client: TestClient) -> None:
    assert client.get("/api/sessions").status_code == 401
