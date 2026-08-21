"""Management client for the llama-swap local gateway — runtime model state.

This is runtime-state management, not a functional LLM call, so it lives outside
the LLM adapter: it speaks llama-swap's admin HTTP API to report and control which
models are resident in memory:
  - GET  /running                      → models currently loaded
  - POST /api/models/unload/{model}    → unload one model
  - GET  /upstream/{model}/health      → proxy a request, which makes the gateway
                                         load the model (llama-swap has no explicit
                                         load endpoint; loading is request-driven)
  - POST /upstream/{model}/v1/chat/completions (1 token, discarded) → warm the
                                         inference path after load so the first real
                                         turn isn't the slow one, optionally priming a
                                         persona system prompt into the KV cache (see `load`)
  - GET  /logs                         → recent gateway stdout, served to the debug
                                         console. It carries llama-swap's OWN lines only,
                                         never llama.cpp's — which is why the load
                                         percentage is measured off device memory
                                         (gpu_guard) rather than read out of here
  - GET  /slots  (per model)           → llama-server's per-slot state, including whatever
                                         this build calls its prefill counters
                                         (jbrain.llm.prefill)

Best-effort by design. The settings screen must render even when the gateway is
down, still cold, or too old to expose these endpoints, so `running()` swallows
every error and returns an empty set; only an explicit `unload()` surfaces a
failure (the operator asked for an action, so they get told if it didn't work).

The OpenAI base URL ends in `/v1`; the admin endpoints sit at the root, so we
strip that suffix once here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import httpx
import structlog

from jbrain import box_events, host_metrics
from jbrain.llm import gpu_guard, local_catalog, local_weights, prefill

log = structlog.get_logger()

# How the in-flight page-cache sweep is paced (`_sweep_page_cache_during_load`).
#
# Two triggers, whichever comes first. TIME alone is not enough: the read that fills the cache
# runs at disk speed, and the measured control put ~50 GiB there in about 35 s — roughly 1.5
# GiB per second — so any interval cheap enough to poll at leaves GB on the floor between
# ticks. SIZE alone is not enough either: a stalled or slow load would never trip it, and the
# residue would sit until the load finished.
#
# 1 GiB bounds the transient at roughly a second of read; 2 s bounds it when growth is slow.
# The poll itself is one small /proc/meminfo read, so it can be much finer than either.
_SWEEP_POLL_S = 0.25
_SWEEP_INTERVAL_S = 2.0
_SWEEP_GROWTH_GB = 1.0

# How the load span's one percentage is split between its two phases.
#
# The weights read owns nearly all of it, because the warm-up that follows is usually a disk
# read rather than a prefill. MEASURED on 2026-08-21: `warm_keeper.slot_restored` put a saved
# 27,787-token KV slot back in ~90 ms, against the 118 s the same prefix took to prefill on a
# load with no saved slot. So the warm is milliseconds in the normal case and a minute or two
# in the cold one, and no fixed split can be right for both.
#
# An earlier version of this constant was 0.4, derived from the single cold load that happened
# not to have a saved slot. On every restored load after it the bar climbed to 0.384 and
# stopped, because phase two never published anything. Weighting it the other way makes the
# common case nearly right and the rare case merely early — and `_warm` publishes 1.0 when it
# returns either way (see `_warm`), so neither case ends short of full.
_WEIGHTS_SHARE = 0.9

_PROGRESS_STEP = 0.01

# A catalog entry wrong by this much is worth waking someone for: the two found on
# 2026-08-19 were light by 1.4 and >5.5 GiB, and the smaller of those was enough to abort
# a healthy load. Below it, drift is ordinary per-build variation and is logged at info.
_FOOTPRINT_DRIFT_GB = 1.0


class LocalGatewayError(Exception):
    """A load/unload call the gateway rejected or couldn't be reached for."""


class LocalGateway(Protocol):
    """The runtime-state surface consumers depend on (report/unload/load), so a
    caller takes the capability rather than the concrete HTTP client — the in-memory
    test fake satisfies it structurally, the same seam as the `ImageGen` protocol."""

    async def running(self) -> set[str]: ...

    async def unload(self, served_model: str) -> None: ...

    async def load(self, served_model: str) -> None: ...


class LocalGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
        gpu_probe: gpu_guard.GpuMemProbe | None = None,
        config_regen: Callable[[], Awaitable[None]] | None = None,
        windows_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
        slots_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
        models_dir: str = "",
    ):
        self._root = base_url.rstrip("/").removesuffix("/v1")
        self._transport = transport
        self._timeout = timeout
        # Device-memory probe for the load guard. It lives HERE, on the client, rather than in
        # the residency coordinator, because `load()` is the single chokepoint every path to
        # committing GPU memory must pass through. Guarding a wrapper only protects the callers
        # who remember to use the wrapper: this box froze three times, and every one went
        # through a load path that skipped the guard (the manual settings load, the debug
        # console load, and the residency RESTORE — three of the six call sites). A safety
        # check a caller can decline is not a safety check.
        self._gpu_probe = gpu_probe
        # One lock per served model, so two callers cannot load the same one at once. See
        # `load` for what that cost the owner without it.
        self._load_locks: dict[str, asyncio.Lock] = {}
        # The live per-model context-window and parallel-slot overrides (Settings → LLM), read
        # per load. They live here for the same chokepoint reason as the probe: KV is LINEAR in
        # the window, so a guard that reserves for the catalog default while llama-swap serves
        # an override is not sized for the load it is guarding. Measured: the abliterated 27B
        # served at `-c 262144` against a 32768 catalog default reserved 20.29 GB for a load
        # that took 36.92 GB. Unset (the tests, the CLI without a settings context) falls back
        # to the catalog default and one slot, which is what an unconfigured box serves.
        # Re-stamp llama-swap.yaml just before a load, instead of on every settings edit.
        #
        # Rewriting that file makes llama-swap reload, and its reload calls `old.Shutdown()`,
        # which kills EVERY running llama-server — not just the model being edited. Doing it on
        # the settings PUT therefore charged an unrelated resident model for someone changing a
        # dropdown, silently: the kill happens inside llama-swap, so nothing writes a
        # `box_events` row and the vitals surface stays quiet. Diagnosed 2026-08-20 from three
        # consecutive manual loads of gpt-oss with ZERO unload rows between them.
        #
        # Deferring is sound because the PWA only lets a model's flags be edited while it is NOT
        # resident (`editable = !m.loaded`): at edit time there is no process to update, so the
        # write buys nothing and costs every other model. The moment it IS needed is the edited
        # model's next load — which is here.
        #
        # Paired with `llama_swap_config.write`'s content compare this is free on the common
        # path: a load whose config already matches writes nothing and triggers no reload.
        self._config_regen = config_regen
        self._windows_loader = windows_loader
        self._slots_loader = slots_loader
        # Where the weights live, so a finished load can drop their PAGE-CACHE copy. Same
        # chokepoint reasoning again: `--no-mmap` leaves every model resident twice — once in
        # GTT, once in the cache the read filled — and the copy that is invisible to
        # `MemAvailable` is the one that killed this host (jbrain.llm.local_weights
        # .drop_weights_page_cache). Unset (a container without the weights mount, the tests)
        # skips the drop and keeps the prior behaviour.
        self._models_dir = models_dir
        # Resident set as of the last `running()` poll, and the models THIS client loaded.
        # Together they say which arrivals came from llama-swap serving a request rather than
        # from us — the loads whose page-cache copy nothing was dropping. See
        # `_drop_cache_for_unannounced`.
        self._seen_resident: set[str] = set()
        self._loaded_here: set[str] = set()

    async def running(self) -> set[str]:
        """Served-model names currently loaded, or an empty set on ANY failure
        (unreachable, non-2xx, malformed, or an old build without /running).

        Also the observation point for a model that arrived WITHOUT us: see
        `_drop_cache_for_unannounced`."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/running")
                resp.raise_for_status()
                resident = _parse_running(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.info("local_gateway.running_unavailable", error=str(exc))
            return set()
        self._drop_cache_for_unannounced(resident)
        return resident

    def _drop_cache_for_unannounced(self, resident: set[str]) -> None:
        """Drop the weights cache for any model that became resident without going through
        `load()` — llama-swap loads on REQUEST, so most loads never touch this client.

        MEASURED on the box, and the reason this exists: `_drop_weights_cache` covers only
        the deliberate paths (residency, warm_keeper, the settings screen, jcode). Actual
        inference goes through the LLM adapter straight to the OpenAI-compatible endpoint,
        and llama-swap loads the model itself to serve it. During one sweep the app's own
        turns swapped gpt-oss-120b in repeatedly and `Cached` climbed 2.36 -> 47.83 GiB with
        `MemFree` at 8.07 on a 121 GiB box — the exact double-residency that livelocked this
        host on 2026-08-19 — and not one `weights_cache_*` line was logged, because no load
        we knew about had happened.

        `running()` is the chokepoint that sees those loads: every poller in the process
        already calls it on a tick. Dropping from here needs no new loop and cannot be
        forgotten by a caller, the same argument that puts the device guard on `load` and the
        vitals narration on `unload`.

        Only on the TRANSITION into residency, so a steady poll costs nothing. Models we
        loaded ourselves are skipped — `load()` already dropped theirs. Several clients exist
        in a process (main, worker), each with its own view; the worst case is one extra
        `posix_fadvise` sweep over already-evicted files, which is idempotent and harmless."""
        if not self._models_dir:
            return
        arrived = resident - self._seen_resident
        self._seen_resident = set(resident)
        for served in sorted(arrived - self._loaded_here):
            model = local_catalog.get_by_served(served)
            if model is not None:
                log.info("local_gateway.unannounced_load", model=model.id)
                self._drop_weights_cache(model)
        # Forget our own loads once they are gone, so a later request-driven reload of the
        # same model is treated as unannounced (it is).
        self._loaded_here &= resident

    async def unload(self, served_model: str) -> None:
        """Unload one model from memory. Raises LocalGatewayError on any failure.

        Narrated to the vitals surface (jbrain.box_events) from HERE rather than from the
        six callers, for the same reason the device-memory guard lives on `load`: this is
        the one chokepoint every path to freeing a model passes through, so instrumenting
        it leaves nothing to forget. WHY it is being unloaded rides in on the caller's
        `box_events.because(...)` — "to make room for gpt-oss-120b", "an image render
        needs the box" — which is the difference between a log and an explanation."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._root}/api/models/unload/{served_model}")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            await box_events.record(
                box_events.MODEL_UNLOAD, served_model, status="failed", detail=str(exc)
            )
            raise LocalGatewayError(str(exc)) from exc
        await box_events.record(box_events.MODEL_UNLOAD, served_model)

    async def _narrate_reload_casualties(self, before: set[str], loading: str) -> None:
        """Record the models a config-driven gateway reload just killed.

        Rewriting llama-swap.yaml makes llama-swap reload, and its reload calls
        `old.Shutdown()` — killing EVERY running llama-server, not only the one whose setting
        changed. `regen_gateway_config` waits for that reload to land before returning, so by
        the time we get here the casualties are observed fact, not a prediction.

        Why this exists at all: the kill happens INSIDE llama-swap, so nothing writes an
        `app.box_events` row and the vitals surface says nothing. That silence is precisely how
        the cost stayed invisible while the owner reported several times that staging a model
        unloaded gpt-oss-120b, and was several times told it was a display artifact. It was not.
        An unavoidable eviction is acceptable; an unexplained one is not.

        Best-effort: narration must never fail the load it is describing."""
        if not before:
            return
        with contextlib.suppress(Exception):  # noqa: BLE001 — narration must not fail a load
            model = local_catalog.get_by_served(loading)
            name = model.id if model is not None else loading
            reason = f"the gateway reloaded to apply changed settings for {name}"
            for served in sorted(before - await self.running() - {loading}):
                await box_events.record(box_events.MODEL_UNLOAD, served, detail=reason)

    async def props(self, served_model: str) -> dict[str, object]:
        """llama-server's own `/props` for one RESIDENT model — `build_info` (the ONLY build
        identity available over HTTP), `total_slots`, and the resolved generation settings
        including the real `n_ctx`.

        REFUSES a model that isn't already loaded, and that refusal is the point. This reads
        through llama-swap's `/upstream/<model>/…` passthrough, and that path makes llama-swap
        LOAD the model on demand — outside `jbrain.llm.residency`, which is the box's sole
        evictor and the only thing that checks whether a load fits. Calling it on a cold model
        beside a large resident one froze this host to a power cycle. A read-only diagnostic
        must never be able to commit gigabytes of device memory, so the caller loads first
        (through residency) and reads second."""
        body = await self._upstream_get(served_model, "props", "/props")
        return body if isinstance(body, dict) else {}

    async def slots(self, served_model: str) -> list[dict[str, object]]:
        """llama-server's `/slots` for one RESIDENT model — per-slot state, and on a
        speculative build the `speculative` object that says whether drafting is actually
        running. `/props`'s `speculative.types` CANNOT answer that (the server builds it from
        a `task_params` it never populates, so it reads "none" on every build); this can.

        Needs `--slots`, which jbrain.llm.llama_swap_config always passes. Refuses a
        non-resident model for the same reason `props` does."""
        body = await self._upstream_get(served_model, "slots", "/slots")
        return body if isinstance(body, list) else []

    async def metrics(self, served_model: str) -> str:
        """llama-server's Prometheus `/metrics` for one RESIDENT model, as raw text.

        Carries the speculative-decoding counters — drafted vs accepted tokens — which are the
        only direct measure of whether MTP is earning its keep and whether `--spec-draft-n-max`
        is set to the right depth. Needs `--metrics`, which the config always passes."""
        return await self._upstream_text(served_model, "metrics", "/metrics")

    async def _require_resident(self, served_model: str, what: str) -> None:
        """Guard shared by every `/upstream/…` read. Reaching that passthrough makes llama-swap
        LOAD the model on demand, outside `jbrain.llm.residency` — the box's sole evictor and
        the only thing that checks whether a load fits. Doing it on a cold model beside a large
        resident one froze this host to a power cycle, so a diagnostic read is never allowed to
        commit gigabytes of device memory."""
        if served_model not in await self.running():
            raise LocalGatewayError(
                f"{served_model} is not resident — refusing to read /{what}, because reaching "
                "it would make the gateway load the model outside the residency budget. Load "
                "it first (which evicts to make room), then read."
            )

    async def _upstream_get(self, served_model: str, what: str, path: str) -> object:
        await self._require_resident(served_model, what)
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 180.0), transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/upstream/{served_model}{path}")
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalGatewayError(str(exc)) from exc

    async def _upstream_text(self, served_model: str, what: str, path: str) -> str:
        await self._require_resident(served_model, what)
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 180.0), transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/upstream/{served_model}{path}")
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError as exc:
            raise LocalGatewayError(str(exc)) from exc

    async def _sweep_page_cache_during_load(
        self,
        model: local_catalog.LocalModel | None,
        *,
        weights_gb: float = 0.0,
        on_progress: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Evict the weights' page cache WHILE the load runs, not only after it — and count
        what goes past on the way, which is the only reading of the READ itself this box has.

        MEASURED on the box, and the reason the after-the-fact drop is not enough: loading
        gpt-oss-120b on an IDLE box took `Cached` from 2.11 to 50.72 GiB and `MemFree` down to
        6.54 GiB, for about 35 seconds, before the post-load drop reclaimed it. That window is
        the whole failure mode — the kernel was already reclaiming under GTT pressure, which is
        what livelocked this host on 2026-08-19.

        Those cached bytes are dead on arrival. llama.cpp allocates the GTT buffer FIRST
        (measured: 57 GB committed before a byte is read), then reads the GGUF into it, so the
        page-cache copy is a side effect of the read and is never read again.

        There is no engine flag that avoids it. `--load-mode dio` opens with `O_DIRECT` and then
        fails on the read — `read_raw_unsafe: Falling back to buffered IO due to Bad address`,
        i.e. EFAULT, because the destination is device memory the kernel cannot DMA a file into.
        `mmap` is worse (peak 60.44 GiB, 26% slower). All three modes must go through a buffered
        read, so evicting continuously is the only lever, and it is entirely ours.

        Fires on whichever comes first: `_SWEEP_INTERVAL_S`, or `_SWEEP_GROWTH_GB` of page-cache
        growth. The size trigger is what bounds the transient — a fast disk can fill GB between
        two ticks of any interval slow enough to be cheap. `last` is therefore the reading at
        the last DROP, never at the last poll: comparing against the poll would make the size
        trigger unreachable (0.25 s of read is nowhere near a GiB) and quietly demote the sweep
        to its interval alone, which is exactly the transient it exists to bound.

        WHY THE PROGRESS FRACTION IS HERE rather than on the device-memory watchdog, where it
        was first put: the paragraph above says llama.cpp commits the whole GTT buffer before it
        reads a byte, so device memory is the RESERVATION. Measured on the box, one cold
        gpt-oss-120b: the device reading was already at 0.78 four seconds in. Cache growth is
        the other side of the same read — bytes that actually arrived from disk — so it tracks
        the work instead of the booking. `read_gb` accumulates the growth each poll sees and so
        survives the drops that keep taking it back out; that load swept 58 times at roughly a
        GiB each across a 69 GiB model.

        The denominator is the model's WEIGHTS ON DISK, not the load's projected footprint:
        the footprint budgets for KV and compute buffers, which are allocated rather than
        read, so measuring a read against it can never reach 1.0 (measured: 57.7 of 68.55).

        The numerator is GLOBAL `Cached`, not this model's alone: `cachestat(2)` is per-fd and
        seccomp-blocked in this container, so `drop_weights_page_cache` returns None here and
        per-file accounting is unavailable. During a load reading tens of GB the global figure
        is dominated by the weights; anything else on the box only makes the bar run ahead,
        which the clamp bounds at "arrived". It is also read at 0.1 GiB resolution
        (`host_metrics.read_page_cache_gb`), which is fine against ~0.4 GiB of growth per poll.

        Best-effort throughout, and cancelled by the caller when the load returns: a failure
        here costs memory, never correctness."""
        if model is None or not self._models_dir:
            return
        cache_before = host_metrics.read_page_cache_gb()
        sweeps = 0
        last = cache_before or 0.0  # at the last DROP — drives the size trigger, see above
        prev = last  # at the last POLL — drives the read accumulator, and only that
        read_gb = 0.0
        published = 0.0
        elapsed = 0.0
        try:
            while True:
                await asyncio.sleep(_SWEEP_POLL_S)
                elapsed += _SWEEP_POLL_S
                now = host_metrics.read_page_cache_gb()
                if now is not None:
                    if now > prev:
                        read_gb += now - prev
                    prev = now
                if on_progress is not None and weights_gb > 0:
                    fraction = read_gb / weights_gb
                    # Throttled to the watchdog's old cadence: the poll is four times a second
                    # because the sweep needs it to be, and a row update that often is narration
                    # outrunning the work it narrates.
                    if fraction - published >= _PROGRESS_STEP:
                        published = fraction
                        with contextlib.suppress(Exception):
                            await on_progress(fraction * _WEIGHTS_SHARE)
                grew = now is not None and (now - last) >= _SWEEP_GROWTH_GB
                if not grew and elapsed < _SWEEP_INTERVAL_S:
                    continue
                # `to_thread`: the walk + fadvise are blocking syscalls, and stalling the event
                # loop during a load would delay the very health probe we are timing.
                await asyncio.to_thread(
                    local_weights.drop_weights_page_cache, self._models_dir, model.id
                )
                sweeps += 1
                elapsed = 0.0
                last = host_metrics.read_page_cache_gb() or 0.0
                prev = last
        except asyncio.CancelledError:
            if sweeps:
                log.info(
                    "local_gateway.load_cache_swept",
                    model=model.id,
                    sweeps=sweeps,
                    cache_before_gb=cache_before,
                    cache_after_gb=host_metrics.read_page_cache_gb(),
                    read_gb=round(read_gb, 2),
                    weights_gb=round(weights_gb, 2),
                )
            raise

    def _drop_weights_cache(self, model: local_catalog.LocalModel | None) -> None:
        """Release the page-cache copy of the weights this load just read.

        Called the moment the load returns, on BOTH paths (guarded and probe-less), because
        the cost it removes is incurred by the read itself. With `--no-mmap` the weights are
        resident twice — GTT plus the cache the read filled — and only the GTT copy is freed
        on unload. Measured: one gpt-oss-120b load put `Cached` at 49.1 GiB and `MemFree` at
        8.4 GiB on a 121 GiB box, and the cache copy survived the unload.

        Synchronous on purpose. It is a handful of `posix_fadvise` calls with no I/O of their
        own, and doing it before the warm-up means the warm-up's allocations meet the memory
        this just returned rather than racing it."""
        if model is None or not self._models_dir:
            return
        freed = local_weights.drop_weights_page_cache(self._models_dir, model.id)
        # All three outcomes are worth a line, and they are not the same thing. `if freed:`
        # logged only the happy case — which was harmless while the figure was the sum of
        # file sizes (always truthy) and became a blind spot the moment it started being
        # MEASURED: a drop that freed nothing, and a kernel that cannot measure, both went
        # silent. Those are precisely the two readings worth having.
        if freed is None:
            log.info(
                "local_gateway.weights_cache_unmeasured",
                model=model.id,
                reason="cachestat(2) unavailable (needs Linux 6.5+) or model dir missing",
            )
        elif freed == 0.0:
            log.info(
                "local_gateway.weights_cache_drop_freed_nothing",
                model=model.id,
                note="pages were dirty, mapped, or under I/O — see local_weights",
            )
        else:
            log.info("local_gateway.weights_cache_dropped", model=model.id, freed_gb=freed)

    async def _served_shape(self, model: local_catalog.LocalModel) -> tuple[int, int]:
        """The (context window, parallel slots) llama-swap will actually serve `model` with —
        the operator's saved overrides when a loader is wired, else the catalog default and one
        slot. Best-effort: a settings read that fails must not block a load, so it degrades to
        the catalog shape rather than raising, and the watchdog still covers the difference."""
        window, slots = model.context_window, 1
        try:
            if self._windows_loader is not None:
                window = (await self._windows_loader()).get(model.id, window)
            if self._slots_loader is not None:
                slots = (await self._slots_loader()).get(model.id, slots)
        except Exception:  # noqa: BLE001 — any settings failure falls back, never blocks
            log.warning("local_gateway.served_shape_unavailable", model=model.id)
        return window, slots

    async def load(
        self,
        served_model: str,
        *,
        warm_system: str | None = None,
        warm_tools: list[dict[str, object]] | None = None,
    ) -> None:
        """Load `served_model` into memory AND warm it for inference. The health probe
        makes llama-swap load the model (request-driven; with --no-mmap the weights are
        read into RAM before it returns). But "weights resident" isn't "inference-ready":
        the first forward pass still pays the inference path — KV-cache allocation for the
        full context, CUDA graph capture, kernel warm-up — which otherwise lands on the
        user's first real turn (it feels like the model reloads: slow first token, fast
        after). So after the probe we force a single-token generation whose output is
        discarded — a readiness probe, the inference-path analog of the health GET, not a
        functional LLM call.

        `warm_system`, when given, is sent as that warm-up's system message so the model
        prefills that exact prefix into its KV cache during load — the manual Load passes
        the interactive persona (jerv) prompt this way. With the gateway's `--cache-reuse`,
        the first real turn carrying the same leading system prompt then reuses that prefix
        instead of prefilling the large static persona prompt cold (the tens-of-seconds
        first-token cost on a big model), moving that cost into the load the operator is
        already waiting on (docs/archive/LLM_PROMPT_CACHE_PLAN.md).

        `warm_tools` MUST carry the same tool schemas the real turn sends. Under the
        gateway's `--jinja`, the model's chat template renders the tool definitions into
        the prompt's LEADING tokens (harmony puts the tool-channel declaration in the system
        header and a `# Tools` block right after the persona) — so a persona-only warm-up
        (no tools) diverges from a real jerv turn (persona + tools) before the reusable
        prefix even ends, and `--cache-reuse` salvages little. Priming with the tools makes
        the warmed prefix an actual prefix of the real turn, so the reuse lands.

        Raises LocalGatewayError if the model can't load; the warm-up itself is best-effort
        (the model is resident regardless — a failed warm-up just leaves that cost on first
        use, the prior behaviour). Generous timeout: a cold 80B reads tens of GB of weights.

        Narrated to the vitals surface for the WHOLE duration — probe, weights, warm-up —
        because that whole duration is what the owner sees as a pinned GPU with nothing in
        the roster to explain it. The event opens before the first byte is read, so the
        screen says "loading gpt-oss-120b…" while it happens rather than accounting for it
        a minute later. Same chokepoint argument as the device-memory guard: every caller
        that can commit GPU memory comes through here, so none of them can forget to say
        so.

        SERIALIZED PER MODEL, because a second load of a model already loading is not a load
        — it is a duplicate that the memory guard then has to refuse. MEASURED on the box
        2026-08-21: a load of gpt-oss-120b was 48 s in, holding ~29 GB of the device pool it
        had already committed, when a deferred workflow retried it. The guard did its job and
        refused ("needs ~68.5 GB but only 39.5 GB is safely available"), which is correct
        arithmetic and a false alarm: nothing was wrong, the model was arriving. The owner got
        a red "failed to load" row for a load that succeeded a minute later, and a red row
        nobody can act on is how a vitals surface stops being read.

        A caller that queues behind an in-flight load takes ITS result: once the lock frees,
        the model is resident and there is nothing left to do. If the load it waited for
        FAILED, the model is not resident and this one proceeds — a genuine retry, which is
        what the second caller wanted in the first place.

        Per PROCESS, not per box. The api and the worker each hold their own client, so this
        cannot stop the two of them racing; residency's cross-process advisory lock is the
        seam for that. Both attempts measured here came through one process, which is the
        case this closes."""
        lock = self._load_locks.setdefault(served_model, asyncio.Lock())
        queued = lock.locked()
        async with lock:
            if queued and served_model in await self.running():
                # The load we waited on brought it in. Recorded rather than silent: an
                # operator watching a load they asked for deserves to know theirs was already
                # under way, not that nothing happened.
                log.info("local_gateway.load_joined", model=served_model)
                await box_events.record(
                    box_events.MODEL_LOAD, served_model, detail="already loading — joined it"
                )
                return
            async with box_events.span(box_events.MODEL_LOAD, served_model):
                await self._load_and_warm(
                    served_model, warm_system=warm_system, warm_tools=warm_tools
                )

    async def _load_and_warm(
        self,
        served_model: str,
        *,
        warm_system: str | None = None,
        warm_tools: list[dict[str, object]] | None = None,
    ) -> None:
        """`load` minus its narration: the health probe that makes llama-swap read the
        weights, the device-memory guard around it, and the inference warm-up."""
        load_timeout = max(self._timeout, 120.0)
        model = local_catalog.get_by_served(served_model)
        projected_gb = 0.0
        if model:
            window, slots = await self._served_shape(model)
            projected_gb = local_catalog.load_footprint_gb(model, window, slots=slots)

        async def _do_load() -> None:
            sweeper = asyncio.create_task(
                self._sweep_page_cache_during_load(
                    model,
                    # The WEIGHTS on disk, not the load's footprint. MEASURED: one cold
                    # gpt-oss-120b swept 57.7 GiB past the cache against a 68.55 GiB
                    # projection, so the bar topped out at 84% of this phase's share and
                    # jumped at the handover. The projection is right for the memory guard
                    # and wrong here: it includes the KV and compute buffers, which are
                    # ALLOCATED and never read from disk. Against the catalog's 59.0 GiB of
                    # weights the same sweep is 97.8% accurate.
                    weights_gb=model.size_gb if model else 0.0,
                    # The sweep's own accounting IS the first half of the loading bar; see its
                    # docstring for why this is not the device-memory watchdog's job.
                    on_progress=box_events.progress,
                )
            )
            try:
                async with httpx.AsyncClient(
                    timeout=load_timeout, transport=self._transport
                ) as client:
                    resp = await client.get(f"{self._root}/upstream/{served_model}/health")
                    resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise LocalGatewayError(str(exc)) from exc
            finally:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper

        # Bring llama-swap.yaml up to date for the model about to load. No-ops when the
        # rendered config already matches, so this costs nothing unless a setting changed.
        # When it DID change, `regen_gateway_config` blocks until llama-swap's reload has
        # landed — the reload kills every running llama-server, so it has to happen BEFORE the
        # load below rather than ~2 s into it. `_narrate_reload_casualties` then records who
        # died, because llama-swap's own kill writes nothing.
        if self._config_regen is not None:
            resident_before = await self.running()
            try:
                await self._config_regen()
            except Exception as exc:  # noqa: BLE001 — a stale config must never FAIL a load
                # Loud, not silent. This path gets ONE attempt per load, where the old per-PUT
                # regen was retried on the operator's next edit — so a swallowed failure here
                # means a model serving flags nobody can see are stale.
                # `api.llm_settings` also records it to box_events and surfaces it on the
                # settings screen; this is the LLM-layer half.
                log.warning("local_gateway.config_regen_failed", model=served_model, error=str(exc))
            else:
                await self._narrate_reload_casualties(resident_before, served_model)

        # Remember it as OURS before the load runs. `_drop_cache_for_unannounced` skips
        # models in this set because the drop below already covers them, and a load that
        # raises has still read the weights — so claiming it up front, rather than on
        # success, keeps a failed load from being dropped twice.
        self._loaded_here.add(served_model)

        if self._gpu_probe is None:  # no probe wired: the prior, unguarded behaviour
            try:
                await _do_load()
            finally:
                # `finally`, not the next line: a load that raises has still READ the
                # weights, so its page-cache copy exists and nothing else will ever drop
                # it. See the guarded branch below for the measurement that proved it.
                self._drop_weights_cache(model)
            await self._warm(served_model, system=warm_system, tools=warm_tools)
            return

        # ALREADY RESIDENT means there is nothing to admit, so the pre-flight must not run.
        # It samples device memory that already contains THIS model's own footprint and then
        # asks for room for a second copy of it. MEASURED 2026-08-21: `POST /prime` on a
        # resident gpt-oss-120b answered 500 — "needs ~68.5 GB but only 34.5 GB is safely
        # available" — on a box holding exactly one healthy model and 105 GiB free. Warming a
        # model that is already up is not a load and cannot need room for one.
        #
        # `warm_keeper` already worked around this at ITS call site (see the double-load note
        # there), which left every other caller carrying it: the owner's Load button and the
        # debug console's prime both reach here through `gateway_load`/`gateway_prime`. The
        # check belongs in the one place all of them pass through.
        #
        # Read AFTER `config_regen` on purpose: a config change reloads llama-swap and kills
        # the running server, so a model resident a moment ago may be gone — and then this
        # reads False and the guard runs, which is correct.
        if served_model in await self.running():
            try:
                await _do_load()
            finally:
                self._drop_weights_cache(model)
            await self._warm(served_model, system=warm_system, tools=warm_tools)
            return

        # PRE-FLIGHT, then WATCH — for every caller with something to admit. The projection
        # includes the VISION PROJECTOR BALLOON (`local_catalog.load_footprint_gb`): an
        # mmproj model pins tens of GB of GTT at load on an AMD iGPU (llama.cpp #27146), and
        # every freeze this box took was a projector-carrying model whose weights+KV
        # arithmetic looked comfortable. The watchdog covers the rest, because the first load
        # of any model is a guess and here a wrong guess costs a power cycle.
        #
        # `baseline` is kept rather than discarded: it is the before-half of the device
        # delta that `_record_measured_footprint` compares against the catalog.
        baseline = await self._gpu_probe.sample()
        gpu_guard.refuse_if_no_device_room(baseline, projected_gb, served_model)
        try:
            await gpu_guard.guarded_load(
                _do_load,
                probe=self._gpu_probe,
                projected_gb=projected_gb,
                target=served_model,
                abort=lambda: self.unload(served_model),
            )
        finally:
            # MEASURED: an aborted qwen3.5-4b left `Cached` +4.29 GiB — its entire 4.3 GB
            # weight file — while a successful 16.8 GB load left it unchanged. The drop
            # works; it simply never ran here, because `guarded_load` raises
            # `GpuBudgetError` from outside its own try/finally and this call used to sit
            # on the line after the `await`.
            #
            # That mattered more than a leak: `host_metrics.read_memory_gb` counts page
            # cache as USED, so a stranded copy shrinks the apparent headroom, which makes
            # the next load likelier to abort, which strands more. `residency` suppresses
            # `GpuBudgetError` on the end-of-turn restore, so the ratchet turned silently.
            #
            # `abort` unloads the model before this runs, so llama-server has released the
            # file and its folios are unlocked — which is what makes the drop effective
            # here rather than racing an in-flight read.
            self._drop_weights_cache(model)
        await self._warm(served_model, system=warm_system, tools=warm_tools)
        # After the warm on purpose: its prefill allocates KV and capture buffers that a
        # served model holds for its whole life, so they belong in the measured cost.
        self._record_measured_footprint(
            model,
            projected_gb,
            await gpu_guard.measure_footprint(self._gpu_probe, baseline, served_model),
        )

    async def _warm(
        self,
        served_model: str,
        *,
        system: str | None = None,
        tools: list[dict[str, object]] | None = None,
    ) -> None:
        """Exercise the inference path with one discarded token. Best-effort: the model is
        already loaded, so a warm-up failure is logged, not raised — it only means the
        first real turn pays the warm-up cost (no worse than before this warm-up existed).

        A `system` prompt makes this a priming warm-up: it becomes the leading message so
        its KV prefix is prefilled and left in the cache for the first real turn to reuse
        (see `load`). `tools`, when given, are sent alongside so the rendered prefix matches
        a real tool-carrying turn (the reuse otherwise misses — see `load`). The timeout is
        generous because that prefill is the real work — the same persona+tools prefill that
        otherwise stalls the user's first turn.

        And because it IS the real work, it is also the load bar's second half. MEASURED: this
        call took 118 s of a 198 s gpt-oss-120b load, during which the bar had nothing to say
        and sat where the weights read left it. `llm.prefill` reads the same `/slots` counter a
        real turn's prefill uses, so the two are one mechanism rather than two that can
        disagree, and the fraction is mapped into `_WEIGHTS_SHARE`..1 so the bar keeps
        advancing across the phase boundary instead of restarting.

        AND IT PUBLISHES 1.0 ON THE WAY OUT, which is the only part of this that is not an
        estimate. The prefill fraction is measured against a guess at the token count, and
        that guess over-predicts here by about a third — the primed prefix is mostly tool
        schemas, and JSON tokenizes far denser than prose — so the bar used to stop at 0.84
        and sit there. Worse, a restored slot skips the prefill entirely and published
        nothing at all. Arriving here means the warm is DONE, however it got done, so the
        honest reading is full."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": "warmup"})
        body: dict[str, object] = {
            "model": served_model,
            "messages": messages,
            "max_tokens": 1,
            "stream": False,
        }
        if tools:
            body["tools"] = tools

        async def _publish(fraction: float) -> None:
            await box_events.progress(_WEIGHTS_SHARE + fraction * (1.0 - _WEIGHTS_SHARE))

        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if tools:
            # The rendered tool schemas are part of what gets prefilled, and on this box they
            # are the bulk of it — the primed prefix measured 27,787 tokens.
            prompt_chars += len(json.dumps(tools))
        try:
            async with (
                prefill.watch(
                    self.slots,
                    served_model,
                    prompt_chars=prompt_chars,
                    on_progress=_publish,
                ) as answered,
                httpx.AsyncClient(
                    timeout=max(self._timeout, 180.0), transport=self._transport
                ) as client,
            ):
                try:
                    resp = await client.post(
                        f"{self._root}/upstream/{served_model}/v1/chat/completions", json=body
                    )
                    resp.raise_for_status()
                finally:
                    answered()
        except httpx.HTTPError as exc:
            log.info("local_gateway.warm_skipped", model=served_model, error=str(exc))
        # Outside the `except`: a warm that FAILED still ends the load's wait, and leaving the
        # bar short on the way to a model that is nonetheless resident and serving would be a
        # worse lie than the failure it is reporting.
        await box_events.progress(1.0)

    async def tool_probe(self, served_model: str) -> None:
        """Send one tool-CARRYING completion (1 token, discarded) to verify the build's
        tool-call path doesn't crash the upstream — the post-upgrade smoke guard
        (jbrain.llm.smoketest) for the opt-in `LOCAL_LLM_AUTO_UPDATE` path. A rolling
        llama.cpp build once regressed gpt-oss's harmony tool grammar so tool turns
        returned HTTP 500; this catches that class of breakage before the box keeps the
        new build. Raises LocalGatewayError on any non-2xx or unreachable, which the
        update path reads as 'roll back to the pinned base'. A readiness probe like
        `_warm` (not a functional LLM call), so it lives here rather than on the adapter.
        The tool is enum-free on purpose (STRIX_HALO_SETUP.md's gpt-oss enum caveat)."""
        body = {
            "model": served_model,
            "messages": [{"role": "user", "content": "ping"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "noop",
                        "description": "A no-op probe tool; do not call it.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "ok": {"type": "boolean", "description": "unused probe field"}
                            },
                        },
                    },
                }
            ],
            "max_tokens": 1,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 120.0), transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._root}/upstream/{served_model}/v1/chat/completions", json=body
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalGatewayError(str(exc)) from exc

    def _record_measured_footprint(
        self, model: object, projected_gb: float, measured_gb: float | None
    ) -> None:
        """Compare what the load ACTUALLY pinned against what the catalog predicted.

        `measured_gb` is `gpu_guard.measure_footprint`'s device delta — the GTT+VRAM the
        load committed, from the samples the guard already brackets it with.

        Three attempts scraped llama.cpp's log for per-buffer figures instead, and each
        failed differently: a `/logs/upstream` path that 404s, SSE framing that was never
        there, and a pattern anchored past the engine's structured `<elapsed> <LEVEL>
        <subsystem>` prefix. The delta was in the tree the whole time — `measure_footprint`,
        whose own docstring calls it "the measurement that turns a catalog guess into a
        fact" — wired into one of six call sites.

        It is also the better number. A log parser cannot see the VISION PROJECTOR at all:
        mmproj weights print as `model size:`, not `model buffer size`, and the projector
        balloon is what every freeze on this box involved. The delta counts whatever the
        device actually pinned — projector, context checkpoints, and anything llama.cpp
        does not narrate."""
        model_id = getattr(model, "id", None)
        if model_id is None or projected_gb <= 0 or measured_gb is None:
            return
        # A resident model always pins GB, so a delta at or below zero does not mean a load
        # with no footprint — it means the model was gone by the time the second sample was
        # taken (an eviction raced the measurement, or the load unwound). OBSERVED: a load
        # cut short logged `measured_gb 0.0, drift_gb -26.59` at WARNING, which reads as a
        # catalog over-predicting by 26 GB when the truth was the exact opposite. Same class
        # of lie as the `if freed:` blind spot in _drop_weights_cache: report the miss.
        if measured_gb <= 0:
            log.info(
                "local_gateway.footprint_unmeasured",
                model=model_id,
                predicted_gb=round(projected_gb, 2),
                device_delta_gb=round(measured_gb, 2),
                reason="model not resident at sample time (eviction raced the measurement)",
            )
            return
        drift = round(measured_gb - projected_gb, 2)
        record = log.warning if abs(drift) >= _FOOTPRINT_DRIFT_GB else log.info
        record(
            "local_gateway.footprint_measured",
            model=model_id,
            predicted_gb=round(projected_gb, 2),
            measured_gb=round(measured_gb, 2),
            drift_gb=drift,
        )

    def drop_page_cache(self, model_ids: list[str] | None = None) -> dict[str, float | None]:
        """Drop the weights page cache for `model_ids`, or for EVERY catalog model when None.
        Returns {model_id: GiB freed}, with None where the drop could not be measured.

        The recovery lever for residue the automatic drops did not catch. `--no-mmap` leaves
        every model resident twice, and only the GTT copy is freed on unload; the cache copy
        survives, and `host_metrics.read_memory_gb` counts it as USED. MEASURED consequence:
        29.19 GiB of stale gpt-oss-120b cache put host pages free at 86.2 GB, and
        qwen3-coder-next-q8 — which needs ~95.5 — was refused for want of 15.3 GB that was
        not actually in use. Dropping that residue is the difference between a model this box
        can serve and one it cannot.

        Until now the only way to reclaim it was `deploy/update-inner.sh`'s global
        `drop_caches`, which needs host shell — so an owner running the box remotely could
        not do it at all (CLAUDE.md #10). This is targeted rather than global: it touches only
        weights files, so Postgres's working set and the rest of the box's cache survive.

        Safe on a RESIDENT model: `POSIX_FADV_DONTNEED` drops clean page cache, never the
        GTT copy llama-server is serving from, and weights are read-only so nothing can be
        lost. Synchronous — a handful of `posix_fadvise` calls with no I/O of their own."""
        if not self._models_dir:
            return {}
        wanted = model_ids if model_ids is not None else [m.id for m in local_catalog.CATALOG]
        freed: dict[str, float | None] = {}
        for model_id in wanted:
            got = local_weights.drop_weights_page_cache(self._models_dir, model_id)
            # Absent directories return None from the walk too, and reporting those as
            # "unmeasurable" would bury the real ones. Only provisioned models get a row.
            if got is not None or local_catalog.get(model_id) is not None:
                freed[model_id] = got
        total = sum(v for v in freed.values() if v)
        log.info(
            "local_gateway.page_cache_dropped",
            models=len(freed),
            freed_gb=round(total, 2),
            requested=("all" if model_ids is None else ",".join(model_ids)),
        )
        return freed

    async def tail_logs(self) -> str:
        """llama-swap's buffered `/logs` — its own account of the box: swap decisions,
        health checks, and the slot-acquired / slot-RELEASED lines that answer whether a
        Stop actually halts decoding.

        Deliberately NOT llama.cpp's output. Three attempts to get the engine's per-buffer
        memory figures through this surface failed, and the memory measurement now comes
        from the device delta instead (`_record_measured_footprint`), which needs no log
        route at all.

        For the record, since two of those attempts left false claims in this file: `/logs`
        is the proxy monitor and carries only llama-swap's lines; `/logs/upstream` does not
        exist; `/logs/stream/{proxy,upstream,<model>}` are `text/plain` chunked (not SSE)
        and DO replay history by default unless `?no-history` is passed. `tail_upstream_logs`
        below reads that replay, so llama.cpp's own output IS reachable — just not from here.

        Raises LocalGatewayError on failure: the operator asked, so a miss is surfaced."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/logs")
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError as exc:
            raise LocalGatewayError(str(exc)) from exc

    async def tail_upstream_logs(self, stream: str = "upstream", idle_s: float = 1.0) -> str:
        """llama-server's OWN stdout — slot lifecycle, per-request throughput, context-checkpoint
        evictions, a failed load's reason — which `tail_logs` cannot reach.

        llama-swap buffers upstream output separately from the proxy log and exposes it only
        at `/logs/stream/{proxy,upstream,<model>}`. Those are endless `text/plain` chunked
        responses, but they REPLAY the buffered history as the opening burst before going
        live, so a reader that takes the burst and hangs up gets a tail. `stream` is
        `upstream` for every model's output interleaved, or a served model id to isolate one.

        It does NOT carry the model loader's per-buffer figures, and the api.debug route's
        docstring records why: on this build the loader prints nothing at all, here or in the
        container log. The memory measurement is the device delta above, not this.

        The reader stops after `idle_s` with no new bytes rather than at a byte count: the
        burst arrives as fast as the socket allows and the silence after it is the only
        honest end-of-history marker llama-swap gives. A load in flight keeps the stream
        talking, so this is bounded overall by `self._timeout` and returns what it has.

        This exists because three attempts to measure a load's real memory from logs failed
        for want of an accessible route, and the next investigation should not have to
        rediscover it. Best-effort within reason: raises LocalGatewayError if the stream
        can't be opened at all (the operator asked), but a mid-burst disconnect returns the
        bytes already read."""
        chunks: list[str] = []
        try:
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout, read=idle_s), transport=self._transport
                ) as client,
                client.stream("GET", f"{self._root}/logs/stream/{stream}") as resp,
            ):
                resp.raise_for_status()
                try:
                    async for chunk in resp.aiter_text():
                        chunks.append(chunk)
                except httpx.HTTPError:
                    # The read timeout firing IS the end of the history burst, not a fault.
                    pass
        except httpx.HTTPError as exc:
            if not chunks:
                raise LocalGatewayError(str(exc)) from exc
        return "".join(chunks)


def parse_spec_counters(metrics_text: str) -> dict[str, float]:
    """Derive the serving numbers — speculation AND prompt-cache reuse — from llama-server's
    Prometheus text.

    The build this box runs exposes NO draft/accept counters — the whole metric set is
    prompt/predict/decode totals — so the acceptance rate cannot be read directly. It can be
    DERIVED, and the derived form is the better measure anyway:

      tokens_per_step = tokens_predicted_total / n_decode_total

    `n_decode_total` counts llama_decode() calls (forward passes); `tokens_predicted_total`
    counts tokens actually emitted. Without speculation the ratio is 1.0. Above 1.0 is
    speculation landing, and on a bandwidth-bound box — where a forward pass costs one full
    read of the weights — that ratio IS the speedup.

    `tokens_per_second` comes from the cumulative predict-time counter, so it is decode
    throughput rather than a wall-clock average that would include prompt processing.

    CAUTION: both are process-lifetime totals. To measure one request, read before and after
    and divide the deltas — a lifetime figure includes warm-up and every prior request.
    Optimise on `tokens_per_second`, NOT `tokens_per_step`: deeper drafts raise tokens/step
    while lowering throughput, because verifying a longer draft costs more per pass. Measured
    here, --spec-draft-n-max 7 reached 2.381 tokens/step and only 8.38 t/s, against 2.454 and
    20.86 t/s at the default of 3.

    Any genuine draft/accept counters are still passed through for a build that grows them,
    matched by SUBSTRING because llama.cpp renames metrics and this box tracks master."""
    v: dict[str, float] = {}
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        try:
            v[name.split("{")[0]] = float(value)
        except ValueError:
            continue
    out = {k: n for k, n in v.items() if any(t in k for t in ("draft", "spec", "accept"))}
    # PROMPT-CACHE counters, and they are the authoritative reuse signal on this box. The
    # per-slot `n_prompt_tokens_cache` in /slots is NOT: llama.cpp zeroes a slot's stats on
    # release, so polling it after a request reads 0 whether reuse was total or nonexistent —
    # which is how an entire investigation concluded a hybrid could not cache at all. These are
    # cumulative and survive release, so a before/after delta around one request is the truth.
    cached = v.get("llamacpp:prompt_tokens_cached_total")
    processed = v.get("llamacpp:prompt_tokens_total")
    if cached is not None:
        out["prompt_tokens_cached_total"] = cached
    if processed is not None:
        out["prompt_tokens_total"] = processed
    if cached is not None and processed is not None and (cached + processed) > 0:
        # Lifetime reuse fraction. Like everything here it is a process-lifetime figure — delta
        # it across one request to measure that request.
        out["cache_hit_rate"] = round(cached / (cached + processed), 4)
    decodes = v.get("llamacpp:n_decode_total", 0.0)
    tokens = v.get("llamacpp:tokens_predicted_total", 0.0)
    seconds = v.get("llamacpp:tokens_predicted_seconds_total", 0.0)
    if decodes > 0:
        out["tokens_per_step"] = round(tokens / decodes, 4)
    if seconds > 0:
        out["tokens_per_second"] = round(tokens / seconds, 3)
    drafted = next((n for k, n in out.items() if "draft" in k and "accept" not in k), None)
    accepted = next((n for k, n in out.items() if "accept" in k), None)
    if drafted and accepted is not None:
        out["accept_rate"] = round(accepted / drafted, 4)
    return out


def _parse_running(payload: object) -> set[str]:
    """Tolerant parse of /running across llama-swap versions: accept a bare list,
    or an object wrapping the list under a common key; pull a model name from each
    item whether it's a string or an object."""
    items: object = payload
    if isinstance(payload, dict):
        items = next(
            (payload[k] for k in ("running", "models", "data") if isinstance(payload.get(k), list)),
            [],
        )
    out: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict):
                name = next(
                    (item[k] for k in ("model", "id", "name") if isinstance(item.get(k), str)),
                    None,
                )
                if name:
                    out.add(name)
    return out
