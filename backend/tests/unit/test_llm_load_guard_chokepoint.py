"""The device-memory guard must be unbypassable.

Three host freezes — each needing a power cycle — came through a `gateway.load()` that never
reached the residency coordinator's guard: the settings screen's deliberate load, the debug
console's, and the coordinator's own end-of-turn restore. The guard now lives inside
`LocalGatewayClient.load`, the single chokepoint. These tests hold that property in place:
the first is a STRUCTURAL check that fails CI the moment a new unguarded gateway is built,
because a reviewer noticing is what already failed three times.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace

import httpx
import pytest

from jbrain.llm import gpu_guard, local_catalog, local_gateway
from jbrain.llm.local_gateway import LocalGatewayClient

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"


@pytest.fixture(autouse=True)
def _ample_host_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-flight now bounds itself by the host's FREE pages as well as the GTT ceiling
    (`gpu_guard.refuse_if_no_device_room`), because on this box `gtt_total` is essentially all
    of RAM and therefore counts page cache as room the GPU could take — the reading that
    admitted the load that livelocked the host on 2026-08-19.

    That makes the guard read the REAL machine, so without this every case below would depend
    on how much memory the CI runner happens to have free. Pin it wide open by default, so
    these keep testing the GTT leg they were written for; the host leg has its own tests."""
    monkeypatch.setattr(
        "jbrain.llm.gpu_guard.read_memory_gb", lambda path="/proc/meminfo": (1024.0, 0.0)
    )


def _gateway_constructions() -> list[tuple[pathlib.Path, ast.Call]]:
    found: list[tuple[pathlib.Path, ast.Call]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "LocalGatewayClient"
            ):
                found.append((path, node))
    return found


def test_every_local_gateway_in_src_is_built_with_a_gpu_probe() -> None:
    """No production code path may construct a local-model gateway without the probe.

    Whisper gateways are exempt: they only ever unload. Anything pointed at
    `local_llm_url` can load, and a load with no probe is the exact hole this closes."""
    constructions = _gateway_constructions()
    assert constructions, "the AST scan found no gateways at all — the check has rotted"
    unguarded = []
    for path, call in constructions:
        first = call.args[0] if call.args else None
        target = ast.unparse(first) if first is not None else ""
        if "whisper" in target:
            continue
        if not any(kw.arg == "gpu_probe" for kw in call.keywords):
            unguarded.append(f"{path.relative_to(_SRC)}:{call.lineno}")
    assert not unguarded, (
        "LocalGatewayClient built without gpu_probe= at: "
        + ", ".join(unguarded)
        + " — every load path must pass the device-memory guard (jbrain.llm.gpu_guard)."
    )


class _StubProbe:
    def __init__(self, *samples: gpu_guard.GpuMem) -> None:
        self._samples = list(samples)
        self.calls = 0

    async def sample(self) -> gpu_guard.GpuMem | None:
        self.calls += 1
        return self._samples[min(self.calls - 1, len(self._samples) - 1)]


def _mem(used: float, total: float = 124.0) -> gpu_guard.GpuMem:
    return gpu_guard.GpuMem(
        gtt_used_gb=used, gtt_total_gb=total, vram_used_gb=0.0, vram_total_gb=0.0
    )


@pytest.mark.anyio
async def test_load_refuses_when_the_device_pool_is_short() -> None:
    """The pre-flight declines rather than risking the host. Sized off `load_footprint_gb`,
    which is weights + KV + runtime overhead — deliberately NOT the vision peak, because that
    buffer does not exist at load time (see test_the_vision_peak_is_budgeted_as_resident)."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    room = local_catalog.load_footprint_gb(model)
    probe = _StubProbe(_mem(124.0 - room))  # not even the floor left over
    gateway = LocalGatewayClient("http://gw", gpu_probe=probe)
    with pytest.raises(gpu_guard.GpuBudgetError) as exc:
        await gateway.load("gpt-oss-120b")
    assert "refusing to load" in str(exc.value)


def _transport(*, delay_s: float = 0.0, seen: list[str] | None = None) -> httpx.MockTransport:
    """A gateway that answers /health (the load), the warm-up, and the unload."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path.endswith("/health") and delay_s:
            await asyncio.sleep(delay_s)
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_load_without_a_probe_keeps_the_prior_behaviour() -> None:
    """A box that can't measure its device pool (no amdgpu, supervisor down) must still be
    able to serve. Absence of a probe means 'we don't know', never a refusal."""
    gateway = LocalGatewayClient("http://gw", transport=_transport())
    await gateway.load("qwen3.8-27b-q4")  # would be refused outright if a probe were wired


@pytest.mark.anyio
async def test_a_probe_that_cannot_read_the_pool_does_not_block_the_load() -> None:
    """Same rule one level down: a wired probe returning None is still 'we don't know'."""

    class _Blind:
        async def sample(self) -> gpu_guard.GpuMem | None:
            return None

    gateway = LocalGatewayClient("http://gw", transport=_transport(), gpu_probe=_Blind())
    await gateway.load("qwen3.8-27b-q4")


@pytest.mark.anyio
async def test_a_load_that_balloons_mid_flight_is_aborted_and_unloaded() -> None:
    """The leg that matters most, exercised THROUGH `load()`: the pre-flight passed, and the
    model then allocated far past its prediction anyway. An estimate can only be wrong in
    ways already seen, so the watchdog is the only check that protects the FIRST load of
    anything new — and here it must abort without the caller having asked for a watchdog."""
    # Roomy for the pre-flight and the watchdog's baseline, then GTT runs away mid-load.
    probe = _StubProbe(_mem(10.0), _mem(10.0), _mem(118.0))
    seen: list[str] = []
    gateway = LocalGatewayClient(
        "http://gw", transport=_transport(delay_s=5.0, seen=seen), gpu_probe=probe, timeout=30.0
    )
    with pytest.raises(gpu_guard.GpuBudgetError) as exc:
        await gateway.load("qwen3.8-27b-q4")
    assert "aborted" in str(exc.value)
    assert any("/unload/qwen3.8-27b-q4" in path for path in seen), seen


def test_the_vision_peak_is_budgeted_as_resident_not_as_a_load_reservation() -> None:
    """Where the CLIP attention workspace belongs, which the first version of this got
    backwards.

    It is NOT a load-time cost: llama.cpp warms the projector at a capped 46x46 image tokens,
    and the full-resolution workspace only appears on the first real image. It IS persistent:
    `ggml_gallocr_reserve_n_impl` only grows the allocation and it is freed at unload, so a
    smaller later image releases nothing. Hence resident budget, not load reservation.

    The GAP between the two is small because flash attention is on (measured — see
    vision_attn_buffer_gb); what matters is the direction, so this asserts ordering rather
    than a magnitude that would have to move if `-fa` ever came off."""
    vision = local_catalog.get("qwen3.8-27b-q4")
    assert vision is not None
    # The control is this same entry with the projector stripped, which isolates exactly one
    # variable. A different model would vary weights and context window too — gpt-oss, the
    # obvious candidate, has a resident-vs-load gap of its own from KV growth.
    text_only = replace(vision, mmproj_include=None, supports_vision=False)
    assert vision.mmproj_include and not text_only.mmproj_include

    # The peak lands in the RESIDENT figure, which is what the eviction budget consults, and
    # is strictly larger there than in the load reservation.
    at_window = local_catalog.footprint_gb(vision, vision.context_window)
    assert at_window > local_catalog.load_footprint_gb(vision)

    # A text-only entry pays no vision term — but it is not the case that its two figures agree
    # exactly, because context checkpoints are a SECOND resident-but-not-load cost, and for the
    # same reason as the vision peak: they do not exist when the model loads. llama.cpp creates
    # them as context is processed, bounded by `--ctx-checkpoints`, and they then persist. So the
    # eviction budget must carry them and the load reservation must not.
    #
    # (If a measurement ever shows llama.cpp preallocating them at load, this is the assertion
    # that should move — put the term in `load_footprint_gb` too and restore the equality.)
    resident_only = local_catalog.footprint_gb(
        text_only, text_only.context_window
    ) - local_catalog.load_footprint_gb(text_only)
    # Two such costs now, both of the same kind — real while the model SERVES, absent at the
    # moment it loads. Checkpoints llama.cpp creates as context is processed; the in-RAM
    # prompt cache (`--cache-ram`, 8 GiB by default) fills as prompts are answered.
    assert resident_only == pytest.approx(
        text_only.checkpoint_gb * local_catalog.CTX_CHECKPOINTS + local_catalog.CACHE_RAM_GB,
        abs=0.01,
    )
    # An entry with no checkpoint cost of its own DOES still agree exactly — which is what
    # isolates the claim above to the checkpoints rather than to some other drift.
    no_checkpoints = replace(text_only, checkpoint_gb=0.0)
    assert (
        local_catalog.footprint_gb(no_checkpoints, no_checkpoints.context_window)
        == local_catalog.load_footprint_gb(no_checkpoints) + local_catalog.CACHE_RAM_GB
    )


def test_the_vision_workspace_is_the_measured_flash_attention_branch() -> None:
    """Pinned because this was assumed wrong once and cost 16 GiB of phantom reservation on
    every vision entry.

    Measured on the box: loading the vision model moved GTT +26.02 GiB (predicted 25.60 with
    flash attention on, 29.62 with it off), and a full-resolution 2.1 MB image then moved it
    +0.11 GiB. Off, that image would have allocated up to 16 GiB."""
    on = local_catalog.vision_attn_buffer_gb()
    off = local_catalog.vision_attn_buffer_gb(flash_attention=False)
    assert on < 1.0, on  # linear in patches
    assert off > 15.0, off  # quadratic — kept for a build where -fa does not apply
    # The anchor the linear branch is fitted to: 248.10 MiB at the 2116-token warmup.
    assert local_catalog.vision_attn_buffer_gb(2116) == 0.24


def _kv(model: local_catalog.LocalModel) -> float:
    """This model's KV term at its own catalog window — the only part of the footprint the
    cache type moves, so the piece to swap when comparing against an f16-era measurement."""
    return local_catalog._kv_gb(model, model.context_window, 1)


def test_the_mtp_estimate_matches_what_was_measured_on_the_box() -> None:
    """Served TEXT-ONLY, this entry measured 19.50 GiB on the box against 19.45 predicted
    (0.26%). The catalog had said 16.4 — weights and KV alone, with the f16 KV under-counted as
    q4_0 and every runtime term missing. That gap is what made the load guard optimistic about
    the one model the box is meant to run all day, so the text-only arithmetic is pinned here.

    The entry now also ships a projector, which that measurement did not include — so the
    text-only arithmetic is pinned against a stripped copy, and the shipped pair is pinned
    separately below."""
    mtp = local_catalog.get("qwen3.8-27b-q4")
    assert mtp is not None
    assert mtp.is_speculative
    text_only = replace(mtp, mmproj_include=None, supports_vision=False, size_gb=15.9)
    predicted = local_catalog.load_footprint_gb(text_only)
    # That 19.50 GiB was measured AT f16, and this family now serves `-ctk/-ctv q8_0`. The
    # measurement is still a fact about the box, so it is checked against the f16-equivalent
    # arithmetic rather than re-anchored on a q8 figure nothing has weighed — putting the
    # cache back is the only difference between then and now.
    at_f16 = predicted - _kv(text_only) + _kv(text_only) * 8.0 / text_only.kv_gb_per_128k
    assert 19.0 <= at_f16 <= 20.0, (predicted, at_f16)
    # And the shipped number is genuinely lower, which is the whole point of the change.
    assert predicted < at_f16


def test_vision_plus_mtp_is_budgeted_but_has_never_been_loaded() -> None:
    """Vision is enabled on the MTP entry to be verified on an EMPTY box. Memory is ruled out
    as the cause of the two freezes this once produced — the second had ~105 GiB free against a
    ~21 GiB model, and the ~33 GiB mmproj balloon blamed on llama.cpp #27146 does not happen
    here (the q4 twin carries the same projector, loads in 26.02 GiB measured, and a full
    image encode adds 0.11 GiB). What is untested is the PAIR: q4 has no MTP head, so no run
    has put a projector and the MTP head in one process on this box.

    This pins the PREDICTION so a divergence at load shows up as a failing test rather than as
    a frozen host — the failure mode that cost this box three power cycles."""
    mtp = local_catalog.get("qwen3.8-27b-q4")
    assert mtp is not None
    assert mtp.mmproj_include and mtp.supports_vision
    predicted = local_catalog.load_footprint_gb(mtp)
    # 19.50 measured text-only, plus the projector weights and its vision workspace — all of
    # it weighed at f16, so the comparison puts that cache back (see the twin test above).
    at_f16 = predicted - _kv(mtp) + _kv(mtp) * 8.0 / mtp.kv_gb_per_128k
    assert 20.5 <= at_f16 <= 22.0, (predicted, at_f16)
    # Must still co-reside with gpt-oss (69.24 GiB measured) inside the ~124 GiB pool, which is
    # the only reason the pair is worth having at all.
    assert predicted + 69.24 < 124.0


def test_spec_numbers_are_derived_when_the_build_has_no_draft_counters() -> None:
    """The build this box runs exposes NO draft/accept counters, so the speedup has to be
    derived from decode totals: tokens emitted per forward pass. On a bandwidth-bound box that
    ratio IS the speedup, and 1.0 means speculation is doing nothing."""
    from jbrain.llm.local_gateway import parse_spec_counters

    text = "\n".join(
        [
            "# HELP llamacpp:n_decode_total decodes",
            "llamacpp:n_decode_total 163",
            "llamacpp:tokens_predicted_total 400",
            "llamacpp:tokens_predicted_seconds_total 19.2",
            "llamacpp:prompt_tokens_total 8600",
        ]
    )
    got = parse_spec_counters(text)
    assert got["tokens_per_step"] == round(400 / 163, 4)
    assert got["tokens_per_second"] == round(400 / 19.2, 3)


def test_derived_numbers_are_absent_rather_than_divided_by_zero() -> None:
    """A freshly restarted server has zero decodes. Reporting nothing beats reporting a
    fabricated ratio, and must never raise."""
    from jbrain.llm.local_gateway import parse_spec_counters

    got = parse_spec_counters("llamacpp:n_decode_total 0\nllamacpp:tokens_predicted_total 0")
    assert "tokens_per_step" not in got
    assert "tokens_per_second" not in got
    assert parse_spec_counters("") == {}
    assert parse_spec_counters("garbage\nllamacpp:n_decode_total not_a_number") == {}


def test_real_draft_counters_are_still_passed_through_if_a_build_grows_them() -> None:
    """Matched by SUBSTRING, not exact name: llama.cpp renames metrics and this box tracks
    master, so a build that adds them should surface them without a code change."""
    from jbrain.llm.local_gateway import parse_spec_counters

    got = parse_spec_counters(
        "llamacpp:n_draft_total 400\n"
        'llamacpp:n_draft_accepted_total{slot="0"} 260\n'
        "llamacpp:n_decode_total 100\n"
        "llamacpp:tokens_predicted_total 200"
    )
    assert got["accept_rate"] == 0.65
    assert got["tokens_per_step"] == 2.0


@pytest.mark.anyio
async def test_slots_and_metrics_refuse_a_non_resident_model() -> None:
    """Same refusal as props, for the same reason: these read through llama-swap's
    /upstream/ passthrough, which LOADS the model on demand outside the residency budget.
    A read-only diagnostic must never be able to commit device memory — doing exactly that
    froze this host to a power cycle."""
    gateway = LocalGatewayClient("http://gw", transport=_transport())
    for call in (gateway.slots("qwen3.8-27b-q4"), gateway.metrics("qwen3.8-27b-q4")):
        with pytest.raises(Exception) as exc:  # noqa: PT011 — LocalGatewayError
            await call
        assert "not resident" in str(exc.value)


@pytest.mark.anyio
async def test_the_preflight_reserves_for_the_served_window_not_the_catalog_default() -> None:
    """The guard sizes off the `-c` llama-swap will really serve.

    This is the bug that motivated the window parameter. The abliterated 27B ships a 32768
    catalog window and is served at `-c 262144`; with the guard blind to the override it
    reserved 20.29 GB for a load that MEASURED 36.92 GB on the box — 1.8x light, on the one
    code path whose stated job is refusing a load rather than freezing the host. KV is linear
    in the window, so the error grows with every widening the operator makes."""
    model = local_catalog.get("qwen3.8-27b-abliterated")
    assert model is not None
    assert model.context_window < 262144  # the premise: the box serves wider than the catalog

    at_catalog = local_catalog.load_footprint_gb(model)
    at_served = local_catalog.load_footprint_gb(model, 262144)
    assert at_served > at_catalog + 8.0, (at_catalog, at_served)
    # The box measured 36.92 GiB here — AT f16, which this family no longer serves. With
    # `-ctk/-ctv q8_0` the cache halves, so the same load is expected near 30 GiB and the old
    # window would fail for the right reason. Widened downward, NOT re-anchored on a guess:
    # the upper bound still holds the f16 measurement's neighbourhood, so if the flag ever
    # comes off without this number following, the reservation goes back out of range and
    # this test says so.
    assert 28.0 <= at_served <= 38.0, at_served

    # A second parallel slot holds its own window-sized KV; the weights are shared. So the
    # increment is exactly one more window's worth of cache, nothing else.
    two_slots = local_catalog.load_footprint_gb(model, 262144, slots=2)
    one_window_of_kv = model.kv_gb_per_128k * 262144 / 131072
    assert two_slots - at_served == pytest.approx(one_window_of_kv, abs=0.01)

    # And the gateway actually consults the override rather than the catalog: a probe with room
    # for the catalog figure but not the served one must REFUSE.
    probe = _StubProbe(_mem(124.0 - at_catalog - 8.0))
    gateway = LocalGatewayClient(
        "http://gw",
        gpu_probe=probe,
        windows_loader=_windows({model.id: 262144}),
    )
    with pytest.raises(gpu_guard.GpuBudgetError):
        await gateway.load(model.served_model)


def _windows(mapping: dict[str, int]) -> Callable[[], Awaitable[Mapping[str, int]]]:
    async def load() -> Mapping[str, int]:
        return mapping

    return load


@pytest.mark.anyio
async def test_an_unreadable_override_falls_back_instead_of_blocking_the_load() -> None:
    """Settings are read per load; a DB hiccup must not become a refusal to load anything.

    The fallback is the catalog window — which under-reserves rather than over-reserves, so the
    load WATCHDOG is what covers the gap. That trade is deliberate: a guard that hard-fails
    when the settings table blinks would take the box's models offline for a transient."""
    model = local_catalog.get("qwen3.8-27b-abliterated")
    assert model is not None

    async def boom() -> Mapping[str, int]:
        raise RuntimeError("settings unavailable")

    seen: list[str] = []
    gateway = LocalGatewayClient(
        "http://gw",
        transport=_transport(seen=seen),
        gpu_probe=_StubProbe(_mem(20.0)),  # ample for the catalog figure
        windows_loader=boom,
    )
    await gateway.load(model.served_model)
    assert any(path.endswith("/health") for path in seen)


def test_a_finished_load_drops_the_page_cache_copy_of_the_weights(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-mmap` leaves every model resident TWICE — once in GTT, once in the page cache
    the read filled — and only the GTT copy goes away on unload.

    Measured on the box: one gpt-oss-120b load took `Cached` from 5.2 to 49.1 GiB and left
    `MemFree` at 8.4 GiB of 121; after the unload the 39.4 GiB cache copy was still there.
    That invisible second copy is what livelocked the host for seven hours on 2026-08-19, so
    the drop belongs on the load chokepoint, not on the callers who remember to ask."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    (tmp_path / model.id).mkdir()
    (tmp_path / model.id / "weights.gguf").write_bytes(b"x" * 4096)

    dropped: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "jbrain.llm.local_weights.drop_weights_page_cache",
        lambda models_dir, model_id: dropped.append((models_dir, model_id)) or 12.5,
    )

    async def run() -> None:
        gateway = LocalGatewayClient(
            "http://gw",
            transport=_transport(),
            gpu_probe=_StubProbe(_mem(10.0)),
            models_dir=str(tmp_path),
        )
        await gateway.load(model.served_model)

    asyncio.run(run())
    assert dropped == [(str(tmp_path), model.id)]


def test_the_drop_is_skipped_when_the_weights_are_not_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway built without `models_dir` (the CLI's unload path, every structural test)
    must load exactly as before rather than erroring on a directory it cannot see."""
    called: list[object] = []
    monkeypatch.setattr(
        "jbrain.llm.local_weights.drop_weights_page_cache",
        lambda models_dir, model_id: called.append(models_dir),
    )

    async def run() -> None:
        gateway = LocalGatewayClient(
            "http://gw", transport=_transport(), gpu_probe=_StubProbe(_mem(10.0))
        )
        await gateway.load("gpt-oss-120b")

    asyncio.run(run())
    assert called == []


def test_the_weights_cache_is_dropped_when_the_guard_aborts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure path, which had no coverage at all.

    An aborted load has still READ the weights, so the page-cache copy exists — measured
    at +4.29 GiB for a 4.3 GB model on 2026-08-19, against +0.00 GiB for a SUCCESSFUL
    16.8 GB one. The drop works; it just never ran here, because `guarded_load` raises
    `GpuBudgetError` from outside its own try/finally and the drop sat on the line after
    the `await`. No unload path drops either, and the abort routes through exactly that
    unload.

    Worse than a leak: `host_metrics.read_memory_gb` counts page cache as USED, so every
    abort shrinks the apparent headroom and makes the next abort likelier, and
    `residency` suppresses `GpuBudgetError` on the end-of-turn restore — so it ratchets
    silently."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    (tmp_path / model.id).mkdir()
    (tmp_path / model.id / "weights.gguf").write_bytes(b"x" * 4096)

    dropped: list[str] = []
    monkeypatch.setattr(
        "jbrain.llm.local_weights.drop_weights_page_cache",
        lambda models_dir, model_id: dropped.append(model_id) or 4.29,
    )

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise gpu_guard.GpuBudgetError("device memory ran away while loading")

    monkeypatch.setattr(gpu_guard, "guarded_load", _refuse)

    async def run() -> None:
        gateway = LocalGatewayClient(
            "http://gw",
            transport=_transport(),
            gpu_probe=_StubProbe(_mem(10.0)),
            models_dir=str(tmp_path),
        )
        with pytest.raises(gpu_guard.GpuBudgetError):
            await gateway.load(model.served_model)

    asyncio.run(run())
    assert dropped == [model.id], "the aborted load left its weights in the page cache"


def test_a_load_reports_the_device_delta_against_the_catalog(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement that replaced three failed log-scraping attempts.

    `gpu_guard.measure_footprint` computes the GTT+VRAM a load actually pinned, from the
    samples the guard already brackets it with. It existed in the tree the whole time,
    wired into one of six call sites, while `local_gateway` — the path every caller goes
    through — threw its baseline away.

    It is also the only instrument that can see the VISION PROJECTOR: mmproj weights print
    as `model size:`, not `model buffer size`, so no log parser counts them, and the
    projector balloon is what every freeze on this box involved."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    (tmp_path / model.id).mkdir()
    (tmp_path / model.id / "weights.gguf").write_bytes(b"x")

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_gateway.log, "warning", lambda event, **kw: events.append({"e": event, **kw})
    )
    monkeypatch.setattr(
        local_gateway.log, "info", lambda event, **kw: events.append({"e": event, **kw})
    )

    async def run() -> None:
        # 10 GB of device memory in use before, 14 GB after: a 4 GB delta.
        #
        # Two DISTINCT samples on purpose. An earlier version passed one — and `_StubProbe`
        # repeats its last sample, so both reads were 10.0 and the delta this test claims to
        # check was zero. It asserted a footprint had been reported, and one had: 0.0. The
        # test was green on precisely the defect that later showed up on the box as
        # `measured_gb 0.0, drift_gb -26.59`.
        probe = _StubProbe(_mem(10.0), _mem(14.0))
        gateway = LocalGatewayClient(
            "http://gw", transport=_transport(), gpu_probe=probe, models_dir=str(tmp_path)
        )
        await gateway.load(model.served_model)

    asyncio.run(run())
    measured = [e for e in events if e["e"] == "local_gateway.footprint_measured"]
    assert measured, f"no footprint reported; saw {[e['e'] for e in events]}"
    row = measured[-1]
    assert row["model"] == model.id
    assert "predicted_gb" in row and "drift_gb" in row
    # Pin the VALUE, not just the key: a measurement that is always 0.0 has every key too.
    assert row["measured_gb"] == 4.0


def test_an_eviction_that_races_the_measurement_is_not_a_zero_footprint(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBSERVED on the box: a load whose model was gone by sample time logged
    `measured_gb 0.0, drift_gb -26.59` at WARNING — which reads as the catalog
    over-predicting a 27B by its entire size, the exact inverse of the truth.

    A resident model always pins GB, so a delta at or below zero is a failed measurement,
    not a free model. Same class of lie as `if freed:` hiding the drop that freed nothing."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    (tmp_path / model.id).mkdir()
    (tmp_path / model.id / "weights.gguf").write_bytes(b"x")

    events: list[str] = []
    monkeypatch.setattr(local_gateway.log, "info", lambda event, **_kw: events.append(event))
    monkeypatch.setattr(local_gateway.log, "warning", lambda event, **_kw: events.append(event))

    async def run() -> None:
        # Same reading before and after: whatever the load pinned is already gone.
        probe = _StubProbe(_mem(10.0), _mem(10.0))
        gateway = LocalGatewayClient(
            "http://gw", transport=_transport(), gpu_probe=probe, models_dir=str(tmp_path)
        )
        await gateway.load(model.served_model)

    asyncio.run(run())
    assert "local_gateway.footprint_unmeasured" in events
    assert "local_gateway.footprint_measured" not in events, (
        "a raced eviction was reported as a real measurement of zero"
    )


def test_a_probe_less_load_reports_nothing_rather_than_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No probe means no measurement — and a measurement of 0.0 would read as a model that
    costs nothing, i.e. infinite headroom. Silence is the only safe answer."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    events: list[str] = []
    monkeypatch.setattr(local_gateway.log, "info", lambda event, **_kw: events.append(event))
    monkeypatch.setattr(local_gateway.log, "warning", lambda event, **_kw: events.append(event))

    async def run() -> None:
        gateway = LocalGatewayClient("http://gw", transport=_transport())
        await gateway.load(model.served_model)

    asyncio.run(run())
    assert "local_gateway.footprint_measured" not in events


@pytest.mark.anyio
async def test_warming_an_already_resident_model_is_not_asked_to_make_room_for_it() -> None:
    """The pre-flight samples device memory that ALREADY holds this model's footprint, then
    asks for room for a second copy of it. So the fuller the box is with the very model being
    warmed, the likelier it is to refuse to warm it.

    MEASURED 2026-08-21: `POST /api/debug/llm/local-models/gpt-oss-120b/prime` on a RESIDENT
    gpt-oss-120b answered 500 — "needs ~68.5 GB but only 34.5 GB is safely available" — on a
    box holding exactly one healthy model with 105 GiB free. `warm_keeper` had worked around
    this at its own call site, which left every other caller carrying it: the owner's Load
    button (`gateway_load`) and the debug console's prime (`gateway_prime`) both land here.

    Warming a model that is already up is not a load and cannot need room for one."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    room = local_catalog.load_footprint_gb(model)
    # Exactly the state a healthy resident model produces: its own footprint accounted for,
    # and nowhere near enough left for a second copy. The old code refused here.
    probe = _StubProbe(_mem(124.0 - room + 1.0))

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": "gpt-oss-120b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    gateway = LocalGatewayClient(
        "http://gw", transport=httpx.MockTransport(handler), gpu_probe=probe
    )
    await gateway.load("gpt-oss-120b")  # must not raise
    # And it really did warm — the point of the call is the prefill, not a no-op return.
    assert any(p.endswith("/v1/chat/completions") for p in seen)


@pytest.mark.anyio
async def test_a_model_that_is_not_resident_is_still_admitted_first() -> None:
    """The escape hatch above must not become a way to skip the guard. A COLD model on the
    same too-full box is still refused — that is the freeze this whole chokepoint exists to
    prevent, and it is one `running()` reading away from the case that must pass."""
    model = local_catalog.get("gpt-oss-120b")
    assert model is not None
    probe = _StubProbe(_mem(124.0 - local_catalog.load_footprint_gb(model) + 1.0))

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": "something-else"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    gateway = LocalGatewayClient(
        "http://gw", transport=httpx.MockTransport(handler), gpu_probe=probe
    )
    with pytest.raises(gpu_guard.GpuBudgetError):
        await gateway.load("gpt-oss-120b")


def _admitted_load_calls() -> list[tuple[pathlib.Path, ast.Call, str]]:
    """Every call to the two shared warm helpers, with the `residency` it passes."""
    found: list[tuple[pathlib.Path, ast.Call, str]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name not in {"gateway_load", "gateway_prime"}:
                continue
            passed = next((kw for kw in node.keywords if kw.arg == "residency"), None)
            found.append((path, node, "" if passed is None else ast.unparse(passed.value)))
    return found


# The only two ways a caller may name the coordinator. Both can evaluate to None on a build
# with none wired, which is a DOCUMENTED degradation, not a hole: `LocalGatewayClient.load`
# still runs the device-memory guard, so an unadmitted warm loses EVICTION (a model that
# would have fit after freeing room is refused with a 409) but can never take the box down.
# A third spelling is how that distinction gets lost, so it fails here until it is added
# deliberately.
_APPROVED_RESIDENCY = {
    "residency",  # the DI dependency (get_residency) — owner + worker paths
    "getattr(request.app.state, 'residency', None)",  # debug console routes
}


def test_every_shared_warm_is_handed_a_residency_by_an_approved_name() -> None:
    """`residency` is a required keyword on both helpers, so pyright already catches an
    omission. What it cannot see is the VALUE: `None` is legal, and a getattr default that
    silently yields it is how the debug console spent two loads evicting nothing at all."""
    calls = _admitted_load_calls()
    assert calls, "the AST scan found no gateway_load/gateway_prime calls — the check has rotted"
    wrong = [
        f"{path.relative_to(_SRC)}:{call.lineno} passes {expr or '<nothing>'}"
        for path, call, expr in calls
        if expr not in _APPROVED_RESIDENCY
    ]
    assert not wrong, (
        "unapproved residency= at: "
        + "; ".join(wrong)
        + " — a load helper must be handed the box's coordinator by one of "
        + str(sorted(_APPROVED_RESIDENCY))
        + ". A bare None (or a new getattr default) makes the warm evict nothing; if that is "
        + "really what you want, add the spelling to _APPROVED_RESIDENCY and say why."
    )


def test_production_never_wires_an_inert_residency() -> None:
    """`ResidencyWiring.inert()` fills every switch with its do-nothing fallback, which is what
    tests and a cloud-only build want. It is also a way to satisfy the required-argument
    constructor without wiring anything — the half-wired gate all over again, one call shorter.
    Production says all eleven or it does not build."""
    users = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "inert"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ResidencyWiring"
            ):
                users.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert not users, (
        "ResidencyWiring.inert() called in production code at: "
        + ", ".join(users)
        + " — inert() is for tests and cloud-only builds. A real box must name every switch, so "
        + "that a missing one is a pyright error rather than a gate that admits everything."
    )
