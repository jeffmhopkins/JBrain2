"""jerv's planning tools' guards + web-gating (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

The DB round-trip and the approval state machine are covered by the RLS integration
test; here we cover the no-DB branches (no conversation, empty args, oversized body,
the "jerv can't self-approve" refusal) and that the tools are jerv-only, never the
curator wildcard.
"""

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.loop import ToolContext
from jbrain.agent.plantools import build_plan_handlers
from jbrain.agent.readtools import TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.db.session import SessionContext

# A None sessionmaker is safe for the branches that return before any DB access.
_HANDLERS = build_plan_handlers(None)  # type: ignore[arg-type]
_PLAN_TOOLS = ("read_plan", "write_plan")


def _ctx(session_id: str | None) -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=session_id
    )


async def test_read_plan_without_a_conversation_is_refused() -> None:
    out = await _HANDLERS["read_plan"]({}, _ctx(None))
    assert "no conversation" in out


async def test_write_plan_without_a_conversation_is_refused() -> None:
    out = await _HANDLERS["write_plan"]({"body": "x"}, _ctx(None))
    assert "no conversation" in out


async def test_write_plan_needs_body_or_status() -> None:
    out = await _HANDLERS["write_plan"]({}, _ctx("sess-1"))
    assert "needs a body" in out


async def test_write_plan_rejects_oversized_body() -> None:
    out = await _HANDLERS["write_plan"]({"body": "x" * 40_001}, _ctx("sess-1"))
    assert "too long" in out


async def test_jerv_cannot_self_approve() -> None:
    """The core anti-injection guard: jerv may never set `approved` — that's the owner's
    alone. Refused before any DB access, so no plan is ever self-approved."""
    out = await _HANDLERS["write_plan"]({"status": "approved"}, _ctx("sess-1"))
    assert "Only the owner can approve" in out


async def _noop(arguments: dict, ctx: ToolContext) -> str:
    return ""


def test_plan_tools_are_web_class_and_jerv_only() -> None:
    reg = ToolRegistry(
        [RegisteredTool(load_tool(TOOLS_DIR / f"{name}.tool"), _noop) for name in _PLAN_TOOLS]
    )
    assert all(reg.get(name).spec.permission == "web" for name in _PLAN_TOOLS)
    # jerv holds them by allowlist; the default knowledge agent (curator, allow=None) is
    # denied the whole opt-in web class — so it never sees a conversation's plan.
    assert set(_PLAN_TOOLS) <= JERV_TOOLS
    assert reg.allowed_names(scopes=(), allow=JERV_TOOLS) == frozenset(_PLAN_TOOLS)
    assert reg.allowed_names(scopes=("general", "health"), allow=None) == frozenset()
