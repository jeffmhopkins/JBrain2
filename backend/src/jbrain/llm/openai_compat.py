"""OpenAI-compatible chat-completions client over raw httpx.

Serves two providers: xAI (https://api.x.ai/v1) and the local escape hatch
(Ollama-style server at JBRAIN_LOCAL_LLM_URL). Keeping them on one client is
what makes "go all-local" a config flip instead of a refactor — see
docs/reference/ANALYSIS.md "Privacy routing".
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import httpx

from jbrain.llm.errors import LlmBadResponseError
from jbrain.llm.retry import post_json, stream_sse
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    ReasoningChunk,
    StopReason,
    StreamPart,
    TextChunk,
    ToolCall,
    UserMessage,
    parse_json_payload,
)

DEFAULT_TIMEOUT = 120.0

# OpenAI finish_reason → our normalized stop reason.
_OPENAI_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


def _user_content(text: str, images: Sequence[LlmImage]) -> str | list[dict[str, Any]]:
    if not images:
        return text
    parts: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": f"data:{i.media_type};base64,{i.data}"}}
        for i in images
    ]
    parts.append({"type": "text", "text": text})
    return parts


def _openai_messages(system: str, messages: Sequence[LlmMessage]) -> list[dict[str, Any]]:
    """Flatten provider-agnostic messages into the OpenAI chat array. Tool
    results become individual `tool`-role messages, one per result."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": _user_content(msg.text, msg.images)})
        elif isinstance(msg, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant", "content": msg.text or None}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in msg.tool_calls
                ]
            out.append(entry)
        else:  # ToolResultMessage
            out.extend(
                {"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
                for r in msg.results
            )
    return out


class OpenAiCompatClient:
    """POST {base_url}/chat/completions with Bearer auth and image_url parts."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        provider: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.provider = provider
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
    ) -> LlmResult:
        user_content: str | list[dict[str, Any]]
        if images:
            user_content = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{img.media_type};base64,{img.data}"},
                }
                for img in images
            ]
            user_content.append({"type": "text", "text": user_text})
        else:
            user_content = user_text
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        self._apply_reasoning(payload, reasoning_effort)
        # Local servers run keyless; omitting the header beats sending "Bearer ".
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = await post_json(
            f"{self._base_url}/chat/completions",
            headers=headers,
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmBadResponseError(f"{self.provider}: unexpected response shape") from exc
        # Local servers may omit usage; zeros keep the call observable anyway.
        usage_body = data.get("usage") or {}
        usage = LlmUsage(
            input_tokens=int(usage_body.get("prompt_tokens", 0)),
            output_tokens=int(usage_body.get("completion_tokens", 0)),
        )
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=usage)

    def _apply_reasoning(self, payload: dict[str, Any], reasoning_effort: str | None) -> None:
        # `reasoning_effort` is honored by xAI Grok and by the local gateway's
        # reasoning models (gpt-oss/GLM via llama.cpp). The ROUTER gates eligibility
        # (it only sets an effort for a reasoning-capable provider+model), so the
        # client applies whatever it's given for those two providers — a non-reasoning
        # local model never reaches here with an effort set.
        if reasoning_effort is not None and self.provider in ("xai", "local"):
            payload["reasoning_effort"] = reasoning_effort

    def _converse_payload(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool],
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _openai_messages(system, messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        self._apply_reasoning(payload, reasoning_effort)
        return payload

    def _auth_headers(self) -> dict[str, str]:
        # Local servers run keyless; omitting the header beats sending "Bearer ".
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
    ) -> LlmTurn:
        payload = self._converse_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        headers = self._auth_headers()
        data = await post_json(
            f"{self._base_url}/chat/completions",
            headers=headers,
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            reasoning = message.get("reasoning_content") or ""
            tool_calls = tuple(self._tool_call(tc) for tc in message.get("tool_calls") or ())
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmBadResponseError(f"{self.provider}: unexpected response shape") from exc
        usage_body = data.get("usage") or {}
        usage = LlmUsage(
            input_tokens=int(usage_body.get("prompt_tokens", 0)),
            output_tokens=int(usage_body.get("completion_tokens", 0)),
        )
        return LlmTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=_OPENAI_STOP.get(finish, "end_turn"),
            usage=usage,
            reasoning=reasoning,
        )

    def _tool_call(self, raw: dict[str, Any]) -> ToolCall:
        """Parse one OpenAI tool_call; its arguments are a JSON *string*."""
        fn = raw["function"]
        arguments = parse_json_payload(fn.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise LlmBadResponseError(
                f"{self.provider}: tool_call arguments were not a JSON object"
            )
        return ToolCall(id=raw["id"], name=fn["name"], arguments=arguments)

    async def converse_stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamPart]:
        """Stream a turn over chat-completions SSE chunks. Content deltas stream
        live; tool_call deltas arrive fragmented and keyed by index (id/name on
        the first, argument fragments after), assembled whole at the end.
        `include_usage` asks for a trailing usage-only chunk (local servers may
        omit it; usage then stays zero, as in `converse`)."""
        payload = self._converse_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        events = stream_sse(
            f"{self._base_url}/chat/completions",
            headers=self._auth_headers(),
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Tool calls keyed by their stream index: id/name land on the first delta
        # for that index, argument fragments accumulate across later ones.
        calls_by_index: dict[int, dict[str, Any]] = {}
        input_tokens = 0
        output_tokens = 0
        stop: StopReason = "end_turn"
        async for event in events:
            usage_body = event.get("usage")
            if usage_body:
                input_tokens = int(usage_body.get("prompt_tokens", input_tokens))
                output_tokens = int(usage_body.get("completion_tokens", output_tokens))
            for choice in event.get("choices") or ():
                delta = choice.get("delta") or {}
                # A reasoning model (gpt-oss/GLM via the local gateway) streams its
                # thinking on a separate `reasoning_content` channel; surface it as a
                # ReasoningChunk — never folded into the answer text.
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield ReasoningChunk(text=reasoning)
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    yield TextChunk(text=content)
                for tc in delta.get("tool_calls") or ():
                    self._accumulate_tool_call(calls_by_index, tc)
                finish = choice.get("finish_reason")
                if finish:
                    stop = _OPENAI_STOP.get(finish, "end_turn")
        tool_calls = tuple(self._finish_tool_call(buf) for _, buf in sorted(calls_by_index.items()))
        yield LlmTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=LlmUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            reasoning="".join(reasoning_parts),
        )

    @staticmethod
    def _accumulate_tool_call(buffers: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
        buf = buffers.setdefault(int(delta.get("index", 0)), {"id": "", "name": "", "args": ""})
        if delta.get("id"):
            buf["id"] = delta["id"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            buf["name"] = fn["name"]
        buf["args"] += fn.get("arguments") or ""

    def _finish_tool_call(self, buf: dict[str, Any]) -> ToolCall:
        arguments = parse_json_payload(buf["args"] or "{}")
        if not isinstance(arguments, dict):
            raise LlmBadResponseError(
                f"{self.provider}: streamed tool_call arguments were not a JSON object"
            )
        return ToolCall(id=buf["id"], name=buf["name"], arguments=arguments)
