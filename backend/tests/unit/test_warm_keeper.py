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


class _FakeGateway:
    def __init__(self, *, running: Collection[str] = ()):
        self._running = set(running)

    async def running(self) -> set[str]:
        return set(self._running)


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
) -> WarmKeeper:
    async def hold_loader() -> Collection[str]:
        return hold

    return WarmKeeper(
        gateway=cast(LocalGatewayClient, gateway),
        registry=cast(ToolRegistry, _Registry()),
        liveness=cast("_Liveness | None", liveness),
        router=cast(LlmRouter, router),
        hold_loader=hold_loader,
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
