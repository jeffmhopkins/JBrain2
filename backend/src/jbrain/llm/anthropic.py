"""Anthropic Messages API client over raw httpx (no SDK — fewer deps, and the
TEI embed client set the transport-injection precedent for tests)."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from jbrain.llm.errors import LlmBadResponseError
from jbrain.llm.retry import post_json
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    StopReason,
    ToolCall,
    UserMessage,
    parse_json_payload,
)

API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 120.0

# Anthropic's stop_reason values mapped onto our three. "stop_sequence" and any
# unknown reason collapse to a finished turn — the loop treats them as "no more
# tools," which is the safe default.
_ANTHROPIC_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
}


def _image_block(img: LlmImage) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": img.media_type, "data": img.data},
    }


def _anthropic_message(msg: LlmMessage) -> dict[str, Any]:
    """Map one provider-agnostic message onto an Anthropic message dict."""
    if isinstance(msg, UserMessage):
        content: list[dict[str, Any]] = [_image_block(i) for i in msg.images]
        content.append({"type": "text", "text": msg.text})
        return {"role": "user", "content": content}
    if isinstance(msg, AssistantMessage):
        blocks: list[dict[str, Any]] = []
        if msg.text:
            blocks.append({"type": "text", "text": msg.text})
        blocks.extend(
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
            for c in msg.tool_calls
        )
        return {"role": "assistant", "content": blocks}
    # ToolResultMessage — tool results are carried on a user-role turn.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content,
                "is_error": r.is_error,
            }
            for r in msg.results
        ],
    }


class AnthropicClient:
    """POST /v1/messages with x-api-key auth and content-block payloads."""

    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
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
    ) -> LlmResult:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": img.media_type, "data": img.data},
            }
            for img in images
        ]
        content.append({"type": "text", "text": user_text})
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if json_schema is not None:
            payload["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
        data = await post_json(
            f"{self._base_url}/v1/messages",
            headers={"x-api-key": self._api_key, "anthropic-version": API_VERSION},
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        try:
            text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
            usage = LlmUsage(
                input_tokens=int(data["usage"]["input_tokens"]),
                output_tokens=int(data["usage"]["output_tokens"]),
            )
        except (KeyError, TypeError) as exc:
            raise LlmBadResponseError(f"{self.provider}: unexpected response shape") from exc
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=usage)

    async def converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmTurn:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [_anthropic_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        data = await post_json(
            f"{self._base_url}/v1/messages",
            headers={"x-api-key": self._api_key, "anthropic-version": API_VERSION},
            payload=payload,
            provider=self.provider,
            request_timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        try:
            blocks = data["content"]
            text = "".join(b["text"] for b in blocks if b.get("type") == "text")
            tool_calls = tuple(
                ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
                for b in blocks
                if b.get("type") == "tool_use"
            )
            usage = LlmUsage(
                input_tokens=int(data["usage"]["input_tokens"]),
                output_tokens=int(data["usage"]["output_tokens"]),
            )
        except (KeyError, TypeError) as exc:
            raise LlmBadResponseError(f"{self.provider}: unexpected response shape") from exc
        stop = _ANTHROPIC_STOP.get(data.get("stop_reason", ""), "end_turn")
        return LlmTurn(text=text, tool_calls=tool_calls, stop_reason=stop, usage=usage)
