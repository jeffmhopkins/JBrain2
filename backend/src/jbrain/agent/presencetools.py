"""jerv's owner-location tool: `current_location` (docs/reference/ASSISTANT.md "Agent selection").

jerv answers location from the position the PWA captured for THIS turn (the same warm
geolocation fix note sends attach) — or, when this turn carried none, the owner's
cached last-known warm fix (`ToolContext.here_as_of` marks it, and the answer says so).
It does NOT read the owner's location domain: no saved places, no OwnTracks device
history.

Resolution is low-footprint by default and the caller picks the detail (`detail`):
- `"city"` (default) — an in-process offline reverse-geocode (`CityGeocoder`, the
  bundled GeoNames cities) names the NEAREST city/region/country: no service, no
  index, no egress;
- `"address"` — when the owner wants a SPECIFIC street address and they configured an
  external geocoder, a direct reverse lookup returns the street (jerv's direct-egress
  sandbox — only the coordinate leaves the box), falling back to the city otherwise;
- `"coordinates"` — the raw latitude/longitude of the fix, reported verbatim with no
  geocoding (also the fallback when no populated place is near enough).

A cached fix is always reported as last-known with its age — never as "here now".

It is gated to jerv alone — a `web`-class (opt-in) tool the default knowledge agent
is never offered (see contracts.PermissionClass).
"""

from datetime import UTC, datetime

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.citygeocode import CityGeocoder
from jbrain.geocode import NominatimReverseClient

log = structlog.get_logger()

_NO_FIX = (
    "I don't have the owner's current location for this turn. They can share it from"
    " the app (location capture), and then I can help with where they are or nearby info."
)
_STALE_TAIL = " — they may have moved since"


def _ago(age_seconds: float) -> str:
    """A coarse human "how long ago" (minutes under 90, else hours), mirroring
    `locations.presence._ago` — a freshness cue, never a precise timestamp."""
    mins = round(age_seconds / 60)
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{mins} min ago"
    return f"{round(age_seconds / 3600)} h ago"


def _staleness(as_of: datetime | None) -> str | None:
    """The "N ago" cue for a cached fix, or None when `here` is this turn's live fix."""
    if as_of is None:
        return None
    return _ago((datetime.now(UTC) - as_of).total_seconds())


def _city_phrase(hit, ago: str | None) -> str:  # noqa: ANN001 - CityHit, loose to avoid an import cycle
    where = ", ".join(p for p in (hit.name, hit.region, hit.country) if p)
    km = hit.distance_m / 1000.0
    place = f"in {where}" if km < 1.0 else f"near {where} (about {round(km)} km away)"
    if ago is None:
        return f"The owner is {place}."
    return (
        f"The owner has no live fix this turn; their last known location ({ago})"
        f" was {place}{_STALE_TAIL}."
    )


def _coords_phrase(lat: float, lon: float, ago: str | None) -> str:
    if ago is None:
        return f"The owner's current coordinates are {lat:.5f}, {lon:.5f}."
    return (
        f"The owner has no live fix this turn; their last known coordinates ({ago})"
        f" were {lat:.5f}, {lon:.5f}{_STALE_TAIL}."
    )


def _address_phrase(addr: str, ago: str | None) -> str:
    if ago is None:
        return f"The owner is at {addr}."
    return (
        f"The owner has no live fix this turn; their last known address ({ago})"
        f" was {addr}{_STALE_TAIL}."
    )


def build_presence_handlers(
    city_geocoder: CityGeocoder, external_reverse: NominatimReverseClient | None = None
) -> dict[str, ToolHandler]:
    async def current_location_tool(arguments: dict, ctx: ToolContext) -> str:
        if ctx.here is None:
            return _NO_FIX
        lat, lon = ctx.here
        ago = _staleness(ctx.here_as_of)
        detail = str(arguments.get("detail") or "city").lower()
        # Raw coordinates when explicitly asked — no geocoding, the fix is the answer.
        if detail == "coordinates":
            return _coords_phrase(lat, lon, ago)
        # A specific street address only when asked AND an external geocoder is set.
        if detail == "address" and external_reverse is not None:
            addr = await external_reverse.reverse(lat, lon)
            if addr:
                return _address_phrase(addr, ago)
        # Default: name the nearest city offline — no service, no egress.
        try:
            hit = city_geocoder.nearest(lat, lon)
        except Exception as exc:  # noqa: BLE001 - a geocode hiccup is recoverable
            log.warning("agent.current_location_city_failed", error=repr(exc))
            hit = None
        if hit is not None:
            return _city_phrase(hit, ago)
        # No populated place close enough — the coordinate is the answer.
        return _coords_phrase(lat, lon, ago)

    return {"current_location": current_location_tool}
