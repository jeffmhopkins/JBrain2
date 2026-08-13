"""WarmKeeper — keep the interactive agent's local model resident AND primed, so the first
jerv message after a restart/update is instant instead of paying a cold weight-load + a
cold persona+tools prefill (the "slow first token" on a big local model).

Nothing else brings the agent.turn model back after a boot: residency's `schedule_restore`
only undoes same-process transient displacements (its keep-hot set is empty on a fresh
boot), and an on-demand turn's `ensure_room` load is bare — it warms the inference path but
does NOT prime the persona/tools prefix. This reconciler fills that gap. It runs on boot and
on an interval, so it self-heals after an app restart, an update (a fresh container), OR a
standalone gateway (llama-swap) restart the app's process never saw.

It only ever ADDS the single interactive model, under the existing residency rules (the
free-RAM floor via `free_room`, and the code-mode hold), never evicts for anything else, and
no-ops once the model is resident — so a real jerv turn (which loads and uses it) makes the
next tick a no-op. Best-effort throughout: a down gateway, a full box, or a load failure is
logged and retried on the next tick, never raised into boot or a turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection

import structlog

from jbrain.agent.priming import HiddenToolsProbe, jerv_prime_spec
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError
from jbrain.llm.router import LlmRouter

log = structlog.get_logger()


class WarmKeeper:
    def __init__(
        self,
        *,
        gateway: LocalGatewayClient,
        residency: ResidencyCoordinator | None,
        registry: ToolRegistry,
        liveness: HiddenToolsProbe | None,
        router: LlmRouter,
        hold_loader: Callable[[], Awaitable[Collection[str]]],
        interval_ready: float = 60.0,
        interval_wait: float = 5.0,
    ):
        self._gateway = gateway
        self._residency = residency
        self._registry = registry
        self._liveness = liveness
        # The router owns routing precedence (env pin, DB override, local gate), so the keeper
        # asks it which model agent.turn resolves to rather than re-deriving it — a re-route
        # then moves the kept-hot model automatically.
        self._router = router
        self._hold_loader = hold_loader
        # Two cadences: retry EAGERLY (interval_wait) while a target is wanted but not yet
        # resident — the boot window where the gateway is still coming up, so the prime lands
        # seconds after it's reachable, not a full steady-interval later. Once resident (or
        # nothing to do), fall back to the slow steady poll (interval_ready) that only exists
        # to catch a later gateway-only restart.
        self._interval_ready = interval_ready
        self._interval_wait = interval_wait

    async def reconcile_once(self) -> bool:
        """Bring the target model to resident+primed if it isn't already. Returns True when
        SETTLED (nothing to keep warm, or the target is resident), False when a target is
        wanted but not yet resident (gateway down / no room / load failed) — the run loop
        reads that as 'retry soon'."""
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
        if served in running:
            return True  # a prior tick or a real turn already warmed it
        # Make room the deliberate way (hold the free-RAM floor); a refusal (won't fit) leaves
        # it for a later tick rather than crashing.
        if self._residency is not None:
            try:
                await self._residency.free_room(served)
            except ResidencyError as exc:
                log.info("warm_keeper.no_room", model=served, error=str(exc))
                return False
        system, tools = await jerv_prime_spec(self._registry, self._liveness)
        try:
            await self._gateway.load(served, warm_system=system, warm_tools=tools)
        except LocalGatewayError as exc:
            log.info("warm_keeper.load_failed", model=served, error=str(exc))
            return False
        log.info("warm_keeper.primed", model=served, tool_count=len(tools))
        return True

    async def run(self) -> None:
        """The reconcile loop: settle, then sleep — short while still trying to reach a
        wanted model (boot / gateway restart), long once resident. Runs until cancelled."""
        while True:
            settled = True
            try:
                settled = await self.reconcile_once()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the keeper
                log.warning("warm_keeper.tick_failed", exc_info=True)
                settled = False
            await asyncio.sleep(self._interval_ready if settled else self._interval_wait)
