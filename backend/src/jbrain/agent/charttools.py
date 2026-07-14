"""The agent's generic charting tools (docs/plans/CHAT_CHARTS_PLAN.md W3).

Two producers of the data-only `chart` tool-view (DESIGN.md "chart & lab_chart
tool-views"), the numeric twin of the health-only `lab_chart`:

- `chart_measurements` — GROUNDED: reads a measurement predicate's numeric history
  from `app.facts` on the caller's RLS-scoped session, so every plotted point traces
  to a note and the domain firewall holds at the source (a scope that can't see a
  fact can't chart it). This is the citable path.
- `render_chart` — the model hands over a series it already read/derived and we plot
  it. No new data path, weaker provenance (the numbers are model-retyped), so it is
  a **general-domain** artifact only: for cited health/finance figures use the
  grounded tools (read_labs / chart_measurements), which carry their sources.

Both emit the same `chart` component; neither authors markup, a URL, or a color
(invariants #1/#9) — just numbers and a title.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.chartscale import nice_scale
from jbrain.agent.contracts import CitationRef, FactRef, ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import scoped_session


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(round(v, 4))


def _as_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _parse_x(v: Any) -> int | None:
    """A point's X → epoch milliseconds. Accepts an epoch-ms number or an ISO date
    (YYYY, YYYY-MM, YYYY-MM-DD, or a full datetime); a bare year/month is anchored to
    its first day. Returns None for anything unparseable (the point is dropped)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) == 4 and s.isdigit():
        s = f"{s}-01-01"
    elif len(s) == 7 and s[4] == "-":
        s = f"{s}-01"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _chart_payload(
    *,
    title: str,
    unit: str,
    domain: str,
    kind: str,
    points: list[dict[str, Any]],
    refs: list[CitationRef],
) -> ViewPayload:
    y_min, y_max, ticks = nice_scale([float(p["y"]) for p in points])
    return ViewPayload(
        view="chart",
        surface="inline",
        data={
            "domain": domain,
            "unit": unit,
            "title": title,
            "kind": kind if kind in ("line", "area") else "line",
            "x_kind": "time",
            "y": {"min": y_min, "max": y_max, "ticks": ticks},
            "series": [{"label": title, "points": points}],
        },
        refs=refs,
    )


def series_chart_view(title: str, unit: str, kind: str, raw_points: Any) -> ViewPayload | None:
    """Build a `chart` view from a model-supplied series (`render_chart`). Points are
    `{x, y}` with x an ISO date or epoch-ms; unparseable / non-numeric points drop.
    Returns None if fewer than two points survive (nothing to trend). General-domain."""
    if not isinstance(raw_points, list):
        return None
    pts: list[dict[str, Any]] = []
    for p in raw_points:
        if not isinstance(p, dict):
            continue
        x = _parse_x(p.get("x"))
        y = _as_number(p.get("y"))
        if x is None or y is None:
            continue
        pts.append({"x": x, "y": y})
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p["x"])
    return _chart_payload(
        title=title or "Chart", unit=unit, domain="general", kind=kind, points=pts, refs=[]
    )


def measurement_chart_view(rows: list[Any], title: str) -> ViewPayload | None:
    """Build a `chart` view from `app.facts` measurement rows (`chart_measurements`).
    Plots each numeric reading at its `valid_from`, one citation per point; the tint
    domain follows the facts (health → rose, else steel). Returns None for < 2 points."""
    pts: list[dict[str, Any]] = []
    refs: list[CitationRef] = []
    unit = ""
    domains: set[str] = set()
    for r in rows:
        vj = r["value_json"] or {}
        y = _as_number(vj.get("value")) if isinstance(vj, dict) else None
        when = r["valid_from"]
        if y is None or when is None:
            continue
        if isinstance(vj, dict) and vj.get("unit"):
            unit = str(vj["unit"])
        domains.add(str(r["domain_code"]))
        note_id = r["note_id"]
        point: dict[str, Any] = {"x": int(when.timestamp() * 1000), "y": y}
        if note_id:
            point["note"] = f"note:{note_id}"
        pts.append(point)
        refs.append(FactRef(fact_id=str(r["id"]), label=f"{title} {_fmt(y)}"))
    if len(pts) < 2:
        return None
    order = sorted(range(len(pts)), key=lambda i: pts[i]["x"])
    pts = [pts[i] for i in order]
    refs = [refs[i] for i in order]
    domain = "health" if domains == {"health"} else "general"
    return _chart_payload(title=title, unit=unit, domain=domain, kind="line", points=pts, refs=refs)


_MEASUREMENT_COLS = (
    "f.id, f.value_json, f.valid_from, f.note_id, f.domain_code, e.canonical_name AS entity_name"
)


def build_chart_handlers(maker: async_sessionmaker[AsyncSession]) -> dict[str, ToolHandler]:
    async def chart_measurements_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        measurement = str(arguments.get("measurement", "") or "").strip()
        if not measurement:
            return ToolOutput("Name the measurement to chart (e.g. weight, resting heart rate).")
        conds = [
            "f.kind = 'measurement'",
            "f.assertion = 'asserted'",
            "f.status = 'active'",
            "f.superseded_by IS NULL",
            "f.valid_from IS NOT NULL",
            "(lower(f.predicate) LIKE :m OR lower(f.statement) LIKE :m"
            " OR lower(e.canonical_name) LIKE :m)",
        ]
        params: dict[str, Any] = {
            "limit": int(arguments.get("limit", 200) or 200),
            "m": f"%{measurement.lower()}%",
        }
        subject = str(arguments.get("subject", "") or "").strip()
        if subject:
            conds.append("lower(e.canonical_name) LIKE :subj")
            params["subj"] = f"%{subject.lower()}%"
        if arguments.get("since"):
            conds.append("f.valid_from >= :since")
            params["since"] = str(arguments["since"])
        if arguments.get("until"):
            conds.append("f.valid_from <= :until")
            params["until"] = str(arguments["until"])
        sql = (
            f"SELECT {_MEASUREMENT_COLS} FROM app.facts f"
            " JOIN app.entities e ON e.id = f.entity_id"
            f" WHERE {' AND '.join(conds)} ORDER BY f.valid_from ASC LIMIT :limit"
        )
        async with scoped_session(maker, ctx.session) as s:
            rows = list((await s.execute(text(sql), params)).mappings().all())
        view = measurement_chart_view(rows, title=measurement)
        if view is None:
            return ToolOutput(
                f"I don't have at least two numeric '{measurement}' readings on record to"
                " chart (nothing matched, or the values aren't a single number)."
            )
        return ToolOutput(_measurement_summary(view, measurement), view=view)

    async def render_chart_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:  # noqa: ARG001
        title = str(arguments.get("title", "") or "").strip() or "Chart"
        unit = str(arguments.get("unit", "") or "")
        kind = str(arguments.get("kind", "line") or "line")
        view = series_chart_view(title, unit, kind, arguments.get("points"))
        if view is None:
            return ToolOutput(
                "I need at least two points, each with a date (x) and a number (y), to plot."
            )
        n = len(view.data["series"][0]["points"])
        return ToolOutput(f"Charted {n} points — {title}.", view=view)

    return {"chart_measurements": chart_measurements_tool, "render_chart": render_chart_tool}


def _measurement_summary(view: ViewPayload, measurement: str) -> str:
    pts = view.data["series"][0]["points"]
    unit = view.data["unit"]
    first, last = pts[0], pts[-1]

    def _d(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")

    u = f" {unit}" if unit else ""
    return (
        f"{len(pts)} {measurement} readings from {_d(first['x'])} to {_d(last['x'])}: "
        f"{_fmt(first['y'])}{u} → {_fmt(last['y'])}{u}. Each point cites its note."
    )
