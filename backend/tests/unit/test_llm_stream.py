"""Token streaming (converse_stream): SSE wire parsing per provider via
MockTransport byte streams, the fake's scripted chunks, router usage recording,
retry-before-first-byte, and the mid-stream hard-fail (no replay)."""

import httpx
import pytest

from jbrain.llm import (
    AnthropicClient,
    FakeLlmClient,
    LlmRouter,
    LlmTool,
    LlmTurn,
    LlmUsage,
    OpenAiCompatClient,
    ReasoningChunk,
    TextChunk,
    ToolCall,
    UserMessage,
)
from jbrain.llm.retry import BASE_DELAY_SECONDS
from jbrain.llm.types import StreamPart

TOOL = LlmTool(name="search", description="find", input_schema={"type": "object"})


def sse(*events: str) -> bytes:
    """Join raw SSE event blocks into a single response body."""
    return ("\n\n".join(events) + "\n\n").encode()


def stream_transport(body: bytes, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handle)


async def collect(client: object, **kw: object) -> list[StreamPart]:
    return [part async for part in client.converse_stream(**kw)]  # type: ignore[attr-defined]


# --- Anthropic ---------------------------------------------------------------

ANTHROPIC_TOOL_STREAM = sse(
    'data: {"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":1}}}',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"let me "}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"check"}}',
    'data: {"type":"content_block_stop","index":0}',
    'data: {"type":"content_block_start","index":1,"content_block":'
    '{"type":"tool_use","id":"tu_1","name":"search","input":{}}}',
    'data: {"type":"content_block_delta","index":1,"delta":'
    '{"type":"input_json_delta","partial_json":"{\\"q\\":"}}',
    'data: {"type":"content_block_delta","index":1,"delta":'
    '{"type":"input_json_delta","partial_json":" \\"x\\"}"}}',
    'data: {"type":"content_block_stop","index":1}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}',
    'data: {"type":"message_stop"}',
)


async def test_anthropic_stream_text_chunks_then_assembled_tool_turn() -> None:
    seen: list[httpx.Request] = []
    client = AnthropicClient("k", transport=stream_transport(ANTHROPIC_TOOL_STREAM, seen))
    parts = await collect(
        client, model="m", system="s", messages=[UserMessage(text="hi")], tools=[TOOL]
    )

    # The request asked to stream and carried the tool.
    import json

    body = json.loads(seen[0].content)
    assert body["stream"] is True
    assert body["tools"][0]["name"] == "search"

    chunks = [p for p in parts if isinstance(p, TextChunk)]
    assert [c.text for c in chunks] == ["let me ", "check"]

    *_, final = parts
    assert isinstance(final, LlmTurn)
    assert final.text == "let me check"
    assert final.stop_reason == "tool_use"
    assert final.tool_calls == (ToolCall(id="tu_1", name="search", arguments={"q": "x"}),)
    assert (final.usage.input_tokens, final.usage.output_tokens) == (12, 7)


async def test_anthropic_stream_plain_text_end_turn() -> None:
    body = sse(
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3,"output_tokens":0}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":'
        '{"type":"text_delta","text":"done"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":1}}',
        'data: {"type":"message_stop"}',
    )
    client = AnthropicClient("k", transport=stream_transport(body))
    parts = await collect(client, model="m", system="s", messages=[UserMessage(text="hi")])
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["done"]
    final = parts[-1]
    assert isinstance(final, LlmTurn)
    assert final.text == "done" and final.stop_reason == "end_turn" and not final.tool_calls


# --- OpenAI-compatible -------------------------------------------------------

OPENAI_TOOL_STREAM = sse(
    'data: {"choices":[{"delta":{"role":"assistant","content":"let me "},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"content":"check"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function",'
    '"function":{"name":"search","arguments":""}}]},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"{\\"q\\": "}}]},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"\\"x\\"}"}}]},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}',
    "data: [DONE]",
)


async def test_openai_stream_text_chunks_then_assembled_tool_turn() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1",
        "k",
        provider="xai",
        transport=stream_transport(OPENAI_TOOL_STREAM, seen),
    )
    parts = await collect(
        client, model="m", system="s", messages=[UserMessage(text="hi")], tools=[TOOL]
    )

    import json

    body = json.loads(seen[0].content)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}

    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["let me ", "check"]
    final = parts[-1]
    assert isinstance(final, LlmTurn)
    assert final.text == "let me check"
    assert final.stop_reason == "tool_use"
    assert final.tool_calls == (ToolCall(id="call_1", name="search", arguments={"q": "x"}),)
    assert (final.usage.input_tokens, final.usage.output_tokens) == (12, 7)


async def test_openai_stream_plain_text_handles_missing_usage_chunk() -> None:
    # A local server may omit the trailing usage chunk entirely; usage stays zero.
    body = sse(
        'data: {"choices":[{"delta":{"content":"hi there"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )
    client = OpenAiCompatClient(
        "http://localhost:11434/v1", "", provider="local", transport=stream_transport(body)
    )
    parts = await collect(client, model="m", system="s", messages=[UserMessage(text="hi")])
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["hi there"]
    final = parts[-1]
    assert isinstance(final, LlmTurn)
    assert final.text == "hi there" and final.stop_reason == "end_turn"
    assert (final.usage.input_tokens, final.usage.output_tokens) == (0, 0)


# A local gpt-oss/GLM stream interleaves harmony reasoning on `reasoning_content`
# before the answer arrives on `content`.
OPENAI_REASONING_STREAM = sse(
    'data: {"choices":[{"delta":{"reasoning_content":"let me "},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"reasoning_content":"think"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"content":"the answer"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
)


async def test_openai_stream_surfaces_reasoning_then_text() -> None:
    client = OpenAiCompatClient(
        "http://localhost:11434/v1",
        "",
        provider="local",
        transport=stream_transport(OPENAI_REASONING_STREAM),
    )
    parts = await collect(client, model="m", system="s", messages=[UserMessage(text="hi")])

    # Reasoning slices arrive as their own chunks, ahead of the answer text.
    assert [p.text for p in parts if isinstance(p, ReasoningChunk)] == ["let me ", "think"]
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["the answer"]
    final = parts[-1]
    assert isinstance(final, LlmTurn)
    # The final turn carries the answer and the joined reasoning trace.
    assert final.text == "the answer"
    assert final.reasoning == "let me think"


# --- Retry / failure semantics ----------------------------------------------


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_stream_retries_before_first_byte_then_succeeds() -> None:
    body = sse(
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1,"output_tokens":0}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":1}}',
    )
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "x"})
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=httpx.MockTransport(handle), sleep=sleep)
    parts = await collect(client, model="m", system="s", messages=[UserMessage(text="hi")])
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["ok"]
    assert calls["n"] == 2
    assert sleep.delays == [BASE_DELAY_SECONDS]


class _RaisingStream(httpx.AsyncByteStream):
    """Yields some bytes then raises — a connection drop mid-stream."""

    def __init__(self, chunks: list[bytes], exc: Exception) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self):  # type: ignore[override]
        for chunk in self._chunks:
            yield chunk
        raise self._exc

    async def aclose(self) -> None:
        pass


async def test_stream_failure_after_first_byte_does_not_replay() -> None:
    first = (
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"partial"}}\n\n'
    )
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            stream=_RaisingStream([first], httpx.ReadError("dropped")),
            headers={"content-type": "text/event-stream"},
        )

    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=httpx.MockTransport(handle), sleep=sleep)
    seen: list[StreamPart] = []
    with pytest.raises(Exception, match="stream interrupted"):
        async for part in client.converse_stream(
            model="m", system="s", messages=[UserMessage(text="hi")]
        ):
            seen.append(part)

    # The partial chunk reached the caller; the drop did not trigger a retry.
    assert [p.text for p in seen if isinstance(p, TextChunk)] == ["partial"]
    assert calls["n"] == 1
    assert sleep.delays == []


# --- Fake + router -----------------------------------------------------------


async def test_fake_streams_scripted_chunks_then_turn() -> None:
    turn = LlmTurn(text="hello world", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(2, 3))
    fake = FakeLlmClient(turns=[turn], stream_chunks=[["hello ", "world"]])
    parts = [
        p async for p in fake.converse_stream(model="m", system="s", messages=[UserMessage("u")])
    ]
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["hello ", "world"]
    assert parts[-1] is turn
    assert fake.stream_calls[0]["model"] == "m"


async def test_fake_default_chunks_to_whole_turn_text() -> None:
    turn = LlmTurn(text="answer", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))
    fake = FakeLlmClient(turns=[turn])
    parts = [
        p async for p in fake.converse_stream(model="m", system="s", messages=[UserMessage("u")])
    ]
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["answer"]
    assert isinstance(parts[-1], LlmTurn)


async def test_router_converse_stream_records_usage_from_final_turn() -> None:
    records: list[tuple[str, int, int]] = []

    class Recorder:
        async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
            records.append((task, usage.input_tokens, usage.output_tokens))

    turn = LlmTurn(text="hi", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(5, 9))
    fake = FakeLlmClient(turns=[turn], stream_chunks=[["h", "i"]])
    router = LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}, recorder=Recorder())

    parts = [
        p
        async for p in router.converse_stream("agent.turn", system="s", messages=[UserMessage("u")])
    ]
    assert [p.text for p in parts if isinstance(p, TextChunk)] == ["h", "i"]
    # Usage recorded exactly once, from the closing turn — chunks carry none.
    assert records == [("agent.turn", 5, 9)]
    assert fake.stream_calls[0]["model"] == "grok-4.3"
