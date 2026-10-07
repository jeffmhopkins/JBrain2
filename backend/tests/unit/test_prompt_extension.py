"""A follow-up turn's prompt is an exact extension of the previous turn's last prompt
(docs/reference/PROMPT_CACHE.md, "A follow-up is an exact extension").

Measured on the box 2026-10-07: a hybrid model reuses its cache only from a context checkpoint
at or before the first differing token, so a follow-up that re-rendered the previous turn any
differently re-read a 117k-token chat. These tests drive a real tool turn through the loop,
persist it the way /chat does (through a JSONB-shaped round trip), rebuild the next turn's
history from that record, and compare the two requests the adapter would send — the jinja
render happens server-side, so the messages array and `chat_template_kwargs` are the contract.
"""

from typing import Any

from jbrain.agent import history_replay as hr
from jbrain.agent.contracts import ToolSpec
from jbrain.agent.loop import AgentLoop, ToolContext
from jbrain.agent.toolfile import ToolFile
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.agent.transcript_accumulator import TranscriptAccumulator
from jbrain.agent.transcript_store import TurnRecord
from jbrain.db.session import SessionContext
from jbrain.llm import (
    FakeLlmClient,
    LlmMessage,
    LlmRouter,
    LlmTurn,
    LlmUsage,
    ToolCall,
    UserMessage,
)
from jbrain.llm.openai_compat import OpenAiCompatClient

FLASH = "qwen3.8-flash-next"
OWNER = SessionContext(principal_kind="owner")


async def _search(arguments: dict, ctx: ToolContext) -> str:
    return f"found: {arguments.get('q', '')}"


def _registry() -> ToolRegistry:
    spec = ToolSpec(name="search", version=1, params={"type": "object"}, permission="read")
    return ToolRegistry(
        [RegisteredTool(toolfile=ToolFile(spec=spec, description="search"), handler=_search)]
    )


def _script() -> list[LlmTurn]:
    """Turn 1: three tool rounds, each with its own thinking — two in a row with no prose
    between them, arguments whose order a JSONB object would not keep — then the answer.
    Turn 2: one answer."""
    return [
        LlmTurn(
            "",
            (ToolCall("c1", "search", {"q": "x", "a": 1}),),
            "tool_use",
            LlmUsage(100, 5),
            reasoning="Search x first.",
        ),
        LlmTurn(
            "",
            (ToolCall("c2", "search", {"q": "y"}), ToolCall("c3", "search", {"q": "z"})),
            "tool_use",
            LlmUsage(120, 5),
            reasoning="Now y and z together.",
        ),
        LlmTurn(
            "Checking one more.",
            (ToolCall("c4", "search", {"zeta": 2, "q": "w"}),),
            "tool_use",
            LlmUsage(150, 5),
            reasoning="One more.",
        ),
        LlmTurn("The answer is 4.", (), "end_turn", LlmUsage(170, 6), reasoning="Enough."),
        LlmTurn("Because of w.", (), "end_turn", LlmUsage(200, 4), reasoning="Easy."),
    ]


def _jsonb(value: Any) -> Any:
    """What a JSONB column hands back: object keys reordered (shortest first, then bytewise)."""
    if isinstance(value, dict):
        return {k: _jsonb(value[k]) for k in sorted(value, key=lambda k: (len(k), k))}
    if isinstance(value, list):
        return [_jsonb(v) for v in value]
    return value


async def _turn(loop: AgentLoop, conversation: list[LlmMessage]) -> TranscriptAccumulator:
    acc = TranscriptAccumulator()
    async for event in loop.run_stream(
        session=OWNER,
        scopes=("general",),
        conversation=conversation,
        on_round=acc.record_round,
    ):
        acc.feed(event)
    return acc


def _payload(call: dict[str, Any]) -> dict[str, Any]:
    client = OpenAiCompatClient("http://gateway/v1", "", provider="local")
    return client._converse_payload(
        model=call["model"],
        system=call["system"],
        messages=call["messages"],
        tools=call["tools"],
        max_tokens=call["max_tokens"],
        reasoning_effort=call["reasoning_effort"],
        replay_reasoning=call["replay_reasoning"],
    )


def _flatten(entries: list[hr.Entry]) -> list[LlmMessage]:
    return [m for e in entries for m in (*e.messages, *e.tail)]


async def _two_turns(*, exact: bool) -> tuple[dict[str, Any], dict[str, Any], FakeLlmClient]:
    fake = FakeLlmClient(turns=_script())
    router = LlmRouter(
        {"local": fake}, {"agent.turn": ("local", FLASH)}, pinned=frozenset({"agent.turn"})
    )
    loop = AgentLoop(router, _registry())
    own = ["[now: Tue 6 Oct, 21:04]", "What is it?"]
    acc = await _turn(loop, [UserMessage(text=t) for t in own])
    stored = [
        TurnRecord(role="user", content="What is it?", seq=1),
        TurnRecord(
            role="assistant",
            content=acc.answer_text,
            tools=_jsonb(acc.tool_steps()),
            reasoning=acc.reasoning_text,
            seq=2,
            wire=_jsonb(acc.wire({"head": [], "tail": own})),
        ),
    ]
    history = _flatten(hr.build(stored, hr.advance_floor(stored, 0, exact=exact), exact=exact))
    follow_up = [UserMessage(text="[now: Tue 6 Oct, 21:09]"), UserMessage(text="Why?")]
    await _turn(loop, [*history, *follow_up])
    assert len(fake.stream_calls) == 5
    return _payload(fake.stream_calls[3]), _payload(fake.stream_calls[4]), fake


async def test_the_follow_ups_prompt_extends_the_last_prompt_byte_for_byte() -> None:
    last, follow_up, fake = await _two_turns(exact=True)
    # The router replayed every step's thinking on both calls, and the template is told to
    # render all of it — so the earlier turn's steps look the same now as they did then.
    assert fake.stream_calls[3]["replay_reasoning"] and fake.stream_calls[4]["replay_reasoning"]
    assert last["chat_template_kwargs"]["preserve_thinking"] is True
    assert follow_up["chat_template_kwargs"] == last["chat_template_kwargs"]
    assert follow_up["tools"] == last["tools"]
    n = len(last["messages"])
    assert follow_up["messages"][:n] == last["messages"]
    # Then the previous answer, with its own thinking, and the new turn's messages.
    assert follow_up["messages"][n] == {
        "role": "assistant",
        "content": "The answer is 4.",
        "reasoning_content": "Enough.",
    }
    assert [m["content"] for m in follow_up["messages"][n + 1 :]] == [
        "[now: Tue 6 Oct, 21:09]",
        "Why?",
    ]


async def test_the_last_prompt_carried_every_steps_thinking_and_its_own_arguments() -> None:
    last, _follow_up, _fake = await _two_turns(exact=True)
    steps = [m for m in last["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in steps] == [
        "Search x first.",
        "Now y and z together.",
        "One more.",
    ]
    # Two rounds at the same prose position stay two rounds, and the arguments keep the order
    # the model wrote them in — the stored record survived a JSONB round trip.
    assert [len(m["tool_calls"]) for m in steps] == [1, 2, 1]
    assert steps[0]["tool_calls"][0]["function"]["arguments"] == '{"q": "x", "a": 1}'
    assert steps[2]["tool_calls"][0]["function"]["arguments"] == '{"zeta": 2, "q": "w"}'


async def test_the_prose_replay_is_what_used_to_break_the_cache() -> None:
    # The pre-fix render: no thinking on earlier steps, rounds merged by prose offset, keys
    # reordered, the turn's own blocks gone. It diverges inside the previous turn.
    last, follow_up, _fake = await _two_turns(exact=False)
    n = len(last["messages"])
    assert follow_up["messages"][:n] != last["messages"]
