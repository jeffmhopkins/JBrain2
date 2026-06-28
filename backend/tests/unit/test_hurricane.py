"""jerv's `hurricane` tool + the NHC CurrentStorms client (docs/DESIGN.md
"hurricane_card tool-view"; build plan docs/HURRICANE_TABS_PLAN.md). HTTP is faked via
MockTransport across every source — no live network and no real clock, like the
weather adapter. Covers the v1 vitals client and the v2 tabbed assembly (track
projection, NWS alert/timeline, impact, coverage degrade, and the location firewall)."""

import httpx

from jbrain.agent.hurricanetools import (
    _governing_alert,
    _pressure_level,
    _project,
    _rain_level,
    _surge_level,
    _wind_level,
    build_hurricane_handlers,
)
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.citygeocode import CityHit
from jbrain.db.session import SessionContext
from jbrain.web.hurricane import (
    HurricaneClient,
    HurricaneError,
    bearing_deg,
    category,
    classify,
    compass,
    format_as_of,
    haversine_mi,
    movement,
    sustained_mph,
)
from jbrain.web.nhc_gis import NhcGisClient, TrackPoint
from jbrain.web.nhc_surge import NhcSurgeClient
from jbrain.web.nws import Alert, NwsClient
from jbrain.web.weather import GeoHit, WeatherClient

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

# Tampa, FL — the place we measure storms from.
_GEO_TAMPA = {
    "results": [
        {
            "name": "Tampa",
            "admin1": "Florida",
            "country": "United States",
            "latitude": 27.9475,
            "longitude": -82.4584,
        }
    ]
}

# A single Category-3 hurricane in the Gulf, southwest of Tampa, within the "near" band.
_STORMS_OK = {
    "activeStorms": [
        {
            "id": "al052026",
            "name": "Elena",
            "classification": "HU",
            "intensity": "105",
            "pressure": "948",
            "latitudeNumeric": 25.5,
            "longitudeNumeric": -85.0,
            "movementDir": 30,
            "movementSpeed": 14,
            "lastUpdate": "2026-09-10T15:00:00.000Z",
        }
    ]
}

# NHC GIS: a layer catalog + Elena's forecast points and cone.
_GIS_LAYERS = {
    "layers": [
        {"id": 12, "name": "AT2 Forecast Points"},
        {"id": 13, "name": "AT2 Forecast Cone"},
    ]
}
_GIS_POINTS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "stormname": "Elena",
                "tau": 0,
                "maxwind": 105,
                "gust": 130,
                "ssnum": "3",
            },
            "geometry": {"type": "Point", "coordinates": [-85.0, 25.5]},
        },
        {
            "type": "Feature",
            "properties": {
                "stormname": "Elena",
                "tau": 24,
                "maxwind": 95,
                "gust": 115,
                "ssnum": "2",
            },
            "geometry": {"type": "Point", "coordinates": [-84.0, 27.5]},
        },
    ],
}
_GIS_CONE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"stormname": "Elena"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-86.0, 25.0], [-83.0, 25.0], [-83.0, 28.0], [-86.0, 28.0]]],
            },
        }
    ],
}

# NWS: a /points reference, a gridpoint with wind/gust/rain, and an alert.
_NWS_POINTS = {
    "properties": {
        "forecastGridData": "https://nws.test/gridpoints/TBW/71,98",
        "timeZone": "America/New_York",
    }
}
_NWS_GRID = {
    "properties": {
        "windSpeed": {
            "uom": "wmoUnit:km_h-1",
            "values": [
                {"validTime": "2026-09-10T16:00:00+00:00/PT6H", "value": 65.0},  # ~40 mph (TS)
                {"validTime": "2026-09-10T22:00:00+00:00/PT6H", "value": 125.0},  # ~78 mph (hurr)
            ],
        },
        "windGust": {
            "uom": "wmoUnit:km_h-1",
            "values": [
                {"validTime": "2026-09-10T16:00:00+00:00/PT12H", "value": 160.0}
            ],  # ~100 mph
        },
        "quantitativePrecipitation": {
            "uom": "wmoUnit:mm",
            "values": [{"validTime": "2026-09-10T16:00:00+00:00/PT6H", "value": 24.0}],  # ~0.94 in
        },
    }
}
_NWS_ALERTS = {
    "features": [
        {
            "properties": {
                "event": "Hurricane Warning",
                "headline": "Hurricane Warning for Tampa Bay",
            }
        },
        {"properties": {"event": "Flood Watch", "headline": "ignored — not tropical"}},
    ]
}
_SURGE = {
    "features": [{"properties": {"popupinfo": "<b>Peak Storm Surge</b> Up to 9 ft above ground"}}]
}


def _handler(*, storms=_STORMS_OK, gis="ok", nws="ok", surge="ok", record=None):  # type: ignore[no-untyped-def]
    """A MockTransport handler routing every source by host/path. `gis`/`nws`/`surge`
    switch a source to a failure/absence to exercise graceful degrade; `nws="404"`
    drives the out-of-coverage (global) path. Pass `record` (a list) to capture every
    requested path — used to assert which sources were (not) hit."""

    def handle(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if record is not None:
            record.append(path)
        if "geo." in host:
            return httpx.Response(200, json=_GEO_TAMPA)
        if "nhc.test" in host:  # CurrentStorms.json
            return httpx.Response(200, json=storms)
        if "gis.test" in host:
            if gis == "503":
                return httpx.Response(503)
            if "PeakStormSurge" in path:
                return httpx.Response(
                    503 if surge == "503" else 200,
                    json={"features": []} if surge == "none" else _SURGE,
                )
            if path.endswith("/layers"):
                return httpx.Response(200, json=_GIS_LAYERS)
            if "/12/query" in path:
                return httpx.Response(200, json=_GIS_POINTS)
            if "/13/query" in path:
                return httpx.Response(200, json=_GIS_CONE)
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        if "nws.test" in host:
            if nws == "404":
                return httpx.Response(404)
            if nws == "503":
                return httpx.Response(503)
            if "/alerts/active" in path:
                return httpx.Response(200, json=_NWS_ALERTS)
            if "/points/" in path:
                return httpx.Response(200, json=_NWS_POINTS)
            if "/gridpoints/" in path:
                return httpx.Response(200, json=_NWS_GRID)
        return httpx.Response(200, json={})

    return handle


class FakeGeocoder:
    def __init__(self, hit: CityHit | None) -> None:
        self._hit = hit

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        return self._hit


def _tool(handler, geocoder=None):  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    hurricane = HurricaneClient("https://nhc.test/CurrentStorms.json", transport=transport)
    weather = WeatherClient(
        "https://api.open-meteo.test", "https://geo.open-meteo.test", transport=transport
    )
    gis = NhcGisClient("https://gis.test/tropical", transport=transport)
    nws = NwsClient("https://nws.test", transport=transport)
    surge = NhcSurgeClient("https://gis.test/tropical", transport=transport)
    geocoder = geocoder or FakeGeocoder(None)
    # FakeGeocoder is structural; the handler never type-checks it.
    return build_hurricane_handlers(hurricane, weather, geocoder, gis, nws, surge)["hurricane"]  # type: ignore[arg-type]


def _client(handler):  # type: ignore[no-untyped-def]
    return HurricaneClient(
        "https://nhc.test/CurrentStorms.json", transport=httpx.MockTransport(handler)
    )


# --- pure helpers (jbrain.web.hurricane) -----------------------------------


def test_classify_maps_codes() -> None:
    assert classify("HU") == "hurricane"
    assert classify("ts") == "tropical-storm"
    assert classify("TD") == "tropical-depression"
    assert classify("STS") == "subtropical-storm"
    assert classify("PTC") == "potential"
    assert classify("???") == "cyclone"


def test_category_from_knots() -> None:
    assert category(105, "hurricane") == "3"
    assert category(140, "hurricane") == "5"
    assert category(64, "hurricane") == "1"
    assert category(63, "hurricane") == ""
    assert category(105, "tropical-storm") == ""


def test_compass_and_geometry() -> None:
    assert compass(0) == "N"
    assert compass(225) == "SW"
    assert compass(360) == "N"
    assert compass(bearing_deg(27.9, -82.5, 27.9, -86.0)) == "W"
    assert compass(bearing_deg(27.9, -82.5, 24.0, -82.5)) == "S"
    assert 68 <= haversine_mi(27.0, -82.0, 28.0, -82.0) <= 70


def test_movement_label_and_stationary() -> None:
    from jbrain.web.hurricane import ActiveStorm

    moving = ActiveStorm("", "", "hurricane", 90, 950, 25.0, -85.0, 30, 14, "")
    assert movement(moving) == "NNE 14 mph"
    still = ActiveStorm("", "", "hurricane", 90, 950, 25.0, -85.0, -1, 0, "")
    assert movement(still) == "stationary"


def test_sustained_mph_rounds_to_five() -> None:
    from jbrain.web.hurricane import ActiveStorm

    assert sustained_mph(ActiveStorm("", "", "hurricane", 105, 0, 0.0, 0.0, -1, 0, "")) == 120
    assert sustained_mph(ActiveStorm("", "", "hurricane", 40, 0, 0.0, 0.0, -1, 0, "")) == 45


def test_format_as_of_utc() -> None:
    assert format_as_of("2026-09-10T15:00:00.000Z") == "Sep 10, 3:00 PM UTC"
    assert format_as_of("2026-09-10T00:30:00.000Z") == "Sep 10, 12:30 AM UTC"
    assert format_as_of("not-a-date") == ""


# --- HurricaneClient (vitals feed) -----------------------------------------


async def test_active_storms_parses_the_feed() -> None:
    storms = await _client(lambda r: httpx.Response(200, json=_STORMS_OK)).active_storms()
    assert len(storms) == 1
    s = storms[0]
    assert (s.name, s.kind, s.wind_kt, s.pressure_mb) == ("Elena", "hurricane", 105, 948)
    assert (round(s.latitude, 1), round(s.longitude, 1)) == (25.5, -85.0)
    assert (s.move_dir, s.move_mph) == (30, 14)


async def test_active_storms_empty_off_season() -> None:
    assert (
        await _client(lambda r: httpx.Response(200, json={"activeStorms": []})).active_storms()
        == ()
    )


async def test_active_storms_http_error_is_recoverable() -> None:
    try:
        await _client(lambda r: httpx.Response(503)).active_storms()
    except HurricaneError as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected HurricaneError")


async def test_active_storms_malformed_body_raises() -> None:
    try:
        await _client(lambda r: httpx.Response(200, json={"unexpected": 1})).active_storms()
    except HurricaneError as exc:
        assert "unexpected" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected HurricaneError")


def test_not_configured_reports_clearly() -> None:
    assert HurricaneClient("").configured is False


# --- the tool handler: full US assembly ------------------------------------


async def test_full_us_assembly_builds_every_tab() -> None:
    out = await _tool(_handler())({"location": "Tampa, FL"}, CTX)
    assert isinstance(out, ToolOutput)
    assert out.view is not None and out.view.view == "hurricane_card"
    data = out.view.data
    # vitals + hero gust from the GIS earliest point (130 kt → 150 mph)
    assert data["storm"]["cat"] == "3"
    assert data["storm"]["sustained_mph"] == 120
    assert data["storm"]["gust_mph"] == 150
    # severity tiers track the real vitals (the card's Storm-stats gauges read these)
    assert data["storm"]["sustained_level"] == "extreme"  # 120 mph
    assert data["storm"]["gust_level"] == "extreme"  # 150 mph
    assert data["storm"]["pressure_level"] == "high"  # 948 mb
    assert data["coverage"] == "us"
    # official alert (the non-tropical Flood Watch is dropped)
    assert data["alert"]["level"] == "warning" and data["alert"]["kind"] == "hurricane"
    assert data["alert"]["event"] == "Hurricane Warning"
    # track projected to [0,1], cone present, you pin present
    assert data["track"] and all(0 <= p["x"] <= 1 and 0 <= p["y"] <= 1 for p in data["track"])
    assert data["track"][0]["label"] == "Now" and data["track"][0]["cat"] == "3"
    assert data["cone"] and 0 <= data["you"]["x"] <= 1
    # timeline + arrival + impact
    assert data["timeline"]
    assert data["arrival"]["ts_force"] is not None
    assert data["arrival"]["hurricane_force"] is not None
    assert data["impact"]["wind"]["gust"] >= 95
    assert data["impact"]["surge"]["band"] == "Up to 9 ft"
    # summary mentions the official alert
    assert "Hurricane Warning" in out
    # FIREWALL: no coordinate rides the data-only payload (#9)
    assert "latitude" not in str(data) and "longitude" not in str(data)
    assert "25.5" not in str(data) and "-85.0" not in str(data)


async def test_non_us_degrades_to_global_and_skips_surge() -> None:
    paths: list[str] = []
    out = await _tool(_handler(nws="404", record=paths))({"location": "Tampa, FL"}, CTX)
    assert isinstance(out, ToolOutput)
    data = out.view.data  # type: ignore[union-attr]
    assert data["coverage"] == "global"
    assert data["alert"] is None
    assert data["timeline"] == []
    assert data["impact"] == {}
    # track still present (NHC GIS is global)
    assert data["track"]
    # the surge MapServer is NEVER queried for an out-of-coverage point (no egress)
    assert not any("PeakStormSurge" in p for p in paths)
    assert "outside NWS coverage" in out


async def test_gis_down_still_renders_hero_and_nws() -> None:
    out = await _tool(_handler(gis="503"))({"location": "Tampa, FL"}, CTX)
    data = out.view.data  # type: ignore[union-attr]
    assert data["track"] == [] and data["cone"] == []  # GIS failed
    assert data["storm"]["gust_mph"] == 0  # no GIS → no hero gust
    assert data["coverage"] == "us" and data["alert"] is not None  # NWS still rendered


async def test_nws_transient_5xx_stays_us_with_empty_timeline() -> None:
    out = await _tool(_handler(nws="503"))({"location": "Tampa, FL"}, CTX)
    data = out.view.data  # type: ignore[union-attr]
    # a blip is NOT out-of-coverage: coverage stays us, timeline/alert just empty
    assert data["coverage"] == "us"
    assert data["timeline"] == [] and data["alert"] is None


async def test_surge_absent_leaves_no_surge_impact() -> None:
    out = await _tool(_handler(surge="none"))({"location": "Tampa, FL"}, CTX)
    assert "surge" not in out.view.data["impact"]  # type: ignore[union-attr]


async def test_no_active_storms_says_so() -> None:
    out = await _tool(_handler(storms={"activeStorms": []}))({"location": "Tampa, FL"}, CTX)
    assert isinstance(out, str) and "No active tropical cyclones" in out


async def test_unknown_place_reports_not_found() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json=_STORMS_OK)

    out = await _tool(handle)({"location": "Atlantis"}, CTX)
    assert isinstance(out, str) and "Atlantis" in out


async def test_no_location_and_no_fix_asks_for_one() -> None:
    out = await _tool(_handler())({}, CTX)
    assert isinstance(out, str) and "name a city" in out


async def test_storm_feed_error_surfaces_through_tool() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            return httpx.Response(200, json=_GEO_TAMPA)
        if "nhc.test" in request.url.host:
            return httpx.Response(503)
        return httpx.Response(200, json={})

    out = await _tool(handle)({"location": "Tampa"}, CTX)
    assert isinstance(out, str) and "unavailable" in out


# --- the location firewall -------------------------------------------------


async def test_here_resolves_to_nearest_city_then_measures() -> None:
    # The owner's fix resolves to a city NAME on-box; only that public name is geocoded,
    # and every off-box detail call uses the geocoded centre — never the precise fix.
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            requested.append(request.url.params.get("name", ""))
            return httpx.Response(200, json=_GEO_TAMPA)
        return _handler()(request)

    geocoder = FakeGeocoder(CityHit("Tampa", "Florida", "United States", 800.0))
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), here=(27.9999, -82.4999)
    )
    out = await _tool(handle, geocoder)({}, ctx)
    assert isinstance(out, ToolOutput)
    assert requested == ["Tampa"]  # the bare city name, not the raw coordinate


async def test_here_precise_fix_never_egresses_to_detail_feeds() -> None:
    # The precise fix (27.9999, -82.4999) must never appear in any NWS/surge request;
    # only the geocoded city centre (27.9475, -82.4584) is sent off-box.
    urls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "nws.test" in request.url.host or "PeakStormSurge" in request.url.path:
            urls.append(str(request.url))
        if "geo." in request.url.host:
            return httpx.Response(200, json=_GEO_TAMPA)
        return _handler()(request)

    geocoder = FakeGeocoder(CityHit("Tampa", "Florida", "United States", 800.0))
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), here=(27.9999, -82.4999)
    )
    out = await _tool(handle, geocoder)({}, ctx)
    blob = " ".join(urls)
    assert "27.9999" not in blob and "82.4999" not in blob  # precise fix never leaves
    assert urls and "27.9475" in blob  # the city centre is what's sent
    # The projection input is the city centre too: the precise fix never reaches the
    # `you` pin / payload (it is consumed only by city_geocoder.nearest, on-box).
    assert "27.9999" not in str(out.view.data) and "82.4999" not in str(out.view.data)  # type: ignore[union-attr]


# --- projection + level helpers (pure) -------------------------------------


def _tp(lat: float, lon: float, label: str = "Now", cat: str = "3") -> TrackPoint:
    return TrackPoint(lat, lon, "", 0, 100, 120, 950, cat, label, False)


def test_project_maps_bbox_to_unit_square_north_up() -> None:
    # A track from (lat 25,lon -86) to (lat 28,lon -83); you at the centre.
    track = (_tp(25.0, -86.0, "Now"), _tp(28.0, -83.0, "+24h"))
    you = GeoHit("X", 26.5, -84.5)
    tx, cone, you_xy = _project(track, (), you)
    assert cone == []
    # north-up: the higher-latitude point has the SMALLER y.
    assert tx[0]["y"] > tx[1]["y"]
    # east is +x: the more-eastern (less negative lon) point has larger x.
    assert tx[1]["x"] > tx[0]["x"]
    # the centre `you` projects near the middle.
    assert abs(you_xy["x"] - 0.5) < 0.01 and abs(you_xy["y"] - 0.5) < 0.01
    # everything inside the unit square.
    assert all(0 <= p["x"] <= 1 and 0 <= p["y"] <= 1 for p in tx)


def test_project_single_point_is_centered() -> None:
    # A degenerate bbox (one point, you co-located) must not divide by ~zero.
    track = (_tp(25.0, -85.0),)
    you = GeoHit("X", 25.0, -85.0)
    tx, _, you_xy = _project(track, (), you)
    assert tx[0] == {"x": 0.5, "y": 0.5, "label": "Now", "cat": "3", "past": False}
    assert you_xy == {"x": 0.5, "y": 0.5}


def test_project_normalises_antimeridian() -> None:
    # A storm straddling ±180° must project without a spurious ~360° span collapsing it.
    track = (_tp(10.0, 178.0, "Now"), _tp(12.0, -178.0, "+24h"))
    you = GeoHit("X", 11.0, 179.0)
    tx, _, you_xy = _project(track, (), you)
    # the two points are 4° apart across the seam, so they land well apart, not on top.
    assert abs(tx[0]["x"] - tx[1]["x"]) > 0.3
    assert all(0 <= p["x"] <= 1 for p in tx) and 0 <= you_xy["x"] <= 1


def test_governing_alert_precedence() -> None:
    watch = Alert("Hurricane Watch", "watch", "hurricane", "")
    warn_ts = Alert("Tropical Storm Warning", "warning", "tropical-storm", "")
    warn_hu = Alert("Hurricane Warning", "warning", "hurricane", "h")
    # a warning outranks a watch; among warnings hurricane outranks tropical-storm.
    assert _governing_alert((watch, warn_ts, warn_hu))["kind"] == "hurricane"  # type: ignore[index]
    assert _governing_alert((watch, warn_ts))["level"] == "warning"  # type: ignore[index]
    assert _governing_alert(()) is None


def test_level_helpers() -> None:
    assert _wind_level(120) == "extreme" and _wind_level(80) == "high"
    assert _wind_level(45) == "moderate" and _wind_level(20) == "low"
    assert _rain_level(13) == "extreme" and _rain_level(7) == "high"
    assert _rain_level(4) == "moderate" and _rain_level(1) == "low"
    assert _surge_level("Above 12 ft") == "extreme" and _surge_level("Up to 9 ft") == "high"
    assert _surge_level("Up to 6 ft") == "high" and _surge_level("Up to 3 ft") == "moderate"
    # pressure is inverse — lower is stronger; unknown (0) reads low
    assert _pressure_level(915) == "extreme" and _pressure_level(940) == "high"
    assert _pressure_level(970) == "moderate" and _pressure_level(1000) == "low"
    assert _pressure_level(0) == "low"
    assert _surge_level("Up to 1 ft") == "low"
