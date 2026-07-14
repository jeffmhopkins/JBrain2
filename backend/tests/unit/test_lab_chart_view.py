"""Unit tests for the `lab_chart` tool-view builder (docs/plans/CHAT_CHARTS_PLAN.md W2).

The builder is pure over the projection rows, so it needs no database — the handler
wiring + RLS firewall are covered by the integration suite (real Postgres); here we pin
the view shape, the current/numeric/non-preliminary filter, the flag enum, and the scale.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jbrain.agent.contracts import FactRef
from jbrain.agent.labtools import lab_chart_view


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "f1",
        "analyte": "platelet count",
        "value_num": 200.0,
        "value_text": None,
        "unit": "x10^9/L",
        "ref_low": 150.0,
        "ref_high": 400.0,
        "ref_text": None,
        "interpretation": None,
        "collected_at": datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        "performing_lab": None,
        "encounter_id": None,
        "report_status": "final",
        "is_current": True,
        "superseded_by_id": None,
        "source_note_id": "n1",
    }
    base.update(over)
    return base


def _draws() -> list[dict[str, Any]]:
    return [
        _row(id="a", value_num=210.0, collected_at=datetime(2025, 1, 1, tzinfo=UTC)),
        _row(
            id="b",
            value_num=96.0,
            interpretation="critical",
            collected_at=datetime(2025, 4, 1, tzinfo=UTC),
            source_note_id="n2",
        ),
        _row(id="c", value_num=180.0, collected_at=datetime(2025, 7, 1, tzinfo=UTC)),
    ]


def test_builds_a_lab_chart_with_band_flags_and_sorted_points() -> None:
    view = lab_chart_view(_draws())
    assert view is not None
    assert view.view == "lab_chart"
    assert view.surface == "inline"
    data = view.data
    assert data["domain"] == "health"
    assert data["unit"] == "x10^9/L"
    assert data["ref"]["lo"] == 150.0 and data["ref"]["hi"] == 400.0
    pts = data["series"][0]["points"]
    assert [p["y"] for p in pts] == [210.0, 96.0, 180.0]
    # ascending by time, the critical low flagged and carrying its note
    assert pts[0]["x"] < pts[1]["x"] < pts[2]["x"]
    assert pts[1]["flag"] == "critical"
    assert pts[1]["note"] == "note:n2"
    # a citation ref per plotted draw
    assert [r.fact_id for r in view.refs if isinstance(r, FactRef)] == ["a", "b", "c"]


def test_returns_none_for_a_single_plottable_point() -> None:
    assert lab_chart_view([_row()]) is None


def test_excludes_superseded_preliminary_and_nonnumeric_draws() -> None:
    rows = [
        _row(id="a", value_num=210.0, collected_at=datetime(2025, 1, 1, tzinfo=UTC)),
        _row(id="s", value_num=999.0, is_current=False),  # superseded — dropped
        _row(id="p", value_num=888.0, report_status="preliminary"),  # preliminary — dropped
        _row(id="t", value_num=None, value_text="see report"),  # non-numeric — dropped
        _row(id="b", value_num=180.0, collected_at=datetime(2025, 7, 1, tzinfo=UTC)),
    ]
    view = lab_chart_view(rows)
    assert view is not None
    ys = [p["y"] for p in view.data["series"][0]["points"]]
    assert ys == [210.0, 180.0]  # only the two current, numeric, final draws


def test_derives_a_flag_from_the_reference_band_when_interpretation_is_bare() -> None:
    rows = [
        _row(id="a", value_num=500.0, collected_at=datetime(2025, 1, 1, tzinfo=UTC)),
        _row(id="b", value_num=100.0, collected_at=datetime(2025, 2, 1, tzinfo=UTC)),
        _row(id="c", value_num=200.0, collected_at=datetime(2025, 3, 1, tzinfo=UTC)),
    ]
    pts = lab_chart_view(rows).data["series"][0]["points"]  # type: ignore[union-attr]
    assert pts[0]["flag"] == "high"  # 500 > ref_high 400
    assert pts[1]["flag"] == "low"  # 100 < ref_low 150
    assert pts[2]["flag"] == "normal"  # 200 in range


def test_scale_covers_the_data_with_ticks() -> None:
    view = lab_chart_view(_draws())
    assert view is not None
    y = view.data["y"]
    assert y["min"] <= 96.0 and y["max"] >= 210.0
    assert len(y["ticks"]) >= 2
    assert all(y["min"] < t < y["max"] for t in y["ticks"])


def test_omits_the_band_when_the_reference_range_is_absent() -> None:
    rows = [
        _row(
            id="a",
            value_num=210.0,
            ref_low=None,
            ref_high=None,
            collected_at=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        _row(
            id="b",
            value_num=180.0,
            ref_low=None,
            ref_high=None,
            collected_at=datetime(2025, 7, 1, tzinfo=UTC),
        ),
    ]
    view = lab_chart_view(rows)
    assert view is not None
    assert "ref" not in view.data
