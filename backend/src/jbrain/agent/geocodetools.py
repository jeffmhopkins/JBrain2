"""The on-box geocoding agent tools (Phase 7 Wave 4): `geocode_reverse` (default)
and `geocode_forward` (owner-only).

Both hit the local Photon service directly — a *read*, not an egress connector, so
neither stages a Proposal (the geocoder runs on a no-egress network; the external
fallback is the separate, owner-approved connector). `geocode_forward` takes a
free-text query, which a typed-parameter allowlist cannot constrain, so it is
gated to a *full* owner session: a narrowed (`owner_scoped`) agent context — the
only place a capability could be smuggled — is refused before the query is sent.
"""

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.geocode import GeocodeClient

log = structlog.get_logger()

_FORWARD_MAX = 10


def _is_full_owner(session: SessionContext) -> bool:
    """The owner, not a narrowed agent scope — the gate the free-text forward
    lookup requires (mirrors `app.is_full_owner()` at the RLS layer)."""
    return session.principal_kind == "owner" and not session.owner_scoped


def build_geocode_handlers(geocoder: GeocodeClient) -> dict[str, ToolHandler]:
    async def geocode_reverse_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        lat, lon = arguments.get("latitude"), arguments.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return ToolOutput("geocode_reverse needs numeric latitude and longitude.")
        try:
            result = await geocoder.reverse(float(lat), float(lon))
        except Exception as exc:  # noqa: BLE001 - a geocoder outage is a recoverable observation
            log.warning("geocode.reverse_failed", error=repr(exc))
            return ToolOutput("the geocoder is unavailable right now.")
        if result is None:
            return ToolOutput("No address found for that coordinate.")
        return ToolOutput(result.label)

    async def geocode_forward_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        if not _is_full_owner(ctx.session):
            return ToolOutput("geocode_forward is owner-only and isn't available in this session.")
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolOutput("geocode_forward needs a query.")
        limit = max(1, min(_FORWARD_MAX, int(arguments.get("limit", 5))))
        try:
            results = await geocoder.forward(query, limit)
        except Exception as exc:  # noqa: BLE001 - recoverable observation, never a crash
            log.warning("geocode.forward_failed", error=repr(exc))
            return ToolOutput("the geocoder is unavailable right now.")
        if not results:
            return ToolOutput(f'No places found for "{query}".')
        return ToolOutput(
            "\n".join(f"- {r.label} ({r.latitude:.5f}, {r.longitude:.5f})" for r in results)
        )

    return {"geocode_reverse": geocode_reverse_tool, "geocode_forward": geocode_forward_tool}
