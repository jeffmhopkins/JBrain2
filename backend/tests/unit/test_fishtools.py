"""Fish-id tool wiring (no DB): the sidecar is dropped when the fishial service is
unconfigured (graceful degrade), it's a jerv-only `web`-class tool (the curator never
gets it), and the pure handler helpers (top_k, thumb, summary, view, unload) behave.

The DB-backed handler behaviour (source resolution, view, unload, error paths) is
covered against real Postgres in tests/integration/test_fishtools_pg.py."""

from pathlib import Path
from typing import Any

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.contracts import ViewPayload
from jbrain.agent.fishtools import (
    _free_fish_model,
    _resolve_top_k,
    _summary,
    _thumb,
    fish_identification_view,
)
from jbrain.agent.readtools import OPTIONAL_FISH_TOOLS, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry
from jbrain.fish_id.catalog import CATALOG
from jbrain.fish_id.client import DEFAULT_TOP_K, FishMatch, FishResult
from tests.unit.fakes import FakeFishIdGateway


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


_RESULT = FishResult(
    (
        FishMatch("Zebrasoma flavescens", "Yellow tang", 0.92),
        FishMatch("Acanthurus coeruleus", "Blue tang", 0.05),
    )
)


def test_sidecar_is_a_jerv_only_web_tool() -> None:
    """The sidecar ships and declares the jerv-only `web` class (the gate keeping it off
    the curator), expensive cost, side-effecting, and no required arg (either source)."""
    tf = load_tool(TOOLS_DIR / "identify_fish.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.cost_class == "expensive"
    assert tf.spec.side_effecting is True
    assert tf.spec.params["required"] == []


def test_sidecar_dropped_when_unconfigured(tmp_path: Path) -> None:
    """`load_registry(optional=...)` drops the optional sidecar when no handler is passed
    (service unset) rather than failing — the registry never advertises an unbacked tool."""
    (tmp_path / "identify_fish.tool").write_text(
        (TOOLS_DIR / "identify_fish.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"search": _noop}, optional=OPTIONAL_FISH_TOOLS)
    assert "search" in registry
    assert "identify_fish" not in registry  # optional + no handler → dropped


def test_sidecar_with_handler_is_kept(tmp_path: Path) -> None:
    (tmp_path / "identify_fish.tool").write_text(
        (TOOLS_DIR / "identify_fish.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"identify_fish": _noop}, optional=OPTIONAL_FISH_TOOLS)
    assert "identify_fish" in registry


def test_tool_is_jerv_only_not_curator() -> None:
    """The `web` class is opt-in: the curator (allow=None) is not offered it, jerv is."""
    registry = ToolRegistry(
        [RegisteredTool(toolfile=load_tool(TOOLS_DIR / "identify_fish.tool"), handler=_noop)]
    )
    curator = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "identify_fish" not in curator
    jerv = registry.allowed_names(scopes=(), allow=JERV_TOOLS)
    assert "identify_fish" in jerv


def test_resolve_top_k_defaults_on_absent_or_bad() -> None:
    assert _resolve_top_k(3) == 3
    assert _resolve_top_k(None) == DEFAULT_TOP_K
    assert _resolve_top_k("five") == DEFAULT_TOP_K
    assert _resolve_top_k(True) == DEFAULT_TOP_K  # a bool is not a count


def test_thumb_prefers_attachment_then_image() -> None:
    assert _thumb({"source_attachment_id": "a1"}) == ("a1", "attachment")
    assert _thumb({"source_image_id": "g1"}) == ("g1", "image")


def test_summary_names_top_and_runners_up() -> None:
    line = _summary(_RESULT)
    assert "Zebrasoma flavescens (Yellow tang)" in line
    assert "92%" in line
    assert "Also considered" in line and "Acanthurus coeruleus 5%" in line


def test_view_is_data_only_hero_verdict() -> None:
    view = fish_identification_view(_RESULT, "att_1", "attachment", CATALOG[0])
    assert isinstance(view, ViewPayload)
    assert view.view == "fish_identification" and view.surface == "inline"
    data = view.data
    assert data["thumb_id"] == "att_1" and data["thumb_kind"] == "attachment"
    assert data["top"] == {
        "species": "Zebrasoma flavescens",
        "common": "Yellow tang",
        "score": 0.92,
    }
    assert [o["species"] for o in data["others"]] == ["Acanthurus coeruleus"]
    assert data["arch"] == CATALOG[0].arch and data["species_count"] == CATALOG[0].species_count
    assert "/api/" not in str(data) and "url" not in data  # data-only, no model-authored url


async def test_free_fish_model_unloads_and_swallows_failure() -> None:
    gw = FakeFishIdGateway()
    await _free_fish_model(gw)
    assert gw.frees == 1
    # A gateway hiccup is logged, never fatal — the result is already in hand.
    await _free_fish_model(FakeFishIdGateway(fail_free=True))  # does not raise
