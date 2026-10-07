"""jerv's history rebuilt from the transcript (docs/plans/TOOL_RESULT_REPLAY_PLAN.md R1):
rounds in order, stubs before the boundary, the stepped boundary, byte-identical renders."""

import dataclasses

from jbrain.agent import history_replay as hr
from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.transcript_store import TurnRecord
from jbrain.llm import AssistantMessage, ToolResultMessage, UserMessage


def _step(i: str, name: str = "grab_frame", *, offset: int = 0, summary: str = "", ok=True):  # type: ignore[no-untyped-def]
    return {
        "id": i,
        "name": name,
        "args": {"seek": i},
        "ok": ok,
        "summary": summary or f"result {i}",
        "text_offset": offset,
    }


def _turns(*assistant_tools: list[dict], content: str = "answer") -> list[TurnRecord]:
    out: list[TurnRecord] = []
    seq = 1
    for tools in assistant_tools:
        out.append(TurnRecord(role="user", content=f"q{seq}", seq=seq))
        out.append(TurnRecord(role="assistant", content=content, tools=tools, seq=seq + 1))
        seq += 2
    return out


def test_rounds_replay_in_the_order_they_ran() -> None:
    content = "Let me look. Now closer. Here it is."
    tools = [
        _step("a", offset=12),
        _step("b", offset=12),
        _step("c", offset=24),
    ]
    (user, assistant) = hr.build(_turns(tools, content=content), floor=0)
    assert user.messages == (UserMessage(text="q1"),)
    m = assistant.messages
    assert isinstance(m[0], AssistantMessage) and m[0].text == "Let me look."
    assert [c.id for c in m[0].tool_calls] == ["a", "b"]
    assert m[0].tool_calls[0].arguments == {"seek": "a"}
    assert isinstance(m[1], ToolResultMessage)
    assert [r.content for r in m[1].results] == ["result a", "result b"]
    assert isinstance(m[2], AssistantMessage) and m[2].text == " Now closer."
    assert [r.content for r in m[3].results] == ["result c"]  # type: ignore[union-attr]
    assert m[4] == AssistantMessage(text=" Here it is.")
    assert assistant.text == content


def test_a_turn_without_tools_is_its_prose() -> None:
    (_, assistant) = hr.build(_turns([]), floor=0)
    assert assistant.messages == (AssistantMessage(text="answer"),)


def test_a_failed_call_replays_as_an_error() -> None:
    (_, assistant) = hr.build(_turns([_step("a", ok=False, summary="boom")]), floor=0)
    result = assistant.messages[1].results[0]  # type: ignore[union-attr]
    assert result.is_error and result.content == "boom"


def test_turns_before_the_floor_keep_their_calls_but_replay_stubs() -> None:
    turns = _turns([_step("old")], [_step("new")])
    entries = hr.build(turns, floor=4)
    old, new = entries[1].messages, entries[3].messages
    assert old[0].tool_calls[0].id == "old"  # type: ignore[union-attr]
    assert old[1].results[0].content == hr.STUB  # type: ignore[union-attr]
    assert new[1].results[0].content == "result new"  # type: ignore[union-attr]


def test_one_huge_result_is_cut() -> None:
    (_, assistant) = hr.build(_turns([_step("a", summary="x" * 40_000)]), floor=0)
    text = assistant.messages[1].results[0].content  # type: ignore[union-attr]
    assert text.startswith("x" * hr.MAX_RESULT_CHARS)
    assert "cut at 16,000 characters" in text and len(text) < hr.MAX_RESULT_CHARS + 100


def test_the_floor_stays_put_under_the_budget() -> None:
    turns = _turns([_step("a", summary="x" * 1000)], [_step("b", summary="x" * 1000)])
    assert hr.advance_floor(turns, 0) == 0


def test_over_the_budget_the_floor_moves_by_whole_turns_to_the_low_water_mark() -> None:
    # Twenty turns of 16k chars = 320k chars, over the 256k budget; the move trims to <= 192k.
    turns = _turns(*[[_step(str(i), summary="x" * 16_000)] for i in range(20)])
    floor = hr.advance_floor(turns, 0)
    kept = [t for t in turns if t.role == "assistant" and t.seq >= floor]
    assert len(kept) * 16_000 <= hr.REPLAY_LOW_WATER_TOKENS * hr.CHARS_PER_TOKEN
    assert len(kept) == 12 and floor == kept[0].seq - 1  # the next turn's user message
    # Stepped: one more turn does not move it again — the boundary holds for ~16k tokens.
    more = [
        *turns,
        TurnRecord(role="user", content="q", seq=41),
        TurnRecord(role="assistant", content="a", tools=[_step("x", summary="x" * 16_000)], seq=42),
    ]
    assert hr.advance_floor(more, floor) == floor


def test_the_floor_never_moves_backward_or_past_the_newest_turn() -> None:
    turns = _turns([_step("a", summary="x" * 16_000)] * 1)
    assert hr.advance_floor(turns, 99) == 99
    # A single turn over the whole budget is still the newest: it is never stubbed.
    huge = _turns([_step(str(i), summary="x" * 16_000) for i in range(30)])
    assert hr.advance_floor(huge, 0) == 0


def test_never_replayed_tools_are_always_stubbed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(hr, "NEVER_REPLAYED", frozenset({"secret_tool"}))
    (_, assistant) = hr.build(_turns([_step("a", name="secret_tool")]), floor=0)
    assert assistant.messages[1].results[0].content == hr.STUB  # type: ignore[union-attr]


def test_a_user_turn_with_media_is_spelled_as_the_client_decorates_it() -> None:
    video = AttachmentInfo("v1", "clip.mp4", "video/mp4", 1, "s", "general")
    doc = AttachmentInfo("d1", "a.pdf", "application/pdf", 1, "s", "general")
    turn = TurnRecord(role="user", content="what?", attachments=[video, doc], seq=1)
    (entry,) = hr.build([turn], floor=0)
    assert entry.text == (
        "what?\n\n[Images the owner attached this turn — source_attachment_id=v1 (clip.mp4)]"
    )


def test_the_render_is_identical_turn_over_turn() -> None:
    turns = _turns([_step("a"), _step("b", offset=3)], [_step("c")], content="abc def")
    assert hr.build(turns, 0) == hr.build(list(turns), 0)


# ---- the exact replay: a turn as the model was sent it (docs/reference/PROMPT_CACHE.md)


def _round(text: str, reasoning: str, *calls: tuple[str, str]) -> dict:
    return {
        "text": text,
        "reasoning": reasoning,
        "model": "flash",
        "calls": [{"id": i, "name": "grab_frame", "arguments": args} for i, args in calls],
    }


def _wired(*, final: bool = True, own: dict | None = None) -> list[TurnRecord]:
    wire: dict = {
        "v": 1,
        "rounds": [
            _round("", "first", ("a", '{"seek": 9, "at": 1}')),
            _round("", "second", ("b", "{}")),
            _round("Closer. ", "third", ("c", '{"seek": 3}')),
        ],
        "suffixes": {"c": "\n[=1]"},
        "input": own or {"head": [], "tail": ["[now: 21:04]", "what numbers?"]},
    }
    if final:
        wire["final"] = {"text": "It is 4.", "reasoning": "settled", "model": "flash"}
    steps = [_step("a"), _step("b"), _step("c", offset=8)]
    return [
        TurnRecord(role="user", content="what numbers?", seq=1),
        TurnRecord(role="assistant", content="Closer. It is 4.", tools=steps, seq=2, wire=wire),
    ]


def test_an_exact_replay_is_the_turn_as_sent() -> None:
    user, assistant = hr.build(_wired(), 0, exact=True)
    # The turn's own blocks and message, after the anchor point (`tail`), not the bare text.
    assert user.messages == () and user.text == "what numbers?"
    assert user.tail == (UserMessage(text="[now: 21:04]"), UserMessage(text="what numbers?"))
    m = assistant.messages
    # Each round on its own, with its own thinking — even two with no prose between them.
    assert [x.reasoning for x in m if isinstance(x, AssistantMessage)] == [
        "first",
        "second",
        "third",
        "settled",
    ]
    assert all(x.reasoning_model == "flash" for x in m if isinstance(x, AssistantMessage))
    # The arguments in the order the model wrote them, and the model-only suffix.
    assert list(m[0].tool_calls[0].arguments) == ["seek", "at"]  # type: ignore[union-attr]
    assert m[5].results[0].content == "result c\n[=1]"  # type: ignore[union-attr]
    assert m[-1] == AssistantMessage(text="It is 4.", reasoning="settled", reasoning_model="flash")


def test_an_anchored_turn_keeps_its_question_before_the_anchor() -> None:
    own = {"head": ["what is this?\n\n[Images…]"], "tail": ["[now]", "(Answer the owner…)"]}
    user, _ = hr.build(_wired(own=own), 0, exact=True)
    assert user.messages == (UserMessage(text="what is this?\n\n[Images…]"),)
    assert [t.text for t in user.tail] == ["[now]", "(Answer the owner…)"]  # type: ignore[union-attr]


def test_without_a_final_round_the_rest_of_the_prose_closes_the_turn() -> None:
    (_, assistant) = hr.build(_wired(final=False), 0, exact=True)
    assert assistant.messages[-1] == AssistantMessage(text="It is 4.")


def test_a_deferred_turn_with_no_user_turn_carries_its_own_input() -> None:
    (_, assistant) = hr.build(_wired(), 0, exact=True)
    (alone,) = hr.build([_wired()[1]], 0, exact=True)
    assert alone.messages[:2] == (
        UserMessage(text="[now: 21:04]"),
        UserMessage(text="what numbers?"),
    )
    assert alone.messages[2:] == assistant.messages


def test_before_the_floor_an_exact_turn_is_compacted_like_any_other() -> None:
    turns = _wired()
    user, assistant = hr.build(turns, floor=3, exact=True)
    assert user.messages == (UserMessage(text="what numbers?"),) and user.tail == ()
    assert all(not getattr(x, "reasoning", "") for x in assistant.messages)
    assert assistant.messages[1].results[0].content == hr.STUB  # type: ignore[union-attr]


def test_a_cloud_route_keeps_the_prose_replay() -> None:
    turns = _wired()
    plain = [TurnRecord(role=t.role, content=t.content, tools=t.tools, seq=t.seq) for t in turns]
    assert hr.build(turns, 0) == hr.build(plain, 0)


def test_a_malformed_record_falls_back_to_the_prose() -> None:
    turns = _wired()
    plain = [TurnRecord(role=t.role, content=t.content, tools=t.tools, seq=t.seq) for t in turns]
    for broken in (
        {"v": 2},
        {"v": 1, "rounds": [_round("", "x", ("zz", "{}"))]},  # a call with no stored step
        {"v": 1, "rounds": [_round("", "x", ("a", "[1]"))]},  # arguments not an object
        {"v": 1, "rounds": [_round("", "x", ("a", "{"))]},  # not JSON at all
        {"v": 1, "rounds": [], "final": {"text": "x"}},
    ):
        turns[1] = dataclasses.replace(turns[1], wire=broken)
        assert hr.build(turns, 0, exact=True) == hr.build(plain, 0, exact=True)


def test_on_the_exact_path_the_budget_counts_thinking_and_the_turns_own_blocks() -> None:
    (_, assistant) = _wired()
    results = sum(len(s["summary"]) for s in assistant.tools) + len("\n[=1]")
    thinking = len("first" + "second" + "third" + "settled")
    blocks = len("[now: 21:04]" + "what numbers?")
    assert hr._turn_chars(assistant, exact=True) == results + thinking + blocks
    assert hr._turn_chars(assistant) == sum(len(s["summary"]) for s in assistant.tools)


def test_the_exact_path_moves_the_floor_over_the_combined_bulk() -> None:
    def turn(seq: int) -> list[TurnRecord]:
        wire = {"v": 1, "rounds": [{**_round("", "t" * 20_000, (f"c{seq}", "{}"))}]}
        step = {**_step(f"c{seq}"), "summary": "x" * 20_000}
        return [
            TurnRecord(role="user", content="q", seq=seq),
            TurnRecord(role="assistant", content="a", tools=[step], seq=seq + 1, wire=wire),
        ]

    turns = [t for i in range(8) for t in turn(2 * i + 1)]
    # 8 × 20k chars of results fit the 256k-char budget; with their thinking they do not.
    assert hr.advance_floor(turns, 0) == 0
    assert hr.advance_floor(turns, 0, exact=True) > 0


# ---- review follow-ups: what the exact path must not do


def _research_then(*follow_ups: TurnRecord) -> list[TurnRecord]:
    steps = [{**_step(f"r{i}"), "summary": "x" * 20_000} for i in range(16)]
    wire = {
        "v": 1,
        "rounds": [_round("", "think", *((f"r{i}", "{}") for i in range(16)))],
        "input": {"head": [], "tail": ["[now]", "research it"]},
    }
    return [
        TurnRecord(role="user", content="research it", seq=1),
        TurnRecord(role="assistant", content="found", tools=steps, seq=2, wire=wire),
        *follow_ups,
    ]


def test_a_trivial_follow_up_never_compacts_the_research_turn_before_it() -> None:
    # Every recorded turn has bulk (its `now` block, its thinking), but only results make a
    # turn the one a follow-up asks about: "thanks" must not stub the research it thanks.
    thanks = {
        "v": 1,
        "rounds": [],
        "final": {"text": "You're welcome.", "reasoning": "polite", "model": "flash"},
        "input": {"head": [], "tail": ["[now]", "thanks"]},
    }
    turns = _research_then(
        TurnRecord(role="user", content="thanks", seq=3),
        TurnRecord(role="assistant", content="You're welcome.", seq=4, wire=thanks),
    )
    assert hr.advance_floor(turns, 0) == 0
    assert hr.advance_floor(turns, 0, exact=True) == 0


def test_a_turn_that_ended_at_the_ceiling_replays_cut_not_whole() -> None:
    # Replayed whole it would overflow every follow-up and brick the chat.
    turns = _research_then()
    full = dataclasses.replace(turns[1], wire={**(turns[1].wire or {}), "full": True})
    (_, assistant) = hr.build([turns[0], full], 0, exact=True)
    result = assistant.messages[1].results[0].content  # type: ignore[union-attr]
    assert len(result) < 20_000 and "cut at 16,000 characters" in result
    assert all(not getattr(m, "reasoning", "") for m in assistant.messages)


def test_a_reclassified_round_still_leaves_the_answer_in_the_replay() -> None:
    # gpt-oss: a round's leaked analysis was moved into the thinking, so the prose does not
    # start with the recorded round text; the answer after the last call still replays.
    wire = {"v": 1, "rounds": [_round("Let me look.", "", ("a", "{}"))]}
    turns = [
        TurnRecord(role="user", content="q", seq=1),
        TurnRecord(role="assistant", content="It is 4.", tools=[_step("a")], seq=2, wire=wire),
    ]
    (_, assistant) = hr.build(turns, 0, exact=True)
    assert assistant.messages[-1] == AssistantMessage(text="It is 4.")
