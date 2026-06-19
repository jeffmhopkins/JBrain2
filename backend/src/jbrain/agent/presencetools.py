"""jerv's owner-location tool: `current_location` (docs/ASSISTANT.md "Agent selection").

jerv runs in an empty-scope, `owner_scoped` sandbox, so an ordinary location read —
which requires a FULL owner via `require_full_owner` — would refuse inside its tool
dispatch. This tool is the deliberate, owner-approved exception, the active form of
the app-open presence read: it reconstructs the FULL owner context from the session's
principal and reads the owner's coarse, coordinate-free presence (a place name +
freshness), NEVER a coordinate, and NEVER an off-box call.

It is gated to jerv alone — a `web`-class (opt-in) tool the default knowledge agent
is never offered — so this one privileged read can run only for the agent the owner
enabled it for. (The `web` class is the jerv sandbox's opt-in direct-exec gate; this
member is an on-box owner read, not internet egress — see contracts.PermissionClass.)
"""

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.db.session import SessionContext
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.geocode import GeocodeClient
from jbrain.locations import LocationToolRefusal, SqlLocationRepo
from jbrain.locations.presence import presence_line, read_owner_presence

log = structlog.get_logger()

_UNAVAILABLE = "I can't check the owner's location in this session."
# A live fix within this of a saved place reads as "at" it; out to the wider bound it
# reads as "near" it (with the rounded distance). Beyond that, fall to the geocoder.
_AT_PLACE_M = 120.0
_NEAR_RADIUS_M = 1500.0


def build_presence_handlers(
    locations: SqlLocationRepo, devices: SqlDeviceRepo, geocoder: GeocodeClient | None = None
) -> dict[str, ToolHandler]:
    async def _from_live_fix(lat: float, lon: float, ctx: ToolContext) -> str:
        """Name the owner's live PWA position without surfacing a coordinate, trying
        the most meaningful sources first: a saved place near the point, then an
        on-box reverse-geocode, then an honest 'have the position, can't name it'."""
        pid = ctx.session.principal_id
        if pid and ctx.session.principal_kind == "owner":
            owner_ctx = SessionContext(principal_id=pid, principal_kind="owner")
            try:
                near = await locations.nearby(
                    owner_ctx, center=(lat, lon), radius_m=_NEAR_RADIUS_M, limit=1
                )
            except Exception as exc:  # noqa: BLE001 - a location read hiccup is recoverable
                log.warning("agent.current_location_nearby_failed", error=repr(exc))
                near = []
            if near:
                place = near[0]
                if place.distance_m <= _AT_PLACE_M:
                    return f"The owner is at {place.name}."
                return (
                    f"The owner is near {place.name}"
                    f" (about {round(place.distance_m / 10) * 10} m away)."
                )
        if geocoder is not None:
            try:
                hit = await geocoder.reverse(lat, lon)
            except Exception as exc:  # noqa: BLE001 - a geocoder hiccup is recoverable
                log.warning("agent.current_location_geocode_failed", error=repr(exc))
                hit = None
            if hit is not None:
                return f"The owner is currently near {hit.label}."
        return (
            "I have the owner's current position, but it isn't near any of their saved"
            " places and I couldn't resolve it to an address right now."
        )

    async def current_location_tool(arguments: dict, ctx: ToolContext) -> str:
        # Prefer the live fix the PWA captured this turn (the same warm geolocation
        # note sends attach) — foreground, current, and independent of OwnTracks.
        if ctx.here is not None:
            return await _from_live_fix(ctx.here[0], ctx.here[1], ctx)
        # No live fix on the turn — fall back to the OwnTracks device stack. jerv's
        # tool session is owner_scoped (empty scopes), which require_full_owner
        # refuses; reconstruct the FULL owner ctx from its principal so the presence
        # read clears that gate — the same privileged read the app-open presence does,
        # reachable only here (the `web`-gated jerv allowlist). Defensive: only an
        # owner principal resolves it.
        pid = ctx.session.principal_id
        if not pid or ctx.session.principal_kind != "owner":
            return _UNAVAILABLE
        owner_ctx = SessionContext(principal_id=pid, principal_kind="owner")
        try:
            presence = await read_owner_presence(locations, devices, owner_ctx)
        except LocationToolRefusal:
            return _UNAVAILABLE
        except Exception as exc:  # noqa: BLE001 - a location read hiccup is recoverable
            log.warning("agent.current_location_failed", error=repr(exc))
            return "I couldn't check the owner's location right now."
        line = presence_line(presence)
        if line is None:
            return (
                "I don't have a recent location fix for the owner, so I can't say where"
                " they are right now."
            )
        return line

    return {"current_location": current_location_tool}
