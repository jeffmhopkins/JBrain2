"""Per-location watches/warnings + a wind/rain/arrival timeline via the NWS API
(`api.weather.gov`) — the US-coverage half of the hurricane card (docs/reference/DESIGN.md
"hurricane_card tool-view", docs/archive/HURRICANE_TABS_PLAN.md §1/§3).

Like the weather and hurricane tools this runs DIRECTLY rather than staging an egress
Proposal — the bounded jerv-sandbox exception to invariant #9. One pinned,
config-supplied upstream (the public NWS API, free, no key, no account); the base URL
defaults to `https://api.weather.gov`, empty disables the source (graceful degrade).

The location firewall holds: callers pass the geocoded city centre (the same coarseness
as naming the city, never the owner's precise fix). This module is deliberately
coordinate-blind in its OBSERVABILITY — it never logs the coordinate or the full request
URL, and the typed errors it surfaces to the model carry only a status code, never a
coordinate (HURRICANE_TABS_PLAN.md §5 `[r-S2-sec]`). A definitive 404 from /points or
/alerts means the point is outside NWS coverage and raises NwsOutOfCoverage (the tool
maps it to `coverage:"global"`); a transient 5xx/timeout/transport/bad-JSON raises
NwsUnavailable (the tool keeps `coverage:"us"` with empty slots — a blip must not
relabel a US place as global, §2 `[r-S6]`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0

# NWS requires a descriptive User-Agent (no key, no account). Hardcoded client
# constants, not owner setup (HURRICANE_TABS_PLAN.md §1).
_USER_AGENT = "JBrain2-hurricane (+https://github.com/jeffmhopkins/JBrain2)"
_ACCEPT = "application/geo+json"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}

_KMH_TO_MPH = 0.621371
_MM_TO_IN = 1.0 / 25.4

_TS_FORCE_MPH = 39.0  # tropical-storm-force sustained wind threshold
_HURRICANE_FORCE_MPH = 74.0  # hurricane-force sustained wind threshold

_BUCKET_HOURS = 3  # the timeline strip's 3-hourly downsample
_WINDOW_HOURS = 36  # the ~36h window the strip covers (≈12 cells)

# NWS alert `event` strings → the (level, kind) the card's banner renders. Only
# tropical events are kept; everything else (winter storms, floods, …) is ignored
# here — precedence among the survivors is the tool's job (HURRICANE_TABS_PLAN.md §3).
_TROPICAL_EVENTS: dict[str, tuple[str, str]] = {
    "Hurricane Warning": ("warning", "hurricane"),
    "Hurricane Watch": ("watch", "hurricane"),
    "Tropical Storm Warning": ("warning", "tropical-storm"),
    "Tropical Storm Watch": ("watch", "tropical-storm"),
    "Storm Surge Warning": ("warning", "surge"),
    "Storm Surge Watch": ("watch", "surge"),
    "Extreme Wind Warning": ("warning", "other"),
}


class NwsOutOfCoverage(RuntimeError):
    """The point is outside NWS coverage (a definitive 404 from /points or /alerts).
    The tool maps this to `coverage:"global"` — an NHC-only card. The message carries
    no coordinate (the location firewall, §5)."""


class NwsUnavailable(RuntimeError):
    """A transient NWS failure — 5xx, timeout, transport error, or malformed body. The
    tool keeps `coverage:"us"` with an empty slot (a blip must not relabel a US place
    as global, §2 `[r-S6]`). The message carries no coordinate (the location
    firewall, §5)."""


@dataclass(frozen=True)
class Alert:
    """One official NWS watch/warning at a point, reduced to the closed enums the
    banner renders plus its headline text. `level` is "warning" | "watch"; `kind` is
    "hurricane" | "tropical-storm" | "surge" | "other"."""

    event: str  # the raw NWS event string, e.g. "Hurricane Warning"
    level: str  # "warning" | "watch"
    kind: str  # "hurricane" | "tropical-storm" | "surge" | "other"
    headline: str  # NWS-sourced free text; rendered as text content only, never markup


@dataclass(frozen=True)
class TimelineCell:
    """One 3-hourly bucket of the local impact strip: a place-local hour label plus the
    bucket's sustained wind, gust, and (summed) rain. Wind is mph, rain inches."""

    label: str  # place-local hour, e.g. "9 PM" / "12 AM"
    wind_mph: int
    gust_mph: int
    rain_in: float


@dataclass(frozen=True)
class Timeline:
    """The downsampled local impact strip plus the derived arrival labels. `tz` is the
    place IANA zone the labels render in; the *_force labels are the place-local hour a
    sustained-wind threshold is first crossed, or None if never within the window."""

    cells: tuple[TimelineCell, ...]
    ts_force_label: str | None  # first hour sustained wind ≥ 39 mph, place-local; or None
    hurricane_force_label: str | None  # first hour sustained wind ≥ 74 mph; or None
    tz: str


def _f(value: object) -> float | None:
    """A JSON number → float, or None for null/non-numeric. Distinct from weather's
    `_i`: the timeline math needs the absent-vs-zero distinction (a null value in a
    series is a gap, not a measured zero)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _hour_label(dt: datetime) -> str:
    """A datetime's hour as a compact 12-hour label: 21:00 → "9 PM", 0:00 → "12 AM".
    Mirrors hurricane.py's clock helpers — no platform strftime, no real clock."""
    suffix = "AM" if dt.hour < 12 else "PM"
    h12 = dt.hour % 12 or 12
    return f"{h12} {suffix}"


def _parse_interval(value: object) -> tuple[datetime, int] | None:
    """An NWS gridpoint `validTime` ("2026-09-10T18:00:00+00:00/PT6H") → (UTC start,
    whole hours covered). The duration is always `PTnH` in these series; a missing or
    malformed value, or a sub-hour duration, yields None (the entry is skipped)."""
    s = str(value or "").strip()
    if "/" not in s:
        return None
    when, dur = s.split("/", 1)
    try:
        start = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    start = start.astimezone(UTC)
    hours = _iso_pt_hours(dur)
    if hours <= 0:
        return None
    return start, hours


def _iso_pt_hours(dur: str) -> int:
    """The hour count of an ISO `PTnH` (or `PTnHmM`/`PnD…`) duration, floored to whole
    hours. NWS gridpoint runs are whole-hour `PTnH`; days are folded in defensively so
    a `P1DT…` never silently reads as zero."""
    d = dur.strip().upper()
    if not d.startswith("P"):
        return 0
    days_part, _, time_part = d[1:].partition("T")
    total = 0.0
    total += _iso_field(days_part, "D") * 24.0
    total += _iso_field(time_part, "H")
    total += _iso_field(time_part, "M") / 60.0
    return int(total)


def _iso_field(segment: str, unit: str) -> float:
    """The numeric value preceding `unit` in an ISO-duration segment, or 0.0."""
    idx = segment.find(unit)
    if idx < 0:
        return 0.0
    num = ""
    i = idx - 1
    while i >= 0 and (segment[i].isdigit() or segment[i] == "."):
        num = segment[i] + num
        i -= 1
    try:
        return float(num)
    except ValueError:
        return 0.0


def _expand_series(series: object, *, accumulate: bool) -> dict[datetime, float]:
    """Expand one run-length-encoded gridpoint series into an hourly {UTC hour → value}
    map. Each entry is `{validTime: "start/PTnH", value}`. `windSpeed`/`windGust` are
    INSTANTANEOUS → the value is replicated into each covered hour; precipitation is an
    ACCUMULATION over the interval → the value is divided evenly across the covered
    hours (replicating would multiply rain by the interval length, §1 `[r-S4]`). A
    null value or a missing/absent series contributes nothing — never raises."""
    out: dict[datetime, float] = {}
    if not isinstance(series, dict):
        return out
    values = series.get("values")
    if not isinstance(values, list):
        return out
    for entry in values:
        if not isinstance(entry, dict):
            continue
        parsed = _parse_interval(entry.get("validTime"))
        raw = _f(entry.get("value"))
        if parsed is None or raw is None:
            continue
        start, hours = parsed
        per_hour = raw / hours if accumulate else raw
        for h in range(hours):
            out[start + timedelta(hours=h)] = per_hour
    return out


def _convert(value: float, uom: object, *, kmh_to_mph: bool) -> float:
    """Convert one upstream value to display units by its uom. Wind is SI km/h
    (`wmoUnit:km_h-1`) → mph; precip is mm (`wmoUnit:mm`) → inches. Defensive: an
    already-imperial uom (mph for wind, inch for precip) passes through unscaled, and a
    missing uom is assumed to be the documented SI unit rather than crashing (§1
    `[r-S4]`). Only wind/gust and precip series reach this, so no percent uom occurs."""
    u = str(uom or "").lower()
    if kmh_to_mph:
        if "mph" in u or "mi_h" in u:
            return value
        return value * _KMH_TO_MPH
    if "in" in u and "min" not in u:  # already inches (but not "min")
        return value
    return value * _MM_TO_IN


class NwsClient:
    """Fetch official alerts and the local gridpoint timeline from the NWS API. The base
    URL is config-pinned; `transport` is injectable so tests run against a MockTransport
    with no network (DEVELOPMENT.md "no network in tests")."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base)

    async def alerts(self, lat: float, lon: float) -> tuple[Alert, ...]:
        """Official tropical watches/warnings active at a point. Returns every tropical
        alert NWS lists; precedence among them is the tool's job. A 404 means the point
        is outside coverage (NwsOutOfCoverage); a 5xx/timeout is transient
        (NwsUnavailable)."""
        body = await self._get(f"{self._base}/alerts/active?point={lat},{lon}")
        features = body.get("features") if isinstance(body, dict) else None
        if not isinstance(features, list):
            return ()
        out: list[Alert] = []
        for feature in features:
            alert = _parse_alert(feature)
            if alert is not None:
                out.append(alert)
        return tuple(out)

    async def timeline(self, lat: float, lon: float) -> Timeline:
        """The local 3-hourly wind/gust/rain strip + sustained-wind arrival labels for a
        point. Resolves /points → forecastGridData + place tz, expands the gridpoint
        series (instantaneous wind replicated, accumulated rain divided), converts to
        mph/inches, and downsamples to ~36h at 3-hourly. Labels render place-local via
        the point's IANA tz. A 404 means out of coverage (NwsOutOfCoverage); a
        5xx/timeout is transient (NwsUnavailable)."""
        point = await self._get(f"{self._base}/points/{lat},{lon}")
        props = point.get("properties") if isinstance(point, dict) else None
        if not isinstance(props, dict):
            raise NwsUnavailable("the forecast service returned an incomplete point")
        grid_url = str(props.get("forecastGridData") or "").strip()
        tz_name = str(props.get("timeZone") or "").strip()
        if not grid_url:
            raise NwsUnavailable("the forecast service returned no gridpoint reference")

        grid = await self._get(grid_url)
        gprops = grid.get("properties") if isinstance(grid, dict) else None
        if not isinstance(gprops, dict):
            raise NwsUnavailable("the forecast service returned an incomplete gridpoint")
        return _build_timeline(gprops, tz_name)

    async def _get(self, url: str) -> object:
        """GET one NWS URL with the required headers. A definitive 404 raises
        NwsOutOfCoverage; everything else (5xx, timeout, transport, bad JSON) raises
        NwsUnavailable. The warning and both error messages carry only a status code —
        never the coordinate or the full URL (the location firewall, §5)."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(url, headers=_HEADERS)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning("web.nws_failed", status=status)
            if status == 404:
                raise NwsOutOfCoverage("the point is outside NWS coverage") from exc
            raise NwsUnavailable(f"the forecast service returned status {status}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.nws_failed")
            raise NwsUnavailable("the forecast service is unavailable right now") from exc


def _parse_alert(feature: object) -> Alert | None:
    """One `features[]` entry → an Alert, or None for a non-tropical / malformed entry.
    The `event` string is mapped to the (level, kind) enums; the headline rides as text
    content only (rendered escaped, never markup — §5)."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties")
    if not isinstance(props, dict):
        return None
    event = str(props.get("event") or "").strip()
    mapped = _TROPICAL_EVENTS.get(event)
    if mapped is None:
        return None
    level, kind = mapped
    headline = str(props.get("headline") or "")
    return Alert(event=event, level=level, kind=kind, headline=headline)


def _zone(tz_name: str) -> ZoneInfo:
    """The place IANA zone, or UTC if the name is empty/unknown. A bad tz must not crash
    the timeline — the labels just fall back to UTC."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _build_timeline(gprops: dict, tz_name: str) -> Timeline:
    """Merge the three gridpoint series into a uniform hourly series, derive the
    sustained-wind arrival labels, and downsample to 3-hourly cells over the next ~36h.
    All times come from the upstream `validTime`s — never a real clock (§8)."""
    wind = _expand_series(gprops.get("windSpeed"), accumulate=False)
    gust = _expand_series(gprops.get("windGust"), accumulate=False)
    rain = _expand_series(gprops.get("quantitativePrecipitation"), accumulate=True)

    wind_uom = _uom(gprops.get("windSpeed"))
    gust_uom = _uom(gprops.get("windGust"))
    rain_uom = _uom(gprops.get("quantitativePrecipitation"))

    zone = _zone(tz_name)
    tz_out = tz_name or "UTC"

    hours = sorted(set(wind) | set(gust) | set(rain))
    if not hours:
        return Timeline(cells=(), ts_force_label=None, hurricane_force_label=None, tz=tz_out)

    # A uniform hourly series from the first valid hour across the window. Each hour
    # carries its (possibly absent) wind/gust/rain; a gap reads as 0 for display.
    start = hours[0]
    series: list[tuple[datetime, float, float, float]] = []
    for h in range(_WINDOW_HOURS):
        when = start + timedelta(hours=h)
        wind_mph = _convert(wind[when], wind_uom, kmh_to_mph=True) if when in wind else 0.0
        gust_mph = _convert(gust[when], gust_uom, kmh_to_mph=True) if when in gust else 0.0
        rain_in = _convert(rain[when], rain_uom, kmh_to_mph=False) if when in rain else 0.0
        series.append((when, wind_mph, gust_mph, rain_in))

    ts_label = _first_crossing(series, _TS_FORCE_MPH, zone)
    hu_label = _first_crossing(series, _HURRICANE_FORCE_MPH, zone)

    cells: list[TimelineCell] = []
    for i in range(0, len(series), _BUCKET_HOURS):
        bucket = series[i : i + _BUCKET_HOURS]
        when = bucket[0][0]
        # Wind/gust are instantaneous → the bucket's peak; rain accumulates → the sum.
        peak_wind = max(w for _, w, _, _ in bucket)
        peak_gust = max(g for _, _, g, _ in bucket)
        sum_rain = sum(r for _, _, _, r in bucket)
        cells.append(
            TimelineCell(
                label=_hour_label(when.astimezone(zone)),
                wind_mph=round(peak_wind),
                gust_mph=round(peak_gust),
                rain_in=round(sum_rain, 2),
            )
        )

    return Timeline(
        cells=tuple(cells),
        ts_force_label=ts_label,
        hurricane_force_label=hu_label,
        tz=tz_out,
    )


def _uom(series: object) -> object:
    """The `uom` of a gridpoint series, or None if the series is absent/malformed."""
    if isinstance(series, dict):
        return series.get("uom")
    return None


def _first_crossing(
    series: list[tuple[datetime, float, float, float]], threshold: float, zone: ZoneInfo
) -> str | None:
    """The place-local hour label of the first hour whose SUSTAINED wind (mph) is at or
    above `threshold`, or None if it never crosses within the window. Sustained wind —
    not gust — is what the §1 arrival definition scans (39 / 74 mph)."""
    for when, wind_mph, _, _ in series:
        if wind_mph >= threshold:
            return _hour_label(when.astimezone(zone))
    return None
