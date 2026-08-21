"""WarmKeeper.reconcile_once: keep the interactive (agent.turn) local model resident AND
primed with jerv's persona + tools, by issuing a throwaway turn down the SAME path a real
turn takes (router.converse) so the primed prefix matches. It no-ops once primed with the
current tool set, and re-primes when the hidden-tool set flips (ComfyUI liveness settling)
or the model is evicted.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import cast

import pytest

from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm import llama_swap_config, local_catalog, warm_keeper
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import LlmTool
from jbrain.llm.warm_keeper import WarmKeeper

_DEFAULT = object()  # "argument not given", distinct from an explicit None


class _FakeGateway:
    def __init__(
        self,
        *,
        running: Collection[str] = (),
        props: object = _DEFAULT,
        saved=(),
        restore_raises: BaseException | None = None,
        slot_tokens: int | None = None,
    ):
        # What `/slots` reports the interactive slot is holding. None = no /slots at all, the
        # "we cannot tell" path. A number lets a test put somebody else's context in the slot.
        self._slot_tokens = slot_tokens
        self._running = set(running)
        # llama-server's own validity check: a state file whose per-layer K/V type does not
        # match the running server is a hard 400, not a silent wrong answer. Modelled so a test
        # can assert the keeper says WHY a restore missed.
        self._restore_raises = restore_raises
        # An explicit `props=None` models a gateway that cannot say what it is running —
        # distinct from "not specified", hence the sentinel. No props, no honest fingerprint.
        self._props = (
            {"build_info": "b1-abc", "chat_template": "T", "total_slots": 2}
            if props is _DEFAULT
            else props
        )
        self._saved = set(saved)  # filenames that exist on the gateway's slot-save path
        self.actions: list[tuple[str, str]] = []  # (action, filename)
        self.slot_ids: list[int] = []  # slot index each action targeted
        self.events: list[str] = []  # load/restore/save/prime, in the order they happened
        self.props_calls = 0  # how many times the slot target was derived

    async def running(self) -> set[str]:
        return set(self._running)

    async def slots(self, served_model: str) -> list[dict[str, object]]:
        if self._slot_tokens is None:
            raise RuntimeError("no /slots on this build")
        total = self._props.get("total_slots", 1) if isinstance(self._props, dict) else 1
        return [
            {"id": i, "n_prompt_tokens": self._slot_tokens if i == int(total) - 1 else 0}
            for i in range(int(total))
        ]

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

    async def slot_action(self, served_model, slot_id, action, *, filename=None):
        self.actions.append((action, str(filename)))
        self.slot_ids.append(slot_id)
        self.events.append(action)
        if served_model not in self._running:
            # llama-server has no slots for a model it has not loaded, and the gateway refuses
            # to reach a cold one rather than loading it outside the residency budget.
            raise RuntimeError(f"{served_model} is not resident — refusing a slot {action}")
        # llama-server serves slots 0..total_slots-1; anything else is not a slot.
        total = self._props.get("total_slots", 1) if isinstance(self._props, dict) else 1
        if not 0 <= slot_id < int(total):
            raise RuntimeError(f"invalid slot {slot_id} (server has {total})")
        if action == "restore" and self._restore_raises is not None:
            raise self._restore_raises
        if action == "restore" and filename not in self._saved:
            raise RuntimeError("no such state file")
        if action == "save":
            self._saved.add(str(filename))
            return {"n_saved": 27476}
        return {"n_restored": 27476}


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

    async def primary_local_served_model(self) -> str | None:
        return self._served

    async def admit_local_load(self, served_model: str) -> None:
        # The REAL one loads. `ensure_room` takes the slow path for a non-resident target and
        # calls `gateway.load` itself, so admission and load are one step in production. A fake
        # that only recorded the call hid a double load in the keeper for exactly one review
        # cycle; model the behaviour, not the signature.
        self.admitted.append(served_model)
        if self._gateway is not None and not self.admit_without_loading:
            await self._gateway.load(served_model)

    async def converse(self, task: str, *, system: str, messages, tools=(), max_tokens=4096):
        if self._gateway is not None:
            self._gateway.events.append("prime")
        if self.fail:
            raise RuntimeError("gateway cold")
        self.converses.append({"task": task, "system": system, "tools": list(tools)})
        return None  # the keeper discards the turn — it wants the prefill in cache


class _Registry:
    def schemas_for(self, scopes, allow=None, extra=(), hidden=()) -> list[LlmTool]:
        # Fewer tools when something is hidden, so a flip is observable via schemas_for.
        base = [LlmTool(name="web_search", description="d", input_schema={})]
        if "generate_image" not in hidden:
            base.append(LlmTool(name="generate_image", description="d", input_schema={}))
        return base


class _Liveness:
    def __init__(self, hidden: Collection[str] = (), *, boom: bool = False):
        self.hidden = set(hidden)
        self._boom = boom

    async def hidden_tools(self) -> Collection[str]:
        if self._boom:
            raise RuntimeError("comfyui probe failed")
        return set(self.hidden)


def _keeper(
    *,
    router: object,
    gateway: object,
    liveness: object = None,
    hold: Collection[str] = (),
    auto_restore: bool | BaseException = True,
    extra_args: dict[str, list[str]] | BaseException | Callable[[], object] | None = None,
) -> WarmKeeper:
    async def hold_loader() -> Collection[str]:
        return hold

    async def auto_restore_loader() -> bool:
        if isinstance(auto_restore, BaseException):
            raise auto_restore
        return auto_restore

    async def extra_args_loader() -> dict[str, list[str]]:
        if isinstance(extra_args, BaseException):
            raise extra_args
        if callable(extra_args):
            # A per-call source, for the tests that need the SECOND read to behave differently.
            return cast("dict[str, list[str]]", extra_args())
        return extra_args or {}

    return WarmKeeper(
        gateway=cast(LocalGatewayClient, gateway),
        registry=cast(ToolRegistry, _Registry()),
        liveness=cast("_Liveness | None", liveness),
        router=cast(LlmRouter, router),
        hold_loader=hold_loader,
        auto_restore_loader=auto_restore_loader,
        extra_args_loader=extra_args_loader,
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
    # ComfyUI comes up between primes → the hidden set changes → the earlier prime no longer
    # matches a live turn, so re-prime with the new tool set.
    r = _FakeRouter("gpt-oss-120b")
    live = _Liveness({"generate_image"})  # ComfyUI down: image tools hidden
    keeper = _keeper(router=r, gateway=_FakeGateway(running={"gpt-oss-120b"}), liveness=live)
    assert await keeper.reconcile_once() is True
    assert len(r.converses) == 1 and len(cast(Sequence, r.converses[0]["tools"])) == 1
    live.hidden = set()  # ComfyUI up now
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
        liveness=None,
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


async def test_a_liveness_probe_error_hides_nothing() -> None:
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=_FakeGateway(), liveness=_Liveness(boom=True))
    assert await keeper.reconcile_once() is True
    assert len(cast(Sequence, r.converses[0]["tools"])) == 2  # nothing hidden → full tool set


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


async def test_saves_the_primed_slot_so_the_next_cold_start_can_skip_the_prefill() -> None:
    gw = _FakeGateway()
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw)
    assert await keeper.reconcile_once() is True
    assert [a for a, _ in gw.actions] == ["restore", "save"]  # miss, then persist
    name = gw.actions[1][1]
    assert name.startswith("jerv-gpt-oss-120b-") and name.endswith(".bin")


async def test_restores_instead_of_reprimes_when_the_file_matches() -> None:
    gw = _FakeGateway()
    first = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw)
    assert await first.reconcile_once() is True  # cold: primes and saves

    # A fresh keeper over the same gateway — a restart, with the file still on disk.
    r2 = _FakeRouter("gpt-oss-120b")
    second = _keeper(router=r2, gateway=gw)
    assert await second.reconcile_once() is True
    assert gw.actions[-1][0] == "restore"  # ...and does NOT save again
    assert len(r2.converses) == 1  # the prime still runs; it is the proof the restore worked


async def test_no_save_after_a_restore() -> None:
    """Re-saving a byte-identical file every tick is pure churn on a ~1 GB artifact."""
    gw = _FakeGateway()
    assert await _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw).reconcile_once() is True
    gw.actions.clear()
    assert await _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw).reconcile_once() is True
    assert [a for a, _ in gw.actions] == ["restore"]


async def test_a_gateway_that_cannot_report_props_still_primes() -> None:
    """No build/template identity means no honest fingerprint. Skipping the cache entirely is
    the right failure: a cold prime is slow, a cache keyed on a guess would be wrong."""
    gw = _FakeGateway(props=None)
    r = _FakeRouter("gpt-oss-120b")
    assert await _keeper(router=r, gateway=gw).reconcile_once() is True
    assert gw.actions == [] and len(r.converses) == 1


async def test_the_fingerprint_moves_with_the_tool_set() -> None:
    """A liveness flip changes the tools, so it must change the filename — otherwise a restore
    would load a prefix for a DIFFERENT tool block and the reuse would silently miss."""
    gw = _FakeGateway()
    live = _Liveness({"generate_image"})
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, liveness=live)
    assert await keeper.reconcile_once() is True
    hidden_name = gw.actions[-1][1]

    live.hidden = set()
    keeper2 = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, liveness=live)
    assert await keeper2.reconcile_once() is True
    assert gw.actions[-1][1] != hidden_name


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


async def test_the_prefix_is_saved_into_a_slot_that_exists_on_a_single_slot_model() -> None:
    """The regression that made the whole disk KV cache inert on the interactive model.

    The slot index was hardcoded to 1, which is right only at `-np 2`. But `-np` is 1 unless the
    operator raised it, and the config generator clamps any `--spec-type` model to 1 regardless —
    and every Qwen3.8 entry serves `--spec-type draft-mtp`. So on the model the owner actually
    waits on, slot 1 did not exist: save and restore both raised, both call sites swallow, and
    the cache was silently never written. The symptom was a cold prefill on every restart with
    nothing in the log to say why."""
    gw = _FakeGateway(
        props={"build_info": "b1-abc", "chat_template": "T", "total_slots": 1},
    )
    keeper = _keeper(gateway=gw, router=_FakeRouter("qwen3.8-27b-q4"))
    assert await keeper.reconcile_once() is True
    assert [a for a, _ in gw.actions] == ["restore", "save"]
    # Every action targeted slot 0 — the only slot a single-slot server has.
    assert gw.slot_ids == [0, 0]
    # And the save actually landed, rather than being swallowed as "skipped".
    assert any(a == "save" for a, _ in gw.actions)
    assert gw._saved


async def test_the_prefix_still_uses_the_last_slot_when_a_second_one_is_configured() -> None:
    """The behaviour the hardcoded 1 was right about, preserved. With `-np 2` llama-server routes
    by longest matching prefix, so the interactive prefix belongs in the LAST slot and slot 0
    stays free for background traffic (verified on the box)."""
    gw = _FakeGateway(props={"build_info": "b1-abc", "chat_template": "T", "total_slots": 2})
    keeper = _keeper(gateway=gw, router=_FakeRouter("gpt-oss-120b"))
    assert await keeper.reconcile_once() is True
    assert gw.slot_ids and set(gw.slot_ids) == {1}


def test_the_interactive_slot_is_the_last_one_and_degrades_to_zero() -> None:
    """Derived from what the engine reports, never guessed. A missing or junk `total_slots`
    falls back to slot 0, which every running model has — a miss, never a wrong slot."""
    assert warm_keeper._interactive_slot(1) == 0
    assert warm_keeper._interactive_slot(2) == 1
    assert warm_keeper._interactive_slot(4) == 3
    assert warm_keeper._interactive_slot(None) == 0
    assert warm_keeper._interactive_slot("two") == 0
    assert warm_keeper._interactive_slot(0) == 0  # never negative


async def test_a_cold_model_is_loaded_before_the_restore_so_the_disk_cache_can_fire() -> None:
    """The cold start is the case this disk cache exists for, and it was the one case it could
    not serve.

    A restore needs slots to restore into, and a cold model has none — `props` and `slot_action`
    both refuse a non-resident model on purpose, since reaching them through the passthrough
    would make llama-swap load it outside the residency budget. But the priming completion was
    what loaded the model, and the restore ran before it. So on a cold box the restore never
    even attempted: it failed at `props`, returned None, and every restart paid a full cold
    prefill while the runbook said the prefix was persisted and restored."""
    gw = _FakeGateway(running=())  # cold box, no saved state
    router = _FakeRouter("qwen3.8-27b-q4", gateway=gw)
    keeper = _keeper(gateway=gw, router=router)
    assert await keeper.reconcile_once() is True
    # Weights first, then the slot work, then the prime that reuses it.
    assert gw.events[0] == "load"
    assert gw.events.index("load") < gw.events.index("prime")
    # Admission ran before the load — the keeper must not co-load past the memory budget.
    assert router.admitted == ["qwen3.8-27b-q4"]
    # And the restore was actually ATTEMPTED, which is the whole point: reaching `slot_action`
    # at all means `_slot_target` got its props, which means the model was resident by then. On
    # the old ordering props raised on the cold model, `_slot_target` returned None, and no slot
    # call was ever made — so this list would not contain "restore" at all.
    assert "restore" in gw.events
    assert gw.events.index("restore") > gw.events.index("load")
    # A slot was computed from the ENGINE's own total_slots (2 in this fixture → the last one),
    # rather than guessed — which is the other half of why the restore can now land.
    assert gw.slot_ids and set(gw.slot_ids) == {1}


async def test_a_cold_start_restores_a_saved_prefix_instead_of_prefilling_it() -> None:
    """End to end: with a state file already on disk, a cold reconcile restores it before the
    prime, so the prime is the cheap confirmation rather than the expensive prefill."""
    gw = _FakeGateway(running=())
    router = _FakeRouter("qwen3.8-27b-q4", gateway=gw)
    keeper = _keeper(gateway=gw, router=router)
    # First pass on an empty box: nothing to restore, so it primes and SAVES.
    assert await keeper.reconcile_once() is True
    saved_names = {name for action, name in gw.actions if action == "save"}
    assert saved_names, "a cold prime must persist its prefix for the next boot"
    # Now simulate the next boot: same fingerprint, model cold again, keeper state reset.
    gw2 = _FakeGateway(running=(), saved=tuple(saved_names))
    router2 = _FakeRouter("qwen3.8-27b-q4", gateway=gw2)
    keeper2 = _keeper(gateway=gw2, router=router2)
    assert await keeper2.reconcile_once() is True
    # The restore HIT, so nothing was re-saved — the prefill was skipped, not repeated.
    assert ("restore", next(iter(saved_names))) in gw2.actions
    assert not [a for a, _ in gw2.actions if a == "save"]


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


async def test_a_failed_slot_save_is_swallowed_but_carries_its_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The swallow is deliberate — no `--slot-save-path`, no disk, no permission is a benign
    gateway, not a broken keeper. But it is also what hid an always-invalid slot index for as
    long as the index was hardcoded, so the log line has to name the cause.

    Nothing exercised this arm, which meant the `error=` added for exactly that reason was
    itself unverified."""

    class _SaveFails(_FakeGateway):
        async def slot_action(self, served_model, slot_id, action, *, filename=None):
            if action == "save":
                raise RuntimeError("no --slot-save-path on this gateway")
            return await super().slot_action(served_model, slot_id, action, filename=filename)

    gw = _SaveFails(running=())
    keeper = _keeper(gateway=gw, router=_FakeRouter("qwen3.8-27b-q4", gateway=gw))
    assert await keeper.reconcile_once() is True  # a failed save is never fatal
    # structlog writes straight to stdout here rather than through stdlib logging, so this
    # reads the rendered line rather than caplog's records. The RENDERER differs by
    # environment — key=value from the console renderer locally, JSON in CI — so assert on
    # both spellings rather than on whichever one this machine happens to produce.
    logged = capsys.readouterr().out
    assert "slot_save_skipped" in logged, "a swallowed save must still be logged"
    # The CAUSE, so a benign gateway is distinguishable from a broken keeper...
    assert "no --slot-save-path" in logged
    # ...and which slot it tried, which is what would have made the hardcoded index obvious.
    compact = logged.replace(" ", "")  # spacing differs between renderers too
    assert "slot=1" in compact or '"slot":1' in compact


# The state file's name has to move whenever the LAUNCH LINE moves, not just when the prompt
# does. MEASURED 2026-08-21: `-ctk/-ctv q8_0` landed on the Qwen3.8 27B family and nothing else
# in the fingerprint's inputs changed with it, so every existing file kept a live-looking name
# and llama-server answered each restore with `400 mismatched key type (8 != 1, layer 0)`.


# `qwen3-vl-30b` is served as `qwen3-vl-30b-a3b`: one of only two catalog entries whose id and
# served name DIFFER. The override store is keyed by catalog id, so every assertion about the
# override reaching the digest has to run on a model where the two cannot be confused — on
# `gpt-oss-120b` (id == served name) a lookup by the wrong key passes the test anyway.
_VL_ID = "qwen3-vl-30b"
_VL_SERVED = "qwen3-vl-30b-a3b"


async def test_an_operator_extra_arg_moves_the_state_files_name() -> None:
    """The remote escape hatch is exactly how `-ctk q8_0` gets flipped on this box — the owner
    has no terminal, so the settings PUT is the way it happens. If the name does not follow, the
    next restart restores f16 cells into a q8 server and pays the cold prefill instead."""
    gw = _FakeGateway()
    assert await _keeper(router=_FakeRouter(_VL_SERVED), gateway=gw).reconcile_once() is True
    f16_name = gw.actions[-1][1]

    gw2 = _FakeGateway()
    keeper = _keeper(
        router=_FakeRouter(_VL_SERVED),
        gateway=gw2,
        extra_args={_VL_ID: ["-ctk", "q8_0", "-ctv", "q8_0"]},
    )
    assert await keeper.reconcile_once() is True
    assert gw2.actions[-1][1] != f16_name


async def test_an_override_keyed_by_the_served_name_is_not_the_operators_override() -> None:
    """The lookup key is the subtle part, and it is invisible on the models this suite otherwise
    uses, where id == served name. Keyed by the served name instead, the owner's `-ctk q8_0` on
    the VL model would never enter the digest: the file would keep a live-looking name and every
    restore would 400 — the exact defect this argument exists to prevent, unnoticed."""
    gw = _FakeGateway()
    plain = _keeper(router=_FakeRouter(_VL_SERVED), gateway=gw)
    assert await plain.reconcile_once() is True
    baseline = gw.actions[-1][1]

    gw2 = _FakeGateway()
    # Keyed by the SERVED name — not where `llm_local_extra_args` stores it, so it is not this
    # model's override and must leave the name alone.
    wrong_key = _keeper(
        router=_FakeRouter(_VL_SERVED), gateway=gw2, extra_args={_VL_SERVED: ["-ctk", "q8_0"]}
    )
    assert await wrong_key.reconcile_once() is True
    assert gw2.actions[-1][1] == baseline


async def test_the_catalog_serving_flags_are_part_of_the_launch_line() -> None:
    """Not only the operator's half. The q8 switch that prompted all this shipped in the CATALOG
    (`local_catalog`, three Qwen3.8 27B entries), so a digest that saw only the saved overrides
    would have been blind to the very change it exists to catch."""
    keeper = _keeper(router=_FakeRouter("qwen3.8-27b-q4"), gateway=_FakeGateway())
    args = await keeper._server_args("qwen3.8-27b-q4")
    assert args is not None
    assert "-ctk" in args and args[args.index("-ctk") + 1] == "q8_0"


async def test_an_override_store_that_has_never_been_read_skips_the_slot_cache() -> None:
    """A PARTIAL launch line is the one answer that does damage. It yields a name that differs
    from the working path's, so the restore misses AND the save then writes ~2 GB under the
    wrong name — on a volume only the deploy prunes, to be missed again next boot. Skipping
    entirely leaves nothing behind, which is how the props failure already fails.

    It is not free, though, and the cost is bigger than one prime: `reconcile_once` records the
    prime BEFORE it saves, so the next tick short-circuits and the save is never retried. Hence
    the memo in the test below — this bare-skip path is only for a store that has never once
    answered."""
    gw = _FakeGateway()
    r = _FakeRouter("gpt-oss-120b")
    keeper = _keeper(router=r, gateway=gw, extra_args=RuntimeError("settings read failed"))
    assert await keeper.reconcile_once() is True
    assert gw.actions == []  # neither restored nor saved
    assert len(r.converses) == 1  # but still primed, the normal cold path


async def test_a_later_store_hiccup_reuses_the_last_good_overrides_and_still_saves() -> None:
    """One transient DB read must not cost the disk cache for the life of the process. It would:
    the keeper marks itself primed before saving, so a skipped save is never retried while the
    model stays resident — and this box deliberately keeps it resident. Stale-but-stable names
    one real launch line, and the prime after a restore is what proves the bytes were useful."""
    calls = 0

    def flaky() -> dict[str, list[str]]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("settings read failed")
        return {_VL_ID: ["-ctk", "q8_0"]}

    gw = _FakeGateway()
    keeper = _keeper(router=_FakeRouter(_VL_SERVED), gateway=gw, extra_args=flaky)
    assert await keeper.reconcile_once() is True
    assert gw.actions and [a for a, _ in gw.actions] == ["restore", "save"]
    good = await keeper._server_args(_VL_SERVED)  # the read that worked

    # Every read from here on fails. The launch line must still resolve, still carry the
    # operator's flag, and be IDENTICAL — a different answer is the ~2 GB mislabelled file
    # this argument exists to prevent, and no answer costs the disk cache until a reboot.
    assert calls > 1
    stale = await keeper._server_args(_VL_SERVED)
    assert stale == good and stale is not None and "-ctk" in stale

    # A keeper whose FIRST read fails has nothing to reuse, so it skips rather than guessing.
    fresh = _keeper(
        router=_FakeRouter(_VL_SERVED),
        gateway=_FakeGateway(),
        extra_args=RuntimeError("settings read failed"),
    )
    assert await fresh._server_args(_VL_SERVED) is None


async def test_the_slot_target_is_derived_once_per_reconcile() -> None:
    """Restore and save used to derive it independently, with the 70-110 s prime in between. A
    settings PUT writes the override store BEFORE its best-effort unload, so a flag flipped from
    the PWA mid-prime made the save file the RUNNING server's KV under the NEXT launch line's
    name. `-ctk` is caught on the way back in; `--swa-full` is not, and the wasted restore then
    looks like a hit."""
    gw = _FakeGateway()
    assert await _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw).reconcile_once() is True
    assert gw.actions and [a for a, _ in gw.actions] == ["restore", "save"]
    assert gw.props_calls == 1, "the launch line was read twice, either side of the prime"


async def test_swa_full_is_part_of_the_launch_line_even_though_no_arg_carries_it() -> None:
    """`--swa-full` is rendered from the catalog's `kv_full_history`, not from
    `extra_server_args`, so a digest that read only the arg tuples would miss it. It is the
    flag with the worst failure mode on this box: llama.cpp does not validate `n_swa`, so a
    mismatched restore reports full success and is then discarded — measured 69,373 ms against
    194 ms. Blind to it, the keeper would count a wasted restore as a hit and skip the save."""
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=_FakeGateway())
    assert "--swa-full" in (await keeper._server_args("gpt-oss-120b") or ())
    # And absent where the catalog does not ask for it, or it would be noise in every name.
    assert "--swa-full" not in (await keeper._server_args("qwen3.8-27b-q4") or ())


async def test_a_missed_restore_says_why() -> None:
    """`400 mismatched key type` and "no file yet on a fresh box" are the same silence at this
    call site, and they call for opposite reactions: the first says the fingerprint failed to
    rotate and EVERY restore from here on pays the cold prefill. The swallow stays — a miss is
    not an error — but the reason has to reach the log."""
    import structlog.testing

    gw = _FakeGateway(
        restore_raises=RuntimeError("HTTP 400: mismatched key type (8 != 1, layer 0)")
    )
    with structlog.testing.capture_logs() as logs:
        assert await _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw).reconcile_once()
    missed = [e for e in logs if e.get("event") == "warm_keeper.slot_restore_missed"]
    assert missed, "a rejected restore left no trace"
    assert "mismatched key type" in missed[0]["error"]


async def test_an_uncatalogued_served_model_gets_no_slot_cache_rather_than_a_blind_name() -> None:
    """Nothing outside the catalog is routed today, but the fail-safe direction still matters:
    for a served name we cannot look up we can see NONE of the launch line — the maximally
    partial answer — so the name would be keyed on a digest blind to how the thing is served."""
    keeper = _keeper(router=_FakeRouter("something-the-operator-runs"), gateway=_FakeGateway())
    assert await keeper._server_args("something-the-operator-runs") is None


# The digest reassembles a SUBSET of what `llama_swap_config.render` actually puts on the command
# line, and that subset is hand-maintained. `--swa-full` is why this test exists: it is rendered
# from the catalog's `kv_full_history` rather than from `extra_server_args`, so the first version
# of the fingerprint could not see it — the same shape of bug as the `-ctk` one it was written to
# fix. This walks the REAL renderer's output and forces every flag to be accounted for, so the
# next derived flag added to `render` fails here instead of silently leaving stale files valid.

# Covered by the digest, but through `/props` rather than through `_server_args`.
_VIA_PROPS = {"-c", "-np"}

# KNOWINGLY outside the digest. Each entry needs a reason it cannot invalidate a state file.
_DIGEST_BLIND = {
    # Constant for every model on every box — they cannot drift without a code change that
    # also moves `build_info` or the catalog, and neither changes the KV cells' layout.
    "--host",
    "--port",
    "-ngl",
    "-fa",
    "-ub",
    "--jinja",
    "--no-warmup",
    "--metrics",
    "--slots",  # expose endpoints; the slots one is what the keeper itself calls
    "--no-mmap",  # how the weights are read off disk, not what the KV holds
    "--slot-save-path",  # where the file goes, not what is in it
    "--cache-reuse",  # prefix-reuse threshold; does not change a stored cell
    "--ctx-checkpoints",
    "--checkpoint-min-step",  # checkpoint COUNT, not cell layout
    "--reasoning-format",  # output channel parsing, downstream of the KV entirely
    "--mmproj",  # the vision projector: not in the text KV a jerv prime writes
    # Operator-settable (llm_local_image_min_tokens) and so the one real gap here, but it is a
    # vision-token floor: it moves image prompts, never the persona+tools prefix this file holds.
    "--image-min-tokens",
    # The weights. THE known hole: a vendor re-quant under the same catalog id keeps the served
    # name, layer count and KV row size, so an old file restores cleanly and every turn then runs
    # on KV computed from different weights — wrong, not merely slow. Tracked separately; adding
    # the file's size/mtime to the digest is what closes it.
    "-m",
}


def _rendered_flags(root: Path, model_id: str, **kw: object) -> set[str]:
    """Every flag token `render` emits for one real catalog model."""
    manifest = json.loads(local_catalog._manifest([model_id]))
    entry = manifest[0]
    (root / model_id).mkdir()
    (root / model_id / "model-Q8_0.gguf").write_bytes(b"\0")
    if entry.get("mmproj_include"):
        (root / model_id / "mmproj-f16.gguf").write_bytes(b"\0")
    entry["gguf_include"] = "*.gguf"
    entry["mmproj_include"] = "mmproj*.gguf" if entry.get("mmproj_include") else None
    text = llama_swap_config.render([entry], str(root), **kw)  # type: ignore[arg-type]
    cmd = next(ln for ln in text.splitlines() if ln.startswith("      ")).split()
    return {t for t in cmd if t.startswith("-")}


@pytest.mark.parametrize("model_id", ["gpt-oss-120b", "qwen3.8-27b-q4", "qwen3-vl-30b"])
async def test_every_rendered_flag_is_either_in_the_digest_or_knowingly_out_of_it(
    tmp_path: Path, model_id: str
) -> None:
    """Three models chosen to span the derived flags: gpt-oss carries `--swa-full`, the 27B
    carries `-ctk/-ctv` and `--spec-type`, the VL model carries `--mmproj` and an id that
    differs from its served name."""
    model = local_catalog.get(model_id)
    assert model is not None
    seen = _rendered_flags(tmp_path, model_id)
    keeper = _keeper(router=_FakeRouter(model.served_model), gateway=_FakeGateway())
    digested = set(await keeper._server_args(model.served_model) or ())

    unaccounted = seen - digested - _VIA_PROPS - _DIGEST_BLIND
    assert not unaccounted, (
        f"{sorted(unaccounted)} reach llama-server for {model_id} but nothing in the state "
        "file's name moves when they do. Add them to WarmKeeper._server_args, or to "
        "_DIGEST_BLIND with a reason they cannot invalidate a saved KV slot."
    )


async def test_the_tripwire_would_actually_fire(tmp_path: Path) -> None:
    """The test above passes trivially if the flag set it reads is empty or the blind list
    swallows everything. Pin both: the renderer really does emit `--swa-full` for gpt-oss, and
    it really is the digest — not the blind list — that accounts for it."""
    seen = _rendered_flags(tmp_path, "gpt-oss-120b")
    assert "--swa-full" in seen
    assert "--swa-full" not in _VIA_PROPS | _DIGEST_BLIND
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=_FakeGateway())
    assert "--swa-full" in (await keeper._server_args("gpt-oss-120b") or ())


# A saved file that does NOT hold the primed prefix is worse than no file: it is NAMED as the
# prefix, so every later cold start restores it, pays the full prefill anyway, and keeps doing
# so until something overwrites it. The feature silently inverts.


async def test_a_slot_that_lost_the_prefix_is_not_saved() -> None:
    """MEASURED 2026-08-21 on gpt-oss-120b: the keeper primed, then saved a slot holding
    2,164 tokens against a ~36k prefix. That model serves `-np 1` while note.extract,
    entity.disambiguate and fact.adjudicate all route to it, so jerv shares its single slot
    with every background task and the prime is evicted between the prime and the save."""
    gw = _FakeGateway(slot_tokens=2164)
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw)
    assert await keeper.reconcile_once() is True
    assert [a for a, _ in gw.actions] == ["restore"], "it saved somebody else's context"


async def test_a_slot_that_still_holds_the_prefix_is_saved() -> None:
    """The guard must not cost the feature its whole point. A slot holding the prefix saves
    exactly as before."""
    gw = _FakeGateway(slot_tokens=40_000)
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw)
    assert await keeper.reconcile_once() is True
    assert "save" in [a for a, _ in gw.actions]


async def test_a_build_without_slots_still_saves() -> None:
    """No `/slots`, no opinion. The check is an extra veto on a measured failure, never a new
    precondition — a gateway that cannot answer must behave exactly as it did before."""
    gw = _FakeGateway()  # slots() raises
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw)
    assert await keeper.reconcile_once() is True
    assert "save" in [a for a, _ in gw.actions]
