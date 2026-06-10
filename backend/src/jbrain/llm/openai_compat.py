"""OpenAI-compatible chat-completions client over raw httpx.

Serves two providers: xAI (https://api.x.ai/v1) and the local escape hatch
(Ollama-style server at JBRAIN_LOCAL_LLM_URL). Keeping them on one client is
what makes "go all-local" a config flip instead of a refactor — see
docs/ANALYSIS.md "Privacy routing".
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from jbrain.llm.errors import LlmBadResponseError
from jbrain.llm.retry import post_json
from jbrain.llm.types import DEFAULT_MAX_TOKENS, LlmImage, LlmResult, LlmUsage, parse_json_payload

DEFAULT_TIMEOUT = 120.0


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
