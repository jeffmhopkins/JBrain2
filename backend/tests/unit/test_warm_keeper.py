"""WarmKeeper.reconcile_once: keep the interactive (agent.turn) local model resident AND
primed with jerv's persona + tools, so the first message after a restart is instant. The
reconciler only ever ADDS the one model, under the residency floor and the code-mode hold,
and no-ops once it's resident.
"""

import asyncio
import contextlib
from collections.abc import Collection
from typing import cast

from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import LlmTool
from jbrain.llm.warm_keeper import WarmKeeper


class _FakeGateway:
    def __init__(self, *, running: Collection[str] = (), load_error: bool = False):
        self._running = set(running)
        self._load_error = load_error
        self.loads: list[tuple[str, str | None, object]] = []

    async def running(self) -> set[str]:
        return set(self._running)

    async def load(self, served_model: str, *, warm_system=None, warm_tools=None) -> None:
        if self._load_error:
            raise LocalGatewayError("boom")
        self.loads.append((served_model, warm_system, warm_tools))
        self._running.add(served_model)


class _FakeResidency:
    def __init__(self, *, refuse: bool = False):
        self._refuse = refuse
        self.freed: list[str] = []

    async def free_room(self, served_model: str) -> None:
        if self._refuse:
            raise ResidencyError("won't fit even after evicting everything")
        self.freed.append(served_model)


class _FakeRouter:
    def __init__(self, served: str | None):
        self._served = served

    async def primary_local_served_model(self) -> str | None:
        return self._served


class _Registry:
    def schemas_for(self, scopes, allow=None, extra=(), hidden=()) -> list[LlmTool]:
        return [LlmTool(name="web_search", description="d", input_schema={})]


def _keeper(
    *,
    router: object,
    gateway: object,
    residency: object = None,
    hold: Collection[str] = (),
) -> WarmKeeper:
    async def hold_loader() -> Collection[str]:
        return hold

    return WarmKeeper(
        gateway=cast(LocalGatewayClient, gateway),
        residency=cast("ResidencyCoordinator | None", residency),
        registry=cast(ToolRegistry, _Registry()),
        liveness=None,
        router=cast(LlmRouter, router),
        hold_loader=hold_loader,
        interval_ready=0.01,
        interval_wait=0.01,
    )


async def test_settles_without_loading_when_agent_turn_is_a_cloud_route() -> None:
    gw = _FakeGateway()
    keeper = _keeper(router=_FakeRouter(None), gateway=gw)
    assert await keeper.reconcile_once() is True
    assert gw.loads == []


async def test_settles_without_loading_when_already_resident() -> None:
    gw = _FakeGateway(running={"gpt-oss-120b"})
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw)
    assert await keeper.reconcile_once() is True
    assert gw.loads == []  # a real turn / prior tick warmed it; nothing to do


async def test_leaves_the_box_alone_while_code_mode_holds_it() -> None:
    gw = _FakeGateway()
    # The hold names a DIFFERENT (code) model — the keeper must never load outside it.
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, hold={"qwen3-coder-next"})
    assert await keeper.reconcile_once() is True
    assert gw.loads == []


async def test_loads_and_primes_the_model_when_it_is_not_resident() -> None:
    gw, res = _FakeGateway(), _FakeResidency()
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, residency=res)
    assert await keeper.reconcile_once() is True
    assert res.freed == ["gpt-oss-120b"]  # made room under the floor first
    assert len(gw.loads) == 1
    served, warm_system, warm_tools = gw.loads[0]
    assert served == "gpt-oss-120b"
    assert warm_system and warm_tools  # primed with BOTH persona and tools, not persona-only


async def test_retries_soon_when_the_box_has_no_room() -> None:
    gw, res = _FakeGateway(), _FakeResidency(refuse=True)
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, residency=res)
    assert await keeper.reconcile_once() is False  # unsettled → run loop retries fast
    assert gw.loads == []  # refused before any load


async def test_retries_soon_when_the_gateway_load_fails() -> None:
    gw = _FakeGateway(load_error=True)
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, residency=_FakeResidency())
    assert await keeper.reconcile_once() is False  # gateway down/cold → retry, never raise


async def test_a_hold_loader_error_does_not_wedge_the_keeper() -> None:
    # A settings-read hiccup on the hold check must degrade to "no hold" (proceed), never raise.
    gw, res = _FakeGateway(), _FakeResidency()

    async def boom_hold() -> Collection[str]:
        raise RuntimeError("settings read failed")

    keeper = WarmKeeper(
        gateway=cast(LocalGatewayClient, gw),
        residency=cast("ResidencyCoordinator | None", res),
        registry=cast(ToolRegistry, _Registry()),
        liveness=None,
        router=cast(LlmRouter, _FakeRouter("gpt-oss-120b")),
        hold_loader=boom_hold,
    )
    assert await keeper.reconcile_once() is True
    assert len(gw.loads) == 1


async def test_a_running_probe_error_is_treated_as_not_resident() -> None:
    class _BoomRunning(_FakeGateway):
        async def running(self) -> set[str]:
            raise RuntimeError("gateway unreachable")

    gw = _BoomRunning()
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, residency=_FakeResidency())
    assert await keeper.reconcile_once() is True
    assert len(gw.loads) == 1  # proceeded to load rather than raising


async def test_run_survives_a_reconcile_error_and_keeps_looping() -> None:
    class _BoomRouter:
        async def primary_local_served_model(self) -> str | None:
            raise RuntimeError("boom")

    keeper = _keeper(router=_BoomRouter(), gateway=_FakeGateway())
    task = asyncio.create_task(keeper.run())
    await asyncio.sleep(0.03)  # several ticks, each raising and being swallowed
    assert not task.done()  # the loop absorbed the errors instead of dying
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_primes_then_keeps_looping_until_cancelled() -> None:
    gw, res = _FakeGateway(), _FakeResidency()
    keeper = _keeper(router=_FakeRouter("gpt-oss-120b"), gateway=gw, residency=res)
    task = asyncio.create_task(keeper.run())
    for _ in range(50):
        await asyncio.sleep(0.005)
        if gw.loads:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert gw.loads  # the boot reconcile primed the model
