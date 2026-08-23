"""WarmKeeper — keep the interactive agent's local model resident AND primed, so the first
jerv message after a restart/update is instant instead of paying a cold weight-load + a
cold persona+tools prefill (the "slow first token" on a big local model).

Nothing else brings the agent.turn model back after a boot: residency's `schedule_restore`
only undoes same-process transient displacements (its keep-hot set is empty on a fresh boot),
and an on-demand turn's load is bare — it warms the inference path but does NOT prime the
persona/tools prefix. This reconciler fills that gap. It runs on boot and on an interval, so
it self-heals after an app restart, an update (a fresh container), OR a standalone gateway
(llama-swap) restart the app's process never saw.

It primes by issuing a throwaway turn down the SAME path a real turn takes — `router.converse`
with jerv's persona + tools + the agent.turn effort — so the primed KV prefix is byte-identical
to what a real turn sends (a hand-built warm on a side path drifts and the reuse silently
misses). That call also loads the model on demand through residency, so a single prime both
resides and warms it. Two subtleties it handles:

  - **Liveness flips.** The primed tool set depends on ComfyUI liveness (the image-gen tools
    are hidden when it's down). A prime taken at boot while ComfyUI was still unreachable hides
    those tools, but a real turn once ComfyUI is up shows them — a mismatch that defeats the
    reuse. So the keeper keys its "already primed" state on (model, hidden-set) and RE-PRIMES
    when the hidden set changes, self-correcting once liveness settles.
  - **Resident ≠ primed.** If something else loaded the model cold first, "resident" doesn't
    mean the jerv prefix is in the cache. The keeper still primes a resident-but-unprimed model
    once; it no-ops only after it has primed the current (model, hidden) — so a real jerv turn's
    growing conversation KV is never clobbered by a redundant re-prime.

Best-effort throughout: a down gateway, a full box, the code-mode hold, or a failed prime is
logged and retried on the next tick, never raised into boot or a turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection

import structlog

from jbrain.agent.priming import HiddenToolsProbe, jerv_prime_inputs
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import UserMessage

log = structlog.get_logger()


# The task the prime routes as — the interactive chat turn (jerv). Priming as this exact task
# is what makes the primed prefix (model, effort, tools) match a real turn's, so the reuse lands.
AGENT_TURN_TASK = "agent.turn"


class WarmKeeper:
    def __init__(
        self,
        *,
        gateway: LocalGatewayClient,
        registry: ToolRegistry,
        liveness: HiddenToolsProbe | None,
        router: LlmRouter,
        hold_loader: Callable[[], Awaitable[Collection[str]]],
        auto_restore_loader: Callable[[], Awaitable[bool]] | None = None,
        kv_prefix: KvPrefixStore | None = None,
        interval_ready: float = 60.0,
        interval_wait: float = 5.0,
    ):
        self._gateway = gateway
        self._registry = registry
        self._liveness = liveness
        # The router owns routing precedence (env pin, DB override, local gate) AND residency
        # admission, so the keeper asks it which model agent.turn resolves to and primes THROUGH
        # it — a re-route moves the kept-hot model automatically and the prime path matches a turn.
        self._router = router
        self._hold_loader = hold_loader
        # The operator's "automatically reload models" switch. The keeper is the SECOND
        # auto-load path on this box — residency restore is the other — and it used to ignore
        # this setting entirely, so turning it off stopped restores while the keeper went on
        # reloading the primary model every interval_wait seconds. An operator who switches
        # auto-reload off and watches a 68 GiB model reappear within five seconds has been
        # told something untrue by the UI. It gates LOADING only: a model already resident is
        # still kept primed, because holding a warm prefix costs nothing and is not a load.
        self._auto_restore_loader = auto_restore_loader
        # The disk layer under the prime (jbrain.llm.kv_prefix): restore before priming so
        # the prime is a ~1 s cache hit instead of a ~60 s prefill, save after priming so
        # the next boot can do the same. Optional — unwired keeps the prior behaviour.
        self._kv_prefix = kv_prefix
        # What we last successfully primed: (served_model, hidden-tool-set). None until primed
        # (or after the model is found evicted). Re-prime when this no longer matches the desired.
        self._primed: tuple[str, frozenset[str]] | None = None
        # Two cadences: retry EAGERLY (interval_wait) while a target is wanted but not yet primed
        # — the boot window where the gateway/ComfyUI are still coming up, so the prime lands
        # seconds after they're reachable, not a full steady-interval later. Once primed (or
        # nothing to do), fall back to the slow steady poll (interval_ready) that only exists to
        # catch a later gateway-only restart or a liveness flip.
        self._interval_ready = interval_ready
        self._interval_wait = interval_wait

    async def _auto_restore_allowed(self) -> bool:
        """Default OPEN when unwired (no loader) or on a settings read failure: this gate only
        suppresses a convenience reload, and a box that silently stopped keeping its model warm
        because a settings query hiccupped would be a worse failure than one extra load."""
        if self._auto_restore_loader is None:
            return True
        try:
            return await self._auto_restore_loader()
        except Exception:  # noqa: BLE001 — a settings hiccup must not wedge the keeper
            return True

    def note_prefix_lost(self, served_model: str) -> None:
        """Forget the primed memo for `served_model` — registered with the residency
        coordinator, which calls it on an eviction or a bare restore-load.

        The memo alone is not enough: it is only invalidated when a tick OBSERVES the model
        missing from the gateway, so an evict+restore that both complete between ticks leaves
        it stale and the next jerv turn pays a cold prefill in the foreground. This is the
        edge-triggered half of that invalidation."""
        if self._primed is not None and self._primed[0] == served_model:
            self._primed = None

    async def reconcile_once(self) -> bool:
        """Bring the target model to resident+primed if it isn't already. Returns True when
        SETTLED (nothing to keep warm, or resident and primed with the current tool set), False
        when a target is wanted but not yet primed (gateway down / no room / prime failed) — the
        run loop reads that as 'retry soon'."""
        served = await self._router.primary_local_served_model()
        if served is None:
            return True  # cloud route or local hosting off — nothing to keep warm
        try:
            held = set(await self._hold_loader() or ())
        except Exception:  # noqa: BLE001 — a settings read hiccup must not wedge the keeper
            held = set()
        if held and served not in held:
            return True  # code mode owns the box; never load outside its reserved set
        try:
            running = await self._gateway.running()
        except Exception:  # noqa: BLE001 — running() already swallows, but be defensive
            running = set()
        cold = served not in running
        if cold:
            self._primed = None  # evicted (or never loaded) → the cache no longer holds our prime
            if not await self._auto_restore_allowed():
                # Off: the operator asked for nothing to be loaded behind their back. SETTLED,
                # not "retry soon" — returning False here would spin the eager 5s cadence
                # forever against a switch that is never going to flip on its own.
                return True
        # Pass the SERVED model: the canvas pair is model-gated, and `jerv_prime_inputs`
        # with no model hides it — so on a canvas-capable model the keeper would prime a
        # prefix WITHOUT tools a real turn sends, the reuse would miss from the tools block
        # onward, and the memo below would record that miss as success. The manual Load path
        # already passes it (api/llm_settings.gateway_load); this closes the gap.
        system, tools, hidden = await jerv_prime_inputs(self._registry, self._liveness, served)
        want = (served, hidden)
        if served in running and self._primed == want:
            # Primed as far as the memo knows — but the memo cannot see a slot being
            # overwritten by traffic (a single-slot configuration loses the prefix to any
            # background task). The store CAN, by reading /slots, and puts it back from
            # disk off-turn — one cheap read per tick when nothing is wrong.
            if self._kv_prefix is not None:
                try:
                    await self._kv_prefix.restore_if_lost(served, system, tools)
                except Exception:  # noqa: BLE001 — the disk layer must never wedge the keeper
                    log.warning("warm_keeper.kv_restore_failed", model=served, exc_info=True)
            return True  # already primed with the current tool set — leave any live conversation be
        # Bring the WEIGHTS up before priming, when the model is cold.
        #
        # There used to be a disk KV-slot restore here, ahead of the prime. It is gone: it never
        # worked on either family this box serves. On a HYBRID (the Qwen3.8 27B entries) the
        # restore path calls `prompt.clear()`, wiping the context checkpoints that are a
        # recurrent model's only prefix-reuse mechanism, so it was inert by construction. On
        # gpt-oss it restored 400s and 2 KB files of whatever background traffic held the single
        # slot. Residency plus this prime are what actually keep the first message fast, and
        # they do it without a ~2 GB file per model on a volume only the deploy could prune.
        #
        # Admission FIRST, through the same coordinator a routed completion goes through — and
        # in the wired configuration that is already the load: `ensure_room` takes the slow path
        # for a non-resident target and calls `gateway.load` itself. So we re-read residency
        # afterwards and only load explicitly if the model is still cold (no coordinator wired).
        #
        # Loading unconditionally here was a DOUBLE load. The second one re-runs
        # `refuse_if_no_device_room` against a post-load sample — the model's own footprint is
        # already subtracted from free GTT — so it demands roughly twice the footprint plus the
        # headroom floor and raises on a box that is perfectly healthy. That lands a spurious
        # "refusing to load rather than risk freezing the host" in the exact log an operator
        # reads while investigating hard-locks, and in the narrow case where the pre-flight
        # passes and the watchdog then trips, its abort UNLOADS the model we just loaded.
        #
        # Whichever path brings it up, it comes up WITHOUT a warm system/tools, so no persona
        # prefill happens here — that is the point, since prefilling would spend the cost the
        # restore exists to avoid.
        #
        # Best-effort: on failure fall through to the prime, which is exactly the old behaviour.
        if cold:
            try:
                await self._router.admit_local_load(served)
                if served not in await self._gateway.running():
                    await self._gateway.load(served)
            except Exception as exc:  # noqa: BLE001 — no room / gateway down: the prime retries
                log.info("warm_keeper.preload_failed", model=served, error=str(exc))
        # The disk layer's moment: with the weights up but the prefix cold, a valid saved
        # slot turns the prime below into a ~1 s cache hit. Best-effort — a miss just
        # means the prime pays the prefill, which is exactly the old behaviour. (This is
        # v2 of a removed idea; the module docstring of `kv_prefix` carries the post-mortem
        # of v1 and the verification rules that answer it.)
        if self._kv_prefix is not None:
            try:
                await self._kv_prefix.restore_if_lost(served, system, tools)
            except Exception:  # noqa: BLE001 — the disk layer must never wedge the keeper
                log.warning("warm_keeper.kv_restore_failed", model=served, exc_info=True)
        # Prime down the real turn path: resolves agent.turn's model+effort, admits through
        # residency (loading the model if needed), and prefills the exact persona+tools prefix a
        # real turn reuses. max_tokens=1 — we want the prefill in cache, not the output.
        try:
            prime_turn = await self._router.converse(
                AGENT_TURN_TASK,
                system=system,
                messages=[UserMessage(text="warmup")],
                tools=tools,
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 — gateway down/cold/no-room: retry, never raise
            log.info("warm_keeper.prime_failed", model=served, error=str(exc))
            return False
        self._primed = want
        # Persist what was just primed, in the same breath — the only moment the slot
        # provably holds exactly this prefix, identified by the prime's own token count.
        if self._kv_prefix is not None:
            try:
                await self._kv_prefix.save_after_prime(
                    served, system, tools, prime_turn.usage.input_tokens
                )
            except Exception:  # noqa: BLE001 — a failed save costs a future restore, nothing now
                log.warning("warm_keeper.kv_save_failed", model=served, exc_info=True)
        log.info(
            "warm_keeper.primed",
            model=served,
            tool_count=len(tools),
            hidden=sorted(hidden),
        )
        return True

    async def run(self) -> None:
        """The reconcile loop: settle, then sleep — short while still trying to reach a wanted
        model (boot / gateway restart / liveness flip), long once primed. Runs until cancelled."""
        while True:
            settled = True
            try:
                settled = await self.reconcile_once()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the keeper
                log.warning("warm_keeper.tick_failed", exc_info=True)
                settled = False
            await asyncio.sleep(self._interval_ready if settled else self._interval_wait)
