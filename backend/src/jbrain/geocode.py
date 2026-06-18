"""The local Photon geocoder client (Phase 7 Wave 4).

Mirrors `embed.TeiEmbedClient`: a thin httpx client over an opt-in compose service
(`JBRAIN_GEOCODER_URL`) that runs on a no-egress internal network, so reverse and
forward geocoding stay entirely on-box — they are *local reads*, never an egress
connector (which is the separate, owner-approved external fallback). Photon is the
cache; nothing is persisted in the DB for a local lookup.

Photon speaks GeoJSON: `GET /reverse?lat=&lon=` and `GET /api?q=` each return a
FeatureCollection whose feature `properties` carry the address parts. We flatten
those to a single human address string and the feature's coordinates.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 10.0


@dataclass(frozen=True)
class GeocodeResult:
    """One geocoder hit: a flattened address label and its coordinates."""

    label: str
    latitude: float
    longitude: float


class GeocodeClient(Protocol):
    async def reverse(self, latitude: float, longitude: float) -> GeocodeResult | None: ...

    async def forward(self, query: str, limit: int = 5) -> list[GeocodeResult]: ...


def format_address(props: dict[str, Any]) -> str:
    """A Photon feature's `properties` flattened to one address line. Photon keys:
    name, housenumber, street, city, district, state, postcode, country. We keep
    the populated parts in postal-ish order and drop the rest — empty in, empty
    out, so the caller can treat "" as no usable address."""
    house_street = " ".join(p for p in (props.get("housenumber"), props.get("street")) if p)
    parts = [
        props.get("name"),
        house_street or None,
        props.get("city") or props.get("district"),
        props.get("state"),
        props.get("postcode"),
        props.get("country"),
    ]
    # De-duplicate adjacent repeats (Photon often echoes name == street) and drop blanks.
    out: list[str] = []
    for part in parts:
        if part and (not out or out[-1] != part):
            out.append(str(part))
    return ", ".join(out)


def _to_result(feature: dict[str, Any]) -> GeocodeResult | None:
    coords = (feature.get("geometry") or {}).get("coordinates")
    if not (isinstance(coords, list) and len(coords) == 2):
        return None
    label = format_address(feature.get("properties") or {})
    if not label:
        return None
    lon, lat = coords
    return GeocodeResult(label=label, latitude=float(lat), longitude=float(lon))


class PhotonGeocoderClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url
        self._transport = transport

    async def _features(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=_TIMEOUT, transport=self._transport
        ) as client:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            data = resp.json()
        features = data.get("features")
        return features if isinstance(features, list) else []

    async def reverse(self, latitude: float, longitude: float) -> GeocodeResult | None:
        """The nearest address to a coordinate, or None when Photon has no hit."""
        features = await self._features("/reverse", {"lat": latitude, "lon": longitude})
        for feature in features:
            result = _to_result(feature)
            if result is not None:
                return result
        return None

    async def forward(self, query: str, limit: int = 5) -> list[GeocodeResult]:
        """Address/place candidates for a free-text query (owner-only at the tool
        layer — a free-text slot the ParamSpec allowlist can't constrain)."""
        features = await self._features("/api", {"q": query, "limit": limit})
        return [r for f in features if (r := _to_result(f)) is not None]
