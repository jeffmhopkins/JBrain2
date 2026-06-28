# Hurricane tool — forecast track, impact timeline & official alerts (build plan)

Upgrades the shipped `hurricane` tool (position + strength only) into the
**tabbed `hurricane_card`** previewed by `docs/mocks/hurricane-view/hurricane-combined-tabs.html`:
a persistent storm hero + a real watch/warning banner, with **Timeline**, **Track**,
and **Impact** tabs. Governed by `docs/PROCESS.md` (waves) on top of
`docs/DEVELOPMENT.md` and the `CLAUDE.md` non-negotiables.

## Hard requirements (owner-set)

1. **Free, no-API-key, no-setup sources only.** Every upstream is public-domain US
   government data reachable with no key and no account. Base URLs are pinned in
   config with public defaults (like `weather`/`hurricane` today) — a fresh box works
   with zero configuration.
2. **Zero new runtime dependency.** Everything is JSON / GeoJSON parsed with the
   stdlib + the existing `httpx`. **No GRIB2** (would need `cfgrib`/`eccodes`, a heavy
   binary dep — explicitly out of scope; the products that are GRIB2-only are dropped,
   see §6).
3. **GUI pre-approved.** The binding mock is
   `docs/mocks/hurricane-view/hurricane-combined-tabs.html` (chosen this session); the
   `docs/PROCESS.md` GUI gate is already satisfied — no new mock round.
4. **The location firewall holds** exactly as for `weather`/`hurricane` (§5).

## 1. Data sources (all verified free / no-key; pinned URLs with public defaults)

| Need | Source | Endpoint (pinned base + path) | Format |
|---|---|---|---|
| Active-storm vitals (shipped) | NHC | `https://www.nhc.noaa.gov/CurrentStorms.json` | JSON |
| Forecast **track points** (lat/lon, hour, max-wind→cat, gust, pressure) + **cone** polygon + watch/warning lines | NHC ArcGIS MapServer | `https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather/MapServer/{layer}/query?where=…&outFields=*&f=geojson` | **GeoJSON** |
| Official **watches/warnings** at a point | NWS API | `https://api.weather.gov/alerts/active?point={lat},{lon}` | JSON |
| Per-location **hourly wind / gust / precip** | NWS API | `https://api.weather.gov/points/{lat},{lon}` → `…/gridpoints/{wfo}/{x},{y}` | JSON |
| **Peak storm-surge** height band (best-effort) | NHC ArcGIS MapServer | `https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_PeakStormSurge/MapServer/2/query?geometry={lon},{lat}&geometryType=esriGeometryPoint&inSR=4326&outFields=*&f=geojson` | GeoJSON |

Notes that bind the implementation:
- **MapServer layer discovery.** Per-storm layer groups (`AT1`…`AT5`, `EP1`…`EP5`,
  `CP1`…`CP4`) are spaced by 10; within a group **Forecast Points = +6, Track = +7,
  Cone = +8, Watch/Warning = +9**. The storm→group assignment is dynamic, so we **do
  not hardcode** it: fetch `…/MapServer/layers?f=json` once, find the group whose
  Forecast-Points layer features carry our storm's `idp_source` wallet (first 3 chars,
  e.g. `al1`) / `stormname`, and read points (`+6`) and cone (`+8`) from that group.
- **Forecast-point fields:** `validtime`, `tau` (12/24/…/120), `maxwind` (kt), `gust`
  (kt), `mslp` (mb), `ssnum` (Saffir-Simpson #), `tcdvlp`, plus geometry lon/lat.
- **NWS User-Agent.** `api.weather.gov` requires a descriptive `User-Agent`
  (no key). It is a **hardcoded client constant**, not owner setup:
  `User-Agent: JBrain2-hurricane (+https://github.com/jeffmhopkins/JBrain2)`.
- **NWS gridpoint series are run-length-encoded ISO intervals** (`start/PTnH`): a
  `PT6H` entry covers 6 hours. The client **expands each entry into its covered hours**
  before bucketing. Units are SI (`km_h-1`, `mm`, `degC`) → convert (km/h × 0.621371 =
  mph; mm ÷ 25.4 = in).
- **Coverage is US-only for the NWS feeds.** `api.weather.gov` 404s for points outside
  US/territory NWS coverage. That is a **graceful-degrade signal**, not an error (§4).

## 2. Frozen `hurricane_card` payload shape (v2)

The model still authors **no markup, no URL, no color, and no raw coordinate** (#9).
The card is built by the tool from upstream data; enums map to glyph + token in the
component. Map geometry is **projected to a unit square `[0,1]` on the backend** (a
storm-relative bbox over track ∪ cone ∪ you), so **no latitude/longitude ever rides
the payload** — consistent with the firewall and the mock's unit-square layout slots.

```jsonc
{
  "place": "Tampa, Florida, United States",
  "as_of": "Sep 10, 3:00 PM UTC",
  "active_count": 2,
  "coverage": "us",                 // "us" = NWS timeline+alerts present; "global" = NHC-only
  "storm": { "name": "Elena", "kind": "hurricane", "cat": "3",
             "sustained_mph": 120, "gust_mph": 150, "pressure_mb": 948,
             "moving": "NNE 14 mph" },
  "distance_mi": 215, "bearing": "SSW", "proximity": "near",

  // Official NWS alert for THIS point — the only source that may show a warning banner.
  "alert": { "level": "warning",    // "warning" | "watch" | "none"
             "kind": "hurricane",   // "hurricane" | "tropical-storm" | "surge" | "other"
             "event": "Hurricane Warning", "headline": "…" } ,   // or null

  // Track tab — projected points + cone polygon + your pin, all x,y in [0,1].
  "track": [ { "x": 0.30, "y": 0.86, "label": "Now", "cat": "3", "past": false }, … ],
  "cone":  [ { "x": 0.31, "y": 0.80 }, … ],     // [] when unavailable
  "you":   { "x": 0.58, "y": 0.42 },

  // Timeline tab — next ~36h, 3-hourly buckets (NWS gridpoint; [] when global).
  "timeline": [ { "label": "Now", "wind_mph": 35, "gust_mph": 50, "rain_in": 0.2,
                  "peak": false }, … ],
  "arrival": { "ts_force": "Wed 9 PM", "hurricane_force": "Thu 2 AM" },  // or nulls

  // Impact tab — derived locally from the above + surge band.
  "impact": {
    "wind":  { "mph": 70, "gust": 100, "level": "high" },
    "surge": { "ft": "5–8", "level": "high" },        // ft is a band string; null when none
    "rain":  { "in": 8, "level": "moderate" },
    "timing":{ "onset": "Wed 9 PM", "peak": "Thu 4 AM", "clear": "Thu 1 PM" }
  }
}
```

`level` enums are `low|moderate|high|extreme` (gauge + tone). Every new slot is
**optional**: a `global` (non-US) storm returns `coverage:"global"`, `alert:null`,
`timeline:[]`, `impact` with only what NHC gives, and the card shows the hero + Track
tab only. The component renders whatever is present and hides empty tabs.

## 3. Architecture & layering (routes → services → repos; clients in `jbrain/web/`)

New, independent client modules (each pure, `httpx.AsyncBaseTransport` injectable for
MockTransport tests, no network in tests):

- **`jbrain/web/nhc_gis.py` — `NhcGisClient`.** `forecast_track(wallet) -> tuple[TrackPoint,…]`
  and `cone(wallet) -> tuple[LatLon,…]` over the NHC tropical MapServer (layer discovery
  + GeoJSON parse). Pure geometry/types; no projection (the tool projects).
- **`jbrain/web/nws.py` — `NwsClient`.** `alerts(lat,lon) -> tuple[Alert,…]` and
  `timeline(lat,lon) -> Timeline` (points→gridpoint, interval expansion, unit
  conversion, TS/hurricane-force arrival derivation at 39/74 mph). Raises a typed
  `NwsUnavailable`/returns empty on 404 (out-of-coverage) so the tool degrades.
- **`jbrain/web/nhc_surge.py` — `NhcSurgeClient`.** `peak_band(lat,lon) -> str|None`
  (point-intersect the Peak Storm Surge polygons; regex the `> N ft` band from
  `name`/`popupinfo`). Best-effort; `None` when no active surge product.

Orchestration in **`jbrain/agent/hurricanetools.py`** (extended): after picking the
nearest storm, fire the independent fetches **concurrently** (`asyncio.gather` with
per-source `try/except` → empty on failure: the card always renders the hero + vitals,
plus whatever succeeded — graceful degrade is the default, never a hard failure). Then:
- project track ∪ cone ∪ you into `[0,1]` (a small pure `_project` helper, unit-tested);
- shape `timeline`/`arrival` from the NWS series;
- pick the governing `alert` (warning > watch; hurricane > surge > tropical-storm);
- derive `impact` from timeline peak + rain total + surge band + alert.

`hurricane.tool` → **version 2**, prose updated: it now *does* surface official
watches/warnings and a local wind/rain timeline **where NWS covers the point (US &
territories)**, and still must not invent surge/impact beyond what the card carries.
Re-pin the sidecar digest. Config: add the three pinned base URLs (public defaults).
`main.py`: construct the three clients, pass into `build_hurricane_handlers`.

## 4. Graceful degradation (binding behavior)

- **Non-US storm / point:** NWS 404 → `coverage:"global"`, no alert/timeline/impact-wind;
  Track tab still works (NHC GIS is global). Hero + Track only.
- **Any single upstream down or empty:** that slot is empty; the rest renders. NHC GIS
  cone occasionally lags points — points without a cone still draw the track line.
- **No active storms:** unchanged from v1 (the "all quiet" string, no card).
- Each upstream call has a **short timeout** and is **best-effort**; a slow/失敗 source
  never blocks the hero.

## 5. Location-firewall & security analysis (red-team gate target)

- **NHC CurrentStorms.json & NHC GIS** are queried by **storm wallet only** (or take no
  query) — **no location egress at all**.
- **NWS & surge** are queried by the **geocoded city-centre coordinate** — the *same*
  coarseness the `weather` tool already sends to Open-Meteo (a city centre, never the
  owner's precise fix). The `here` path resolves to a nearest-city **name** on-box
  first, then geocodes that name → city centre → queries NWS. The precise fix never
  leaves the box. This is the **only new egress of a coordinate**, and it is identical
  in coarseness to the shipped `weather` behavior.
- **No coordinate rides the payload (#9):** map geometry is projected to `[0,1]`
  storm-relative slots on the backend; the payload carries no lat/lon (owner's or
  storm's). The `you` pin reveals only relative position within the storm bbox.
- No owner notes / RLS-scoped data are in jerv's context; this is the sandbox `web`
  class. No new table, so no RLS test needed — but the **firewall reasoning above is a
  mandatory per-wave red-team review item** (Wave 2).

## 6. Explicitly out of scope (keeps zero-dep / no-setup)

- **GRIB2 products** (probabilistic storm surge P-Surge, NHC wind-speed-probability &
  arrival-time grids). They are the "authoritative" arrival/surge numbers but are
  binary rasters needing a heavy dep — **dropped**. Arrival is *derived* from the NWS
  hourly wind crossing 39/74 mph (documented as approximate); surge is the Peak-Surge
  *band*, not a modeled depth.
- **Third-party APIs** (Xweather, Ambee, Google/Apple) — all keyed and all reselling
  this same NHC/NWS data; rejected by requirement (1).
- **Tile/basemap rendering** — the Track tab is a stylized storm-relative diagram (unit
  square + coastline-free), exactly like the mock; no map tiles (also #9-friendly).

## 7. Waves (per `docs/PROCESS.md`)

**Wave 1 — data clients (3 parallel tasks, isolated worktrees).** New files only, no
cross-deps → maximal parallelism. Each: builder agent → independent adversarial review
→ local `ruff`+`pyright`+unit tests before merge to `wave-1`.
- **1a `NhcGisClient`** (track points + cone GeoJSON; layer discovery) + tests.
- **1b `NwsClient`** (alerts + gridpoint timeline; interval expansion; arrival
  derivation; unit conversion; out-of-coverage → empty) + tests.
- **1c `NhcSurgeClient`** (peak-surge band; regex) + tests.

**Wave 2 — tool assembly + payload + wiring (1 task, depends on Wave 1).** Extend
`hurricanetools.py` (concurrent orchestration, `_project`, shaping, impact derivation,
graceful degrade); bump `hurricane.tool` v2 + re-pin; config + `main.py` wiring; update
the shipped-tool digest-pin test. **Per-wave red-team review** of the firewall analysis
(§5) is mandatory here. Tests: handler-level with MockTransport across all sources incl.
the non-US degrade path; `_project` unit tests; firewall assertions (no lat/lon in
payload, `here` sends only the city name).

**Wave 3 — frontend tabbed card (1 task, depends on Wave 2's frozen shape).** Rebuild
`HurricaneCard` into the tabbed component (persistent hero + real alert banner +
Timeline/Track/Impact tabs + My-impact/Storm-stats toggle) matching the binding mock;
tokens-only `.tv-hu-*`; inline SVG track/cone drawing from `[0,1]` slots; view render
tests (warning vs watch vs none; us vs global coverage; empty-tab hiding). GUI
pre-approved — no mock round.

Each wave: one PR (or, on this feature branch, one wave commit set + wave-status
report), both review gates clean, locally verified; CI green before proceeding.

## 8. Testing plan

- **Clients (unit, MockTransport):** GeoJSON parse incl. empty/off-season; layer
  discovery picks the right group by wallet; interval-series expansion (PT1H/PT3H/PT6H)
  → hourly buckets; km/h→mph & mm→in; arrival-time crossing at 39/74 mph; surge band
  regex (`> 9 ft`); 404/5xx → typed-empty (recoverable).
- **Tool (unit, MockTransport across sources):** full US assembly; non-US degrade
  (`coverage:"global"`, empty timeline/alert); one-source-down degrade; alert
  precedence; `_project` maps a known bbox to expected `[0,1]`; **firewall:** no
  `latitude`/`longitude` substring in payload, `here` geocodes only the city name.
- **Frontend (Vitest/RTL):** tab switch; warning banner (rose) vs watch (amber) vs none;
  global coverage hides Timeline/Impact; track/cone SVG drawn from slots; inline-SVG-only
  (#9, no `<img>`).
- Coverage ≥ 80% (gate); firewall/degrade paths covered. No network, no real clock
  (inject `as_of`/labels from upstream strings, never `datetime.now`).

## 9. Open decisions (escalate per PROCESS §Communication)

- **Timeline window/resolution:** default **next 36h at 3-hourly** (≈12 cells) to match
  the mock's strip density; revisit if NWS resolution degrades past 36h makes it ragged.
- **Surge band display:** show the NHC Peak-Surge band string verbatim (e.g. "greater
  than 9 ft") rather than a single number, since the product is banded — avoids implying
  false precision.
