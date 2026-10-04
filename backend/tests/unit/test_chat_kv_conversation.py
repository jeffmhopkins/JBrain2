"""The conversation cache (FLASH_NEXT F4c) through REAL `/api/chat` turns: the chat's message
list is not stable between turns (volatile blocks, the turn's own tool steps), so a restore must
be attempted on identity alone — and only for a chat that can never hold firewalled data."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.loop import ToolOutput
from jbrain.agent.session import AgentSessionInfo
from jbrain.config import Settings
from jbrain.llm import (
    FakeLlmClient,
    LlmRouter,
    LlmTurn,
    LlmUsage,
    ToolCall,
    kv_conversation,
    kv_prefix,
    llama_swap_config,
)
from jbrain.llm import engine as engines
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore
from tests.unit.test_agent_api import (
    NOW,
    FakeAgentSessions,
    FakeChatAttachments,
    FakeChatBlobs,
    FakeRunLog,
    FakeTranscript,
    login,
    registry_with_tool,
)
from tests.unit.test_kv_prefix_roles import BUILD, FLASH, LINE, FakeGateway, _slots


class _Transcript(FakeTranscript):
    """The fake plus the tool history the privacy check reads."""

    async def tool_names(self, ctx: Any, session_id: str) -> set[str]:
        return {
            t["name"]
            for r in self.recorded
            if r["session_id"] == session_id
            for t in r["tools"]
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        }


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    monkeypatch.setattr(kv_prefix, "_free_bytes", lambda _folder: 10**13)
    (tmp_path / engines.CONFIG_FILE[engines.FLASH_NEXT]).write_text(
        f"models:\n  {FLASH}:\n    cmd: {LINE}\n"
    )
    folder = tmp_path / llama_swap_config.KVSLOT_DIR / FLASH
    folder.mkdir(parents=True)
    kv_prefix.write_gate_verdict(
        str(folder), {"fingerprint": kv_prefix.gate_fingerprint(LINE, BUILD), "verdict": "passed"}
    )
    gw = FakeGateway(folder)
    store = KvPrefixStore(
        gw,  # type: ignore[arg-type]
        str(tmp_path),
        engine=engines.FLASH_NEXT,
        conversations=True,
    )
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    with TestClient(app) as client:
        repo = FakeAuthRepo()
        app.state.auth_repo = repo
        app.state.agent_sessions = FakeAgentSessions()
        app.state.agent_runlog = FakeRunLog()
        app.state.agent_transcript = _Transcript()
        app.state.turn_attachments = FakeChatAttachments()
        app.state.blob_store = FakeChatBlobs()
        app.state.settings_store = FakeSettingsStore()
        app.state.kv_prefix = store
        login(client, repo)
        yield client, (app, store, gw, folder)


def _jerv(app: Any, session_id: str, scopes: tuple[str, ...] = ("general",)) -> None:
    app.state.agent_sessions.add(
        AgentSessionInfo(session_id, "", "active", scopes, (), NOW, NOW, agent="jerv")
    )


def _router(app: Any, store: KvPrefixStore, turns: list[LlmTurn]) -> FakeLlmClient:
    fake = FakeLlmClient(turns=turns, stream_chunks=[[t.text] if t.text else [] for t in turns])
    # Pinned, so the agent's declared strength cannot re-route the turn to a cloud tier; any
    # other task (a title) lands on the cloud fake.
    app.state.llm_router = LlmRouter(
        {"local": fake, "xai": FakeLlmClient()},
        {"agent.turn": ("local", FLASH)},
        pinned=frozenset({"agent.turn"}),
        kv_prefix=store,
    )
    return fake


def _final(text: str, n_in: int, *, cached: int = 0) -> LlmTurn:
    return LlmTurn(text, (), "end_turn", LlmUsage(n_in, 300, cached_tokens=cached))


def _tool_turn(name: str, n_in: int) -> LlmTurn:
    return LlmTurn("", (ToolCall("c1", name, {}),), "tool_use", LlmUsage(n_in, 50))


def _chat(client: TestClient, session_id: str, message: str, history: list[dict]) -> None:
    resp = client.post(
        "/api/chat", json={"session_id": session_id, "message": message, "history": history}
    )
    assert resp.status_code == 200, resp.text


async def _now_time(arguments: Any, ctx: Any) -> ToolOutput:
    return ToolOutput("12:00")


def test_two_real_chat_turns_restore_the_conversation_on_the_second(
    box: tuple[TestClient, Any],
) -> None:
    client, (app, store, gw, folder) = box
    _jerv(app, "sess-A")
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    _router(
        app,
        store,
        [
            _tool_turn("current_time", 39_000),  # turn 1 of A runs a tool, then answers
            _final("it is noon", 40_000),
            _final("hello B", 31_000),
            _final("still noon", 41_000, cached=40_100),
        ],
    )
    _chat(client, "sess-A", "what time is it?", [])
    gw.slot_state = _slots(s0=40_299)  # slot 0 holds A's prompt + answer
    _chat(client, "sess-B", "hi", [])  # B takes the slot: A is saved first
    names = [n for _, n in gw.saved]
    assert len(names) == 1 and kv_conversation.is_conversation_file(names[0])
    gw.slot_state = _slots(s0=31_099)
    gw.restore_n = 40_299
    # Turn 2 of A: a different message list than turn 1 sent (history, no tool steps, fresh
    # volatile blocks) — restored anyway, on identity.
    _chat(
        client,
        "sess-A",
        "and now?",
        [
            {"role": "user", "content": "what time is it?"},
            {"role": "assistant", "content": "it is noon"},
        ],
    )
    assert gw.restored and gw.restored[-1] == (0, names[0])
    assert store._counters.get("conversation_restore_hit") == 1


def test_a_chat_that_ran_a_location_tool_never_reaches_disk(
    box: tuple[TestClient, Any],
) -> None:
    client, (app, store, gw, folder) = box
    _jerv(app, "sess-A")
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_location", _now_time)
    _router(
        app,
        store,
        [
            _tool_turn("current_location", 39_000),
            _final("you are home", 40_000),
            _final("hello B", 31_000),
            _final("hello A", 41_000),
        ],
    )
    _chat(client, "sess-A", "where am I?", [])
    gw.slot_state = _slots(s0=40_299)
    _chat(client, "sess-B", "hi", [])
    assert store._counters.get("conversation_tainted") == 1
    assert gw.saved == [], "a conversation in which a location tool ran is never saved"
    _chat(client, "sess-A", "and now?", [{"role": "user", "content": "where am I?"}])
    assert gw.restored == []
    assert not list(folder.glob("c-*.kvslot"))


def test_a_brain_chat_never_gets_a_conversation_file(box: tuple[TestClient, Any]) -> None:
    client, (app, store, gw, folder) = box
    app.state.agent_sessions.add(
        AgentSessionInfo("sess-A", "", "active", ("general",), (), NOW, NOW, agent="curator")
    )
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    fake = _router(app, store, [_final("brain answer", 40_000), _final("hello B", 31_000)])
    _chat(client, "sess-A", "what do my notes say?", [])
    assert len(fake.stream_calls) == 1, "the turn ran"
    gw.slot_state = _slots(s0=40_299)
    _chat(client, "sess-B", "hi", [])
    assert gw.saved == []


def test_deleting_or_rescoping_a_session_forgets_its_files(
    box: tuple[TestClient, Any],
) -> None:
    client, (app, store, gw, folder) = box
    _jerv(app, "sess-A")
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    _router(
        app,
        store,
        [_final("a", 40_000), _final("b", 31_000), _final("a2", 41_000)],
    )
    _chat(client, "sess-A", "one", [])
    gw.slot_state = _slots(s0=40_299)
    _chat(client, "sess-B", "two", [])
    assert len(list(folder.glob("c-*.kvslot"))) == 1
    assert (
        client.post("/api/sessions/sess-A/scope", json={"domain_scopes": ["health"]}).status_code
        == 204
    )
    assert not list(folder.glob("c-*.kvslot")), "a scope change drops what was judged under the old"
    gw.slot_state = _slots(s0=31_099)
    _chat(client, "sess-B", "three", [])  # nothing to save: B is still the holder
    gw.slot_state = _slots(s0=31_400)
    store._conv_hold.clear()
    asyncio.run(store.forget_conversation("sess-B"))
    assert client.delete("/api/sessions/sess-B").status_code == 204
