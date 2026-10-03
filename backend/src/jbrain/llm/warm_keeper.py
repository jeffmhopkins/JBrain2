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

  - **Hidden-set flips.** The primed tool set depends on the model-gated hidden set (the
    canvas pair is withheld from an unqualified model). A prime taken with one hidden set
    no longer matches a real turn once it changes — a mismatch that defeats the reuse. So
    the keeper keys its "already primed" state on (model, hidden-set) and RE-PRIMES when
    the hidden set changes.
  - **Resident ≠ primed.** If something else loaded the model cold first, "resident" doesn't
    mean the jerv prefix is in the cache. The keeper still primes a resident-but-unprimed model
    once; it no-ops only after it has primed the current (model, hidden) — so a real jerv turn's
    growing conversation KV is never clobbered by a redundant re-prime.

On a POOLED model (Flash-Next's eight role-pinned slots, `jbrain.llm.slot_roles`) slot 0 is only
jerv's. The keeper then also primes the other roles whose prefix is stable — scheduled tasks
(jerv's prefix again, in their own slot), ingest (the disambiguation prompt) and the kid pet — one
role per tick and only while no slot is processing, so a prime never queues behind, or slows,
real work. Every prime goes through the router with its `slot_role`, which is what pins it.

Best-effort throughout: a down gateway, a full box, the code-mode hold, or a failed prime is
logged and retried on the next tick, never raised into boot or a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import TypedDict

import structlog

from jbrain.agent.priming import jerv_prime_inputs
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm import local_catalog
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.slot_roles import WARM_ROLE, KvPool, SlotRole
from jbrain.llm.types import LlmTool, UserMessage

log = structlog.get_logger()


# The task the prime routes as — the interactive chat turn (jerv). Priming as this exact task
# is what makes the primed prefix (model, effort, tools) match a real turn's, so the reuse lands.
AGENT_TURN_TASK = "agent.turn"


@dataclass(frozen=True)
class RolePrime:
    """A one-shot completion whose system prompt is the stable prefix of a pooled role's real
    calls. Built by the app (the prompts live in modules this one must not import) and primed
    with the same task and strength those calls route with, so the rendered prefix matches."""

    role: SlotRole
    task: str
    system: str
    strength: str | None = None


# Roles with no stable prefix to prime, listed so the omission reads as decided:
#  - RESEARCH: each sub-agent's system and tool allowlist come from its persona and the fan's
#    plan, and the children of one fan overwrite each other in the slot; the synthesis prompt
#    runs only after them, so a prime of either is gone before anything could reuse it.
#  - WORKSHOP: the wiki editor, note conversations and guided intake share the slot with
#    prompts that differ per article, per note and per interview brief.
#  - JCODE, SMALL: the coding client owns its prompt; small calls are one-shot by design.
_ROLE_PRIME_ORDER: tuple[SlotRole, ...] = (SlotRole.SCHEDULED, SlotRole.INGEST, SlotRole.PET)


class _Pin(TypedDict, total=False):
    slot_role: SlotRole


def _prefix_digest(system: str, tools: Sequence[LlmTool], effort: str | None) -> str:
    h = hashlib.sha256()
    h.update(system.encode())
    schemas = [[t.name, t.description, t.input_schema] for t in tools]
    h.update(json.dumps(schemas, sort_keys=True).encode())
    h.update(str(effort).encode())
    return h.hexdigest()


class WarmKeeper:
    def __init__(
        self,
        *,
        gateway: LocalGatewayClient,
        registry: ToolRegistry,
        router: LlmRouter,
        hold_loader: Callable[[], Awaitable[Collection[str]]],
        auto_restore_loader: Callable[[], Awaitable[bool]] | None = None,
        kv_prefix: KvPrefixStore | None = None,
        interval_ready: float = 60.0,
        interval_wait: float = 5.0,
        role_primes: Sequence[RolePrime] = (),
    ):
        self._gateway = gateway
        self._registry = registry
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
        # What we last successfully primed: (served_model, hidden-tool-set, reasoning effort).
        # None until primed (or after the model is found evicted). Re-prime when this no longer
        # matches the desired.
        #
        # The EFFORT is in here because it is in the rendered prompt — gpt-oss's harmony
        # template writes a literal "Reasoning: <level>" into the leading tokens — and so in
        # the store's fingerprint. Without it, changing the agent task's effort in Settings
        # left `want == self._primed`, the keeper took its settled branch forever, no prime
        # ran, no file was ever saved under the new identity, and every interactive turn
        # re-prefilled the whole prefix until a restart. The store's own comment named that
        # hazard and fixed only the fingerprint half of it.
        self._primed: tuple[str, frozenset[str], str | None] | None = None
        # Two cadences: retry EAGERLY (interval_wait) while a target is wanted but not yet primed
        # — the boot window where the gateway is still coming up, so the prime lands
        # seconds after it's reachable, not a full steady-interval later. Once primed (or
        # nothing to do), fall back to the slow steady poll (interval_ready) that only exists to
        # catch a later gateway-only restart or a hidden-set flip.
        self._interval_ready = interval_ready
        self._interval_wait = interval_wait
        # Set by `note_prefix_lost` to cut the sleep short. Without it the edge trigger was
        # only half an edge: residency reported the loss immediately and the keeper then slept
        # out the rest of its interval before acting. Its main production caller is the
        # end-of-turn restore, so it fires just after the owner finished a message — which
        # makes the next message, inside that same minute, the one that pays the prefill the
        # hook exists to prevent.
        self._wake = asyncio.Event()
        # Consecutive failed ticks, for the backoff below. A prime that fails for a persistent
        # reason used to retry at the eager 5 s cadence forever — three settings reads, a
        # /running GET, an admission that can EVICT to fit, and a log line every five seconds,
        # on a box whose owner reads those logs through a debug console.
        self._failures = 0
        # Bumped on every prime attempt. `_primed` is only written by the attempt that still
        # owns the current generation, so a `note_prefix_lost` arriving DURING a prime is no
        # longer overwritten by that prime's completion — a 60-200 s window in which the model
        # could be evicted and bare-reloaded, leaving it resident, cold, and marked primed.
        self._generation = 0
        # Pooled models only: the one-shot primes for roles other than slot 0, and what each
        # role was last primed with as (served, digest of system + tools + effort). A changed
        # digest re-primes that role; a lost prefix clears them all along with `_primed`.
        self._role_primes = {p.role: p for p in role_primes}
        self._roles_primed: dict[SlotRole, tuple[str, str]] = {}
        # True while a pooled model still has a role to prime and the last attempt did not
        # fail, so the run loop polls at the eager cadence until they are done rather than
        # one role per steady minute.
        self._roles_pending = False

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
        self._forget_roles(served_model)
        # Invalidate any prime currently in flight: it was priming a slot that no longer
        # exists, and letting it record success would re-assert the memo this just cleared.
        self._generation += 1
        self._wake.set()

    def _forget_roles(self, served_model: str) -> None:
        self._roles_primed = {
            role: memo for role, memo in self._roles_primed.items() if memo[0] != served_model
        }

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
        pool = local_catalog.pool_of(served)
        self._roles_pending = False
        if cold:
            self._primed = None  # evicted (or never loaded) → the cache no longer holds our prime
            self._forget_roles(served)
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
        system, tools, hidden = await jerv_prime_inputs(self._registry, served)
        # The effort the prime's turn will carry — part of the rendered prompt and so part
        # of the disk cache's identity (see kv_prefix._fingerprint). Resolved the same way
        # the prime's converse below resolves it; a resolution hiccup degrades to None,
        # which at worst keys the file under the wrong effort and costs one prefill.
        effort: str | None = None
        if self._kv_prefix is not None:
            try:
                effort = await self._router.effective_reasoning_effort(AGENT_TURN_TASK)
            except Exception:  # noqa: BLE001 — identity input only, never wedge the keeper
                log.warning("warm_keeper.effort_resolve_failed", model=served, exc_info=True)
        want = (served, hidden, effort)
        if served in running and self._primed == want:
            # Primed as far as the memo knows — but the memo cannot see a slot being
            # overwritten by traffic (a single-slot configuration loses the prefix to any
            # background task). The store CAN, by reading /slots, and puts it back from
            # disk off-turn — one cheap read per tick when nothing is wrong.
            if self._kv_prefix is not None:
                try:
                    await self._kv_prefix.restore_if_lost(
                        served, system, tools, reasoning_effort=effort
                    )
                except Exception:  # noqa: BLE001 — the disk layer must never wedge the keeper
                    log.warning("warm_keeper.kv_restore_failed", model=served, exc_info=True)
            if pool is not None:
                await self._prime_next_role(served, pool, system, tools)
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
                # Load what residency ADMITTED: an engine switch between the two reads remaps it.
                admitted = await self._router.admit_local_load(served) or served
                if admitted not in await self._gateway.running():
                    await self._gateway.load(admitted)
            except Exception as exc:  # noqa: BLE001 — no room / gateway down: the prime retries
                log.info("warm_keeper.preload_failed", model=served, error=str(exc))
        # The disk layer's moment: with the weights up but the prefix cold, a valid saved
        # slot turns the prime below into a ~1 s cache hit. Best-effort — a miss just
        # means the prime pays the prefill, which is exactly the old behaviour. (This is
        # v2 of a removed idea; the module docstring of `kv_prefix` carries the post-mortem
        # of v1 and the verification rules that answer it.)
        if self._kv_prefix is not None:
            try:
                await self._kv_prefix.restore_if_lost(
                    served, system, tools, reasoning_effort=effort
                )
            except Exception:  # noqa: BLE001 — the disk layer must never wedge the keeper
                log.warning("warm_keeper.kv_restore_failed", model=served, exc_info=True)
        # Prime down the real turn path: resolves agent.turn's model+effort, admits through
        # residency (loading the model if needed), and prefills the exact persona+tools prefix a
        # real turn reuses. max_tokens=1 — we want the prefill in cache, not the output.
        #
        # The generation is read BEFORE the await and compared after. A cold prime takes
        # 60-200 s, and `note_prefix_lost` firing inside that window used to be erased by this
        # prime's own completion re-asserting `_primed` — leaving the model resident, cold, and
        # believed primed, which is the exact state that hook exists to prevent.
        generation = self._generation
        # Named only on a pool: elsewhere the call stays exactly what it always was.
        pin: _Pin = {"slot_role": WARM_ROLE} if pool is not None else {}
        try:
            prime_turn = await self._router.converse(
                AGENT_TURN_TASK,
                system=system,
                messages=[UserMessage(text="warmup")],
                tools=tools,
                max_tokens=1,
                **pin,
            )
        except Exception as exc:  # noqa: BLE001 — gateway down/cold/no-room: retry, never raise
            log.info("warm_keeper.prime_failed", model=served, error=str(exc))
            return False
        if generation != self._generation:
            # The slot we primed was dropped while we were priming. Say nothing about being
            # primed, and let the next tick start over — the save below is skipped too,
            # because the token count it would key on describes a slot that is gone.
            log.info("warm_keeper.prime_superseded", model=served)
            return False
        self._primed = want
        # Persist what was just primed, in the same breath — the only moment the slot
        # provably holds exactly this prefix, identified by the prime's own token count.
        if self._kv_prefix is not None:
            try:
                await self._kv_prefix.save_after_prime(
                    served, system, tools, prime_turn.usage.input_tokens, reasoning_effort=effort
                )
            except Exception:  # noqa: BLE001 — a failed save costs a future restore, nothing now
                log.warning("warm_keeper.kv_save_failed", model=served, exc_info=True)
        log.info(
            "warm_keeper.primed",
            model=served,
            tool_count=len(tools),
            hidden=sorted(hidden),
        )
        # The other roles start next tick: this one already spent its prefill.
        self._roles_pending = pool is not None and self._next_role(served, pool) is not None
        return True

    def _candidates(self, pool: KvPool) -> list[SlotRole]:
        pooled = {r.role for r in pool.reservations}
        return [
            role
            for role in _ROLE_PRIME_ORDER
            if role in pooled and (role == SlotRole.SCHEDULED or role in self._role_primes)
        ]

    def _next_role(self, served: str, pool: KvPool) -> SlotRole | None:
        """The first role in prime order with no memo for `served`. A digest change is caught
        when the role's turn comes round, since its digest needs a route read."""
        for role in self._candidates(pool):
            memo = self._roles_primed.get(role)
            if memo is None or memo[0] != served:
                return role
        return None

    async def _idle(self, served: str) -> bool:
        """No slot of the model is processing. A pinned prime sent to a busy slot would queue
        behind real work, and one beside it slows that work down; neither is worth a warm
        prefix. An unreadable /slots reads as busy."""
        try:
            slots = await self._gateway.slots(served)
        except Exception:  # noqa: BLE001 — a probe hiccup only postpones the prime
            return False
        return not any(s.get("is_processing") for s in slots)

    async def _prime_next_role(
        self, served: str, pool: KvPool, jerv_system: str, jerv_tools: list[LlmTool]
    ) -> None:
        """Prime at most one pooled role this tick — the first whose memo is stale — and set
        `_roles_pending` for the run loop's cadence."""
        for role in self._candidates(pool):
            if role == SlotRole.SCHEDULED:
                # Scheduled tasks and plan continuations run jerv's persona and tool set
                # (tasks/runner.py hides the same model-gated tools the chat does), so their
                # slot wants the very prefix slot 0 holds.
                task, system, tools, strength = AGENT_TURN_TASK, jerv_system, jerv_tools, None
            else:
                spec = self._role_primes[role]
                task, system, tools, strength = spec.task, spec.system, [], spec.strength
            try:
                provider, model = await self._router.effective_spec(task, strength)
                effort = await self._router.effective_reasoning_effort(task, strength)
            except Exception:  # noqa: BLE001 — a routing hiccup postpones this role
                log.info("warm_keeper.role_route_failed", model=served, role=role, exc_info=True)
                return
            if provider != local_catalog.LOCAL_PROVIDER or model != served:
                # Routed elsewhere (a cloud model, another local one): this model's slot would
                # hold a prefix no call of the role sends here, and priming through that route
                # would spend on, or load, the other model.
                continue
            digest = _prefix_digest(system, tools, effort)
            if self._roles_primed.get(role) == (served, digest):
                continue
            if not await self._idle(served):
                self._roles_pending = True
                return
            if await self._prime_role(served, role, task, system, tools, strength):
                self._roles_primed[role] = (served, digest)
                self._roles_pending = self._next_role(served, pool) is not None
            return

    async def _prime_role(
        self,
        served: str,
        role: SlotRole,
        task: str,
        system: str,
        tools: list[LlmTool],
        strength: str | None,
    ) -> bool:
        generation = self._generation
        try:
            if tools:
                await self._router.converse(
                    task,
                    system=system,
                    messages=[UserMessage(text="warmup")],
                    tools=tools,
                    max_tokens=1,
                    slot_role=role,
                )
            else:
                await self._router.complete(
                    task,
                    system=system,
                    user_text="warmup",
                    max_tokens=1,
                    strength=strength,
                    slot_role=role,
                )
        except Exception as exc:  # noqa: BLE001 — a failed role prime waits for a later tick
            log.info("warm_keeper.role_prime_failed", model=served, role=role, error=str(exc))
            return False
        if generation != self._generation:
            log.info("warm_keeper.role_prime_superseded", model=served, role=role)
            return False
        log.info("warm_keeper.role_primed", model=served, role=role, task=task)
        return True

    def _retry_delay(self) -> float:
        """The eager interval, doubled per consecutive failure, capped at the steady one.

        A prime that fails for a PERSISTENT reason — a gateway that will not come up, a model
        that cannot fit — used to retry at the eager cadence forever: ~17k log lines a day, and
        each attempt runs an admission that can EVICT to fit, so the keeper and the worker
        could trade the same 68 GB model back and forth every five seconds. Backoff makes a
        transient failure cost nothing extra and a permanent one cost almost nothing at all."""
        delay = self._interval_wait * (2 ** min(self._failures, 8))
        return min(delay, self._interval_ready)

    async def run(self) -> None:
        """The reconcile loop: settle, then sleep — short while still trying to reach a wanted
        model (boot / gateway restart / hidden-set flip), long once primed, and backing off
        while it keeps failing. A dropped prefix cuts the sleep short. Runs until cancelled."""
        while True:
            settled = True
            try:
                settled = await self.reconcile_once()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the keeper
                log.warning("warm_keeper.tick_failed", exc_info=True)
                settled = False
            self._failures = 0 if settled else self._failures + 1
            if not settled:
                delay = self._retry_delay()
            elif self._roles_pending:
                delay = self._interval_wait
            else:
                delay = self._interval_ready
            # Wait for the delay OR for a reported loss, whichever comes first. The hook's
            # main caller is the end-of-turn restore, so it fires just after the owner sends a
            # message — and sleeping out the rest of the interval is what made their NEXT
            # message pay the prefill the hook was added to prevent.
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
