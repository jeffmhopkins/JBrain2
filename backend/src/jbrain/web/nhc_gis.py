"""Forecast track points + cone polygon for an active storm via the NHC tropical
ArcGIS MapServer (docs/archive/HURRICANE_TABS_PLAN.md §1/§3, the `NhcGisClient` bullet).

This is the only off-box source in the hurricane card that carries NO location: the
MapServer is queried by storm IDENTITY (stormname / basin+number), never by the
owner's coordinate, so nothing here touches the location firewall. The tool that
consumes this client does the projection into `[0,1]`, unit conversion, and payload
assembly — this module only fetches and parses absolute lon/lat + attributes.

Layer selection is NAME-BASED, never arithmetic (`[r-B1]`): per-storm layer groups
exist but are not spaced by a fixed offset, so we fetch the MapServer's `layers`
catalog, keep the layers whose `name` ends in "Forecast Points" / "Forecast Cone",
and bind one to our storm by matching its features' identity fields (`[r-B2]`). A
name guard on the chosen layer protects against an NHC reshuffle silently drawing the
wrong geometry.

The base URL is config-pinned (free, no API key); empty disables the source so a
fresh box degrades gracefully (the tool simply omits the track/cone).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0


class NhcGisError(RuntimeError):
    """The NHC GIS MapServer was unreachable, returned a non-2xx, or sent a malformed
    body. Surfaced to the agent as a recoverable error (like HurricaneError); the
    message never carries a coordinate — this source is queried by storm identity, so
    there is none to leak, and we keep the no-coordinate discipline regardless."""


@dataclass(frozen=True)
class TrackPoint:
    """One NHC forecast point: absolute position plus the vitals the card shows. Wind
    and gust stay in KNOTS as the feed reports them (the tool converts to mph). `tau`
    is the forecast hour (0/12/24/…); the earliest carries the analysis position and
    is labelled "Now"."""

    latitude: float
    longitude: float
    valid_time: str  # the feed's `validtime` string, verbatim (no clock here)
    tau: int  # forecast hour: 0/12/24/…/120
    max_wind_kt: int
    gust_kt: int
    mslp_mb: int
    ss_cat: str  # Saffir-Simpson number from `ssnum`, "" when absent
    label: str  # short deterministic label derived from tau ("Now" / "+12h")
    past: bool  # always False — this layer is forecast-only


def _decompose_id(storm_id: str) -> tuple[str, str]:
    """An NHC storm id ("al092024") → its (basin, stormnum) identity pair ("AL", "09"),
    the fallback match when a feature carries no `stormname`. The basin is the first two
    chars upper-cased; the number is the next two. Short/empty ids yield ("", "")."""
    s = storm_id.strip()
    if len(s) < 4:
        return "", ""
    return s[:2].upper(), s[2:4]


def _i(value: object) -> int:
    """Round a JSON number/numeric-string to int, defaulting 0 for None/non-numeric —
    the feed sends some fields as strings and may omit others."""
    try:
        return round(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _coord(value: object) -> float | None:
    """A single GeoJSON coordinate component → float, or None when unparseable so the
    caller can skip a malformed feature rather than crash."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ends_with(name: object, suffix: str) -> bool:
    """Case-sensitive endswith on a possibly-missing layer name (NHC uses exactly
    "Forecast Points" / "Forecast Cone")."""
    return isinstance(name, str) and name.endswith(suffix)


def _feature_matches_storm(props: object, name: str, basin: str, stormnum: str) -> bool:
    """Does a feature belong to our storm? Prefer an exact (case-insensitive)
    `stormname` match; fall back to basin + stormnum from the decomposed id."""
    if not isinstance(props, dict):
        return False
    sn = props.get("stormname")
    if isinstance(sn, str) and name and sn.strip().casefold() == name.casefold():
        return True
    if not basin or not stormnum:
        return False
    fb = props.get("basin")
    fnum = props.get("stormnum")
    fb_ok = isinstance(fb, str) and fb.strip().upper() == basin
    fnum_ok = fnum is not None and str(fnum).strip().zfill(2) == stormnum
    return fb_ok and fnum_ok


def _label_for(tau: int, earliest: bool) -> str:
    """A short deterministic label for a forecast point — "Now" for the earliest point,
    else "+{tau}h". No real clock, so tests are stable."""
    if earliest:
        return "Now"
    return f"+{tau}h"


class NhcGisClient:
    """Discover and parse a storm's forecast track + cone from the NHC tropical
    MapServer. The base URL is config-pinned; `transport` is injectable so tests run
    against a MockTransport with no network (DEVELOPMENT.md "no network in tests")."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base)

    async def forecast_track(self, storm: object) -> tuple[TrackPoint, ...]:
        """The storm's forecast points, tau-sorted (earliest first, labelled "Now").
        An empty tuple means no Forecast-Points layer is bound to this storm yet (a
        valid state — a storm may have no GIS product), NOT an error. The tool reads
        `track[0].gust_kt` for the hero gust, so the sort matters."""
        name, basin, stormnum = self._identity(storm)
        if not name and not basin:
            return ()
        layer_id = await self._find_layer(name, basin, stormnum, "Forecast Points")
        if layer_id is None:
            return ()
        fc = await self._query_layer(layer_id)
        points = [p for p in (_parse_track_point(f) for f in _features(fc)) if p is not None]
        points.sort(key=lambda p: p.tau)
        if not points:
            return ()
        earliest_tau = points[0].tau
        return tuple(_relabel(p, earliest=p.tau == earliest_tau) for p in points)

    async def cone(self, storm: object) -> tuple[tuple[float, float], ...]:
        """The storm's forecast-cone outer ring as (lon, lat) pairs. Empty tuple when no
        Forecast-Cone layer is bound to the storm or it carries no polygon — the track
        still draws without a cone (cones can lag points)."""
        name, basin, stormnum = self._identity(storm)
        if not name and not basin:
            return ()
        layer_id = await self._find_layer(name, basin, stormnum, "Forecast Cone")
        if layer_id is None:
            return ()
        fc = await self._query_layer(layer_id)
        for feature in _features(fc):
            ring = _outer_ring(feature)
            if ring:
                return ring
        return ()

    def _identity(self, storm: object) -> tuple[str, str, str]:
        """The storm's match keys: display name plus the basin/number decomposed from
        its id. Read structurally so the tool can pass any object carrying name + id."""
        name = str(getattr(storm, "name", "") or "").strip()
        basin, stormnum = _decompose_id(str(getattr(storm, "id", "") or ""))
        return name, basin, stormnum

    async def _find_layer(self, name: str, basin: str, stormnum: str, suffix: str) -> int | None:
        """The id of the layer whose `name` ends in `suffix` AND whose features belong
        to this storm. Returns None when no such layer exists (storm-not-found is not an
        error). Guards that the chosen layer's name really ends in `suffix` — never trust
        layer arithmetic (`[r-B1]`)."""
        catalog = await self._get(f"{self._base}/NHC_tropical_weather/MapServer/layers?f=json")
        layers = catalog.get("layers") if isinstance(catalog, dict) else None
        if not isinstance(layers, list):
            raise NhcGisError("the NHC GIS service returned an unexpected response")
        for layer in layers:
            if not isinstance(layer, dict) or not _ends_with(layer.get("name"), suffix):
                continue
            layer_id = layer.get("id")
            if not isinstance(layer_id, int):
                continue
            # Only name-matched layers reach here (the `continue` above is the guard),
            # so a returned layer's name always ends in `suffix` — never layer
            # arithmetic, so an NHC reshuffle can't draw the wrong geometry (`[r-B1]`).
            fc = await self._query_layer(layer_id)
            if any(_feature_matches_storm(_props(f), name, basin, stormnum) for f in _features(fc)):
                return layer_id
        return None

    async def _query_layer(self, layer_id: int) -> object:
        """Query one MapServer layer as a GeoJSON FeatureCollection."""
        url = (
            f"{self._base}/NHC_tropical_weather/MapServer/{layer_id}/query"
            "?where=1=1&outFields=*&returnGeometry=true&f=geojson"
        )
        return await self._get(url)

    async def _get(self, url: str) -> object:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            # No coordinate is logged — this source carries none, and we keep the
            # discipline so the log line is identical regardless of the request.
            log.warning("web.nhc_gis_failed", status=exc.response.status_code, error=repr(exc))
            raise NhcGisError("the NHC GIS service is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.nhc_gis_failed", error=repr(exc))
            raise NhcGisError("the NHC GIS service is unavailable right now") from exc


def _features(fc: object) -> list[object]:
    """The `features` list of a GeoJSON FeatureCollection, or [] when malformed —
    defensive so a bad body yields no points rather than a crash."""
    if not isinstance(fc, dict):
        return []
    feats = fc.get("features")
    return feats if isinstance(feats, list) else []


def _props(feature: object) -> object:
    """A feature's `properties` object (may be any shape; callers validate)."""
    return feature.get("properties") if isinstance(feature, dict) else None


def _parse_track_point(feature: object) -> TrackPoint | None:
    """Shape one GeoJSON Point feature into a TrackPoint. Defensive: a feature missing a
    usable geometry is skipped rather than crashing the whole track."""
    if not isinstance(feature, dict):
        return None
    geom = feature.get("geometry")
    if not isinstance(geom, dict):
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    lon = _coord(coords[0])
    lat = _coord(coords[1])
    if lon is None or lat is None:
        return None
    props = feature.get("properties")
    props = props if isinstance(props, dict) else {}
    tau = _i(props.get("tau"))
    return TrackPoint(
        latitude=lat,
        longitude=lon,
        valid_time=str(props.get("validtime") or "").strip(),
        tau=tau,
        max_wind_kt=_i(props.get("maxwind")),
        gust_kt=_i(props.get("gust")),
        mslp_mb=_i(props.get("mslp")),
        ss_cat=str(props.get("ssnum") or "").strip(),
        label=_label_for(tau, earliest=False),
        past=False,
    )


def _relabel(point: TrackPoint, earliest: bool) -> TrackPoint:
    """Return `point` with its label fixed up now that we know whether it is the earliest
    tau in the sorted track (only the earliest reads "Now")."""
    return TrackPoint(
        latitude=point.latitude,
        longitude=point.longitude,
        valid_time=point.valid_time,
        tau=point.tau,
        max_wind_kt=point.max_wind_kt,
        gust_kt=point.gust_kt,
        mslp_mb=point.mslp_mb,
        ss_cat=point.ss_cat,
        label=_label_for(point.tau, earliest=earliest),
        past=point.past,
    )


def _outer_ring(feature: object) -> tuple[tuple[float, float], ...]:
    """The outer ring of a GeoJSON Polygon / MultiPolygon feature as (lon, lat) pairs,
    or () when the geometry is absent or malformed. The cone is one polygon; for a
    MultiPolygon we take the first part's outer ring."""
    if not isinstance(feature, dict):
        return ()
    geom = feature.get("geometry")
    if not isinstance(geom, dict):
        return ()
    coords = geom.get("coordinates")
    gtype = geom.get("type")
    ring: object = None
    if gtype == "Polygon" and isinstance(coords, (list, tuple)) and coords:
        ring = coords[0]
    elif gtype == "MultiPolygon" and isinstance(coords, (list, tuple)) and coords:
        first = coords[0]
        if isinstance(first, (list, tuple)) and first:
            ring = first[0]
    if not isinstance(ring, (list, tuple)):
        return ()
    out: list[tuple[float, float]] = []
    for pair in ring:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        lon = _coord(pair[0])
        lat = _coord(pair[1])
        if lon is not None and lat is not None:
            out.append((lon, lat))
    return tuple(out)
