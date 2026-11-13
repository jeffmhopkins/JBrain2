"""The archivist memory tools' guards + web-gating (docs/archive/EMAIL_ARCHIVIST_PLAN.md). The
DB round-trip is covered by the RLS integration test; here we cover the no-DB branches
(no principal, oversized content) and that the tools are archivist-only."""

from jbrain.agent.agents import ARCHIVIST_TOOLS, JERV_TOOLS, MEMORY_TOOLS
from jbrain.agent.archivisttools import build_archivist_memory_handlers
from jbrain.agent.loop import ToolContext
from jbrain.agent.readtools import TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.db.session import SessionContext

# These guards return before any DB access, so a None sessionmaker is never used.
_HANDLERS = build_archivist_memory_handlers(None)  # type: ignore[arg-type]
_OWNERLESS = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())


async def test_read_without_principal_is_refused() -> None:
    out = await _HANDLERS["archivist_memory_read"]({}, _OWNERLESS)
    assert "no owner principal" in out


async def test_write_without_principal_is_refused() -> None:
    out = await _HANDLERS["archivist_memory_write"]({"content": "x"}, _OWNERLESS)
    assert "no owner principal" in out


async def test_write_rejects_oversized_content_with_a_concrete_cut() -> None:
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    out = await _HANDLERS["archivist_memory_write"]({"content": "x" * 22_000}, ctx)
    assert "NOT saved" in out
    assert "Cut at least 6000 chars" in out
    assert ctx.memory_write_refusals == [22_000]


async def test_second_oversized_write_in_a_turn_closes_saving() -> None:
    """The Email overview task spent ~50 minutes a morning rewriting its memory a few hundred
    chars shorter per pass, every pass refused, until the run's budget cut it off with no
    brief delivered. The second refusal in a turn must tell it to stop and answer."""
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    await _HANDLERS["archivist_memory_write"]({"content": "x" * 22_000}, ctx)
    out = await _HANDLERS["archivist_memory_write"]({"content": "x" * 21_800}, ctx)
    assert "Stop trying to save in this turn" in out
    assert "give Jeff your answer now" in out
    assert ctx.memory_write_refusals == [22_000, 21_800]


async def test_refusals_are_counted_per_turn() -> None:
    first_turn = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    await _HANDLERS["archivist_memory_write"]({"content": "x" * 22_000}, first_turn)
    next_turn = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    out = await _HANDLERS["archivist_memory_write"]({"content": "x" * 22_000}, next_turn)
    assert "Cut at least" in out


async def _noop(arguments: dict, ctx: ToolContext) -> str:
    return ""


def test_memory_tools_are_web_class_and_archivist_only() -> None:
    reg = ToolRegistry(
        [RegisteredTool(load_tool(TOOLS_DIR / f"{name}.tool"), _noop) for name in MEMORY_TOOLS]
    )
    assert all(reg.get(name).spec.permission == "web" for name in MEMORY_TOOLS)
    # The archivist's allowlist admits them; the default knowledge agent (curator,
    # allow=None) is denied the whole opt-in web class — so it never sees the scratchpad.
    assert reg.allowed_names(scopes=(), allow=MEMORY_TOOLS) == frozenset(MEMORY_TOOLS)
    assert reg.allowed_names(scopes=(), allow=None) == frozenset()


def test_jerv_cannot_reach_the_archivist_scratchpad() -> None:
    """jerv must not access the archivist's memory. RLS alone wouldn't stop it (jerv
    runs as the owner), so the firewall is the tool allowlist: the memory tools are not
    in JERV_TOOLS, and the `web` gate admits a tool only when it is explicitly
    allowlisted — so jerv neither sees them (`schemas_for`) nor may call them
    (`allowed_names`, the dispatch-time gate that refuses an un-offered name)."""
    assert not (MEMORY_TOOLS & JERV_TOOLS)  # not granted to jerv at the persona level
    assert MEMORY_TOOLS <= ARCHIVIST_TOOLS  # they belong to the archivist alone

    reg = ToolRegistry(
        [RegisteredTool(load_tool(TOOLS_DIR / f"{name}.tool"), _noop) for name in MEMORY_TOOLS]
    )
    # Under jerv's own allowlist, at any scope, the memory tools are neither callable
    # nor visible — even though jerv is itself an owner session.
    assert reg.allowed_names(scopes=(), allow=JERV_TOOLS) == frozenset()
    assert reg.allowed_names(scopes=("general", "location"), allow=JERV_TOOLS) == frozenset()
    assert reg.schemas_for(scopes=(), allow=JERV_TOOLS) == []
