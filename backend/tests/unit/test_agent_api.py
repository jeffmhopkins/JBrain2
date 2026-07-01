"""The /chat SSE endpoint and the sessions API, with fakes on app.state — a real
turn-loop driven by the fake adapter, no database. Asserts the loop's ChatEvents
serialize as `data:`-framed SSE and that the run log is opened and closed."""

import asyncio
import contextlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.clock import _CLOCK_FRAME
from jbrain.agent.contracts import EntityRef, NoteSource, ProposalRef, ToolSpec, ViewPayload
from jbrain.agent.identity import _ME_FRAME
from jbrain.agent.loop import ToolOutput
from jbrain.agent.session import AgentSessionInfo
from jbrain.agent.toolfile import ToolFile
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.agent.transcript_store import TurnRecord
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.llm import FakeLlmClient, LlmClient, LlmRouter, LlmTurn, LlmUsage, TextChunk, ToolCall
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore

NOW = datetime(2026, 6, 12, tzinfo=UTC)


class FakeAgentSessions:
    def __init__(self) -> None:
        self.touched: list[str] = []
        self._by_id: dict[str, AgentSessionInfo] = {}
        # The last (session_id, tokens, window) the chat handler persisted at settle.
        self.recorded_context: tuple[str, int, int] | None = None

    def add(self, info: AgentSessionInfo) -> None:
        self._by_id[info.id] = info

    async def create(self, ctx, *, domain_scopes, subject_ids=(), title="", agent="curator"):  # type: ignore[no-untyped-def]
        info = AgentSessionInfo(
            id=f"sess-{len(self._by_id) + 1}",
            title=title,
            status="active",
            domain_scopes=tuple(domain_scopes),
            subject_ids=tuple(subject_ids),
            created_at=NOW,
            last_active_at=NOW,
            agent=agent,
        )
        self.add(info)
        return info

    async def list(self, ctx):  # type: ignore[no-untyped-def]
        return list(self._by_id.values())

    async def get(self, ctx, session_id):  # type: ignore[no-untyped-def]
        return self._by_id.get(session_id)

    async def touch(self, ctx, session_id):  # type: ignore[no-untyped-def]
        self.touched.append(session_id)

    async def rename(self, ctx, session_id, title):  # type: ignore[no-untyped-def]
        info = self._by_id.get(session_id)
        if info is not None:
            self._by_id[session_id] = replace(info, title=title)

    async def record_context(self, ctx, session_id, tokens, window):  # type: ignore[no-untyped-def]
        self.recorded_context = (session_id, tokens, window)
        info = self._by_id.get(session_id)
        if info is not None:
            self._by_id[session_id] = replace(info, context_tokens=tokens, context_window=window)

    async def set_status(self, ctx, session_id, status):  # type: ignore[no-untyped-def]
        info = self._by_id.get(session_id)
        if info is not None:
            self._by_id[session_id] = replace(info, status=status)

    async def set_scopes(self, ctx, session_id, domain_scopes):  # type: ignore[no-untyped-def]
        info = self._by_id.get(session_id)
        if info is not None:
            self._by_id[session_id] = replace(info, domain_scopes=tuple(domain_scopes))

    async def delete(self, ctx, session_id):  # type: ignore[no-untyped-def]
        self._by_id.pop(session_id, None)


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


class FakeTranscript:
    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.turns: dict[str, list[TurnRecord]] = {}

    async def record_exchange(  # type: ignore[no-untyped-def]
        self, ctx, *, session_id, run_id, user_text, assistant_text, tools, reasoning=""
    ):
        self.recorded.append(
            {
                "session_id": session_id,
                "run_id": run_id,
                "user": user_text,
                "assistant": assistant_text,
                "tools": list(tools),
                "reasoning": reasoning,
            }
        )
        # Hand back a user-turn id so the endpoint can bind the turn's attachments.
        return f"turn-{len(self.recorded)}"

    async def load(self, ctx, session_id):  # type: ignore[no-untyped-def]
        return self.turns.get(session_id, [])


class FakeChatBlobs:
    """An in-memory blob store keyed by sha256, for the /chat attachment path."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def get(self, sha256):  # type: ignore[no-untyped-def]
        return self.data[sha256]


class FakeChatAttachments:
    """Just `get` (RLS modeled by membership) + `bind_to_turn`, recording binds."""

    def __init__(self) -> None:
        self.rows: dict[str, AttachmentInfo] = {}
        self.bound: list[tuple[tuple[str, ...], list[str], str]] = []

    def add(self, info: AttachmentInfo) -> None:
        self.rows[info.id] = info

    async def get(self, ctx, attachment_id):  # type: ignore[no-untyped-def]
        return self.rows.get(attachment_id)

    async def bind_to_turn(self, ctx, attachment_ids, turn_id):  # type: ignore[no-untyped-def]
        self.bound.append((tuple(ctx.domain_scopes), list(attachment_ids), turn_id))


def registry_with_tool(name, handler) -> ToolRegistry:  # type: ignore[no-untyped-def]
    spec = ToolSpec(name=name, version=1, params={"type": "object"}, permission="read")
    return ToolRegistry(
        [RegisteredTool(toolfile=ToolFile(spec=spec, description=name), handler=handler)]
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


@pytest.fixture
def transcript() -> FakeTranscript:
    return FakeTranscript()


@pytest.fixture
def chat_attachments() -> FakeChatAttachments:
    return FakeChatAttachments()


@pytest.fixture
def chat_blobs() -> FakeChatBlobs:
    return FakeChatBlobs()


def stream_router(turns: list[LlmTurn], stream_chunks: list[list[str]]) -> LlmRouter:
    fake = FakeLlmClient(turns=turns, stream_chunks=stream_chunks)
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")})


class BoomStreamClient:
    """A client whose stream raises mid-turn, to exercise the /chat error path."""

    async def converse_stream(self, **_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("model exploded")
        yield  # unreachable; makes this an async generator


class CancelStreamClient:
    """Streams a partial answer, then raises CancelledError — the server-side shape of
    the owner tapping Stop (or a dropped connection) mid-answer."""

    async def converse_stream(self, **_kw):  # type: ignore[no-untyped-def]
        yield TextChunk(text="the drive is about ")
        raise asyncio.CancelledError


class BoomAfterTextClient:
    """Streams a partial answer, then raises a non-cancellation error — the shape of a
    compose-the-reply call breaking after a tool already ran (e.g. the local LLM
    failing to reload). The partial turn must still persist so the work isn't lost."""

    async def converse_stream(self, **_kw):  # type: ignore[no-untyped-def]
        yield TextChunk(text="I've generated a ")
        raise RuntimeError("model exploded")


class HangStreamClient:
    """Streams a partial answer, then hangs forever — a runaway turn that would peg the
    GPU. The hard turn wall-clock must force-end it (cancelling the hung call)."""

    async def converse_stream(self, **_kw):  # type: ignore[no-untyped-def]
        yield TextChunk(text="working ")
        await asyncio.sleep(60)  # cancelled by the (patched-tiny) turn wall-clock


@pytest.fixture
def client(
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
    transcript: FakeTranscript,
    chat_attachments: FakeChatAttachments,
    chat_blobs: FakeChatBlobs,
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
        app.state.agent_transcript = transcript
        app.state.turn_attachments = chat_attachments
        app.state.blob_store = chat_blobs
        app.state.agent_registry = ToolRegistry([])  # no tools: the model answers directly
        app.state.settings_store = FakeSettingsStore()  # /chat reads owner_timezone
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
    # A `usage` event rides after the model turn (before `done`) so the PWA's context
    # meter can read the prompt fill against the model's window (grok-4.3 here).
    assert events == [
        {"type": "text_delta", "text": "hi "},
        {"type": "text_delta", "text": "there"},
        {"type": "usage", "input_tokens": 7, "output_tokens": 3, "context_window": 256_000},
        {"type": "done", "stop_reason": "end_turn"},
    ]
    # The run was opened, the session touched, and the run closed with its summary.
    assert runlog.started == [("sess-1", "agent-system-v7")]
    assert sessions_store.touched == ["sess-1"]
    assert runlog.finished == [
        {"status": "done", "stop_reason": "end_turn", "step_count": 1, "cost_tokens": 10}
    ]


def test_chat_persists_the_turns_context_fill(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
) -> None:
    # At settle, the turn's context fill (last usage event's prompt+output = 7+3) and
    # the model's window ride onto the session, so reopening it restores the meter.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    assert resp.status_code == 200
    _ = resp.text  # drain the stream so the detached turn settles
    assert sessions_store.recorded_context == ("sess-1", 10, 256_000)


def test_chat_persists_the_exchange_to_the_transcript(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.post("/api/chat", json={"session_id": "sess-1", "message": "hello?"})
    assert transcript.recorded == [
        {
            "session_id": "sess-1",
            "run_id": "run-1",
            "user": "hello?",
            "assistant": "hi there",
            "tools": [],
            "reasoning": "",
        }
    ]


def test_chat_streams_and_persists_reasoning(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [LlmTurn("the answer", (), "end_turn", LlmUsage(1, 1), reasoning="let me think")],
        stream_chunks=[["the answer"]],
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "why?"})
    events = sse_events(resp.text)
    # The reasoning crosses the wire as its own event, ahead of the answer text.
    assert events[0] == {"type": "reasoning_delta", "text": "let me think"}
    assert {"type": "text_delta", "text": "the answer"} in events
    # And it is persisted on the assistant turn for replay.
    assert transcript.recorded[0]["reasoning"] == "let me think"


def test_chat_persists_tool_steps_with_sources(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def search(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput("found 1", (NoteSource(note_id="n1", domain="general", snippet="born"),))

    client.app.state.agent_registry = registry_with_tool("search", search)  # type: ignore[attr-defined]
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [
            LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["here you go"]],
    )
    client.post("/api/chat", json={"session_id": "sess-1", "message": "when born?"})

    rec = transcript.recorded[-1]
    assert rec["assistant"] == "here you go"
    assert rec["tools"] == [
        {
            "id": "c1",
            "name": "search",
            "ok": True,
            "summary": "found 1",
            "sources": [{"note_id": "n1", "domain": "general", "snippet": "born"}],
            # No prose streamed before the call (stream_chunks[0] == ""), so the split
            # point is 0 — the whole answer is the tool's "reply".
            "text_offset": 0,
            # No reasoning streamed before the call either, so it interleaves at the head
            # of the (empty) thinking trace.
            "reasoning_offset": 0,
        }
    ]


def test_chat_persists_a_tool_calls_arguments(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    # A sourceless tool (the web tools) carries its target only in its arguments —
    # persist them so the step replays its url/query on reopen, not an empty row.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def fetch(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput("page text")

    client.app.state.agent_registry = registry_with_tool("web_fetch", fetch)  # type: ignore[attr-defined]
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [
            LlmTurn(
                "",
                (ToolCall("c1", "web_fetch", {"url": "https://example.com"}),),
                "tool_use",
                LlmUsage(1, 1),
            ),
            LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["here"]],
    )
    client.post("/api/chat", json={"session_id": "sess-1", "message": "read it"})

    step = transcript.recorded[-1]["tools"][0]
    assert step["args"] == {"url": "https://example.com"}
    assert step["summary"] == "page text"


def test_chat_records_a_tool_calls_text_offset(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    # The prose streamed before a call is its preamble; persist where the turn's text
    # splits around the tool so the PWA replays an image turn as preamble → image →
    # reply (and live-splits the same way).
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def gen(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput("generated")

    client.app.state.agent_registry = registry_with_tool("generate_image", gen)  # type: ignore[attr-defined]
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [
            LlmTurn(
                "", (ToolCall("c1", "generate_image", {"prompt": "x"}),), "tool_use", LlmUsage(1, 1)
            ),
            LlmTurn("here it is", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[["I'll make it"], ["here it is"]],
    )
    client.post("/api/chat", json={"session_id": "sess-1", "message": "make x"})

    step = transcript.recorded[-1]["tools"][0]
    assert step["text_offset"] == len("I'll make it")  # the preamble length


def test_chat_forwards_a_reflexion_verdict_after_done(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    # A critique-worthy turn (surfaced a source) whose answer is ungrounded emits a
    # `verdict` SSE event after `done` — the default verify-and-annotate mode.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def search(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput(
            "found 1", (NoteSource(note_id="n1", domain="general", snippet="cholesterol labs"),)
        )

    client.app.state.agent_registry = registry_with_tool("search", search)  # type: ignore[attr-defined]
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [
            LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["the roof needs replacing"]],
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "labs?"})
    events = sse_events(resp.text)
    # The verdict rides last, right after the terminal done.
    assert events[-2]["type"] == "done"
    assert events[-1]["type"] == "verdict" and events[-1]["passed"] is False
    # The structured ungrounded-claim sentence crosses the wire (the PWA anchors its
    # inline flag against it), alongside the prose issues.
    assert events[-1]["ungrounded_claims"] == ["the roof needs replacing"]
    # Ephemeral: the verdict is forwarded but never written to the transcript.
    assert "verdict" not in transcript.recorded[-1]


def test_chat_forwards_a_general_knowledge_label_after_done(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    # A turn answered purely from world knowledge — no tools, no sources — emits a
    # neutral `general_knowledge` SSE event after `done`, forwarded but not recorded.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [LlmTurn("Jeff is a short form of Jeffrey.", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["Jeff is a short form of Jeffrey."]],
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "what is jeff?"})
    events = sse_events(resp.text)
    assert events[-2]["type"] == "done"
    assert events[-1]["type"] == "general_knowledge"
    # No amber verdict rode alongside it — the two are mutually exclusive.
    assert not any(e["type"] == "verdict" for e in events)
    # Ephemeral: the label is forwarded but never written to the transcript.
    assert "general_knowledge" not in transcript.recorded[-1]


def test_chat_suppresses_general_knowledge_label_for_a_non_kb_agent(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # jerv/teacher read no notes, so the "from general knowledge — not your notes"
    # label is meaningless and must not be emitted, even on a substantive answer.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-j", "", "active", (), (), NOW, NOW, agent="jerv"))
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [LlmTurn("Mount Everest is the tallest mountain.", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["Mount Everest is the tallest mountain."]],
    )
    resp = client.post("/api/chat", json={"session_id": "sess-j", "message": "tallest mountain?"})
    events = sse_events(resp.text)
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "general_knowledge" for e in events)


def test_chat_buffer_retry_gate_default_off_streams_live(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # With the gate unset (default off), the turn streams live (the streaming
    # adapter path), not the buffered produce path.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("health",), (), NOW, NOW))

    async def search(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput(
            "found 1", (NoteSource(note_id="n1", domain="health", snippet="cholesterol labs"),)
        )

    client.app.state.agent_registry = registry_with_tool("search", search)  # type: ignore[attr-defined]
    router = stream_router(
        [
            LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["the roof ", "needs replacing"]],
    )
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    fake = cast(FakeLlmClient, router._clients["xai"])
    # The streaming adapter ran (one stream call per model turn); the non-streaming
    # buffered produce path did not.
    assert len(fake.stream_calls) == 2 and fake.converse_calls == []
    # The live stream still annotates: it touched a health source (critique-worthy
    # via touched_sensitive) and the answer is ungrounded against it.
    assert sse_events(resp.text)[-1]["type"] == "verdict"


def test_chat_buffer_retry_gate_on_uses_the_buffered_path(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # Flipping the settings gate on routes the turn through buffer-then-retry: the
    # non-streaming converse path produces it, then the kept answer streams.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("health",), (), NOW, NOW))
    client.app.state.settings_store.values["reflexion_buffer_retry"] = True  # type: ignore[attr-defined]

    async def search(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput(
            "found 1", (NoteSource(note_id="n1", domain="health", snippet="cholesterol labs"),)
        )

    client.app.state.agent_registry = registry_with_tool("search", search)  # type: ignore[attr-defined]
    tool_use = LlmTurn("", (ToolCall("c1", "search", {}),), "tool_use", LlmUsage(1, 1))
    answer = LlmTurn("the roof needs replacing", (), "end_turn", LlmUsage(1, 1))
    # Both produce attempts run the tool (surface the health source) and stay
    # ungrounded → no strict improvement → the incumbent stands and a verdict rides.
    router = stream_router([tool_use, answer, tool_use, answer], stream_chunks=[])
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    fake = cast(FakeLlmClient, router._clients["xai"])
    # Buffered produce uses converse (non-streaming), not the streaming adapter.
    assert fake.converse_calls and fake.stream_calls == []
    # It touched a health source (critique-worthy) and the answer is ungrounded.
    assert sse_events(resp.text)[-1]["type"] == "verdict"


def test_chat_buffer_retry_is_forced_off_for_a_spawner(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # Even with the buffer-retry gate ON, a spawner (jerv) streams live: buffer-retry
    # re-produces the turn, which would re-dispatch spawn_subagent and re-run the whole
    # fan (new child sessions + spend) per retry — the M6 failure at the parent layer.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-j", "", "active", (), (), NOW, NOW, agent="jerv"))
    client.app.state.settings_store.values["reflexion_buffer_retry"] = True  # type: ignore[attr-defined]
    router = stream_router(
        [LlmTurn("here you go", (), "end_turn", LlmUsage(1, 1))],
        stream_chunks=[["here you go"]],
    )
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    resp = client.post("/api/chat", json={"session_id": "sess-j", "message": "hi"})
    fake = cast(FakeLlmClient, router._clients["xai"])
    # The streaming adapter ran; the non-streaming buffered produce path did not.
    assert fake.stream_calls and fake.converse_calls == []
    assert sse_events(resp.text)[-1]["type"] == "done"


def test_chat_persists_proposal_and_entity_chips(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def find(arguments, ctx):  # type: ignore[no-untyped-def]
        # A tool that both stages a proposal and resolves an entity — both chips
        # must persist alongside the (empty) note sources.
        return ToolOutput(
            "staged",
            proposal=ProposalRef(proposal_id="p1", kind="correction"),
            entities=(EntityRef(entity_id="e1", label="Me", domain="general"),),
        )

    client.app.state.agent_registry = registry_with_tool("find_entity", find)  # type: ignore[attr-defined]
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [
            LlmTurn("", (ToolCall("c1", "find_entity", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["that is you"]],
    )
    client.post("/api/chat", json={"session_id": "sess-1", "message": "who am i?"})

    step = transcript.recorded[-1]["tools"][0]
    assert step["proposal"] == {"proposal_id": "p1", "kind": "correction"}
    assert step["entities"] == [
        {
            "kind": "entity",
            "entity_id": "e1",
            "label": "Me",
            "domain": "general",
            "aliases": [],
            "facts": [],
        }
    ]


def test_chat_persists_a_tool_view(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def show(arguments, ctx):  # type: ignore[no-untyped-def]
        return ToolOutput("here", view=ViewPayload(view="list_card", data={"title": "Groceries"}))

    client.app.state.agent_registry = registry_with_tool("read_list", show)  # type: ignore[attr-defined]
    client.app.state.llm_router = stream_router(  # type: ignore[attr-defined]
        [
            LlmTurn("", (ToolCall("c1", "read_list", {}),), "tool_use", LlmUsage(1, 1)),
            LlmTurn("done", (), "end_turn", LlmUsage(1, 1)),
        ],
        stream_chunks=[[""], ["your list"]],
    )
    client.post("/api/chat", json={"session_id": "sess-1", "message": "show my list"})

    # The view rides on its tool step so the bubble replays the card on reopen.
    step = transcript.recorded[-1]["tools"][0]
    assert step["view"]["view"] == "list_card"
    assert step["view"]["data"] == {"title": "Groceries"}


def test_session_transcript_endpoint_replays_stored_turns(
    client: TestClient, repo: FakeAuthRepo, transcript: FakeTranscript
) -> None:
    login(client, repo)
    transcript.turns["sess-1"] = [
        TurnRecord("user", "when born?"),
        TurnRecord(
            "assistant",
            "March 19, 1986.",
            [
                {
                    "id": "c1",
                    "name": "search",
                    "ok": True,
                    "sources": [{"note_id": "n1", "domain": "general", "snippet": "born"}],
                }
            ],
            reasoning="checked the birthday note",
        ),
    ]
    resp = client.get("/api/sessions/sess-1/transcript")
    assert resp.status_code == 200
    data = resp.json()
    assert [t["role"] for t in data] == ["user", "assistant"]
    assert data[1]["content"] == "March 19, 1986."
    assert data[1]["tools"][0]["sources"][0]["note_id"] == "n1"
    # The stored reasoning replays so the "thinking" disclosure reopens (collapsed).
    assert data[1]["reasoning"] == "checked the birthday note"
    assert data[0]["reasoning"] == ""


def test_transcript_endpoint_requires_owner(client: TestClient) -> None:
    assert client.get("/api/sessions/sess-1/transcript").status_code == 401


def test_chat_history_is_replayed_into_the_turn(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])

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
    # The loop received prior turns plus the new user message, in order — after the
    # ambient date/time block that now leads every turn (filtered out here).
    msgs = fake.stream_calls[0]["messages"]
    sent = [m for m in msgs if _CLOCK_FRAME not in getattr(m, "text", "")]
    assert [type(m).__name__ for m in sent] == [
        "UserMessage",
        "AssistantMessage",
        "UserMessage",
    ]
    assert sent[-1].text == "and the second?"


class _FakeAnalysisRepo:
    """Just the owner_entity_id slice the ambient owner-self block reads."""

    def __init__(self, entity_id: str | None) -> None:
        self._id = entity_id

    async def owner_entity_id(self, ctx) -> str | None:  # type: ignore[no-untyped-def]
        return self._id


def test_chat_injects_the_owner_self_block_for_a_kb_agent(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # A knowledge-base turn is handed the owner's "Me" entity id up front, so an
    # owner self-attribute ("my birthday") is one read_entity — no find_entity hop.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.analysis_repo = _FakeAnalysisRepo("me-123")  # type: ignore[attr-defined]
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])

    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "what's my birthday"})
    assert resp.status_code == 200
    me_lines = [m for m in fake.stream_calls[0]["messages"] if _ME_FRAME in getattr(m, "text", "")]
    assert len(me_lines) == 1 and "me-123" in me_lines[0].text


def test_chat_omits_the_owner_self_block_for_jerv(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # jerv reads no owner data (reads_knowledge_base False) — the owner's entity id
    # must never ride into its sandboxed context, even if the repo could resolve it.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-j", "", "active", (), (), NOW, NOW, agent="jerv"))
    client.app.state.analysis_repo = _FakeAnalysisRepo("me-123")  # type: ignore[attr-defined]
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])

    resp = client.post("/api/chat", json={"session_id": "sess-j", "message": "tallest mountain?"})
    assert resp.status_code == 200
    assert not any(_ME_FRAME in getattr(m, "text", "") for m in fake.stream_calls[0]["messages"])


def test_chat_attachments_ride_the_final_user_message(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    chat_attachments: FakeChatAttachments,
    chat_blobs: FakeChatBlobs,
) -> None:
    import base64

    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("health",), (), NOW, NOW))
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])
    chat_blobs.data["sha-img"] = b"\x89PNGdata"
    chat_blobs.data["sha-txt"] = b"some notes"
    chat_attachments.add(AttachmentInfo("a1", "scan.png", "image/png", 8, "sha-img", "health"))
    chat_attachments.add(AttachmentInfo("a2", "notes.txt", "text/plain", 10, "sha-txt", "health"))

    resp = client.post(
        "/api/chat",
        json={"session_id": "sess-1", "message": "what is this?", "attachment_ids": ["a1", "a2"]},
    )
    assert resp.status_code == 200
    # The FINAL user message carries the image and the appended text-file block.
    final = fake.stream_calls[0]["messages"][-1]
    assert type(final).__name__ == "UserMessage"
    assert [im.media_type for im in final.images] == ["image/png"]
    assert base64.b64decode(final.images[0].data) == b"\x89PNGdata"
    assert "what is this?" in final.text
    assert "[notes.txt]:" in final.text and "some notes" in final.text


def test_chat_binds_attachments_to_the_user_turn(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    chat_attachments: FakeChatAttachments,
    chat_blobs: FakeChatBlobs,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("health",), (), NOW, NOW))
    chat_blobs.data["sha-img"] = b"img"
    chat_attachments.add(AttachmentInfo("a1", "scan.png", "image/png", 3, "sha-img", "health"))

    client.post(
        "/api/chat",
        json={"session_id": "sess-1", "message": "hi", "attachment_ids": ["a1"]},
    )
    # The ids were bound to the recorded USER turn, under the SESSION's narrowed scope.
    assert chat_attachments.bound == [(("health",), ["a1"], "turn-1")]


def test_chat_without_attachments_sends_no_images_and_binds_nothing(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    chat_attachments: FakeChatAttachments,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])

    client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    assert fake.stream_calls[0]["messages"][-1].images == ()
    assert chat_attachments.bound == []


def test_chat_appointment_id_rides_the_turn_not_the_transcript(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])
    appt_id = "2c5ea4dc-36eb-4d2a-a922-362b9e155667"

    resp = client.post(
        "/api/chat",
        json={"session_id": "sess-1", "message": "what's this about?", "appointment_id": appt_id},
    )
    assert resp.status_code == 200
    # The model turn gains the id as an explicit read_appointment instruction…
    sent = fake.stream_calls[0]["messages"]
    assert appt_id in sent[-1].text
    assert "read_appointment" in sent[-1].text
    # …but the persisted transcript keeps the owner's words verbatim.
    assert transcript.recorded[-1]["user"] == "what's this about?"


def test_chat_ignores_a_non_uuid_appointment_id(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    router: LlmRouter = client.app.state.llm_router  # type: ignore[attr-defined]
    fake = cast(FakeLlmClient, router._clients["xai"])

    resp = client.post(
        "/api/chat",
        json={"session_id": "sess-1", "message": "hi", "appointment_id": "not-a-uuid"},
    )
    assert resp.status_code == 200
    # A malformed id never reaches the prompt — the message is sent clean.
    assert fake.stream_calls[0]["messages"][-1].text == "hi"


def test_chat_high_reasoning_effort_widens_the_tool_step_cap(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
) -> None:
    # agent.turn stored at high effort on a reasoning-capable model → the loop's step
    # cap widens to 40. A model that always asks for a tool runs the full 40 steps.
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))

    async def search(arguments, ctx):  # type: ignore[no-untyped-def]
        return "found"

    async def loader() -> dict[str, dict[str, str]]:
        return {"agent.turn": {"reasoning_effort": "high"}}

    fake = FakeLlmClient(
        turns=[LlmTurn("", (ToolCall("c", "search", {}),), "tool_use", LlmUsage(1, 1))]
    )
    client.app.state.agent_registry = registry_with_tool("search", search)  # type: ignore[attr-defined]
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}, overrides_loader=loader
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "dig deep"})
    assert resp.status_code == 200
    assert runlog.finished[-1]["stop_reason"] == "max_steps"
    # The model was asked 40 times before the widened cap stopped it (20 by default).
    assert len(fake.stream_calls) == 40


def test_chat_model_failure_emits_error_done_and_marks_run_failed(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": cast(LlmClient, BoomStreamClient())}, {"agent.turn": ("xai", "grok-4.3")}
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    # The failure surfaces as a terminal event, never a 500, and the run is closed.
    assert resp.status_code == 200
    assert sse_events(resp.text)[-1] == {"type": "done", "stop_reason": "error"}
    assert runlog.finished[-1]["status"] == "error"
    assert runlog.finished[-1]["stop_reason"] == "error"


def test_chat_turn_wall_clock_force_ends_a_runaway_turn(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that runs past the hard wall-clock is force-ended — the timeout cancels the
    in-flight LLM call (and would cascade into any sub-agents) and the run closes
    `turn_timeout` rather than pegging the GPU."""
    import jbrain.api.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_MAX_TURN_WALL_CLOCK_S", 0.1)
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": cast(LlmClient, HangStreamClient())}, {"agent.turn": ("xai", "grok-4.3")}
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    assert resp.status_code == 200
    assert sse_events(resp.text)[-1] == {"type": "done", "stop_reason": "turn_timeout"}
    assert runlog.finished[-1]["status"] == "error"
    assert runlog.finished[-1]["stop_reason"] == "turn_timeout"


def test_chat_turn_timeout_persists_the_partial_turn(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
    transcript: FakeTranscript,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn force-ended by the hard wall-clock must PERSIST whatever streamed (the
    partial answer + tools), not drop the whole exchange. The `finally` guard now
    includes `turn_timeout` alongside disconnected/error — without it a timed-out fan
    would vanish on reload (no assistant turn, no user turn). The HangStreamClient
    streams "working " before hanging; that partial is the durable artifact."""
    import jbrain.api.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_MAX_TURN_WALL_CLOCK_S", 0.1)
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": cast(LlmClient, HangStreamClient())}, {"agent.turn": ("xai", "grok-4.3")}
    )
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "how far?"})
    assert resp.status_code == 200
    # The exchange WAS recorded — both the user turn (record_exchange writes both) and
    # the partial assistant answer streamed before the wall-clock fired.
    assert transcript.recorded, "a timed-out turn must persist its partial, not drop it"
    assert transcript.recorded[-1]["user"] == "how far?"
    assert transcript.recorded[-1]["assistant"] == "working "
    assert runlog.finished[-1]["stop_reason"] == "turn_timeout"


def test_chat_persists_a_partial_answer_when_the_owner_stops(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": cast(LlmClient, CancelStreamClient())}, {"agent.turn": ("xai", "grok-4.3")}
    )
    # The owner Stops (or the connection drops) after a partial answer streamed: the
    # CancelledError unwinds the request, but the finally still persists what streamed.
    with contextlib.suppress(BaseException):
        client.post("/api/chat", json={"session_id": "sess-1", "message": "how far?"})
    # The partial answer was recorded so reopening the chat replays what the owner saw.
    assert transcript.recorded[-1]["user"] == "how far?"
    assert transcript.recorded[-1]["assistant"] == "the drive is about "
    # The run still closes as error/disconnected — a partial turn is not a clean `done`.
    assert runlog.finished[-1]["status"] == "error"
    assert runlog.finished[-1]["stop_reason"] == "disconnected"


def test_chat_persists_a_partial_turn_when_the_compose_step_errors(
    client: TestClient,
    repo: FakeAuthRepo,
    sessions_store: FakeAgentSessions,
    runlog: FakeRunLog,
    transcript: FakeTranscript,
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": cast(LlmClient, BoomAfterTextClient())}, {"agent.turn": ("xai", "grok-4.3")}
    )
    # A mid-turn error (not a Stop) after the model streamed — the shape of a render's
    # compose call breaking once a side-effecting tool has run. The finally must persist
    # the partial turn so reopening the chat replays it and the work isn't silently lost.
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "make an image"})
    assert resp.status_code == 200
    assert sse_events(resp.text)[-1] == {"type": "done", "stop_reason": "error"}
    # The partial answer was recorded even though the turn ended in error (not disconnect).
    assert transcript.recorded[-1]["user"] == "make an image"
    assert transcript.recorded[-1]["assistant"] == "I've generated a "
    assert runlog.finished[-1]["status"] == "error"
    assert runlog.finished[-1]["stop_reason"] == "error"


async def test_live_turn_replays_from_offset_then_follows_live() -> None:
    # The reconnect primitive: a subscriber joining at `after` replays the buffered tail,
    # then follows live frames, and ends when the turn finishes.
    from jbrain.api.agent import _LiveTurn

    live = _LiveTurn()
    live.emit(b"data: 1\n\n")
    live.emit(b"data: 2\n\n")

    got: list[bytes] = []

    async def consume() -> None:
        async for frame in live.stream(after=1):  # skip frame 1, resume at 2
            got.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let it backfill + register as a live subscriber
    live.emit(b"data: 3\n\n")
    await asyncio.sleep(0)
    live.finish()
    await asyncio.wait_for(task, timeout=1.0)

    assert got == [b"data: 2\n\n", b"data: 3\n\n"]


async def test_live_turn_subscribe_after_finish_replays_tail_and_ends() -> None:
    # A reconnect that arrives just as the turn ends still gets the buffered frames and a
    # clean end (no hang) — the broker replays the tail and terminates immediately.
    from jbrain.api.agent import _LiveTurn

    live = _LiveTurn()
    live.emit(b"data: 1\n\n")
    live.finish()

    got = [frame async for frame in live.stream(after=0)]
    assert got == [b"data: 1\n\n"]


async def test_live_turn_stream_clamps_out_of_range_offsets() -> None:
    # A reconnect's `after` is owner-supplied; out-of-range values must be safe — negative
    # clamps to a full replay, past-the-end yields nothing, and both end cleanly.
    from jbrain.api.agent import _LiveTurn

    live = _LiveTurn()
    live.emit(b"data: 1\n\n")
    live.emit(b"data: 2\n\n")
    live.finish()

    assert [frame async for frame in live.stream(after=-5)] == [b"data: 1\n\n", b"data: 2\n\n"]
    assert [frame async for frame in live.stream(after=99)] == []


def test_live_turn_evicts_oldest_frames_past_the_cap() -> None:
    # The memory backstop (fix #4): a runaway turn that streams past _MAX_BUFFERED_FRAMES
    # evicts the OLDEST frames off the front rather than growing unbounded, and advances
    # `_base` (the absolute index of frames[0]) by the count evicted.
    import jbrain.api.agent as agent_mod
    from jbrain.api.agent import _LiveTurn

    cap = agent_mod._MAX_BUFFERED_FRAMES
    live = _LiveTurn()
    for i in range(cap + 5):
        live.emit(f"data: {i}\n\n".encode())
    # The buffer is bounded at the cap; the first 5 frames were evicted, so `_base` is 5
    # and frames[0] is now logical frame #5.
    assert len(live.frames) == cap
    assert live._base == 5
    assert live.frames[0] == b"data: 5\n\n"
    assert live.frames[-1] == f"data: {cap + 4}\n\n".encode()


async def test_live_turn_replay_after_offset_translates_across_eviction() -> None:
    # `after` stays an ABSOLUTE event index even after eviction: a reconnect at an offset
    # that lands inside the surviving window replays from exactly that frame (translated
    # past `_base`), and a reconnect before the evicted point clamps to the oldest survivor
    # (degrades, never breaks — fix #3 re-creates a child whose spawn frame is gone).
    import jbrain.api.agent as agent_mod
    from jbrain.api.agent import _LiveTurn

    cap = agent_mod._MAX_BUFFERED_FRAMES
    live = _LiveTurn()
    for i in range(cap + 5):  # evicts frames 0..4; survivors are 5..cap+4
        live.emit(f"data: {i}\n\n".encode())
    live.finish()

    # An absolute offset inside the surviving window resumes at exactly that frame.
    got = [frame async for frame in live.stream(after=cap + 2)]
    assert got == [
        f"data: {cap + 2}\n\n".encode(),
        f"data: {cap + 3}\n\n".encode(),
        f"data: {cap + 4}\n\n".encode(),
    ]
    # An offset BEFORE the evicted boundary (the client missed the gap) clamps to the
    # oldest survivor — it replays from frame #5 on, never re-delivering an evicted frame.
    head = [frame async for frame in live.stream(after=0)]
    assert head[0] == b"data: 5\n\n"
    assert len(head) == cap


async def test_live_turn_evicted_reconnect_follows_live_across_the_boundary() -> None:
    # A reconnect that lands at an absolute offset PAST the buffered tail (frames it hasn't
    # caught up to yet are still coming) backfills the surviving window and then follows the
    # live frames in order — the eviction/`_base` accounting holds across the snapshot→live
    # seam, and the keepalive offset bookkeeping (data frames only) is unchanged.
    import jbrain.api.agent as agent_mod
    from jbrain.api.agent import _LiveTurn

    cap = agent_mod._MAX_BUFFERED_FRAMES
    live = _LiveTurn()
    for i in range(cap + 3):  # evicts 0..2; survivors 3..cap+2, `_base` == 3
        live.emit(f"data: {i}\n\n".encode())
    assert live._base == 3

    got: list[bytes] = []

    async def consume() -> None:
        # Resume from an absolute index mid-window; must backfill that tail then go live.
        async for frame in live.stream(after=cap + 1):
            got.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let it backfill + register as a live subscriber
    live.emit(f"data: {cap + 3}\n\n".encode())  # a NEW live frame after the reconnect
    await asyncio.sleep(0)
    live.finish()
    await asyncio.wait_for(task, timeout=1.0)

    # Backfilled cap+1, cap+2 (the surviving tail from the offset) then the live cap+3 —
    # in order, no gap, no re-delivery of an evicted frame.
    assert got == [
        f"data: {cap + 1}\n\n".encode(),
        f"data: {cap + 2}\n\n".encode(),
        f"data: {cap + 3}\n\n".encode(),
    ]


def test_chat_resume_requires_owner(client: TestClient) -> None:
    assert client.get("/api/chat/runs/run-1/stream").status_code == 401


def test_chat_resume_unknown_run_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    # No live run by that id → 404, so the client falls back to the transcript.
    assert client.get("/api/chat/runs/ghost/stream").status_code == 404


def test_chat_response_carries_the_run_id_header(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    # The PWA reads the run id off the response header so its Stop can target the
    # detached turn (which no longer dies when the SSE stream closes).
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["x-run-id"] == "run-1"


def test_chat_cancel_requires_owner(client: TestClient) -> None:
    assert client.post("/api/chat/runs/run-1/cancel").status_code == 401


def test_chat_cancel_cancels_a_registered_run(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)

    class _StubTurn:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    stub = _StubTurn()
    client.app.state.live_turns["run-9"] = stub  # type: ignore[attr-defined]
    resp = client.post("/api/chat/runs/run-9/cancel")
    assert resp.status_code == 204
    assert stub.cancelled
    # The stub isn't a real awaitable Task; drop it so the fixture's lifespan shutdown
    # (which gathers in-flight turns) doesn't try to await it.
    client.app.state.live_turns.clear()  # type: ignore[attr-defined]


def test_chat_cancel_is_idempotent_for_unknown_run(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    # An unknown/finished run is a no-op, not an error — the Stop can race the turn's
    # own completion.
    assert client.post("/api/chat/runs/ghost/cancel").status_code == 204


def test_chat_completed_turn_deregisters_from_live_turns(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    client.post("/api/chat", json={"session_id": "sess-1", "message": "hi"})
    # The done-callback popped the finished turn, so the registry is empty again.
    assert client.app.state.live_turns == {}  # type: ignore[attr-defined]


class GatedStreamClient:
    """Streams a partial answer, then BLOCKS on a release event before finishing — lets a
    test drop the SSE connection mid-turn and prove the detached turn still completes."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    async def converse_stream(self, **_kw):  # type: ignore[no-untyped-def]
        yield TextChunk(text="partial ")
        await self._release.wait()
        yield TextChunk(text="answer")


async def test_chat_turn_survives_a_client_disconnect() -> None:
    # The core fix: a backgrounded PWA dropping the socket mid-turn must NOT cancel the
    # turn. We open the stream, read the first frame, exit the stream context early
    # (the disconnect), release the gated model, and assert the detached turn still
    # persisted a clean `done`.
    import httpx

    repo = FakeAuthRepo()
    sessions_store = FakeAgentSessions()
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    runlog = FakeRunLog()
    transcript = FakeTranscript()
    release = asyncio.Event()

    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    # ASGITransport does NOT run lifespan, so wire the state the endpoint reads by hand.
    app.state.live_turns = {}
    app.state.auth_repo = repo
    app.state.agent_sessions = sessions_store
    app.state.agent_runlog = runlog
    app.state.agent_transcript = transcript
    app.state.turn_attachments = FakeChatAttachments()
    app.state.blob_store = FakeChatBlobs()
    app.state.agent_registry = ToolRegistry([])
    app.state.settings_store = FakeSettingsStore()
    app.state.llm_router = LlmRouter(
        {"xai": cast(LlmClient, GatedStreamClient(release))}, {"agent.turn": ("xai", "grok-4.3")}
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        key = await service.rotate_owner_key(repo)
        assert (await ac.post("/api/auth/session", json={"owner_key": key})).status_code == 204

        # Read the stream in a task so we can cut it mid-turn the way a dropped socket
        # would. The task is cancelled (the disconnect) BEFORE the gate releases — proving
        # the turn isn't waiting on the connection.
        async def read_first_frame() -> None:
            async with ac.stream(
                "POST", "/api/chat", json={"session_id": "sess-1", "message": "how far?"}
            ) as r:
                assert r.status_code == 200
                async for _chunk in r.aiter_bytes():
                    return  # first frame arrived; hold the open stream until cancelled

        reader = asyncio.create_task(read_first_frame())
        # Wait until the detached turn has been registered — it is now mid-flight,
        # blocked on the gated model.
        for _ in range(200):
            if app.state.live_turns or reader.done():
                break
            await asyncio.sleep(0.01)

        # The turn registered (is mid-flight) at the moment we drop the connection — so
        # what survives below is a genuinely in-flight turn, not one that already finished.
        assert app.state.live_turns, "the detached turn registered before the disconnect"

        reader.cancel()  # the PWA backgrounds / the socket drops
        with contextlib.suppress(asyncio.CancelledError):
            await reader

        # The client is gone; release the gated model so the DETACHED turn finishes.
        release.set()

        # The turn runs independently of the (now-closed) response — poll briefly for it
        # to complete and persist.
        for _ in range(200):
            if transcript.recorded and runlog.finished:
                break
            await asyncio.sleep(0.01)

    assert transcript.recorded, "the detached turn persisted the exchange despite disconnect"
    assert transcript.recorded[-1]["assistant"] == "partial answer"
    assert runlog.finished[-1]["status"] == "done"


async def test_chat_cancel_endpoint_persists_the_partial_and_closes_the_stream() -> None:
    # The real Stop path end-to-end: a connected client streams a turn, the owner taps
    # Stop (POST /chat/runs/{id}/cancel) mid-answer, and we prove (a) the partial answer
    # persists, (b) the run closes error/disconnected, and (c) the sentinel still reaches
    # the connected response so the SSE stream terminates instead of hanging.
    import httpx

    repo = FakeAuthRepo()
    sessions_store = FakeAgentSessions()
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    runlog = FakeRunLog()
    transcript = FakeTranscript()
    release = asyncio.Event()  # never set: the turn blocks here until the Stop cancels it

    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    app.state.live_turns = {}
    app.state.auth_repo = repo
    app.state.agent_sessions = sessions_store
    app.state.agent_runlog = runlog
    app.state.agent_transcript = transcript
    app.state.turn_attachments = FakeChatAttachments()
    app.state.blob_store = FakeChatBlobs()
    app.state.agent_registry = ToolRegistry([])
    app.state.settings_store = FakeSettingsStore()
    app.state.llm_router = LlmRouter(
        {"xai": cast(LlmClient, GatedStreamClient(release))}, {"agent.turn": ("xai", "grok-4.3")}
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        key = await service.rotate_owner_key(repo)
        assert (await ac.post("/api/auth/session", json={"owner_key": key})).status_code == 204

        async def read_all() -> list[bytes]:
            frames: list[bytes] = []
            async with ac.stream(
                "POST", "/api/chat", json={"session_id": "sess-1", "message": "how far?"}
            ) as r:
                assert r.status_code == 200
                async for chunk in r.aiter_bytes():
                    frames.append(chunk)
            return frames

        reader = asyncio.create_task(read_all())
        for _ in range(200):
            if app.state.live_turns:
                break
            await asyncio.sleep(0.01)
        run_id = next(iter(app.state.live_turns))

        # The owner taps Stop — the explicit cancel the detached turn now needs.
        cancel = await ac.post(f"/api/chat/runs/{run_id}/cancel")
        assert cancel.status_code == 204

        # The sentinel reaches the connected response, so the stream terminates (no hang).
        frames = await asyncio.wait_for(reader, timeout=5.0)

    assert b"partial " in b"".join(frames)
    # The partial answer was persisted, and the run closed as a (benign) disconnect.
    assert transcript.recorded[-1]["assistant"] == "partial "
    assert runlog.finished[-1]["status"] == "error"
    assert runlog.finished[-1]["stop_reason"] == "disconnected"


async def test_chat_resume_streams_the_rest_of_a_live_turn() -> None:
    # The live-resume path end-to-end: the PWA's stream drops mid-turn, then it reconnects
    # (GET /chat/runs/{id}/stream?after=N) and receives the frames it MISSED — the live
    # tail — not a replay of what it already saw, and follows to completion.
    import httpx

    repo = FakeAuthRepo()
    sessions_store = FakeAgentSessions()
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    runlog = FakeRunLog()
    transcript = FakeTranscript()
    release = asyncio.Event()  # holds the turn mid-answer until the resume is attached

    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    app.state.live_turns = {}
    app.state.auth_repo = repo
    app.state.agent_sessions = sessions_store
    app.state.agent_runlog = runlog
    app.state.agent_transcript = transcript
    app.state.turn_attachments = FakeChatAttachments()
    app.state.blob_store = FakeChatBlobs()
    app.state.agent_registry = ToolRegistry([])
    app.state.settings_store = FakeSettingsStore()
    app.state.llm_router = LlmRouter(
        {"xai": cast(LlmClient, GatedStreamClient(release))}, {"agent.turn": ("xai", "grok-4.3")}
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        key = await service.rotate_owner_key(repo)
        assert (await ac.post("/api/auth/session", json={"owner_key": key})).status_code == 204

        # Open the turn and hold the stream in a task; cancel it (the OS-killed-socket
        # shape) once the detached turn has buffered its first frame — the "partial " the
        # gated model emits before it blocks, the one the client "already saw".
        async def hold_post() -> None:
            async with ac.stream(
                "POST", "/api/chat", json={"session_id": "sess-1", "message": "how far?"}
            ) as r:
                assert r.status_code == 200
                async for _chunk in r.aiter_bytes():
                    pass  # keep the connection open until cancelled

        reader = asyncio.create_task(hold_post())
        for _ in range(200):
            if app.state.live_turns:
                break
            await asyncio.sleep(0.01)
        run_id = next(iter(app.state.live_turns))
        live = app.state.live_turns[run_id]
        for _ in range(200):
            if len(live.frames) >= 1:
                break
            await asyncio.sleep(0.01)
        assert len(live.frames) >= 1 and b"partial " in live.frames[0]
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader

        # Reconnect skipping the one frame already seen, and collect the live tail.
        rest = b""

        async def resume() -> None:
            nonlocal rest
            async with ac.stream("GET", f"/api/chat/runs/{run_id}/stream?after=1") as r:
                assert r.status_code == 200
                async for chunk in r.aiter_bytes():
                    rest += chunk

        rtask = asyncio.create_task(resume())
        await asyncio.sleep(0.05)  # let the resume subscribe before releasing the gate
        release.set()
        await asyncio.wait_for(rtask, timeout=5.0)

    # The resume delivered the live tail (the "answer" text + the terminal done)…
    assert b"answer" in rest
    assert b"end_turn" in rest
    # …and did NOT replay the already-seen first frame.
    assert b"partial " not in rest


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


def test_rename_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "old", "active", ("general",), (), NOW, NOW))
    resp = client.patch("/api/sessions/sess-1", json={"title": "renamed"})
    assert resp.status_code == 204
    assert sessions_store._by_id["sess-1"].title == "renamed"


def test_delete_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    resp = client.delete("/api/sessions/sess-1")
    assert resp.status_code == 204
    assert client.get("/api/sessions").json() == []


def test_rename_and_delete_require_owner(client: TestClient) -> None:
    assert client.patch("/api/sessions/sess-1", json={"title": "x"}).status_code == 401
    assert client.delete("/api/sessions/sess-1").status_code == 401


def test_archive_and_unarchive_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "x", "active", ("general",), (), NOW, NOW))
    assert client.post("/api/sessions/sess-1/archive").status_code == 204
    assert sessions_store._by_id["sess-1"].status == "archived"
    assert client.post("/api/sessions/sess-1/unarchive").status_code == 204
    assert sessions_store._by_id["sess-1"].status == "active"


def test_archive_requires_owner(client: TestClient) -> None:
    assert client.post("/api/sessions/sess-1/archive").status_code == 401
    assert client.post("/api/sessions/sess-1/unarchive").status_code == 401


def test_rescope_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "x", "active", ("general",), (), NOW, NOW))
    resp = client.post("/api/sessions/sess-1/scope", json={"domain_scopes": ["general", "health"]})
    assert resp.status_code == 204
    assert sessions_store._by_id["sess-1"].domain_scopes == ("general", "health")


def test_rescope_requires_owner(client: TestClient) -> None:
    assert (
        client.post("/api/sessions/sess-1/scope", json={"domain_scopes": ["general"]}).status_code
        == 401
    )


def test_list_carries_card_metadata(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "x", "active", ("general",), (), NOW, NOW))
    card = client.get("/api/sessions").json()[0]
    # The card fields are always present on the wire (0/"" when the fake omits them).
    assert card["turn_count"] == 0
    assert card["preview"] == ""
    assert card["staged_count"] == 0


def test_chat_autotitles_an_untitled_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    fake = FakeLlmClient(
        responses=["Weekly Recap"],  # what the titler's complete() returns
        turns=[LlmTurn("hi there", (), "end_turn", LlmUsage(7, 3))],
        stream_chunks=[["hi ", "there"]],
    )
    client.app.state.llm_router = LlmRouter(  # type: ignore[attr-defined]
        {"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}
    )
    client.post("/api/chat", json={"session_id": "sess-1", "message": "what happened this week?"})
    # The first turn names the chat; the question reached the titler.
    assert sessions_store._by_id["sess-1"].title == "Weekly Recap"
    assert any("what happened this week?" in c["user_text"] for c in fake.calls)


def test_chat_does_not_retitle_a_named_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "My Chat", "active", ("general",), (), NOW, NOW))
    client.post("/api/chat", json={"session_id": "sess-1", "message": "anything"})
    # An owner-named chat is left alone — auto-titling only fills an empty title.
    assert sessions_store._by_id["sess-1"].title == "My Chat"


def _capturing_router() -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(
        turns=[LlmTurn("ok", (), "end_turn", LlmUsage(1, 1))], stream_chunks=[["ok"]]
    )
    return LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}), fake


def _web_registry() -> ToolRegistry:
    """A registry holding only the two web tools, bound to inert handlers — enough
    to assert which tools the endpoint offers a given agent."""
    import jbrain.agent.readtools as readtools
    from jbrain.agent.toolfile import load_tool
    from jbrain.agent.webtools import build_web_handlers
    from jbrain.web import SearxngClient, WebFetcher

    handlers = build_web_handlers(SearxngClient(""), WebFetcher())
    return ToolRegistry(
        [
            RegisteredTool(load_tool(readtools.TOOLS_DIR / f), handlers[n])
            for n, f in (("web_search", "web_search.tool"), ("web_fetch", "web_fetch.tool"))
        ]
    )


def test_chat_runs_the_selected_agents_prompt_and_only_its_tools(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    """A jerv session runs the jerv persona prompt with ONLY the web tools, reads
    no owner data (empty scope), and stamps the jerv version."""
    from jbrain.agent.agents import AGENTS

    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-j", "", "active", (), (), NOW, NOW, agent="jerv"))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    client.app.state.agent_registry = _web_registry()  # type: ignore[attr-defined]

    resp = client.post("/api/chat", json={"session_id": "sess-j", "message": "hi"})
    assert resp.status_code == 200
    call = fake.stream_calls[0]
    assert call["system"] == AGENTS["jerv"].prompt
    assert {t.name for t in call["tools"]} == {"web_search", "web_fetch"}
    # The run carries its version.
    assert ("sess-j", "agent-jerv-v23") in client.app.state.agent_runlog.started  # type: ignore[attr-defined]


def test_chat_curator_is_offered_no_web_tools(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    """The Full Brain curator (the default) never gains the opt-in web tools, even
    though they're registered — the invariant-#9 guard for the main agent."""
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-c", "", "active", ("general",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    client.app.state.agent_registry = _web_registry()  # type: ignore[attr-defined]

    resp = client.post("/api/chat", json={"session_id": "sess-c", "message": "hi"})
    assert resp.status_code == 200
    assert fake.stream_calls[0]["tools"] == []


# --- L7b: the data-framed owner-presence injection --------------------------
# The app-open presence reaches the agent as a PREPENDED data-framed UserMessage in
# the conversation channel — NOT the system prompt
# (run_stream hardcodes SYSTEM_PROMPT, so a system injection would silently no-op).
# Owner-gated: present only when the session holds the `location` scope AND the read
# runs as a full owner; absent for a narrowed (non-location) session. Freshness-honest.

from datetime import timedelta  # noqa: E402

from jbrain.locations import FixPoint, LatestPlace, NearestFix  # noqa: E402
from jbrain.locations.presence import _PRESENCE_FRAME  # noqa: E402


class _FakeLocationRepo:
    def __init__(self, *, near: NearestFix | None, place: LatestPlace | None) -> None:
        self._near = near
        self._place = place

    async def device_activity(self, ctx):  # noqa: ANN001, ANN201
        return {}

    async def nearest_fix(self, ctx, *, subject_id, at, max_gap_seconds):  # noqa: ANN001, ANN201
        return self._near

    async def latest_place(self, ctx, *, subject_id):  # noqa: ANN001, ANN201
        return self._place


class _FakeDeviceRepo:
    def __init__(self, subs: list[str]) -> None:
        self._subs = subs

    async def owner_device_subjects(self, ctx):  # noqa: ANN001, ANN201
        return self._subs

    async def list(self, ctx):  # noqa: ANN001, ANN201
        return []


def _wire_presence(client: TestClient, *, near, place, subs):  # noqa: ANN001, ANN202
    client.app.state.location_repo = _FakeLocationRepo(near=near, place=place)  # type: ignore[attr-defined]
    client.app.state.device_repo = _FakeDeviceRepo(subs)  # type: ignore[attr-defined]


def _fresh_near() -> NearestFix:
    captured = datetime.now(UTC) - timedelta(minutes=4)
    return NearestFix(fix=FixPoint(captured, 40.0, -74.0, 10, 80), gap_seconds=240)


def _stale_near() -> NearestFix:
    captured = datetime.now(UTC) - timedelta(hours=3)
    return NearestFix(fix=FixPoint(captured, 40.0, -74.0, 10, 80), gap_seconds=3 * 3600)


def test_chat_prepends_presence_as_data_framed_user_message(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    # A location-scoped session → presence is injected.
    sessions_store.add(AgentSessionInfo("sess-loc", "", "active", ("location",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    _wire_presence(client, near=_fresh_near(), place=LatestPlace("e", "Home", NOW), subs=["s1"])

    resp = client.post("/api/chat", json={"session_id": "sess-loc", "message": "where am I?"})
    assert resp.status_code == 200
    msgs = fake.stream_calls[0]["messages"]
    # It is a UserMessage in the conversation channel (data-framed), NOT a system change.
    framed = [m for m in msgs if _PRESENCE_FRAME in getattr(m, "text", "")]
    assert len(framed) == 1
    assert type(framed[0]).__name__ == "UserMessage"
    assert "currently at Home" in framed[0].text
    # Prepended BEFORE the user's actual message.
    texts = [getattr(m, "text", "") for m in msgs]
    assert texts.index(framed[0].text) < texts.index("where am I?")


def test_chat_presence_is_freshness_honest(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-stale", "", "active", ("location",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    _wire_presence(client, near=_stale_near(), place=LatestPlace("e", "Office", NOW), subs=["s1"])

    client.post("/api/chat", json={"session_id": "sess-stale", "message": "hi"})
    joined = "\n".join(getattr(m, "text", "") for m in fake.stream_calls[0]["messages"])
    # A stale fix reads "last known", never "currently at"/"here now".
    assert "last known" in joined and "Office" in joined
    assert "currently at" not in joined and "here now" not in joined


def test_chat_presence_absent_for_a_narrowed_session(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    # A session WITHOUT the location scope → no presence line at all.
    sessions_store.add(AgentSessionInfo("sess-gen", "", "active", ("general",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    _wire_presence(client, near=_fresh_near(), place=LatestPlace("e", "Home", NOW), subs=["s1"])

    client.post("/api/chat", json={"session_id": "sess-gen", "message": "hi"})
    joined = "\n".join(getattr(m, "text", "") for m in fake.stream_calls[0]["messages"])
    assert _PRESENCE_FRAME not in joined


def test_chat_presence_absent_when_no_fix(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-nofix", "", "active", ("location",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    # No usable fix → nothing to report, no line injected.
    _wire_presence(client, near=None, place=None, subs=["s1"])

    client.post("/api/chat", json={"session_id": "sess-nofix", "message": "hi"})
    joined = "\n".join(getattr(m, "text", "") for m in fake.stream_calls[0]["messages"])
    assert _PRESENCE_FRAME not in joined


def test_chat_presence_carries_no_coordinate(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-coord", "", "active", ("location",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    near = NearestFix(
        fix=FixPoint(datetime.now(UTC) - timedelta(minutes=2), 40.123456, -74.654321, 10, 80),
        gap_seconds=120,
    )
    _wire_presence(client, near=near, place=LatestPlace("e", "Home", NOW), subs=["s1"])

    client.post("/api/chat", json={"session_id": "sess-coord", "message": "hi"})
    joined = "\n".join(getattr(m, "text", "") for m in fake.stream_calls[0]["messages"])
    assert "40.123456" not in joined and "-74.654321" not in joined


# --- the ambient date/time block + jerv's owner-opt-in presence --------------


def test_chat_prepends_the_current_datetime_as_data_framed_user_message(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-1", "", "active", ("general",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]

    resp = client.post("/api/chat", json={"session_id": "sess-1", "message": "what day is it?"})
    assert resp.status_code == 200
    msgs = fake.stream_calls[0]["messages"]
    framed = [m for m in msgs if _CLOCK_FRAME in getattr(m, "text", "")]
    # Exactly one, a UserMessage (conversation channel, not the system prompt),
    # prepended before the owner's actual message.
    assert len(framed) == 1
    assert type(framed[0]).__name__ == "UserMessage"
    texts = [getattr(m, "text", "") for m in msgs]
    assert texts.index(framed[0].text) < texts.index("what day is it?")


def test_chat_datetime_block_honours_owner_timezone(
    client: TestClient, repo: FakeAuthRepo, sessions_store: FakeAgentSessions
) -> None:
    login(client, repo)
    sessions_store.add(AgentSessionInfo("sess-tz", "", "active", ("general",), (), NOW, NOW))
    router, fake = _capturing_router()
    client.app.state.llm_router = router  # type: ignore[attr-defined]
    client.app.state.settings_store.values["owner_timezone"] = "Asia/Tokyo"  # type: ignore[attr-defined]

    client.post("/api/chat", json={"session_id": "sess-tz", "message": "hi"})
    joined = "\n".join(getattr(m, "text", "") for m in fake.stream_calls[0]["messages"])
    assert "(Asia/Tokyo)" in joined


# --- GET /api/agent/favicon (web citation chip logos) ----------------------

_FAV_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
_FAV_HOME = b'<html><head><link rel="icon" href="/icon.png"></head><body>x</body></html>'


def _favicon_transport(found: bool) -> "object":
    import httpx

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, content=_FAV_HOME, headers={"content-type": "text/html"})
        if found and request.url.path == "/icon.png":
            return httpx.Response(200, content=_FAV_PNG)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_favicon_requires_owner(client: TestClient) -> None:
    assert client.get("/api/agent/favicon?host=example.com").status_code == 401


def test_favicon_serves_on_box_fetched_image(client: TestClient, repo: FakeAuthRepo) -> None:
    from jbrain.web import FaviconFetcher

    login(client, repo)
    client.app.state.favicon_fetcher = FaviconFetcher(  # type: ignore[attr-defined]
        transport=_favicon_transport(found=True)  # type: ignore[arg-type]
    )
    resp = client.get("/api/agent/favicon?host=https://example.com/games")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == _FAV_PNG
    # Cached at the browser, and not re-fetched server-side (the on-box TTL cache).
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_favicon_missing_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    from jbrain.web import FaviconFetcher

    login(client, repo)
    client.app.state.favicon_fetcher = FaviconFetcher(  # type: ignore[attr-defined]
        transport=_favicon_transport(found=False)  # type: ignore[arg-type]
    )
    assert client.get("/api/agent/favicon?host=nofavicon.example").status_code == 404
