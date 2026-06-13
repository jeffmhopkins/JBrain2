"""Unit tests for the content-addressed FixtureLlmClient (plan W0.3).

Pure (no DB, no provider): the deterministic dev/test backend that replays
authored model responses and records misses for authoring.
"""

import json

import pytest

from jbrain.llm.fixtures import FixtureLlmClient, MissingFixture, turn_from_dict, turn_to_dict
from jbrain.llm.types import (
    AssistantMessage,
    LlmTurn,
    LlmUsage,
    StreamPart,
    TextChunk,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


async def test_complete_replays_authored_text(tmp_path):
    c = FixtureLlmClient(tmp_path)
    c.write_complete(model="grok", system="sys", user_text="hi", text="hello")
    r = await c.complete(model="grok", system="sys", user_text="hi")
    assert r.text == "hello"
    assert r.parsed is None


async def test_complete_parses_json_when_schema_requested(tmp_path):
    c = FixtureLlmClient(tmp_path)
    # note.extract is always called with a schema; author the schema variant.
    c.write_complete(
        model="grok", system="sys", user_text="give json", text='{"a": 1}', with_schema=True
    )
    r = await c.complete(
        model="grok", system="sys", user_text="give json", json_schema={"type": "object"}
    )
    assert r.parsed == {"a": 1}


async def test_complete_miss_raises_with_prompt(tmp_path):
    c = FixtureLlmClient(tmp_path)
    with pytest.raises(MissingFixture) as exc:
        await c.complete(model="grok", system="sys", user_text="unseen")
    assert exc.value.prompt["user_text"] == "unseen"
    assert exc.value.prompt["op"] == "complete"


async def test_record_mode_captures_pending_prompt(tmp_path):
    c = FixtureLlmClient(tmp_path, record=True)
    with pytest.raises(MissingFixture) as exc:
        await c.complete(model="grok", system="sys", user_text="author me")
    pending = tmp_path / "_pending" / f"{exc.value.key}.json"
    assert pending.exists()
    captured = json.loads(pending.read_text())
    # The pending file's shape is exactly what replay reads: an op-specific null
    # slot ("text" for a complete call) the author fills, then promotes.
    assert captured["text"] is None
    assert captured["prompt"]["user_text"] == "author me"


async def test_pending_file_promotes_and_replays_complete(tmp_path):
    # The headline workflow: record a miss, author the slot, move the file up,
    # and a fresh strict client replays it.
    rec = FixtureLlmClient(tmp_path, record=True)
    with pytest.raises(MissingFixture) as exc:
        await rec.complete(model="grok", system="sys", user_text="author me")
    key = exc.value.key
    data = json.loads((tmp_path / "_pending" / f"{key}.json").read_text())
    data["text"] = "authored answer"
    (tmp_path / f"{key}.json").write_text(json.dumps(data))

    rep = FixtureLlmClient(tmp_path)
    r = await rep.complete(model="grok", system="sys", user_text="author me")
    assert r.text == "authored answer"


async def test_pending_file_promotes_and_replays_converse(tmp_path):
    rec = FixtureLlmClient(tmp_path, record=True)
    msgs = [UserMessage(text="find x")]
    with pytest.raises(MissingFixture) as exc:
        await rec.converse(model="grok", system="sys", messages=msgs)
    key = exc.value.key
    data = json.loads((tmp_path / "_pending" / f"{key}.json").read_text())
    assert data["turn"] is None  # op-specific slot for a converse call
    data["turn"] = turn_to_dict(
        LlmTurn(text="answer", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(0, 0))
    )
    (tmp_path / f"{key}.json").write_text(json.dumps(data))

    rep = FixtureLlmClient(tmp_path)
    t = await rep.converse(model="grok", system="sys", messages=msgs)
    assert t.text == "answer"


async def test_unauthored_promoted_fixture_raises(tmp_path):
    # A fixture present but with its slot still null must not replay as None.
    rec = FixtureLlmClient(tmp_path, record=True)
    with pytest.raises(MissingFixture) as exc:
        await rec.complete(model="grok", system="sys", user_text="x")
    key = exc.value.key
    (tmp_path / f"{key}.json").write_text((tmp_path / "_pending" / f"{key}.json").read_text())
    rep = FixtureLlmClient(tmp_path)
    with pytest.raises(MissingFixture):
        await rep.complete(model="grok", system="sys", user_text="x")


async def test_strict_mode_does_not_write_pending(tmp_path):
    c = FixtureLlmClient(tmp_path, record=False)
    with pytest.raises(MissingFixture):
        await c.complete(model="grok", system="sys", user_text="x")
    assert not (tmp_path / "_pending").exists()


async def test_identical_request_is_one_stable_key(tmp_path):
    c = FixtureLlmClient(tmp_path)
    c.write_complete(model="grok", system="sys", user_text="dup", text="once")
    a = await c.complete(model="grok", system="sys", user_text="dup")
    b = await c.complete(model="grok", system="sys", user_text="dup")
    assert a.text == b.text == "once"


async def test_converse_loop_walks_two_distinct_prompts(tmp_path):
    # Turn 1: the model asks for a tool. Turn 2 (messages grown by the result):
    # the model answers. Each turn is a distinct prompt → its own fixture.
    c = FixtureLlmClient(tmp_path)
    tool_turn = LlmTurn(
        text="",
        tool_calls=(ToolCall(id="t1", name="search", arguments={"q": "x"}),),
        stop_reason="tool_use",
        usage=LlmUsage(0, 0),
    )
    msgs1 = [UserMessage(text="find x")]
    c.write_converse(model="grok", system="sys", messages=msgs1, turn=tool_turn)

    msgs2 = [
        UserMessage(text="find x"),
        AssistantMessage(text="", tool_calls=tool_turn.tool_calls),
        ToolResultMessage(results=(ToolResult(tool_call_id="t1", content="found"),)),
    ]
    final = LlmTurn(text="done", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(0, 0))
    c.write_converse(model="grok", system="sys", messages=msgs2, turn=final)

    t1 = await c.converse(model="grok", system="sys", messages=msgs1)
    assert t1.stop_reason == "tool_use" and t1.tool_calls[0].name == "search"
    t2 = await c.converse(model="grok", system="sys", messages=msgs2)
    assert t2.stop_reason == "end_turn" and t2.text == "done"
    assert len(c.calls) == 2


async def test_converse_stream_yields_chunks_then_turn(tmp_path):
    c = FixtureLlmClient(tmp_path)
    msgs = [UserMessage(text="hi")]
    turn = LlmTurn(text="streamed", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(0, 0))
    c.write_converse(model="grok", system="sys", messages=msgs, turn=turn)
    parts: list[StreamPart] = [
        p async for p in c.converse_stream(model="grok", system="sys", messages=msgs)
    ]
    assert isinstance(parts[0], TextChunk) and parts[0].text == "streamed"
    assert isinstance(parts[-1], LlmTurn) and parts[-1].text == "streamed"


def test_turn_roundtrip_serialization():
    turn = LlmTurn(
        text="t",
        tool_calls=(ToolCall(id="a", name="n", arguments={"k": 1}),),
        stop_reason="tool_use",
        usage=LlmUsage(5, 6),
    )
    again = turn_from_dict(turn_to_dict(turn))
    assert again.text == "t"
    assert again.tool_calls[0].name == "n" and again.tool_calls[0].arguments == {"k": 1}
    assert again.stop_reason == "tool_use"


def test_turn_from_dict_infers_stop_reason():
    # A recorded dict without stop_reason infers tool_use iff it has tool calls.
    assert turn_from_dict({"text": "hi"}).stop_reason == "end_turn"
    assert (
        turn_from_dict({"tool_calls": [{"id": "1", "name": "x", "arguments": {}}]}).stop_reason
        == "tool_use"
    )
