"""The llama-swap admin client: tolerant /running parsing, best-effort failure
on running(), and a surfaced error on unload(). All via httpx.MockTransport."""

import json

import httpx
import pytest

from jbrain.llm.local_gateway import (
    LocalGatewayClient,
    LocalGatewayError,
    _parse_load_progress,
    _parse_running,
)


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
    # Health GET loads the model; the 1-token POST faults the weights in so the user's
    # first real turn isn't the cold load. The upstream capture runs alongside and is
    # covered separately — asserting it here would encode a race, since a load this fast
    # can finish before the watcher task is ever scheduled.
    assert [c for c in seen if not c[1].startswith("/logs")] == [
        ("GET", "/upstream/qwen3-vl-30b-a3b/health"),
        ("POST", "/upstream/qwen3-vl-30b-a3b/v1/chat/completions"),
    ]
    assert body["model"] == "qwen3-vl-30b-a3b"
    assert body["max_tokens"] == 1
    # No persona prompt passed → a bare readiness probe, no system message.
    assert body["messages"] == [{"role": "user", "content": "warmup"}]


async def test_load_primes_the_warm_up_with_the_given_system_prompt() -> None:
    # When a persona prompt is passed, the warm-up leads with it as the system message so
    # its KV prefix is prefilled during load — the first real turn carrying the same
    # leading prompt then reuses that prefix (gateway --cache-reuse) instead of a cold
    # prefill (the 60-90s first-response latency this targets).
    body: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("gpt-oss-120b", warm_system="PERSONA SYSTEM PROMPT")
    assert body["messages"] == [
        {"role": "system", "content": "PERSONA SYSTEM PROMPT"},
        {"role": "user", "content": "warmup"},
    ]
    assert body["max_tokens"] == 1


async def test_load_primes_with_tools_matching_a_real_turn() -> None:
    # Under the gateway's --jinja the template renders tool defs into the prompt's leading
    # tokens, so a persona-only warm diverges from a real (tool-carrying) turn before the
    # reusable prefix ends. Priming the SAME tools makes the warmed prefix an actual prefix
    # of the real turn, so --cache-reuse lands.
    body: dict[str, object] = {}
    fn = {"name": "web_search", "description": "", "parameters": {}}
    tools = [{"type": "function", "function": fn}]

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("gpt-oss-120b", warm_system="PERSONA", warm_tools=tools)
    assert body["messages"] == [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "warmup"},
    ]
    assert body["tools"] == tools


async def test_load_without_tools_sends_no_tools_key() -> None:
    # No tools passed → the warm body carries no `tools` key at all (not an empty list), so a
    # bare readiness probe stays byte-identical to before this priming existed.
    body: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("gpt-oss-120b", warm_system="PERSONA")
    assert "tools" not in body


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


async def test_tool_probe_posts_a_tool_carrying_completion() -> None:
    body: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).tool_probe("gpt-oss-120b")
    # A tool-CARRYING turn (that's the point — it exercises the build's tool grammar),
    # capped at one discarded token, against the served model's completions path.
    assert isinstance(body["tools"], list) and body["tools"]
    assert body["model"] == "gpt-oss-120b"
    assert body["max_tokens"] == 1


async def test_tool_probe_raises_on_a_gateway_error() -> None:
    # A build whose tool path crashes the upstream returns a non-2xx — the smoke test's
    # rollback signal, so (unlike the best-effort warm-up) this MUST surface.
    with pytest.raises(LocalGatewayError):
        await _client(lambda r: httpx.Response(500)).tool_probe("gpt-oss-120b")


def test_parse_running_tolerates_messy_shapes() -> None:
    assert _parse_running({"models": ["x", {"id": "y"}, {"name": "z"}, 5, {}]}) == {"x", "y", "z"}
    assert _parse_running("garbage") == set()
    assert _parse_running({"unexpected": 1}) == set()
    assert _parse_running([]) == set()


async def test_load_progress_parses_the_latest_percent_from_the_logs() -> None:
    logs = (
        "srv  load_model: loading model from /models/coder.gguf\n"
        "load_tensors: loading model tensors 25 %\n"
        "load_tensors: loading model tensors 80%\n"
        "srv  update_slots: all slots are idle\n"  # a non-load line is ignored
    )
    # The freshest load line wins (80%), reported as a 0..1 fraction.
    assert await _client(lambda r: httpx.Response(200, text=logs)).load_progress() == 0.8


async def test_load_progress_is_none_without_a_parseable_line() -> None:
    # No load-keyword line carrying a percent → soft miss, not an error (bar uses the
    # time estimate). A stray "100% idle" lacks a load keyword and must be ignored.
    logs = "server listening\nupdate_slots: 100% idle\n"
    assert await _client(lambda r: httpx.Response(200, text=logs)).load_progress() is None


async def test_load_progress_is_none_when_logs_are_unavailable() -> None:
    # Old build without /logs (or gateway down) is a soft miss, never raised.
    assert await _client(lambda r: httpx.Response(404)).load_progress() is None


async def test_tail_logs_reads_the_proxy_buffer() -> None:
    """`/logs` is llama-swap's proxy monitor — its own lines, not llama.cpp's.

    Three attempts to reach the engine's output through this surface failed, each
    differently: `/logs/upstream` 404s; `/logs/stream/*` is chunked text, not SSE; and it
    DOES replay history, so the attach-before-load design built on the opposite belief was
    unnecessary as well as unreliable. The memory measurement now comes from the device
    delta and needs no log route."""
    seen: dict[str, str] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, text="slot 0 launch\nslot 0 released\n")

    out = await _client(handle).tail_logs()
    assert out == "slot 0 launch\nslot 0 released\n"
    assert seen["path"] == "/logs"


async def test_tail_logs_raises_when_the_gateway_is_unreachable() -> None:
    # Unlike load_progress (a soft miss), tail_logs surfaces the failure — the operator
    # asked for the logs, so an empty success would mislead.
    with pytest.raises(LocalGatewayError):
        await _client(lambda r: httpx.Response(503)).tail_logs()


async def test_tail_upstream_logs_reads_the_replay_burst_off_the_stream() -> None:
    """The engine's own output IS reachable — via the history `/logs/stream/*` replays.

    The route `tail_logs` cannot serve. This is the surface the three failed memory-scrape
    attempts were looking for, so the test pins both the path and that the burst is what
    comes back."""
    seen: dict[str, str] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(
            200, text="llama_model_loader: loaded\nmodel buffer size = 4400 MiB\n"
        )

    out = await _client(handle).tail_upstream_logs()
    assert "model buffer size" in out
    assert seen["path"] == "/logs/stream/upstream"


async def test_tail_upstream_logs_can_isolate_one_served_model() -> None:
    seen: dict[str, str] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, text="")

    await _client(handle).tail_upstream_logs("qwen3-30b")
    assert seen["path"] == "/logs/stream/qwen3-30b"


async def test_tail_upstream_logs_raises_when_the_stream_cannot_be_opened() -> None:
    # Same contract as tail_logs: the operator asked, so a miss is surfaced rather than
    # returning an empty body that reads as "the engine printed nothing".
    with pytest.raises(LocalGatewayError):
        await _client(lambda r: httpx.Response(404)).tail_upstream_logs()


def test_parse_load_progress_tolerates_floats_and_ignores_out_of_range() -> None:
    assert _parse_load_progress("load weights 12.5%") == 0.125
    assert _parse_load_progress("loading tensors 0%") == 0.0
    # A bogus >100 percent on a load line is rejected, leaving no signal.
    assert _parse_load_progress("load tensors 250%") is None
    assert _parse_load_progress("") is None


async def test_slot_action_refuses_a_model_that_is_not_resident() -> None:
    """The same guard `props` carries, for the same reason: `slot_action` reaches llama-server
    through the `/upstream/` passthrough, so calling it on a cold model would make llama-swap
    load that model outside the residency budget — the path that froze this host. The refusal
    must happen BEFORE any upstream request is issued."""
    touched: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        touched.append(req.url.path)
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": "other"}]})
        raise AssertionError(f"must not reach upstream: {req.url.path}")

    with pytest.raises(LocalGatewayError, match="not resident"):
        await _client(handle).slot_action("gpt-oss-120b", 1, "restore", filename="x.bin")
    assert touched == ["/running"]  # nothing proxied


async def test_slot_action_proceeds_for_a_resident_model() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": "gpt-oss-120b"}]})
        assert req.url.path == "/upstream/gpt-oss-120b/slots/1"
        assert req.url.params["action"] == "save"
        return httpx.Response(200, json={"id_slot": 1, "n_saved": 27476})

    result = await _client(handle).slot_action("gpt-oss-120b", 1, "save", filename="x.bin")
    assert result["n_saved"] == 27476
