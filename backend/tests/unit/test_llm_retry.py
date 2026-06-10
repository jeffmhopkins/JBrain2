"""Retry/backoff behavior and the error taxonomy, via MockTransport."""

import httpx
import pytest

from jbrain.llm import (
    AnthropicClient,
    LlmAuthError,
    LlmBadResponseError,
    LlmRateLimitError,
    LlmTransientError,
)
from jbrain.llm.retry import BASE_DELAY_SECONDS, MAX_TRIES, post_json

OK = {
    "content": [{"type": "text", "text": "ok"}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def sequence_transport(statuses: list[int]) -> httpx.MockTransport:
    """Returns the next status per call; 200 carries a valid Anthropic body."""
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        status = statuses[calls["n"]]
        calls["n"] += 1
        return httpx.Response(status, json=OK if status == 200 else {"error": "x"})

    return httpx.MockTransport(handle)


async def test_429_then_success_retries_with_exponential_backoff() -> None:
    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=sequence_transport([429, 429, 200]), sleep=sleep)
    result = await client.complete(model="m", system="s", user_text="u")
    assert result.text == "ok"
    assert sleep.delays == [BASE_DELAY_SECONDS, BASE_DELAY_SECONDS * 2]


async def test_rate_limit_exhausts_into_rate_limit_error() -> None:
    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=sequence_transport([429] * MAX_TRIES), sleep=sleep)
    with pytest.raises(LlmRateLimitError):
        await client.complete(model="m", system="s", user_text="u")
    assert len(sleep.delays) == MAX_TRIES - 1


async def test_5xx_retries_then_transient_error() -> None:
    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=sequence_transport([500, 503, 529, 502]), sleep=sleep)
    with pytest.raises(LlmTransientError):
        await client.complete(model="m", system="s", user_text="u")


async def test_network_error_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=OK)

    client = AnthropicClient("k", transport=httpx.MockTransport(handle), sleep=SleepRecorder())
    result = await client.complete(model="m", system="s", user_text="u")
    assert result.text == "ok"
    assert calls["n"] == 2


async def test_auth_error_is_immediate_no_retry() -> None:
    transport = sequence_transport([401, 200])
    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=transport, sleep=sleep)
    with pytest.raises(LlmAuthError):
        await client.complete(model="m", system="s", user_text="u")
    assert sleep.delays == []


async def test_other_4xx_is_bad_response_no_retry() -> None:
    sleep = SleepRecorder()
    client = AnthropicClient("k", transport=sequence_transport([400, 200]), sleep=sleep)
    with pytest.raises(LlmBadResponseError):
        await client.complete(model="m", system="s", user_text="u")
    assert sleep.delays == []


async def test_200_with_non_json_body_is_bad_response() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    sleep = SleepRecorder()
    with pytest.raises(LlmBadResponseError):
        await post_json(
            "https://example.test/x",
            headers={},
            payload={},
            provider="test",
            request_timeout=1.0,
            transport=httpx.MockTransport(handle),
            sleep=sleep,
        )
