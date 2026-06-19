"""The on-box geocoding agent tool: `geocode_reverse`.

Reverse geocoding is the offline nearest-city lookup (`jbrain.citygeocode`) — a *read*,
not an egress connector, so it stages nothing and never leaves the box; it names a
coordinate at city/region/country granularity (not a street). There is no on-box
forward geocoder; the only off-box path is the owner-approved external connector.
"""

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.citygeocode import CityGeocoder

log = structlog.get_logger()


def build_geocode_handlers(city_geocoder: CityGeocoder) -> dict[str, ToolHandler]:
    async def geocode_reverse_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        lat, lon = arguments.get("latitude"), arguments.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return ToolOutput("geocode_reverse needs numeric latitude and longitude.")
        try:
            hit = city_geocoder.nearest(float(lat), float(lon))
        except Exception as exc:  # noqa: BLE001 - a geocode hiccup is a recoverable observation
            log.warning("geocode.reverse_failed", error=repr(exc))
            return ToolOutput("the geocoder is unavailable right now.")
        if hit is None:
            return ToolOutput("No populated place is near that coordinate.")
        km = hit.distance_m / 1000
        how_far = "right here" if km < 1 else f"~{round(km)} km away"
        return ToolOutput(f"{hit.label} (nearest city, {how_far})")

    return {"geocode_reverse": geocode_reverse_tool}
