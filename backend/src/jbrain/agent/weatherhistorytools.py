"""jerv's `weather_history` tool (docs/reference/ASSISTANT.md "Agent selection").

A jerv-only `web`-class tool: given a place and a past date range, it fetches the
hourly archive, computes the NWS heat index on-box, and returns the aggregated
temperature / humidity / heat-index numbers as text. It answers the class of question
the forecast `weather` tool can't (history beyond a week) and web search can't reliably
(per-year heat index is a computation over hourly data, not a published figure).

Geocoding is reused from the forecast weather tool so the location firewall holds
identically — a named place geocodes by name; the owner's "here" fix is resolved to a
nearest-city NAME on-box before any coordinate is sent, so only a public city centre
reaches the archive API.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

from jbrain.agent.clock import _resolve as _resolve_zone
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.citygeocode import CityGeocoder
from jbrain.web.weather import GeoHit, WeatherClient, WeatherError
from jbrain.web.weather_history import HistoryStats, WeatherHistoryClient, parse_iso_date

_NO_LOCATION = (
    'I need a place to check — name a city (e.g. "heat index history for Austin"), or '
    "share your location and I'll use the nearest city."
)
_NOT_CONFIGURED = "Historical weather isn't configured on this instance."
# A single call is bounded to roughly a year so the hourly payload stays sane; a
# multi-year question is answered by calling once per year (the sidecar says so), which
# also keeps each year's aggregate cleanly separated.
_MAX_RANGE_DAYS = 370


def build_weather_history_handlers(
    history: WeatherHistoryClient,
    weather_client: WeatherClient,
    city_geocoder: CityGeocoder,
) -> dict[str, ToolHandler]:
    async def weather_history_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if not history.configured:
            return _NOT_CONFIGURED
        start = parse_iso_date(arguments.get("start_date", ""))
        end = parse_iso_date(arguments.get("end_date", ""))
        err = _validate_range(start, end, _today(ctx))
        if err is not None:
            return err
        assert start is not None and end is not None  # _validate_range guarantees it
        name = str(arguments.get("location", "")).strip()
        try:
            hit = await _resolve(weather_client, city_geocoder, name, ctx)
        except WeatherError as exc:
            return str(exc)
        if hit is None:
            return f'I couldn\'t find a place called "{name}".' if name else _NO_LOCATION
        try:
            stats = await history.archive(hit, start, end)
        except WeatherError as exc:
            return str(exc)
        return _summarize(stats)

    return {"weather_history": weather_history_tool}


def _today(ctx: ToolContext) -> date:
    """Today's date in the owner's display timezone — the boundary a history range must
    stay behind. Read from the clock the same way `current_time` does (no owner data)."""
    zone, _ = _resolve_zone(ctx.timezone)
    return datetime.now(UTC).astimezone(zone).date()


def _validate_range(start: date | None, end: date | None, today: date) -> str | None:
    """Reject a range the archive can't serve, as a recoverable observation the model can
    fix on the next turn: missing/unparseable dates, reversed order, a window wider than a
    year (call once per year instead), or a range that runs into the future (the archive is
    history only)."""
    if start is None or end is None:
        return (
            "I need both start_date and end_date as calendar dates (YYYY-MM-DD), e.g. "
            "2023-07-01 to 2023-07-31."
        )
    if end < start:
        return "The end_date is before the start_date — swap them and try again."
    if (end - start).days + 1 > _MAX_RANGE_DAYS:
        return (
            "That range is over a year. Ask for up to a year at a time — for a multi-year "
            "question call once per year (e.g. each July separately) so each year's "
            "averages come back cleanly separated."
        )
    if end >= today:
        return (
            "This is a history tool, so the range must be fully in the past. For the next "
            "few days use the weather forecast instead."
        )
    return None


async def _resolve(
    client: WeatherClient, city_geocoder: CityGeocoder, name: str, ctx: ToolContext
) -> GeoHit | None:
    """Turn the request into a place to look up — the same firewall the forecast weather
    tool uses: a named place geocodes directly; an empty location uses the owner's fix,
    resolved to a city NAME on-box first so only a public place name (never the precise
    fix) is geocoded."""
    if name:
        return await client.geocode(name)
    if ctx.here is None:
        return None
    lat, lon = ctx.here
    city = city_geocoder.nearest(lat, lon)
    if city is None:
        raise WeatherError("I couldn't pin a nearby city to check the history for.")
    return await client.geocode(city.name)


def _t(value: float) -> str:
    """A temperature to one decimal, or "n/a" when the daily block was missing (nan)."""
    return "n/a" if math.isnan(value) else f"{value:.1f}°F"


def _summarize(s: HistoryStats) -> str:
    """A concise, numbers-first observation for the model to answer from. Leads with the
    heat-index figures (the tool's reason to exist), names which average is which so the
    model doesn't conflate them, and states the range so a per-year call is self-labeling."""
    span = f"{s.start.isoformat()} to {s.end.isoformat()}"
    danger = (
        f" {s.danger_days} of {s.days} days reached the NWS \"Danger\" heat-index band "
        f"(peak ≥103°F)."
        if s.danger_days
        else ""
    )
    return (
        f"{s.place} — {span} ({s.days} days). "
        f"Heat index: average across all hours {s.avg_hi_f:.1f}°F, "
        f"average daily peak {s.avg_high_hi_f:.1f}°F, single peak {s.peak_hi_f:.1f}°F. "
        f"Air temperature: average {s.avg_temp_f:.1f}°F, average high {_t(s.avg_high_f)}, "
        f"average low {_t(s.avg_low_f)}. Average relative humidity {s.avg_humidity}%."
        f"{danger} "
        "Heat index is computed on-box from the hourly temperature and humidity (NWS "
        "formula); the average daily peak is the daytime \"feels like\" figure."
    )
