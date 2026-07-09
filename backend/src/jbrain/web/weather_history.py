"""Historical weather via the Open-Meteo **Archive** API (docs/reference/ASSISTANT.md
"Agent selection").

The forecast `weather` tool only reaches out ~7 days; a "what was July like the last
five summers" question needs the archive, and — for heat index — a *computation*, not a
lookup: per-year heat index is published nowhere, it must be derived from the hourly
temperature + humidity series and aggregated. So this client fetches the hourly archive
for a date range, computes the NWS heat index per hour on-box, and returns the finished
aggregates the model reads back.

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

from jbrain.web.weather import GeoHit, WeatherError

log = structlog.get_logger()

_TIMEOUT = 30.0  # a month of hourly archive is a larger body than a forecast


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
    """Aggregates for one date range at one place, computed from the hourly archive.
    `avg_high_hi_f` — the mean of each day's PEAK heat index — is the headline
    "feels like" figure a climatology page reports; `avg_hi_f` averages all hours (so
    night lulls pull it down), and `peak_hi_f` is the single hottest hour in the range."""

    place: str
    start: date
    end: date
    days: int
    avg_temp_f: float
    avg_high_f: float
    avg_low_f: float
    avg_humidity: int
    avg_hi_f: float
    avg_high_hi_f: float
    peak_hi_f: float
    danger_days: int  # days whose peak heat index reached the NWS "Danger" band


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
        """Fetch the hourly temperature + humidity and daily high/low for the range, then
        compute the heat-index aggregates. Defensive: a missing/ragged block is a
        malformed body, surfaced as a WeatherError rather than a crash."""
        if not self._archive_url:
            raise WeatherError("historical weather is not configured on this instance")
        params = {
            "latitude": f"{hit.latitude:.4f}",
            "longitude": f"{hit.longitude:.4f}",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": "temperature_2m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
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


def _reduce(place: str, start: date, end: date, body: dict) -> HistoryStats:
    """Turn Open-Meteo's column arrays into a HistoryStats: mean temp/humidity, the daily
    high/low means, and the heat-index aggregates (all-hours mean, mean daily peak, and
    single peak) computed per hour and grouped by calendar day."""
    hourly = body.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherError("the weather service returned an incomplete history")
    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    hums = hourly.get("relative_humidity_2m")
    if not (isinstance(times, list) and isinstance(temps, list) and isinstance(hums, list)):
        raise WeatherError("the weather service returned an incomplete history")

    temp_sum = hum_sum = hi_sum = 0.0
    n = 0
    peak_hi = float("-inf")
    day_peak: dict[str, float] = {}  # calendar day → its hottest heat-index hour
    for t, temp, rh in zip(times, temps, hums, strict=False):
        if temp is None or rh is None:
            continue
        temp = float(temp)
        rh = float(rh)
        hi = heat_index_f(temp, rh)
        temp_sum += temp
        hum_sum += rh
        hi_sum += hi
        n += 1
        peak_hi = max(peak_hi, hi)
        day = str(t)[:10]
        day_peak[day] = max(day_peak.get(day, float("-inf")), hi)
    if n == 0:
        raise WeatherError("the weather service returned no usable history for that range")

    avg_high_f, avg_low_f = _daily_hilo(body.get("daily"))
    peaks = list(day_peak.values())
    return HistoryStats(
        place=place,
        start=start,
        end=end,
        days=len(day_peak),
        avg_temp_f=temp_sum / n,
        avg_high_f=avg_high_f,
        avg_low_f=avg_low_f,
        avg_humidity=round(hum_sum / n),
        avg_hi_f=hi_sum / n,
        avg_high_hi_f=sum(peaks) / len(peaks),
        peak_hi_f=peak_hi,
        danger_days=sum(1 for p in peaks if p >= _DANGER_HI),
    )


def _daily_hilo(daily: object) -> tuple[float, float]:
    """Mean of the daily max and min temperatures, or (nan, nan) if the block is absent —
    the hourly aggregates stand on their own, so a missing daily block is not fatal."""
    if not isinstance(daily, dict):
        return float("nan"), float("nan")
    highs = [v for v in (daily.get("temperature_2m_max") or []) if v is not None]
    lows = [v for v in (daily.get("temperature_2m_min") or []) if v is not None]
    hi = sum(float(v) for v in highs) / len(highs) if highs else float("nan")
    lo = sum(float(v) for v in lows) / len(lows) if lows else float("nan")
    return hi, lo


def parse_iso_date(raw: str) -> date | None:
    """Parse a YYYY-MM-DD date, or None if it isn't one (a full timestamp is tolerated —
    only the date part is used)."""
    try:
        return datetime.fromisoformat(str(raw).strip()).date()
    except (TypeError, ValueError):
        return None
