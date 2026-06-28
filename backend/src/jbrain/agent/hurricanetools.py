"""jerv's `hurricane` tool (docs/ASSISTANT.md "Agent selection", DESIGN.md
"hurricane_card tool-view").

A jerv-only `web`-class tool: given a place, it finds the nearest active tropical
cyclone from NHC's global feed and returns a concise summary AND a data-only
`hurricane_card` view (the storm's identity, vitals, and its distance + bearing from
the place). Thin over HurricaneClient; geocoding is reused from the weather tool so
the location firewall holds identically — a named place is geocoded by name, the
owner's "here" fix is resolved to a nearest-city NAME on-box first, and the
distance/bearing to a storm is computed on-box from the city centre, so the owner's
precise position never leaves the box.

The NHC feed is the global active-storm list and takes no query, so checking for
storms sends no location at all; the only place name that ever goes out is the
geocoder lookup, exactly as with weather.
"""

from __future__ import annotations

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
    sustained_mph,
)
from jbrain.web.weather import GeoHit, WeatherClient, WeatherError

_NO_LOCATION = (
    'I need a place to check — name a city (e.g. "hurricane near Tampa"), or share '
    "your location and I'll use the nearest city."
)
_NO_STORMS = (
    "No active tropical cyclones are being tracked right now (NHC's Atlantic, Eastern "
    "Pacific, and Central Pacific basins are all quiet). The app shows no storm card."
)
# Distance bands (statute miles) for the computed proximity enum — a neutral
# "how close is the nearest storm" assessment, NOT an official NWS watch/warning.
_NEAR_MI = 300
_REGIONAL_MI = 700


def build_hurricane_handlers(
    client: HurricaneClient, weather_client: WeatherClient, city_geocoder: CityGeocoder
) -> dict[str, ToolHandler]:
    async def hurricane_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("location", "")).strip()
        try:
            hit = await _resolve(weather_client, city_geocoder, name, ctx)
        except WeatherError as exc:
            return str(exc)
        if hit is None:
            if name:
                return f'I couldn\'t find a place called "{name}".'
            return _NO_LOCATION
        try:
            storms = await client.active_storms()
        except HurricaneError as exc:
            return str(exc)
        if not storms:
            return _NO_STORMS
        nearest, distance, bearing = _nearest(storms, hit)
        return ToolOutput(
            _summarize(hit, nearest, distance, bearing, len(storms)),
            view=hurricane_view(hit, nearest, distance, bearing, len(storms)),
        )

    return {"hurricane": hurricane_tool}


async def _resolve(
    client: WeatherClient, city_geocoder: CityGeocoder, name: str, ctx: ToolContext
) -> GeoHit | None:
    """Turn the request into a place to measure from — the same firewall the weather
    tool uses: a named place geocodes directly; an empty location uses the owner's
    fix, resolved to a city NAME on-box first so only a public place name (never the
    precise fix) is geocoded, and the distance is computed on-box from the centre."""
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
    """The active storm closest to the place, with the rounded distance (miles) and
    the compass bearing FROM the place TO that storm — the geometry that answers
    "is it coming at me, and from where?"."""
    measured = [
        (s, haversine_mi(hit.latitude, hit.longitude, s.latitude, s.longitude)) for s in storms
    ]
    storm, dist = min(measured, key=lambda m: m[1])
    bearing = compass(bearing_deg(hit.latitude, hit.longitude, storm.latitude, storm.longitude))
    return storm, round(dist), bearing


def _proximity(distance_mi: int, kind: str) -> str:
    """A neutral proximity enum from distance + storm type — `near` (caution), then
    `regional`, then `distant` (info). This is a computed how-close assessment the
    component tones, NOT an NWS watch/warning (those need the api.weather.gov alerts
    feed, a follow-up); a weak/remnant system never reads `near`."""
    threatening = kind in ("hurricane", "typhoon", "tropical-storm", "potential")
    if distance_mi <= _NEAR_MI and threatening:
        return "near"
    if distance_mi <= _REGIONAL_MI:
        return "regional"
    return "distant"


def hurricane_view(
    hit: GeoHit, storm: ActiveStorm, distance_mi: int, bearing: str, active_count: int
) -> ViewPayload:
    """The data-only twin of the summary: a `hurricane_card` view the app renders
    inline. No URLs, no markup (#9); `kind`/`cat`/`proximity` are enums the component
    maps to a glyph + tone, never colors (DESIGN.md "Agent tool views"). The shape
    leaves room for the forecast track + impact timeline (a follow-up over NHC's GIS
    and api.weather.gov feeds), absent here."""
    cat = category(storm.wind_kt, storm.kind)
    return ViewPayload(
        view="hurricane_card",
        surface="inline",
        data={
            "place": hit.name,
            "as_of": format_as_of(storm.last_update),
            "active_count": active_count,
            "storm": {
                "name": storm.name,
                "kind": storm.kind,
                "cat": cat,
                "sustained_mph": sustained_mph(storm),
                "pressure_mb": storm.pressure_mb,
                "moving": movement(storm),
            },
            "distance_mi": distance_mi,
            "bearing": bearing,
            "proximity": _proximity(distance_mi, storm.kind),
        },
    )


def _label(storm: ActiveStorm) -> str:
    """The storm's headline name for prose: "Hurricane Elena (Category 3)",
    "Tropical Storm Bret", "Potential Tropical Cyclone Four"."""
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
    title = titles.get(storm.kind, "Cyclone")
    head = f"{title} {storm.name}"
    return f"{head} (Category {cat})" if cat else head


def _summarize(
    hit: GeoHit, storm: ActiveStorm, distance_mi: int, bearing: str, active_count: int
) -> str:
    """A concise observation so the model can answer in prose even though the card
    carries the detail. Names the nearest storm, where it is relative to the place,
    its strength and motion, then notes the card is shown and that watches/warnings
    are not included (the model should defer to official advisories for those)."""
    others = ""
    if active_count > 1:
        n = active_count - 1
        others = f" ({n} other active {'storm' if n == 1 else 'storms'} elsewhere.)"
    move = movement(storm)
    motion = f"moving {move}" if move != "stationary" else "nearly stationary"
    return (
        f"Nearest active tropical cyclone to {hit.name}: {_label(storm)}, "
        f"about {distance_mi} mi {bearing} and {motion}. Max sustained winds "
        f"{sustained_mph(storm)} mph, pressure {storm.pressure_mb} mb.{others} "
        "The app is showing a hurricane card with the storm's vitals, distance, and "
        "bearing. This covers position and intensity only — it does NOT include "
        "watches, warnings, or local surge/rain/wind timing, so for those defer to "
        "official NWS/NHC advisories rather than inferring them."
    )
