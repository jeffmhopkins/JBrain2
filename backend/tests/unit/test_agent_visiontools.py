"""`compare_images` tool wiring + source collection (no DB): a jerv-only `web`-class
tool the curator is never offered, dropped without a handler; the source list is
collected from `image_ids` + `attachment_ids` (a list contract, not paired a/b fields).

The handler behaviour (resolve N sources, vision compare, stitch + persist, error paths)
is covered against real Postgres in tests/integration/test_compare_images_pg.py."""

from pathlib import Path
from typing import Any

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.readtools import OPTIONAL_COMPARE_TOOL, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry
from jbrain.agent.visiontools import _collect_source_ids


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


def test_compare_images_sidecar_is_a_web_read_requiring_a_prompt() -> None:
    tf = load_tool(TOOLS_DIR / "compare_images.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.side_effecting is False
    assert tf.spec.params["required"] == ["prompt"]
    props = tf.spec.params["properties"]
    # A LIST contract (arrays), never an a/b-sides object — the grammar-safe shape.
    assert props["image_ids"]["type"] == "array"
    assert props["attachment_ids"]["type"] == "array"
    assert "compare_images" in OPTIONAL_COMPARE_TOOL


def test_collect_source_ids_orders_images_then_attachments_and_drops_junk() -> None:
    ids = _collect_source_ids(
        {
            "image_ids": ["a", " b ", "", 5, None],
            "attachment_ids": ["c", "  ", "d"],
        }
    )
    # image_ids first (trimmed, junk dropped), then attachment_ids.
    assert ids == [("a", ""), ("b", ""), ("", "c"), ("", "d")]


def test_compare_images_dropped_without_handler_kept_with_one(tmp_path: Path) -> None:
    (tmp_path / "compare_images.tool").write_text(
        (TOOLS_DIR / "compare_images.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    dropped = load_registry(tmp_path, {"search": _noop}, optional=OPTIONAL_COMPARE_TOOL)
    assert "compare_images" not in dropped
    kept = load_registry(
        tmp_path, {"search": _noop, "compare_images": _noop}, optional=OPTIONAL_COMPARE_TOOL
    )
    assert "compare_images" in kept


def test_compare_images_is_jerv_only_not_curator() -> None:
    registry = ToolRegistry(
        [RegisteredTool(toolfile=load_tool(TOOLS_DIR / "compare_images.tool"), handler=_noop)]
    )
    curator = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "compare_images" not in curator
    assert "compare_images" in registry.allowed_names(scopes=(), allow=JERV_TOOLS)
