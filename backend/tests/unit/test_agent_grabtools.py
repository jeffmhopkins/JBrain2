"""`grab_frame` tool wiring (no DB): the sidecar is dropped when ffmpeg is unconfigured
(graceful degrade), and when present it is a jerv-only `web`-class read-shaped tool the
curator is never offered.

The handler behaviour (URL/attachment grab, persist with provenance, the show flag, the
inline question, error paths) is covered against real Postgres in
tests/integration/test_grab_frame_pg.py."""

from pathlib import Path
from typing import Any

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.readtools import OPTIONAL_GRAB_TOOL, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


def test_grab_frame_sidecar_is_a_web_read_two_optional_sources() -> None:
    """`web` (jerv-only) like the media tools, and read-shaped (not side-effecting: the
    stored still is a transient chat artifact, like analyze_stream's write-through). Both
    sources are OPTIONAL — the handler enforces exactly-one, since a JSON-Schema `required`
    can't express "one of two", and an enum/over-constrained shape risks the gpt-oss grammar."""
    tf = load_tool(TOOLS_DIR / "grab_frame.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.side_effecting is False
    assert tf.spec.params["required"] == []
    props = tf.spec.params["properties"]
    assert {"url", "source_attachment_id", "seek", "question", "show"} <= set(props)
    assert "grab_frame" in OPTIONAL_GRAB_TOOL


def test_grab_frame_dropped_without_handler_kept_with_one(tmp_path: Path) -> None:
    """Optional + no handler (no ffmpeg) → dropped, so the registry never advertises an
    unbacked tool; with a handler it binds normally."""
    (tmp_path / "grab_frame.tool").write_text(
        (TOOLS_DIR / "grab_frame.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    dropped = load_registry(tmp_path, {"search": _noop}, optional=OPTIONAL_GRAB_TOOL)
    assert "search" in dropped and "grab_frame" not in dropped
    kept = load_registry(
        tmp_path, {"search": _noop, "grab_frame": _noop}, optional=OPTIONAL_GRAB_TOOL
    )
    assert "grab_frame" in kept


def test_grab_frame_is_jerv_only_not_curator() -> None:
    """The `web` class is opt-in: the curator (allow=None) is never offered grab_frame; jerv
    (allowlisting it) is."""
    registry = ToolRegistry(
        [RegisteredTool(toolfile=load_tool(TOOLS_DIR / "grab_frame.tool"), handler=_noop)]
    )
    curator = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "grab_frame" not in curator
    jerv = registry.allowed_names(scopes=(), allow=JERV_TOOLS)
    assert "grab_frame" in jerv
