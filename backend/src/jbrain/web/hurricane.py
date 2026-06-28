"""Active tropical-cyclone lookups via the NHC feed (docs/ASSISTANT.md "Agent
selection", DESIGN.md "hurricane_card tool-view").

Like the weather tool this runs DIRECTLY rather than staging an egress Proposal —
the bounded jerv-sandbox exception to invariant #9. One pinned, config-supplied
upstream (the National Hurricane Center's public `CurrentStorms.json`), never
model-supplied; it is the GLOBAL active-storm list and takes no query, so the
request carries no location at all — the only egress that ever names a place is the
shared weather geocoder, which sends a public place name / city centre, the same
coarseness as naming the city (the location firewall).

The feed gives each active storm's vitals and position; the distance and bearing
from the owner's place to a storm are computed ON-BOX from the geocoded city centre,
so the owner's precise fix never leaves the box. The base URL defaults to the public
NHC endpoint (free, no API key); empty disables the tool (the sidecar still loads and
the handler reports "not configured").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import asin, atan2, cos, degrees, radians, sin, sqrt

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0
_EARTH_MI = 3958.7613  # mean Earth radius, statute miles
_KT_TO_MPH = 1.15078

_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


class HurricaneError(RuntimeError):
    """The active-storm list could not be produced — the upstream was unreachable,
    returned a non-2xx, or sent a malformed body. Surfaced to the agent as a
    recoverable tool error, like WeatherError."""


# NHC classification codes → the `kind` enum the component maps to a label + tone
# (DESIGN.md: enums, never colors). The Saffir-Simpson category number rides a
# separate `cat` field, derived from wind, and is meaningful only for a hurricane or
# typhoon — so the table carries the storm *type*, the wind carries the *strength*.
_KIND: dict[str, str] = {
    "HU": "hurricane",
    "MH": "hurricane",  # major hurricane (cat >= 3); category still rides `cat`
    "TY": "typhoon",
    "ST": "typhoon",  # super typhoon
    "TS": "tropical-storm",
    "TD": "tropical-depression",
    "STS": "subtropical-storm",
    "SS": "subtropical-storm",
    "STD": "subtropical-depression",
    "SD": "subtropical-depression",
    "EX": "post-tropical",
    "PT": "post-tropical",
    "PTC": "potential",  # Potential Tropical Cyclone
    "PC": "potential",
    "LO": "low",
    "DB": "low",
}

# Saffir-Simpson thresholds in KNOTS (the unit the feed's `intensity` is in). The
# category is meaningful only once a system reaches hurricane/typhoon strength.
_SAFFIR_SIMPSON = ((137, "5"), (113, "4"), (96, "3"), (83, "2"), (64, "1"))


def classify(code: str) -> str:
    """Map an NHC classification code to its `kind` enum; unknown reads as `cyclone`."""
    return _KIND.get(code.strip().upper(), "cyclone")


def category(wind_kt: int, kind: str) -> str:
    """The Saffir-Simpson category number ("1".."5") for a hurricane/typhoon, or ""
    for anything weaker — the badge the component shows only when it applies."""
    if kind not in ("hurricane", "typhoon"):
        return ""
    for floor, cat in _SAFFIR_SIMPSON:
        if wind_kt >= floor:
            return cat
    return ""


def compass(deg: float) -> str:
    """A bearing in degrees → the nearest 16-point compass abbreviation."""
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in statute miles."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * _EARTH_MI * asin(sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing FROM point 1 TO point 2, in degrees (0 = N)."""
    p1, p2 = radians(lat1), radians(lat2)
    dl = radians(lon2 - lon1)
    y = sin(dl) * cos(p2)
    x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
    return degrees(atan2(y, x)) % 360


def _i(value: object) -> int:
    """Round a JSON number/numeric-string to int, defaulting 0 for None/non-numeric."""
    try:
        return round(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _coord(numeric: object, text: object, neg: str) -> float | None:
    """A storm coordinate: prefer the feed's numeric field; fall back to parsing the
    suffixed string (e.g. "20.5N" / "94.5W"), negating the `neg` hemisphere (S/W)."""
    try:
        return float(numeric)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    s = str(text or "").strip().upper()
    if not s or s[-1] not in "NSEW":
        return None
    try:
        val = float(s[:-1])
    except ValueError:
        return None
    return -val if s[-1] == neg else val


@dataclass(frozen=True)
class ActiveStorm:
    """One active tropical cyclone from the NHC feed: its identity, vitals, and
    position. Wind is knots and pressure millibars as the feed reports them; the
    handler derives the category and converts to mph for display."""

    id: str
    name: str
    kind: str  # the classified `kind` enum (hurricane, tropical-storm, …)
    wind_kt: int
    pressure_mb: int
    latitude: float
    longitude: float
    move_dir: int  # degrees the storm is moving TOWARD (0 = N); -1 if unknown
    move_mph: int  # forward speed, mph; 0 if stationary/unknown
    last_update: str  # ISO-8601 UTC instant the advisory was issued


class HurricaneClient:
    """Fetch the NHC global active-storm list. The base URL is config-pinned;
    `transport` is injectable so tests run against a MockTransport with no network
    (DEVELOPMENT.md "no network in tests")."""

    def __init__(self, current_storms_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._url = current_storms_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._url)

    async def active_storms(self) -> tuple[ActiveStorm, ...]:
        """Every tropical cyclone NHC is currently tracking, worldwide. An empty
        tuple means none are active (the common off-season case), which is a valid
        answer, not an error."""
        if not self._url:
            raise HurricaneError("the hurricane tracker is not configured on this instance")
        body = await self._get(self._url)
        rows = body.get("activeStorms") if isinstance(body, dict) else None
        if rows is None:
            raise HurricaneError("the hurricane tracker returned an unexpected response")
        if not isinstance(rows, list):
            raise HurricaneError("the hurricane tracker returned an unexpected response")
        out: list[ActiveStorm] = []
        for row in rows:
            storm = _parse_storm(row)
            if storm is not None:
                out.append(storm)
        return tuple(out)

    async def _get(self, url: str) -> object:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning("web.hurricane_failed", status=exc.response.status_code, error=repr(exc))
            raise HurricaneError("the hurricane tracker is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.hurricane_failed", error=repr(exc))
            raise HurricaneError("the hurricane tracker is unavailable right now") from exc


def _parse_storm(row: object) -> ActiveStorm | None:
    """Shape one feed entry into an ActiveStorm. Defensive: a row missing a usable
    position is skipped rather than crashing the whole lookup."""
    if not isinstance(row, dict):
        return None
    lat = _coord(row.get("latitudeNumeric"), row.get("latitude"), "S")
    lon = _coord(row.get("longitudeNumeric"), row.get("longitude"), "W")
    if lat is None or lon is None:
        return None
    kind = classify(str(row.get("classification") or ""))
    move_dir = _i(row.get("movementDir")) if row.get("movementDir") is not None else -1
    return ActiveStorm(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or "").strip() or "Unnamed",
        kind=kind,
        wind_kt=_i(row.get("intensity")),
        pressure_mb=_i(row.get("pressure")),
        latitude=lat,
        longitude=lon,
        move_dir=move_dir,
        move_mph=_i(row.get("movementSpeed")),
        last_update=str(row.get("lastUpdate") or "").strip(),
    )


def format_as_of(iso_utc: str) -> str:
    """An NHC `lastUpdate` instant ("2026-06-28T15:00:00.000Z") → a compact UTC label
    ("Jun 28, 3:00 PM UTC") without a platform-specific strftime; "" if unparseable."""
    s = iso_utc.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s).astimezone(UTC)
    except (TypeError, ValueError):
        return ""
    suffix = "AM" if dt.hour < 12 else "PM"
    h12 = dt.hour % 12 or 12
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {h12}:{dt.minute:02d} {suffix} UTC"


def movement(storm: ActiveStorm) -> str:
    """A storm's motion as a compact label: "NNE 14 mph", or "stationary"."""
    if storm.move_mph <= 0 or storm.move_dir < 0:
        return "stationary"
    return f"{compass(storm.move_dir)} {storm.move_mph} mph"


def sustained_mph(storm: ActiveStorm) -> int:
    """The storm's max sustained wind in mph, rounded to the nearest 5 as NHC reports
    (the feed carries knots)."""
    mph = storm.wind_kt * _KT_TO_MPH
    return int(round(mph / 5.0) * 5)
