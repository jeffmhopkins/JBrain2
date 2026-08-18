"""WarmKeeper.reconcile_once: keep the interactive (agent.turn) local model resident AND
primed with jerv's persona + tools, by issuing a throwaway turn down the SAME path a real
turn takes (router.converse) so the primed prefix matches. It no-ops once primed with the
current tool set, and re-primes when the hidden-tool set flips (ComfyUI liveness settling)
or the model is evicted.
"""

import asyncio
import contextlib
from collections.abc import Collection, Sequence
from typing import cast

from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import LlmTool
from jbrain.llm.warm_keeper import WarmKeeper

_DEFAULT = object()  # "argument not given", distinct from an explicit None


class _FakeGateway:
    def __init__(self, *, running: Collection[str] = (), props: object = _DEFAULT, saved=()):
        self._running = set(running)
        # An explicit `props=None` models a gateway that cannot say what it is running —
        # distinct from "not specified", hence the sentinel. No props, no honest fingerprint.
        self._props = (
            {"build_info": "b1-abc", "chat_template": "T", "total_slots": 2}
            if props is _DEFAULT
            else props
        )
        self._saved = set(saved)  # filenames that exist on the gateway's slot-save path
        self.actions: list[tuple[str, str]] = []  # (action, filename)

    async def running(self) -> set[str]:
        return set(self._running)

    async def props(self, served_model: str) -> dict:
        if not isinstance(self._props, dict):
            raise RuntimeError("props unavailable")
        return dict(self._props)

    async def slot_action(self, served_model, slot_id, action, *, filename=None):
        self.actions.append((action, str(filename)))
        if action == "restore" and filename not in self._saved:
            raise RuntimeError("no such state file")
        if action == "save":
            self._saved.add(str(filename))
            return {"n_saved": 27476}
        return {"n_restored": 27476}


class _FakeRouter:
    def __init__(self, served: str | None):
        self._served = served
        self.converses: list[dict[str, object]] = []
        self.fail = False

    async def primary_local_served_model(self) -> str | None:
        return self._served

    async def converse(self, task: str, *, system: str, messages, tools=(), max_tokens=4096):
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
        liveness=cast("_Liveness | None", liveness),
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
    assert r.converses == []  # nothing loaded, nothing primed


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
