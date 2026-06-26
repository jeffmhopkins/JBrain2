"""jerv's `weather` tool (docs/ASSISTANT.md "Agent selection", DESIGN.md
"weather_card tool-view").

A jerv-only `web`-class tool that replaces the multi-step web-search-and-scrape
weather flow with one call returning a concise summary AND a data-only
`weather_card` view (current conditions + an hourly strip). Thin over WeatherClient;
the location firewall lives here — a named place is geocoded by name, but the owner's
"here" fix is resolved to a nearest-city NAME on-box (the offline geocoder) before any
coordinate is forecast, so the owner's precise position never leaves the box.
"""

from __future__ import annotations

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.citygeocode import CityGeocoder
from jbrain.web.weather import GeoHit, Weather, WeatherClient, WeatherError

_NO_LOCATION = (
    'I need a place to check — name a city (e.g. "weather in Austin"), or share your '
    "location and I'll use the nearest city."
)
_SUMMARY_HOURS = 6  # how many hours the model-facing text spells out inline


def build_weather_handlers(
    client: WeatherClient, city_geocoder: CityGeocoder
) -> dict[str, ToolHandler]:
    async def weather_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("location", "")).strip()
        try:
            hit = await _resolve(client, city_geocoder, name, ctx)
        except WeatherError as exc:
            return str(exc)
        if hit is None:
            if name:
                return f'I couldn\'t find a place called "{name}".'
            return _NO_LOCATION
        try:
            weather = await client.forecast(hit)
        except WeatherError as exc:
            return str(exc)
        return ToolOutput(_summarize(weather), view=weather_view(weather))

    return {"weather": weather_tool}


async def _resolve(
    client: WeatherClient, city_geocoder: CityGeocoder, name: str, ctx: ToolContext
) -> GeoHit | None:
    """Turn the request into a place to forecast. A named place geocodes directly; an
    empty location uses the owner's fix, resolved to a city NAME on-box first so only a
    public place name — never the precise fix — is sent off-box (the location firewall)."""
    if name:
        return await client.geocode(name)
    if ctx.here is None:
        return None
    lat, lon = ctx.here
    city = city_geocoder.nearest(lat, lon)
    if city is None:
        # No populated place near the fix (open ocean / remote) — we won't forecast the
        # raw coordinate, since that would send the precise position off-box.
        raise WeatherError("I couldn't pin a nearby city to check the weather for.")
    # Geocode the bare city name (Open-Meteo's search matches a single place token, not
    # a "City, Region, Country" line) — the public name only, never the precise fix.
    return await client.geocode(city.name)


def weather_view(w: Weather) -> ViewPayload:
    """The data-only twin of the summary: a `weather_card` view the app renders inline.
    No URLs, no markup (#9); `cond`/`is_day` are enums/flags the component maps to a
    glyph + token, never colors (DESIGN.md "Agent tool views")."""
    return ViewPayload(
        view="weather_card",
        surface="inline",
        data={
            "place": w.place,
            "as_of": w.as_of,
            "tz": w.tz_abbr,
            "now": {
                "temp_f": w.temp_f,
                "feels_f": w.feels_f,
                "cond": w.cond,
                "is_day": w.is_day,
                "label": w.label,
                "humidity": w.humidity,
                "wind_mph": w.wind_mph,
                "wind_dir": w.wind_dir,
            },
            "hi_f": w.hi_f,
            "lo_f": w.lo_f,
            "hours": [
                {
                    "label": h.label,
                    "temp_f": h.temp_f,
                    "feels_f": h.feels_f,
                    "cond": h.cond,
                    "is_day": h.is_day,
                    "pop": h.pop,
                    "wind_mph": h.wind_mph,
                    "wind_dir": h.wind_dir,
                }
                for h in w.hours
            ],
        },
    )


def _summarize(w: Weather) -> str:
    """A concise text observation so the model can answer in prose even though the card
    carries the detail; spells out the next few hours, then notes the card is shown."""
    head = (
        f"{w.place} — now {w.temp_f}°F (feels {w.feels_f}°), {w.label.lower()}, "
        f"{w.wind_dir} {w.wind_mph} mph, humidity {w.humidity}%. "
        f"Today: high {w.hi_f}°, low {w.lo_f}°."
    )
    parts = []
    for h in w.hours[1 : _SUMMARY_HOURS + 1]:
        rain = f", {h.pop}% rain" if h.pop else ""
        parts.append(f"{h.label} {h.temp_f}° (feels {h.feels_f}°{rain})")
    trend = f" Next hours: {'; '.join(parts)}." if parts else ""
    return f"{head}{trend} The app is showing the hourly forecast."
