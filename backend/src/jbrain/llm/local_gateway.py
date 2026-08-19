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
  - GET  /logs                         → recent gateway + upstream stdout, which the
                                         loading bar mines for the llama.cpp model-load
                                         percentage (a real "weights read in" signal,
                                         since we run --no-mmap)

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
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import httpx
import structlog

from jbrain import box_events
from jbrain.llm import gpu_guard, local_catalog, local_weights, memory_report

log = structlog.get_logger()

# A catalog entry wrong by this much is worth waking someone for: the two found on
# 2026-08-19 were light by 1.4 and >5.5 GiB, and the smaller of those was enough to abort
# a healthy load. Below it, drift is ordinary per-build variation and is logged at info.
_FOOTPRINT_DRIFT_GB = 1.0

# How long to wait for the upstream log stream to attach before loading anyway. Short
# because it is pure instrumentation: a build without the route sets the event immediately
# on its 404, and the only case that spends the full budget is a gateway already too sick
# to answer — where delaying the load further helps nobody.
_UPSTREAM_ATTACH_S = 2.0


class LocalGatewayError(Exception):
    """A load/unload call the gateway rejected or couldn't be reached for."""


class LocalGateway(Protocol):
    """The runtime-state surface consumers depend on (report/unload/load), so a
    caller takes the capability rather than the concrete HTTP client — the in-memory
    test fake satisfies it structurally, the same seam as the `ImageGen` protocol."""

    async def running(self) -> set[str]: ...

    async def unload(self, served_model: str) -> None: ...

    async def load(self, served_model: str) -> None: ...

    # NOTE: load_progress() is deliberately NOT on this protocol. It's an optional,
    # best-effort extension only the jcode status probes (via getattr), so keeping it off
    # the protocol lets the many structural test fakes satisfy LocalGateway without each
    # having to stub it. Add it here only if a typed caller must depend on it.


class LocalGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
        gpu_probe: gpu_guard.GpuMemProbe | None = None,
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
        # The live per-model context-window and parallel-slot overrides (Settings → LLM), read
        # per load. They live here for the same chokepoint reason as the probe: KV is LINEAR in
        # the window, so a guard that reserves for the catalog default while llama-swap serves
        # an override is not sized for the load it is guarding. Measured: the abliterated 27B
        # served at `-c 262144` against a 32768 catalog default reserved 20.29 GB for a load
        # that took 36.92 GB. Unset (the tests, the CLI without a settings context) falls back
        # to the catalog default and one slot, which is what an unconfigured box serves.
        self._windows_loader = windows_loader
        self._slots_loader = slots_loader
        # Where the weights live, so a finished load can drop their PAGE-CACHE copy. Same
        # chokepoint reasoning again: `--no-mmap` leaves every model resident twice — once in
        # GTT, once in the cache the read filled — and the copy that is invisible to
        # `MemAvailable` is the one that killed this host (jbrain.llm.local_weights
        # .drop_weights_page_cache). Unset (a container without the weights mount, the tests)
        # skips the drop and keeps the prior behaviour.
        self._models_dir = models_dir

    async def running(self) -> set[str]:
        """Served-model names currently loaded, or an empty set on ANY failure
        (unreachable, non-2xx, malformed, or an old build without /running)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/running")
                resp.raise_for_status()
                return _parse_running(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.info("local_gateway.running_unavailable", error=str(exc))
            return set()

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

    async def slot_action(
        self, served_model: str, slot_id: int, action: str, *, filename: str | None = None
    ) -> dict[str, object]:
        """Drive llama-server's KV-slot save/restore/erase for one model
        (`POST /slots/{id}?action=…`), through the same `/upstream/…` passthrough.

        Requires the server to have been started with `--slot-save-path`; without it
        llama-server answers 501, which surfaces here as LocalGatewayError. `filename` must be
        a bare basename — llama.cpp concatenates it onto the save path and rejects anything
        with a separator (or a colon) as an invalid filename.

        REFUSES a model that isn't already resident, for the same reason `props` does: this also
        reaches llama-server through the `/upstream/` passthrough, so calling it on a cold model
        would make llama-swap load that model OUTSIDE the residency budget — the path that froze
        this host. `props` was guarded when that was found; this shares the passthrough and
        needed the same guard.

        IMPORTANT for callers: a 200 from `restore` does NOT mean the next turn will skip its
        prefill. On a sliding-window model llama-server can accept the restore and then discard
        it, logging `forcing full prompt re-processing`. The gateway log is the only honest
        signal; treat this method's success as "the bytes loaded", never as "the prefill is
        saved" (docs/runbooks/STRIX_HALO_SETUP.md).

        On a HYBRID (`qwen35`, Nemotron Mamba-2) it is stronger than that: a slot restore can
        NEVER save a prefill, because the restore path calls `prompt.clear()`, which clears the
        context checkpoints — and checkpoints are the only prefix-reuse mechanism a recurrent
        model has. So a disk-restored slot always full-reprocesses its next request. The bytes
        load, the log line says success, and the next turn pays in full. This whole KV-slot
        feature is a gpt-oss win; on a hybrid it is inert by construction, which is why the
        keeper's restore is worth keeping only for its cheapness, never for its promise."""
        if served_model not in await self.running():
            raise LocalGatewayError(
                f"{served_model} is not resident — refusing a slot {action}, because reaching it "
                "would make the gateway load the model outside the residency budget. Load it "
                "first (which evicts to make room), then act on its slots."
            )
        body = {"filename": filename} if filename is not None else {}
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 600.0), transport=self._transport
            ) as client:
                # Generous timeout: a save/restore against a busy slot is DEFERRED by
                # llama-server until the slot frees, not rejected, so the call can block.
                resp = await client.post(
                    f"{self._root}/upstream/{served_model}/slots/{slot_id}",
                    params={"action": action},
                    json=body,
                )
                resp.raise_for_status()
                parsed = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalGatewayError(str(exc)) from exc
        return parsed if isinstance(parsed, dict) else {}

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
        so."""
        async with box_events.span(box_events.MODEL_LOAD, served_model):
            await self._load_and_warm(served_model, warm_system=warm_system, warm_tools=warm_tools)

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
            try:
                async with httpx.AsyncClient(
                    timeout=load_timeout, transport=self._transport
                ) as client:
                    resp = await client.get(f"{self._root}/upstream/{served_model}/health")
                    resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise LocalGatewayError(str(exc)) from exc

        # Streaming BEFORE the load, because `/logs/stream/upstream` carries no history:
        # llama.cpp prints its per-buffer sizes once, while loading, and a reader that
        # attaches afterwards sees nothing at all.
        captured: list[str] = []
        attached = asyncio.Event()
        watcher = asyncio.ensure_future(self.capture_upstream_logs(captured, attached))
        # Wait for the stream to be live before loading. `ensure_future` only SCHEDULES the
        # watcher, so without this the load can run to completion first and the buffer
        # sizes — printed once, during the load — are missed entirely. Bounded, and
        # `attached` is set on failure too, so a build without the route costs ~nothing.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(attached.wait(), timeout=_UPSTREAM_ATTACH_S)

        async def _stop_watching() -> None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher

        try:
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

            # PRE-FLIGHT, then WATCH — for every caller, with no way to opt out. The projection
            # includes the VISION PROJECTOR BALLOON (`local_catalog.load_footprint_gb`): an
            # mmproj model pins tens of GB of GTT at load on an AMD iGPU (llama.cpp #27146), and
            # every freeze this box took was a projector-carrying model whose weights+KV
            # arithmetic looked comfortable. The watchdog covers the rest, because the first load
            # of any model is a guess and here a wrong guess costs a power cycle.
            gpu_guard.refuse_if_no_device_room(
                await self._gpu_probe.sample(), projected_gb, served_model
            )
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
        finally:
            # One exit point for the watcher: the pre-flight can refuse, the guard can
            # abort, and the warm can fail — each would otherwise leak a streaming
            # connection per load. `_stop_watching` is idempotent.
            await _stop_watching()
            self._record_measured_footprint(model, projected_gb, captured)

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
        otherwise stalls the user's first turn."""
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
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 180.0), transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._root}/upstream/{served_model}/v1/chat/completions", json=body
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("local_gateway.warm_skipped", model=served_model, error=str(exc))

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
        self, model: object, projected_gb: float, captured: list[str]
    ) -> None:
        """Compare what llama.cpp says the load cost against what the catalog predicted.

        Fed from `capture_upstream_logs`, which must already be streaming when the load
        starts. The first version of this polled `tail_logs()` afterwards, which cannot
        work: the buffered endpoint does not carry llama-server's output at all, and the
        streaming one carries no history.

        Best-effort and non-fatal — a failure costs a log line, never a load. The point is
        that a wrong catalog entry announces itself. On 2026-08-19 two were light by 1.4
        and >5.5 GiB, which aborted a healthy load and rolled back a llama.cpp upgrade,
        and llama.cpp had been printing the true figures at every load, unread."""
        model_id = getattr(model, "id", None)
        if model_id is None or projected_gb <= 0 or not captured:
            return
        with contextlib.suppress(Exception):
            report = memory_report.parse_memory_report("\n".join(captured))
            if report is None:
                # Captured something and could not read it — the case that has to be
                # LOUD rather than silent. A 3772-byte capture that parsed to nothing is
                # how this failed on 2026-08-19, and with no sample there was no way to
                # tell a framing problem from a build that prints nothing. The sample is
                # model-load output (buffer sizes, device names), not user content.
                log.warning(
                    "local_gateway.footprint_unparsed",
                    model=model_id,
                    lines=len(captured),
                    sample=[line[:200] for line in captured[:6]],
                )
                return
            drift = memory_report.catalog_divergence(report, projected_gb)
            record = log.warning if abs(drift) >= _FOOTPRINT_DRIFT_GB else log.info
            record(
                "local_gateway.footprint_measured",
                model=model_id,
                predicted_gb=round(projected_gb, 2),
                measured_gb=report.total_gb,
                drift_gb=drift,
                weights_gb=report.model_gb,
                kv_gb=report.kv_gb,
                compute_gb=report.compute_gb,
                unaccounted_gb=report.unaccounted_gb,
            )

    async def tail_logs(self) -> str:
        """The gateway's buffered log — llama-swap's `/logs`, the only BUFFERED endpoint it
        has.

        A previous version of this took a `source` and fetched `/logs/upstream` for
        llama-server's own output. That path does not exist. llama-swap's real routes are
        `/logs` (buffered, combined) and `/logs/stream/{proxy,upstream,<model>}`, which are
        STREAMING — they hold the connection open and carry no history, so they cannot back
        a tail. The wrong guess degraded safely (it fell back here) but bought nothing.

        What is buffered here is llama-swap's own account: swap decisions, health checks,
        and the slot-acquired / slot-RELEASED lines that answer whether a Stop actually
        halts decoding. llama.cpp's per-load memory breakdown is NOT in it on the pinned
        build — see `capture_upstream_logs`, which is the shape that can catch it.

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

    async def capture_upstream_logs(
        self, collected: list[str], attached: asyncio.Event | None = None
    ) -> None:
        """Append llama-server's output to `collected` until cancelled.

        `/logs/stream/upstream` carries no history, so this cannot be polled after a load —
        it has to be attached BEFORE the load starts and cancelled once it finishes. It
        appends into a caller-owned list rather than returning, because the caller cancels
        it and cancellation must not throw away what was already read.

        `attached` is set once the stream is live OR once it is known to be unavailable, so
        a caller can wait for the attach without risking a hang on a build that lacks the
        route. Without it the caller races: `ensure_future` only schedules this, and the
        loop may not run it until after the load has already printed its buffer sizes —
        which is exactly what the wiring test caught.

        Entirely best-effort. A missing route, an unreachable gateway, or a mid-read failure
        all leave `collected` short or empty, and the caller simply has nothing to measure.
        A load must never fail because its instrumentation did."""
        try:
            async with (
                httpx.AsyncClient(timeout=None, transport=self._transport) as client,
                client.stream("GET", f"{self._root}/logs/stream/upstream") as resp,
            ):
                if attached is not None:
                    attached.set()
                if resp.status_code != 200:
                    return
                async for line in resp.aiter_lines():
                    collected.append(line)
        except Exception:
            pass
        finally:
            # Also on failure, so a caller waiting to attach is never left blocking on a
            # build that does not serve this route.
            if attached is not None:
                attached.set()

    async def load_progress(self) -> float | None:
        """A real load fraction (0..1) for the model currently coming onto the box, parsed
        best-effort from the gateway's recent logs — or None when it can't be determined
        (gateway down, no /logs endpoint, or the build emits no parseable progress). The
        loading bar follows this when present and falls back to a time estimate otherwise,
        so None is a soft miss, never an error. Only one model loads at a time (we evict
        the others first), so the latest progress line in the log is unambiguous."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/logs")
                resp.raise_for_status()
                return _parse_load_progress(resp.text)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("local_gateway.logs_unavailable", error=str(exc))
            return None


# llama.cpp surfaces model-load progress on its stderr (captured by llama-swap's /logs).
# The exact wording shifts across builds, so match tolerantly: a recent log line that pairs
# a load/tensor/weight keyword with a percentage. We take the LAST such line — progress
# only climbs, and the freshest line is the truest read of how far the load has gotten.
_LOAD_KEYWORD_RE = re.compile(r"(?i)load|tensor|weight")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _parse_load_progress(text: str) -> float | None:
    last: float | None = None
    for line in text.splitlines():
        if not _LOAD_KEYWORD_RE.search(line):
            continue
        m = _PERCENT_RE.search(line)
        if m is None:
            continue
        pct = float(m.group(1))
        if 0.0 <= pct <= 100.0:
            last = pct / 100.0
    return last


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
