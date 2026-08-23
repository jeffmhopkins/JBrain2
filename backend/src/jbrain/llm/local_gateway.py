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
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol

import httpx
import structlog

from jbrain import box_events, host_metrics
from jbrain.llm import gpu_guard, local_catalog, local_weights, openai_compat, prefill
from jbrain.llm.admission import Outcome, Phase
from jbrain.llm.ledger import ReservationLedger

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


# llama-swap's own name for a model that is up and serving. Its process states are
# `stopped, starting, ready, stopping, shutdown` (`internal/process/process.go`), and
# `/running` filters only the first and last — so `starting` and `stopping` both reach us
# and neither is serving. Confirmed at the pin in `deploy/Dockerfile.local-llm`.
STATE_READY = "ready"  # llama-swap ProcessState; shared with residency so it is spelled once
# The one that is NOT resident and NOT absent. A model llama-swap is stopping is still listed
# by `/running`, so "in running()" reads it as up — and a health GET against it does not join
# a live process, it makes llama-swap RELAUNCH one. See `_settle_a_stopping_model`.
STATE_STOPPING = "stopping"

# How long to wait for a stop to finish before refusing the load.
#
# NOT sized off llama-swap's `DEFAULT_UNLOAD_TIMEOUT = 10`, which an earlier version of this
# doubled and called a bound. That figure is when llama-swap sends SIGKILL, not when the kernel
# has finished tearing down 85 GB of pinned GTT — and `/running` is waiting on the latter. The
# case that decides the number is a config regen: `regen_gateway_config` reloads llama-swap,
# which kills EVERY running llama-server, and then waits a fixed 4 s because llama-swap exposes
# no reload-done signal to poll. So the owner's Load button can arrive with an 85 GB server four
# seconds into its teardown on a memory-pressured box. A minute is long enough for that to be a
# real refusal rather than an impatient one, and short enough that the owner is told rather than
# left watching a spinner.
STOP_SETTLE_TIMEOUT_S = 60.0
STOP_SETTLE_POLL_S = 0.5


class LocalGatewayError(Exception):
    """A load/unload call the gateway rejected or couldn't be reached for."""


class LocalGateway(Protocol):
    """The runtime-state surface consumers depend on (report/unload/load), so a
    caller takes the capability rather than the concrete HTTP client — the in-memory
    test fake satisfies it structurally, the same seam as the `ImageGen` protocol."""

    async def running(self) -> set[str]: ...

    async def unload(self, served_model: str) -> None: ...

    async def load(self, served_model: str) -> None: ...

    def state_of(self, served_model: str) -> str:
        """The state `/running` last reported. REQUIRED, not optional: residency narrates
        `short_circuit_not_ready` from it, and that counter is the evidence W0's
        `running()`/`ready()` split is gated on. A gateway without it would make the counter
        read zero — indistinguishable from "the bug is not happening" — so absence is a type
        error rather than a silent all-clear, for the same reason `ResidencyWiring` has no
        defaults. `""` means "no state known", which is never read as ready."""
        ...


class LocalGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
        gpu_probe: gpu_guard.GpuMemProbe | None = None,
        reservations: ReservationLedger | None = None,
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
        # The reservation ledger (jbrain.llm.ledger), or None on a box without a database.
        # IN SHADOW while it is being characterised: it charges and releases against the real
        # load lifecycle and records what it WOULD have decided, and decides nothing. See
        # `_reservation`.
        self._reservations = reservations
        # What /running last reported, name -> state, and the non-ready subset we have
        # already narrated. Cached so a caller can ask `state_of` without a second round
        # trip; see `_note_not_ready`.
        self._last_states: dict[str, str] = {}
        self._last_not_ready: dict[str, str] = {}
        # One lock per served model, so two callers cannot load the same one at once. See
        # `load` for what that cost the owner without it.
        self._load_locks: dict[str, asyncio.Lock] = {}
        # And ONE lock across all of them, so two DIFFERENT models never load concurrently.
        #
        # The runaway watchdog anchors its ceiling to a GTT baseline sampled when the load
        # starts (`gpu_guard.guarded_load`), which is only meaningful if nothing else is still
        # allocating. MEASURED 2026-08-21: gpt-oss-120b was 30 s into a reload — GTT at 36.4 GB
        # on its way to a measured 69.24 — when a staged qwen3.8-27b-abliterated sampled that
        # 36.4 as ITS baseline and set a ceiling of 78.6. GTT then reached 79.9, which was
        # gpt-oss finishing plus abliterated starting, and the guard attributed the whole climb
        # to abliterated and aborted it: "device memory ran away ... for a model predicted at
        # 24.1 GB". Nothing ran away. The previous model was still arriving.
        #
        # A false abort is not cheap here. It unloads a model that was fine, and it strands the
        # weights it had already read in the page cache, which `read_memory_gb` counts as used
        # — so the next load sees less headroom and is likelier to abort in turn.
        #
        # Serialising is the honest fix rather than widening the multiple: the ceiling's job is
        # to catch an order-of-magnitude balloon, and loosening it to absorb a second model's
        # allocation would blind it to exactly what it exists for. It also halves the transient
        # page cache, since only one model is reading weights at a time. Loads are the box's
        # rare, expensive operation — a queued one costs latency, never correctness — and this
        # is precisely the co-residency path, where a second model is staged right after a first.
        self._global_load_lock = asyncio.Lock()
        # Which model currently holds that lock, so a caller that has to wait can SAY what it
        # is waiting for. A silent wait is the failure mode here: the owner drives this box
        # through the PWA (CLAUDE.md #10), and a Load button that does nothing for three
        # minutes is indistinguishable from one that is broken.
        self._loading_now: str | None = None
        # Models THIS client has a load in flight for. Distinct from `_loaded_here`, which is
        # pruned to what is actually resident: a load in flight is by definition not resident
        # yet, so it needs a claim that survives the prune. Without this, every guarded load of
        # a cold model reported itself as unannounced.
        self._loading: set[str] = set()
        # Which client this is, so a cross-process sighting is legible as one. Two long-lived
        # clients run on this box (api, worker) and each sees the other's loads as unannounced.
        self._client_id = f"{os.getpid()}:{id(self):x}"
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
        # Whether this client has polled AT ALL. Distinct from `_seen_resident` being empty,
        # which is also true of an idle box: an api restart with nothing resident, then a
        # request-driven load, would otherwise stamp the arrival `first_poll=true` and the
        # runbook says to dismiss those — discarding the bypass signal in the one case it is
        # reporting a real one.
        self._polled = False
        self._loaded_here: set[str] = set()

    async def running(self) -> set[str]:
        """Served-model names currently loaded, or an empty set on ANY failure
        (unreachable, non-2xx, malformed, or an old build without /running).

        Also the observation point for a model that arrived WITHOUT us: see
        `_drop_cache_for_unannounced`."""
        return set(await self.running_states() or ())

    async def running_states(self) -> dict[str, str] | None:
        """`running()` with the states kept — name -> llama-swap ProcessState, `""` when the
        build reports none. **None means the read FAILED**, which an empty dict does not: an
        empty dict is a box with nothing loaded, and a caller that cannot tell those apart
        decides "the stop landed, the box is clear" off a dropped connection.

        Returned rather than only cached because `state_of` reads PROCESS-GLOBAL state that
        every caller of this method overwrites, and this client is shared: `warm_keeper`
        reconciles on its own loop against the same instance. A caller that tests the cache and
        then acts on its own older snapshot is reading two different observations as one, which
        is how a stopping model could still take the already-resident free pass. Anyone whose
        decision spans more than one poll takes the dict."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/running")
                resp.raise_for_status()
                states = _parse_running_states(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.info("local_gateway.running_unavailable", error=str(exc))
            return None
        self._last_states = dict(states)
        self._note_not_ready(states)
        resident = set(states)
        self._drop_cache_for_unannounced(resident)
        return dict(states)

    def _note_not_ready(self, states: Mapping[str, str]) -> None:
        """Record — and narrate once per transition — any model /running lists in a state
        that is not `ready`.

        MEASUREMENT, not yet a behaviour change. `_parse_running_states` explains why a
        non-ready model in this list is a hole; what nobody has measured is how often the
        window is actually open on this box, and for which state. Landing the counter first
        is deliberate: the fix removes the thing being measured, so shipping both together
        would only ever report zero. A predecessor plan made this log the trigger for a wave
        scheduled last, so the evidence could never be gathered at all.

        Logged on change rather than per poll: `running()` is called on every poller tick in
        both processes, so an unconditional line here would bury the transition it exists to
        show."""
        not_ready = {n: st for n, st in states.items() if st and st != STATE_READY}
        if not_ready != self._last_not_ready:
            if not_ready:
                log.info(
                    "local_gateway.not_ready_in_running",
                    models=sorted(not_ready),
                    states=sorted(set(not_ready.values())),
                    client=self._client_id,
                )
            self._last_not_ready = dict(not_ready)

    def state_of(self, served_model: str) -> str:
        """The state /running last reported for a model — `""` when it was absent from that
        response, or when the gateway build reports no state at all. Best-effort and cached
        from the most recent `running()` in this process, so a caller can narrate WHY it
        treated a model as resident without paying a second round trip.

        `""` is deliberately indistinguishable between "not listed" and "build reports no
        state": both mean *we do not know*, and a caller must not read either as `ready`."""
        return self._last_states.get(served_model, "")

    async def _settle_a_stopping_model(
        self, served_model: str, states: Mapping[str, str]
    ) -> set[str] | None:
        """Wait out a stop in progress, so the load that follows decides against a settled box.

        THE HOLE THIS CLOSES. `/running` filters only `stopped` and `shutdown`, so a model
        llama-swap is in the middle of stopping is still listed — and `_load_and_warm`'s
        already-resident branch reads that list. Taking that branch skips BOTH the device
        pre-flight and the runaway watchdog, on the premise that a resident model needs no room
        for a second copy of itself. For a STOPPING model that premise is false twice over: the
        process is going away, and the health GET the branch then issues makes llama-swap
        launch a fresh one. A completely unguarded load, on the one box where an unadmitted
        load is a power cycle.

        WAIT, do not re-decide. Routing a stopping model to the guarded path instead would ask
        for room while the dying model's own footprint is still charged to the device pool —
        counting one model twice, which is the exact error three earlier attempts at this made
        (see `docs/plans/LOCAL_MODEL_LEDGER_PLAN.md`). Waiting removes the ambiguity rather
        than arbitrating it: once the stop lands, "resident" and "absent" mean what they say
        and every existing number is right.

        A stop that never lands is a REFUSAL, not a fallthrough — the fallthrough is the
        unguarded load this exists to prevent. Retryable on purpose: the caller's next attempt
        meets a settled box.

        WHAT IT DOES NOT COVER, said plainly. It reads `state_of`, which is `""` when the
        build reports no state at all — and `""` means *we do not know*, so a stopping model on
        such a build still reads as resident and still takes the free pass. This narrows the
        hole to builds that report states (the pinned one does); it does not remove it. The
        removal is L2's ledger, which does not ask the gateway what is resident.

        `starting` is deliberately NOT waited on — but NOT because "another loader already
        admitted it", which this said first and which is false: llama-swap loads on REQUEST, so
        most loads never touch this client at all (`_drop_cache_for_unannounced` exists because
        of that, and measured `Cached` climbing 2.36 -> 47.83 GiB with nothing logged). The
        actual reason is narrower and still holds: a `starting` process EXISTS and is already
        allocating, so the health GET joins it rather than launching a second one — which is the
        specific harm this method is here to prevent. Whether that load was admitted is a real
        question, and it is the ledger's, not this method's.

        It waits for the TARGET only. A DIFFERENT model stuck in `stopping` still has its
        footprint charged to the device pool when the pre-flight samples, so that model is still
        counted twice — a spurious refusal, which is the safe direction, and the one this cannot
        fix without the ledger.

        `states` is the caller's own `running_states()` reading, passed in rather than read
        from the cache for the reason above.

        Returns the fresh `/running` set when it waited (the caller's own read is stale by
        exactly the stop it just waited out), or None when there was nothing to wait for.
        """
        if states.get(served_model) != STATE_STOPPING:
            return None
        deadline = asyncio.get_running_loop().time() + STOP_SETTLE_TIMEOUT_S
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise LocalGatewayError(
                    f"{served_model} has been stopping for over "
                    f"{STOP_SETTLE_TIMEOUT_S:.0f}s — refusing to load it on top of a process "
                    "that is still holding its memory. Try again in a moment."
                )
            await asyncio.sleep(STOP_SETTLE_POLL_S)
            # ONE observation decides both the exit and the answer. Re-reading `state_of` here
            # would test a cache any concurrent `running()` on this shared client may have
            # rewritten, and then return a set from a different moment — two observations worn
            # as one, which puts the stopping model straight back on the resident free pass.
            polled = await self.running_states()
            if polled is None:
                continue  # a dropped poll is not a settled stop; wait for a real reading
            if polled.get(served_model) != STATE_STOPPING:
                log.info("local_gateway.stop_settled", model=served_model)
                return set(polled)

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
        loaded ourselves are skipped — `load()` already dropped theirs.

        THE LABEL IS NOT PROOF OF A BYPASS, and two live false positives are why the event now
        says so itself.

        1. A load in flight used to report ITSELF. `load()` claims the model before the weights
           are read, and the prune at the end of this method ran while it was still not
           resident — dropping the claim moments before it arrived, so the warm-up's own
           `/slots` poll saw it arrive un-owned. MEASURED 2026-08-21: `load_cache_swept
           qwen3.8-27b-q4` at 21:41:14.804 then `unannounced_load qwen3.8-27b-q4` at
           21:41:17.873 — same client, three seconds apart, one load. `_loading` now holds the
           claim for the duration and the prune spares it.

        2. Another PROCESS's load is unannounced here by construction. `_loaded_here` is per
           client instance and this box runs two (api, worker), so each reports the other's
           work — and a fresh client reports every already-resident model on its first poll,
           because `_seen_resident` starts empty. MEASURED the same day: the worker logged
           gpt-oss-120b and qwen3.8-27b-q4 at the same millisecond (21:41:23.348/.349), both
           loaded by the api minutes earlier.

        So the event carries the client's identity and whether this was its first poll. An
        investigation that reads an un-annotated line as an unguarded load chases its own
        tail — this one cost a wrong diagnosis before the annotation existed.

        The page-cache drop stays unconditional: it is idempotent, and sweeping a cache we did
        not strand costs one `posix_fadvise` over already-evicted files."""
        if not self._models_dir:
            return
        arrived = resident - self._seen_resident
        first_poll = not self._polled
        self._polled = True
        self._seen_resident = set(resident)
        for served in sorted(arrived - self._loaded_here - self._loading):
            model = local_catalog.get_by_served(served)
            if model is not None:
                log.info(
                    "local_gateway.unannounced_load",
                    model=model.id,
                    client=self._client_id,
                    first_poll=first_poll,
                )
                self._drop_weights_cache(model)
        # Forget our own COMPLETED loads once they are gone, so a later request-driven reload
        # of the same model is treated as unannounced (it is). A load still IN FLIGHT is spared:
        # it is not resident yet, and `_load_and_warm` itself calls `running()` between claiming
        # the model and it arriving. Pruning on that call destroyed the durable claim while
        # `_loading` masked the symptom — so the false alarm surfaced later, on the first poll
        # after `load()` released `_loading`, as `unannounced_load … first_poll=false` for this
        # client's own guarded load. That is the reading DEBUG_ACCESS.md calls a real bypass.
        self._loaded_here &= resident | self._loading

    async def unload(self, served_model: str) -> None:
        """Unload one model from memory. Raises LocalGatewayError on any failure.

        Narrated to the vitals surface (jbrain.box_events) from HERE rather than from the
        six callers, for the same reason the device-memory guard lives on `load`: this is
        the one chokepoint every path to freeing a model passes through, so instrumenting
        it leaves nothing to forget. WHY it is being unloaded rides in on the caller's
        `box_events.because(...)` — "to make room for gpt-oss-120b", "an image render
        needs the box" — which is the difference between a log and an explanation.

        The timeout is WIDENED, like every other slow call on this client. llama-swap's
        `/api/models/unload/{model}` BLOCKS until the process has actually stopped
        (`internal/router/router.go`: "It blocks until each targeted process has stopped"),
        and it grants each one `DEFAULT_UNLOAD_TIMEOUT = 10` seconds of graceful stop before
        escalating — a figure the generated config never overrides. Against that, the client
        default of 3 s is not a timeout, it is a coin flip on a 69 GB model.

        What that cost, all of it while the unload was SUCCEEDING underneath: a
        `MODEL_UNLOAD status="failed"` row on the vitals surface the owner reads; a
        `LocalGatewayError` that makes `residency`'s next plan still count the victim's
        footprint as resident, which is an eviction-budget error from a direction the budget
        cannot see; `image_gen.render` abandoning the REST of its unload loop on the first
        slow model, so a ComfyUI render begins with models still holding the pool; and
        `cli.py`'s pre-update unload reporting failure on a success inside `set -e`.

        An earlier version of this docstring claimed the call "manufactures a `stopping`
        window: control returns to the caller while the process is genuinely still stopping",
        and six callers were reasoned about on that basis. It is NOT true of the pinned
        llama-swap (`60226b6`, v250): `Process.Stop` sets `StateStopped` BEFORE responding
        (`process_command.go:388` then `:390`), the router's `OnUnload` stops synchronously so
        that "after Unload returns, the process is stopped" (`fifo.go:258-262`), and only then
        does the handler write 200 (`apigroup.go:156-158`).

        **A 200 from here means the child is reaped and its memory released.** The `stopping`
        window is real but comes from elsewhere: a config reload killing every server, a stop
        another process initiated, or THIS CALL TIMING OUT — a client that gives up early
        returns to a model that is still stopping, which is precisely what the 3 s timeout
        used to do against a 10 s graceful stop. Widened rather than made unbounded, because a
        genuinely wedged llama-swap must still surface as an error rather than hang a
        request."""
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 30.0), transport=self._transport
            ) as client:
                if self._reservations is not None:
                    # Before the call, and it KEEPS THE FULL CHARGE. Discharging on the
                    # shutdown intent is the one anti-pattern every prior-art scheduler names,
                    # because the kernel has freed nothing at the moment the request is made.
                    await self._reservations.draining(served_model)
                resp = await client.post(f"{self._root}/api/models/unload/{served_model}")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            await box_events.record(
                box_events.MODEL_UNLOAD, served_model, status="failed", detail=str(exc)
            )
            # The rows stay DRAINING and stay charged: a failed unload is a model that may
            # well still be running, and the TTL sweep is what eventually decides otherwise.
            raise LocalGatewayError(str(exc)) from exc
        if self._reservations is not None:
            # CONFIRMED DEATH. This endpoint blocks until each targeted process has stopped, so
            # a 200 is the one moment this codebase can honestly say the memory is back.
            await self._reservations.discharge_model(served_model)
        await box_events.record(box_events.MODEL_UNLOAD, served_model)

    async def _narrate_reload_casualties(self, before: set[str], loading: str) -> None:
        """Record the models a config-driven gateway reload just killed.

        Rewriting llama-swap.yaml makes llama-swap reload, and its reload calls
        `old.Shutdown()` — killing EVERY running llama-server, not only the one whose setting
        changed. `regen_gateway_config` waits for that reload to land before returning, so by
        the time we get here the casualties are observed fact, not a prediction.

        SURVIVORS ARE THE READY ONES, not everything `/running` lists. `/running` filters only
        `stopped` and `shutdown`, so a server llama-swap is four seconds into killing is still
        in that list — and subtracting it as a survivor is how this method came to report an
        empty casualty list in exactly the case it was written for. `regen_gateway_config`'s
        wait is a fixed 4 s sleep (llama-swap exposes no reload-done signal to poll), which is
        nowhere near an 85 GB teardown, so this is the common shape of a casualty.

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
            states = await self.running_states()
            if states is None:
                # A failed poll is not a casualty list. A phantom eviction row is worse than a
                # missing one: it teaches the operator to ignore the row that matters.
                return
            survived = {n for n, st in states.items() if st != STATE_STOPPING}
            for served in sorted(before - survived - {loading}):
                await box_events.record(box_events.MODEL_UNLOAD, served, detail=reason)
                if self._reservations is not None:
                    # THE ONE EVICTION NOBODY ELSE REPORTS. llama-swap's reload kills these
                    # inside itself, so no `unload()` runs and nothing would ever release their
                    # charges — they would sit RESIDENT until a process restart reconciled them,
                    # shrinking the budget the whole time. This narration is already the only
                    # place that knows they died; releasing here is the same fact, acted on.
                    await self._reservations.discharge_model(served)

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

    async def save_slot(self, served_model: str, slot_id: int, filename: str) -> dict:
        """Ask llama-server to write one slot's KV state to `filename` under its
        --slot-save-path. The response's `n_saved` is the caller's verification hook —
        `jbrain.llm.kv_prefix` refuses to keep a file whose count does not exactly match
        the prime it believes it captured (the v1 failure was trusting this blindly)."""
        body = await self._upstream_post(
            served_model, "slots-save", f"/slots/{slot_id}?action=save", {"filename": filename}
        )
        return body if isinstance(body, dict) else {}

    async def restore_slot(self, served_model: str, slot_id: int, filename: str) -> dict:
        """Restore a saved KV state into one slot; `n_restored` verifies it. ~2 s for the
        ~2 GiB jerv prefix off NVMe, against the ~60 s prefill it replaces."""
        body = await self._upstream_post(
            served_model,
            "slots-restore",
            f"/slots/{slot_id}?action=restore",
            {"filename": filename},
        )
        return body if isinstance(body, dict) else {}

    async def _upstream_post(
        self, served_model: str, what: str, path: str, payload: dict
    ) -> object:
        await self._require_resident(served_model, what)
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 180.0), transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._root}/upstream/{served_model}{path}", json=payload
                )
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalGatewayError(str(exc)) from exc

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
        warm_reasoning_effort: str | None = None,
        before_warm: Callable[[], Awaitable[object]] | None = None,
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
            # QUEUE, not just a gate. Say so before blocking, and name what is ahead: this is
            # the co-residency path — stage a second model right after a first — and a wait of
            # two or three minutes with nothing on screen reads as a hung button.
            if self._global_load_lock.locked():
                ahead = self._loading_now
                log.info("local_gateway.load_queued", model=served_model, behind=ahead)
                await box_events.record(
                    box_events.MODEL_LOAD,
                    served_model,
                    detail=f"queued — waiting for {ahead} to finish loading"
                    if ahead
                    else "queued — waiting for another load to finish",
                )
            async with self._global_load_lock:
                self._loading_now = served_model
                self._loading.add(served_model)
                try:
                    async with box_events.span(box_events.MODEL_LOAD, served_model):
                        await self._load_and_warm(
                            served_model,
                            warm_system=warm_system,
                            warm_tools=warm_tools,
                            warm_reasoning_effort=warm_reasoning_effort,
                            before_warm=before_warm,
                        )
                finally:
                    self._loading_now = None
                    self._loading.discard(served_model)

    @contextlib.asynccontextmanager
    async def _reservation(
        self, served_model: str, model: local_catalog.LocalModel | None, window: int, slots: int
    ) -> AsyncIterator[None]:
        """Charge the ledger for the load about to run, and release it if the load fails.

        The declaration is written down ONCE here and never recomputed for this instance —
        the arithmetic that admitted a model is the arithmetic that protects it. `STARTING`
        goes in before the load rather than after, because the phase describes what the process
        is about to do and the TTL sweep needs to know a long load is legitimately in progress.

        RELEASE ON ANY FAILURE, including cancellation: a charge with no process behind it
        shrinks the budget permanently, and the box would slowly refuse everything. The TTL is
        the backstop for a process that dies before it gets here, not the mechanism.

        A no-op when no ledger is wired (a cloud-only box, tests, the CLI) or when the model is
        not in the catalog, because a declaration is the one thing that cannot be guessed."""
        ledger = self._reservations
        if ledger is None or model is None:
            yield
            return
        host_gb, device_gb = local_catalog.declared_gb(model, window, slots=slots)
        charge = await ledger.charge(served_model, host_gb=host_gb, device_gb=device_gb)
        if charge.instance_id is None:
            # Only reachable once the ledger is authoritative; in shadow it always charges.
            # `GpuBudgetError` on purpose rather than a new class: every caller of `load`
            # already handles it — 409 on the settings screen, a defer in the worker, a
            # suppression on the restore — so making the ledger speak the language the box
            # already understands is what lets L2b be a one-line change rather than a sweep.
            # The outcome rides along because that language was one word short: the worker's
            # defer is right for DEFERRED and a forever-loop for INFEASIBLE, which by
            # definition no eviction can satisfy.
            raise gpu_guard.GpuBudgetError(
                charge.decision.reason,
                permanent=charge.decision.outcome is Outcome.INFEASIBLE,
            )
        await ledger.advance(charge.instance_id, Phase.STARTING)
        try:
            yield
        except BaseException:
            await ledger.discharge(charge.instance_id)
            raise
        await ledger.advance(charge.instance_id, Phase.RESIDENT)

    async def _load_and_warm(
        self,
        served_model: str,
        *,
        warm_system: str | None = None,
        warm_tools: list[dict[str, object]] | None = None,
        warm_reasoning_effort: str | None = None,
        before_warm: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        """`load` minus its narration: the health probe that makes llama-swap read the
        weights, the device-memory guard around it, and the inference warm-up."""
        load_timeout = max(self._timeout, 120.0)
        model = local_catalog.get_by_served(served_model)
        projected_gb = 0.0
        window, slots = 0, 1
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
        # success, keeps a failed load from being dropped twice. Ordered before the poll below
        # for tidiness rather than for safety: `_loading` already covers this window (`load`
        # adds to it before calling here, and the prune is `_loaded_here &= resident |
        # _loading`), so the false `unannounced_load` this looks like it prevents was already
        # prevented.
        self._loaded_here.add(served_model)

        # A stop in flight makes both branches below wrong (see `_settle_a_stopping_model`),
        # and it is read AFTER `config_regen` because a config reload kills every running
        # llama-server — so the state this waits on is the one the load will actually meet.
        states = await self.running_states() or {}
        resident = set(states)
        # `is not None`, NOT `or`: an EMPTY set is the most important answer this can give —
        # the stop landed and the box is now clear — and `or` would discard it for the stale
        # read that still lists the model as up.
        settled = await self._settle_a_stopping_model(served_model, states)
        if settled is not None:
            resident = settled

        if self._gpu_probe is None:  # no probe wired: the prior, unguarded behaviour
            async with self._reservation(served_model, model, window, slots):
                try:
                    await _do_load()
                finally:
                    # `finally`, not the next line: a load that raises has still READ the
                    # weights, so its page-cache copy exists and nothing else will ever drop
                    # it. See the guarded branch below for the measurement that proved it.
                    self._drop_weights_cache(model)
                await self._warm(
                    served_model,
                    system=warm_system,
                    tools=warm_tools,
                    reasoning_effort=warm_reasoning_effort,
                    before_warm=before_warm,
                )
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
        if served_model in resident:
            try:
                await _do_load()
            finally:
                self._drop_weights_cache(model)
            await self._warm(
                served_model,
                system=warm_system,
                tools=warm_tools,
                reasoning_effort=warm_reasoning_effort,
                before_warm=before_warm,
            )
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

        async def _load_then_warm() -> None:
            # The WARM RUNS INSIDE THE WATCHDOG. It used to sit after `guarded_load` returned,
            # which left the phase that allocates the KV cache and the graph-capture buffers
            # with nothing sampling the device pool — MEASURED at 118 s of a 198 s cold
            # gpt-oss-120b, so roughly 60% of a cold load was unwatched, and it was the 60%
            # doing the allocating. The pre-flight already admitted the full footprint (weights
            # + KV + projector), so `guarded_load`'s ceiling covers the warm as it stands; only
            # the watching stopped early.
            await _do_load()
            # Between the load and the warm, exactly where it was: the warm's allocations
            # should meet the memory this returns rather than race it. See
            # `_drop_weights_cache`.
            self._drop_weights_cache(model)
            await self._warm(
                served_model,
                system=warm_system,
                tools=warm_tools,
                reasoning_effort=warm_reasoning_effort,
                before_warm=before_warm,
            )

        try:
            # The reservation wraps the WATCHDOG, not the other way round: an aborted load has
            # its charge released by the same failure path as any other, and the abort's unload
            # runs inside the charge rather than after it has gone.
            async with self._reservation(served_model, model, window, slots):
                await gpu_guard.guarded_load(
                    _load_then_warm,
                    probe=self._gpu_probe,
                    projected_gb=projected_gb,
                    target=served_model,
                    abort=lambda: self.unload(served_model),
                )
        except BaseException:
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
            #
            # UNCONDITIONAL, and on `except` rather than `finally`. A flag that skipped this
            # when the inner drop had already run got the one path that matters backwards: a
            # breach DURING THE WARM drops inside `_load_then_warm` while llama-server still
            # holds the weight file, and then `abort()` unloads — so the only effective drop
            # would be the one the flag suppressed. Dropping twice on that path costs a second
            # sweep of `posix_fadvise` calls over already-evicted files; not dropping once
            # strands the whole weight file in `Cached`, which is the ratchet above.
            self._drop_weights_cache(model)
            raise
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
        reasoning_effort: str | None = None,
        before_warm: Callable[[], Awaitable[object]] | None = None,
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
        # A restore hook runs FIRST: with a saved KV slot on disk, putting it back before
        # this request turns the prefill below into a cache hit — and the order matters on a
        # single-slot server, where a full warm prefill would CLOBBER a restored cache
        # instead of reusing it. Best-effort like everything else here.
        if before_warm is not None:
            try:
                await before_warm()
            except Exception:  # noqa: BLE001 — a failed restore just means a real prefill
                log.warning("local_gateway.before_warm_failed", model=served_model, exc_info=True)
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
        # The SAME reasoning encoding a real routed turn carries (openai_compat): the chat
        # template renders it into the prompt's leading tokens, so omitting it here primed a
        # DIFFERENT prefix from the one every turn actually sends — a warm that warmed
        # nothing, at full prefill cost (observed live 2026-08-23, ~62 s per load).
        openai_compat.apply_local_reasoning(body, reasoning_effort)

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
    """Served-model names from /running, discarding the state. See
    `_parse_running_states` for why the state is worth keeping."""
    return set(_parse_running_states(payload))


def _parse_running_states(payload: object) -> dict[str, str]:
    """Tolerant parse of /running across llama-swap versions: accept a bare list,
    or an object wrapping the list under a common key; pull a model name from each
    item whether it's a string or an object. Maps name -> reported state, or "" when
    the build reports none.

    The state matters and was being thrown away. llama-swap's `RunningModels` filters
    only `stopped` and `shutdown` (`internal/router/base.go`, confirmed at the pin in
    `deploy/Dockerfile.local-llm`), so a model it is in the middle of **stopping** is
    still listed here. Every caller that reads this as "resident" therefore treats a
    model on its way out as one that is up: residency's already-resident short-circuit
    returns without admitting, and `model_already_loaded` reports its precondition met.
    The completion then reaches llama-swap, which relaunches the model — a load with no
    admission, no device guard and no `box_events` row."""
    items: object = payload
    if isinstance(payload, dict):
        items = next(
            (payload[k] for k in ("running", "models", "data") if isinstance(payload.get(k), list)),
            [],
        )
    out: dict[str, str] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                out[item] = ""
            elif isinstance(item, dict):
                name = next(
                    (item[k] for k in ("model", "id", "name") if isinstance(item.get(k), str)),
                    None,
                )
                if name:
                    state = next(
                        (item[k] for k in ("state", "status") if isinstance(item.get(k), str)),
                        "",
                    )
                    out[name] = state
    return out
