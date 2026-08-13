"""jerv's `weather` tool (docs/reference/ASSISTANT.md "Agent selection", DESIGN.md
"weather_card tool-view").

A jerv-only `web`-class tool that replaces the multi-step web-search-and-scrape
weather flow with one call returning a concise summary AND a data-only
`weather_card` view (current conditions + an hourly strip). Thin over WeatherClient;
the location firewall lives here — a named place is geocoded by name, but the owner's
"here" fix is resolved to a nearest-city NAME on-box (the offline geocoder) before any
coordinate is forecast, so the owner's precise position never leaves the box.
"""

from __future__ import annotations

import asyncio

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.citygeocode import CityGeocoder
from jbrain.web.nws import NwsClient, NwsOutOfCoverage, NwsUnavailable, WeatherAlert
from jbrain.web.weather import GeoHit, Weather, WeatherClient, WeatherError

_NO_LOCATION = (
    'I need a place to check — name a city (e.g. "weather in Austin"), or share your '
    "location and I'll use the nearest city."
)
_SUMMARY_HOURS = 6  # how many hours the today text spells out inline
_SUMMARY_DAYS = 7  # how many days the week text spells out inline
# When the day's peak feels-like runs this far above the air high (or the coldest
# feels-like this far below the low), the divergence is worth a line so the model can
# answer "how hot/cold will it feel" — the gap a raw high/low hides.
_FEELS_GAP_F = 4
# Ranking for the single alert the banner shows when a place has several active: a
# warning outranks a watch outranks an advisory, and within a tone NWS's own severity
# breaks the tie. NWS-sourced — the only legitimate watch/warning surface.
_ALERT_TONE_RANK = {"warning": 3, "watch": 2, "advisory": 1}
_ALERT_SEVERITY_RANK = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1, "unknown": 0}
# What `range` values mean "the week ahead"; anything else is the default single day.
_WEEK_WORDS = frozenset({"week", "weekly", "7day", "7-day", "this week", "next week"})


def build_weather_handlers(
    client: WeatherClient, city_geocoder: CityGeocoder, nws_client: NwsClient
) -> dict[str, ToolHandler]:
    async def weather_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("location", "")).strip()
        weekly = str(arguments.get("range", "today")).strip().lower() in _WEEK_WORDS
        try:
            hit = await _resolve(client, city_geocoder, name, ctx)
        except WeatherError as exc:
            return str(exc)
        if hit is None:
            if name:
                return f'I couldn\'t find a place called "{name}".'
            return _NO_LOCATION
        # The forecast (required) and the official NWS alerts for the city centre
        # (best-effort, US-only) fetch concurrently — a heat/cold advisory is exactly the
        # thing the raw high/low hides, but a missing or out-of-coverage alert feed must
        # never fail the forecast. `_active_alerts` swallows its own errors → always a tuple.
        forecast_r, alerts = await asyncio.gather(
            client.forecast(hit, weekly=weekly),
            _active_alerts(nws_client, hit),
            return_exceptions=True,
        )
        if isinstance(forecast_r, WeatherError):
            return str(forecast_r)
        if isinstance(forecast_r, BaseException):
            raise forecast_r
        alert = _alert_slot(alerts if isinstance(alerts, tuple) else ())
        return ToolOutput(_summarize(forecast_r, alert), view=weather_view(forecast_r, alert))

    return {"weather": weather_tool}


async def _active_alerts(nws: NwsClient, hit: GeoHit) -> tuple[WeatherAlert, ...]:
    """Official NWS alerts for the city centre, best-effort: an unconfigured source, a
    point outside NWS coverage (non-US), or a transient failure all yield no banner — the
    forecast still renders. Never raises, so it composes cleanly under `asyncio.gather`."""
    if not nws.configured:
        return ()
    try:
        return await nws.weather_alerts(hit.latitude, hit.longitude)
    except (NwsOutOfCoverage, NwsUnavailable):
        return ()


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


def _alert_slot(alerts: tuple[WeatherAlert, ...]) -> dict | None:
    """The single most urgent NWS alert for the place (warning > watch > advisory, ties
    broken by NWS severity), plus a count of any others, or None. NWS-sourced text only —
    the banner is the card's one official watch/warning surface, never a computed one."""
    if not alerts:
        return None
    top = max(
        alerts,
        key=lambda a: (
            _ALERT_TONE_RANK.get(a.tone, 0),
            _ALERT_SEVERITY_RANK.get(a.severity, 0),
        ),
    )
    return {
        "event": top.event,
        "tone": top.tone,
        "headline": top.headline,
        "more": len(alerts) - 1,  # how many other alerts are also active
    }


def weather_view(w: Weather, alert: dict | None = None) -> ViewPayload:
    """The data-only twin of the summary: a `weather_card` view the app renders inline.
    No URLs, no markup (#9); `cond`/`is_day` are enums/flags the component maps to a
    glyph + token, never colors (DESIGN.md "Agent tool views"). `alert` is the governing
    NWS watch/warning/advisory for the place (or None) — the banner's only source."""
    return ViewPayload(
        view="weather_card",
        surface="inline",
        data={
            "place": w.place,
            "as_of": w.as_of,
            "tz": w.tz_abbr,
            "range": w.kind,
            "alert": alert,
            "now": {
                "temp_f": w.temp_f,
                "feels_f": w.feels_f,
                "feels_hi_f": w.feels_hi_f,
                "feels_lo_f": w.feels_lo_f,
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
            "days": [
                {
                    "label": d.label,
                    "cond": d.cond,
                    "hi_f": d.hi_f,
                    "lo_f": d.lo_f,
                    "feels_hi_f": d.feels_hi_f,
                    "feels_lo_f": d.feels_lo_f,
                    "pop": d.pop,
                    "wind_mph": d.wind_mph,
                    "wind_dir": d.wind_dir,
                }
                for d in w.days
            ],
        },
    )


def _heat_note(w: Weather) -> str:
    """A feels-like divergence line for the summary: the day's peak heat index when it
    runs well above the air high (the NWS "feels like" a Heat Advisory turns on), or the
    lowest apparent temp when it dips well below the low. The raw high/low alone reads mild
    on a day the afternoon heat index tops 110°. Empty when feels tracks the air temp."""
    if w.feels_hi_f - w.hi_f >= _FEELS_GAP_F:
        return f" Heat index peaks near {w.feels_hi_f}° this afternoon."
    if w.lo_f - w.feels_lo_f >= _FEELS_GAP_F:
        return f" Feels as cool as {w.feels_lo_f}° at its coolest."
    return ""


def _summarize(w: Weather, alert: dict | None = None) -> str:
    """A concise text observation so the model can answer in prose even though the card
    carries the detail; spells out the next few hours (today) or days (week), then notes
    the card is shown. Leads with any OFFICIAL NWS alert — the card banners it, and it is
    exactly the thing (a Heat Advisory, a Wind Chill Warning) the raw numbers understate."""
    alert_note = ""
    if alert is not None:
        others = alert.get("more") or 0
        extra = f" (+{others} more active)" if others else ""
        alert_note = f" Official NWS alert: {alert['event']}{extra}."
    head = (
        f"{w.place} — now {w.temp_f}°F (feels {w.feels_f}°), {w.label.lower()}, "
        f"{w.wind_dir} {w.wind_mph} mph, humidity {w.humidity}%. "
        f"Today: high {w.hi_f}°, low {w.lo_f}°.{_heat_note(w)}{alert_note}"
    )
    if w.kind == "week":
        parts = []
        for d in w.days[:_SUMMARY_DAYS]:
            rain = f", {d.pop}% rain" if d.pop else ""
            parts.append(f"{d.label} {d.hi_f}°/{d.lo_f}° {d.cond}{rain}")
        trend = f" This week: {'; '.join(parts)}." if parts else ""
        return f"{head}{trend} The app is showing the 7-day forecast."
    parts = []
    for h in w.hours[1 : _SUMMARY_HOURS + 1]:
        rain = f", {h.pop}% rain" if h.pop else ""
        parts.append(f"{h.label} {h.temp_f}° (feels {h.feels_f}°{rain})")
    trend = f" Next hours: {'; '.join(parts)}." if parts else ""
    return f"{head}{trend} The app is showing the hourly forecast."
