"""WarmKeeper.reconcile_once: keep the interactive (agent.turn) local model resident AND
primed with jerv's persona + tools, by issuing a throwaway turn down the SAME path a real
turn takes (router.converse) so the primed prefix matches. It no-ops once primed with the
current tool set, and re-primes when the hidden-tool set flips (the model-gated canvas
pair) or the model is evicted.
"""

import asyncio
import contextlib
from collections.abc import Collection, Sequence
from typing import Any, cast

import pytest

from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm import kv_prefix
from jbrain.llm.kv_prefix import ChatSlotTakenError
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.slot_roles import SlotRole
from jbrain.llm.types import LlmTool, LlmTurn, LlmUsage
from jbrain.llm.warm_keeper import WarmKeeper

_DEFAULT = object()  # "argument not given", distinct from an explicit None


class _FakeGateway:
    def __init__(
        self,
        *,
        running: Collection[str] = (),
        props: object = _DEFAULT,
        saved=(),
    ):
        self._running = set(running)
        # An explicit `props=None` models a gateway that cannot say what it is running —
        # distinct from "not specified", hence the sentinel. No props, no honest fingerprint.
        self._props = (
            {"build_info": "b1-abc", "chat_template": "T", "total_slots": 2}
            if props is _DEFAULT
            else props
        )
        self.events: list[str] = []  # load/restore/save/prime, in the order they happened
        self.props_calls = 0  # how many times the slot target was derived

    async def running(self) -> set[str]:
        return set(self._running)

    async def load(self, served_model: str, *, warm_system=None, warm_tools=None) -> None:
        # The real load brings weights up and runs a one-token readiness probe. Passing no warm
        # system/tools means it does NOT prefill the persona — the keeper relies on that, since
        # prefilling here would spend the cost the restore exists to avoid.
        assert warm_system is None and warm_tools is None
        self.events.append("load")
        self._running.add(served_model)

    async def props(self, served_model: str) -> dict:
        self.props_calls += 1
        if served_model not in self._running:
            raise RuntimeError(f"{served_model} is not resident — refusing to read props")
        if not isinstance(self._props, dict):
            raise RuntimeError("props unavailable")
        return dict(self._props)


class _FakeRouter:
    def __init__(self, served: str | None, gateway: "_FakeGateway | None" = None):
        self._served = served
        self.converses: list[dict[str, object]] = []
        self.fail = False
        self.admitted: list[str] = []
        # Set for the no-coordinator configuration, where admission is inert and the keeper
        # has to do the load itself.
        self.admit_without_loading = False
        # Shared so the prime can be ordered against the load/restore the gateway records.
        self._gateway = gateway
        self.effort: str | None = "low"
        # Every prime's max_tokens. The store's exact-integer save gate is sound ONLY because
        # the prime generates exactly one token; nothing asserted it.
        self.max_tokens: list[int] = []
        # (slot_role, exact_slot) of every prime: a chat pair's prime is pinned exactly.
        self.pinned: list[tuple[object, bool]] = []

    async def primary_local_served_model(self) -> str | None:
        return self._served

    async def effective_reasoning_effort(self, task: str) -> str | None:
        # The live box carries a stored "low" on agent.turn — the value whose absence from
        # the gateway warm caused the 2026-08-23 mismatch. Distinctive, not None, so a
        # keeper that drops it on the way to the store cannot pass. Settable, so a test can
        # do what the owner does in Settings and change it under a running keeper.
        return self.effort

    async def admit_local_load(self, served_model: str) -> None:
        # The REAL one loads. `ensure_room` takes the slow path for a non-resident target and
        # calls `gateway.load` itself, so admission and load are one step in production. A fake
        # that only recorded the call hid a double load in the keeper for exactly one review
        # cycle; model the behaviour, not the signature.
        self.admitted.append(served_model)
        if self._gateway is not None and not self.admit_without_loading:
            await self._gateway.load(served_model)

    async def converse(
        self,
        task: str,
        *,
        system: str,
        messages,
        tools=(),
        max_tokens=4096,
        slot_role=None,
        exact_slot=False,
    ):
        self.max_tokens.append(max_tokens)
        self.pinned.append((slot_role, exact_slot))
        if self._gateway is not None:
            self._gateway.events.append("prime")
        if self.fail:
            raise RuntimeError("gateway cold")
        self.converses.append({"task": task, "system": system, "tools": list(tools)})
        # The keeper wants the prefill in cache, but it READS the turn's usage now: the
        # prime's input_tokens is what identifies the primed slot for the disk save.
        # DISTINCTIVE on purpose (not the real prefix's 28757): a keeper that hardcoded
        # the measured constant instead of reading the turn's own usage would still match
        # a realistic fake — this value only ever arrives by being read off this turn.
        return LlmTurn(
            text="",
            tool_calls=(),
            stop_reason="end_turn",
            usage=LlmUsage(input_tokens=12345, output_tokens=1),
        )


class _Registry:
    def schemas_for(self, scopes, allow=None, extra=(), hidden=()) -> list[LlmTool]:
        # Fewer tools when something is hidden, so a flip is observable via schemas_for.
        base = [LlmTool(name="web_search", description="d", input_schema={})]
        if "canvas" not in hidden:
            base.append(LlmTool(name="canvas", description="d", input_schema={}))
        return base


def _keeper(
    *,
    router: object,
    gateway: object,
    hold: Collection[str] = (),
    auto_restore: bool | BaseException = True,
) -> WarmKeeper:
    async def hold_loader() -> Collection[str]:
        return hold

    async def auto_restore_loader() -> bool:
        if isinstance(auto_restore, BaseException):
            raise auto_restore
        return auto_restore

    return WarmKeeper(
        gateway=cast(LocalGatewayClient, gateway),
        registry=cast(ToolRegistry, _Registry()),
        router=cast(LlmRouter, router),
        hold_loader=hold_loader,
        auto_restore_loader=auto_restore_loader,
        interval_ready=0.01,
        interval_wait=0.01,
    )


async def test_settles_without_priming_when_agent_turn_is_a_cloud_route() -> None:
    r = _FakeRouter(None)
    keeper = _keeper(router=r, gateway=_FakeGateway())
    assert await keeper.reconcile_once() is True
    assert r.converses == []


async def test_leaves_the_box_alone_while_code_mode_holds_it() -> None:
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_FakeGateway(), hold={"qwen3-coder-next"})
    assert await keeper.reconcile_once() is True
    assert r.converses == []


async def test_primes_via_the_real_turn_path_when_not_resident() -> None:
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_FakeGateway())
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1
    c = r.converses[0]
    assert c["task"] == "agent.turn"  # primes as the real task so effort+tools match a turn
    assert c["system"] and c["tools"]  # persona AND tools, not persona-only


async def test_no_reprime_once_primed_with_the_same_tool_set() -> None:
    # Resident + already primed this (model, hidden) → leave any live conversation be.
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_FakeGateway(running={"gpt-oss-120b"}))
    assert await keeper.reconcile_once() is True  # primes once
    assert await keeper.reconcile_once() is True  # no-op
    assert len(r.converses) == 1


async def test_reprimes_when_the_hidden_tool_set_flips() -> None:
    # The hidden set is model-gated (the canvas pair): a route change from an unqualified
    # model to a canvas-qualified one flips it, so the earlier prime no longer matches a
    # live turn and the keeper re-primes with the new tool set.
    r = _FakeRouter("gpt-oss-120b")  # unqualified: the canvas pair is hidden
    gw = _FakeGateway(running={"gpt-oss-120b", "qwen3.8-27b"})
    keeper = _keeper(router=r, gateway=gw)
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1 and len(cast(Sequence, r.converses[0]["tools"])) == 1
    r._served = "qwen3.8-27b"  # re-routed to a canvas-qualified model: nothing hidden
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 2 and len(cast(Sequence, r.converses[1]["tools"])) == 2


async def test_reprimes_after_the_model_is_evicted() -> None:
    gw = _FakeGateway(running={"gpt-oss-120b"})
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=gw)
    assert await keeper.reconcile_once() is True  # primes
    gw._running.clear()  # evicted (a coder swap, an image render)
    assert await keeper.reconcile_once() is True  # re-primes (also reloads via converse)
    assert len(r.converses) == 2


async def test_note_prefix_lost_forces_a_reprime_the_running_check_would_miss() -> None:
    """The bug this closes: an evict and its end-of-turn restore that BOTH complete between
    ticks leave the model running and the memo set, so the keeper reports settled and the
    owner's next jerv turn pays a cold prefill in the FOREGROUND. Residency now reports the
    dropped prefix edge-wise, which is the only signal available when the level never changes."""
    gw = _FakeGateway(running={"gpt-oss-120b"})
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=gw)
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1
    # Evicted and restored between ticks: still resident, so `running` looks unchanged.
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1  # ...and without the hook it would stay stale here

    keeper.note_prefix_lost("gpt-oss-120b")
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 2


async def test_note_prefix_lost_ignores_a_different_model() -> None:
    """A coder or vision model losing its prefix says nothing about jerv's — clearing the memo
    then would cost a needless 56s re-prime on every unrelated eviction."""
    gw = _FakeGateway(running={"gpt-oss-120b"})
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=gw)
    assert await keeper.reconcile_once() is True
    keeper.note_prefix_lost("qwen3-coder-next")
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1


async def test_retries_soon_when_the_prime_fails() -> None:
    r = _FakeRouter("gpt-oss-120b")
    r.fail = True
    keeper = _keeper(router=r, gateway=_FakeGateway())
    assert await keeper.reconcile_once() is False  # gateway down/cold → retry, never raise


async def test_a_hold_loader_error_does_not_wedge_the_keeper() -> None:
    r = _FakeRouter("gpt-oss-120b")

    async def boom_hold() -> Collection[str]:
        raise RuntimeError("settings read failed")

    keeper = WarmKeeper(
        gateway=cast(LocalGatewayClient, _FakeGateway()),
        registry=cast(ToolRegistry, _Registry()),
        router=cast(LlmRouter, r),
        hold_loader=boom_hold,
    )
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1  # degraded to "no hold" and primed


async def test_a_running_probe_error_is_treated_as_not_resident() -> None:
    class _BoomGateway(_FakeGateway):
        async def running(self) -> set[str]:
            raise RuntimeError("gateway unreachable")

    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_BoomGateway())
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1  # proceeded to prime rather than raising


async def test_run_survives_a_reconcile_error_and_keeps_looping() -> None:
    class _BoomRouter:
        async def primary_local_served_model(self) -> str | None:
            raise RuntimeError("boom")

    keeper = _keeper(router=_BoomRouter(), gateway=_FakeGateway())
    task = asyncio.create_task(keeper.run())
    await asyncio.sleep(0.03)  # several ticks, each raising and being swallowed
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_primes_then_keeps_looping_until_cancelled() -> None:
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_FakeGateway())
    task = asyncio.create_task(keeper.run())
    for _ in range(50):
        await asyncio.sleep(0.005)
        if r.converses:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert r.converses  # the boot reconcile primed the model


# --- the disk-backed primed prefix ------------------------------------------
# Measured on the box: a real prefix restores in ~0.3s and the prime after it returns in
# ~0.2s, against 70-110s cold. The prime is kept in BOTH paths on purpose — it is what makes
# a silently-useless restore (the SWA failure mode, or any stale-but-loadable file) degrade to
# a slow prime rather than a wrong answer.


async def test_auto_restore_off_stops_the_keeper_loading_a_model_that_is_gone() -> None:
    """The keeper is the SECOND auto-load path on this box, and it used to ignore the
    operator's switch entirely — so turning auto-reload off stopped residency restores while
    the keeper went on reloading the primary model every interval_wait seconds. On this
    hardware that meant watching a 68 GiB model reappear within five seconds of unloading it,
    which is the UI telling the operator something untrue.

    SETTLED, not "retry soon": returning False would spin the eager cadence forever against a
    switch that will never flip on its own."""
    r = _FakeRouter("gpt-oss-120b")
    g = _FakeGateway(running=set())
    k = _keeper(router=r, gateway=g, auto_restore=False)
    assert await k.reconcile_once() is True
    assert r.converses == []  # nothing primed
    # And nothing LOADED. The keeper now brings weights up itself before restoring a saved
    # prefix, so this gate has to sit in front of that too — an operator who switches automatic
    # reloading off and watches a 68 GiB model reappear anyway has been told something untrue.
    assert g.events == []
    assert r.admitted == []


async def test_flash_next_is_kept_loaded_even_with_auto_restore_off() -> None:
    """Flash-Next is the only model on its engine and serves every local task, so reloading it
    evicts nothing: the owner wants it loaded by default, whatever the standard switch says."""
    r = _FakeRouter("qwen3.8-flash-next")
    g = _FakeGateway(running=set())
    k = _keeper(router=r, gateway=g, auto_restore=False)
    await k.reconcile_once()
    assert len(r.converses) == 1


async def test_auto_restore_off_still_primes_a_model_that_is_already_resident() -> None:
    """The gate is on LOADING, not on priming. A resident model's warm prefix costs nothing to
    hold, and dropping it would make every first turn slow for no memory saved."""
    r = _FakeRouter("gpt-oss-120b")
    g = _FakeGateway(running={"gpt-oss-120b"})
    k = _keeper(router=r, gateway=g, auto_restore=False)
    assert await k.reconcile_once() is True
    assert len(r.converses) == 1


async def test_an_auto_restore_read_failure_leaves_the_keeper_working() -> None:
    """Defaults OPEN. This gate only suppresses a convenience reload, so a box that quietly
    stopped keeping its model warm because a settings query hiccupped would be the worse
    failure of the two."""
    r = _FakeRouter("gpt-oss-120b")
    g = _FakeGateway(running=set())
    k = _keeper(router=r, gateway=g, auto_restore=RuntimeError("settings down"))
    assert await k.reconcile_once() is True
    assert len(r.converses) == 1


async def test_a_failed_preload_still_reaches_the_prime_rather_than_short_circuiting() -> None:
    """Best-effort: a preload failure must not skip the prime, so the outcome is decided by the
    same path that decided it before this reordering.

    Note what this does NOT claim. In production the usual cause — no room — fails the prime
    too, because `converse` calls the same `ensure_room`; the reconcile then returns False and
    retries, as it always did. What is pinned here is only that the keeper still ATTEMPTS the
    prime, i.e. the preload is not a new early exit."""

    class _NoRoomRouter(_FakeRouter):
        async def admit_local_load(self, served_model: str) -> None:
            raise RuntimeError("no room")

    gw = _FakeGateway(running=())
    router = _NoRoomRouter("qwen3.8-27b-q4", gateway=gw)
    keeper = _keeper(gateway=gw, router=router)
    await keeper.reconcile_once()
    assert "prime" in gw.events  # the prime was reached
    assert "load" not in gw.events  # admission refused, so no load was attempted


# `qwen3-vl-30b` is served as `qwen3-vl-30b-a3b`: one of only two catalog entries whose id and
# served name DIFFER. The override store is keyed by catalog id, so every assertion about the
# override reaching the digest has to run on a model where the two cannot be confused — on
# `gpt-oss-120b` (id == served name) a lookup by the wrong key passes the test anyway.
_VL_ID = "qwen3-vl-30b"
_VL_SERVED = "qwen3-vl-30b-a3b"


async def _none_held() -> Collection[str]:
    return ()


class _FakeKvStore:
    """Scripted disk layer, recording call order against the gateway's event list."""

    def __init__(self, gateway: _FakeGateway) -> None:
        self._gateway = gateway
        self.restores: list[str] = []
        self.saves: list[tuple[str, int, str | None]] = []  # (served, tokens, effort)
        self.restore_result = False
        self.raise_on_restore = False
        self.raise_on_save = False
        self.roles: list[tuple[str, SlotRole | None]] = []  # the role each restore and save named
        self.idle_saves: list[str] = []
        # The chat pair: the member a prime goes to, and what a re-warm says it could not serve.
        self.target: SlotRole = SlotRole.INTERACTIVE
        self.unservable: list[SlotRole] = []
        self.pair_listeners: list[Any] = []
        self.rewarms = 0
        self.backgrounds: list[bool] = []
        # The pair state the keeper reads: where the latest chat is, how long the chat has been
        # quiet, whether a prime may claim its slot — and what it claimed and released.
        self.recent: int | None = 0
        self.quiet = float("inf")
        self.claim_ok = True
        self.claims: list[SlotRole] = []
        self.released: list[SlotRole] = []

    def recent_slot(self, served: str) -> int | None:
        return self.recent

    def chat_quiet_s(self, served: str) -> float:
        return self.quiet

    def claim_for_prime(self, served: str, role: SlotRole) -> bool:
        if self.claim_ok:
            self.claims.append(role)
        return self.claim_ok

    def release_prime(self, served: str, role: SlotRole) -> None:
        self.released.append(role)

    def add_pair_listener(self, listener: Any) -> None:
        self.pair_listeners.append(listener)

    def warm_target(self, served: str) -> SlotRole:
        return self.target

    async def rewarm_pair(
        self, served: str, system: str, tools, *, reasoning_effort: str | None = None
    ) -> list[SlotRole]:
        self.rewarms += 1
        self.roles.append(("rewarm", None))
        due, self.unservable = self.unservable, []
        return due

    async def restore_if_lost(
        self,
        served: str,
        system: str,
        tools,
        *,
        reasoning_effort: str | None = None,
        role: SlotRole | None = None,
        background: bool = False,
    ) -> bool:
        self._gateway.events.append("kv_restore")
        self.restores.append(served)
        self.roles.append(("restore", role))
        self.backgrounds.append(background)
        if self.raise_on_restore:
            raise RuntimeError("disk went away")
        return self.restore_result

    async def save_after_prime(
        self,
        served: str,
        system: str,
        tools,
        prime_tokens: int,
        *,
        reasoning_effort: str | None = None,
        role: SlotRole | None = None,
    ) -> bool:
        self._gateway.events.append("kv_save")
        self.saves.append((served, prime_tokens, reasoning_effort))
        self.roles.append(("save", role))
        if self.raise_on_save:
            raise RuntimeError("disk went away")
        return True

    async def save_idle_conversation(self, served: str) -> bool:
        self.idle_saves.append(served)
        return False


def _kept_with_store(
    served: str = "gpt-oss-120b", *, running: Collection[str] = ()
) -> tuple[WarmKeeper, _FakeGateway, _FakeRouter, _FakeKvStore]:
    gateway = _FakeGateway(running=running)
    router = _FakeRouter(served, gateway)
    store = _FakeKvStore(gateway)
    keeper = WarmKeeper(
        gateway=cast(LocalGatewayClient, gateway),
        registry=cast(ToolRegistry, _Registry()),
        router=cast(LlmRouter, router),
        hold_loader=_none_held,
        kv_prefix=store,  # type: ignore[arg-type]
    )
    return keeper, gateway, router, store


async def test_a_cold_prime_restores_from_disk_first_then_saves_what_it_primed() -> None:
    """The disk layer's whole contract with the keeper in one order: weights up, restore
    (so the prime is a cache hit, not a 60 s prefill), prime, save keyed by the prime's
    own token count. A save that ran before the prime would capture the wrong state —
    that ordering IS v1's fatal bug, so the order is the assertion."""
    keeper, gateway, router, store = _kept_with_store()
    assert await keeper.reconcile_once() is True
    assert gateway.events == ["load", "kv_restore", "prime", "kv_save"]
    assert store.saves == [("gpt-oss-120b", 12345, "low")], (
        "the count must be the prime turn's own usage, not any constant"
    )


async def test_a_settled_tick_still_probes_for_a_lost_prefix() -> None:
    """The memo cannot see a slot being overwritten by traffic; the store can. Once primed,
    every tick still asks the store — a cheap /slots read — so a single-slot clobber is
    healed off-turn instead of by the owner's next message."""
    keeper, gateway, router, store = _kept_with_store(running=("gpt-oss-120b",))
    assert await keeper.reconcile_once() is True  # primes and memoises
    events_after_prime = list(gateway.events)
    assert await keeper.reconcile_once() is True  # settled — but must still probe
    assert gateway.events == [*events_after_prime, "kv_restore"]
    assert len(router.converses) == 1, "the settled tick must not re-prime"


async def test_a_broken_disk_layer_never_wedges_the_keeper() -> None:
    """Best-effort means best-effort: a raising restore still lets the prime run, and a
    raising save still lets the reconcile settle — the disk layer can only ever add speed,
    never subtract availability."""
    keeper, gateway, router, store = _kept_with_store()
    store.raise_on_restore = True
    store.raise_on_save = True
    assert await keeper.reconcile_once() is True
    assert "prime" in gateway.events
    assert len(router.converses) == 1


async def test_changing_the_reasoning_effort_makes_the_keeper_re_prime() -> None:
    """The effort is in the RENDERED prompt — gpt-oss's harmony template writes a literal
    "Reasoning: <level>" into the leading tokens — and therefore in the store's fingerprint.

    Without it in the memo, the settled branch was unreachable-by-design: `want ==
    self._primed` stayed true across a Settings change, so the keeper never re-primed, no
    save ever ran, no file was ever written under the new identity, and every interactive
    turn re-prefilled the whole ~30k prefix until a restart or an eviction. The store's own
    fingerprint comment named that hazard and closed only its half."""
    keeper, _gateway, router, store = _kept_with_store(running=("gpt-oss-120b",))
    assert await keeper.reconcile_once() is True
    primed_once = len(router.converses)
    assert primed_once == 1

    # Settled: the same identity does not re-prime, only re-checks the slot.
    assert await keeper.reconcile_once() is True
    assert len(router.converses) == primed_once

    # The owner changes agent.turn's effort in Settings. Nothing unloads the model.
    router.effort = "high"

    assert await keeper.reconcile_once() is True
    assert len(router.converses) == primed_once + 1, "a new effort is a new prefix to prime"
    assert store.saves[-1][2] == "high", "and the save must be keyed by the effort it primed"


# ---- P4: the loop's own failure modes -----------------------------------------------------


async def test_a_loss_reported_during_a_prime_is_not_erased_by_that_prime() -> None:
    """The lost update. A cold prime is 60-200 s, and residency reporting a drop inside that
    window used to be overwritten by the prime's own completion re-asserting the memo — the
    model then sits resident, COLD, and believed primed, which is precisely the state the
    hook was added to prevent. The prime that no longer owns its generation says nothing."""
    keeper, _gateway, router, store = _kept_with_store(running=("gpt-oss-120b",))

    # Residency drops the model while the prime is in flight.
    async def converse_then_lose(*a: object, **kw: object):
        keeper.note_prefix_lost("gpt-oss-120b")
        return await _FakeRouter.converse(router, *a, **kw)  # type: ignore[arg-type]

    router.converse = converse_then_lose  # type: ignore[method-assign]

    assert await keeper.reconcile_once() is False, "a superseded prime has not settled"
    assert keeper._primed is None, "and it must not claim the slot it primed still holds it"
    assert store.saves == [], "nor save a prefix keyed to a slot that is gone"


async def test_a_reported_loss_cuts_the_sleep_short() -> None:
    """The edge trigger was only half an edge: the hook fired immediately and the keeper then
    slept out the rest of its interval. Its main production caller is the end-of-turn restore,
    so it lands just after the owner sends a message — making their NEXT message, inside that
    same minute, the one that pays the prefill the hook exists to prevent."""
    keeper, _gateway, _router, _store = _kept_with_store(running=("gpt-oss-120b",))
    keeper._interval_ready = 30.0  # a steady interval no test should ever wait out

    task = asyncio.create_task(keeper.run())
    await asyncio.sleep(0.05)  # let it settle and enter the sleep
    ticks = len(_store.restores) if hasattr(_store, "restores") else 0

    keeper.note_prefix_lost("gpt-oss-120b")
    await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert keeper._wake is not None
    # It woke: a second reconcile ran well inside the 30 s interval.
    assert len(getattr(_store, "restores", [])) > ticks or keeper._primed is not None


async def test_a_failing_prime_backs_off_instead_of_hammering_the_box() -> None:
    """A prime failing for a PERSISTENT reason retried at the eager cadence forever: ~17k log
    lines a day, and each attempt runs an admission that can EVICT to fit — so the keeper and
    the worker could trade the same 68 GB model back and forth every five seconds."""
    keeper, _gateway, _router, _store = _kept_with_store()
    keeper._interval_wait = 1.0
    keeper._interval_ready = 60.0

    assert keeper._retry_delay() == 1.0  # no failures yet
    keeper._failures = 1
    assert keeper._retry_delay() == 2.0
    keeper._failures = 4
    assert keeper._retry_delay() == 16.0
    keeper._failures = 99
    assert keeper._retry_delay() == 60.0, "and never slower than the steady poll"


async def test_the_prime_generates_exactly_one_token() -> None:
    """The load-bearing premise of the whole save path, and nothing asserted it.

    `save_after_prime` identifies the primed slot by an EXACT integer match on the prime's own
    `usage.input_tokens`. That works only because the server appends every sampled token to
    the slot's cache except the final stop token — so a `max_tokens=1` prime leaves the cache
    at precisely its prompt size. Raise it and no slot ever matches: the save is skipped and
    logged, the disk layer goes silently inert, and every test stays green. The store's own
    comment says "if slot_unidentified becomes chronic, look here first" — this is that look,
    made automatic."""
    keeper, _gateway, router, _store = _kept_with_store()
    assert await keeper.reconcile_once() is True
    assert router.max_tokens == [1], "a prime that generates more can never be identified"


class _PinRecordingRouter(_FakeRouter):
    def __init__(self, served: str) -> None:
        super().__init__(served)
        self.pins: list[object] = []

    async def converse(self, task: str, *, system: str, messages, tools=(), max_tokens=4096, **kw):
        self.pins.append(kw.get("slot_role", "absent"))
        return await super().converse(
            task, system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )


async def test_a_pooled_model_primes_only_jerv_s_slot_and_names_it() -> None:
    # On a pooled model an unpinned prime would land in whichever slot llama-server picks and
    # evict some other role's prefix; the router pins it from the role named here. No other
    # role is primed.
    fn = "qwen3.8-flash-next"
    r = _PinRecordingRouter(fn)
    keeper = _keeper(router=r, gateway=_FakeGateway(running={fn}))
    for _ in range(3):
        assert await keeper.reconcile_once() is True
    assert r.pins == [SlotRole.INTERACTIVE]


async def test_a_standard_model_prime_names_the_same_role_for_the_router_to_ignore() -> None:
    # The router pins only a pooled model, so the keeper need not know which kind it primes.
    r = _PinRecordingRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_FakeGateway(running={"gpt-oss-120b"}))
    assert await keeper.reconcile_once() is True
    assert r.pins == [SlotRole.INTERACTIVE]


async def test_the_prime_is_saved_and_restored_as_the_interactive_role() -> None:
    keeper, _gateway, _router, store = _kept_with_store(running={"gpt-oss-120b"})
    assert await keeper.reconcile_once() is True
    assert ("save", SlotRole.INTERACTIVE) in store.roles
    assert all(role is SlotRole.INTERACTIVE for _, role in store.roles)


async def test_a_settled_pooled_tick_refills_the_scheduled_slot_and_saves_an_idle_chat() -> None:
    # F4: jerv's saved prefix also serves the scheduled slot (same persona, tools and effort),
    # restored on an empty slot only — never a second prime — and the interactive slot's
    # conversation is offered to disk once idle.
    fn = "qwen3.8-flash-next"
    keeper, _gateway, _router, store = _kept_with_store(fn, running={fn})
    assert await keeper.reconcile_once() is True  # primes
    store.roles.clear()
    assert await keeper.reconcile_once() is True  # settled
    # The chat pair is tended by re-warm (the interactive restore is part of it).
    assert store.roles == [("rewarm", None), ("restore", SlotRole.SCHEDULED)]
    assert store.idle_saves == [fn]


async def test_a_standard_model_tends_no_other_role() -> None:
    keeper, _gateway, _router, store = _kept_with_store(running={"gpt-oss-120b"})
    assert await keeper.reconcile_once() is True
    store.roles.clear()
    assert await keeper.reconcile_once() is True
    assert store.roles == [("restore", SlotRole.INTERACTIVE)]
    assert store.idle_saves == []


# ---- the chat pair --------------------------------------------------------------------------


def _pair_keeper(
    *, slots: list[dict[str, object]] | None = None
) -> tuple[WarmKeeper, _FakeGateway, _FakeRouter, _FakeKvStore]:
    fn = "qwen3.8-flash-next"
    keeper, gateway, router, store = _kept_with_store(fn, running={fn})
    live = slots if slots is not None else [{"id": i, "is_processing": False} for i in range(10)]

    async def read(_served: str) -> list[dict[str, object]]:
        return live

    gateway.slots = read  # type: ignore[attr-defined]
    return keeper, gateway, router, store


async def test_a_pair_prime_goes_exactly_to_the_member_the_store_names() -> None:
    keeper, _gateway, router, store = _pair_keeper()
    store.target = SlotRole.INTERACTIVE_ALT
    assert await keeper.reconcile_once() is True
    assert router.pinned == [(SlotRole.INTERACTIVE_ALT, True)]
    assert ("restore", SlotRole.INTERACTIVE_ALT) in store.roles
    assert store.backgrounds[0] is True, "never waits on, or erases, a chat's slot"
    assert ("save", SlotRole.INTERACTIVE_ALT) in store.roles
    # The prime claimed its slot and let it go, whatever happened.
    assert store.claims == store.released == [SlotRole.INTERACTIVE_ALT]


async def test_a_prime_whose_slot_a_chat_holds_is_not_sent() -> None:
    keeper, _gateway, router, store = _pair_keeper()
    store.claim_ok = False
    assert await keeper.reconcile_once() is False, "the next tick picks the target again"
    assert router.pinned == []
    assert store.released == []


async def test_a_prime_the_router_refuses_at_dispatch_is_released() -> None:
    keeper, _gateway, router, store = _pair_keeper()
    assert await keeper.reconcile_once() is True
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    store.claims.clear()
    store.released.clear()

    async def taken(*_a: Any, **_kw: Any) -> Any:
        raise ChatSlotTakenError("a chat took it")

    router.converse = taken  # type: ignore[method-assign]
    assert await keeper.reconcile_once() is True, "the chat's now: nothing to retry"
    assert store.claims == store.released == [SlotRole.INTERACTIVE_ALT]


async def test_no_fallback_prime_between_a_chats_tool_rounds() -> None:
    # The new chat's first model call was just noted: the agent loop is running a tool, both
    # chat slots read idle — but the chat is not quiet, so nothing is primed.
    keeper, _gateway, router, store = _pair_keeper()
    assert await keeper.reconcile_once() is True
    router.pinned.clear()
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    store.quiet = 5.0
    assert await keeper.reconcile_once() is True
    assert router.pinned == []
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    store.quiet = kv_prefix.PRIME_QUIET_S + 1
    assert await keeper.reconcile_once() is True
    assert router.pinned == [(SlotRole.INTERACTIVE_ALT, True)]


async def test_after_a_restart_with_the_gate_closed_only_the_first_prime_runs() -> None:
    # A fresh api: the store knows no latest chat. The keeper primes its target once; the
    # other slot, which may hold the owner's live chat, is not primed on a guess.
    keeper, _gateway, router, store = _pair_keeper()
    store.recent = None
    assert await keeper.reconcile_once() is True
    assert router.pinned == [(SlotRole.INTERACTIVE, True)]
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    assert await keeper.reconcile_once() is True
    assert router.pinned == [(SlotRole.INTERACTIVE, True)]


async def test_a_slot_no_restore_can_serve_is_primed_exactly_and_saved_as_warm() -> None:
    keeper, _gateway, router, store = _pair_keeper()
    assert await keeper.reconcile_once() is True  # the first prime
    router.pinned.clear()
    store.roles.clear()
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    assert await keeper.reconcile_once() is True
    assert router.pinned == [(SlotRole.INTERACTIVE_ALT, True)]
    assert ("save", SlotRole.INTERACTIVE_ALT) in store.roles


async def test_no_fallback_prime_runs_while_a_chat_slot_is_busy() -> None:
    busy = [{"id": i, "is_processing": i == 0} for i in range(10)]
    keeper, _gateway, router, store = _pair_keeper(slots=busy)
    assert await keeper.reconcile_once() is True
    router.pinned.clear()
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    assert await keeper.reconcile_once() is True
    assert router.pinned == [], "the owner's turn comes first"


async def test_an_unreadable_slots_read_counts_as_busy() -> None:
    keeper, gateway, router, store = _pair_keeper()
    assert await keeper.reconcile_once() is True
    router.pinned.clear()

    async def broken(_served: str) -> list[dict[str, object]]:
        raise RuntimeError("gateway gone")

    gateway.slots = broken  # type: ignore[attr-defined]
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    assert await keeper.reconcile_once() is True
    assert router.pinned == []


async def test_a_failed_fallback_prime_asks_for_a_quick_retry() -> None:
    keeper, _gateway, router, store = _pair_keeper()
    assert await keeper.reconcile_once() is True
    router.fail = True
    store.unservable = [SlotRole.INTERACTIVE_ALT]
    assert await keeper.reconcile_once() is False


async def test_a_chat_moving_slots_wakes_the_keeper_now_and_after_each_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keeper, _gateway, _router, store = _pair_keeper()
    assert len(store.pair_listeners) == 1
    later: list[float] = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_later", lambda delay, _cb, *a: later.append(delay))
    keeper._wake.clear()
    store.pair_listeners[0]("qwen3.8-flash-next")
    assert keeper._wake.is_set()
    assert later == [kv_prefix.REWARM_QUIET_S + 1.0, kv_prefix.PRIME_QUIET_S + 1.0]


def test_a_move_with_no_running_loop_still_wakes() -> None:
    keeper, _gateway, _router, store = _pair_keeper()
    store.pair_listeners[0]("qwen3.8-flash-next")
    assert keeper._wake.is_set()


async def test_a_model_without_a_pair_never_rewarms() -> None:
    keeper, _gateway, _router, store = _kept_with_store(running={"gpt-oss-120b"})
    assert await keeper.reconcile_once() is True
    assert await keeper.reconcile_once() is True
    assert store.rewarms == 0
