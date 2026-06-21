"""Image-gen tool wiring (no DB): the sidecars are dropped when ComfyUI is unconfigured
(graceful degrade), and when present they are jerv-only `web`-class tools — the curator
(the default knowledge agent) is never offered them.

The handler behaviour (insert, view, source resolution, error paths) is covered against real
Postgres in tests/integration/test_imagegentools_pg.py."""

from pathlib import Path
from typing import Any

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.readtools import IMAGE_TOOL_NAMES, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


def test_image_sidecars_exist_and_are_web_class() -> None:
    """Both `.tool` sidecars ship and declare the jerv-only `web` permission class
    (the gate that keeps them off the curator) plus the expensive/side-effecting flags."""
    for name in IMAGE_TOOL_NAMES:
        tf = load_tool(TOOLS_DIR / f"{name}.tool")
        assert tf.spec.permission == "web"
        assert tf.spec.cost_class == "expensive"
        assert tf.spec.side_effecting is True
        assert tf.spec.params["required"] == ["prompt"]


def test_optional_sidecars_dropped_when_unconfigured(tmp_path: Path) -> None:
    """`load_registry(optional=...)` drops an optional sidecar that has no handler (ComfyUI
    unset) rather than failing — so the registry never advertises an unbacked tool."""
    (tmp_path / "generate_image.tool").write_text(
        (TOOLS_DIR / "generate_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"search": _noop}, optional=IMAGE_TOOL_NAMES)
    assert "search" in registry
    assert "generate_image" not in registry  # optional + no handler → dropped


def test_optional_sidecar_with_handler_is_kept(tmp_path: Path) -> None:
    """An optional sidecar WITH a handler (ComfyUI configured) still binds normally."""
    (tmp_path / "edit_image.tool").write_text(
        (TOOLS_DIR / "edit_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"edit_image": _noop}, optional=IMAGE_TOOL_NAMES)
    assert "edit_image" in registry


def _image_registry() -> ToolRegistry:
    """A registry of just the two image sidecars, bound to no-op handlers — enough to assert
    the permission gate (curator vs jerv) without composing every other tool."""
    return ToolRegistry(
        [
            RegisteredTool(toolfile=load_tool(TOOLS_DIR / f"{name}.tool"), handler=_noop)
            for name in IMAGE_TOOL_NAMES
        ]
    )


def test_image_tools_are_jerv_only_not_curator() -> None:
    """The `web` class is opt-in: the curator (allow=None) is offered neither tool, while
    jerv (allowlisting them via JERV_TOOLS) is offered both."""
    registry = _image_registry()

    curator_offered = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "generate_image" not in curator_offered
    assert "edit_image" not in curator_offered

    jerv_offered = registry.allowed_names(scopes=(), allow=JERV_TOOLS)
    assert {"generate_image", "edit_image"} <= jerv_offered


def test_progress_callback_data_uris_previews_and_passes_steps() -> None:
    """The driver's (step, total, preview_bytes) ticks reach the turn sink with the
    preview base-64'd into a data URI; a missing preview passes through as None."""
    from jbrain.agent.imagegentools import _progress_callback
    from jbrain.agent.loop import ToolContext
    from jbrain.db.session import SessionContext

    seen: list[tuple[int, int, str | None]] = []
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        emit_progress=lambda s, t, p: seen.append((s, t, p)),
    )
    cb = _progress_callback(ctx)
    assert cb is not None
    cb(5, 20, b"jpegbytes")
    cb(20, 20, None)
    assert seen[0] == (5, 20, "data:image/jpeg;base64,anBlZ2J5dGVz")
    assert seen[1] == (20, 20, None)


def test_progress_callback_is_none_without_a_sink() -> None:
    # The batch path (no emit_progress) yields no callback, so the driver skips its
    # WebSocket and just polls for the final image.
    from jbrain.agent.imagegentools import _progress_callback
    from jbrain.agent.loop import ToolContext
    from jbrain.db.session import SessionContext

    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    assert _progress_callback(ctx) is None
