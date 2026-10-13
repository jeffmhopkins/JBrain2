"""What a chat share would publish (security plan S4): the reasons come from the stored turns,
so a chat re-scoped mid-way, or one that read mail or memory, still warns."""

from __future__ import annotations

from pathlib import Path

from jbrain.agent import chat_shares
from jbrain.agent.chat_shares import DOMAIN_TAGGED_TOOLS, SENSITIVE_TOOLS, sensitivity_reasons
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


def test_memory_and_prefs_tools_count() -> None:
    for name in ("remember", "prefs_read", "prefs_write"):
        assert sensitivity_reasons([], [[_step(name)]]) == ["memory"], name
    assert sensitivity_reasons([], [[_step("archivist_memory_write")]]) == ["mail"]


def test_domain_tagged_readers_count_only_firewalled_rows() -> None:
    """Appointments and lists name each row's domain only in their result text: a health
    appointment read before a re-scope warns, a grocery list does not."""
    appts = _step(
        "read_appointments",
        summary="- Cardiology — Tue 12:45 [health] id=a1\n- Haircut — Wed [general] id=a2",
    )
    assert sensitivity_reasons(["general"], [[appts]]) == ["health"]
    one = _step("read_appointment", summary="Tax prep [finance]\nwhen: Fri")
    assert sensitivity_reasons(["general"], [[one]]) == ["finance"]
    groceries = _step("read_list", summary="Groceries [general]\n[ ] milk id=i1")
    assert sensitivity_reasons(["general"], [[groceries]]) == []
    lists = _step("read_lists", summary="- Meds [health] (1/2 open) id=l1")
    assert sensitivity_reasons(["general"], [[lists]]) == ["health"]
    # An interrupted read carries no rows.
    assert sensitivity_reasons([], [[_step("read_lists")]]) == []


def test_measurement_chart_counts_whatever_its_view_says() -> None:
    # A finance or mixed chart is tagged `general` with no sources.
    chart = _step("chart_measurements", view={"kind": "chart", "domain": "general"})
    assert sensitivity_reasons(["general"], [[chart]]) == ["measurements"]


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


# Readers that need no entry: their steps carry domain-tagged note sources or entity refs, or
# they read the agent's own state, a Minecraft player's, or the radio. A new reader must be
# classified here or in `chat_shares`, so one can't publish private rows unnoticed.
_UNTAGGED_READERS_OK = frozenset(
    {
        "read_note",
        "read_entity",
        "read_wiki",
        "read_artifact",
        "read_plan",
        "close_reading",
        "mc_log_read",
        "mc_memory_add",
        "mc_memory_read",
        "mc_memory_remove",
        "mc_memory_replace",
        "scratch_read",
        "sdr_read",
    }
)


def _is_reader(name: str) -> bool:
    return (
        name.startswith("read_")
        or name.endswith("_read")
        or any(word in name for word in ("memory", "prefs", "recall", "remember"))
    )


def test_every_reader_tool_is_classified() -> None:
    unclassified = []
    for path in sorted(_TOOLS_DIR.glob("*.tool")):
        name = load_tool(path).spec.name
        if not _is_reader(name) or name.startswith("gmail_"):
            continue
        if name in SENSITIVE_TOOLS or name in DOMAIN_TAGGED_TOOLS or name in _UNTAGGED_READERS_OK:
            continue
        unclassified.append(name)
    assert unclassified == []
