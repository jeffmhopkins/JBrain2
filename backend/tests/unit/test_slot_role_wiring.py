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


# Router calls whose task is not a literal yet need no role, each with why: the task can only
# be one that `TASK_ROLES` already maps to a non-interactive slot.
ROUTER_CALL_ALLOWLIST = {
    ("api/debug.py", "_run_vision"),  # body.task is checked against the vision tasks first
    ("wiki/lint.py", "_verify_batch"),  # always a wiki.lint.* task
}
_ROUTER_METHODS = {"complete", "converse", "converse_stream", "context_window"}


def _agent_turn_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
            if isinstance(value, ast.Constant) and value.value == "agent.turn":
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _names_a_role(call: ast.Call) -> bool:
    # `slot_role=...`, or a `**` mapping whose expression says it carries the slot role (the
    # callers that pass it only when set, so an unset role keeps the old call shape).
    return any(
        kw.arg == "slot_role" or (kw.arg is None and "slot" in ast.unparse(kw.value))
        for kw in call.keywords
    )


def test_every_agent_turn_router_call_names_its_slot_role() -> None:
    """A router call under `agent.turn` (a literal, a module constant bound to it, or a task
    the call site does not fix) lands in jerv's slot unless it names a role."""
    missing: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        turn_names = _agent_turn_names(tree)
        rel = path.relative_to(SRC).as_posix()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in _ROUTER_METHODS or not node.args:
                    continue
                if "router" not in ast.unparse(node.func.value).lower():
                    continue
                task = node.args[0]
                if isinstance(task, ast.Constant) and task.value != "agent.turn":
                    continue
                if isinstance(task, ast.Name) and task.id.isupper() and task.id not in turn_names:
                    continue
                if _names_a_role(node) or (rel, fn.name) in ROUTER_CALL_ALLOWLIST:
                    continue
                missing.append(f"{rel}:{node.lineno} {fn.name}({ast.unparse(task)})")
    assert missing == []


def test_every_gateway_completion_post_is_pinned() -> None:
    """A direct POST to a model's completions endpoint bypasses the router, so it must pin its
    own slot or it evicts whichever role's prefix llama-server's LRU pick lands on."""
    path = SRC / "llm" / "local_gateway.py"
    unpinned: list[str] = []
    for fn in ast.walk(ast.parse(path.read_text())):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        posts = any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and "/completion" in n.value
            for n in ast.walk(fn)
        )
        pins = any(isinstance(n, ast.Attribute) and n.attr == "_pin_slot" for n in ast.walk(fn))
        if posts and not pins:
            unpinned.append(fn.name)
    assert unpinned == []


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


def _gateway(
    bodies: list[dict[str, Any]], *, live_slots: int = 8, slot_reads: list[int] | None = None
) -> LocalGatewayClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/running":
            running = [{"model": m, "state": "ready"} for m in (FN, STANDARD)]
            return httpx.Response(200, json={"running": running})
        if request.url.path.endswith("/slots"):
            if slot_reads is not None:
                slot_reads.append(1)
            return httpx.Response(200, json=[{"id": i} for i in range(live_slots)])
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 3}},
        )

    return LocalGatewayClient("http://gw/v1", transport=httpx.MockTransport(handle))


async def _warm(gw: LocalGatewayClient, served: str) -> None:
    await gw._warm(served, system="you are jerv", tools=[])


async def _bare_warm(gw: LocalGatewayClient, served: str) -> None:
    await gw._warm(served)


async def _tool_probe(gw: LocalGatewayClient, served: str) -> None:
    await gw.tool_probe(served)


async def _text_probe(gw: LocalGatewayClient, served: str) -> None:
    await gw.text_probe(served)


async def _image_probe(gw: LocalGatewayClient, served: str) -> None:
    await gw.image_probe(served)


_SENDERS: list[tuple[Callable[[LocalGatewayClient, str], Any], SlotRole]] = [
    (_warm, WARM_ROLE),
    # A warm without a persona (a restore, a smoke load, an engine switch) is a probe: in
    # jerv's slot it would overwrite the persona prefix.
    (_bare_warm, PROBE_ROLE),
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


@pytest.mark.parametrize(("send", "role"), _SENDERS)
async def test_a_stale_live_layout_sends_unpinned_rather_than_wrapping(
    send: Callable[[LocalGatewayClient, str], Any], role: SlotRole
) -> None:
    # A config stamped before the pool serves four slots; llama-server would wrap id_slot 7
    # onto slot 3 and evict that role's prefix, so the request goes unpinned instead.
    bodies: list[dict[str, Any]] = []
    await send(_gateway(bodies, live_slots=4), FN)
    assert bodies and all("id_slot" not in b for b in bodies)


async def test_a_model_without_a_pool_never_reads_slots() -> None:
    reads: list[int] = []
    gw = _gateway([], slot_reads=reads)
    assert await gw.slot_for(STANDARD, PROBE_ROLE) is None
    assert reads == []


async def test_an_unreadable_layout_sends_unpinned() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": FN, "state": "ready"}]})
        return httpx.Response(500)

    gw = LocalGatewayClient("http://gw/v1", transport=httpx.MockTransport(handle))
    assert await gw.slot_for(FN, PROBE_ROLE) is None


@pytest.mark.parametrize(
    ("task", "asked", "pin"),
    [
        ("agent.turn", None, {"slot_role": SlotRole.WORKSHOP}),
        ("debug.complete", None, {}),
        ("entity.disambiguate", None, {}),
        ("agent.turn", SlotRole.INTERACTIVE, {"slot_role": SlotRole.INTERACTIVE}),
    ],
)
def test_a_console_call_stays_out_of_jervs_slot_unless_asked(
    task: str, asked: SlotRole | None, pin: dict[str, SlotRole]
) -> None:
    from jbrain.api.debug import _slot_pin

    assert _slot_pin(task, asked) == pin
