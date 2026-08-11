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
from jbrain.llm.errors import LlmContextOverflowError
from jbrain.llm.retry import BASE_DELAY_SECONDS, MAX_TRIES, post_json, stream_sse

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


# llama.cpp/llama-swap's real 400 body when the prompt outgrows the served window.
_CTX_OVERFLOW_BODY = {
    "error": {
        "message": "the request exceeds the available context size. "
        "try increasing the context size or enable context shift. n_ctx = 32768, n_tokens = 33990",
        "type": "invalid_request_error",
        "code": 400,
    }
}


async def _post_400(body: object) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    await post_json(
        "https://example.test/x",
        headers={},
        payload={},
        provider="local",
        request_timeout=1.0,
        transport=httpx.MockTransport(handle),
        sleep=SleepRecorder(),
    )


async def test_context_overflow_400_is_a_typed_error_no_retry() -> None:
    # A 400 whose body says the prompt exceeded n_ctx becomes the context-overflow
    # subtype so callers can tell the owner the model ran out of context.
    sleep = SleepRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_CTX_OVERFLOW_BODY)

    with pytest.raises(LlmContextOverflowError) as ei:
        await post_json(
            "https://example.test/x",
            headers={},
            payload={},
            provider="local",
            request_timeout=1.0,
            transport=httpx.MockTransport(handle),
            sleep=sleep,
        )
    # Still a bad-response (keeps existing non-retry handling); carries NO body text.
    assert isinstance(ei.value, LlmBadResponseError)
    assert "context window exceeded" in str(ei.value)
    assert "n_ctx" not in str(ei.value)  # the private body never rides the message
    assert sleep.delays == []  # a 400 is terminal — never retried


async def test_generic_400_stays_a_plain_bad_response() -> None:
    # A 400 unrelated to context must NOT be misclassified as an overflow — including one
    # that merely mentions "context" with a "too long" value (the old loose matcher's trap).
    with pytest.raises(LlmBadResponseError) as ei:
        await _post_400({"error": {"message": "invalid tool arguments"}})
    assert not isinstance(ei.value, LlmContextOverflowError)
    with pytest.raises(LlmBadResponseError) as ei2:
        await _post_400({"error": {"message": "field 'context' value too long"}})
    assert not isinstance(ei2.value, LlmContextOverflowError)


async def test_cloud_overflow_is_not_context_typed() -> None:
    # Only a LOCAL model's overflow becomes the typed error (the "raise the window in
    # Settings" advice is local-only). The SAME body from a cloud provider stays generic.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_CTX_OVERFLOW_BODY)

    with pytest.raises(LlmBadResponseError) as ei:
        await post_json(
            "https://example.test/x",
            headers={},
            payload={},
            provider="xai",
            request_timeout=1.0,
            transport=httpx.MockTransport(handle),
            sleep=SleepRecorder(),
        )
    assert not isinstance(ei.value, LlmContextOverflowError)


async def test_stream_sse_classifies_context_overflow() -> None:
    # The streaming path (the agent turn) is the one that actually hits this — a 400
    # before any SSE data must surface as the typed overflow, non-retryably.
    sleep = SleepRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_CTX_OVERFLOW_BODY)

    with pytest.raises(LlmContextOverflowError):
        async for _ in stream_sse(
            "https://example.test/x",
            headers={},
            payload={},
            provider="local",
            request_timeout=1.0,
            transport=httpx.MockTransport(handle),
            sleep=sleep,
        ):
            pass
    assert sleep.delays == []
