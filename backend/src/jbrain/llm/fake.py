"""Canned-response LlmClient for tests — the only LLM tests may call."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    StreamPart,
    TextChunk,
    parse_json_payload,
)


class FakeLlmClient:
    """Replays scripted responses and records every call.

    `responses` drive `complete` (last one repeats); `turns` drive `converse`,
    letting a test script a tool-using exchange (turn 1 requests a tool, turn 2
    answers). `calls` records `complete` calls, `converse_calls` records
    `converse` calls — both assertable in tests."""

    def __init__(
        self,
        responses: Sequence[str] = ("ok",),
        turns: Sequence[LlmTurn] = (),
        stream_chunks: Sequence[Sequence[str]] = (),
    ):
        self._responses = list(responses)
        self._turns = list(turns)
        # Per-converse_stream-call text chunks; defaults to the turn's whole text
        # as one chunk. Lets a test assert that text streams in pieces.
        self._stream_chunks = [list(c) for c in stream_chunks]
        self.calls: list[dict[str, Any]] = []
        self.converse_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user_text": user_text,
                "images": list(images),
                "json_schema": json_schema,
                "max_tokens": max_tokens,
            }
        )
        text = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=LlmUsage(1, 1))

    async def converse(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmTurn:
        self.converse_calls.append(
            {
                "model": model,
                "system": system,
                "messages": list(messages),
                "tools": list(tools),
                "max_tokens": max_tokens,
            }
        )
        if not self._turns:
            return LlmTurn(text="ok", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))
        return self._turns[min(len(self.converse_calls) - 1, len(self._turns) - 1)]

    async def converse_stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> AsyncIterator[StreamPart]:
        self.stream_calls.append(
            {
                "model": model,
                "system": system,
                "messages": list(messages),
                "tools": list(tools),
                "max_tokens": max_tokens,
            }
        )
        idx = len(self.stream_calls) - 1
        turn = (
            self._turns[min(idx, len(self._turns) - 1)]
            if self._turns
            else LlmTurn(text="ok", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))
        )
        if self._stream_chunks:
            chunks = self._stream_chunks[min(idx, len(self._stream_chunks) - 1)]
        else:
            chunks = [turn.text] if turn.text else []
        for chunk in chunks:
            yield TextChunk(text=chunk)
        yield turn
