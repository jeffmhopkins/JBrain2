"""The external reverse-geocoder fallback connector (Phase 7 Wave 4b).

The on-box offline city geocoder (`jbrain.citygeocode`) is the default; this is the
owner-approved fallback for a specific street address. It is an ordinary egress
Connector, so it
inherits the whole #9 chokepoint: the tool stages an egress Proposal (never calls
out), the guard admits only the typed `lat`/`lon` slots — there is NO free-text
query slot, so a coordinate is all that can ever leave the box — and the result
(an address, PII) is cached in `connector_cache` under the location + owner RLS
firewall (proven in test_agent_connectors_rls.py).

DEFAULT OFF: with no `external_geocoder_url` configured the factory returns an
empty list, so the connector is never registered and no off-box geocoding path
exists at all. Targets a Nominatim-compatible reverse endpoint (`/reverse`,
`display_name`); a Photon-style GeoJSON response is also understood.
"""

from typing import Any

from jbrain.connectors.base import Connector, ParamSpec
from jbrain.geocode import format_address


def parse_external_geocode(data: Any) -> str:
    """The external reverse response → one address line, source-attributed.
    Handles Nominatim (`display_name`) and Photon-style GeoJSON (`features`)."""
    if isinstance(data, dict):
        name = data.get("display_name")
        if isinstance(name, str) and name.strip():
            return f"Address (source: external geocoder):\n{name.strip()}"
        features = data.get("features")
        if isinstance(features, list) and features:
            label = format_address((features[0] or {}).get("properties") or {})
            if label:
                return f"Address (source: external geocoder):\n{label}"
    return "No address found."


def geocode_connectors(external_geocoder_url: str) -> list[Connector]:
    """The external reverse-geocoder, or nothing when unconfigured (default off).
    Reverse only — `lat`/`lon` typed slots, never a free-text query."""
    if not external_geocoder_url:
        return []
    return [
        Connector(
            name="geocode_external",
            base_url=external_geocoder_url,
            # format baked in (Nominatim returns XML otherwise); the guard still
            # only fills the typed lat/lon slots.
            path="/reverse?format=jsonv2",
            domain="location",
            params=(ParamSpec("lat", float), ParamSpec("lon", float)),
            parse=parse_external_geocode,
        )
    ]
