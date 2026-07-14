# Chat Charts & Lab Plots — Build Plan

> **Status:** Shipped · **Last verified:** 2026-07-14 · **Waves:** W0✅ (GUI gate — **C chosen**) W1✅ W2✅ W3✅

**A shipped build record** (per `docs/DOC_LIFECYCLE.md`): let the assistant answer a
"chart / graph / plot this over time" or "show me my lab trend" question with an
**interactive, zoomable/pannable chart tool-view** in the chat transcript, instead of a wall
of numbers. Synthesized against the shipped tool-view system
(`docs/reference/ASSISTANT.md` "Tool result views"; `frontend/src/agent/views/registry.tsx`;
the data-only `ViewPayload` contract in `frontend/src/agent/types.ts` and
`backend/.../agent/loop.py` `ToolOutput.view`), the EMR lab surface
(`docs/plans/EMR_IMPORT_PLAN.md`; `app.lab_results` projection; `read_labs`/`read_encounters`
tools in `agent/labtools.py`), and the settled zoom/pan precedent for maps (the Leaflet
`location_map` / `hurricane_card` Track tab). Migration numbers below are a snapshot — re-derive
the head from `backend/migrations/versions/` before building.

The user ask: *"chats need to render charts, graphs, lab results … make sure they can zoom/move
as makes sense … use the wave process so we get good mockups."*

---

## 1. Goal & scope

**Goal.** A first-party, data-only chart **view** the agent emits from a read tool, rendered
inline in the transcript, with **direct-manipulation zoom (pinch / wheel) and pan (drag)** on the
time axis and a **tap-to-scrub readout** — plus a health-firewalled **lab-plot** specialization
that draws the reference band and tones abnormal draws. Charts are the *presentation* of numbers
the agent already read under RLS; they never let model output author markup, URLs, or color
(invariants #1/#9).

**In scope**
- A reusable **interactive chart engine** (SVG, X-axis zoom/pan, tap-scrub, reset), theme-tokened,
  reduced-motion aware, keyboard-operable — the numeric analogue of `renderTrail`/`renderPlace`.
- A generic **`chart` view** (`{view:"chart", kind, unit, series:[{label,points:[{x,y,flag?}]}],
  y:{min,max,ticks}, x_kind}`) for arbitrary owner numeric data (weight, spend-by-month, …).
- A **`lab_chart` view** (the long-promised `lab_plot`) specializing `chart` with a reference
  band (`ref:{lo,hi,label}`), abnormal-flag enum (`normal|low|high|critical`) toned by the
  component, and `ref_fact_ids` / note citations (pointers-not-copies).
- **Backend wiring**: `read_labs` emits `lab_chart` on its single-analyte `trend:true` path
  (health-scoped by construction — a non-health session sees nothing); a grounded path for the
  generic `chart` (see §5, the open decision).
- Tests: view-registry render tests + a fixture per view; backend tool-view emission tests;
  the existing RLS isolation guarantee is inherited (no new table).

**Out of scope (named follow-ons)**
- Multi-series generic charts beyond ~4 lines (folds to "Other" / small multiples per dataviz).
- Bar/scatter/area *as distinct agent-selectable forms* beyond the line/area default (the engine
  supports them; the tool contract exposes line first).
- A standing "dashboard" surface outside chat, and cross-domain composed charts (firewall: a
  chart is single-domain, scoped to the read that produced it).
- Editing/annotating a chart; exporting an image.

**Safety frame (binding).** A `lab_chart` shows *what the record says* — values, reference range,
flags, dates — cited, never a diagnosis or recommendation (mirrors `read_labs`' own prose rule).

---

## 2. Why a new view, and what it reuses

The tool-view system is already the sanctioned way to render rich UI in chat: a schema-validated,
**data-only** payload names a **registered first-party component**; an unknown `view` renders
nothing (`ToolView` in `registry.tsx`). Adding a chart is "add a registered component + a `.tool`
that emits it", exactly like `weather_card`/`hurricane_card` landed. What's genuinely net-new is
the **interactive chart engine** — today the only numeric primitive is the *static* sparkline
`TimeSeriesPlot` (Ops + `server_metrics`); there is no zoom/pan chart. Maps already pan/zoom via
Leaflet, so the interaction paradigm is house-settled; this brings it to numeric series.

| Need | Reuses | Net-new |
|---|---|---|
| Rich chat UI, data-only, no model markup | `ViewPayload` + registry + `ToolView` | 2 components (`chart`, `lab_chart`) |
| Emit a view from a tool | `ToolOutput(view=…)`, `ToolViewEvent`, persisted `TranscriptTurn.view` | `lab_chart` on `read_labs` trend |
| Lab data, firewalled + cited | `app.lab_results`, `read_labs`, `CitationRef` | reference-band + flag slots |
| Zoom/pan interaction | the settled map gesture model (precedent only) | the SVG chart engine |
| Tests | registry render tests + testcontainers + adapter fake | fixtures + emission tests |

**Net new: zero tables, zero migrations, zero runtime deps** — the chart engine is hand-rolled
SVG (no chart library; validated against the zero-new-dep goal). Only `scripts/dev-setup.sh`
touch is none.

---

## 3. The interactive chart engine (`frontend/src/components/InteractiveChart`)

A presentational, model-blind component (like `TimeSeriesPlot`): callers pass parsed points +
token classes + formatters; it never reads model data directly. Contract:
- **Domain state** `(viewStart, viewEnd)` over the X axis; render maps the visible domain to the
  plot rect and **clips** the line/points to it. Y is fixed to the payload's `{min,max,ticks}`.
- **Zoom** anchored at the pointer: wheel (desktop) and two-pointer pinch (touch), clamped between
  a max-zoom (`FULL/25`) and the full span.
- **Pan** via one-pointer horizontal drag, clamped to the data bounds; `touch-action: pan-y` so
  the page still scrolls vertically (a chart never traps the scroll).
- **Scrub**: a tap/click selects the nearest point → a pinned crosshair + a readout callback
  (`onPin(point)`), keyboard `←/→` steps the selection (accessibility: the readout is the
  non-visual reading of every value; a table view is always reachable).
- **Reset** affordance appears only when zoomed. Honors `prefers-reduced-motion`.
- Reference band + flag-toned points are opt-in props (the `lab_chart` case).

The GUI-gate mock's `makeChart` (`docs/mocks/chat-charts/*.html`) is the reference implementation
of this engine and its math; the React port mirrors it 1:1.

---

## 4. The two views (data-only payloads)

- **`chart`** — `{view:"chart", surface:"inline", data:{ kind:"line"|"area", unit, x_kind:"time",
  series:[{label, points:[{x:epoch_ms, y:number, flag?}]}], y:{min,max,ticks:[…]}, title, sub },
  refs:[…]}`. One or more series; the component owns the palette from the categorical token order.
- **`lab_chart`** — `chart` + `data.ref:{lo,hi,label}` and per-point `flag ∈
  {normal,low,high,critical}` (a closed enum the component maps to a tone — never a model color);
  `refs` carry the draws' `fact_id`/`note_id` for the citation chips and the Table view.

Both are additive registry entries in `registry.tsx` (`chart`, `lab_chart`) built on
`InteractiveChart`, and both are exercised by a fixture in `registry.test.tsx`.

---

## 5. Backend wiring & the one open decision

- **`lab_chart` from `read_labs`** (Wave 2): on `trend:true` + a single `analyte`, the handler
  already fetches the ordered time-series; it additionally builds a `lab_chart` `ViewPayload`
  (values, unit, `ref_low/ref_high`, `interpretation → flag`, `collected_at → x`, note refs) and
  returns it via `ToolOutput(view=…)`. Superseded / preliminary draws are excluded from the plotted
  current series (marked in the Table view), matching the prose tool's rule. Health-scoped session
  is the firewall; a non-health scope never reaches the handler's data.
- **Generic `chart` — RESOLVED (owner chose BOTH):** two producers ship (W3):
  - **(a) `chart_measurements`** *(the grounded, citable path)* — reads a measurement predicate's
    numeric history from `app.facts` on the RLS-scoped session, so every point traces to a note and
    the firewall holds at the source. Tint domain follows the facts (health → rose, else steel).
  - **(b) `render_chart`** — the model hands over a series it assembled; **general-domain only**, so
    it never launders health/finance numbers into an un-cited chart (those keep their grounded
    tools). The prose binds the model to state where the numbers came from.

---

## 6. Waves

- **W0 — GUI gate ✅.** Three interactive mock HTMLs in `docs/mocks/chat-charts/`
  (A inline-card→fullscreen-explorer · B direct-inline-manipulation · C tabbed-multi-view-card),
  each rendering both a generic and a lab plot with **real** zoom/pan. **Owner chose C** (tabbed
  multi-view card — Trend/Table/Range·Stats); the reasoning is recorded in DESIGN.md
  "`chart` & `lab_chart` tool-views". C's shell is the binding frontend spec; the React port
  mirrors `c-tabbed-card.html` 1:1.
- **W1 — the engine + the `chart`/`lab_chart` views (mock-driven, frontend-only) ✅.**
  `frontend/src/components/InteractiveChart.tsx` (the ported zoom/pan/scrub engine, keyboard-
  operable, `role="application"`), the `chart` + `lab_chart` tabbed cards in `registry.tsx`
  (Trend/Table/Range·Stats), `.tv-cc-*`/`.tv-plot-*` styles, and 7 render/interaction tests
  (band + flags, wheel-zoom reveals reset, keyboard scrub, tab switch, empty state). The
  `lab_chart` **component** lands here (it shares the engine); its **backend emission + RLS**
  is W2. Full frontend suite green (1138 tests), lint + typecheck clean.
- **W2 — `lab_chart` + `read_labs` emission ✅.** `agent/labtools.py` gains a **pure**
  `lab_chart_view(rows)` builder (plots only current/numeric/non-preliminary draws, maps
  `interpretation → flag` with a reference-band fallback, a nice 1/2/2.5/5×10ⁿ y-scale, a
  `FactRef` per draw) wired into `read_labs`' `trend:true` single-analyte path via
  `ToolOutput(view=…)`. 6 pure unit tests pin the shape/filter/flag/scale; the existing
  `read_labs` PG integration test now also asserts the firewall zeroes the view (a non-health
  scope emits no `lab_chart`) and that any emitted view is a well-formed health plot. The
  `.tool` sidecar is unchanged (the view is handler behavior, not a schema/prose change), so no
  version bump / digest pin was needed. ruff + pyright clean.
- **W3 — the generic `chart` producers (§5 — both) ✅.** `agent/charttools.py` +
  `agent/chartscale.py` (shared `nice_scale`, refactored out of `labtools`): the grounded
  `chart_measurements` (reads `app.facts` under RLS, one citation per point) and `render_chart`
  (model-supplied general series) tools, each with a `.tool` sidecar (pinned + version-guarded) and
  wired into the registry (curator wildcard picks them up). The `chart` view now renders `kind:area`
  (a filled path to the baseline). 7 pure unit tests + a PG integration test asserting the
  `chart_measurements` RLS firewall zeroes the view for a non-health scope. Backend ruff + pyright
  clean; frontend suite green.

Each wave: per-task adversarial review, per-wave review (W2/W3 touch the health firewall → a
red-team read), one PR per wave, CI green before merge (`docs/reference/PROCESS.md`).

---

## 7. Tests & gates

Frontend: registry render test per view (known view renders; unknown renders nothing), a
zoom/pan interaction test (a wheel/pointer sequence changes the visible domain and the readout),
a reduced-motion assertion, mock fixtures covering empty / single-point / all-abnormal / long
series. Backend: `read_labs` emits a well-formed `lab_chart` only for a single-analyte trend and
only under a health scope; the RLS isolation guarantee is inherited from `app.lab_results` (a
non-health scope gets no rows and no view). Coverage to the 80% / security-100% gates; `.tool`
digest pin updated.
