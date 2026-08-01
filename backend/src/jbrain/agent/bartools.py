"""The agent's bar-graph tool (docs/mocks/bar-charts/, DESIGN.md "bar_chart tool-view").

`render_bars` — the model hands over a categorical breakdown or ranking it already
read/derived and we render it as the data-only `bar_chart` view (the tabbed
Chart/Table/Stats card, binding mock `docs/mocks/bar-charts/c-tabbed-card.html`). The
categorical twin of `render_chart`: like it, this plots exactly the numbers passed, so
it is a **general-domain** artifact with model-retyped provenance — state where the
numbers came from in your reply. It authors no markup, URL, or color (invariants
#1/#9): series are colored by index, a component-owned mapping, never a model hex.

There is no grounded counterpart here (the way `chart_measurements` grounds
`render_chart`) — a cited count-by-category producer over `app.facts` is a possible
follow-up, but the rendering tool is what the wiki/chat need first.
"""

from __future__ import annotations

from typing import Any

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput

# The registered component owns six color keys (the domain accents); more series
# than that would collide, so we cap. The category cap keeps a phone-width card
# legible (and a runaway payload bounded).
_MAX_SERIES = 6
_MAX_CATEGORIES = 40


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


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(round(v, 4))


def bars_view(
    *, title: str, unit: str, stacked: bool, categories: Any, series: Any
) -> ViewPayload | None:
    """Build a `bar_chart` view from model-supplied categories + named series.

    Category labels are coerced to strings (empties dropped); each series' values are
    aligned to the surviving categories — a missing or non-numeric value becomes 0 (a
    zero-height bar, never a dropped category), extras past the last category are
    dropped. Series are colored by index (component-owned). Returns None unless there is
    enough to be a graph: at least one category and (≥2 categories, or ≥2 series).
    """
    if not isinstance(categories, list) or not isinstance(series, list):
        return None
    cats = [s for s in (str(c).strip() for c in categories) if s][:_MAX_CATEGORIES]
    if not cats:
        return None
    out_series: list[dict[str, Any]] = []
    for s in series[:_MAX_SERIES]:
        if not isinstance(s, dict) or not isinstance(s.get("values"), list):
            continue
        vals = [(_as_number(v) or 0.0) for v in s["values"]][: len(cats)]
        vals += [0.0] * (len(cats) - len(vals))
        # A lone all-zero series is nothing to show; with siblings it is a real (empty)
        # category, so it stays.
        if len(series) == 1 and not any(vals):
            continue
        name = str(s.get("name", "") or "").strip() or f"Series {len(out_series) + 1}"
        out_series.append({"name": name, "key": len(out_series), "values": vals})
    if not out_series or (len(cats) < 2 and len(out_series) < 2):
        return None
    return ViewPayload(
        view="bar_chart",
        surface="inline",
        data={
            "title": title or "Chart",
            "unit": unit,
            "domain": "general",
            "stacked": bool(stacked),
            "categories": [{"label": c} for c in cats],
            "series": out_series,
        },
    )


def _summary(view: ViewPayload) -> str:
    data = view.data
    cats, series, unit = data["categories"], data["series"], data["unit"]
    u = f" {unit}" if unit else ""
    total = sum(sum(s["values"]) for s in series)
    if len(series) == 1:
        vals = series[0]["values"]
        top = max(range(len(vals)), key=lambda i: vals[i])
        return (
            f"{data['title']}: {len(cats)} bars, {_fmt(total)}{u} total — "
            f"biggest is {cats[top]['label']} at {_fmt(vals[top])}{u}."
        )
    names = ", ".join(s["name"] for s in series)
    return (
        f"{data['title']}: {len(cats)} categories across {len(series)} series "
        f"({names}), {_fmt(total)}{u} total."
    )


def build_bar_handlers() -> dict[str, ToolHandler]:
    async def render_bars_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:  # noqa: ARG001
        title = str(arguments.get("title", "") or "").strip() or "Chart"
        unit = str(arguments.get("unit", "") or "")
        stacked = bool(arguments.get("stacked", False))
        view = bars_view(
            title=title,
            unit=unit,
            stacked=stacked,
            categories=arguments.get("categories"),
            series=arguments.get("series"),
        )
        if view is None:
            return ToolOutput(
                "I need at least two labelled categories (or two series), each with a"
                " numeric value, to draw a bar graph."
            )
        return ToolOutput(_summary(view), view=view)

    return {"render_bars": render_bars_tool}
