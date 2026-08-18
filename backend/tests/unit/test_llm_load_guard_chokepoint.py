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
from dataclasses import replace

import httpx
import pytest

from jbrain.llm import gpu_guard, local_catalog
from jbrain.llm.local_gateway import LocalGatewayClient

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"


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
    assert resident_only == pytest.approx(
        text_only.checkpoint_gb * local_catalog.CTX_CHECKPOINTS, abs=0.01
    )
    # An entry with no checkpoint cost of its own DOES still agree exactly — which is what
    # isolates the claim above to the checkpoints rather than to some other drift.
    no_checkpoints = replace(text_only, checkpoint_gb=0.0)
    assert local_catalog.footprint_gb(
        no_checkpoints, no_checkpoints.context_window
    ) == local_catalog.load_footprint_gb(no_checkpoints)


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
    assert 19.0 <= predicted <= 20.0, predicted


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
    # 19.50 measured text-only, plus the projector weights and its vision workspace.
    assert 20.5 <= predicted <= 22.0, predicted
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
