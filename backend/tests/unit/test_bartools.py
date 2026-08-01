"""Unit tests for the bar-graph tool (DESIGN.md "bar_chart tool-view").

`bars_view` is pure over its inputs (model-supplied categories + series), so it
needs no database. We pin the view shape, the category coercion, the value
alignment (short/long/non-numeric), the series color-by-index, the "enough to be a
graph" gate, and the summary. The sidecar is checked to parse and match its handler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jbrain.agent.bartools import bars_view, build_bar_handlers
from jbrain.agent.toolfile import load_tool

TOOLS = Path(__file__).resolve().parents[2] / "src" / "jbrain" / "agent" / "tools"


# --- bars_view --------------------------------------------------------------


def test_bars_view_builds_a_general_single_series_chart() -> None:
    view = bars_view(
        title="Notes captured",
        unit="notes",
        stacked=False,
        categories=["Jan", "Feb", "Mar"],
        series=[{"name": "Notes", "values": [18, 24, 31]}],
    )
    assert view is not None
    assert view.view == "bar_chart"
    assert view.surface == "inline"
    assert view.data["domain"] == "general"
    assert view.data["unit"] == "notes"
    assert [c["label"] for c in view.data["categories"]] == ["Jan", "Feb", "Mar"]
    assert view.data["series"] == [{"name": "Notes", "key": 0, "values": [18.0, 24.0, 31.0]}]
    assert view.refs == []  # model-supplied numbers carry no citations


def test_bars_view_colors_series_by_index_and_keeps_stacked_flag() -> None:
    view = bars_view(
        title="By domain",
        unit="",
        stacked=True,
        categories=["Mar", "Apr"],
        series=[
            {"name": "General", "values": [19, 12]},
            {"name": "Health", "values": [6, 14]},
            {"name": "Finance", "values": [4, 5]},
        ],
    )
    assert view is not None
    assert view.data["stacked"] is True
    assert [s["key"] for s in view.data["series"]] == [0, 1, 2]
    assert [s["name"] for s in view.data["series"]] == ["General", "Health", "Finance"]


def test_bars_view_aligns_values_to_categories() -> None:
    # short series is zero-padded; a long one is truncated; a non-numeric cell -> 0.
    view = bars_view(
        title="x",
        unit="",
        stacked=False,
        categories=["a", "b", "c"],
        series=[{"name": "s", "values": [5, "nope", 7, 9]}],
    )
    assert view is not None
    assert view.data["series"][0]["values"] == [5.0, 0.0, 7.0]


def test_bars_view_drops_empty_labels_and_unnamed_series_get_a_default() -> None:
    view = bars_view(
        title="x",
        unit="",
        stacked=False,
        categories=["a", "  ", "b"],  # blank label dropped
        series=[{"values": [1, 2]}],  # no name -> "Series 1"; values realign to 2 cats
    )
    assert view is not None
    assert [c["label"] for c in view.data["categories"]] == ["a", "b"]
    assert view.data["series"][0]["name"] == "Series 1"


def test_bars_view_requires_enough_to_be_a_graph() -> None:
    # a single bar is not a graph...
    assert (
        bars_view(
            title="x",
            unit="",
            stacked=False,
            categories=["a"],
            series=[{"name": "s", "values": [3]}],
        )
        is None
    )
    # ...but two series over one category is a valid comparison.
    assert (
        bars_view(
            title="x",
            unit="",
            stacked=False,
            categories=["a"],
            series=[{"name": "p", "values": [3]}, {"name": "q", "values": [4]}],
        )
        is not None
    )


def test_bars_view_drops_lone_all_zero_series_and_rejects_bad_shapes() -> None:
    assert (
        bars_view(
            title="x",
            unit="",
            stacked=False,
            categories=["a", "b"],
            series=[{"name": "s", "values": [0, 0]}],
        )
        is None
    )
    assert bars_view(title="x", unit="", stacked=False, categories="nope", series=[]) is None
    assert bars_view(title="x", unit="", stacked=False, categories=["a", "b"], series=[]) is None


def test_bars_view_caps_series_at_six() -> None:
    view = bars_view(
        title="x",
        unit="",
        stacked=False,
        categories=["a", "b"],
        series=[{"name": f"s{i}", "values": [i, i]} for i in range(1, 9)],
    )
    assert view is not None
    assert len(view.data["series"]) == 6


# --- render_bars handler ----------------------------------------------------


def test_render_bars_handler_emits_the_view_with_a_summary() -> None:
    handler = build_bar_handlers()["render_bars"]
    out = asyncio.run(
        handler(
            {
                "title": "Top tags",
                "unit": "notes",
                "categories": ["recipes", "workout", "work"],
                "series": [{"name": "Notes", "values": [61, 54, 48]}],
            },
            None,  # type: ignore[arg-type]
        )
    )
    assert out.view is not None
    assert out.view.view == "bar_chart"
    # ToolOutput is a str subclass — the summary text IS the value.
    assert "recipes" in out and "163" in out  # biggest called out; total summed


def test_render_bars_handler_declines_when_there_is_nothing_to_draw() -> None:
    handler = build_bar_handlers()["render_bars"]
    out = asyncio.run(handler({"title": "x", "categories": ["a"], "series": []}, None))  # type: ignore[arg-type]
    assert out.view is None
    assert "bar graph" in out


# --- sidecar wiring ---------------------------------------------------------


def test_sidecar_parses_and_matches_handler_name() -> None:
    handlers = build_bar_handlers()
    assert set(handlers) == {"render_bars"}
    tf = load_tool(TOOLS / "render_bars.tool")
    assert tf.spec.name == "render_bars"
    assert tf.spec.permission == "read"
    assert tf.description.strip()
