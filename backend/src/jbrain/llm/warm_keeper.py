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

On a POOLED model (Flash-Next's role-pinned slots) the disk layer works per role
(FLASH_NEXT_ENGINE_PLAN §4b, F4): jerv's prime is saved from slot 0 and restored into slot 0,
and once settled each tick also puts the same file back into the scheduled-task slot when that
slot is empty — scheduled turns run jerv's persona and tools — and saves the interactive slot's
conversation once it has idled (`KvPrefixStore.save_idle_conversation`). Restores only, never a
prime: a second ~60 s prefill right after a load competed with the owner's first turn, which is
why F3a dropped role priming.

On a pool with a CHAT PAIR (two chat slots: the most recent conversation, and the warm jerv
prefix) the keeper keeps the warm half warm. Its prime goes to the pair slot the store names
(`KvPrefixStore.warm_target`), pinned exactly. And once a chat moves to the other slot the store
wakes the keeper, whose tick re-warms the slot that chat left (`rewarm_pair`: erase, restore the
file). When no restore can serve — no file for this identity yet, or the restore gate still
awaiting its probe — it primes that slot instead, but only while neither chat slot is busy, so
the background prefill never runs under the owner's turn.

Best-effort throughout: a down gateway, a full box, the code-mode hold, or a failed prime is
logged and retried on the next tick, never raised into boot or a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Collection

import structlog

from jbrain.agent.priming import jerv_prime_inputs
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm import engine as engines
from jbrain.llm import kv_prefix as kv_prefix_mod
from jbrain.llm import local_catalog
from jbrain.llm.kv_prefix import ChatSlotTakenError, KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.slot_roles import WARM_ROLE, SlotRole, exact_pin
from jbrain.llm.types import LlmTool, LlmTurn, UserMessage

log = structlog.get_logger()


# The task the prime routes as — the interactive chat turn (jerv). Priming as this exact task
# is what makes the primed prefix (model, effort, tools) match a real turn's, so the reuse lands.
AGENT_TURN_TASK = "agent.turn"

# Pooled roles, beyond the interactive slot, that the keeper restores jerv's saved prefix into.
# Only roles whose calls send jerv's prefix at the agent task's effort (so the same file is
# their own identity): scheduled tasks and plan continuations (tasks/runner.py). Left out, as
# decided: ingest and the pet send ~400-500-token prefixes, under the store's 4096-token floor
# and under a second of prefill; research has no stable prefix (each sub-agent's persona and
# the fan's plan differ); jcode, workshop and small prompts own or vary their prompts. Their
# turns still restore into their own slot on demand if a file of their identity ever exists.
DISK_RESTORED_ROLES: tuple[SlotRole, ...] = (SlotRole.SCHEDULED,)


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
        if kv_prefix is not None:
            # A chat moving to the other pair slot leaves a slot to re-warm: tick now.
            kv_prefix.add_pair_listener(self._on_pair_move)

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
        # Invalidate any prime currently in flight: it was priming a slot that no longer
        # exists, and letting it record success would re-assert the memo this just cleared.
        self._generation += 1
        self._wake.set()

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
            # Auto-restore governs the standard gateway, where reloading one model can evict
            # another. Flash-Next is the only model on its engine — nothing to evict, and every
            # local task runs on it — so it is kept loaded whatever that switch says (owner,
            # 2026-10-03: "defaulted to having Flash loaded").
            sole_engine = local_catalog.engine_of(served) != engines.STANDARD
            if not sole_engine and not await self._auto_restore_allowed():
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
                if self._has_pair(served):
                    if not await self._tend_pair(served, system, tools, effort):
                        return False
                else:
                    try:
                        await self._kv_prefix.restore_if_lost(
                            served, system, tools, reasoning_effort=effort, role=WARM_ROLE
                        )
                    except Exception:  # noqa: BLE001 — the disk layer must never wedge it
                        log.warning("warm_keeper.kv_restore_failed", model=served, exc_info=True)
                if local_catalog.pool_of(served) is not None:
                    await self._tend_pooled_roles(served, system, tools, effort)
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
        # On a chat pair the prime goes to the member the store names — the one due warming,
        # never the most recent conversation — pinned there exactly.
        role = self._prime_role(served)
        if self._kv_prefix is not None:
            # On a chat pair the restore is a background one: it never waits on a busy slot and
            # never erases one a chat request was just routed to.
            try:
                if self._has_pair(served):
                    await self._kv_prefix.restore_if_lost(
                        served, system, tools, reasoning_effort=effort, role=role, background=True
                    )
                else:
                    await self._kv_prefix.restore_if_lost(
                        served, system, tools, reasoning_effort=effort, role=role
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
        # On a pooled model the prime is pinned to jerv's slot by naming its role (the router
        # ignores a role off a pool). No other role is primed — their stable prefixes are a few
        # hundred tokens, or a long prefill nobody is waiting on that would compete with the
        # owner's first turn after boot.
        try:
            prime_turn = await self._prime(served, system, tools, role)
        except ChatSlotTakenError:
            return False  # a chat holds the slot: the next tick picks the target again
        if prime_turn is None:
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
                    served,
                    system,
                    tools,
                    prime_turn.usage.input_tokens,
                    reasoning_effort=effort,
                    role=role,
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

    def _has_pair(self, served: str) -> bool:
        pool = local_catalog.pool_of(served)
        return self._kv_prefix is not None and pool is not None and pool.chat_pair is not None

    def _prime_role(self, served: str) -> SlotRole:
        if self._kv_prefix is None or not self._has_pair(served):
            return WARM_ROLE
        return self._kv_prefix.warm_target(served) or WARM_ROLE

    def _on_pair_move(self, _served: str) -> None:
        """A chat moved to the other pair slot: tick now (nothing is due yet — the chat is
        mid-turn) and again once each quiet window can have passed, so the slot it left is
        re-warmed, or primed, soon after the owner stops rather than a whole interval later."""
        self._wake.set()
        with contextlib.suppress(RuntimeError):  # no running loop: the steady tick covers it
            loop = asyncio.get_running_loop()
            for quiet in (kv_prefix_mod.REWARM_QUIET_S, kv_prefix_mod.PRIME_QUIET_S):
                loop.call_later(quiet + 1.0, self._wake.set)

    async def _prime(
        self, served: str, system: str, tools: list[LlmTool], role: SlotRole
    ) -> LlmTurn | None:
        """One prime turn into `role`'s slot — exactly that slot on a chat pair, where the
        router would otherwise route a turn naming no chat to the warm member. There the prime
        first claims the slot (`claim_for_prime`), and the router refuses to send it if a chat
        took the slot meanwhile; either refusal raises `ChatSlotTakenError`. None on any other
        failure."""
        pair = self._has_pair(served)
        store = self._kv_prefix
        if pair and store is not None and not store.claim_for_prime(served, role):
            raise ChatSlotTakenError(f"{served}'s {role} slot holds or awaits a chat")
        slot_kw = exact_pin(role, pair)
        try:
            return await self._router.converse(
                AGENT_TURN_TASK,
                system=system,
                messages=[UserMessage(text="warmup")],
                tools=tools,
                max_tokens=1,
                **slot_kw,
            )
        except ChatSlotTakenError:
            log.info("warm_keeper.prime_skipped_slot_taken", model=served, role=str(role))
            raise
        except Exception as exc:  # noqa: BLE001 — gateway down/cold/no-room: retry, never raise
            log.info("warm_keeper.prime_failed", model=served, role=str(role), error=str(exc))
            return None
        finally:
            if pair and store is not None:
                store.release_prime(served, role)

    async def _tend_pair(
        self, served: str, system: str, tools: list[LlmTool], effort: str | None
    ) -> bool:
        """Keep the chat pair's warm half warm: re-warm from disk every pair slot due it, and
        prime one that no restore can serve. False when such a prime failed (the tick retries
        soon). The owner's turn comes first: a prime waits until the chat has been quiet
        `PRIME_QUIET_S` and neither chat slot is processing — a turn between tool rounds reads
        idle on `/slots`, which is why the quiet clock, not `/slots`, is the gate — and the
        router checks again right before sending it. Nothing is primed while this process does
        not know where the latest chat is (after a restart): the guess could be wrong."""
        store = self._kv_prefix
        if store is None:
            return True
        try:
            unservable = await store.rewarm_pair(served, system, tools, reasoning_effort=effort)
        except Exception:  # noqa: BLE001 — the disk layer must never wedge the keeper
            log.warning("warm_keeper.kv_rewarm_failed", model=served, exc_info=True)
            return True
        if not unservable or store.recent_slot(served) is None:
            return True
        if store.chat_quiet_s(served) < kv_prefix_mod.PRIME_QUIET_S:
            return True
        if await self._chat_busy(served):
            return True
        role = unservable[0]
        generation = self._generation
        try:
            turn = await self._prime(served, system, tools, role)
        except ChatSlotTakenError:
            return True  # a chat got there first; that slot is its now
        if turn is None:
            return False
        if generation != self._generation:
            return False
        try:
            await store.save_after_prime(
                served, system, tools, turn.usage.input_tokens, reasoning_effort=effort, role=role
            )
        except Exception:  # noqa: BLE001 — a failed save costs a future restore, nothing now
            log.warning("warm_keeper.kv_save_failed", model=served, exc_info=True)
        log.info("warm_keeper.pair_primed", model=served, role=str(role))
        return True

    async def _chat_busy(self, served: str) -> bool:
        """Whether either chat pair slot is processing — or `/slots` cannot say."""
        pool = local_catalog.pool_of(served)
        if pool is None or pool.chat_pair is None:
            return False
        try:
            slots = await self._gateway.slots(served)
        except Exception:  # noqa: BLE001 — unreadable reads as busy: no blind prefill
            return True
        wanted = {pool.slot(role) for role in pool.chat_pair}
        return any(
            isinstance(s, dict) and s.get("id") in wanted and s.get("is_processing") for s in slots
        )

    async def _tend_pooled_roles(
        self, served: str, system: str, tools: list[LlmTool], effort: str | None
    ) -> None:
        """The settled tick's pooled work: jerv's file into each empty restored role slot, and
        an idle conversation to disk. Every step is the store's own guarded best effort."""
        store = self._kv_prefix
        if store is None:
            return
        for role in DISK_RESTORED_ROLES:
            try:
                await store.restore_if_lost(
                    served, system, tools, reasoning_effort=effort, role=role
                )
            except Exception:  # noqa: BLE001 — the disk layer must never wedge the keeper
                log.warning("warm_keeper.kv_role_restore_failed", model=served, role=role)
        try:
            await store.save_idle_conversation(served)
        except Exception:  # noqa: BLE001
            log.warning("warm_keeper.kv_conversation_save_failed", model=served, exc_info=True)

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
            delay = self._interval_ready if settled else self._retry_delay()
            # Wait for the delay OR for a reported loss, whichever comes first. The hook's
            # main caller is the end-of-turn restore, so it fires just after the owner sends a
            # message — and sleeping out the rest of the interval is what made their NEXT
            # message pay the prefill the hook was added to prevent.
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
