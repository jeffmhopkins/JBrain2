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
