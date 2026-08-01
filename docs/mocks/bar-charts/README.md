# Bar graphs — GUI-gate mockups

> **Status:** Living · **Last verified:** 2026-08-01

Three interactive mockups for a **bar-graph tool-view** — the card `jerv` shows when an answer
is a categorical breakdown or ranking (counts per month, notes by domain, top tags) rather than
a time series. Companion to the settled line/lab-plot set in `../chat-charts/`; the GUI gate is
`docs/reference/PROCESS.md`. Each is a self-contained phone-framed page using JBrain design tokens
(`docs/reference/DESIGN.md`), theme-toggleable, and each renders **both** a single-series chart
(steel, general domain) and a **multi-series** chart (notes by domain — one color per domain,
drawn from the domain accents). Data is data-only (numbers + closed enums/keys), never
model-authored markup, URLs, or color — invariants #1/#9.

| Variant | File | Thesis | Interaction |
|---|---|---|---|
| **A** | `a-inline-explorer.html` | **Answer-first card → fullscreen explorer** (mirrors the settled `location_map` / chat-chart-A) | Bubble shows a compact glanceable bar chart; **tap to explore** opens a full-screen sheet with sort (value · label · original), a **stacked↔grouped** toggle, value labels, a tap-a-bar readout, and a Table view. |
| **B** | `b-inline-ranked.html` | **Direct inline ranked bars** — the chart *is* the message | Interactive **horizontal** bars in the bubble; tap a bar for its exact value + share of total, a sort control (value · name · original), and a **show-all / top-N** expander. Best for a categorical ranking. |
| **C** | `c-tabbed-card.html` | **Tabbed multi-view card** (mirrors the settled `weather_card` / `hurricane_card` / chat-chart-C) | One inline card with a tab row — **Chart** (vertical bars, stacked↔grouped toggle, tap-a-bar readout) · **Table** (raw counts) · **Stats** (total · biggest · smallest · mean · top domain). |

**Trade-offs.** **A** keeps the transcript calm and puts the heavy controls (sort, group/stack,
labels, table) behind one tap — least clutter, one extra step, and a fullscreen stage is the best
place to compare a wide grouped chart. **B** is the most immediate — the ranking reads at a glance
and every control lives in the bubble — at the cost of a taller bubble; it leans into *horizontal*
bars, so it's the strongest for "which is biggest" rankings but the weakest for multi-series
grouped/stacked comparisons. **C** surfaces the raw counts and the summary stats without leaving the
bubble and carries the grouped↔stacked toggle inline, at the cost of a busier card and a tab to find
the chart.

**Shared contract (all three).** The payload the model fills is `{ view:"bar_chart", title, unit,
categories:[{label, note}], series:[{name, key, values:[…]}], stacked? }`. One or more named series;
each series' color comes from its closed `key` (0→steel, 1→rose, 2→violet, 3→teal, 4→green, …),
mapped to a design token by the component — no hex crosses the wire. Category `note` is the
citation pointer (pointers-not-copies). A single-series payload renders one bar per category; a
multi-series payload renders grouped or stacked. This is a **new registered component** (a
deliberate registry addition, like a tool), not a generic chart kitchen-sink — bars only.

The owner picks one; the choice + reasoning is then recorded in `docs/reference/DESIGN.md`
("Agent tool views"), the chosen file becomes the binding spec the React port mirrors 1:1, and the
`bar_chart` view is registered in `frontend/src/agent/views/registry.tsx`.
