"""Unit tests for the generic charting tools (docs/plans/CHAT_CHARTS_PLAN.md W3).

The two view builders are pure over their inputs (a model-supplied series /
`app.facts` rows), so they need no database — the `chart_measurements` SQL + RLS
firewall is covered by the integration suite. Here we pin the view shape, the
date parsing, the numeric/skip filter, the citation refs, and the tint domain.
The sidecars are checked to parse and to match their handler names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jbrain.agent.charttools import (
    _parse_x,
    build_chart_handlers,
    measurement_chart_view,
    multi_series_chart_view,
    series_chart_view,
)
from jbrain.agent.contracts import FactRef
from jbrain.agent.toolfile import load_tool

TOOLS = Path(__file__).resolve().parents[2] / "src" / "jbrain" / "agent" / "tools"


# --- _parse_x ---------------------------------------------------------------


def test_parse_x_accepts_iso_year_month_day_and_epoch() -> None:
    assert _parse_x("2025-06-15") == int(datetime(2025, 6, 15, tzinfo=UTC).timestamp() * 1000)
    assert _parse_x("2025-06") == int(datetime(2025, 6, 1, tzinfo=UTC).timestamp() * 1000)
    assert _parse_x("2025") == int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    assert _parse_x(1_700_000_000_000) == 1_700_000_000_000
    assert _parse_x("not-a-date") is None
    assert _parse_x(None) is None


# --- render_chart (series_chart_view) ---------------------------------------


def test_series_chart_view_builds_a_general_chart_from_dated_points() -> None:
    view = series_chart_view(
        "Weekly miles",
        "mi",
        "area",
        [{"x": "2025-01", "y": 12}, {"x": "2025-03", "y": 18}, {"x": "2025-02", "y": 9}],
    )
    assert view is not None
    assert view.view == "chart"
    assert view.data["domain"] == "general"
    assert view.data["kind"] == "area"
    pts = view.data["series"][0]["points"]
    assert [p["y"] for p in pts] == [12, 9, 18]  # sorted by date (Jan, Feb, Mar)
    assert view.refs == []  # model-supplied numbers carry no citations


def test_series_chart_view_drops_bad_points_and_needs_two() -> None:
    assert series_chart_view("x", "", "line", [{"x": "2025-01", "y": 1}]) is None
    assert series_chart_view("x", "", "line", [{"x": "bad", "y": 1}, {"x": "2025", "y": 2}]) is None
    assert series_chart_view("x", "", "line", "not-a-list") is None


def test_series_chart_view_tags_the_single_series_with_key_zero() -> None:
    view = series_chart_view(
        "W", "lb", "line", [{"x": "2025-01", "y": 1}, {"x": "2025-02", "y": 2}]
    )
    assert view is not None
    assert view.data["series"][0]["key"] == 0


# --- render_chart (multi_series_chart_view) ---------------------------------


def test_multi_series_chart_view_overlays_named_lines_keyed_by_index() -> None:
    view = multi_series_chart_view(
        "SpaceX launches",
        "launches",
        "line",
        [
            {"name": "Falcon 9", "points": [{"x": "2025-08", "y": 12}, {"x": "2025-09", "y": 10}]},
            {"name": "Starship", "points": [{"x": "2025-08", "y": 1}, {"x": "2025-09", "y": 0}]},
        ],
    )
    assert view is not None
    assert view.view == "chart"
    assert view.data["domain"] == "general"
    assert [s["label"] for s in view.data["series"]] == ["Falcon 9", "Starship"]
    assert [s["key"] for s in view.data["series"]] == [0, 1]
    # the y-scale spans BOTH series (Starship's 0 pulls the floor down)
    assert view.data["y"]["min"] <= 0


def test_multi_series_chart_view_drops_empty_series_and_needs_two_points() -> None:
    # a lone series with one point is not a trend
    assert (
        multi_series_chart_view("x", "", "line", [{"name": "a", "points": [{"x": "2025", "y": 1}]}])
        is None
    )
    # an empty / unparseable series drops; what survives must still reach two points
    view = multi_series_chart_view(
        "x",
        "",
        "line",
        [
            {"name": "keep", "points": [{"x": "2025-01", "y": 1}, {"x": "2025-02", "y": 2}]},
            {"name": "drop", "points": []},
            "not-a-dict",
        ],
    )
    assert view is not None
    assert [s["label"] for s in view.data["series"]] == ["keep"]
    assert multi_series_chart_view("x", "", "line", "nope") is None


def test_multi_series_chart_view_caps_at_six_lines() -> None:
    view = multi_series_chart_view(
        "x",
        "",
        "line",
        [
            {"name": f"s{i}", "points": [{"x": "2025-01", "y": i}, {"x": "2025-02", "y": i + 1}]}
            for i in range(8)
        ],
    )
    assert view is not None
    assert len(view.data["series"]) == 6


# --- chart_measurements (measurement_chart_view) ----------------------------


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "f1",
        "value_json": {"value": 182.5, "unit": "lb"},
        "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
        "note_id": "n1",
        "domain_code": "general",
        "entity_name": "Me",
    }
    base.update(over)
    return base


def test_measurement_chart_view_plots_cited_points_sorted() -> None:
    rows = [
        _row(
            id="a",
            value_json={"value": 190, "unit": "lb"},
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        _row(
            id="b",
            value_json={"value": 182, "unit": "lb"},
            valid_from=datetime(2025, 6, 1, tzinfo=UTC),
            note_id="n2",
        ),
    ]
    view = measurement_chart_view(rows, title="weight")
    assert view is not None
    assert view.view == "chart"
    assert view.data["domain"] == "general"
    assert view.data["unit"] == "lb"
    pts = view.data["series"][0]["points"]
    assert [p["y"] for p in pts] == [190, 182]
    assert pts[1]["note"] == "note:n2"
    assert [r.fact_id for r in view.refs if isinstance(r, FactRef)] == ["a", "b"]


def test_measurement_chart_view_health_rows_tint_health() -> None:
    rows = [
        _row(id="a", domain_code="health", value_json={"value": 60, "unit": "bpm"}),
        _row(
            id="b",
            domain_code="health",
            value_json={"value": 66, "unit": "bpm"},
            valid_from=datetime(2025, 2, 1, tzinfo=UTC),
        ),
    ]
    view = measurement_chart_view(rows, title="resting heart rate")
    assert view is not None
    assert view.data["domain"] == "health"


def test_measurement_chart_view_skips_nonnumeric_and_needs_two() -> None:
    rows = [
        _row(id="a", value_json={"value": 182, "unit": "lb"}),
        _row(id="x", value_json={"value": {"systolic": 120}}),  # non-scalar -> skipped
        _row(id="y", value_json=None),  # no value -> skipped
    ]
    assert measurement_chart_view(rows, title="weight") is None  # only one numeric point left


# --- render_chart handler (single vs. multi) --------------------------------


def test_render_chart_handler_uses_series_when_present() -> None:
    import asyncio

    handler = build_chart_handlers(maker=None)["render_chart"]  # type: ignore[arg-type]
    out = asyncio.run(
        handler(
            {
                "title": "SpaceX launches",
                "unit": "launches",
                "series": [
                    {
                        "name": "Falcon 9",
                        "points": [{"x": "2025-08", "y": 12}, {"x": "2025-09", "y": 10}],
                    },
                    {
                        "name": "Starship",
                        "points": [{"x": "2025-08", "y": 1}, {"x": "2025-09", "y": 0}],
                    },
                ],
            },
            None,  # type: ignore[arg-type]
        )
    )
    assert out.view is not None
    assert len(out.view.data["series"]) == 2
    assert "2 series" in out and "Falcon 9" in out


def test_render_chart_handler_still_plots_a_single_points_list() -> None:
    import asyncio

    handler = build_chart_handlers(maker=None)["render_chart"]  # type: ignore[arg-type]
    out = asyncio.run(
        handler(
            {"title": "W", "points": [{"x": "2025-01", "y": 1}, {"x": "2025-02", "y": 2}]},
            None,  # type: ignore[arg-type]
        )
    )
    assert out.view is not None
    assert len(out.view.data["series"]) == 1


# --- sidecar wiring ---------------------------------------------------------


def test_sidecars_parse_and_match_handler_names() -> None:
    handlers = build_chart_handlers(maker=None)  # type: ignore[arg-type]
    assert set(handlers) == {"chart_measurements", "render_chart"}
    for name in handlers:
        tf = load_tool(TOOLS / f"{name}.tool")
        assert tf.spec.name == name
        assert tf.description.strip()
