# Hurricane tool — forecast track, impact timeline & official alerts (build plan)

Upgrades the shipped `hurricane` tool (position + strength only) into the
**tabbed `hurricane_card`** previewed by `docs/mocks/hurricane-view/hurricane-combined-tabs.html`:
a persistent storm hero + a real watch/warning banner, with **Timeline**, **Track**,
and **Impact** tabs. Governed by `docs/PROCESS.md` (waves) on top of
`docs/DEVELOPMENT.md` and the `CLAUDE.md` non-negotiables.

> **Revised after independent dual review** (feasibility + security). Data-source
> specifics in §1 were corrected against the live services; the firewall narrative
> (§5) was reframed to city-centre coarseness. Review deltas are marked `[rN]`.

## Hard requirements (owner-set)

1. **Free, no-API-key, no-setup sources only.** Every upstream is public-domain US
   government data reachable with no key and no account. Base URLs are pinned in
   config with public defaults (like `weather`/`hurricane` today) — a fresh box works
   with zero configuration.
2. **Zero new runtime dependency.** Everything is JSON / GeoJSON parsed with the
   stdlib (`json`, `zoneinfo`) + the existing `httpx`. **No GRIB2** (would need
   `cfgrib`/`eccodes`) and **no geometry/projection lib** (`shapely`/`pyproj`): the
   surge point-in-polygon is done **server-side** by the ArcGIS `query` (`esriGeometryPoint`),
   and the Track projection is plain affine arithmetic. `[r-N1]`
3. **GUI pre-approved.** The binding mock is
   `docs/mocks/hurricane-view/hurricane-combined-tabs.html` (chosen this session); the
   `docs/PROCESS.md` GUI gate is already satisfied — no new mock round.
4. **The location firewall holds** exactly as for `weather`/`hurricane` (§5).

## 1. Data sources (verified free / no-key against the live services)

| Need | Source | Endpoint (pinned base + path) | Format |
|---|---|---|---|
| Active-storm vitals (shipped) | NHC | `https://www.nhc.noaa.gov/CurrentStorms.json` | JSON |
| Forecast **track points** + **cone** polygon | NHC ArcGIS MapServer | `…/tropical/NHC_tropical_weather/MapServer/{layer}/query?where=…&outFields=*&f=geojson` | **GeoJSON** |
| Official **watches/warnings** at a point | NWS API | `https://api.weather.gov/alerts/active?point={lat},{lon}` | JSON |
| Per-location **hourly wind / gust / precip** + **place tz** | NWS API | `https://api.weather.gov/points/{lat},{lon}` → `…/gridpoints/{wfo}/{x},{y}` | JSON |
| **Peak storm-surge** band (best-effort, US/territory coastal) | NHC ArcGIS MapServer | `…/tropical/NHC_PeakStormSurge/MapServer/2/query?geometry={lon},{lat}&geometryType=esriGeometryPoint&inSR=4326&outFields=*&f=geojson` | GeoJSON |

NHC tropical MapServer base: `https://mapservices.weather.noaa.gov/tropical/rest/services/tropical`.

Binding implementation notes (corrected against live `?f=json`):

- **`[r-B1]` Layer matching is NAME-BASED, not arithmetic.** Per-storm layer groups
  exist (`AT1…AT5`, `EP1…EP5`, `CP1…CP5`) but are **not** spaced by 10 and the
  within-group offsets are not `+6..+9` — do **not** compute layer ids. Instead fetch
  `…/NHC_tropical_weather/MapServer/layers?f=json` once and select the layer whose
  `name` ends in **"Forecast Points"** (and, for the cone, **"Forecast Cone"**) whose
  features belong to our storm (next note). A name+geometryType assertion guards
  against an NHC layer reshuffle silently drawing the wrong geometry. `[r-N5]`
- **`[r-B2]` Storm→feature match is by storm identity fields.** The wallet is the
  storm's **`id`** from `CurrentStorms.json` (e.g. `"al092024"`), already on
  `ActiveStorm.id`. Match Forecast-Points features on their **`stormname`** (and
  `basin`/`stormnum`) attributes — `id[:4]` (`"al09"`) decomposes to basin `AL` +
  number `09` for a fallback match. Do **not** use `idp_source[:3]` (that was wrong on
  both the field and the slice).
- **Forecast-point fields:** `validtime`, `tau` (12/24/…/120; the earliest carries the
  current/analysis position), `maxwind` (kt), `gust` (kt), `mslp` (mb), `ssnum`
  (Saffir-Simpson #), `tcdvlp`, plus geometry lon/lat. The hero's **`gust_mph` comes
  from the earliest forecast point's `gust`** (kt→mph) — `CurrentStorms.json` has no
  current gust — and is omitted when the GIS layer is unavailable. `[r-S3]`
- **NWS User-Agent.** `api.weather.gov` requires a descriptive `User-Agent` (no key,
  no account). Hardcoded client constant, not owner setup:
  `User-Agent: JBrain2-hurricane (+https://github.com/jeffmhopkins/JBrain2)`.
- **NWS `/points` returns the place IANA tz** (`properties.timeZone`, e.g.
  `America/New_York`). Timeline / arrival / timing labels render **place-local** via
  `zoneinfo.ZoneInfo` (stdlib, no dep); gridpoint `validTime`s are UTC. `as_of` stays
  the storm `lastUpdate` in UTC (shipped `format_as_of`). `[r-S3]`
- **NWS gridpoint series are run-length-encoded ISO intervals** (`start/PTnH`). Expand
  each entry across its covered hours, but **split by kind**: `windSpeed`/`windGust`
  are **instantaneous → replicate** the value into each hour; `quantitativePrecipitation`
  is an **accumulation over the interval → divide** the value across the covered hours
  (replicating it would multiply rain by the interval length). Convert km/h→mph
  (×0.621371) and mm→in (÷25.4). Any series may be **entirely absent** (e.g. no
  `windGust`) → treat as empty, never raise. `[r-S4]`
- **`[r-B3]` Surge band vocabulary.** The Peak-Surge polygon layer has **no numeric
  band field**; the band is text inside the feature's **`popupinfo`** HTML, drawn from
  the renderer's labels **"Up to 3 ft" / "Up to 6 ft" / "Up to 9 ft" / "Up to 12 ft" /
  "Above 12 ft"**. Parse the band out of `popupinfo` (regex `(Up to|Above)\s+\d+\s*ft`),
  not a `> N ft` string. Best-effort: `None` when no field or no active surge product.

## 2. Frozen `hurricane_card` payload shape (v2)

The model still authors **no markup and no color** (#9). **Superseded:** the Track tab
now renders on **real map tiles** (the on-box `/api/tiles` proxy), so the geometry
carries **real `{lat, lon}`** rather than a projected unit square, and the payload
carries one URL — `nhc_url`, the storm's public NHC graphics page. The `track`/`cone`
are public NHC coordinates; the `you` pin is still the **geocoded city centre** (never
`ctx.here`), so the most recoverable from the payload remains **city-centre coarseness**
— the same coarseness the shipped `weather`/`hurricane` tools already expose (§5), and
the same coarseness the old projected pin already revealed. `[r-B2-sec]`

```jsonc
{
  "place": "Tampa, Florida, United States",
  "as_of": "Sep 10, 3:00 PM UTC",        // storm lastUpdate, UTC
  "active_count": 2,
  "coverage": "us",                      // "us" = NWS reachable (timeline/alerts present);
                                         // "global" = NWS out-of-coverage (404) → NHC-only
  "storm": { "name": "Elena", "kind": "hurricane", "cat": "3",
             "sustained_mph": 120, "gust_mph": 150, "pressure_mb": 948,
             "moving": "NNE 14 mph" },   // gust_mph from GIS earliest point; 0 if no GIS
  "distance_mi": 215, "bearing": "SSW", "proximity": "near",

  "alert": { "level": "warning",         // "warning" | "watch" | "none"
             "kind": "hurricane",        // "hurricane" | "tropical-storm" | "surge" | "other"
             "event": "Hurricane Warning", "headline": "…" } ,   // or null; NWS-sourced text

  "track": [ { "x": 0.30, "y": 0.86, "label": "Now", "cat": "3", "past": false }, … ],
  "cone":  [ { "x": 0.31, "y": 0.80 }, … ],     // [] when unavailable
  "you":   { "x": 0.58, "y": 0.42 },            // projected geocoded city centre

  "timeline": [ { "label": "9 PM", "wind_mph": 35, "gust_mph": 50, "rain_in": 0.2,
                  "peak": false }, … ],          // place-local labels; [] when global
  "arrival": { "ts_force": "Wed 9 PM", "hurricane_force": "Thu 2 AM" },  // sustained-wind crossings; or null

  "impact": {
    "wind":  { "mph": 70, "gust": 100, "level": "high" },
    "surge": { "band": "Up to 9 ft", "level": "high" },   // verbatim NHC band; null when none
    "rain":  { "in": 8, "level": "moderate" },
    "timing":{ "onset": "Wed 9 PM", "peak": "Thu 4 AM", "clear": "Thu 1 PM" }  // derived/approx
  }
}
```

`level` enums are `low|moderate|high|extreme`. Every new slot is **optional**: a
`global` storm returns `coverage:"global"`, `alert:null`, `timeline:[]`, no
`impact.wind`/`rain`/`timing` (NWS-derived), and the card shows the hero + Track tab.
The component renders only what is present and hides empty tabs.

**`[r-S6]` Coverage derivation.** `coverage:"global"` is set **only** on a definitive
NWS **404** (out of coverage). A transient NWS **5xx/timeout** keeps `coverage:"us"`
with an empty `timeline`/`alert` (a blip must not relabel a US place as global).

## 3. Architecture & layering (routes → services → repos; clients in `jbrain/web/`)

New, independent client modules (each pure, `httpx.AsyncBaseTransport` injectable for
MockTransport tests; **no network, no real clock** in tests):

- **`jbrain/web/nhc_gis.py` — `NhcGisClient`.** `forecast_track(storm) -> tuple[TrackPoint,…]`
  and `cone(storm) -> tuple[LatLon,…]` over the tropical MapServer: name-based layer
  discovery + storm-identity feature match + GeoJSON parse. Returns absolute lon/lat +
  attributes; **no projection** (the tool projects). Earliest point's `gust` exposed
  for the hero.
- **`jbrain/web/nws.py` — `NwsClient`.** `alerts(lat,lon) -> tuple[Alert,…]` and
  `timeline(lat,lon) -> Timeline` (points→gridpoint, place tz, interval expansion with
  the accumulation/instantaneous split, unit conversion, TS/hurricane-force arrival at
  39/74 mph on **sustained** wind). A definitive 404 raises `NwsOutOfCoverage`; 5xx/timeout
  raises `NwsUnavailable` — the tool maps the two to `global` vs `us`+empty. **No
  coordinate or full request URL is ever logged**, and typed-error messages surfaced to
  the model carry no coordinate. `[r-S2-sec]`
- **`jbrain/web/nhc_surge.py` — `NhcSurgeClient`.** `peak_band(lat,lon) -> str|None`
  via the server-side `esriGeometryPoint` intersect; parse the band from `popupinfo`.
  Best-effort, US/territory coastal only; same no-coordinate-logging discipline.

Orchestration in **`jbrain/agent/hurricanetools.py`** (extended): after picking the
nearest storm, fire the independent fetches **concurrently** (`asyncio.gather(...,
return_exceptions=True)` — every source is best-effort; a failure yields an empty slot
so the hero + vitals always render). **Every off-box client is called with
`hit.latitude/hit.longitude` (the geocoded city centre), never `ctx.here`** — the
`here` precise fix is consumed only by `city_geocoder.nearest()` on-box, exactly as
shipped. `[r-S1-sec]` Then:
- project `track ∪ cone ∪ you` into `[0,1]` (`_project`, a pure helper, unit-tested
  with the edge cases in §8) and drop the lat/lon;
- shape `timeline`/`arrival` from the NWS series (place-local labels);
- pick the governing `alert` (warning > watch; among warnings hurricane > surge >
  tropical-storm);
- derive `impact` from timeline peak wind/gust + summed rain + surge band + alert.

**`hurricane.tool` → version 2.** Prose updated: it now surfaces official
watches/warnings and a local wind/rain timeline **where NWS covers the point (US &
territories)**, while binding the model to (a) scope claims to that coverage, (b) treat
`arrival`/`impact.timing` as **derived/approximate** (not official onset grids) and
defer **evacuation** to official orders, and (c) report surge as the **band** the card
shows, never a modeled depth. `[r-N3-sec]` A version bump re-stamps the digest guard
(`toolfile.py`); confirm the actual guard form rather than assuming a manifest. `[r-N2-feas]`

Config: add the pinned base URLs (`nhc_tropical_mapserver_url`,
`nws_api_url`, `nhc_surge_mapserver_url`) with public defaults, env-overridable, empty
disables that source (graceful degrade). `main.py`: construct the three clients, pass
into `build_hurricane_handlers`.

**`[r-S1-feas]` DESIGN.md is updated in this build** (a Wave-2 doc task or folded into
Wave 3): the shipped `hurricane_card` section says "position + strength only / never
rose / footer always" and pins the v1 payload — it must be rewritten to describe the
tabbed v2 card, the NWS-sourced alert banner (legitimate rose for a real warning), the
v2 shape, and the retained honesty boundaries.

## 4. Graceful degradation (binding behavior)

- **Non-US storm / point:** NWS 404 → `coverage:"global"`, no alert/timeline/impact-wind;
  Track tab still works (NHC GIS is global). Surge is **also US/territory-only** — for a
  non-US point the surge call is **skipped** (not fired), avoiding a pointless coordinate
  egress. `[r-S4-sec]` Hero + Track only.
- **Any single upstream down/empty (5xx/timeout):** that slot is empty; the rest renders;
  `coverage` stays `us`. NHC cone may lag points — a track without a cone still draws the line.
- **Per-series absence** (e.g. gridpoint without `windGust`/QPF): that field empty, not an error.
- **No active storms:** unchanged from v1 (the "all quiet" string, no card).
- Each call has a **short timeout** and is best-effort; a slow source never blocks the hero.

## 5. Location-firewall & security analysis (red-team gate target)

Reframed per the security review — the honest statement of egress and recoverability:

- **`[r-B1-sec]` New coordinate egress, bounded to city-centre coarseness.** The shipped
  `hurricane` tool sent **no** location anywhere. This build adds **two new coordinate
  egresses**: `api.weather.gov` (`point=lat,lon` for alerts + points/gridpoint) and the
  NHC **surge** MapServer (`geometry=lon,lat`). Both receive the **geocoded city centre**
  — the *same coarseness* the `weather` tool already sends to Open-Meteo (a city centre,
  never the owner's precise fix). The NHC **track/cone** GIS and `CurrentStorms.json`
  still carry **no** location (queried by storm identity). §1/§3 state this plainly; the
  v1 "no NHC location egress at all" wording is retired.
- **`[r-B2-sec]` The payload is invertible only to city-centre coarseness.** Because
  forecast track points have **public** absolute coordinates, an observer can recover the
  bbox→`[0,1]` affine transform and invert `you` to an absolute point. That point is the
  **geocoded city centre** (the projection input is `hit`, never `ctx.here`), so the leak
  is bounded to city-centre coarseness — acceptable and identical to naming the city. The
  v1 claim "no coordinate is recoverable" is **false** and is replaced by this bounded
  statement. `_project` MUST take the city centre; a test asserts `ctx.here` never feeds
  projection or any off-box client.
- **`here` path** resolves the precise fix to a nearest-city **name** on-box, geocodes
  that name → city centre, and that single `GeoHit` centre feeds every off-box client and
  the projection. The precise fix is consumed only by `city_geocoder.nearest()`. `[r-S1-sec]`
- **No coordinate in logs or error text.** New clients never log the raw coordinate or
  the full request URL; typed errors surfaced to the model carry no coordinate. `[r-S2-sec]`
- **Data/instruction boundary.** Upstream free-text (`headline`, `event`, `stormname`,
  surge `popupinfo`) renders as **text content only** in the data-only view (no
  `dangerouslySetInnerHTML`, no markdown/URL parsing). A Wave-3 test renders a headline
  containing markup and asserts it is escaped. `[r-N2-sec]`
- No owner notes / RLS data in jerv's context (sandbox `web` class); no new table → no
  RLS test, but **§5 is a mandatory per-wave red-team item** (Wave 2).

## 6. Explicitly out of scope (keeps zero-dep / no-setup)

- **GRIB2 products** (probabilistic P-Surge, NHC wind-speed-probability & arrival-time
  grids) — binary rasters needing a heavy dep. Dropped. Arrival is **derived** from NWS
  hourly **sustained** wind crossing 39/74 mph (documented approximate); surge is the
  Peak-Surge **band**, not a modeled depth.
- **Third-party APIs** (Xweather, Ambee, Google/Apple) — all keyed, all reselling this
  NHC/NWS data; rejected by requirement (1).
- **Tile/basemap rendering** — the Track tab is a stylized storm-relative diagram (unit
  square, no coastline), like the mock; no tiles (also #9-friendly).

## 7. Waves (per `docs/PROCESS.md`)

**Wave 1 — data clients (3 parallel tasks, isolated worktrees; new files only).** Each:
builder agent → independent adversarial review (≠ builder) → local `ruff`+`pyright`+unit
tests before merge to `wave-1`.
- **1a `NhcGisClient`** — name-based layer discovery, storm-identity match, track points +
  cone GeoJSON, earliest-point gust + tests.
- **1b `NwsClient`** — alerts + gridpoint timeline; place tz; interval expansion
  (accumulation/instantaneous split); arrival on sustained wind; 404→OutOfCoverage vs
  5xx→Unavailable; no-coordinate logging + tests.
- **1c `NhcSurgeClient`** — server-side point intersect; `popupinfo` band parse
  ("Up to/Above N ft") + tests.

**Wave 2 — tool assembly + payload + wiring + DESIGN.md (1 task; heaviest — flag for
possible scope-deviation escalation `[r-N5-feas]`; depends on Wave 1).** Extend
`hurricanetools.py` (concurrent orchestration; `hit`-only egress; `_project` with edge
cases; timeline/impact shaping; alert precedence; graceful degrade; coverage derivation);
bump `hurricane.tool` v2; config + `main.py` wiring; **update DESIGN.md** `[r-S1-feas]`.
**Mandatory per-wave red-team review of §5.** Tests: handler-level MockTransport across
all sources incl. the non-US degrade and one-source-down paths; `_project` unit tests
(below); firewall assertions (no `latitude`/`longitude` substring in payload; `here`
geocodes only the city name; off-box clients receive `hit`, never `ctx.here`).

**Wave 3 — frontend tabbed card (1 task; depends on Wave 2's frozen shape).** Rebuild
`HurricaneCard` into the tabbed component (persistent hero + NWS alert banner +
Timeline/Track/Impact tabs + My-impact/Storm-stats toggle) matching the binding mock;
tokens-only `.tv-hu-*`; inline SVG track/cone from `[0,1]` slots; view render tests
(warning/watch/none; us/global coverage; empty-tab hiding; upstream-headline escaping).
GUI pre-approved — no mock round.

Each wave: one PR (or, on this feature branch, one wave commit set + a wave-status
report), both review gates clean, locally verified; CI green before proceeding.

## 8. Testing plan

- **Clients (unit, MockTransport):** GeoJSON parse incl. empty/off-season; **name-based**
  layer pick (right layer by name + storm match; wrong-name layer rejected); interval
  expansion PT1H/PT3H/PT6H with **QPF divided** vs **wind replicated**; km/h→mph & mm→in;
  arrival crossing at 39/74 mph on sustained wind; **missing series → empty**; surge band
  regex on real `popupinfo` ("Up to 9 ft"/"Above 12 ft"); 404→OutOfCoverage vs
  5xx→Unavailable; no coordinate in logs/errors.
- **Tool (unit, MockTransport across sources):** full US assembly; non-US degrade
  (`coverage:"global"`, surge **skipped**, empty timeline/alert); one-source-5xx →
  `coverage:"us"`+empty; alert precedence; `_project` maps a known bbox to expected
  `[0,1]` **and** handles single-point/degenerate-span (no div-by-zero; centered) and
  **antimeridian** (longitudes normalized before bbox) `[r-S5-feas]`; **firewall:** no
  `latitude`/`longitude` substring in payload; off-box clients invoked with `hit`, never
  `ctx.here`.
- **Frontend (Vitest/RTL):** tab switch; warning banner (rose) vs watch (amber) vs none;
  `coverage:"global"` hides Timeline/Impact; track/cone SVG drawn from slots;
  inline-SVG-only (#9, no `<img>`); **upstream headline with markup is escaped text**.
- Coverage ≥ 80% (gate); firewall/degrade paths covered. No network, no real clock
  (labels come from upstream ISO strings via `zoneinfo`, never `datetime.now`).

## 9. Open decisions (escalate per PROCESS §Communication)

- **Timeline window/resolution:** default **next 36h at 3-hourly** (≈12 cells) to match
  the mock's strip density; revisit if NWS resolution degrades past 36h makes it ragged.
- **Label timezone:** timeline/arrival/timing render **place-local** via the NWS `/points`
  `timeZone` + `zoneinfo`; `as_of` stays UTC. (Decided `[r-S3-feas]`.)
- **Surge band display:** show the NHC Peak-Surge band string verbatim ("Up to 9 ft")
  rather than a single number — the product is banded; avoids false precision.
- **Aspect ratio:** `_project` preserves aspect by padding the shorter axis (letterbox)
  so the storm geometry isn't distorted in the unit square. (Decided `[r-S5-feas]`.)
