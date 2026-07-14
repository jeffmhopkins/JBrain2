# Chat charts & lab plots — GUI-gate mockups

> **Status:** Living · **Last verified:** 2026-07-14

Three interactive mockups for the **chart / graph / lab-plot tool-view** in the Full Brain
chat (build plan: `docs/archive/CHAT_CHARTS_PLAN.md`; the GUI gate, `docs/reference/PROCESS.md`).
Each is a self-contained phone-framed page using JBrain design tokens (`docs/reference/DESIGN.md`),
and each renders **both** a generic chart (body weight, general/steel domain) and a lab plot
(platelet count, health/rose domain — reference band + abnormal-flag toned points, a critical
low). All three share one **proven interactive chart engine** (`makeChart`): SVG time-series
with pinch/wheel **zoom**, drag **pan**, tap-to-scrub readout, and a reset affordance — the
"zoom/move as makes sense" the owner asked for. Data is data-only (numbers + closed enums), never
model-authored markup/URLs/color (invariants #1/#9).

| Variant | File | Thesis | Interaction |
|---|---|---|---|
| **A** | `a-inline-explorer.html` | **Answer-first card → fullscreen explorer** (mirrors the settled `location_map` pattern) | Bubble shows a compact glanceable chart; **tap to explore** opens a full-screen sheet with zoom/pan, range presets (3M/1Y/All), scrub readout, and a Table toggle. |
| **B** | `b-inline-direct.html` | **Direct inline manipulation** | The full chart is interactive **inside the bubble** — pinch/wheel-zoom + drag-pan in place, tap a draw to read it, a reset pill when zoomed; labs keep raw rows in an inline accordion. No sheet, no tabs. |
| **C** | `c-tabbed-card.html` | **Tabbed multi-view card** (mirrors the settled `hurricane_card` / `weather_card`) | One inline card with a tab row — **Trend** (zoom/pan chart) · **Table** (rows) · **Range** (labs: each reading gauged vs. the reference band) / **Stats** (generic: min/max/avg/change). |

**Trade-offs.** A keeps the transcript calm and puts heavy interaction behind one tap (least
clutter, one extra step, and the fullscreen stage is the best place to pinch-zoom a long history).
B is the most immediate — the chart *is* the message — at the cost of a taller bubble and gestures
that share space with page scroll (`touch-action: pan-y` keeps vertical scroll working). C surfaces
the raw rows and the in-range judgement without leaving the bubble, at the cost of a busier card and
a tab to find the chart.

The owner picks one; the choice + reasoning is then recorded in `docs/reference/DESIGN.md`
("Agent tool views") and the chosen file becomes the binding spec the React port mirrors 1:1.
