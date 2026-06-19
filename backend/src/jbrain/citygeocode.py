"""An offline, in-process reverse geocoder (option 2 of the jerv location fallbacks).

Resolves a coordinate to the NEAREST populated place — city / region / country, never
a street — over the GeoNames cities bundled by `geonamescache` (population >= 15000,
~32k points). There is no resident index service, no on-disk index beyond the package
data, and no egress: the cost is ~0 RAM at rest (the data loads lazily on first use)
and a few MB once warm. A lookup ranks by a cheap equirectangular proxy, then takes a
single haversine on the winner, so a query is a few milliseconds.

This is the low-footprint stand-in for the on-box Photon geocoder (which wants a
resident Lucene engine + a multi-GB index). When a specific street address is needed,
the caller falls back to the owner-configured external reverse-geocoder.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

_EARTH_M = 6_371_000.0
# Beyond this, the nearest populated place is not a useful "where you are" — report
# the coordinate instead of claiming a city a long way off (open ocean, remote area).
_DEFAULT_MAX_KM = 150.0


@dataclass(frozen=True)
class CityHit:
    """The nearest populated place to a coordinate: names + distance only, never a
    coordinate. `region` is the US state name when known (GeoNames admin1), else ""."""

    name: str
    region: str
    country: str
    distance_m: float


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_M * math.asin(math.sqrt(a))


class CityGeocoder:
    """Nearest-city reverse geocoding over the bundled GeoNames cities. Thread-unsafe
    lazy load is fine: the loaders are idempotent and the loop is read-only, so a rare
    double-load on a cold concurrent burst only wastes a little work."""

    def __init__(self, max_km: float = _DEFAULT_MAX_KM) -> None:
        self._max_m = max_km * 1000.0
        self._lat = array("d")
        self._lon = array("d")
        self._meta: list[tuple[str, str, str]] = []  # (name, region, country)
        self._loaded = False

    def _load(self) -> None:
        import geonamescache

        gc = geonamescache.GeonamesCache()
        countries = {code: c.get("name", code) for code, c in gc.get_countries().items()}
        us_states = {s: v.get("name", s) for s, v in gc.get_us_states().items()}
        for city in gc.get_cities().values():
            try:
                lat = float(city["latitude"])
                lon = float(city["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            cc = city.get("countrycode", "")
            region = us_states.get(city.get("admin1code", ""), "") if cc == "US" else ""
            self._lat.append(lat)
            self._lon.append(lon)
            self._meta.append((city.get("name", ""), region, countries.get(cc, cc)))
        self._loaded = True
        log.info("citygeocode.loaded", cities=len(self._meta))

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        """The nearest populated place within `max_km`, or None when there is none that
        close (the caller then reports the coordinate)."""
        if not self._loaded:
            self._load()
        if not self._meta:
            return None
        coslat = math.cos(math.radians(lat))
        lats, lons = self._lat, self._lon
        best_i, best_d2 = -1, math.inf
        for i in range(len(lats)):
            dy = lats[i] - lat
            dx = (lons[i] - lon) * coslat
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2, best_i = d2, i
        dist = _haversine_m(lat, lon, lats[best_i], lons[best_i])
        if dist > self._max_m:
            return None
        name, region, country = self._meta[best_i]
        return CityHit(name=name, region=region, country=country, distance_m=dist)
