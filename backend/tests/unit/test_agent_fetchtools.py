"""`fetch_image` tool wiring (no DB): a jerv-only `web`-class read-shaped tool the
curator is never offered, dropped from the registry without a handler.

The handler behaviour (fetch → validate → persist with provenance, the non-image
rejection, the show flag) is covered against real Postgres in
tests/integration/test_fetch_image_pg.py; the redirect-safe byte path in test_web.py."""

from pathlib import Path
from typing import Any

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.readtools import OPTIONAL_FETCH_IMAGE_TOOL, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


def test_fetch_image_sidecar_is_a_web_read_requiring_a_url() -> None:
    tf = load_tool(TOOLS_DIR / "fetch_image.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.side_effecting is False
    assert tf.spec.params["required"] == ["url"]
    assert "fetch_image" in OPTIONAL_FETCH_IMAGE_TOOL


def test_fetch_image_dropped_without_handler_kept_with_one(tmp_path: Path) -> None:
    (tmp_path / "fetch_image.tool").write_text(
        (TOOLS_DIR / "fetch_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    dropped = load_registry(tmp_path, {"search": _noop}, optional=OPTIONAL_FETCH_IMAGE_TOOL)
    assert "fetch_image" not in dropped
    kept = load_registry(
        tmp_path, {"search": _noop, "fetch_image": _noop}, optional=OPTIONAL_FETCH_IMAGE_TOOL
    )
    assert "fetch_image" in kept


def test_fetch_image_is_jerv_only_not_curator() -> None:
    registry = ToolRegistry(
        [RegisteredTool(toolfile=load_tool(TOOLS_DIR / "fetch_image.tool"), handler=_noop)]
    )
    curator = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "fetch_image" not in curator
    assert "fetch_image" in registry.allowed_names(scopes=(), allow=JERV_TOOLS)
