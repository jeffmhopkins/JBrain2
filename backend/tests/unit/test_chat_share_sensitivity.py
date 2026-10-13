"""What a chat share would publish (security plan S4): the reasons come from the stored turns,
so a chat re-scoped mid-way, or one that read mail or memory, still warns."""

from __future__ import annotations

from pathlib import Path

from jbrain.agent import chat_shares
from jbrain.agent.chat_shares import SENSITIVE_TOOLS, sensitivity_reasons
from jbrain.agent.reflexion import SENSITIVE_SCOPES
from jbrain.agent.toolfile import load_tool

_TOOLS_DIR = Path(chat_shares.__file__).parent / "tools"


def _step(name: str, **extra: object) -> dict[str, object]:
    return {"id": "t1", "name": name, "ok": True, "sources": [], **extra}


def test_harmless_chat_has_no_reasons() -> None:
    web = _step(
        "web_search",
        args={"query": "spacex"},
        web_sources=[{"url": "https://cnbc.com", "title": "CNBC"}],
    )
    search = _step(
        "search", sources=[{"note_id": "n1", "domain": "general", "snippet": "groceries"}]
    )
    assert sensitivity_reasons(["general"], [[web], [search], []]) == []


def test_firewalled_source_in_a_turn_counts_after_a_rescope() -> None:
    # The session now reads only `general`, but an earlier turn quoted a health note.
    read = _step("search", sources=[{"note_id": "n1", "domain": "health", "snippet": "A1c 6.1"}])
    assert sensitivity_reasons(["general"], [[read]]) == ["health"]


def test_current_firewalled_scope_still_counts() -> None:
    # Later turns show on a live link, so a chat that CAN read finance warns before it has.
    assert sensitivity_reasons(["general", "finance"], []) == ["finance"]


def test_mail_and_memory_tools_count() -> None:
    turns = [[_step("gmail_read", summary="From: bank")], [_step("memory_read")]]
    assert sensitivity_reasons(["general"], turns) == ["mail", "memory"]


def test_sourceless_firewalled_readers_count() -> None:
    assert sensitivity_reasons([], [[_step("read_labs")], [_step("where_was_i")]]) == [
        "health",
        "location",
    ]


def test_nested_and_malformed_shapes() -> None:
    nested = _step("spawn_subagent", view={"items": [{"domain": "location"}]})
    assert sensitivity_reasons([], [[nested], None, "junk", {"x": 1}]) == ["location"]
    # A bare `name` with no step shape (a view row, an entity label) is not a tool call.
    assert sensitivity_reasons([], [[_step("search", view={"name": "gmail_read"})]]) == []


def test_every_firewalled_tool_is_listed() -> None:
    """A `.tool` that declares a firewalled domain must be in the map, so a new location tool
    can't publish its reads without the warning."""
    missing = []
    for path in sorted(_TOOLS_DIR.glob("*.tool")):
        spec = load_tool(path).spec
        if set(spec.domains) & SENSITIVE_SCOPES and spec.name not in SENSITIVE_TOOLS:
            missing.append(spec.name)
    assert missing == []
