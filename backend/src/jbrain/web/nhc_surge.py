"""Peak storm-surge band lookup via the NHC Peak-Surge ArcGIS MapServer (best-effort;
docs/archive/HURRICANE_TABS_PLAN.md §1/§3). The ArcGIS `query` does the point-in-polygon
intersect SERVER-SIDE (`esriGeometryPoint`), so the box carries no geometry library
(plan requirement #2). The Peak-Surge layer has no numeric band field — the band is
text inside each feature's `popupinfo` HTML, drawn from the renderer's labels ("Up
to 3 ft" … "Above 12 ft") — so the band is parsed out with a regex (`[r-B3]`).

The product is US/territory coastal only, so a miss (no intersecting feature) is the
common, expected case and reads as `None`, not an error. Like the other hurricane
clients the surge call carries the geocoded city centre; per §5 NO coordinate ever
reaches the log or a surfaced error message.
"""

from __future__ import annotations

import re

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0

# The renderer's band labels live as free text inside the `popupinfo` HTML blob; pull
# the first "Up to N ft" / "Above N ft" out of it (case-insensitive, flexible spacing).
_BAND_RE = re.compile(r"(Up to|Above)\s+\d+\s*ft", re.IGNORECASE)
_FEET_RE = re.compile(r"\d+")


class NhcSurgeError(RuntimeError):
    """The surge lookup hit a non-2xx, a transport failure, or a malformed body. The
    tool treats surge as best-effort and swallows this, so it is recoverable; its
    message carries no coordinate (§5)."""


def _band_match(text: str) -> str | None:
    """The first surge-band label in a `popupinfo` blob, normalized to the renderer's
    canonical casing ("Up to 9 ft" / "Above 12 ft"); None when the blob has none."""
    m = _BAND_RE.search(text)
    if m is None:
        return None
    prefix = "Above" if m.group(1).lower() == "above" else "Up to"
    feet = _FEET_RE.search(m.group(0))
    if feet is None:  # pragma: no cover - the band regex guarantees a digit
        return None
    return f"{prefix} {feet.group(0)} ft"


def _band_feet(band: str) -> int:
    """A band label → a comparable integer for picking the highest of several
    intersecting bands. "Up to 9 ft" → 9; "Above 12 ft" → 13 so an open-ended top band
    always sorts above the banded ones. 0 when the label carries no number."""
    feet = _FEET_RE.search(band)
    if feet is None:
        return 0
    n = int(feet.group(0))
    return n + 1 if band.lower().startswith("above") else n


class NhcSurgeClient:
    """Query the NHC Peak-Surge MapServer for the surge band at a point. The base URL is
    config-pinned (empty disables the source); `transport` is injectable so tests run
    against a MockTransport with no network (DEVELOPMENT.md "no network in tests")."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base)

    async def peak_band(self, lat: float, lon: float) -> str | None:
        """The peak storm-surge band at (lat, lon), or None when no surge product
        intersects the point (off-coast, inland, or no active product) or no band is
        parseable. If several features intersect, the highest band wins."""
        if not self._base:
            return None
        url = f"{self._base}/NHC_PeakStormSurge/MapServer/2/query"
        params = {
            "geometry": f"{lon},{lat}",  # ArcGIS point order is lon,lat
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "geojson",
        }
        body = await self._get(url, params)
        features = body.get("features") if isinstance(body, dict) else None
        if not isinstance(features, list):
            return None
        best: str | None = None
        for feature in features:
            band = _feature_band(feature)
            if band is not None and (best is None or _band_feet(band) > _band_feet(best)):
                best = band
        return best

    async def _get(self, url: str, params: dict[str, str]) -> object:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            # §5: no coordinate and no full request URL ever reach the log.
            log.warning("web.nhc_surge_failed", status=exc.response.status_code)
            raise NhcSurgeError("the storm-surge lookup is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.nhc_surge_failed", error=type(exc).__name__)
            raise NhcSurgeError("the storm-surge lookup is unavailable right now") from exc


def _feature_band(feature: object) -> str | None:
    """The surge band for one GeoJSON feature, read from its `popupinfo` text; None
    when the feature is shapeless or carries no parseable band."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties")
    if not isinstance(props, dict):
        return None
    popup = props.get("popupinfo")
    if not isinstance(popup, str):
        return None
    return _band_match(popup)
