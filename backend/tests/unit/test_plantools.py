"""jerv's planning tools' guards + web-gating (docs/archive/JERV_PLANNING_TOOL_PLAN.md).

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
from jbrain.models.plan import has_open_checklist_item, normalize_plan_body, tick_next_step

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


def test_has_open_checklist_item() -> None:
    """The continuation gate: a plan with an unchecked `- [ ]` step has work left; a
    fully-`[x]` plan (or prose with no checklist) does not."""
    assert has_open_checklist_item("- [ ] do this\n- [x] done")
    assert has_open_checklist_item("* [ ] star bullet works too")
    assert not has_open_checklist_item("- [x] all\n- [x] done")
    assert not has_open_checklist_item("just prose, no checklist")
    assert not has_open_checklist_item("")


async def test_jerv_cannot_self_approve() -> None:
    """The core anti-injection guard: jerv may never set `approved` — that's the owner's
    alone. Refused before any DB access, so no plan is ever self-approved."""
    out = await _HANDLERS["write_plan"]({"status": "approved"}, _ctx("sess-1"))
    assert "Only the owner can approve" in out


def test_tick_next_step() -> None:
    """Ticks exactly the FIRST unchecked box; leaves the rest, and is a no-op when none remain."""
    assert tick_next_step("- [ ] one\n- [ ] two") == "- [x] one\n- [ ] two"
    assert tick_next_step("- [x] one\n- [ ] two") == "- [x] one\n- [x] two"
    assert tick_next_step("- [x] one\n- [x] two") == "- [x] one\n- [x] two"  # none left → no-op
    assert tick_next_step("") == ""


def test_normalize_plan_body_flattens_to_checkboxes() -> None:
    """Every list item — bare bullet, indented sub-item, or existing checkbox at any indent —
    becomes a top-level `- [ ]`/`- [x]` step; headings/prose pass through; checked stays checked;
    idempotent (the Step-4-heading-with-nested-subitems render bug)."""
    messy = "# Guide\n- [ ] Step 1\n- **Step 4**:\n  - [x] top pick\n  - budget pick\nplain prose"
    normalized = (
        "# Guide\n- [ ] Step 1\n- [ ] **Step 4**:\n- [x] top pick\n- [ ] budget pick\nplain prose"
    )
    assert normalize_plan_body(messy) == normalized
    assert normalize_plan_body(normalized) == normalized  # idempotent
    assert normalize_plan_body("") == ""


def test_normalize_plan_body_keeps_a_notes_section_as_prose() -> None:
    """A trailing notes section (`**Notes**:`, `## Caveats`, …) is jerv's explanatory prose, NOT
    steps — its bullets must stay bullets so they render as notes, not as extra checkboxes that
    inflate the step count and feed non-actions ('Await your approval before starting') into the
    loop. The checklist above it still flattens to `- [ ]` steps. Regression guard for the
    candidate-report plan whose 3 notes became 3 phantom steps (0 of 8 instead of 0 of 5)."""
    body = (
        "- [ ] Run deep_research for Chris Gleason\n"
        "- [ ] Run deep_research for Ashley Moody\n"
        "\n"
        "**Notes**:\n"
        "- Each deep_research call produces a cited report saved to the library.\n"
        "- The compare step will pull those reports.\n"
        "- Await your approval before starting."
    )
    out = normalize_plan_body(body)
    # Only the two real steps are checklist items…
    assert out.count("- [ ]") == 2
    # …the notes stayed prose bullets (never turned into `- [ ]` steps).
    assert "- Each deep_research call produces" in out
    assert "- Await your approval before starting" in out
    assert "- [ ] Await your approval" not in out
    assert has_open_checklist_item(out)  # still has real work
    # The loop's step scan sees exactly the two steps, not the notes.
    assert normalize_plan_body(out) == out  # idempotent


def test_normalize_plan_body_recognizes_notes_headings_but_not_step_text() -> None:
    """The prose-section trigger is a bare label (`**Notes**`, `## Caveats`, `Notes:`), so it
    stops flattening — but a real step that merely starts with 'Note' is untouched."""
    for heading in ("**Notes**", "**Notes:**", "## Notes", "Notes:", "### Caveats", "**Tips**"):
        out = normalize_plan_body(f"- [ ] do it\n{heading}\n- a caveat")
        assert "- [ ] a caveat" not in out, heading  # the bullet under the heading stays prose
        assert "- a caveat" in out, heading
    # A step whose text starts with 'Note' is NOT a prose heading — it stays a real step.
    assert normalize_plan_body("- Note the FEC deadline") == "- [ ] Note the FEC deadline"


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
