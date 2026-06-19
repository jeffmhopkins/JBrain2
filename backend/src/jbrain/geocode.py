"""The external reverse-geocoder client (Phase 7 Wave 4b).

On-box reverse geocoding is the offline nearest-city lookup (`jbrain.citygeocode`);
there is no resident geocoder service. The ONLY off-box geocoding path is the
owner-configured external reverse-geocoder, reached two ways: the staged
`geocode_external` connector (the curator's #9-chokepoint egress) and the direct
`NominatimReverseClient` below (jerv's "specific street address" fallback, inside its
direct-egress sandbox). Both are default OFF — empty `external_geocoder_url`.

A Nominatim-compatible `/reverse` (jsonv2 → `display_name`) is the target; a
Photon-style GeoJSON response is also understood, so `format_address` is shared with
the connector's parser.
"""

from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 10.0


def format_address(props: dict[str, Any]) -> str:
    """A geocoder feature's `properties` flattened to one address line (GeoJSON keys:
    name, housenumber, street, city, district, state, postcode, country). We keep the
    populated parts in postal-ish order and drop the rest — empty in, empty out, so the
    caller can treat "" as no usable address."""
    house_street = " ".join(p for p in (props.get("housenumber"), props.get("street")) if p)
    parts = [
        props.get("name"),
        house_street or None,
        props.get("city") or props.get("district"),
        props.get("state"),
        props.get("postcode"),
        props.get("country"),
    ]
    # De-duplicate adjacent repeats (a feed often echoes name == street) and drop blanks.
    out: list[str] = []
    for part in parts:
        if part and (not out or out[-1] != part):
            out.append(str(part))
    return ", ".join(out)


class NominatimReverseClient:
    """A DIRECT reverse-geocode against the owner-configured external geocoder — the
    "specific street address" fallback for jerv's current_location. Unlike the staged
    `geocode_external` connector (the curator's #9-chokepoint egress), this runs
    directly, inside jerv's existing direct-egress sandbox: the only thing that leaves
    the box is the coordinate the owner shared this turn, to the URL the owner set.
    Default OFF — an empty `base_url` yields a client that never calls out.

    Targets a Nominatim-compatible `/reverse` (jsonv2 → `display_name`); a Photon-style
    GeoJSON response is also understood, mirroring the connector's parser."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    async def reverse(self, latitude: float, longitude: float) -> str | None:
        """The street address for a coordinate, or None (unconfigured, no hit, or an
        outage) — a recoverable miss the caller degrades past, never a raised error."""
        if not self._base_url:
            return None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=_TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.get(
                    "/reverse", params={"format": "jsonv2", "lat": latitude, "lon": longitude}
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("geocode.external_reverse_failed", error=repr(exc))
            return None
        if isinstance(data, dict):
            name = data.get("display_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
            features = data.get("features")
            if isinstance(features, list) and features:
                return format_address((features[0] or {}).get("properties") or {}) or None
        return None
