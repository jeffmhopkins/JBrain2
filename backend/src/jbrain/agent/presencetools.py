"""jerv's owner-location tool: `current_location` (docs/ASSISTANT.md "Agent selection").

jerv answers location only from the live position the PWA captured for THIS turn (the
same warm geolocation fix note sends attach) — it does NOT read the owner's location
domain: no saved places, no OwnTracks device history.

Resolution is low-footprint by default and escalates only when asked:
- an in-process offline reverse-geocode (`CityGeocoder`, the bundled GeoNames cities)
  names the NEAREST city/region/country — no service, no index, no egress;
- when the owner wants a SPECIFIC street address (`precise`), and they configured an
  external geocoder, a direct reverse lookup returns the street (jerv's direct-egress
  sandbox — only the coordinate leaves the box);
- otherwise the coordinate itself is the answer.

It is gated to jerv alone — a `web`-class (opt-in) tool the default knowledge agent
is never offered (see contracts.PermissionClass).
"""

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.citygeocode import CityGeocoder
from jbrain.geocode import NominatimReverseClient

log = structlog.get_logger()

_NO_FIX = (
    "I don't have the owner's current location for this turn. They can share it from"
    " the app (location capture), and then I can help with where they are or nearby info."
)


def _city_phrase(hit) -> str:  # noqa: ANN001 - CityHit, kept loose to avoid an import cycle
    where = ", ".join(p for p in (hit.name, hit.region, hit.country) if p)
    km = hit.distance_m / 1000.0
    if km < 1.0:
        return f"The owner is in {where}."
    return f"The owner is near {where} (about {round(km)} km away)."


def build_presence_handlers(
    city_geocoder: CityGeocoder, external_reverse: NominatimReverseClient | None = None
) -> dict[str, ToolHandler]:
    async def current_location_tool(arguments: dict, ctx: ToolContext) -> str:
        if ctx.here is None:
            return _NO_FIX
        lat, lon = ctx.here
        # A specific street address only when asked AND an external geocoder is set.
        if bool(arguments.get("precise")) and external_reverse is not None:
            addr = await external_reverse.reverse(lat, lon)
            if addr:
                return f"The owner is at {addr}."
        # Default: name the nearest city offline — no service, no egress.
        try:
            hit = city_geocoder.nearest(lat, lon)
        except Exception as exc:  # noqa: BLE001 - a geocode hiccup is recoverable
            log.warning("agent.current_location_city_failed", error=repr(exc))
            hit = None
        if hit is not None:
            return _city_phrase(hit)
        # No populated place close enough — the coordinate is the answer.
        return f"The owner's current coordinates are approximately {lat:.5f}, {lon:.5f}."

    return {"current_location": current_location_tool}
