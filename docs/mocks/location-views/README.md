# Location inline views — mock gate (Wave L3)

Three interactive directions for the **two new inline tool-views** the L3 wave
introduces — `location_map` (#3) and `place_card` (#4) — per the PROCESS.md GUI
gate. These are **not full screens**: they render *inside an agent chat bubble*,
as new entries in `frontend/src/agent/views/registry.tsx` (the first
Leaflet-dependent tool-views; today the registry holds only pure-DOM views).
Pick one; the chosen mock becomes the binding spec for the L3 view work and its
rationale is recorded in `DESIGN.md` ("Agent tool views") in the same PR.

Each file is **self-contained and opens standalone** (double-click → browser).
Each shows **both views** in a realistic transcript: a `location_map` answering
"map of me last week" (a multi-day trail with a real GPS gap) and a `place_card`
for **Home** (visit stats + note-sourced entity chips). All three honor the
plan's load-bearing invariants:

- **Coordinates are render-only** — lat/lon live *only* inside the Leaflet
  layers, never as text in the bubble or card. Cards show names/addresses.
- **Gap-aware trail** — a >max-gap GPS hole **splits the polyline** into
  separate legs; none of the three draws a solid straight line across a gap.
- **Owner-gated derived stats** — every `place_card` stat block carries an
  owner-only note (stats are omitted for a narrowed/non-owner session).
- **Tokens-only**, dark-first with a working theme toggle; the location accent
  rides `--steel` (DESIGN.md's info/agent accent — see note below).

> **Tiles note (mock vs. production).** Production tiles come from the on-box
> `/api/tiles` proxy (`leafletMap.ts`) — the phone never talks to a tile host.
> These mocks load OSM tiles from a CDN **only** so pan/zoom is real in a
> standalone file. The chosen view inherits the proxy, unchanged.

| File | Direction | Map vs. text emphasis | How the gap is shown | Card layout | Freshness | Key tradeoff |
|---|---|---|---|---|---|---|
| `option-a.html` | **Map-forward** | Map is the hero (large) | Toggleable dashed "no-signal" bridge between two solid legs | Full-width map atop stat strip + chips | Float pill on the map ("last fix 41 min ago") | Most spatial, but tallest in the transcript |
| `option-b.html` | **Answer-first, map on demand** | Prose leads; map is a tap-to-expand thumbnail | A **segments list** — two named legs split by a "no signal" row (text) + two polylines when expanded | Dense one-row (mini-map beside title/stats) | Pill on the thumbnail | Most compact + most legible gap explanation, but least spatial-at-a-glance |
| `option-c.html` | **Dossier split + honest freshness** | Balanced — square map beside a key/value column | A **numbered marker (#1)** on the map tied to a dossier "Gaps" row | Map beside a key/value dossier grid | **Full-width banner** (amber stale / green fresh) up top | Numbers + map read together and staleness is loud, but densest and the smaller side-by-side map is less immersive |

## Trade-offs in prose

- **A (Map-forward)** gives the strongest spatial read and makes the gap a
  visible geometric choice (the dashed bridge is *off* by default, so the split
  is honest; turning it on shows "no signal ~7 h"). Cost: it's the tallest card
  and pushes derived numbers below the fold.
- **B (Answer-first)** keeps the transcript readable — the map is opt-in and the
  gap is explained in words *before* you ever open the map (the segments list
  reads "No signal — route unknown · ~7 h gap · not drawn across"). Cost: you
  must tap to really see the path; the dense card trades air for compactness.
- **C (Dossier split)** is best when the answer *is* numbers next to a place
  ("battery@Walmart", "how long at Home") and makes **freshness the loudest
  signal** — an old fix gets an amber "last known position, not necessarily
  current" banner, the safest reading of stale GPS. Cost: it's the densest and
  the side-by-side map is the smallest of the three.

## Token note (for whoever builds the chosen view)

The mocks accent the location views with **`--steel`** — DESIGN.md's
info/agent/Full-Brain accent, and already the color of `leafletMap.ts`'s
`loc-lf-trail`/`loc-lf-fence`/`loc-lf-live` classes in production. DESIGN.md
records the **location *domain* color as undecided** (a teal candidate, assigned
"when Phase 7 lands"), and the Phase-7 surface mock README leaned `--teal`.
Because these are inline *agent* views (steel = agent surface) and reuse the
existing steel Leaflet styling, the mocks stay on steel; the domain-vs-agent
accent question should be confirmed with the owner at implementation. Start
(`--green`) and gap (`--amber`) markers reuse the same semantics as
`loc-lf-start` and the warning/pending tone.

> **DESIGN.md interpretation to confirm.** The "Agent tool views" section
> currently says "**no external map tile ever** … location renders as text, or
> later a basemap-free … `mini_map`". The L3 plan supersedes this for the
> location domain: `location_map`/`place_card` are explicitly "the first
> Leaflet-dependent tool-views" over the **on-box `/api/tiles` proxy** (not an
> external host, so the exfiltration/I-9 concern that motivated the ban does not
> apply). The chosen mock's adoption should update that DESIGN.md paragraph in
> the same PR to record the proxy-tile carve-out.

## DoD (for the implementing wave)

Mock states beyond these two happy-path examples are part of the view's
definition of done: **empty** (no fixes in window), **single-pin** (`where_is`),
**long trail** (downsample applied), **stale fix**, **non-owner** (stats
omitted), and **offline**. The vitest must `vi.mock("./leafletMap")` and assert
the downsampled, gap-split GeoJSON handed to the mock (jsdom has no layout
engine) — not rendered tiles.
