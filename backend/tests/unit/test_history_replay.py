"""jerv's history rebuilt from the transcript (docs/plans/TOOL_RESULT_REPLAY_PLAN.md R1):
rounds in order, stubs before the boundary, the stepped boundary, byte-identical renders."""

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
