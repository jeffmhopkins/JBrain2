"""Weather lookups via Open-Meteo (docs/reference/ASSISTANT.md "Agent selection", DESIGN.md
"weather_card tool-view").

Like the jerv web tools, this runs DIRECTLY rather than staging an egress Proposal —
the bounded jerv-sandbox exception to invariant #9. Two pinned, config-supplied
upstreams (the geocoding API and the forecast API), never model-supplied; only a
public place name and a coordinate go out, never owner data. The base URLs default
to the public Open-Meteo endpoints (free, no API key); empty disables the tool (the
sidecar still loads and the handler reports "not configured").

The location firewall holds: the weather tool never sends the owner's *precise*
position off-box. A named place is forward-geocoded by name; the owner's "here"
fix is first resolved to a nearest-city NAME on-box (the offline geocoder), and
only that public city name is geocoded — so the coordinate that reaches the
forecast API is a city centre, the same coarseness as naming the city.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0
_HOURS_AHEAD = 24  # the today card's hourly-strip window
_WEEK_DAYS = 7  # the week card's daily-list window (Open-Meteo supports up to 16)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Statuses that mean "try again" rather than "this request is wrong": Open-Meteo's
# rate limit (429) and the transient upstream/gateway 5xx family. A 4xx like 400/404
# is deterministic (bad params, no such place) — retrying it only wastes the window.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRIES = 2  # extra attempts after the first — 3 tries total for a transient blip
_BACKOFF = 0.5  # base seconds; doubled each retry (0.5s, 1.0s)


class WeatherError(RuntimeError):
    """A forecast could not be produced — an upstream was unreachable, returned a
    non-2xx, sent a malformed body, or the place could not be located. Surfaced to
    the agent as a recoverable tool error."""


# WMO weather-interpretation codes → (cond enum, human label). `cond` is the closed
# enum the component maps to a glyph + token (DESIGN.md: enums, never colors). The
# day/night split rides a separate `is_day` flag, so the component owns the night
# variants rather than the model or this table.
_WMO: dict[int, tuple[str, str]] = {
    0: ("clear", "Clear"),
    1: ("clear", "Mainly clear"),
    2: ("partly", "Partly cloudy"),
    3: ("cloudy", "Overcast"),
    45: ("fog", "Fog"),
    48: ("fog", "Freezing fog"),
    51: ("rain", "Light drizzle"),
    53: ("rain", "Drizzle"),
    55: ("rain", "Heavy drizzle"),
    56: ("rain", "Freezing drizzle"),
    57: ("rain", "Freezing drizzle"),
    61: ("rain", "Light rain"),
    63: ("rain", "Rain"),
    65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"),
    67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"),
    73: ("snow", "Snow"),
    75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("rain", "Light showers"),
    81: ("rain", "Showers"),
    82: ("rain", "Violent showers"),
    85: ("snow", "Snow showers"),
    86: ("snow", "Heavy snow showers"),
    95: ("storm", "Thunderstorms"),
    96: ("storm", "Thunderstorms with hail"),
    99: ("storm", "Severe thunderstorms"),
}


def describe_code(code: int) -> tuple[str, str]:
    """Map a WMO code to its (cond, label); unknown codes read as cloudy."""
    return _WMO.get(int(code), ("cloudy", "Cloudy"))


@dataclass(frozen=True)
class GeoHit:
    """A geocoded place: a display name plus the centre coordinate to forecast for.
    `name` is built from the populated place + region/country the geocoder returned."""

    name: str
    latitude: float
    longitude: float


# US state/territory postal abbreviations → full names, so "Cocoa, FL" and
# "San Juan, PR" match the geocoder's spelled-out admin1. The map is only a hint for
# picking among candidates; an unknown qualifier just falls through to the top hit.
_US_STATES = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
    "dc": "district of columbia",
    "pr": "puerto rico",
    "vi": "virgin islands",
    "gu": "guam",
    "as": "american samoa",
    "mp": "northern mariana islands",
}


def _split_place(name: str) -> tuple[str, list[str]]:
    """Split a typed place into (search term, qualifier hints). The first
    comma-separated segment is the name Open-Meteo searches; the rest are region
    hints (a state/province/country, spelled out or a US abbreviation)."""
    segments = [seg.strip() for seg in name.split(",")]
    segments = [seg for seg in segments if seg]
    if not segments:
        return "", []
    return segments[0], segments[1:]


def _pick_row(rows: list[dict], hints: list[str]) -> dict:
    """Choose the candidate best matching the region hints, else the top hit. A hint
    matches when it equals a row's admin1/admin2/country/country_code (case-insensitive),
    with US state abbreviations expanded to full names first."""
    if not hints:
        return rows[0]
    wanted: set[str] = set()
    for hint in hints:
        low = hint.casefold()
        wanted.add(low)
        full = _US_STATES.get(low)
        if full:
            wanted.add(full)
    for row in rows:
        fields = {
            str(row.get(key) or "").casefold()
            for key in ("admin1", "admin2", "country", "country_code")
        }
        if wanted & (fields - {""}):
            return row
    return rows[0]


def _build_hit(row: dict, fallback: str) -> GeoHit | None:
    """Shape a geocoder result row into a GeoHit, or None if it lacks coordinates. The
    display name is the place plus admin1 (state/region) and country to disambiguate."""
    try:
        lat = float(row["latitude"])
        lon = float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    parts = [str(row.get("name") or "").strip()]
    for key in ("admin1", "country"):
        val = str(row.get(key) or "").strip()
        if val and val not in parts:
            parts.append(val)
    place = ", ".join(p for p in parts if p) or fallback
    return GeoHit(name=place, latitude=lat, longitude=lon)


@dataclass(frozen=True)
class HourPoint:
    """One hour of the forecast: the slots the `weather_card` hourly strip renders."""

    label: str  # short local hour, e.g. "2p" / "12a"
    temp_f: int
    feels_f: int
    cond: str
    is_day: bool
    pop: int  # precipitation probability, %
    wind_mph: int
    wind_dir: str  # compass abbreviation, e.g. "SE"


@dataclass(frozen=True)
class DayPoint:
    """One day of the weekly forecast: the slots the `weather_card` daily list renders."""

    label: str  # short weekday, e.g. "Today" / "Mon"
    cond: str
    hi_f: int
    lo_f: int
    pop: int  # max precipitation probability for the day, %
    wind_mph: int
    wind_dir: str


@dataclass(frozen=True)
class Weather:
    """A resolved forecast for one place, shaped for both the model-facing summary
    and the data-only `weather_card` view. `kind` is `today` (current + hourly strip)
    or `week` (current + a daily list); only the matching detail list is populated."""

    place: str
    as_of: str  # short local clock time the forecast was issued, e.g. "1:14 PM"
    tz_abbr: str  # the place's timezone abbreviation, e.g. "EDT"
    kind: str  # "today" | "week"
    temp_f: int
    feels_f: int
    cond: str
    label: str
    is_day: bool
    humidity: int
    wind_mph: int
    wind_dir: str
    hi_f: int
    lo_f: int
    hours: tuple[HourPoint, ...] = ()
    days: tuple[DayPoint, ...] = ()


_COMPASS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def _compass(deg: float) -> str:
    """A wind bearing in degrees → the nearest 16-point compass abbreviation."""
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def _hour_label(dt: datetime) -> str:
    """Local hour as a compact label: 13:00 → "1p", 0:00 → "12a"."""
    h = dt.hour
    suffix = "a" if h < 12 else "p"
    h12 = h % 12 or 12
    return f"{h12}{suffix}"


def _i(value: object) -> int:
    """Round a JSON number to int, defaulting 0 for None/non-numeric."""
    try:
        return round(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class WeatherClient:
    """Forward-geocode a place name and fetch its forecast from Open-Meteo. Base URLs
    are config-pinned; `transport` is injectable so tests run against a MockTransport
    with no network (DEVELOPMENT.md "no network in tests")."""

    def __init__(
        self,
        forecast_url: str,
        geocode_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        retries: int = _RETRIES,
        backoff: float = _BACKOFF,
    ):
        self._forecast_url = forecast_url.rstrip("/")
        self._geocode_url = geocode_url.rstrip("/")
        self._transport = transport
        # A single blip from the free public upstream (a 503, a rate-limit 429, a
        # dropped connection, a slow moment past the timeout) otherwise surfaces
        # straight to the owner as "unavailable" with no second try; bounded
        # retry-with-backoff heals the transient case. `backoff` is injectable so
        # tests exercise the loop without real sleeps.
        self._retries = retries
        self._backoff = backoff

    @property
    def configured(self) -> bool:
        return bool(self._forecast_url and self._geocode_url)

    async def geocode(self, name: str) -> GeoHit | None:
        """Resolve a place name to its centre coordinate, or None if not found.

        Open-Meteo's geocoder matches a BARE place name — it does not parse
        "City, State", so passing "Cocoa, FL" or "Cocoa, Florida" straight
        through returns nothing while "Cocoa" resolves. To accept the natural
        formats a person (or the model) actually types, a comma-qualified
        request is split here: the first segment is the term searched, and the
        trailing segments (a state/region/country — spelled out or a US postal
        abbreviation) disambiguate among the candidates. Without a usable
        qualifier the top hit is returned, as before."""
        if not self._geocode_url:
            raise WeatherError("weather is not configured on this instance")
        query, hints = _split_place(name)
        if not query:
            return None
        # count=10 so a qualifier ("Portland, ME" vs "Portland, OR") has candidates to
        # choose from; a bare name still takes the first (most populous) result.
        params = {"name": query, "count": 10, "language": "en", "format": "json"}
        body = await self._get(f"{self._geocode_url}/v1/search", params)
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return None
        rows = [r for r in rows if isinstance(r, dict)]
        if not rows:
            return None
        return _build_hit(_pick_row(rows, hints), name)

    async def forecast(self, hit: GeoHit, *, weekly: bool = False) -> Weather:
        """Fetch the forecast for a geocoded place. `weekly` swaps the hourly strip for
        a 7-day daily list (today keeps the next-24h hourly detail)."""
        if not self._forecast_url:
            raise WeatherError("weather is not configured on this instance")
        daily = "temperature_2m_max,temperature_2m_min"
        params = {
            "latitude": f"{hit.latitude:.4f}",
            "longitude": f"{hit.longitude:.4f}",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m,wind_direction_10m,is_day",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": _WEEK_DAYS if weekly else 2,
        }
        if weekly:
            # The daily list needs each day's sky, rain chance, and wind; skip the hourly
            # block entirely (a week of hourly is a large payload the daily card never uses).
            params["daily"] = (
                f"{daily},weather_code,precipitation_probability_max,"
                "wind_speed_10m_max,wind_direction_10m_dominant"
            )
        else:
            params["daily"] = daily
            params["hourly"] = (
                "temperature_2m,apparent_temperature,weather_code,"
                "precipitation_probability,wind_speed_10m,wind_direction_10m,is_day"
            )
        body = await self._get(f"{self._forecast_url}/v1/forecast", params)
        if not isinstance(body, dict):
            raise WeatherError("the weather service returned an unexpected response")
        return _shape(hit.name, body, weekly=weekly)

    async def _get(self, url: str, params: dict) -> object:
        """GET a pinned Open-Meteo endpoint, retrying transient failures with backoff.
        GETs are idempotent, so a retry is safe. A retryable status (429/5xx) or a
        transport error (connect/read timeout, dropped connection) gets another try;
        a non-retryable 4xx or a malformed body fails fast — retrying can't fix it."""
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=_TIMEOUT, transport=self._transport
                ) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                log.warning("web.weather_failed", status=exc.response.status_code, error=repr(exc))
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise WeatherError("the weather service is unavailable right now") from exc
                last_exc = exc
            except httpx.TransportError as exc:
                # connect/read timeout, DNS blip, reset connection — the transient family.
                log.warning("web.weather_failed", error=repr(exc))
                last_exc = exc
            except (httpx.HTTPError, ValueError) as exc:
                # a non-transient protocol error or malformed JSON — a retry won't help.
                log.warning("web.weather_failed", error=repr(exc))
                raise WeatherError("the weather service is unavailable right now") from exc
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * (2**attempt))
        raise WeatherError("the weather service is unavailable right now") from last_exc


def _shape(place: str, body: dict, *, weekly: bool = False) -> Weather:
    """Turn Open-Meteo's column-arrays into a Weather. Defensive: a missing block or
    a ragged array is a malformed body, surfaced as a WeatherError, not a crash. The
    current-conditions hero is shared; `weekly` swaps the hourly strip for a daily list."""
    cur = body.get("current")
    daily = body.get("daily")
    if not isinstance(cur, dict):
        raise WeatherError("the weather service returned an incomplete forecast")

    try:
        now = datetime.fromisoformat(str(cur["time"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherError("the weather service returned an incomplete forecast") from exc

    cond, label = describe_code(cur.get("weather_code", 0))
    tz_abbr = str(body.get("timezone_abbreviation") or "").strip()

    hours: tuple[HourPoint, ...] = ()
    days: tuple[DayPoint, ...] = ()
    if weekly:
        days = _days(daily)
    else:
        hourly = body.get("hourly")
        if not isinstance(hourly, dict):
            raise WeatherError("the weather service returned an incomplete forecast")
        times = hourly.get("time")
        if not isinstance(times, list) or not times:
            raise WeatherError("the weather service returned no hourly forecast")
        # The strip starts at the current hour: the first hourly slot at or after "now".
        now_hour = now.replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
        start = next((i for i, t in enumerate(times) if str(t) >= now_hour), 0)
        hours = _hours(hourly, times, start)

    hi, lo = _today_hilo(daily)
    return Weather(
        place=place,
        as_of=_clock(now),
        tz_abbr=tz_abbr,
        kind="week" if weekly else "today",
        temp_f=_i(cur.get("temperature_2m")),
        feels_f=_i(cur.get("apparent_temperature")),
        cond=cond,
        label=label,
        is_day=bool(cur.get("is_day", 1)),
        humidity=_i(cur.get("relative_humidity_2m")),
        wind_mph=_i(cur.get("wind_speed_10m")),
        wind_dir=_compass(float(cur.get("wind_direction_10m") or 0)),
        hi_f=hi if hi is not None else _i(cur.get("temperature_2m")),
        lo_f=lo if lo is not None else _i(cur.get("temperature_2m")),
        hours=hours,
        days=days,
    )


def _days(daily: object) -> tuple[DayPoint, ...]:
    """Parse the daily arrays into the weekly list. The first day reads "Today"; the
    rest are weekday abbreviations from the date."""
    if not isinstance(daily, dict):
        raise WeatherError("the weather service returned no daily forecast")
    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise WeatherError("the weather service returned no daily forecast")
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    code = daily.get("weather_code") or []
    pop = daily.get("precipitation_probability_max") or []
    wspd = daily.get("wind_speed_10m_max") or []
    wdir = daily.get("wind_direction_10m_dominant") or []
    out: list[DayPoint] = []
    for i, raw in enumerate(times):
        try:
            dt = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        cond, _ = describe_code(code[i] if i < len(code) else 0)
        out.append(
            DayPoint(
                label="Today" if i == 0 else _WEEKDAYS[dt.weekday()],
                cond=cond,
                hi_f=_i(highs[i]) if i < len(highs) else 0,
                lo_f=_i(lows[i]) if i < len(lows) else 0,
                pop=_i(pop[i]) if i < len(pop) else 0,
                wind_mph=_i(wspd[i]) if i < len(wspd) else 0,
                wind_dir=_compass(float(wdir[i]) if i < len(wdir) else 0.0),
            )
        )
    if not out:
        raise WeatherError("the weather service returned no usable daily forecast")
    return tuple(out)


def _hours(hourly: dict, times: list, start: int) -> tuple[HourPoint, ...]:
    temp = hourly.get("temperature_2m") or []
    feels = hourly.get("apparent_temperature") or []
    code = hourly.get("weather_code") or []
    pop = hourly.get("precipitation_probability") or []
    wspd = hourly.get("wind_speed_10m") or []
    wdir = hourly.get("wind_direction_10m") or []
    isday = hourly.get("is_day") or []
    out: list[HourPoint] = []
    for i in range(start, min(start + _HOURS_AHEAD, len(times))):
        try:
            dt = datetime.fromisoformat(str(times[i]))
        except (TypeError, ValueError):
            continue
        cond, _ = describe_code(code[i] if i < len(code) else 0)
        out.append(
            HourPoint(
                label=_hour_label(dt),
                temp_f=_i(temp[i]) if i < len(temp) else 0,
                feels_f=_i(feels[i]) if i < len(feels) else 0,
                cond=cond,
                is_day=bool(isday[i]) if i < len(isday) else True,
                pop=_i(pop[i]) if i < len(pop) else 0,
                wind_mph=_i(wspd[i]) if i < len(wspd) else 0,
                wind_dir=_compass(float(wdir[i]) if i < len(wdir) else 0.0),
            )
        )
    if not out:
        raise WeatherError("the weather service returned no usable hourly forecast")
    return tuple(out)


def _today_hilo(daily: object) -> tuple[int | None, int | None]:
    if not isinstance(daily, dict):
        return None, None
    highs = daily.get("temperature_2m_max")
    lows = daily.get("temperature_2m_min")
    hi = _i(highs[0]) if isinstance(highs, list) and highs else None
    lo = _i(lows[0]) if isinstance(lows, list) and lows else None
    return hi, lo


def _clock(dt: datetime) -> str:
    """A 12-hour clock label without a platform-specific strftime, e.g. "1:14 PM"."""
    suffix = "AM" if dt.hour < 12 else "PM"
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d} {suffix}"
