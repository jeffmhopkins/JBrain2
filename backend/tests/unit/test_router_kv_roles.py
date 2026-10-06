"""The router's half of the per-role disk prefix cache (FLASH_NEXT_ENGINE_PLAN F4): which role
a restore is for, the conversation hooks around an interactive turn on a pooled model, and the
loop naming the chat conversation it belongs to. The store is a recording fake."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from jbrain.agent.loop import AgentLoop
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, UserMessage, kv_prefix
from jbrain.llm.slot_roles import SlotRole
from jbrain.llm.types import LlmTool

FLASH = "qwen3.8-flash-next"
STANDARD = "gpt-oss-120b"


class RecordingStore:
    def __init__(
        self, *, restores_conversation: bool = False, picks: SlotRole | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._restores_conversation = restores_conversation
        # What `pick_chat_role` answers: the chat pair member a chat turn is routed to.
        self._picks = picks or SlotRole.INTERACTIVE_ALT
        self.released: list[SlotRole] = []
        # The last check before an exact-slot prime is sent.
        self.prime_free = True

    def prime_target_free(self, model: str, role: SlotRole | None) -> bool:
        self.calls.append(("prime_check", {"model": model, "role": role}))
        return self.prime_free

    def pick_chat_role(self, model: str, chat_key: str | None) -> SlotRole:
        self.calls.append(("pick", {"model": model, "chat_key": chat_key}))
        return self._picks

    def release_chat_role(self, model: str, role: SlotRole) -> None:
        self.released.append(role)

    async def prepare_conversation(
        self,
        model: str,
        key: str | None,
        system: str,
        tools: Sequence[LlmTool],
        effort: str | None,
        *,
        role: SlotRole | None = None,
    ) -> tuple[bool, int]:
        self.calls.append(("prepare", {"model": model, "key": key, "role": role}))
        return self._restores_conversation, 7

    async def restore_if_lost(self, model: str, system: str, tools: Any, **kw: Any) -> bool:
        self.calls.append(("restore", {"model": model, **kw}))
        return False

    def identity_of(self, model: str, system: str, tools: Any, effort: Any) -> str:
        return "fp"

    def note_agent_turn(self, model: str, input_tokens: int, **kw: Any) -> None:
        self.calls.append(("turn", {"model": model, **kw}))

    def note_conversation_turn(self, model: str, key: str | None, **kw: Any) -> None:
        self.calls.append(("conversation_turn", {"model": model, "key": key, **kw}))

    def note_conversation_abandoned(
        self, model: str, chat_key: str | None = None, **kw: Any
    ) -> None:
        self.calls.append(("abandoned", {"model": model, "chat_key": chat_key, **kw}))

    def note_prefix_used(self, model: str, fingerprint: Any, **kw: Any) -> None:
        self.calls.append(("used", {"model": model, **kw}))


def _router(store: RecordingStore, model: str = FLASH) -> tuple[LlmRouter, FakeLlmClient]:
    turn = LlmTurn(text="ok", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(500, 40))
    fake = FakeLlmClient(turns=[turn, turn, turn])
    router = LlmRouter(
        {"local": fake},
        {"agent.turn": ("local", model)},
        kv_prefix=store,  # type: ignore[arg-type]
    )
    return router, fake


def _kinds(store: RecordingStore) -> list[str]:
    return [k for k, _ in store.calls]


async def test_a_background_turn_restores_into_its_own_role_and_touches_no_conversation() -> None:
    store = RecordingStore()
    router, _ = _router(store)
    await router.converse(
        "agent.turn", system="s", messages=[UserMessage("hi")], slot_role=SlotRole.SCHEDULED
    )
    assert _kinds(store) == ["restore", "turn"]
    assert store.calls[0][1]["role"] is SlotRole.SCHEDULED
    assert store.calls[1][1]["role"] is SlotRole.SCHEDULED


async def test_an_interactive_turn_prepares_its_conversation_then_records_it() -> None:
    store = RecordingStore()
    router, _ = _router(store)
    msgs = [UserMessage("hi")]
    await router.converse(
        "agent.turn",
        system="s",
        messages=msgs,
        slot_role=SlotRole.INTERACTIVE,
        conversation_key="chat-1",
    )
    assert _kinds(store) == ["pick", "prepare", "restore", "turn", "conversation_turn"]
    assert store.calls[1][1] == {
        "model": FLASH,
        "key": "chat-1",
        "role": SlotRole.INTERACTIVE_ALT,
    }
    recorded = store.calls[4][1]
    assert recorded["key"] == "chat-1"
    assert (recorded["input_tokens"], recorded["output_tokens"]) == (500, 40)
    assert recorded["fingerprint"] == "fp"
    # The prepare's sequence rides back so a superseded turn cannot claim the slot.
    assert recorded["seq"] == 7


async def test_a_restored_conversation_is_not_overwritten_by_the_persona() -> None:
    store = RecordingStore(restores_conversation=True)
    router, _ = _router(store)
    await router.converse(
        "agent.turn", system="s", messages=[UserMessage("hi")], conversation_key="chat-1"
    )
    assert "restore" not in _kinds(store)


async def test_a_model_without_a_pool_never_reaches_the_conversation_layer() -> None:
    store = RecordingStore()
    router, _ = _router(store, STANDARD)
    await router.converse(
        "agent.turn", system="s", messages=[UserMessage("hi")], conversation_key="chat-1"
    )
    assert "prepare" not in _kinds(store)
    # The store itself ignores a conversation on a model with no pool; the router still reports.
    assert store.calls[0] == (
        "restore",
        {
            "model": STANDARD,
            "reasoning_effort": None,
            "role": SlotRole.INTERACTIVE,
            "chat_key": None,
        },
    )


async def test_an_abandoned_interactive_stream_drops_the_conversation_claim() -> None:
    store = RecordingStore()
    router, _ = _router(store)
    stream = router.converse_stream(
        "agent.turn", system="s", messages=[UserMessage("hi")], conversation_key="chat-1"
    )
    async for _part in stream:
        break
    await stream.aclose()  # type: ignore[attr-defined]
    assert "abandoned" in _kinds(store)
    assert "conversation_turn" not in _kinds(store)


# ---- the chat pair: routing a chat turn to its slot --------------------------------------


async def test_a_chat_turn_is_routed_to_the_pair_slot_the_store_picks_end_to_end() -> None:
    store = RecordingStore(picks=SlotRole.INTERACTIVE_ALT)
    router, fake = _router(store)
    await router.converse(
        "agent.turn",
        system="s",
        messages=[UserMessage("hi")],
        slot_role=SlotRole.INTERACTIVE,
        conversation_key="chat-1",
        chat_key="session-1",
    )
    assert store.calls[0] == ("pick", {"model": FLASH, "chat_key": "session-1"})
    restore = dict(store.calls)["restore"]
    assert restore["role"] is SlotRole.INTERACTIVE_ALT and restore["chat_key"] == "session-1"
    turn = dict(store.calls)["turn"]
    assert turn["role"] is SlotRole.INTERACTIVE_ALT and turn["chat_key"] == "session-1"
    assert dict(store.calls)["conversation_turn"]["role"] is SlotRole.INTERACTIVE_ALT
    # The pick reaches the engine as the slot id of the member it chose.
    assert fake.converse_calls[0]["id_slot"] == 9
    # The claim covers the whole call and goes when it ends.
    assert store.released == [SlotRole.INTERACTIVE_ALT]


async def test_a_turn_naming_no_chat_is_still_routed_never_pinned_to_slot_0() -> None:
    store = RecordingStore(picks=SlotRole.INTERACTIVE_ALT)
    router, fake = _router(store)
    await router.converse("agent.turn", system="s", messages=[UserMessage("hi")])
    assert store.calls[0] == ("pick", {"model": FLASH, "chat_key": None})
    assert fake.converse_calls[0]["id_slot"] == 9


async def test_an_exact_pin_skips_the_pick() -> None:
    store = RecordingStore(picks=SlotRole.INTERACTIVE_ALT)
    router, fake = _router(store)
    await router.converse(
        "agent.turn",
        system="s",
        messages=[UserMessage("hi")],
        slot_role=SlotRole.INTERACTIVE,
        exact_slot=True,
    )
    assert "pick" not in _kinds(store)
    assert fake.converse_calls[0]["id_slot"] == 0
    # Checked after the prepare (whose save can stream for seconds), right before dispatch.
    assert _kinds(store).index("prime_check") > _kinds(store).index("restore")
    assert store.released == [], "an exact call took no chat claim, so it releases none"


async def test_an_exact_prime_whose_slot_a_chat_took_is_never_sent() -> None:
    store = RecordingStore()
    store.prime_free = False
    router, fake = _router(store)
    with pytest.raises(kv_prefix.ChatSlotTakenError):
        await router.converse(
            "agent.turn",
            system="s",
            messages=[UserMessage("warmup")],
            slot_role=SlotRole.INTERACTIVE_ALT,
            exact_slot=True,
        )
    assert fake.converse_calls == []
    with pytest.raises(kv_prefix.ChatSlotTakenError):
        async for _part in router.converse_stream(
            "agent.turn",
            system="s",
            messages=[UserMessage("warmup")],
            slot_role=SlotRole.INTERACTIVE_ALT,
            exact_slot=True,
        ):
            pass
    assert fake.stream_calls == []
    assert store.released == []


async def test_background_roles_and_other_tasks_are_never_routed() -> None:
    store = RecordingStore()
    router, _ = _router(store)
    await router.converse(
        "agent.turn", system="s", messages=[UserMessage("hi")], slot_role=SlotRole.SCHEDULED
    )
    await router.converse(
        "agent.turn", system="s", messages=[UserMessage("hi")], slot_role=SlotRole.BROWSE
    )
    assert "pick" not in _kinds(store)


async def test_a_model_without_a_pair_is_never_routed() -> None:
    store = RecordingStore()
    router, _ = _router(store, STANDARD)
    await router.converse("agent.turn", system="s", messages=[UserMessage("hi")], chat_key="x")
    assert "pick" not in _kinds(store)


async def test_a_routed_call_that_fails_releases_its_claim() -> None:
    store = RecordingStore(picks=SlotRole.INTERACTIVE_ALT)
    router, fake = _router(store)

    async def boom(**_kw: Any) -> LlmTurn:
        raise RuntimeError("engine fell over")

    fake.converse = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await router.converse(
            "agent.turn", system="s", messages=[UserMessage("hi")], chat_key="session-1"
        )
    assert store.released == [SlotRole.INTERACTIVE_ALT]


async def test_a_stream_that_never_starts_releases_its_claim_and_a_stopped_one_notes_it() -> None:
    store = RecordingStore(picks=SlotRole.INTERACTIVE_ALT)
    router, fake = _router(store)
    stream_ok = fake.converse_stream

    async def dead(**_kw: Any) -> Any:
        raise RuntimeError("engine fell over")
        yield  # an async generator that dies before its first part

    fake.converse_stream = dead  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        async for _part in router.converse_stream(
            "agent.turn", system="s", messages=[UserMessage("hi")], chat_key="session-1"
        ):
            pass
    assert store.released == [SlotRole.INTERACTIVE_ALT]
    fake.converse_stream = stream_ok  # type: ignore[method-assign]
    stream = router.converse_stream(
        "agent.turn", system="s", messages=[UserMessage("hi")], chat_key="session-2"
    )
    async for _part in stream:
        break
    await stream.aclose()  # type: ignore[attr-defined]
    abandoned = dict(store.calls)["abandoned"]
    assert abandoned["chat_key"] == "session-2" and abandoned["role"] is SlotRole.INTERACTIVE_ALT
    assert store.released == [SlotRole.INTERACTIVE_ALT] * 2, "a stopped stream releases too"


async def test_a_stream_routes_like_a_turn() -> None:
    store = RecordingStore(picks=SlotRole.INTERACTIVE_ALT)
    router, _ = _router(store)
    async for _part in router.converse_stream(
        "agent.turn", system="s", messages=[UserMessage("hi")], chat_key="session-1"
    ):
        pass
    turn = dict(store.calls)["turn"]
    assert turn["role"] is SlotRole.INTERACTIVE_ALT and turn["chat_key"] == "session-1"


async def test_the_chat_loop_names_its_conversation_on_every_call() -> None:
    turn = LlmTurn(text="hi", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))
    router = LlmRouter(
        {"xai": FakeLlmClient(turns=[turn, turn])}, {"agent.turn": ("xai", "grok-4.3")}
    )
    seen: list[object] = []
    stream = router.converse_stream

    def spy(*a: Any, **kw: Any) -> Any:
        seen.append((kw.get("conversation_key"), kw.get("chat_key")))
        return stream(*a, **kw)

    router.converse_stream = spy  # type: ignore[method-assign]
    loop = AgentLoop(
        router,
        ToolRegistry([]),
        slot_role=SlotRole.INTERACTIVE,
        conversation_key="chat-9",
        chat_key="session-9",
    )
    async for _ in loop.run_stream(
        session=SessionContext(principal_kind="owner"),
        scopes=("general",),
        conversation=[UserMessage(text="hello")],
    ):
        pass
    assert seen and all(k == ("chat-9", "session-9") for k in seen)


async def test_a_loop_without_a_conversation_keeps_the_old_call_shape() -> None:
    turn = LlmTurn(text="hi", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))
    router = LlmRouter({"xai": FakeLlmClient(turns=[turn])}, {"agent.turn": ("xai", "grok-4.3")})
    seen: list[bool] = []
    converse = router.converse

    async def spy(*a: Any, **kw: Any) -> LlmTurn:
        seen.append("conversation_key" in kw)
        return await converse(*a, **kw)

    router.converse = spy  # type: ignore[method-assign]
    loop = AgentLoop(router, ToolRegistry([]), slot_role=SlotRole.SCHEDULED)
    await loop.run(
        session=SessionContext(principal_kind="owner"),
        scopes=("general",),
        conversation=[UserMessage(text="hello")],
    )
    assert seen == [False]


def test_the_servers_cached_prompt_tokens_reach_the_turns_usage() -> None:
    from jbrain.llm.openai_compat import _cached_tokens

    assert _cached_tokens({"prompt_tokens": 9, "prompt_tokens_details": {"cached_tokens": 7}}) == 7
    assert _cached_tokens({"prompt_tokens": 9}) == 0
    assert _cached_tokens({"prompt_tokens_details": {"cached_tokens": "x"}}) == 0
    assert LlmUsage(1, 2).cached_tokens == 0
