"""Canned-response LlmClient for tests — the only LLM tests may call."""

from collections.abc import Sequence
from typing import Any

from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    LlmImage,
    LlmResult,
    LlmUsage,
    parse_json_payload,
)


class FakeLlmClient:
    """Replays `responses` in order (last one repeats) and records every call."""

    def __init__(self, responses: Sequence[str] = ("ok",)):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

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
