"""NhcGisClient — name-based layer discovery, storm-identity feature match, and
GeoJSON track/cone parse (docs/HURRICANE_TABS_PLAN.md §8). HTTP is faked via
MockTransport — no live network and no real clock, like the hurricane/weather
adapters."""

from dataclasses import FrozenInstanceError

import httpx
import pytest

from jbrain.web.nhc_gis import (
    NhcGisClient,
    NhcGisError,
    TrackPoint,
    _decompose_id,
)


class _Storm:
    """A minimal storm carrying the identity fields the client reads structurally
    (the real ActiveStorm has these plus more)."""

    def __init__(self, name: str, sid: str) -> None:
        self.name = name
        self.id = sid


ELENA = _Storm("Elena", "al092024")

_BASE = "https://gis.test/tropical"

# A MapServer `layers?f=json` catalog with two per-storm groups. Only one
# "Forecast Points" layer carries Elena's features; a same-suffix layer for a
# different storm must be rejected, and a "Forecast Track Line" layer (wrong suffix)
# must never be chosen even though its features match Elena.
_LAYERS = {
    "layers": [
        {"id": 10, "name": "AT1 Forecast Points"},  # belongs to a different storm
        {"id": 11, "name": "AT1 Forecast Track Line"},  # wrong suffix, but Elena's
        {"id": 12, "name": "AT2 Forecast Points"},  # Elena's points
        {"id": 13, "name": "AT2 Forecast Cone"},  # Elena's cone
    ]
}

# Elena's forecast points, deliberately OUT of tau order so the sort is exercised.
_POINTS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "stormname": "Elena",
                "basin": "AL",
                "stormnum": "09",
                "validtime": "12Z SEP 11",
                "tau": 24,
                "maxwind": 95,
                "gust": 115,
                "mslp": 955,
                "ssnum": "2",
                "tcdvlp": "HU",
            },
            "geometry": {"type": "Point", "coordinates": [-86.0, 26.0]},
        },
        {
            "type": "Feature",
            "properties": {
                "stormname": "Elena",
                "basin": "AL",
                "stormnum": "09",
                "validtime": "12Z SEP 10",
                "tau": 0,
                "maxwind": 105,
                "gust": 130,
                "mslp": 948,
                "ssnum": "3",
                "tcdvlp": "HU",
            },
            "geometry": {"type": "Point", "coordinates": [-85.0, 25.5]},
        },
    ],
}

# A different storm's forecast points (Dora), to live behind layer 10.
_POINTS_OTHER = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"stormname": "Dora", "basin": "EP", "stormnum": "05", "tau": 0},
            "geometry": {"type": "Point", "coordinates": [-130.0, 15.0]},
        }
    ],
}

_CONE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"stormname": "Elena", "basin": "AL", "stormnum": "09"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-85.0, 25.0], [-86.0, 26.0], [-84.0, 27.0], [-85.0, 25.0]]],
            },
        }
    ],
}


def _router(routes):  # type: ignore[no-untyped-def]
    """Route MockTransport requests by what the URL path/query contains, serving the
    catalog for `/layers` and per-layer FeatureCollections for `/{id}/query`."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/layers"):
            return httpx.Response(200, json=_LAYERS)
        for layer_id, body in routes.items():
            if f"/MapServer/{layer_id}/query" in path:
                return httpx.Response(200, json=body)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    return httpx.MockTransport(handle)


def _client(routes):  # type: ignore[no-untyped-def]
    return NhcGisClient(_BASE, transport=_router(routes))


# --- pure helpers ----------------------------------------------------------


def test_decompose_id_cases() -> None:
    assert _decompose_id("al092024") == ("AL", "09")
    assert _decompose_id("EP052026") == ("EP", "05")
    assert _decompose_id("cp01") == ("CP", "01")
    assert _decompose_id("") == ("", "")
    assert _decompose_id("al9") == ("", "")  # too short to decompose


def test_not_configured() -> None:
    assert NhcGisClient("").configured is False
    assert NhcGisClient(_BASE).configured is True


# --- forecast_track --------------------------------------------------------


async def test_forecast_track_picks_correct_layer_by_storm_match() -> None:
    routes = {10: _POINTS_OTHER, 12: _POINTS, 13: _CONE}
    track = await _client(routes).forecast_track(ELENA)
    # Two points parsed, sorted by tau (0 then 24), earliest labelled "Now".
    assert [p.tau for p in track] == [0, 24]
    assert track[0].label == "Now"
    assert track[1].label == "+24h"
    first = track[0]
    assert (round(first.latitude, 1), round(first.longitude, 1)) == (25.5, -85.0)
    assert (first.max_wind_kt, first.gust_kt, first.mslp_mb) == (105, 130, 948)
    assert first.ss_cat == "3"
    assert first.past is False
    # The hero gust the tool reads is the earliest point's gust.
    assert track[0].gust_kt == 130


async def test_forecast_track_rejects_wrong_suffix_layer() -> None:
    # Layer 11 ("Forecast Track Line") carries Elena's features but the wrong name
    # suffix; layer 12 is the real points layer. If discovery wrongly chose 11 we would
    # get its single feature. Assert we got the 2-point layer 12 instead.
    routes = {
        11: {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"stormname": "Elena", "tau": 0},
                    "geometry": {"type": "Point", "coordinates": [-99.0, 9.0]},
                }
            ],
        },
        12: _POINTS,
        13: _CONE,
    }
    track = await _client(routes).forecast_track(ELENA)
    assert len(track) == 2  # the points layer, not the 1-feature track-line layer
    assert round(track[0].longitude, 1) == -85.0  # not -99.0 from the wrong layer


async def test_forecast_track_matches_by_basin_and_num_without_stormname() -> None:
    # A feature with no stormname still binds via basin + stormnum from id[:4].
    points = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"basin": "AL", "stormnum": "09", "tau": 0, "gust": 100},
                "geometry": {"type": "Point", "coordinates": [-85.0, 25.5]},
            }
        ],
    }
    routes = {10: _POINTS_OTHER, 12: points, 13: _CONE}
    track = await _client(routes).forecast_track(ELENA)
    assert len(track) == 1
    assert track[0].gust_kt == 100


async def test_forecast_track_storm_not_found_returns_empty() -> None:
    # No layer's features belong to this storm → empty tuple, not an error.
    routes = {10: _POINTS_OTHER, 12: _POINTS_OTHER, 13: _POINTS_OTHER}
    unknown = _Storm("Zelda", "al992024")
    assert await _client(routes).forecast_track(unknown) == ()


async def test_forecast_track_skips_malformed_geometry() -> None:
    points = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"stormname": "Elena", "tau": 0}},  # no geom
            {
                "type": "Feature",
                "properties": {"stormname": "Elena", "tau": 12, "gust": 90},
                "geometry": {"type": "Point", "coordinates": [-85.0, 25.0]},
            },
        ],
    }
    routes = {12: points, 13: _CONE}
    track = await _client(routes).forecast_track(ELENA)
    assert len(track) == 1 and track[0].tau == 12 and track[0].label == "Now"


async def test_forecast_track_empty_when_no_id_or_name() -> None:
    assert await _client({}).forecast_track(_Storm("", "")) == ()


async def test_forecast_track_matched_layer_all_malformed_returns_empty() -> None:
    # A layer binds to the storm but every feature has unusable geometry → empty tuple
    # (a matched-but-pointless layer is not an error).
    points = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"stormname": "Elena", "tau": 0}}],
    }
    routes = {12: points, 13: _CONE}
    assert await _client(routes).forecast_track(ELENA) == ()


# --- cone ------------------------------------------------------------------


async def test_cone_parses_outer_ring() -> None:
    routes = {10: _POINTS_OTHER, 12: _POINTS, 13: _CONE}
    ring = await _client(routes).cone(ELENA)
    assert ring == ((-85.0, 25.0), (-86.0, 26.0), (-84.0, 27.0), (-85.0, 25.0))


async def test_cone_storm_not_found_returns_empty() -> None:
    routes = {13: _POINTS_OTHER}
    assert await _client(routes).cone(_Storm("Zelda", "al992024")) == ()


async def test_cone_empty_when_no_id_or_name() -> None:
    assert await _client({}).cone(_Storm("", "")) == ()


async def test_cone_multipolygon_first_ring() -> None:
    cone = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"stormname": "Elena"},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [[[[-85.0, 25.0], [-86.0, 26.0], [-85.0, 25.0]]]],
                },
            }
        ],
    }
    routes = {13: cone}
    ring = await _client(routes).cone(ELENA)
    assert ring == ((-85.0, 25.0), (-86.0, 26.0), (-85.0, 25.0))


# --- error handling --------------------------------------------------------


async def test_http_5xx_raises_nhc_gis_error_without_coordinate() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = NhcGisClient(_BASE, transport=httpx.MockTransport(handle))
    with pytest.raises(NhcGisError) as exc:
        await client.forecast_track(ELENA)
    msg = str(exc.value)
    assert "unavailable" in msg
    # No coordinate ever rides the surfaced error message.
    assert "25.5" not in msg and "-85.0" not in msg and "latitude" not in msg


async def test_bad_json_raises_nhc_gis_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}
        )

    client = NhcGisClient(_BASE, transport=httpx.MockTransport(handle))
    with pytest.raises(NhcGisError):
        await client.cone(ELENA)


async def test_cone_skips_malformed_polygon_then_empty() -> None:
    # A cone layer bound to the storm but whose feature has no usable polygon → ().
    cone = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"stormname": "Elena"},
                "geometry": {"type": "Polygon", "coordinates": []},  # empty rings
            }
        ],
    }
    routes = {13: cone}
    assert await _client(routes).cone(ELENA) == ()


async def test_layers_with_noninteger_id_skipped() -> None:
    # A catalog entry missing an int id is ignored; discovery still returns ().
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/layers"):
            return httpx.Response(
                200,
                json={"layers": [{"name": "AT1 Forecast Points"}, "junk"]},
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    client = NhcGisClient(_BASE, transport=httpx.MockTransport(handle))
    assert await client.forecast_track(ELENA) == ()


async def test_track_outer_ring_on_point_geometry_is_empty() -> None:
    # The cone parser tolerates a non-polygon geometry (returns no ring) — covered via
    # a cone layer whose feature is a Point.
    cone = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"stormname": "Elena"},
                "geometry": {"type": "Point", "coordinates": [-85.0, 25.0]},
            }
        ],
    }
    routes = {13: cone}
    assert await _client(routes).cone(ELENA) == ()


async def test_unexpected_catalog_shape_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/layers"):
            return httpx.Response(200, json={"unexpected": 1})
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    client = NhcGisClient(_BASE, transport=httpx.MockTransport(handle))
    with pytest.raises(NhcGisError):
        await client.forecast_track(ELENA)


def test_trackpoint_is_frozen() -> None:
    p = TrackPoint(25.0, -85.0, "", 0, 100, 120, 950, "3", "Now", False)
    with pytest.raises(FrozenInstanceError):
        p.tau = 12  # type: ignore[misc]
