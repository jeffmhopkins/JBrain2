"""jerv's owner-location tool: `current_location` (docs/ASSISTANT.md "Agent selection").

jerv answers location only from the live position the PWA captured for THIS turn (the
same warm geolocation fix note sends attach) — it does NOT read the owner's location
domain: no saved places, no OwnTracks device history. It reverse-geocodes that live
coordinate on-box (Photon, no egress) to a city/street address; when the geocoder
can't name it, the coordinate itself is an acceptable answer. With no live fix on the
turn (capture off, or no permission), there's nothing to report.

It is gated to jerv alone — a `web`-class (opt-in) tool the default knowledge agent
is never offered. (The `web` class is the jerv sandbox's opt-in direct-exec gate; this
member geocodes a request-carried coordinate, not internet egress and not a read of
the firewalled location domain — see contracts.PermissionClass.)
"""

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.geocode import GeocodeClient

log = structlog.get_logger()

_NO_FIX = (
    "I don't have the owner's current location for this turn. They can share it from"
    " the app (location capture), and then I can help with where they are or nearby info."
)


def build_presence_handlers(geocoder: GeocodeClient | None = None) -> dict[str, ToolHandler]:
    async def current_location_tool(arguments: dict, ctx: ToolContext) -> str:
        if ctx.here is None:
            return _NO_FIX
        lat, lon = ctx.here
        if geocoder is not None:
            try:
                hit = await geocoder.reverse(lat, lon)
            except Exception as exc:  # noqa: BLE001 - a geocoder hiccup is recoverable
                log.warning("agent.current_location_geocode_failed", error=repr(exc))
                hit = None
            if hit is not None:
                return f"The owner is currently near {hit.label}."
        # Couldn't resolve an address — the coordinate itself is an acceptable answer.
        return f"The owner's current coordinates are approximately {lat:.5f}, {lon:.5f}."

    return {"current_location": current_location_tool}
