"""The llama-swap admin client: tolerant /running parsing, best-effort failure
on running(), and a surfaced error on unload(). All via httpx.MockTransport."""

import asyncio
import contextlib
import json
import pathlib

import httpx
import pytest

from jbrain import host_metrics
from jbrain.llm import local_catalog, local_gateway, local_weights
from jbrain.llm.local_gateway import (
    LocalGatewayClient,
    LocalGatewayError,
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
    # Unlike the best-effort narration reads, tail_logs surfaces the failure — the operator
    # asked for the logs, so an empty success would mislead.
    with pytest.raises(LocalGatewayError):
        await _client(lambda r: httpx.Response(503)).tail_logs()


async def test_the_sweeper_fires_on_cache_GROWTH_before_the_interval_elapses(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Size is the trigger that bounds the transient.

    MEASURED: the control load put ~50 GiB of page cache down in ~35 s — about 1.5 GiB/s. An
    interval cheap enough to poll at leaves GB on the floor between ticks, so growth has to be
    able to fire the sweep on its own, well before the clock would."""
    dropped: list[str] = []
    monkeypatch.setattr(
        local_weights, "drop_weights_page_cache", lambda _d, mid: dropped.append(mid) or 1.0
    )
    # Cache jumps a full GiB immediately; the 2 s interval is nowhere near elapsed.
    readings = iter([0.0, 0.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0])
    monkeypatch.setattr(host_metrics, "read_page_cache_gb", lambda: next(readings, 9.0))
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        models_dir=str(tmp_path),
    )
    task = asyncio.create_task(gw._sweep_page_cache_during_load(model))
    await asyncio.sleep(0.6)  # ~2 polls at 0.25 s, far short of the 2 s interval
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert dropped, "a 9 GiB jump did not trigger a sweep before the interval"


async def test_the_size_trigger_measures_growth_since_the_last_DROP_not_the_last_poll(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that matters most in this function, and the one a single big jump misses.

    Real growth arrives in instalments — ~1.5 GiB/s spread over polls a quarter-second apart is
    under 0.4 GiB each — so the threshold is only ever reached by ACCUMULATING. Compare against
    the previous POLL instead of the previous DROP and no single step ever reaches a GiB: the
    size trigger silently stops firing, the sweep quietly demotes to its 2 s interval, and the
    transient it exists to bound grows by ~3x. Nothing else here would notice."""
    dropped: list[str] = []
    monkeypatch.setattr(
        local_weights, "drop_weights_page_cache", lambda _d, mid: dropped.append(mid) or 1.0
    )
    # 0.4 GiB per poll: never a GiB in one step, past a GiB by the third.
    readings = iter([0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8])
    monkeypatch.setattr(host_metrics, "read_page_cache_gb", lambda: next(readings, 3.0))
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        models_dir=str(tmp_path),
    )
    task = asyncio.create_task(gw._sweep_page_cache_during_load(model))
    await asyncio.sleep(1.1)  # ~4 polls at 0.25 s, still well short of the 2 s interval
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert dropped, "growth accumulating past a GiB across polls did not trigger a sweep"


async def test_the_load_fraction_counts_what_went_PAST_the_cache_not_what_sits_in_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bar's first half. The sweep keeps taking the cache back out, so the level is flat
    (MEASURED: 1.9 GiB before a 69 GiB load and 1.9 GiB after) while the bytes that passed
    through it are the whole read. Only a running total sees that."""
    monkeypatch.setattr(local_weights, "drop_weights_page_cache", lambda _d, _m: 1.0)
    # Grow, get swept back to zero, grow again — the real sawtooth.
    readings = iter([0.0, 0.6, 1.2, 0.0, 0.6, 1.2, 0.0, 0.6, 1.2])
    monkeypatch.setattr(host_metrics, "read_page_cache_gb", lambda: next(readings, 1.2))
    published: list[float] = []

    async def on_progress(value: float) -> None:
        published.append(value)

    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        models_dir=str(tmp_path),
    )
    task = asyncio.create_task(
        gw._sweep_page_cache_during_load(model, weights_gb=10.0, on_progress=on_progress)
    )
    await asyncio.sleep(1.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert published, "a load that read GB published no fraction"
    # Monotonic across the drops: a sawtooth in the LEVEL must not put a sawtooth in the bar.
    assert published == sorted(published)
    # And scaled into the weights phase, never past it — the warm-up owns the rest.
    assert max(published) <= 0.4 + 1e-9


async def test_the_load_fraction_is_absent_when_the_catalog_cannot_size_the_model(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No weight size is no denominator. Silence beats a bar climbing against a guess.
    monkeypatch.setattr(local_weights, "drop_weights_page_cache", lambda _d, _m: 1.0)
    readings = iter([0.0, 2.0, 4.0, 6.0])
    monkeypatch.setattr(host_metrics, "read_page_cache_gb", lambda: next(readings, 6.0))
    published: list[float] = []

    async def on_progress(value: float) -> None:
        published.append(value)

    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        models_dir=str(tmp_path),
    )
    task = asyncio.create_task(
        gw._sweep_page_cache_during_load(model, weights_gb=0.0, on_progress=on_progress)
    )
    await asyncio.sleep(0.6)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert published == []


async def test_the_sweeper_is_a_no_op_without_a_weights_mount() -> None:
    # No mount means nothing to fadvise; it must return rather than spin a pointless loop.
    gw = LocalGatewayClient(
        "http://gw:8080/v1", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    await asyncio.wait_for(gw._sweep_page_cache_during_load(model), timeout=1.0)


async def test_a_model_that_appears_without_us_gets_its_cache_dropped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """llama-swap loads on REQUEST, so most loads never call `load()`.

    MEASURED: during one sweep the app's own turns swapped gpt-oss-120b in repeatedly and
    `Cached` went 2.36 -> 47.83 GiB with `MemFree` at 8.07 on a 121 GiB box — the exact
    double-residency that livelocked this host — with no `weights_cache_*` line, because no
    load we knew about had happened. `running()` is the one poll that sees those arrivals."""
    dropped: list[str] = []
    monkeypatch.setattr(
        local_weights, "drop_weights_page_cache", lambda _d, mid: dropped.append(mid) or 1.0
    )
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"running": [{"model": model.served_model}]})
        ),
        models_dir=str(tmp_path),
    )
    assert await gw.running() == {model.served_model}
    assert dropped == [model.id], "a request-driven load left its weights in the page cache"
    # Only on the TRANSITION: a steady poll must not re-sweep every tick.
    await gw.running()
    assert dropped == [model.id]


async def test_a_model_we_loaded_ourselves_is_not_dropped_twice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load()` already dropped it; a poll that dropped it again would be pure waste."""
    dropped: list[str] = []
    monkeypatch.setattr(
        local_weights, "drop_weights_page_cache", lambda _d, mid: dropped.append(mid) or 1.0
    )
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"running": [{"model": model.served_model}]})
        ),
        models_dir=str(tmp_path),
    )
    gw._loaded_here.add(model.served_model)
    await gw.running()
    assert dropped == []


async def test_drop_page_cache_reports_the_measured_total(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery lever: 29.19 GiB of stale cache is what refused qwen3-coder-next-q8 for
    want of 15.3 GB nothing was using, and no no-terminal path could reclaim it."""
    monkeypatch.setattr(local_weights, "drop_weights_page_cache", lambda _d, mid: 2.5)
    gw = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        models_dir=str(tmp_path),
    )
    out = gw.drop_page_cache(["gpt-oss-120b", "qwen3.5-0.8b"])
    assert out == {"gpt-oss-120b": 2.5, "qwen3.5-0.8b": 2.5}


async def test_drop_page_cache_is_a_no_op_without_a_weights_mount() -> None:
    # A container without the mount must not invent a reclaim it cannot perform.
    gw = LocalGatewayClient(
        "http://gw:8080/v1", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    assert gw.drop_page_cache() == {}


async def test_tail_upstream_logs_reads_the_replay_burst_off_the_stream() -> None:
    """llama-server's own output IS reachable — via the history `/logs/stream/*` replays.

    The route `tail_logs` cannot serve. The sample is real box output rather than the
    `model buffer size` line an earlier draft used: that line does not appear on this
    build (see the api.debug route's docstring), and a fixture implying otherwise would
    reinstate the false belief this whole series of commits exists to clear out."""
    seen: dict[str, str] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, text="0.02.36 I slot launch_slot_: id  0 | task 0\n")

    out = await _client(handle).tail_upstream_logs()
    assert "launch_slot_" in out
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


async def test_a_config_reload_that_kills_a_bystander_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewriting llama-swap.yaml makes the gateway reload, and its reload kills EVERY running
    llama-server — not just the model whose setting changed. That eviction is unavoidable, but
    it must not be SILENT: the kill happens inside llama-swap, which writes no `box_events` row,
    and that silence is why the owner reported several times that staging a model unloaded
    gpt-oss-120b and was several times told it was a display artifact.
    """
    recorded: list[tuple[str, str, str | None]] = []

    async def _record(kind: str, subject: str, *, detail: str | None = None, **_: object) -> None:
        recorded.append((kind, subject, detail))

    monkeypatch.setattr(local_gateway.box_events, "record", _record)
    resident = {"gpt-oss-120b"}  # a bystander, resident before the edited model is loaded

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            return httpx.Response(200, json=sorted(resident))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async def regen() -> None:
        resident.clear()  # what llama-swap's reload() -> old.Shutdown() actually does

    client = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(handle),
        config_regen=regen,
    )
    await client.load("qwen3-vl-30b-a3b")
    evictions = [(subject, detail) for kind, subject, detail in recorded if kind == "model_unload"]
    assert [s for s, _ in evictions] == ["gpt-oss-120b"], (
        f"the bystander's death went unrecorded: {recorded}"
    )
    # The reason has to NAME the load that caused it, by CATALOG id (what the operator sees in
    # the PWA), or the vitals row is as mysterious as the silence it replaced.
    assert evictions[0][1] == "the gateway reloaded to apply changed settings for qwen3-vl-30b"


async def test_a_config_regen_that_changes_nothing_records_no_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case: nearly every load re-stamps an identical config, no reload fires, and
    the resident set is untouched. Narrating a phantom eviction there would be worse than the
    silence — it would teach the operator to ignore the row that matters."""
    recorded: list[str] = []

    async def _record(kind: str, subject: str, **_: object) -> None:
        recorded.append(f"{kind}:{subject}")

    monkeypatch.setattr(local_gateway.box_events, "record", _record)

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            return httpx.Response(200, json=["gpt-oss-120b"])
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async def regen() -> None:
        return None  # content compare said the file already matches — nothing written

    client = LocalGatewayClient(
        "http://gw:8080/v1",
        transport=httpx.MockTransport(handle),
        config_regen=regen,
    )
    await client.load("qwen3-vl-30b-a3b")
    assert not [r for r in recorded if r.startswith("model_unload")], recorded
