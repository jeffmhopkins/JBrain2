"""Wire format per provider: auth headers, content blocks, image parts,
structured-output requests, and usage extraction. All via MockTransport."""

import json
from typing import Any

import httpx
import pytest

from jbrain.llm import (
    AnthropicClient,
    AssistantMessage,
    LlmBadResponseError,
    LlmImage,
    LlmTool,
    OpenAiCompatClient,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}
TOOL = LlmTool(name="search", description="find things", input_schema=SCHEMA)


def capture_transport(captured: list[httpx.Request], body: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 12, "output_tokens": 5},
}

OPENAI_OK = {
    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}


async def test_anthropic_request_shape() -> None:
    seen: list[httpx.Request] = []
    client = AnthropicClient("sk-ant-test", transport=capture_transport(seen, ANTHROPIC_OK))
    result = await client.complete(model="claude-sonnet-4-6", system="sys", user_text="hi")

    (request,) = seen
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-4-6"
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "output_config" not in body
    assert result.text == "hello"
    assert result.parsed is None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 5)


async def test_anthropic_vision_uses_base64_image_blocks() -> None:
    seen: list[httpx.Request] = []
    client = AnthropicClient("k", transport=capture_transport(seen, ANTHROPIC_OK))
    await client.complete(
        model="m",
        system="s",
        user_text="describe",
        images=[LlmImage(media_type="image/png", data="QUJD")],
    )
    content = json.loads(seen[0].content)["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }
    assert content[1] == {"type": "text", "text": "describe"}


async def test_anthropic_structured_output_request_and_parse() -> None:
    seen: list[httpx.Request] = []
    body = {
        "content": [{"type": "text", "text": '{"name": "Ada"}'}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client = AnthropicClient("k", transport=capture_transport(seen, body))
    result = await client.complete(model="m", system="s", user_text="u", json_schema=SCHEMA)
    sent = json.loads(seen[0].content)
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert result.parsed == {"name": "Ada"}


async def test_anthropic_concatenates_text_blocks_and_rejects_bad_shape() -> None:
    body = {
        "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    seen: list[httpx.Request] = []
    client = AnthropicClient("k", transport=capture_transport(seen, body))
    result = await client.complete(model="m", system="s", user_text="u")
    assert result.text == "ab"

    broken = AnthropicClient("k", transport=capture_transport([], {"unexpected": True}))
    with pytest.raises(LlmBadResponseError):
        await broken.complete(model="m", system="s", user_text="u")


async def test_xai_request_shape() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1",
        "xai-test",
        provider="xai",
        transport=capture_transport(seen, OPENAI_OK),
    )
    result = await client.complete(model="grok-4.3", system="sys", user_text="hi")

    (request,) = seen
    assert str(request.url) == "https://api.x.ai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer xai-test"
    body = json.loads(request.content)
    assert body["model"] == "grok-4.3"
    # Text-only calls use a plain string user message, not content parts.
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert result.text == "hello"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 5)


async def test_xai_vision_uses_image_url_parts() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=capture_transport(seen, OPENAI_OK)
    )
    await client.complete(
        model="m",
        system="s",
        user_text="describe",
        images=[LlmImage(media_type="image/jpeg", data="QUJD")],
    )
    user = json.loads(seen[0].content)["messages"][1]
    assert user["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,QUJD"},
    }
    assert user["content"][1] == {"type": "text", "text": "describe"}


async def test_xai_structured_output_request_and_parse() -> None:
    seen: list[httpx.Request] = []
    body = {
        "choices": [{"message": {"content": '```json\n{"name": "Ada"}\n```'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=capture_transport(seen, body)
    )
    result = await client.complete(model="m", system="s", user_text="u", json_schema=SCHEMA)
    sent = json.loads(seen[0].content)
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": SCHEMA, "strict": True},
    }
    # Fenced JSON parses without burning the re-ask.
    assert result.parsed == {"name": "Ada"}


async def test_local_provider_hits_configured_base_url_without_auth() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "http://localhost:11434/v1",
        "",
        provider="local",
        transport=capture_transport(seen, {"choices": [{"message": {"content": "hi"}}]}),
    )
    result = await client.complete(model="llama3", system="s", user_text="u")
    (request,) = seen
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in request.headers
    # Local servers may omit usage entirely.
    assert (result.usage.input_tokens, result.usage.output_tokens) == (0, 0)


async def test_openai_compat_rejects_bad_shape() -> None:
    client = OpenAiCompatClient(
        "https://api.x.ai/v1",
        "k",
        provider="xai",
        transport=capture_transport([], {"choices": []}),
    )
    with pytest.raises(LlmBadResponseError):
        await client.complete(model="m", system="s", user_text="u")


# --- converse (tool-using turns) -------------------------------------------

ANTHROPIC_TOOL_TURN = {
    "content": [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "x"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 3, "output_tokens": 4},
}
ANTHROPIC_END_TURN = {
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


async def test_anthropic_converse_sends_tools_and_parses_tool_use() -> None:
    seen: list[httpx.Request] = []
    client = AnthropicClient("k", transport=capture_transport(seen, ANTHROPIC_TOOL_TURN))
    turn = await client.converse(
        model="m", system="s", messages=[UserMessage(text="hi")], tools=[TOOL]
    )
    body = json.loads(seen[0].content)
    assert body["tools"] == [
        {"name": "search", "description": "find things", "input_schema": SCHEMA}
    ]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert turn.text == "let me check"
    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls == (ToolCall(id="tu_1", name="search", arguments={"q": "x"}),)
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (3, 4)


async def test_anthropic_converse_replays_tool_calls_and_results() -> None:
    seen: list[httpx.Request] = []
    client = AnthropicClient("k", transport=capture_transport(seen, ANTHROPIC_END_TURN))
    messages = [
        UserMessage(text="hi"),
        AssistantMessage(
            text="checking", tool_calls=[ToolCall(id="tu_1", name="search", arguments={"q": "x"})]
        ),
        ToolResultMessage(results=[ToolResult(tool_call_id="tu_1", content="found")]),
    ]
    turn = await client.converse(model="m", system="s", messages=messages)
    msgs = json.loads(seen[0].content)["messages"]
    assert msgs[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "x"}},
        ],
    }
    assert msgs[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "found", "is_error": False}
        ],
    }
    assert turn.stop_reason == "end_turn" and turn.text == "done"


async def test_anthropic_converse_unknown_stop_reason_is_end_turn() -> None:
    body = {
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "stop_sequence",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client = AnthropicClient("k", transport=capture_transport([], body))
    turn = await client.converse(model="m", system="s", messages=[UserMessage(text="hi")])
    assert turn.stop_reason == "end_turn"


OPENAI_TOOL_TURN = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q": "x"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
}


async def test_openai_converse_sends_tools_and_parses_tool_calls() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1",
        "k",
        provider="xai",
        transport=capture_transport(seen, OPENAI_TOOL_TURN),
    )
    turn = await client.converse(
        model="m", system="s", messages=[UserMessage(text="hi")], tools=[TOOL]
    )
    body = json.loads(seen[0].content)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "search", "description": "find things", "parameters": SCHEMA},
        }
    ]
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
    ]
    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls == (ToolCall(id="call_1", name="search", arguments={"q": "x"}),)
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (3, 4)


async def test_openai_converse_serializes_assistant_calls_and_tool_results() -> None:
    seen: list[httpx.Request] = []
    body = {
        "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=capture_transport(seen, body)
    )
    messages = [
        UserMessage(text="hi"),
        AssistantMessage(
            text="", tool_calls=[ToolCall(id="call_1", name="search", arguments={"q": "x"})]
        ),
        ToolResultMessage(results=[ToolResult(tool_call_id="call_1", content="found")]),
    ]
    turn = await client.converse(model="m", system="s", messages=messages)
    msgs = json.loads(seen[0].content)["messages"]
    assert msgs[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": "x"}'},
            }
        ],
    }
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "found"}
    assert turn.text == "done" and turn.stop_reason == "end_turn"


async def test_openai_converse_rejects_non_object_tool_arguments() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "x", "arguments": "not json"},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=capture_transport([], body)
    )
    with pytest.raises(LlmBadResponseError):
        await client.converse(model="m", system="s", messages=[UserMessage(text="hi")])


# --- reasoning_effort: xai-only, threaded through every surface ---------------


def _sse(*events: str) -> bytes:
    return ("\n\n".join(events) + "\n\n").encode()


def _stream_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    body = _sse(
        'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handle)


async def test_xai_complete_includes_reasoning_effort() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=capture_transport(seen, OPENAI_OK)
    )
    await client.complete(model="m", system="s", user_text="u", reasoning_effort="high")
    assert json.loads(seen[0].content)["reasoning_effort"] == "high"


async def test_xai_converse_includes_reasoning_effort() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=capture_transport(seen, OPENAI_OK)
    )
    await client.converse(
        model="m", system="s", messages=[UserMessage(text="u")], reasoning_effort="low"
    )
    assert json.loads(seen[0].content)["reasoning_effort"] == "low"


async def test_xai_converse_stream_includes_reasoning_effort() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "https://api.x.ai/v1", "k", provider="xai", transport=_stream_transport(seen)
    )
    async for _ in client.converse_stream(
        model="m", system="s", messages=[UserMessage(text="u")], reasoning_effort="medium"
    ):
        pass
    assert json.loads(seen[0].content)["reasoning_effort"] == "medium"


async def test_local_never_sends_reasoning_effort_even_when_passed() -> None:
    seen: list[httpx.Request] = []
    client = OpenAiCompatClient(
        "http://localhost:11434/v1",
        "",
        provider="local",
        transport=capture_transport(seen, OPENAI_OK),
    )
    await client.complete(model="m", system="s", user_text="u", reasoning_effort="high")
    await client.converse(
        model="m", system="s", messages=[UserMessage(text="u")], reasoning_effort="high"
    )
    assert all("reasoning_effort" not in json.loads(r.content) for r in seen)


async def test_anthropic_ignores_reasoning_effort_kwarg() -> None:
    seen: list[httpx.Request] = []
    client = AnthropicClient("k", transport=capture_transport(seen, ANTHROPIC_OK))
    # Accepts the kwarg without error and never leaks it onto the wire.
    await client.complete(model="m", system="s", user_text="u", reasoning_effort="high")
    assert "reasoning_effort" not in json.loads(seen[0].content)
