"""jerv's `weather_history` tool (docs/reference/ASSISTANT.md "Agent selection").

A jerv-only `web`-class tool: given a place and a past date range, it fetches the
hourly + daily archive, computes the NWS heat index on-box, and returns aggregates across
every dimension the record carries (temperature, humidity, dew point, heat index,
precipitation, wind, sky, pressure) as text. It answers the class of question the forecast
`weather` tool can't (history beyond a week) and web search can't reliably (per-year heat
index is a computation over hourly data, not a published figure).

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
from jbrain.web.weather_history import (
    DayPointHI,
    HistoryStats,
    HourPointHI,
    WeatherHistoryClient,
    parse_iso_date,
)

_NO_LOCATION = (
    'I need a place to check — name a city (e.g. "heat index history for Austin"), or '
    "share your location and I'll use the nearest city."
)
_NOT_CONFIGURED = "Historical weather isn't configured on this instance."
# A single call is bounded to roughly a year so the hourly payload stays sane; a
# multi-year question is answered by calling once per year (the sidecar says so), which
# also keeps each year's aggregate cleanly separated.
_MAX_RANGE_DAYS = 370
# The detail modes and their span caps: an hour-by-hour list is only legible for a few
# days (a month of hours is 744 rows), and a per-day list for at most a season.
_DETAILS = ("summary", "daily", "hourly")
_MAX_HOURLY_DAYS = 7
_MAX_DAILY_DAYS = 92


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
        detail = str(arguments.get("detail", "summary")).strip().lower()
        if detail not in _DETAILS:
            detail = "summary"
        err = _validate_range(start, end, _today(ctx)) or _validate_detail(detail, start, end)
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
            if detail == "hourly":
                return _hourly_table(hit.name, await history.archive_hourly(hit, start, end))
            if detail == "daily":
                return _daily_table(hit.name, await history.archive_daily(hit, start, end))
            return _summarize(await history.archive(hit, start, end))
        except WeatherError as exc:
            return str(exc)

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


def _validate_detail(detail: str, start: date | None, end: date | None) -> str | None:
    """Cap the row-by-row modes so a table stays legible: an hour-by-hour list is only
    useful over a few days, a per-day list over at most a season. Wider spans steer to a
    coarser mode. `summary` has no cap (it's already reduced to one paragraph)."""
    if start is None or end is None:
        return None  # _validate_range already handled the missing-dates case
    span = (end - start).days + 1
    if detail == "hourly" and span > _MAX_HOURLY_DAYS:
        return (
            f"Hour-by-hour detail is capped at {_MAX_HOURLY_DAYS} days (that range is "
            f"{span}). Narrow it to a day or a few days, or use detail='daily' for a "
            "per-day list over a longer span."
        )
    if detail == "daily" and span > _MAX_DAILY_DAYS:
        return (
            f"Per-day detail is capped at about {_MAX_DAILY_DAYS} days (that range is "
            f"{span}). Narrow it, or use the default summary for a longer span."
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


def _hourly_table(place: str, rows: list[HourPointHI]) -> str:
    """The per-hour heat-index table: a header line naming the place and span, then one line
    per hour (time, temp, humidity, heat index). This is the hour-by-hour view the summary
    rolls up — used for a single day or a few days."""
    span = f"{rows[0].time} to {rows[-1].time}"
    lines = [f"{r.time}: {r.temp_f}°F, {r.humidity}% RH → heat index {r.hi_f}°F" for r in rows]
    return (
        f"{place} — hour-by-hour heat index, {span} ({len(rows)} hours). "
        "Each row is the air temperature, relative humidity, and the NWS heat index "
        f"computed from them on-box:\n" + "\n".join(lines)
    )


def _daily_table(place: str, rows: list[DayPointHI]) -> str:
    """The per-day heat-index table: one line per calendar day with the high/low, average
    humidity, and the day's average and peak heat index."""
    span = f"{rows[0].date} to {rows[-1].date}"
    lines = [
        f"{r.date}: high {r.high_f}°F / low {r.low_f}°F, humidity {r.avg_humidity}%, "
        f"heat index avg {r.avg_hi_f}°F / peak {r.peak_hi_f}°F"
        for r in rows
    ]
    return (
        f"{place} — daily heat index, {span} ({len(rows)} days). Heat index is computed "
        f"on-box from the hourly temperature and humidity (NWS formula):\n" + "\n".join(lines)
    )


def _f(value: float, suffix: str) -> str | None:
    """A one-decimal number with a unit suffix, or None when the value is absent (nan) so
    the caller can drop that clause entirely rather than print a placeholder."""
    return None if math.isnan(value) else f"{value:.1f}{suffix}"


def _summarize(s: HistoryStats) -> str:
    """A numbers-first observation for the model to answer from, grouped by dimension.
    Leads with the heat-index figures (the tool's reason to exist), names which average is
    which so the model doesn't conflate them, and states the range so a per-year call is
    self-labeling. Any dimension the archive didn't return is silently omitted."""
    span = f"{s.start.isoformat()} to {s.end.isoformat()}"
    parts: list[str] = [f"{s.place} — {span} ({s.days} days)."]

    parts.append(
        f"Heat index: average across all hours {s.avg_hi_f:.1f}°F, "
        f"average daily peak {s.avg_high_hi_f:.1f}°F, single peak {s.peak_hi_f:.1f}°F."
    )
    if s.danger_days:
        parts.append(
            f'{s.danger_days} of {s.days} days reached the NWS "Danger" heat-index band '
            "(peak ≥103°F)."
        )

    temp = (
        f"Air temperature: average {s.avg_temp_f:.1f}°F, "
        f"average high {_f(s.avg_high_f, '°F')}, average low {_f(s.avg_low_f, '°F')}, "
        f"range {_f(s.min_temp_f, '°F')} to {_f(s.max_temp_f, '°F')}."
    )
    parts.append(temp.replace("None", "n/a"))

    moisture = f"Humidity {s.avg_humidity}% average"
    dew = _f(s.avg_dew_point_f, "°F")
    moisture += f", dew point {dew} average." if dew else " average."
    parts.append(moisture)

    precip = _precip_clause(s)
    if precip:
        parts.append(precip)

    wind = _wind_clause(s)
    if wind:
        parts.append(wind)

    sky = _sky_clause(s)
    if sky:
        parts.append(sky)

    pressure = _f(s.avg_pressure_mb, " hPa")
    if pressure:
        parts.append(f"Average surface pressure {pressure}.")

    parts.append(
        "Heat index is computed on-box from the hourly temperature and humidity (NWS "
        'formula); the average daily peak is the daytime "feels like" figure. Totals '
        "(precipitation, snow) are for the whole range; the rest are averages over it."
    )
    return " ".join(parts)


def _precip_clause(s: HistoryStats) -> str | None:
    total = _f(s.total_precip_in, '"')
    if total is None:
        return None
    clause = f"Precipitation: {total} total over {s.rainy_days} rainy days"
    wettest = _f(s.max_daily_precip_in, '"')
    if wettest:
        clause += f" (wettest day {wettest})"
    if s.total_snow_in:
        clause += f', plus {s.total_snow_in:.1f}" snow'
    return clause + "."


def _wind_clause(s: HistoryStats) -> str | None:
    avg = _f(s.avg_wind_mph, " mph")
    if avg is None:
        return None
    clause = f"Wind: average {avg}"
    gust = _f(s.max_gust_mph, " mph")
    if gust:
        clause += f", peak gust {gust}"
    if s.wind_dir:
        clause += f", prevailing from the {s.wind_dir}"
    return clause + "."


def _sky_clause(s: HistoryStats) -> str | None:
    sun = _f(s.avg_sunshine_hours, " h")
    cloud = None if s.avg_cloud_cover == 0 else f"{s.avg_cloud_cover}% average cloud cover"
    bits = [b for b in (f"{sun}/day sunshine" if sun else None, cloud) if b]
    return "Sky: " + ", ".join(bits) + "." if bits else None
