"""The `.tool` sidecar loader and the tool registry: load-time validation,
scope-filtered visibility, and exact sidecar↔handler binding."""

from pathlib import Path
from typing import Any

import pytest

from jbrain.agent.toolfile import ToolFile, ToolFileError, load_tool
from jbrain.agent.toolregistry import (
    RegisteredTool,
    ToolRegistry,
    ToolRegistryError,
    load_registry,
)

SEARCH_TOOL = """\
---
name: search
version: 1
permission: read
params:
  type: object
  properties:
    query: {type: string}
  required: [query]
---
Search the knowledge base for notes, facts, and entities.
"""

LAB_TOOL = """\
---
name: read_lab
version: 2
permission: read
domains: [health]
params: {type: object}
---
Read a lab result by id.
"""

SEARCH_TOOL_WITH_EXAMPLES = """\
---
name: search
version: 1
permission: read
params:
  type: object
  properties:
    query: {type: string}
  required: [query]
examples:
  - {query: "quarterly revenue"}
  - {query: "who is Celine"}
---
Search the knowledge base for notes, facts, and entities.
"""


def write_tool(directory: Path, filename: str, content: str) -> Path:
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


async def noop(**_: Any) -> None:
    return None


# --- loader ---------------------------------------------------------------


def test_load_tool_parses_spec_and_description(tmp_path: Path) -> None:
    tf = load_tool(write_tool(tmp_path, "search.tool", SEARCH_TOOL))
    assert tf.spec.name == "search"
    assert tf.spec.version == 1
    assert tf.spec.permission == "read"
    assert tf.spec.params["required"] == ["query"]
    assert tf.description == "Search the knowledge base for notes, facts, and entities."


def test_load_tool_digest_is_stable_and_changes_with_content(tmp_path: Path) -> None:
    a = load_tool(write_tool(tmp_path, "a.tool", SEARCH_TOOL))
    again = load_tool(write_tool(tmp_path, "a2.tool", SEARCH_TOOL))
    assert a.digest == again.digest
    edited = load_tool(write_tool(tmp_path, "b.tool", SEARCH_TOOL.replace("notes", "things")))
    assert edited.digest != a.digest


def test_load_tool_rejects_missing_frontmatter(tmp_path: Path) -> None:
    with pytest.raises(ToolFileError, match="frontmatter"):
        load_tool(write_tool(tmp_path, "x.tool", "no frontmatter here"))


def test_load_tool_rejects_invalid_spec(tmp_path: Path) -> None:
    # Missing the required `permission` field.
    bad = "---\nname: x\nversion: 1\nparams: {}\n---\nA tool.\n"
    with pytest.raises(ToolFileError, match="invalid tool frontmatter"):
        load_tool(write_tool(tmp_path, "x.tool", bad))


def test_load_tool_rejects_empty_description(tmp_path: Path) -> None:
    empty = "---\nname: x\nversion: 1\npermission: read\nparams: {}\n---\n\n"
    with pytest.raises(ToolFileError, match="empty description"):
        load_tool(write_tool(tmp_path, "x.tool", empty))


# --- examples (the near-term tool_guide: call examples, TOOL_CATALOG_PLAN) -------


def test_load_tool_parses_examples_and_keeps_them_off_the_spec(tmp_path: Path) -> None:
    tf = load_tool(write_tool(tmp_path, "s.tool", SEARCH_TOOL_WITH_EXAMPLES))
    assert tf.examples == ({"query": "quarterly revenue"}, {"query": "who is Celine"})
    assert "examples" not in tf.spec.model_dump()  # examples are not a ToolSpec/params field


def test_examples_change_the_digest_but_their_absence_does_not(tmp_path: Path) -> None:
    plain = load_tool(write_tool(tmp_path, "a.tool", SEARCH_TOOL))
    with_ex = load_tool(write_tool(tmp_path, "b.tool", SEARCH_TOOL_WITH_EXAMPLES))
    # Adding examples changes the digest (a deliberate version bump); an example-less tool keeps
    # exactly the pre-examples digest (so the fleet of tools without examples never mass-bumps).
    assert with_ex.digest != plain.digest
    assert plain.digest == load_tool(write_tool(tmp_path, "a2.tool", SEARCH_TOOL)).digest


def test_load_tool_rejects_malformed_examples(tmp_path: Path) -> None:
    bad = SEARCH_TOOL.replace("---\nSearch", "examples:\n  - not-an-object\n---\nSearch")
    with pytest.raises(ToolFileError, match="examples"):
        load_tool(write_tool(tmp_path, "x.tool", bad))


def test_as_llm_tool_appends_examples_to_the_description(tmp_path: Path) -> None:
    rt = registered(SEARCH_TOOL_WITH_EXAMPLES, tmp_path, "s.tool")
    desc = rt.as_llm_tool().description
    assert "Example call" in desc
    assert '{"query": "quarterly revenue"}' in desc  # the exact shape to copy


def test_as_llm_tool_without_examples_is_just_the_description(tmp_path: Path) -> None:
    rt = registered(SEARCH_TOOL, tmp_path, "s.tool")
    desc = rt.as_llm_tool().description
    assert desc == "Search the knowledge base for notes, facts, and entities."


# --- registry -------------------------------------------------------------


def registered(content: str, tmp_path: Path, name: str) -> RegisteredTool:
    return RegisteredTool(toolfile=load_tool(write_tool(tmp_path, name, content)), handler=noop)


def test_schemas_for_filters_by_scope(tmp_path: Path) -> None:
    registry = ToolRegistry(
        [
            registered(SEARCH_TOOL, tmp_path, "search.tool"),  # no domains → all scopes
            registered(LAB_TOOL, tmp_path, "lab.tool"),  # health-only
        ]
    )
    general = {t.name for t in registry.schemas_for({"general"})}
    assert general == {"search"}  # health tool hidden
    health = {t.name for t in registry.schemas_for({"general", "health"})}
    assert health == {"search", "read_lab"}


def test_hidden_removes_a_tool_from_visibility_and_dispatch(tmp_path: Path) -> None:
    # A runtime outage (e.g. ComfyUI down) hides a tool for the turn: it is neither
    # offered (schemas_for) nor callable (allowed_names), ahead of every other rule.
    registry = ToolRegistry(
        [
            registered(SEARCH_TOOL, tmp_path, "search.tool"),
            registered(LAB_TOOL, tmp_path, "lab.tool"),
        ]
    )
    scopes = {"general", "health"}
    assert {t.name for t in registry.schemas_for(scopes)} == {"search", "read_lab"}
    offered = {t.name for t in registry.schemas_for(scopes, hidden={"read_lab"})}
    assert offered == {"search"}
    assert registry.allowed_names(scopes, hidden={"read_lab"}) == frozenset({"search"})


def test_schemas_for_is_stable_order(tmp_path: Path) -> None:
    registry = ToolRegistry(
        [
            registered(LAB_TOOL, tmp_path, "lab.tool"),
            registered(SEARCH_TOOL, tmp_path, "search.tool"),
        ]
    )
    names = [t.name for t in registry.schemas_for({"health"})]
    assert names == sorted(names)


def test_registry_get_and_unknown(tmp_path: Path) -> None:
    registry = ToolRegistry([registered(SEARCH_TOOL, tmp_path, "search.tool")])
    assert registry.get("search").permission == "read"
    assert "search" in registry and len(registry) == 1
    with pytest.raises(ToolRegistryError, match="unknown tool"):
        registry.get("nope")


def test_registry_rejects_duplicate_names(tmp_path: Path) -> None:
    with pytest.raises(ToolRegistryError, match="duplicate tool name"):
        ToolRegistry(
            [
                registered(SEARCH_TOOL, tmp_path, "a.tool"),
                registered(SEARCH_TOOL, tmp_path, "b.tool"),
            ]
        )


def test_as_llm_tool_carries_description_and_schema(tmp_path: Path) -> None:
    tool = registered(SEARCH_TOOL, tmp_path, "search.tool").as_llm_tool()
    assert tool.name == "search"
    assert "knowledge base" in tool.description
    assert tool.input_schema["required"] == ["query"]


# --- load_registry binding ------------------------------------------------


def test_load_registry_binds_sidecars_to_handlers(tmp_path: Path) -> None:
    write_tool(tmp_path, "search.tool", SEARCH_TOOL)
    write_tool(tmp_path, "read_lab.tool", LAB_TOOL)
    registry = load_registry(tmp_path, {"search": noop, "read_lab": noop})
    assert registry.names() == {"search", "read_lab"}


def test_load_registry_fails_on_sidecar_without_handler(tmp_path: Path) -> None:
    write_tool(tmp_path, "search.tool", SEARCH_TOOL)
    with pytest.raises(ToolRegistryError, match="sidecars without handlers"):
        load_registry(tmp_path, {})


def test_load_registry_fails_on_handler_without_sidecar(tmp_path: Path) -> None:
    write_tool(tmp_path, "search.tool", SEARCH_TOOL)
    with pytest.raises(ToolRegistryError, match="handlers without sidecars"):
        load_registry(tmp_path, {"search": noop, "ghost": noop})


def test_load_tool_returns_toolfile_type(tmp_path: Path) -> None:
    assert isinstance(load_tool(write_tool(tmp_path, "search.tool", SEARCH_TOOL)), ToolFile)


# --- extra_tools: the per-agent grant ahead of the web/NEVER_DEFAULT gates (DEEP_PRODUCE W2) --

_WEB_TOOL = """\
---
name: web
version: 1
permission: web
params: {type: object}
---
A web tool.
"""


def test_extra_tools_admits_ahead_of_web_and_never_default_gates(tmp_path: Path) -> None:
    """A wildcard (allow=None) agent excludes web-class and NEVER_DEFAULT tools; a per-agent
    `extra` grant admits a named one ahead of BOTH gates, still bounded by domain visibility,
    without widening the wildcard for the rest."""
    # A read-class tool whose NAME is in NEVER_DEFAULT (deep_produce), a web tool, and a plain
    # read tool — the last always visible to the wildcard.
    dp = registered(SEARCH_TOOL.replace("name: search", "name: deep_produce"), tmp_path, "dp.tool")
    web = registered(_WEB_TOOL, tmp_path, "web.tool")
    plain = registered(SEARCH_TOOL, tmp_path, "search.tool")
    registry = ToolRegistry([dp, web, plain])

    # Wildcard WITHOUT the grant: NEVER_DEFAULT deep_produce + web both excluded.
    base = registry.allowed_names({"general"}, None)
    assert base == {"search"}
    # WITH extra={deep_produce}: admitted ahead of the NEVER_DEFAULT gate; web still excluded;
    # the rest of the wildcard is unchanged (no widening).
    grant = frozenset({"deep_produce"})
    granted = registry.allowed_names({"general"}, None, grant)
    assert granted == {"search", "deep_produce"}
    assert {t.name for t in registry.schemas_for({"general"}, None, grant)} == granted
    # extra also beats the web gate when a web tool is the granted one.
    assert "web" in registry.allowed_names({"general"}, None, frozenset({"web"}))
    # An explicit-allow agent is unaffected by extra it doesn't need (allow gates first).
    assert registry.allowed_names({"general"}, frozenset({"search"}), grant) == {"search"}


def test_extra_tools_still_respects_domain_visibility(tmp_path: Path) -> None:
    """A granted tool is still bounded by its declared domains — a health-domain grant is
    invisible to a session that doesn't hold `health`."""
    health_dp = registered(
        LAB_TOOL.replace("name: read_lab", "name: deep_produce"), tmp_path, "dp.tool"
    )
    registry = ToolRegistry([health_dp])
    assert registry.allowed_names({"general"}, None, frozenset({"deep_produce"})) == set()
    assert registry.allowed_names({"health"}, None, frozenset({"deep_produce"})) == {"deep_produce"}


# --- with_handlers (JMOLT_LEDGER_ENGINE_PLAN.md, S1) ----------------------


def test_with_handlers_swaps_the_handler_and_keeps_the_sidecar(tmp_path: Path) -> None:
    """The simulated run must offer the model exactly the tool definitions the live one
    does — only what happens behind them differs."""

    async def other(_a: dict, _c: Any) -> str:
        return "sim"

    registry = ToolRegistry(
        [
            registered(SEARCH_TOOL, tmp_path, "search.tool"),
            registered(LAB_TOOL, tmp_path, "lab.tool"),
        ]
    )
    swapped = registry.with_handlers({"search": other})
    assert swapped.get("search").handler is other
    assert swapped.get("search").toolfile is registry.get("search").toolfile
    assert swapped.get("read_lab").handler is registry.get("read_lab").handler
    assert swapped.names() == registry.names()


def test_with_handlers_leaves_the_original_registry_alone(tmp_path: Path) -> None:
    """A copy, not a mutation — the live night keeps running on the registry the simulator
    was derived from."""

    async def other(_a: dict, _c: Any) -> str:
        return "sim"

    registry = ToolRegistry([registered(SEARCH_TOOL, tmp_path, "search.tool")])
    original = registry.get("search").handler
    registry.with_handlers({"search": other})
    assert registry.get("search").handler is original


def test_with_handlers_refuses_a_name_it_does_not_hold(tmp_path: Path) -> None:
    """Silently ignoring an unknown name would hand back a registry still pointing at the
    real client — for the simulator, that is a write to the live platform."""
    registry = ToolRegistry([registered(SEARCH_TOOL, tmp_path, "search.tool")])
    with pytest.raises(ToolRegistryError, match="moltbook_post"):
        registry.with_handlers({"moltbook_post": noop})
