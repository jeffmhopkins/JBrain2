"""Every caller of the shared `agent.turn` task names its pooled-engine slot role.

`agent.turn` defaults to the interactive slot (jerv's), so a background agent that forgets its
role silently evicts the persona prefix the owner's next chat turn reuses. These tests pin the
role at each construction site, check the loop carries it on every model call, and check the
direct gateway requests that bypass the router are pinned too (FLASH_NEXT_ENGINE_PLAN §4a).
"""

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from jbrain.agent.loop import AgentLoop
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, UserMessage
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, PROBE_ROLE, WARM_ROLE, SlotRole

SRC = Path(__file__).resolve().parents[2] / "src" / "jbrain"
FN = "qwen3.8-flash-next"
STANDARD = "gpt-oss-120b"

# Every AgentLoop construction in the app and the role expression it passes. A new site must
# be added here deliberately — and must name its role, or it lands in jerv's slot.
AGENT_LOOP_ROLES = {
    "api/agent.py": ["SlotRole.INTERACTIVE"],
    "api/intake.py": ["SlotRole.WORKSHOP"],
    "wiki/editor.py": ["SlotRole.WORKSHOP"],
    "agent/spawn.py": ["_CHILD_SLOT_ROLE"],
    "tasks/runner.py": ["self.slot_role"],
}
# LoopTurnExecutor defaults to the scheduled slot; a site that is not a scheduled-style
# background turn names its own.
EXECUTOR_ROLES = {
    "main.py": [None, None, None],
    "analysis/converse.py": ["SlotRole.WORKSHOP", "SlotRole.WORKSHOP"],
}


def _constructions(name: str) -> dict[str, list[str | None]]:
    found: dict[str, list[str | None]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called != name:
                continue
            role = next((kw.value for kw in node.keywords if kw.arg == "slot_role"), None)
            rel = path.relative_to(SRC).as_posix()
            found.setdefault(rel, []).append(ast.unparse(role) if role is not None else None)
    return found


def test_every_agent_loop_construction_names_its_slot_role() -> None:
    assert _constructions("AgentLoop") == AGENT_LOOP_ROLES


def test_every_loop_turn_executor_construction_is_accounted_for() -> None:
    assert _constructions("LoopTurnExecutor") == EXECUTOR_ROLES


def test_the_executor_default_is_the_scheduled_slot() -> None:
    from jbrain.tasks.runner import LoopTurnExecutor

    executor = LoopTurnExecutor(router=object(), registry=object())  # type: ignore[arg-type]
    assert executor.slot_role == SlotRole.SCHEDULED


# --- the loop carries its role on every model call ------------------------------------------


def _spied_router() -> tuple[LlmRouter, list[tuple[str, object]]]:
    turn = LlmTurn(text="hi", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(1, 1))
    router = LlmRouter(
        {"xai": FakeLlmClient(turns=[turn, turn, turn])}, {"agent.turn": ("xai", "grok-4.3")}
    )
    seen: list[tuple[str, object]] = []
    converse = router.converse
    converse_stream = router.converse_stream

    async def spy_converse(*a: Any, **kw: Any) -> LlmTurn:
        seen.append(("converse", kw.get("slot_role")))
        return await converse(*a, **kw)

    def spy_stream(*a: Any, **kw: Any) -> Any:
        seen.append(("converse_stream", kw.get("slot_role")))
        return converse_stream(*a, **kw)

    router.converse = spy_converse  # type: ignore[method-assign]
    router.converse_stream = spy_stream  # type: ignore[method-assign]
    return router, seen


@pytest.mark.parametrize("role", [None, SlotRole.SCHEDULED, SlotRole.RESEARCH])
async def test_the_loop_passes_its_role_on_converse(role: SlotRole | None) -> None:
    router, seen = _spied_router()
    loop = AgentLoop(router, ToolRegistry([]), slot_role=role)
    await loop.run(
        session=SessionContext(principal_kind="owner"),
        scopes=("general",),
        conversation=[UserMessage(text="hello")],
    )
    assert seen and all(r == role for _, r in seen)


async def test_the_loop_passes_its_role_on_the_stream() -> None:
    router, seen = _spied_router()
    loop = AgentLoop(router, ToolRegistry([]), slot_role=SlotRole.WORKSHOP)
    async for _ in loop.run_stream(
        session=SessionContext(principal_kind="owner"),
        scopes=("general",),
        conversation=[UserMessage(text="hello")],
    ):
        pass
    assert ("converse_stream", SlotRole.WORKSHOP) in seen
    assert all(r == SlotRole.WORKSHOP for _, r in seen)


async def test_the_loop_streams_a_child_turn_with_its_role() -> None:
    router, seen = _spied_router()
    loop = AgentLoop(router, ToolRegistry([]), slot_role=SlotRole.RESEARCH)
    await loop.run(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        conversation=[UserMessage(text="hello")],
        on_text=lambda _t: None,
    )
    assert seen == [("converse_stream", SlotRole.RESEARCH)]


# --- direct gateway requests to a pooled model are pinned --------------------------------


def _gateway(bodies: list[dict[str, Any]]) -> LocalGatewayClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/running":
            running = [{"model": m, "state": "ready"} for m in (FN, STANDARD)]
            return httpx.Response(200, json={"running": running})
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 3}},
        )

    return LocalGatewayClient("http://gw/v1", transport=httpx.MockTransport(handle))


async def _warm(gw: LocalGatewayClient, served: str) -> None:
    await gw._warm(served, system="you are jerv", tools=[])


async def _tool_probe(gw: LocalGatewayClient, served: str) -> None:
    await gw.tool_probe(served)


async def _text_probe(gw: LocalGatewayClient, served: str) -> None:
    await gw.text_probe(served)


async def _image_probe(gw: LocalGatewayClient, served: str) -> None:
    await gw.image_probe(served)


_SENDERS: list[tuple[Callable[[LocalGatewayClient, str], Any], SlotRole]] = [
    (_warm, WARM_ROLE),
    (_tool_probe, PROBE_ROLE),
    (_text_probe, PROBE_ROLE),
    (_image_probe, PROBE_ROLE),
]


@pytest.mark.parametrize(("send", "role"), _SENDERS)
async def test_every_gateway_request_to_a_pooled_model_is_pinned(
    send: Callable[[LocalGatewayClient, str], Any], role: SlotRole
) -> None:
    bodies: list[dict[str, Any]] = []
    await send(_gateway(bodies), FN)
    assert bodies and all(b.get("id_slot") == FLASH_NEXT_POOL.slot(role) for b in bodies)


@pytest.mark.parametrize(("send", "role"), _SENDERS)
async def test_a_standard_model_request_carries_no_slot(
    send: Callable[[LocalGatewayClient, str], Any], role: SlotRole
) -> None:
    bodies: list[dict[str, Any]] = []
    await send(_gateway(bodies), STANDARD)
    assert bodies and all("id_slot" not in b for b in bodies)
