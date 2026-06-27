"""The llama-swap admin client: tolerant /running parsing, best-effort failure
on running(), and a surfaced error on unload(). All via httpx.MockTransport."""

import json

import httpx
import pytest

from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError, _parse_running


def _client(handler: object) -> LocalGatewayClient:
    # base_url ends in /v1; the admin endpoints must resolve at the root.
    return LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_running_parses_object_with_a_running_list() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/running"  # /v1 stripped
        return httpx.Response(200, json={"running": [{"model": "a"}, {"model": "b"}]})

    assert await _client(handle).running() == {"a", "b"}


async def test_running_parses_a_bare_list_of_strings() -> None:
    assert await _client(lambda r: httpx.Response(200, json=["a", "b"])).running() == {"a", "b"}


async def test_running_is_empty_on_http_error() -> None:
    assert await _client(lambda r: httpx.Response(404)).running() == set()


async def test_running_is_empty_when_unreachable() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert await _client(boom).running() == set()


async def test_unload_posts_to_the_model_path() -> None:
    seen: list[tuple[str, str]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200)

    await _client(handle).unload("qwen3-vl-30b-a3b")
    assert seen == [("POST", "/api/models/unload/qwen3-vl-30b-a3b")]


async def test_unload_raises_on_gateway_failure() -> None:
    with pytest.raises(LocalGatewayError):
        await _client(lambda r: httpx.Response(500)).unload("a")


async def test_load_probes_health_then_warms_with_one_token() -> None:
    seen: list[tuple[str, str]] = []
    body: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        if req.method == "POST":
            body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("qwen3-vl-30b-a3b")
    # Health GET loads the model; the 1-token POST faults the mmap'd weights in so the
    # user's first real turn isn't the cold load.
    assert seen == [
        ("GET", "/upstream/qwen3-vl-30b-a3b/health"),
        ("POST", "/upstream/qwen3-vl-30b-a3b/v1/chat/completions"),
    ]
    assert body["model"] == "qwen3-vl-30b-a3b"
    assert body["max_tokens"] == 1


async def test_load_raises_when_the_model_cannot_load() -> None:
    # The health probe is the hard gate: a model that won't load surfaces an error.
    with pytest.raises(LocalGatewayError):
        await _client(lambda r: httpx.Response(503)).load("a")


async def test_load_warm_up_is_best_effort() -> None:
    # Health succeeds (model loaded) but the warm-up generation fails — load() must NOT
    # raise: the model is resident, the warm-up only pre-faults it.
    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if req.url.path.endswith("/health") else 500)

    await _client(handle).load("a")  # no raise


def test_parse_running_tolerates_messy_shapes() -> None:
    assert _parse_running({"models": ["x", {"id": "y"}, {"name": "z"}, 5, {}]}) == {"x", "y", "z"}
    assert _parse_running("garbage") == set()
    assert _parse_running({"unexpected": 1}) == set()
    assert _parse_running([]) == set()
