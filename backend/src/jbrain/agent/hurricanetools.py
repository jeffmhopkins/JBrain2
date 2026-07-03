"""jerv's `hurricane` tool (docs/reference/ASSISTANT.md "Agent selection", DESIGN.md
"hurricane_card tool-view"; build plan docs/HURRICANE_TABS_PLAN.md).

A jerv-only `web`-class tool: given a place, it finds the nearest active tropical
cyclone from NHC's global feed and returns a concise summary AND a data-only
`hurricane_card` view. The card carries the storm's identity + vitals, its forecast
**track** and cone, the official NWS **watch/warning** for the place, and a local
wind/rain **timeline** with derived impacts — wherever NWS covers the point (US &
territories); a non-US point degrades to the storm hero + track only.

Geocoding is reused from the weather tool so the location firewall holds identically.
The NHC active-storm + GIS feeds carry no location (queried by storm identity); the
two new coordinate egresses — NWS (alerts + gridpoint) and the NHC surge MapServer —
receive only the geocoded **city centre** (`hit`), never the owner's precise fix
(`ctx.here`), the same coarseness the weather tool already exposes.

The Track tab renders on real map tiles (the on-box `/api/tiles` proxy), so the card
carries real coordinates — a scoped relaxation of the no-lat/lon rule (#9) for THIS
view. The storm's track + cone are public NHC data; the only owner-derived coordinate
is the `you` pin, and it is the geocoded **city centre** (`hit`), never `ctx.here` —
the same coarseness the projected pin already revealed (it was recoverable by
inverting it against the public track), so nothing new about the owner egresses. The
card also carries the storm's public NHC graphics-page URL for a "see the official
forecast" link.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.citygeocode import CityGeocoder
from jbrain.web.hurricane import (
    ActiveStorm,
    HurricaneClient,
    HurricaneError,
    bearing_deg,
    category,
    compass,
    format_as_of,
    haversine_mi,
    movement,
    nhc_storm_url,
    sustained_mph,
)
from jbrain.web.nhc_gis import NhcGisClient, TrackPoint
from jbrain.web.nhc_surge import NhcSurgeClient
from jbrain.web.nws import Alert, NwsClient, NwsOutOfCoverage, Timeline
from jbrain.web.weather import GeoHit, WeatherClient, WeatherError

_NO_LOCATION = (
    'I need a place to check — name a city (e.g. "hurricane near Tampa"), or share '
    "your location and I'll use the nearest city."
)
_NO_STORMS = (
    "No active tropical cyclones are being tracked right now (NHC's Atlantic, Eastern "
    "Pacific, and Central Pacific basins are all quiet). The app shows no storm card."
)
# Distance bands (statute miles) for the computed proximity enum (a neutral how-close
# tone, NOT an official watch/warning — those come from the NWS `alert` slot).
_NEAR_MI = 300
_REGIONAL_MI = 700
_KT_TO_MPH = 1.15078
# Coordinate precision on the payload: ~4 decimals (~11 m) is ample for a storm-scale
# map and trims float noise; the `you` pin is already the city centre, so this rounding
# is cosmetic, not a coarsening step.
_COORD_DP = 4

# Alert ranking for the governing-alert pick: a warning outranks a watch; within a
# level the more acute hazard wins. These are NWS-sourced (the only legitimate
# watch/warning surface), never computed.
_ALERT_LEVEL_RANK = {"warning": 2, "watch": 1}
_ALERT_KIND_RANK = {"hurricane": 3, "surge": 2, "tropical-storm": 1, "other": 0}


@dataclass(frozen=True)
class StormDetail:
    """The per-storm detail gathered from the GIS + NWS + surge feeds, each best-effort
    (an empty/None slot means that source was unavailable or out of coverage). `coverage`
    is "us" when NWS served the point (timeline/alerts present) or "global" when NWS
    reported the point out of coverage (a 404, not a transient failure)."""

    track: tuple[TrackPoint, ...]
    cone: tuple[tuple[float, float], ...]
    alerts: tuple[Alert, ...]
    timeline: Timeline | None
    surge_band: str | None
    coverage: str


def build_hurricane_handlers(
    client: HurricaneClient,
    weather_client: WeatherClient,
    city_geocoder: CityGeocoder,
    gis_client: NhcGisClient,
    nws_client: NwsClient,
    surge_client: NhcSurgeClient,
) -> dict[str, ToolHandler]:
    async def hurricane_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("location", "")).strip()
        try:
            hit = await _resolve(weather_client, city_geocoder, name, ctx)
        except WeatherError as exc:
            return str(exc)
        if hit is None:
            if name:
                # The geocoder already accepts "City", "City, State", and "City, Country"
                # (state may be abbreviated), so a not-found is a genuine miss — retrying
                # the same place in another comma format won't help; steer to a nearby city.
                return (
                    f'I couldn\'t find a place called "{name}". Try the nearest larger '
                    "city, or check the spelling — retrying the same place a different "
                    "way won't change the result."
                )
            return _NO_LOCATION
        try:
            storms = await client.active_storms()
        except HurricaneError as exc:
            return str(exc)
        if not storms:
            return _NO_STORMS
        nearest, distance, bearing = _nearest(storms, hit)
        detail = await _gather_detail(gis_client, nws_client, surge_client, nearest, hit)
        return ToolOutput(
            _summarize(hit, nearest, distance, bearing, len(storms), detail),
            view=hurricane_view(hit, nearest, distance, bearing, len(storms), detail),
        )

    return {"hurricane": hurricane_tool}


async def _resolve(
    client: WeatherClient, city_geocoder: CityGeocoder, name: str, ctx: ToolContext
) -> GeoHit | None:
    """Turn the request into a place to measure from — the same firewall the weather
    tool uses: a named place geocodes directly; an empty location uses the owner's fix,
    resolved to a city NAME on-box first so only a public place name (never the precise
    fix) is geocoded, and every off-box measurement uses the resulting city centre."""
    if name:
        return await client.geocode(name)
    if ctx.here is None:
        return None
    lat, lon = ctx.here
    city = city_geocoder.nearest(lat, lon)
    if city is None:
        raise WeatherError("I couldn't pin a nearby city to check for storms.")
    return await client.geocode(city.name)


def _nearest(storms: tuple[ActiveStorm, ...], hit: GeoHit) -> tuple[ActiveStorm, int, str]:
    """The active storm closest to the place, with the rounded distance (miles) and the
    compass bearing FROM the place TO that storm."""
    measured = [
        (s, haversine_mi(hit.latitude, hit.longitude, s.latitude, s.longitude)) for s in storms
    ]
    storm, dist = min(measured, key=lambda m: m[1])
    bearing = compass(bearing_deg(hit.latitude, hit.longitude, storm.latitude, storm.longitude))
    return storm, round(dist), bearing


async def _gather_detail(
    gis: NhcGisClient,
    nws: NwsClient,
    surge: NhcSurgeClient,
    storm: ActiveStorm,
    hit: GeoHit,
) -> StormDetail:
    """Fetch track/cone (by storm identity, no location) and alerts/timeline (the city
    centre) concurrently — every source best-effort, so a failure yields an empty slot
    and the hero + vitals always render. NWS coverage is read from a definitive 404
    (out of coverage → "global") vs a transient failure (stays "us", empty). The surge
    point-query — a coordinate egress — fires ONLY for an in-coverage US point."""
    lat, lon = hit.latitude, hit.longitude
    track_r, cone_r, alerts_r, timeline_r = await asyncio.gather(
        gis.forecast_track(storm),
        gis.cone(storm),
        nws.alerts(lat, lon),
        nws.timeline(lat, lon),
        return_exceptions=True,
    )
    # `return_exceptions=True` surfaces any raised error (incl. a BaseException like
    # CancelledError) as the result value; a non-success result is intentionally treated
    # as "that source was unavailable" → an empty slot, so one feed never fails the card.
    track = track_r if isinstance(track_r, tuple) else ()
    cone = cone_r if isinstance(cone_r, tuple) else ()
    alerts = alerts_r if isinstance(alerts_r, tuple) else ()
    timeline = timeline_r if isinstance(timeline_r, Timeline) else None
    out_of_coverage = isinstance(alerts_r, NwsOutOfCoverage) or isinstance(
        timeline_r, NwsOutOfCoverage
    )
    coverage = "global" if out_of_coverage else "us"
    surge_band: str | None = None
    if coverage == "us":
        try:
            surge_band = await surge.peak_band(lat, lon)
        except Exception:  # noqa: BLE001 — surge is best-effort; any failure → no band
            surge_band = None
    return StormDetail(track, cone, alerts, timeline, surge_band, coverage)


# --- view assembly ---------------------------------------------------------


def hurricane_view(
    hit: GeoHit,
    storm: ActiveStorm,
    distance_mi: int,
    bearing: str,
    active_count: int,
    detail: StormDetail,
) -> ViewPayload:
    """The data-only `hurricane_card` view (docs/HURRICANE_TABS_PLAN.md §2). Markup-free;
    `kind`/`cat`/`proximity`/`alert.level`/`level` are enums the component maps to glyph +
    tone. The Track tab draws on real tiles, so the map geometry carries real lat/lon — the
    public storm track + cone, plus the `you` city centre (the scoped #9 relaxation, see the
    module docstring). `nhc_url` is the storm's public NHC graphics page."""
    cat = category(storm.wind_kt, storm.kind)
    gust_mph = _kt_to_mph(detail.track[0].gust_kt) if detail.track else 0
    sustained = sustained_mph(storm)
    track_geo, cone_geo, you_geo = _geo(detail.track, detail.cone, hit)
    return ViewPayload(
        view="hurricane_card",
        surface="inline",
        data={
            "place": hit.name,
            "as_of": format_as_of(storm.last_update),
            "active_count": active_count,
            "coverage": detail.coverage,
            "storm": {
                "name": storm.name,
                "kind": storm.kind,
                "cat": cat,
                "sustained_mph": sustained,
                # Severity tiers the card maps to a gauge fill + tone (DESIGN.md: the
                # backend owns the enum, the component the palette), so the Storm-stats
                # gauges track the real storm rather than a fixed decoration.
                "sustained_level": _wind_level(sustained),
                "gust_mph": gust_mph,
                "gust_level": _wind_level(gust_mph),
                "pressure_mb": storm.pressure_mb,
                "pressure_level": _pressure_level(storm.pressure_mb),
                "moving": movement(storm),
            },
            "distance_mi": distance_mi,
            "bearing": bearing,
            "proximity": _proximity(distance_mi, storm.kind),
            "alert": _governing_alert(detail.alerts),
            "track": track_geo,
            "cone": cone_geo,
            "you": you_geo,
            "nhc_url": nhc_storm_url(storm),
            "timeline": _timeline_cells(detail.timeline),
            "arrival": _arrival(detail.timeline),
            "impact": _impact(detail.timeline, detail.surge_band),
        },
    )


def _proximity(distance_mi: int, kind: str) -> str:
    """A neutral proximity enum from distance + storm type — `near` (caution), then
    `regional`, then `distant` (info). NOT an NWS watch/warning; a weak/remnant system
    never reads `near`."""
    threatening = kind in ("hurricane", "typhoon", "tropical-storm", "potential")
    if distance_mi <= _NEAR_MI and threatening:
        return "near"
    if distance_mi <= _REGIONAL_MI:
        return "regional"
    return "distant"


def _governing_alert(alerts: tuple[Alert, ...]) -> dict | None:
    """The single most acute NWS alert for the place (warning > watch; within a level
    hurricane > surge > tropical-storm > other), or None. NWS-sourced text only — the
    only legitimate watch/warning surface."""
    if not alerts:
        return None
    top = max(
        alerts,
        key=lambda a: (_ALERT_LEVEL_RANK.get(a.level, 0), _ALERT_KIND_RANK.get(a.kind, 0)),
    )
    return {"level": top.level, "kind": top.kind, "event": top.event, "headline": top.headline}


def _timeline_cells(timeline: Timeline | None) -> list[dict]:
    """The timeline strip cells; the peak-gust cell is flagged for the component. Empty
    when NWS is out of coverage or returned nothing."""
    if timeline is None or not timeline.cells:
        return []
    peak_gust = max((c.gust_mph for c in timeline.cells), default=0)
    peaked = False
    cells: list[dict] = []
    for c in timeline.cells:
        is_peak = not peaked and peak_gust > 0 and c.gust_mph == peak_gust
        if is_peak:
            peaked = True
        cells.append(
            {
                "label": c.label,
                "wind_mph": c.wind_mph,
                "gust_mph": c.gust_mph,
                "rain_in": round(c.rain_in, 1),
                "peak": is_peak,
            }
        )
    return cells


def _arrival(timeline: Timeline | None) -> dict:
    """The derived (approximate) arrival labels for tropical-storm- and hurricane-force
    sustained winds; both None when NWS is absent or the thresholds aren't reached."""
    if timeline is None:
        return {"ts_force": None, "hurricane_force": None}
    return {
        "ts_force": timeline.ts_force_label,
        "hurricane_force": timeline.hurricane_force_label,
    }


def _impact(timeline: Timeline | None, surge_band: str | None) -> dict:
    """The Impact-tab summary derived locally from the timeline + surge band. Wind/rain/
    timing come from the NWS series (present only with US coverage); surge is the NHC
    band. Every field is optional — the component renders what's present."""
    impact: dict = {}
    if timeline is not None and timeline.cells:
        peak_wind = max(c.wind_mph for c in timeline.cells)
        peak_gust = max(c.gust_mph for c in timeline.cells)
        rain_total = round(sum(c.rain_in for c in timeline.cells), 1)
        impact["wind"] = {
            "mph": peak_wind,
            "gust": peak_gust,
            "level": _wind_level(peak_wind),
        }
        impact["rain"] = {"in": rain_total, "level": _rain_level(rain_total)}
        impact["timing"] = _timing(timeline)
    if surge_band:
        impact["surge"] = {"band": surge_band, "level": _surge_level(surge_band)}
    return impact


def _timing(timeline: Timeline) -> dict:
    """Onset / peak / clear labels from the timeline: TS-force arrival, the peak-gust
    cell, and the first post-peak cell whose sustained wind drops back below TS-force."""
    cells = timeline.cells
    peak_idx = max(range(len(cells)), key=lambda i: cells[i].gust_mph) if cells else 0
    clear: str | None = None
    for c in cells[peak_idx + 1 :]:
        if c.wind_mph < 39:
            clear = c.label
            break
    peak_label = cells[peak_idx].label if cells else None
    return {"onset": timeline.ts_force_label, "peak": peak_label, "clear": clear}


def _wind_level(mph: int) -> str:
    if mph >= 110:
        return "extreme"
    if mph >= 74:
        return "high"
    if mph >= 39:
        return "moderate"
    return "low"


def _pressure_level(mb: int) -> str:
    """Central pressure → severity tone (lower is stronger), banded to roughly track the
    Saffir-Simpson pressure ranges: cat 5 / cat 3–4 / cat 1–2 / weaker. Unknown (0)
    reads low."""
    if mb <= 0:
        return "low"
    if mb <= 920:
        return "extreme"
    if mb <= 964:
        return "high"
    if mb <= 989:
        return "moderate"
    return "low"


def _rain_level(inches: float) -> str:
    if inches >= 12:
        return "extreme"
    if inches >= 6:
        return "high"
    if inches >= 3:
        return "moderate"
    return "low"


def _surge_level(band: str) -> str:
    """Map an NHC surge band ("Up to 9 ft" / "Above 12 ft") to a severity tone by feet."""
    digits = "".join(ch if ch.isdigit() else " " for ch in band).split()
    feet = int(digits[0]) if digits else 0
    if "above" in band.lower() or feet >= 9:
        return "extreme" if feet >= 12 else "high"
    if feet >= 6:
        return "high"
    if feet >= 3:
        return "moderate"
    return "low"


def _geo(
    track: tuple[TrackPoint, ...],
    cone: tuple[tuple[float, float], ...],
    hit: GeoHit,
) -> tuple[list[dict], list[dict], dict]:
    """Shape the storm geometry + the place into `{lat, lon}` slots the Track tab draws
    on real tiles. The track/cone are public NHC coordinates; the `you` pin is the
    geocoded city centre (`hit`), NEVER the owner's precise fix — the scoped #9
    relaxation for this on-tile view (see the module docstring). Leaflet frames the map
    from these bounds, so no on-box projection is needed. Cone vertices arrive as
    `(lon, lat)` pairs (GeoJSON order) and are reordered to `{lat, lon}` here."""
    track_geo = [
        {
            "lat": round(p.latitude, _COORD_DP),
            "lon": round(p.longitude, _COORD_DP),
            "label": p.label,
            "cat": p.ss_cat,
            "past": p.past,
        }
        for p in track
    ]
    cone_geo = [{"lat": round(lat, _COORD_DP), "lon": round(lon, _COORD_DP)} for lon, lat in cone]
    you_geo = {"lat": round(hit.latitude, _COORD_DP), "lon": round(hit.longitude, _COORD_DP)}
    return track_geo, cone_geo, you_geo


def _kt_to_mph(kt: int) -> int:
    """Knots → mph, rounded to the nearest 5 as NHC reports."""
    return int(round(kt * _KT_TO_MPH / 5.0) * 5)


# --- model-facing summary --------------------------------------------------


def _label(storm: ActiveStorm) -> str:
    """The storm's headline name for prose: "Hurricane Elena (Category 3)" etc."""
    cat = category(storm.wind_kt, storm.kind)
    titles = {
        "hurricane": "Hurricane",
        "typhoon": "Typhoon",
        "tropical-storm": "Tropical Storm",
        "tropical-depression": "Tropical Depression",
        "subtropical-storm": "Subtropical Storm",
        "subtropical-depression": "Subtropical Depression",
        "post-tropical": "Post-Tropical Cyclone",
        "potential": "Potential Tropical Cyclone",
        "low": "Tropical Low",
    }
    head = f"{titles.get(storm.kind, 'Cyclone')} {storm.name}"
    return f"{head} (Category {cat})" if cat else head


def _summarize(
    hit: GeoHit,
    storm: ActiveStorm,
    distance_mi: int,
    bearing: str,
    active_count: int,
    detail: StormDetail,
) -> str:
    """A concise observation for the model. Names the nearest storm, where it is, its
    strength and motion, then the OFFICIAL alert (if any) and the derived arrival —
    flagged approximate — and notes the card carries the rest. Binds the model to the
    honesty boundary: official warnings come from NWS where it covers the point; surge,
    rainfall, and arrival timing are approximate or banded, and evacuation follows
    official orders, not this card."""
    move = movement(storm)
    motion = f"moving {move}" if move != "stationary" else "nearly stationary"
    parts = [
        f"Nearest active tropical cyclone to {hit.name}: {_label(storm)}, about "
        f"{distance_mi} mi {bearing} and {motion}. Max sustained winds "
        f"{sustained_mph(storm)} mph, pressure {storm.pressure_mb} mb."
    ]
    if active_count > 1:
        n = active_count - 1
        parts.append(f"({n} other active {'storm' if n == 1 else 'storms'} elsewhere.)")
    alert = _governing_alert(detail.alerts)
    if alert is not None:
        parts.append(f"Official NWS alert for {hit.name}: {alert['event']}.")
    if detail.timeline is not None and detail.timeline.ts_force_label:
        parts.append(
            f"Tropical-storm-force winds arrive about {detail.timeline.ts_force_label} "
            "(approximate, derived from the local forecast)."
        )
    if detail.coverage == "global":
        parts.append(
            "This point is outside NWS coverage, so no official watches/warnings or local "
            "timeline are available — the card shows the storm and its forecast track only."
        )
    parts.append(
        "The app is showing a hurricane card with the storm's vitals, forecast track, and "
        "(where NWS covers the place) the official alert and a local wind/rain timeline. "
        "Surge is a banded estimate and arrival/impact timing is approximate — for "
        "watches, warnings, and especially evacuation decisions, defer to official "
        "NWS/NHC advisories and local emergency management, not this card."
    )
    return " ".join(parts)
