"""Historical weather via the Open-Meteo **Archive** API (docs/reference/ASSISTANT.md
"Agent selection").

The forecast `weather` tool only reaches out ~7 days; a "what was July like the last
five summers" question needs the archive, and — for heat index — a *computation*, not a
lookup: per-year heat index is published nowhere, it must be derived from the hourly
temperature + humidity series and aggregated. So this client fetches the hourly + daily
archive for a date range, computes the NWS heat index per hour on-box, and reduces the
whole record to a broad aggregate the model reads back — temperature, humidity, dew
point, heat index, precipitation, wind, cloud, sunshine, and pressure.

Like the forecast weather tool it runs DIRECTLY over one pinned, config-supplied
upstream (never model-supplied) — the bounded jerv-sandbox exception to invariant #9.
The location firewall is identical: a named place is forward-geocoded by name (reusing
the forecast client's geocoder), and the owner's "here" fix is resolved to a nearest-city
NAME on-box before any coordinate is sent, so only a public city centre reaches the API.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import httpx
import structlog

from jbrain.web.weather import GeoHit, WeatherError, _compass

log = structlog.get_logger()

_TIMEOUT = 30.0  # a year of hourly archive across many variables is a large body

# The hourly variables pulled from the archive — everything the aggregate reports that is
# only available (or best computed) at hourly resolution. Temperature + humidity also feed
# the on-box heat-index computation.
_HOURLY = (
    "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,"
    "wind_speed_10m,wind_gusts_10m,cloud_cover,surface_pressure"
)
# The daily variables — the published per-day roll-ups (temperature extremes, precip and
# snow totals, wind maxima + dominant direction, and sunshine) that are cleaner taken
# straight from Open-Meteo than re-derived from the hourly series.
_DAILY = (
    "temperature_2m_max,temperature_2m_min,precipitation_sum,rain_sum,snowfall_sum,"
    "precipitation_hours,wind_speed_10m_max,wind_gusts_10m_max,"
    "wind_direction_10m_dominant,sunshine_duration"
)

_RAINY_DAY_IN = 0.01  # a day counts as "rainy" at or above this precipitation total (in)


def heat_index_f(temp_f: float, rh: float) -> float:
    """The NWS heat index ("feels like" from heat + humidity) in °F, from an air
    temperature (°F) and relative humidity (%). Below ~80 °F apparent, the Steadman
    average applies; at or above it the Rothfusz regression with the NWS low- and
    high-humidity adjustments — the same math the National Weather Service publishes."""
    # Steadman's simpler form first; only escalate to Rothfusz when it lands in-range.
    simple = 0.5 * (temp_f + 61.0 + (temp_f - 68.0) * 1.2 + rh * 0.094)
    if (simple + temp_f) / 2 < 80:
        return simple
    hi = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 6.83783e-3 * temp_f * temp_f
        - 5.481717e-2 * rh * rh
        + 1.22874e-3 * temp_f * temp_f * rh
        + 8.5282e-4 * temp_f * rh * rh
        - 1.99e-6 * temp_f * temp_f * rh * rh
    )
    if rh < 13 and 80 <= temp_f <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(temp_f - 95.0)) / 17)
    elif rh > 85 and 80 <= temp_f <= 87:
        hi += ((rh - 85) / 10) * ((87 - temp_f) / 5)
    return hi


# The NWS heat-index caution bands (°F), used to count how many days reached a stress
# level — the concrete "how bad was it" number behind the averages.
_DANGER_HI = 103  # NWS "Danger": heat cramps/exhaustion likely, heat stroke possible


@dataclass(frozen=True)
class HistoryStats:
    """Aggregates for one date range at one place, computed from the hourly + daily
    archive. Every optional dimension is `nan`/`None` when the archive didn't return it,
    so the summary can omit what's missing rather than print a placeholder.

    `avg_high_hi_f` — the mean of each day's PEAK heat index — is the headline "feels
    like" figure a climatology page reports; `avg_hi_f` averages all hours (so night
    lulls pull it down), and `peak_hi_f` is the single hottest hour in the range."""

    place: str
    start: date
    end: date
    days: int
    # Temperature (°F)
    avg_temp_f: float
    avg_high_f: float
    avg_low_f: float
    max_temp_f: float
    min_temp_f: float
    # Moisture
    avg_humidity: int
    avg_dew_point_f: float
    # Heat index (°F)
    avg_hi_f: float
    avg_high_hi_f: float
    peak_hi_f: float
    danger_days: int
    # Precipitation (inches) + snow
    total_precip_in: float
    rainy_days: int
    max_daily_precip_in: float
    total_snow_in: float
    # Wind (mph) + dominant direction
    avg_wind_mph: float
    max_gust_mph: float
    wind_dir: str | None
    # Sky
    avg_cloud_cover: int
    avg_sunshine_hours: float  # per day
    # Pressure (hPa / mb)
    avg_pressure_mb: float


class WeatherHistoryClient:
    """Fetch the hourly + daily archive for a place/date-range from Open-Meteo's Archive
    API and reduce it to a `HistoryStats`. The base URL is config-pinned; `transport` is
    injectable so tests run against a MockTransport with no network."""

    def __init__(self, archive_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._archive_url = archive_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._archive_url)

    async def archive(self, hit: GeoHit, start: date, end: date) -> HistoryStats:
        """Fetch the hourly + daily record for the range and reduce it to the full
        aggregate. Defensive: a missing/ragged block is a malformed body, surfaced as a
        WeatherError rather than a crash."""
        if not self._archive_url:
            raise WeatherError("historical weather is not configured on this instance")
        params = {
            "latitude": f"{hit.latitude:.4f}",
            "longitude": f"{hit.longitude:.4f}",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": _HOURLY,
            "daily": _DAILY,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
        }
        body = await self._get(f"{self._archive_url}/v1/archive", params)
        if not isinstance(body, dict):
            raise WeatherError("the weather service returned an unexpected response")
        return _reduce(hit.name, start, end, body)

    async def _get(self, url: str, params: dict) -> object:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning("web.weather_history_failed", status=exc.response.status_code)
            raise WeatherError("the weather service is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.weather_history_failed", error=repr(exc))
            raise WeatherError("the weather service is unavailable right now") from exc


def _nums(seq: object) -> list[float]:
    """The numeric values of a JSON column, dropping nulls and non-numbers."""
    if not isinstance(seq, list):
        return []
    out: list[float] = []
    for v in seq:
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _avg(seq: object) -> float:
    """Mean of a column's numeric values, or nan when the column is empty/absent."""
    xs = _nums(seq)
    return sum(xs) / len(xs) if xs else float("nan")


def _reduce(place: str, start: date, end: date, body: dict) -> HistoryStats:
    """Turn Open-Meteo's column arrays into a HistoryStats. The heat index is computed per
    hour from paired temperature + humidity and grouped by calendar day; the other
    dimensions are simple column reductions (hourly means / daily sums), each defensive so
    an absent variable becomes nan/None rather than a crash."""
    hourly = body.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherError("the weather service returned an incomplete history")
    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    hums = hourly.get("relative_humidity_2m")
    if not (isinstance(times, list) and isinstance(temps, list) and isinstance(hums, list)):
        raise WeatherError("the weather service returned an incomplete history")

    # Heat index + temperature extremes: one pass over paired hourly temp/humidity, with
    # the per-day peak heat index accumulated for the "average daily peak" headline figure.
    temp_sum = hi_sum = 0.0
    n = 0
    peak_hi = max_temp = float("-inf")
    min_temp = float("inf")
    day_peak: dict[str, float] = {}
    for t, temp, rh in zip(times, temps, hums, strict=False):
        if temp is None or rh is None:
            continue
        temp = float(temp)
        rh = float(rh)
        hi = heat_index_f(temp, rh)
        temp_sum += temp
        hi_sum += hi
        n += 1
        peak_hi = max(peak_hi, hi)
        max_temp = max(max_temp, temp)
        min_temp = min(min_temp, temp)
        day = str(t)[:10]
        day_peak[day] = max(day_peak.get(day, float("-inf")), hi)
    if n == 0:
        raise WeatherError("the weather service returned no usable history for that range")

    daily_raw = body.get("daily")
    daily: dict = daily_raw if isinstance(daily_raw, dict) else {}
    avg_high_f, avg_low_f = _daily_hilo(daily)
    peaks = list(day_peak.values())

    # Precipitation / snow: prefer the published daily sums (cleaner than re-summing hours).
    precip_days = _nums(daily.get("precipitation_sum"))
    snow_days = _nums(daily.get("snowfall_sum"))

    return HistoryStats(
        place=place,
        start=start,
        end=end,
        days=len(day_peak),
        avg_temp_f=temp_sum / n,
        avg_high_f=avg_high_f,
        avg_low_f=avg_low_f,
        max_temp_f=max_temp,
        min_temp_f=min_temp,
        avg_humidity=round(_avg(hums)) if not math.isnan(_avg(hums)) else 0,
        avg_dew_point_f=_avg(hourly.get("dew_point_2m")),
        avg_hi_f=hi_sum / n,
        avg_high_hi_f=sum(peaks) / len(peaks),
        peak_hi_f=peak_hi,
        danger_days=sum(1 for p in peaks if p >= _DANGER_HI),
        total_precip_in=sum(precip_days) if precip_days else float("nan"),
        rainy_days=sum(1 for p in precip_days if p >= _RAINY_DAY_IN),
        max_daily_precip_in=max(precip_days) if precip_days else float("nan"),
        total_snow_in=sum(snow_days) if snow_days else 0.0,
        avg_wind_mph=_avg(hourly.get("wind_speed_10m")),
        max_gust_mph=_max(daily.get("wind_gusts_10m_max"), hourly.get("wind_gusts_10m")),
        wind_dir=_dominant_dir(daily.get("wind_direction_10m_dominant")),
        avg_cloud_cover=round(_avg(hourly.get("cloud_cover")))
        if not math.isnan(_avg(hourly.get("cloud_cover")))
        else 0,
        avg_sunshine_hours=_avg_sunshine(daily.get("sunshine_duration")),
        avg_pressure_mb=_avg(hourly.get("surface_pressure")),
    )


def _daily_hilo(daily: dict) -> tuple[float, float]:
    """Mean of the daily max and min temperatures, or nan when absent — the hourly
    aggregates stand on their own, so a missing daily block is not fatal."""
    highs = _nums(daily.get("temperature_2m_max"))
    lows = _nums(daily.get("temperature_2m_min"))
    hi = sum(highs) / len(highs) if highs else float("nan")
    lo = sum(lows) / len(lows) if lows else float("nan")
    return hi, lo


def _max(*cols: object) -> float:
    """The largest value across one or more columns (e.g. daily gust max, falling back to
    hourly gusts), or nan when none carry a number."""
    vals = [v for col in cols for v in _nums(col)]
    return max(vals) if vals else float("nan")


def _dominant_dir(col: object) -> str | None:
    """The prevailing wind direction as a compass abbreviation, from the circular mean of
    the daily dominant-direction series, or None when absent."""
    degs = _nums(col)
    if not degs:
        return None
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if x == 0 and y == 0:
        return None
    return _compass(math.degrees(math.atan2(y, x)) % 360)


def _avg_sunshine(col: object) -> float:
    """Average daily sunshine in HOURS (the archive reports it per day in seconds), or nan
    when absent."""
    secs = _nums(col)
    return (sum(secs) / len(secs)) / 3600 if secs else float("nan")


def parse_iso_date(raw: str) -> date | None:
    """Parse a YYYY-MM-DD date, or None if it isn't one (a full timestamp is tolerated —
    only the date part is used)."""
    try:
        return datetime.fromisoformat(str(raw).strip()).date()
    except (TypeError, ValueError):
        return None
