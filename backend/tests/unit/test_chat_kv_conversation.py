"""The conversation cache (FLASH_NEXT F4c) through REAL `/api/chat` turns: the chat's message
list is not stable between turns (volatile blocks, the turn's own tool steps), so a restore must
be attempted on identity alone — and only for a chat that can never hold firewalled data."""

from __future__ import annotations

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


def _displace(client: TestClient, app: Any, gw: Any, a_tokens: int = 40_299) -> None:
    """Two more chats after sess-A, so A's slot is reused. Flash-Next's chat has two slots
    (the chat pair): the first other chat lands in the second slot and A stays live in slot 0;
    only the next one takes slot 0, and A's conversation is saved off it first."""
    _jerv(app, "sess-B")
    _jerv(app, "sess-C")
    gw.slot_state = _slots(s0=a_tokens)
    _chat(client, "sess-B", "two", [])  # slot 9
    gw.slot_state = _slots(s0=a_tokens, s9=31_299)
    _chat(client, "sess-C", "three", [])  # slot 0: A is saved as C takes it


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
            _final("hello C", 32_000),
            _final("still noon", 41_000, cached=40_100),
        ],
    )
    _chat(client, "sess-A", "what time is it?", [])
    # B lands in the chat pair's other slot (A stays live); C takes A's slot: A is saved first.
    _displace(client, app, gw)
    names = [n for _, n in gw.saved]
    assert len(names) == 1 and kv_conversation.is_conversation_file(names[0])
    # Slot 0 now holds C (the latest chat), slot 9 B: A comes back into slot 9, saving B.
    gw.slot_state = _slots(s0=32_299, s9=31_299)
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
    assert gw.restored and gw.restored[-1] == (9, names[0])
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
            _final("hello C", 32_000),
            _final("hello A", 41_000),
        ],
    )
    _chat(client, "sess-A", "where am I?", [])
    _displace(client, app, gw)
    assert store._counters.get("conversation_tainted") == 1
    assert gw.saved == [], "a conversation in which a location tool ran is never saved"
    _chat(client, "sess-A", "and now?", [{"role": "user", "content": "where am I?"}])
    assert gw.restored == []
    # A's return displaced B (saved, as any clean chat is); nothing on disk is A's.
    claims = [
        kv_conversation.ConversationMeta.from_json(m.read_text())
        for m in folder.glob("c-*.kvslot.meta")
    ]
    assert all(c is not None and c.key != kv_conversation.key_hash("sess-A") for c in claims)


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
    _displace(client, app, gw)
    assert gw.saved == []


def test_rescoping_a_session_forgets_its_files_and_keeps_it_off_disk(
    box: tuple[TestClient, Any],
) -> None:
    client, (app, store, gw, folder) = box
    _jerv(app, "sess-A")
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    _router(app, store, [_final("a", 40_000), _final("b", 31_000)])
    _chat(client, "sess-A", "one", [])
    _displace(client, app, gw)
    assert len(list(folder.glob("c-*.kvslot"))) == 1
    resp = client.post("/api/sessions/sess-A/scope", json={"domain_scopes": ["health"]})
    assert resp.status_code == 204
    assert not list(folder.glob("c-*.kvslot")), "a scope change drops what was judged under the old"


def test_the_delete_route_itself_deletes_the_sessions_files(box: tuple[TestClient, Any]) -> None:
    client, (app, store, gw, folder) = box
    _jerv(app, "sess-A")
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    _router(app, store, [_final("a", 40_000), _final("b", 31_000)])
    _chat(client, "sess-A", "one", [])
    _displace(client, app, gw)
    files = list(folder.glob("c-*.kvslot"))
    assert len(files) == 1
    assert client.delete("/api/sessions/sess-A").status_code == 204
    assert not files[0].exists()
    assert not list(folder.glob("c-*.kvslot.meta"))


def test_a_session_once_scoped_to_a_firewalled_domain_never_reaches_disk(
    box: tuple[TestClient, Any],
) -> None:
    client, (app, store, gw, folder) = box
    _jerv(app, "sess-A", scopes=("health",))
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    # Narrowed to `general` after it could read health: its history may carry that.
    resp = client.post("/api/sessions/sess-A/scope", json={"domain_scopes": ["general"]})
    assert resp.status_code == 204
    _router(app, store, [_final("a", 40_000), _final("b", 31_000)])
    _chat(client, "sess-A", "one", [])
    _displace(client, app, gw)
    assert gw.saved == []
    assert "sess-A" in app.state.settings_store.values["llm_kv_conversation_excluded_sessions"]


def test_the_routes_forget_under_the_canonical_id_whatever_case_they_were_called_with(
    box: tuple[TestClient, Any],
) -> None:
    client, (app, store, gw, folder) = box
    sid = "0f6b3c2a-9d1e-4f5a-8b7c-1234567890ab"
    _jerv(app, sid)
    _jerv(app, "sess-B")
    app.state.agent_registry = registry_with_tool("current_time", _now_time)
    _router(app, store, [_final("a", 40_000), _final("b", 31_000)])
    _chat(client, sid, "one", [])
    _displace(client, app, gw)
    files = list(folder.glob("c-*.kvslot"))
    assert len(files) == 1
    # The chat keyed it on str(session.id); the route is called with the id upper-cased.
    assert client.delete(f"/api/sessions/{sid.upper()}").status_code == 204
    assert not files[0].exists()
