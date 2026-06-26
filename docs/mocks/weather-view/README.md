# Weather tool-view — mock round (4 variants)

> **Decided: Variant A (hero + hourly strip) + Open-Meteo.** Shipped — the `weather`
> tool (jerv-only, `web` class) + the registered `weather_card` view. The settled
> pattern is recorded in `docs/DESIGN.md` "`weather_card` tool-view"; B/C/D are kept
> below as the record.


A `weather` tool for **jerv** + a registered in-chat **`weather_card`** view, to
replace the long multi-step web-search-and-markdown-table flow with one glanceable
component. Mock-first per DESIGN.md "UI development process" — these four are the
options-before-commitment round; the owner picks, and the chosen pattern + reasoning
gets a `docs/DESIGN.md` subsection in the implementing PR.

All four are tokens-only, dark-first with a light toggle, and render **no external
resources** (invariant #9 — condition glyphs are inline Lucide-style SVG, never
fetched icons or map tiles). Sample data is the Cocoa, FL · now→midnight forecast
from the reference screenshots. Weather is non-personal jerv info, so the card rides
the **steel** info accent; a high heat index reads **amber** (the warn/heat tone).
`cond` is an enum the component maps to a glyph + token — the model never sends a
color (DESIGN.md "components express tone/flag/kind enums, never colors").

## The variants

| | File | Direction | Strengths | Trade-off |
|---|---|---|---|---|
| **A** | `weather-a-hero-strip.html` | **Hero + hourly strip** — big current card, finger-scrollable hourly row | Most glanceable; the familiar phone-weather idiom; instant "what's it like now" | Hourly detail (feels/wind) hides in the strip |
| **B** | `weather-b-curve.html` | **Temperature curve** — `lab_plot`-style SVG: temp line + feels band + precip bars, tap to pin an hour | Shows the *shape* of the day; great for "from now until midnight" trend questions | Per-hour precision needs a tap; chart is the most net-new component |
| **C** | `weather-c-rows.html` | **Compact dossier rows** — one tight row per hour (glyph · temp bar · feels · wind/precip) | The honest 1:1 upgrade of the markdown table; most complete & precise; pure `data_table` paradigm | Tallest in the transcript for a 12-hour span; least "designed" |
| **D** | `weather-d-segmented.html` | **Segmented facets** — Now · Hourly · Rain & wind, one panel at a time (settled segmented-tasks paradigm) | Stays compact while carrying current detail + hourly + precip/wind; reuses an established pattern | Hourly numbers are a tap away; three surfaces to design |

## Recommendation

**A or D.** A is the lowest-friction answer to the literal question ("what's the
weather now → midnight") and reads in one glance. D layers the same hero over the
established Now·Hourly·Rain-&-wind segmented pattern when the owner wants the extra
facets without a tall card. B is the most distinctive and best for trend-shaped
questions but is the heaviest new component; C is the safest (reuses `data_table`)
but is the least of an upgrade over the table it replaces. A common landing spot is
**A's hero as the default with D's segments as the expand** — but that is a
post-pick synthesis, not a fifth option here.

## How it wires up (research summary)

Mirrors the existing jerv tool + view machinery exactly (see the tool/view map in
the implementing PR):

- **Tool sidecar** `backend/src/jbrain/agent/tools/weather.tool` — YAML frontmatter
  (`name: weather`, `version: 1`, `permission: web`, params: `{location?}` —
  defaults to jerv's `current_location` when omitted) + prose the model reads.
- **Handler** `backend/src/jbrain/agent/weathertools.py` —
  `async def weather_tool(arguments, ctx: ToolContext) -> ToolOutput`, returning a
  concise text observation **and** `view=weather_view(...)` (a `ViewPayload`,
  `view="weather_card"`, `surface="inline"`, data-only slots — no URLs).
- **Allowlist** — add `"weather"` to `JERV_TOOLS` in `agents.py`; `permission: web`
  keeps it jerv-only (curator never gets it). Registered via `build_registry(...)`
  in `readtools.py`; gated off (graceful degrade) if its upstream is unconfigured.
- **Frontend** — add `weather_card: WeatherCard` to the `REGISTRY` in
  `frontend/src/agent/views/registry.tsx`; `WeatherCard({data})` renders the chosen
  variant from typed slots.
- **Tests** — `test_agent_weathertools.py` (handler returns text + a `weather_card`
  view with the expected slots; the adapter/upstream faked) following the
  `analyze_video` / `geocode_reverse` test patterns.

### Open question for the owner — the data source

Weather needs an upstream and a way to turn a place into coordinates. Options, all
fitting jerv's sandbox (the bound is the sandbox, not a promise — jerv holds no
knowledge-base tools, §ASSISTANT "the web exception"):

1. **Open-Meteo** — free, no API key, one call returns current + hourly + daily, and
   it has a free name→lat/lon geocoding endpoint (covers the "no on-box forward
   geocoder" gap). Simplest; a pinned base URL like SearXNG.
2. **US NWS `api.weather.gov`** — authoritative (the screenshots' source), free, but
   US-only and needs a lat/lon (a 2-step gridpoint lookup).

Either runs as a **pinned-base-URL jerv tool** (config-pinned host, typed params, the
same direct-web posture as `web_search`/SearXNG and the SSRF-guarded `web_fetch`) —
**not** a staged egress connector, since jerv's web tools run directly by design.
For a named city the tool geocodes the name; for "here" it uses jerv's coarse
`current_location` (place name only — coordinates stay render-only, never in a query).
Recommend **Open-Meteo** for the no-key, single-call, built-in-geocoding fit.
