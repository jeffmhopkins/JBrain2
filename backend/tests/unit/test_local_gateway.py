"""The llama-swap admin client: tolerant /running parsing, best-effort failure
on running(), and a surfaced error on unload(). All via httpx.MockTransport."""

import asyncio
import contextlib
import json
import pathlib

import httpx
import pytest

from jbrain import box_events, host_metrics
from jbrain.llm import gpu_guard, local_catalog, local_gateway, local_weights
from jbrain.llm.admission import Decision, Layer, Outcome, Phase
from jbrain.llm.ledger import Charge
from jbrain.llm.local_gateway import (
    LocalGatewayClient,
    LocalGatewayError,
    _parse_running,
)


async def _resolved(value: set[str]) -> set[str]:
    """`running()` as a coroutine, for the tests that stub residency."""
    return value


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
    # `/logs` and `/running` are bookkeeping, not the load: the upstream capture and the
    # stop-settle read (`_settle_a_stopping_model`, which keeps a load from relaunching a
    # model llama-swap is still stopping). What this pins is the ORDER of the two calls that
    # are the load.
    infra = ("/logs", "/running")
    assert [c for c in seen if not c[1].startswith(infra)] == [
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
    assert max(published) <= local_gateway._WEIGHTS_SHARE + 1e-9


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


async def test_a_second_load_of_a_model_already_loading_joins_it_rather_than_racing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED on the box 2026-08-21: a gpt-oss-120b load was 48 s in, holding ~29 GB it had
    already committed, when a deferred workflow retried it. The device guard refused the
    second one — correct arithmetic, and a false alarm that put a red "failed to load" row in
    front of the owner for a load that succeeded a minute later.

    The second caller waits, then takes the first one's result: the model is resident, so
    there is nothing left to do and no second load to guard."""
    loads: list[str] = []
    resident: set[str] = set()
    release = asyncio.Event()

    async def slow_load(self: object, served_model: str, **kw: object) -> None:
        loads.append(served_model)
        await release.wait()
        resident.add(served_model)

    monkeypatch.setattr(LocalGatewayClient, "_load_and_warm", slow_load)
    monkeypatch.setattr(LocalGatewayClient, "running", lambda self: _resolved(set(resident)))
    gw = _client(lambda r: httpx.Response(200, json={}))

    first = asyncio.create_task(gw.load("gpt-oss-120b"))
    await asyncio.sleep(0.05)  # let it take the lock
    second = asyncio.create_task(gw.load("gpt-oss-120b"))
    await asyncio.sleep(0.05)  # and let it queue behind

    release.set()
    await asyncio.gather(first, second)

    assert loads == ["gpt-oss-120b"], "the second caller started a duplicate load"


async def test_a_second_load_still_runs_when_the_first_one_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Joining is only right when there is something to join. A load that failed leaves the
    model absent, and the caller that waited for it wanted it LOADED — so that one is a
    genuine retry, not a duplicate."""
    loads: list[str] = []

    async def failing_load(self: object, served_model: str, **kw: object) -> None:
        loads.append(served_model)
        raise LocalGatewayError("no room")

    monkeypatch.setattr(LocalGatewayClient, "_load_and_warm", failing_load)
    monkeypatch.setattr(LocalGatewayClient, "running", lambda self: _resolved(set()))
    gw = _client(lambda r: httpx.Response(200, json={}))

    for _ in range(2):
        with contextlib.suppress(LocalGatewayError):
            await gw.load("gpt-oss-120b")

    assert loads == ["gpt-oss-120b", "gpt-oss-120b"]


async def test_the_warm_completes_the_bar_however_the_warm_got_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one number on this path that is not an estimate.

    MEASURED, two loads on 2026-08-21: with a saved KV slot restored (~90 ms) the warm
    prefill never runs and the bar stopped at 0.384; without one the prefill ran but the
    token estimate over-predicts by about a third, so it stopped at 0.841. Reaching the end
    of `_warm` means the warm is done whichever way it went, so the bar is full."""
    published: list[float] = []

    async def capture(value: float) -> None:
        published.append(value)

    monkeypatch.setattr(box_events, "progress", capture)
    gw = _client(lambda r: httpx.Response(200, json={"choices": []}))
    await gw._warm("gpt-oss-120b", system="you are jerv", tools=[])

    assert published and published[-1] == 1.0


async def test_a_failed_warm_still_completes_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    # The model is resident and serving either way; a warm-up failure only means the first
    # real turn pays a cost. Leaving the bar short would be the worse lie.
    published: list[float] = []

    async def capture(value: float) -> None:
        published.append(value)

    monkeypatch.setattr(box_events, "progress", capture)
    gw = _client(lambda r: httpx.Response(500, json={}))
    await gw._warm("gpt-oss-120b", system="you are jerv", tools=[])

    assert published and published[-1] == 1.0


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


async def test_a_casualty_still_being_killed_is_recorded_as_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model llama-swap is four seconds into killing has NOT survived the reload.

    `/running` filters only `stopped` and `shutdown`, so a dying server is still in that list —
    and subtracting it as a survivor made this narration report nothing in exactly the case it
    was written for. `regen_gateway_config`'s wait is a fixed 4 s sleep (llama-swap exposes no
    reload-done signal to poll), which is nowhere near an 85 GB teardown, so this is the common
    shape of a reload casualty rather than a corner of it."""
    recorded: list[tuple[str, str]] = []

    async def _record(kind: str, subject: str, **_: object) -> None:
        recorded.append((kind, subject))

    monkeypatch.setattr(local_gateway.box_events, "record", _record)
    states = {"gpt-oss-120b": "ready", "qwen3.5-4b": "ready"}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            return httpx.Response(
                200, json=[{"model": n, "state": st} for n, st in sorted(states.items())]
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async def regen() -> None:
        # What four seconds after llama-swap's reload actually looks like: the big one is still
        # being torn down and is still listed; the small one is already gone.
        states["gpt-oss-120b"] = "stopping"
        del states["qwen3.5-4b"]

    client = LocalGatewayClient(
        "http://gw:8080/v1", transport=httpx.MockTransport(handle), config_regen=regen
    )
    await client.load("qwen3-vl-30b-a3b")
    killed = sorted(subject for kind, subject in recorded if kind == "model_unload")
    assert killed == ["gpt-oss-120b", "qwen3.5-4b"], (
        f"a model still being killed was counted as a survivor: {recorded}"
    )


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


async def test_two_different_models_never_load_at_the_same_time() -> None:
    """The runaway watchdog anchors its ceiling to a GTT baseline sampled when a load starts,
    which only means anything if nothing else is still allocating.

    MEASURED 2026-08-21: gpt-oss-120b was 30 s into a reload — GTT 36.4 GB on its way to a
    measured 69.24 — when a staged qwen3.8-27b-abliterated sampled that 36.4 as its OWN
    baseline and set a ceiling of 36.4 + 24.1x1.75 = 78.6. GTT then reached 79.9, which was
    gpt-oss finishing plus abliterated starting, and the guard blamed all of it on abliterated:
    "device memory ran away ... for a model predicted at 24.1 GB". Nothing ran away.

    The per-model lock does not cover this — the two models are different, so both proceed.
    A false abort unloads a healthy model AND strands the weights it had read in the page
    cache, which `read_memory_gb` counts as used, so the next load starts with less headroom
    and is likelier to abort in turn."""
    inflight = 0
    overlapped = False

    async def slow_load(self: object, served: str, **kw: object) -> None:
        nonlocal inflight, overlapped
        inflight += 1
        overlapped = overlapped or inflight > 1
        await asyncio.sleep(0.05)
        inflight -= 1

    gw = _client(lambda r: httpx.Response(200, json={}))
    # Stub the work itself: this test is about the gate around it, not the load.
    gw._load_and_warm = slow_load.__get__(gw)  # type: ignore[attr-defined,method-assign]
    await asyncio.gather(gw.load("gpt-oss-120b"), gw.load("qwen3.8-27b-abliterated"))
    assert not overlapped, "two different models loaded concurrently — the guard's baseline moves"


async def test_a_queued_load_says_what_it_is_waiting_for() -> None:
    """A gate that blocks silently is a hung Load button. The owner drives this box entirely
    through the PWA (CLAUDE.md #10), so a wait of two or three minutes behind a 120B has to
    reach the vitals surface as a QUEUED state naming the model ahead of it — not as nothing."""
    recorded: list[tuple[str, str | None]] = []

    async def fake_record(kind: object, model: str, *, detail: str | None = None, **kw: object):
        recorded.append((model, detail))

    async def slow_load(self: object, served: str, **kw: object) -> None:
        await asyncio.sleep(0.05)

    gw = _client(lambda r: httpx.Response(200, json={}))
    gw._load_and_warm = slow_load.__get__(gw)  # type: ignore[attr-defined,method-assign]
    original = box_events.record
    box_events.record = fake_record  # type: ignore[assignment]
    try:
        await asyncio.gather(gw.load("gpt-oss-120b"), gw.load("qwen3.8-27b-abliterated"))
    finally:
        box_events.record = original  # type: ignore[assignment]

    queued = [(m, d) for m, d in recorded if d and d.startswith("queued")]
    assert queued, f"a load waited its turn without saying so: {recorded}"
    model, detail = queued[0]
    # It names the model ahead, so the screen reads "waiting for gpt-oss-120b", not "waiting".
    assert "gpt-oss-120b" in detail or "qwen3.8-27b-abliterated" in detail, detail


async def test_a_guarded_load_does_not_report_itself_as_unannounced(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unannounced_load` is the box's only signal that something loaded a model outside the
    residency budget and the GPU guard — the documented host-freeze path. It reported the
    client's OWN guarded loads.

    `load()` claims the model before the weights are read, but the prune at the end of
    `_drop_cache_for_unannounced` ran while it was still not resident and dropped the claim,
    so the warm-up's own `/slots` poll saw it arrive un-owned. MEASURED 2026-08-21 on the box:
    `load_cache_swept qwen3.8-27b-q4` at 21:41:14.804 then `unannounced_load qwen3.8-27b-q4`
    at 21:41:17.873 — same client, three seconds apart, one load. A false alarm on this
    surface is expensive: it sent a live investigation after a bypass that never happened."""
    events: list[str] = []
    monkeypatch.setattr(
        local_gateway.log,
        "info",
        lambda ev, **kw: events.append(ev),  # type: ignore[arg-type]
    )
    gw = _client(lambda r: httpx.Response(200, json={}))
    gw._models_dir = str(tmp_path)  # type: ignore[attr-defined]

    # A load is in flight: claimed, not yet resident — the state the prune used to destroy.
    gw._loading.add("gpt-oss-120b")  # type: ignore[attr-defined]
    gw._drop_cache_for_unannounced(set())  # a poll while the weights are still being read
    # ...and now it arrives, on the warm-up's own poll.
    gw._drop_cache_for_unannounced({"gpt-oss-120b"})
    assert "local_gateway.unannounced_load" not in events, "a guarded load reported itself"


async def test_a_genuinely_unannounced_load_is_still_reported(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The veto must not blind the signal. A model this client never claimed is exactly what
    the event exists for, and it still fires — annotated, so a cross-process sighting is
    legible as one rather than read as an unguarded load."""
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_gateway.log,
        "info",
        lambda ev, **kw: seen.append({"event": ev, **kw}),  # type: ignore[arg-type]
    )
    gw = _client(lambda r: httpx.Response(200, json={}))
    gw._models_dir = str(tmp_path)  # type: ignore[attr-defined]
    gw._seen_resident = {"qwen3.5-4b"}  # type: ignore[attr-defined]
    gw._polled = True  # not this client's first poll  # type: ignore[attr-defined]

    gw._drop_cache_for_unannounced({"qwen3.5-4b", "gpt-oss-120b"})
    hits = [e for e in seen if e["event"] == "local_gateway.unannounced_load"]
    assert len(hits) == 1 and hits[0]["model"] == "gpt-oss-120b"
    assert hits[0]["first_poll"] is False
    assert hits[0]["client"], "no client identity — a cross-process sighting reads as a bypass"


async def test_a_fresh_clients_first_poll_is_marked_as_such(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new client reports every already-resident model, because `_seen_resident` starts
    empty. MEASURED: the worker logged gpt-oss-120b and qwen3.8-27b-q4 at the same millisecond
    (21:41:23.348/.349) — both loaded by the api minutes earlier. Unavoidable and harmless,
    but it must be DISTINGUISHABLE from a load that appeared while the client was watching."""
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_gateway.log,
        "info",
        lambda ev, **kw: seen.append({"event": ev, **kw}),  # type: ignore[arg-type]
    )
    gw = _client(lambda r: httpx.Response(200, json={}))
    gw._models_dir = str(tmp_path)  # type: ignore[attr-defined]

    gw._drop_cache_for_unannounced({"gpt-oss-120b"})  # the very first poll
    hits = [e for e in seen if e["event"] == "local_gateway.unannounced_load"]
    assert hits and hits[0]["first_poll"] is True


async def test_unload_waits_longer_than_the_default_client_timeout() -> None:
    """llama-swap's unload BLOCKS until the process has stopped, and grants it
    `DEFAULT_UNLOAD_TIMEOUT = 10` s of graceful stop first. The client default is 3 s, so
    unloading a 69 GB model raced its own success: every other slow call on this client widens
    (`max(self._timeout, 180.0)` for the slot ops, `120.0` for the load probe) and this one was
    missed.

    A false unload failure is expensive in four places, all while the unload succeeds
    underneath: a `MODEL_UNLOAD status="failed"` row on the owner's vitals surface; a
    `residency` plan that still counts the victim as resident; `image_gen.render` abandoning
    the rest of its unload loop before a ComfyUI render; and `cli.py`'s pre-update unload
    reporting failure inside `set -e`. It also hands control back while the process is still
    `stopping` — a state `/running` reports as resident, so every caller then reasons from a
    roster that is wrong."""
    seen: list[float | None] = []

    class _Recorder(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout", {}).get("connect"))
            return httpx.Response(200, json={})

    gw = LocalGatewayClient("http://gw:8080/v1", transport=_Recorder(), timeout=3.0)
    await gw.unload("gpt-oss-120b")
    assert seen and seen[0] is not None
    assert seen[0] >= 30.0, f"unload still races llama-swap's 10 s graceful stop: {seen[0]}s"


# --- /running carries a STATE, and a non-ready model is not serving -----------------
# llama-swap lists a model it is STOPPING (only `stopped`/`shutdown` are filtered), so
# every caller reading this as "resident" treats a model on its way out as one that is
# up. These cover the measurement that lands before that behaviour changes.


def test_parse_running_states_keeps_the_state() -> None:
    states = local_gateway._parse_running_states(
        {"running": [{"model": "a", "state": "ready"}, {"model": "b", "state": "stopping"}]}
    )
    assert states == {"a": "ready", "b": "stopping"}


def test_parse_running_states_is_empty_string_when_the_build_reports_none() -> None:
    """An older gateway sends bare names. Unknown must not read as ready."""
    assert local_gateway._parse_running_states(["a", "b"]) == {"a": "", "b": ""}


def test_parse_running_still_returns_names_only() -> None:
    """The set-returning parser is unchanged for every existing caller."""
    payload = {"running": [{"model": "a", "state": "ready"}, {"model": "b", "state": "stopping"}]}
    assert _parse_running(payload) == {"a", "b"}


async def test_running_still_lists_a_stopping_model() -> None:
    """The behaviour this wave MEASURES but does not yet change."""
    client = _client(
        lambda r: httpx.Response(
            200, json={"running": [{"model": "gpt-oss-120b", "state": "stopping"}]}
        )
    )
    assert await client.running() == {"gpt-oss-120b"}


async def test_state_of_reports_the_last_seen_state() -> None:
    client = _client(
        lambda r: httpx.Response(
            200,
            json={
                "running": [{"model": "a", "state": "ready"}, {"model": "b", "state": "starting"}]
            },
        )
    )
    await client.running()
    assert client.state_of("a") == "ready"
    assert client.state_of("b") == "starting"


async def test_state_of_is_unknown_for_a_model_that_is_not_listed() -> None:
    """`""` must mean "we do not know", never "ready" — a caller that reads an absent
    model as ready would skip admission for a model that is not there at all."""
    client = _client(lambda r: httpx.Response(200, json={"running": [{"model": "a"}]}))
    await client.running()
    assert client.state_of("nope") == ""


async def test_not_ready_is_logged_once_per_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """`running()` is polled on every tick in both processes, so an unconditional line
    would bury the transition it exists to show."""
    lines: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(local_gateway.log, "info", lambda ev, **kw: lines.append((ev, kw)))
    client = _client(
        lambda r: httpx.Response(200, json={"running": [{"model": "a", "state": "stopping"}]})
    )
    await client.running()
    await client.running()
    hits = [kw for ev, kw in lines if ev == "local_gateway.not_ready_in_running"]
    assert len(hits) == 1
    assert hits[0]["models"] == ["a"]
    assert hits[0]["states"] == ["stopping"]


async def test_a_ready_only_roster_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(local_gateway.log, "info", lambda ev, **kw: lines.append(ev))
    client = _client(
        lambda r: httpx.Response(200, json={"running": [{"model": "a", "state": "ready"}]})
    )
    await client.running()
    assert "local_gateway.not_ready_in_running" not in lines


async def test_the_line_returns_when_a_model_goes_not_ready_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log-on-change must not mean log-once-ever: a model that settles and later stops
    again is a new transition and has to narrate."""
    lines: list[str] = []
    monkeypatch.setattr(local_gateway.log, "info", lambda ev, **kw: lines.append(ev))
    states = iter(["stopping", "ready", "stopping"])
    client = _client(
        lambda r: httpx.Response(200, json={"running": [{"model": "a", "state": next(states)}]})
    )
    for _ in range(3):
        await client.running()
    assert lines.count("local_gateway.not_ready_in_running") == 2


async def test_an_idle_box_does_not_disguise_a_bypass_as_a_first_poll(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`first_poll` used to be `not self._seen_resident`, which is ALSO true of an idle box.
    So an api restart with nothing resident, followed by a request-driven llama-swap load,
    stamped the arrival `first_poll=true` — and DEBUG_ACCESS.md says to treat a line as a real
    bypass only when `first_poll=false`. The one case the event exists to catch was the case
    it told the reader to dismiss."""
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_gateway.log,
        "info",
        lambda ev, **kw: seen.append({"event": ev, **kw}),  # type: ignore[arg-type]
    )
    gw = _client(lambda r: httpx.Response(200, json={}))
    gw._models_dir = str(tmp_path)  # type: ignore[attr-defined]

    gw._drop_cache_for_unannounced(set())  # first poll of an EMPTY box — nothing to report
    assert not [e for e in seen if e["event"] == "local_gateway.unannounced_load"]

    gw._drop_cache_for_unannounced({"gpt-oss-120b"})  # now something loads behind our back
    hits = [e for e in seen if e["event"] == "local_gateway.unannounced_load"]
    assert len(hits) == 1 and hits[0]["model"] == "gpt-oss-120b"
    assert hits[0]["first_poll"] is False, "a real bypass was labelled a first-poll artifact"


async def test_a_short_load_does_not_report_itself_after_the_claim_is_pruned(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim must survive the poll that `_load_and_warm` makes DURING the load.

    Sequence, all one client: `load()` claims the model in `_loaded_here` before loading, then
    `_load_and_warm` calls `running()` while it is still arriving. That poll pruned the claim
    (`&= resident`, and it is not resident yet). `_loading` hid the damage at the time, but it
    is released in `load()`'s `finally`, so the NEXT poll saw the model with no claim in either
    set and logged `unannounced_load … first_poll=false` — a guarded load accusing itself, in
    the one shape DEBUG_ACCESS.md tells the operator to trust as a real bypass."""
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_gateway.log,
        "info",
        lambda ev, **kw: seen.append({"event": ev, **kw}),  # type: ignore[arg-type]
    )
    gw = _client(lambda r: httpx.Response(200, json={}))
    gw._models_dir = str(tmp_path)  # type: ignore[attr-defined]
    # not a first poll, so nothing here is excused as a fresh-client artifact
    gw._polled = True  # type: ignore[attr-defined]

    gw._loaded_here.add(
        "gpt-oss-120b"
    )  # claimed before the load runs (_load_and_warm)  # type: ignore[attr-defined]
    gw._loading.add("gpt-oss-120b")  # and in flight  # type: ignore[attr-defined]
    gw._drop_cache_for_unannounced(set())  # the poll _load_and_warm itself makes: not resident
    gw._loading.discard(
        "gpt-oss-120b"
    )  # load() finally: the in-flight claim is released  # type: ignore[attr-defined]

    gw._drop_cache_for_unannounced({"gpt-oss-120b"})  # now it arrives, on a later poll
    hits = [e for e in seen if e["event"] == "local_gateway.unannounced_load"]
    assert not hits, f"a guarded load reported itself as unannounced: {hits}"


# Deliberately says nothing a refusal would plausibly say. A reason built from the served model
# ("... refused") let production swap in a canned string and still satisfy a substring assertion,
# which is how the owner's 409 body and the box-event narration could quietly lose their numbers.
_REASON = "tokamak overpressure"


class _RefusingLedger:
    """A ledger that refuses, which the real one only does once `shadow=False`.

    Hand-rolled rather than a `ReservationLedger` against a database: the branch under test is
    the gateway's REACTION to a refusal, and a fake keeps that reaction the only variable."""

    def __init__(self, outcome: Outcome) -> None:
        self._outcome = outcome

    async def charge(self, served_model: str, *, host_gb: float, device_gb: float) -> Charge:
        return Charge(Decision(self._outcome, _REASON, Layer.HOST), None)


def _catalog_model() -> local_catalog.LocalModel:
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    return model


@pytest.mark.parametrize(
    ("outcome", "permanent"),
    [(Outcome.INFEASIBLE, True), (Outcome.DEFERRED, False)],
)
async def test_a_refused_charge_carries_whether_waiting_could_ever_help(
    outcome: Outcome, permanent: bool
) -> None:
    """The one branch that goes live at the `shadow=False` flip, and the reason it needed a
    flag: the worker defers on a GpuBudgetError, so an INFEASIBLE that arrived indistinguishable
    from a DEFERRED would be re-attempted against a condition that cannot arrive."""
    gw = _client(lambda r: httpx.Response(200, json={"running": []}))
    gw._reservations = _RefusingLedger(outcome)  # type: ignore[assignment]
    model = _catalog_model()

    with pytest.raises(gpu_guard.GpuBudgetError) as exc:
        async with gw._reservation(model.served_model, model, window=4096, slots=1):
            pytest.fail("the body must not run when the charge was refused")

    assert exc.value.permanent is permanent
    # EQUALITY, not a substring: the decision's reason is the only thing carrying the sizes the
    # owner needs ("needs 200.0 GB … this box has 118.0 GB to give"), and it is what reaches the
    # 409 body and the box event. A canned message would pass any looser check.
    assert str(exc.value) == _REASON


async def test_an_admitted_charge_runs_the_body_and_does_not_raise() -> None:
    """The companion assertion: the refusal path above must not have made every load raise."""
    admitted: list[str] = []

    charged: list[tuple[str, float, float]] = []

    class _AdmittingLedger:
        async def charge(self, served_model: str, *, host_gb: float, device_gb: float) -> Charge:
            charged.append((served_model, host_gb, device_gb))
            return Charge(Decision(Outcome.ADMIT, "fits", None), "instance-1")

        async def advance(self, instance_id: str, phase: Phase) -> None:
            admitted.append(f"{instance_id}:{phase.value}")

    gw = _client(lambda r: httpx.Response(200, json={"running": []}))
    gw._reservations = _AdmittingLedger()  # type: ignore[assignment]
    # NOT gpt-oss: its host and device declarations are numerically EQUAL (no checkpoint, no
    # CACHE_RAM), so swapping the two columns is undetectable against it. This model carries a
    # host-only checkpoint term, which makes the two numbers actually discriminate.
    model = local_catalog.get("qwen3.8-27b-q4")
    assert model is not None
    expected = local_catalog.declared_gb(model, 32768, slots=1)

    async with gw._reservation(model.served_model, model, window=32768, slots=1):
        pass

    assert admitted == ["instance-1:starting", "instance-1:resident"]
    # The declaration the ledger is charged is the whole point of the reservation: the columns
    # must not be swapped, and the window/slots must be the ones the load will actually use.
    assert charged == [(model.served_model, expected[0], expected[1])]
    assert expected[0] > expected[1], "this model must discriminate host from device"


async def test_a_model_absent_from_the_catalog_is_not_charged_at_all() -> None:
    """`_load_and_warm` resolves `model` from the catalog and passes None for anything it does
    not know, with `window, slots = 0, 1`. Without that half of the guard, `declared_gb` is
    called on None and every such load dies with an AttributeError once a ledger is wired —
    a declaration is the one thing that cannot be guessed."""
    touched = False

    class _ExplodingLedger:
        async def charge(self, served_model: str, *, host_gb: float, device_gb: float) -> Charge:
            nonlocal touched
            touched = True
            raise AssertionError("an uncatalogued model must never reach the ledger")

    gw = _client(lambda r: httpx.Response(200, json={"running": []}))
    gw._reservations = _ExplodingLedger()  # type: ignore[assignment]

    async with gw._reservation("something-llama-swap-has-that-we-do-not", None, window=0, slots=1):
        pass

    assert touched is False


async def test_slot_save_and_restore_speak_llama_servers_exact_dialect() -> None:
    """The kv-prefix store's verification hangs off these two calls, so the wire shape is
    the contract: POST through llama-swap's upstream passthrough, the action as a query
    param, the filename in the JSON body — and a non-resident model refused before any of
    it, because reaching the passthrough makes llama-swap LOAD outside residency."""
    seen: list[tuple[str, str, bytes]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": "gpt-oss-120b"}]})
        seen.append((req.method, str(req.url), req.content))
        if "action=save" in str(req.url):
            return httpx.Response(200, json={"id_slot": 1, "n_saved": 28757})
        return httpx.Response(200, json={"id_slot": 0, "n_restored": 28757})

    gw = _client(handle)
    saved = await gw.save_slot("gpt-oss-120b", 1, "abc.kvslot")
    restored = await gw.restore_slot("gpt-oss-120b", 0, "abc.kvslot")
    assert saved["n_saved"] == 28757 and restored["n_restored"] == 28757
    assert seen[0][0] == "POST" and "/upstream/gpt-oss-120b/slots/1?action=save" in seen[0][1]
    assert b'"filename"' in seen[0][2] and b"abc.kvslot" in seen[0][2]
    assert "/upstream/gpt-oss-120b/slots/0?action=restore" in seen[1][1]

    with pytest.raises(LocalGatewayError):
        await gw.save_slot("not-resident-model", 0, "abc.kvslot")


async def test_load_warms_with_the_turns_reasoning_encoding() -> None:
    # The effort lands in the prompt's LEADING tokens (gpt-oss's harmony template writes a
    # literal "Reasoning: <level>" header), so a warm that omits it primes a prefix no real
    # turn ever sends — a full ~62 s prefill that warmed nothing, observed live 2026-08-23,
    # right past a 497 ms KV restore it then clobbered. The warm must carry exactly what
    # `openai_compat` puts on a routed turn's wire.
    body: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("gpt-oss-120b", warm_system="PERSONA", warm_reasoning_effort="high")
    assert body["reasoning_effort"] == "high"  # harmony family: verbatim


async def test_load_translates_a_hybrids_reasoning_like_a_real_turn() -> None:
    # The Qwen hybrids ignore a top-level reasoning_effort — their template reads the
    # chat_template_kwargs bag. The warm must go through the same per-family translation
    # a routed turn does, or the rendered prefixes diverge exactly as above.
    body: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("qwen3.8-27b-q4", warm_system="PERSONA", warm_reasoning_effort="low")
    kwargs = body.get("chat_template_kwargs")
    assert isinstance(kwargs, dict) and kwargs["enable_thinking"] is True
    assert "reasoning_effort" not in body, "a hybrid must not carry the top-level field"


async def test_before_warm_runs_before_the_warm_request() -> None:
    # The hook is the KV restore: it must land BEFORE the warm's completion request, or on
    # a single-slot server the warm's prefill clobbers the freshly restored cache instead
    # of cache-hitting it.
    order: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            order.append("warm_request")
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async def restore() -> bool:
        order.append("restore")
        return True

    await _client(handle).load("gpt-oss-120b", warm_system="P", before_warm=restore)
    assert order == ["restore", "warm_request"]


async def test_a_failing_before_warm_never_fails_the_load() -> None:
    async def broken() -> bool:
        raise RuntimeError("disk layer down")

    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    await _client(handle).load("gpt-oss-120b", warm_system="P", before_warm=broken)


async def test_after_warm_receives_the_warms_prompt_size() -> None:
    """The save hook fires exactly once, after a warm that returned, with the usage the
    server reported — the caller keys its slot-save verification on that number."""
    received: list[int] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": ""}}],
                    "usage": {"prompt_tokens": 37142, "completion_tokens": 1},
                },
            )
        return httpx.Response(200, json={})

    async def _save(tokens: int) -> None:
        received.append(tokens)

    await _client(handle).load("qwen3-vl-30b-a3b", after_warm=_save)
    assert received == [37142]


async def test_after_warm_is_skipped_when_the_warm_fails() -> None:
    """A warm that never returned proves nothing about the slot — saving it would be the
    v1 mistake (persisting whatever happened to be there)."""
    received: list[int] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(500)
        return httpx.Response(200, json={})

    async def _save(tokens: int) -> None:
        received.append(tokens)

    await _client(handle).load("qwen3-vl-30b-a3b", after_warm=_save)
    assert received == []


async def test_a_failing_after_warm_never_fails_the_load() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": ""}}],
                    "usage": {"prompt_tokens": 20000},
                },
            )
        return httpx.Response(200, json={})

    async def _save(tokens: int) -> None:
        raise RuntimeError("disk full")

    await _client(handle).load("qwen3-vl-30b-a3b", after_warm=_save)  # must not raise
