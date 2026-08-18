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
import datetime as dt
from collections.abc import Awaitable, Callable, Collection

import structlog

from jbrain.agent.priming import (
    HiddenToolsProbe,
    jerv_prime_fingerprint,
    jerv_prime_inputs,
    jerv_slot_filename,
)
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.openai_compat import openai_tools
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import LlmTool, UserMessage

log = structlog.get_logger()

# The slot the jerv prefix is restored into and saved from. With `-np 2` llama-server routes
# by longest matching prefix and the interactive prefix lands in slot 1 (verified on the box);
# slot 0 stays empty for background traffic. A wrong guess costs a miss, never a wrong answer.
_INTERACTIVE_SLOT = 1

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
        if served not in running:
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
            return True  # already primed with the current tool set — leave any live conversation be
        # Try the disk cache FIRST. A hit turns the prefill below into a no-op: measured on
        # the box, a real turn's prefix restores in ~0.3s and the prime that follows returns in
        # ~0.2s, against 70-110s cold. The prime still runs either way — it is what makes this
        # safe, because a restore that silently did nothing (the SWA failure mode, and any
        # stale-but-loadable file) is simply a slow prime, never a wrong answer. We never trust
        # the restore's own 200; we let the prefill be the proof.
        restored = await self._try_restore(served, system, tools)
        # Prime down the real turn path: resolves agent.turn's model+effort, admits through
        # residency (loading the model if needed), and prefills the exact persona+tools prefix a
        # real turn reuses. max_tokens=1 — we want the prefill in cache, not the output.
        try:
            await self._router.converse(
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
        log.info(
            "warm_keeper.primed",
            model=served,
            tool_count=len(tools),
            hidden=sorted(hidden),
            restored=restored,
        )
        if not restored:
            # Only write when we actually paid for the prefill. Saving after a restore would
            # rewrite a byte-identical file every tick for nothing.
            await self._try_save(served, system, tools)
        return True

    async def _slot_filename(
        self, served_model: str, system: str, tools: list[LlmTool]
    ) -> str | None:
        """The state file this exact prefix, build and day would use — or None when the gateway
        cannot tell us what it is running. Best-effort: no props, no fingerprint, no restore
        (and so a normal cold prime), which is the correct way to fail."""
        try:
            props = await self._gateway.props(served_model)
        except Exception:  # noqa: BLE001 — a props hiccup must not break the reconcile
            return None
        gen = props.get("default_generation_settings")
        fingerprint = jerv_prime_fingerprint(
            system,
            openai_tools(tools),
            build_info=str(props.get("build_info", "")),
            chat_template=str(props.get("chat_template", "")),
            n_ctx=(gen or {}).get("n_ctx") if isinstance(gen, dict) else None,
            n_slots=props.get("total_slots"),
            today_utc=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
        )
        return jerv_slot_filename(served_model, fingerprint)

    async def _try_restore(self, served_model: str, system: str, tools: list[LlmTool]) -> bool:
        """Load this prefix's saved KV into the interactive slot. True only means the bytes
        loaded — the prime that follows is what establishes they were USEFUL."""
        name = await self._slot_filename(served_model, system, tools)
        if name is None:
            return False
        try:
            result = await self._gateway.slot_action(
                served_model, _INTERACTIVE_SLOT, "restore", filename=name
            )
        except Exception:  # noqa: BLE001 — a miss is the common case on a fresh box, not an error
            return False
        log.info("warm_keeper.slot_restored", model=served_model, tokens=result.get("n_restored"))
        return True

    async def _try_save(self, served_model: str, system: str, tools: list[LlmTool]) -> None:
        """Persist the just-primed slot so the next cold start skips the prefill."""
        name = await self._slot_filename(served_model, system, tools)
        if name is None:
            return
        try:
            result = await self._gateway.slot_action(
                served_model, _INTERACTIVE_SLOT, "save", filename=name
            )
        except Exception:  # noqa: BLE001 — no --slot-save-path, no disk, no permission: skip
            log.info("warm_keeper.slot_save_skipped", model=served_model)
            return
        log.info("warm_keeper.slot_saved", model=served_model, tokens=result.get("n_saved"))

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
