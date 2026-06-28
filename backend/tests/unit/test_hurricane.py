"""jerv's `hurricane` tool + the NHC CurrentStorms client (docs/DESIGN.md
"hurricane_card tool-view"). HTTP is faked via MockTransport — no live network, like
the weather and web/search adapters."""

import httpx

from jbrain.agent.hurricanetools import build_hurricane_handlers
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
from jbrain.web.weather import WeatherClient, WeatherError

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

# Tampa, FL — the place we measure storms from. The weather geocoder builds the
# display name from name + admin1 + country.
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

# A single Category-3 hurricane in the Gulf, southwest of Tampa and within the
# "near" band. Intensity/pressure are strings in knots/mb as the real feed sends them.
_STORMS_OK = {
    "activeStorms": [
        {
            "id": "al052026",
            "name": "Elena",
            "classification": "HU",
            "intensity": "105",
            "pressure": "948",
            "latitude": "25.5N",
            "longitude": "85.0W",
            "latitudeNumeric": 25.5,
            "longitudeNumeric": -85.0,
            "movementDir": 30,
            "movementSpeed": 14,
            "lastUpdate": "2026-09-10T15:00:00.000Z",
        }
    ]
}

# Two storms: a distant tropical storm in the East Pacific and a near hurricane; the
# handler must pick the nearer one and report the other only as a count.
_STORMS_TWO = {
    "activeStorms": [
        {
            "id": "ep092026",
            "name": "Dora",
            "classification": "TS",
            "intensity": "50",
            "pressure": "995",
            "latitudeNumeric": 15.0,
            "longitudeNumeric": -130.0,
            "movementDir": 280,
            "movementSpeed": 12,
            "lastUpdate": "2026-09-10T15:00:00.000Z",
        },
        _STORMS_OK["activeStorms"][0],
    ]
}


def _clients(handler):  # type: ignore[no-untyped-def]
    """A hurricane client + weather (geocode) client sharing one MockTransport that
    routes by host: the geocoder host serves place lookups, everything else the NHC
    feed."""
    transport = httpx.MockTransport(handler)
    hurricane = HurricaneClient("https://nhc.test/CurrentStorms.json", transport=transport)
    weather = WeatherClient(
        "https://api.open-meteo.test", "https://geo.open-meteo.test", transport=transport
    )
    return hurricane, weather


def _geo_then(storms: dict):  # type: ignore[no-untyped-def]
    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            return httpx.Response(200, json=_GEO_TAMPA)
        return httpx.Response(200, json=storms)

    return handle


class FakeGeocoder:
    def __init__(self, hit: CityHit | None) -> None:
        self._hit = hit

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        return self._hit


def _tool(handler, geocoder=None):  # type: ignore[no-untyped-def]
    hurricane, weather = _clients(handler)
    geocoder = geocoder or FakeGeocoder(None)
    # FakeGeocoder is structural (it has nearest()); the handler never type-checks it.
    return build_hurricane_handlers(hurricane, weather, geocoder)["hurricane"]  # type: ignore[arg-type]


# --- pure helpers ----------------------------------------------------------


def test_classify_maps_codes() -> None:
    assert classify("HU") == "hurricane"
    assert classify("ts") == "tropical-storm"
    assert classify("TD") == "tropical-depression"
    assert classify("STS") == "subtropical-storm"
    assert classify("PTC") == "potential"
    assert classify("???") == "cyclone"  # unknown falls back


def test_category_from_knots() -> None:
    assert category(105, "hurricane") == "3"  # 96–112 kt
    assert category(140, "hurricane") == "5"
    assert category(64, "hurricane") == "1"
    assert category(63, "hurricane") == ""  # below hurricane strength
    assert category(105, "tropical-storm") == ""  # category only applies to hurricanes


def test_compass_and_geometry() -> None:
    assert compass(0) == "N"
    assert compass(225) == "SW"
    assert compass(360) == "N"
    # Tampa → a point due west is ~ W; due south is ~ S.
    assert compass(bearing_deg(27.9, -82.5, 27.9, -86.0)) == "W"
    assert compass(bearing_deg(27.9, -82.5, 24.0, -82.5)) == "S"
    # ~69 statute miles per degree of latitude.
    assert 68 <= haversine_mi(27.0, -82.0, 28.0, -82.0) <= 70


def test_sustained_mph_rounds_to_five() -> None:
    assert sustained_mph_for(105) == 120  # 105 kt = 120.8 mph → nearest 5
    assert sustained_mph_for(40) == 45  # 40 kt = 46.0 mph → nearest 5


def sustained_mph_for(kt: int):  # type: ignore[no-untyped-def]
    from jbrain.web.hurricane import ActiveStorm

    return sustained_mph(ActiveStorm("", "", "hurricane", kt, 0, 0.0, 0.0, -1, 0, ""))


def test_movement_label_and_stationary() -> None:
    from jbrain.web.hurricane import ActiveStorm

    moving = ActiveStorm("", "", "hurricane", 90, 950, 25.0, -85.0, 30, 14, "")
    assert movement(moving) == "NNE 14 mph"
    still = ActiveStorm("", "", "hurricane", 90, 950, 25.0, -85.0, -1, 0, "")
    assert movement(still) == "stationary"


def test_format_as_of_utc() -> None:
    assert format_as_of("2026-09-10T15:00:00.000Z") == "Sep 10, 3:00 PM UTC"
    assert format_as_of("2026-09-10T00:30:00.000Z") == "Sep 10, 12:30 AM UTC"
    assert format_as_of("not-a-date") == ""


# --- HurricaneClient -------------------------------------------------------


async def test_active_storms_parses_the_feed() -> None:
    hurricane, _ = _clients(lambda r: httpx.Response(200, json=_STORMS_OK))
    storms = await hurricane.active_storms()
    assert len(storms) == 1
    s = storms[0]
    assert (s.name, s.kind, s.wind_kt, s.pressure_mb) == ("Elena", "hurricane", 105, 948)
    assert (round(s.latitude, 1), round(s.longitude, 1)) == (25.5, -85.0)
    assert (s.move_dir, s.move_mph) == (30, 14)


async def test_active_storms_empty_off_season() -> None:
    hurricane, _ = _clients(lambda r: httpx.Response(200, json={"activeStorms": []}))
    assert await hurricane.active_storms() == ()


async def test_active_storms_parses_suffixed_coords_without_numeric() -> None:
    feed = {
        "activeStorms": [
            {"name": "X", "classification": "TS", "latitude": "12.0N", "longitude": "40.0W"}
        ]
    }
    hurricane, _ = _clients(lambda r: httpx.Response(200, json=feed))
    storms = await hurricane.active_storms()
    assert (storms[0].latitude, storms[0].longitude) == (12.0, -40.0)


async def test_active_storms_skips_rows_without_position() -> None:
    feed = {
        "activeStorms": [
            {"name": "NoPos", "classification": "TS"},
            _STORMS_OK["activeStorms"][0],
        ]
    }
    hurricane, _ = _clients(lambda r: httpx.Response(200, json=feed))
    storms = await hurricane.active_storms()
    assert [s.name for s in storms] == ["Elena"]


async def test_active_storms_http_error_is_recoverable() -> None:
    hurricane, _ = _clients(lambda r: httpx.Response(503))
    try:
        await hurricane.active_storms()
    except HurricaneError as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected HurricaneError")


async def test_active_storms_malformed_body_raises() -> None:
    hurricane, _ = _clients(lambda r: httpx.Response(200, json={"unexpected": 1}))
    try:
        await hurricane.active_storms()
    except HurricaneError as exc:
        assert "unexpected" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected HurricaneError")


def test_not_configured_reports_clearly() -> None:
    assert HurricaneClient("").configured is False


# --- the tool handler ------------------------------------------------------


async def test_named_place_returns_summary_and_view() -> None:
    out = await _tool(_geo_then(_STORMS_OK))({"location": "Tampa, FL"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "Hurricane Elena (Category 3)" in out
    assert "Tampa, Florida, United States" in out
    assert out.view is not None and out.view.view == "hurricane_card"
    data = out.view.data
    assert data["place"] == "Tampa, Florida, United States"
    assert data["storm"]["cat"] == "3"
    assert data["storm"]["sustained_mph"] == 120
    assert data["storm"]["pressure_mb"] == 948
    assert data["storm"]["moving"] == "NNE 14 mph"
    assert data["proximity"] == "near"
    assert data["bearing"] in {"SW", "SSW", "WSW"}
    assert 180 <= data["distance_mi"] <= 280
    assert data["active_count"] == 1
    # No coordinate rides the data-only payload (#9) — names + numbers only.
    assert "latitude" not in str(data) and "longitude" not in str(data)


async def test_picks_nearest_and_counts_others() -> None:
    out = await _tool(_geo_then(_STORMS_TWO))({"location": "Tampa, FL"}, CTX)
    assert isinstance(out, ToolOutput)
    assert out.view is not None
    assert out.view.data["storm"]["name"] == "Elena"  # the near one, not distant Dora
    assert out.view.data["active_count"] == 2
    assert "1 other active storm" in out  # the distant storm is a count, not the card


async def test_proximity_bands_by_distance() -> None:
    # A hurricane a few hundred miles out reads `regional`; one an ocean away reads
    # `distant`. Neither is `near` (reserved for a close, threatening system) and
    # neither is an official watch/warning.
    def storm_at(lat: float, lon: float) -> dict:
        return {
            "activeStorms": [
                {
                    "name": "Far",
                    "classification": "HU",
                    "intensity": "90",
                    "pressure": "960",
                    "latitudeNumeric": lat,
                    "longitudeNumeric": lon,
                    "movementDir": 0,
                    "movementSpeed": 10,
                    "lastUpdate": "2026-09-10T15:00:00.000Z",
                }
            ]
        }

    regional = await _tool(_geo_then(storm_at(22.0, -80.0)))({"location": "Tampa"}, CTX)
    assert regional.view.data["proximity"] == "regional"  # type: ignore[union-attr]
    distant = await _tool(_geo_then(storm_at(15.0, -130.0)))({"location": "Tampa"}, CTX)
    assert distant.view.data["proximity"] == "distant"  # type: ignore[union-attr]


async def test_stationary_storm_reads_in_summary() -> None:
    feed = {
        "activeStorms": [
            {
                "name": "Still",
                "classification": "TS",
                "intensity": "45",
                "pressure": "1000",
                "latitudeNumeric": 25.0,
                "longitudeNumeric": -84.0,
                "movementDir": -1,
                "movementSpeed": 0,
                "lastUpdate": "2026-09-10T15:00:00.000Z",
            }
        ]
    }
    out = await _tool(_geo_then(feed))({"location": "Tampa"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "nearly stationary" in out
    assert out.view.data["storm"]["moving"] == "stationary"  # type: ignore[union-attr]


async def test_storm_feed_error_surfaces_through_tool() -> None:
    # The geocode succeeds but the NHC feed is down — the handler returns the
    # recoverable HurricaneError text, not a card.
    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            return httpx.Response(200, json=_GEO_TAMPA)
        return httpx.Response(503)

    out = await _tool(handle)({"location": "Tampa"}, CTX)
    assert isinstance(out, str) and "unavailable" in out


async def test_no_active_storms_says_so() -> None:
    out = await _tool(_geo_then({"activeStorms": []}))({"location": "Tampa, FL"}, CTX)
    assert isinstance(out, str) and "No active tropical cyclones" in out


async def test_unknown_place_reports_not_found() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json=_STORMS_OK)

    out = await _tool(handle)({"location": "Atlantis"}, CTX)
    assert isinstance(out, str) and "Atlantis" in out


async def test_no_location_and_no_fix_asks_for_one() -> None:
    out = await _tool(_geo_then(_STORMS_OK))({}, CTX)  # CTX.here is None
    assert isinstance(out, str) and "name a city" in out


async def test_here_resolves_to_nearest_city_then_measures() -> None:
    # The owner's fix is resolved to a city NAME on-box first; only that public name
    # is geocoded — the precise fix never reaches an upstream (the location firewall).
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            requested.append(request.url.params.get("name", ""))
            return httpx.Response(200, json=_GEO_TAMPA)
        return httpx.Response(200, json=_STORMS_OK)

    geocoder = FakeGeocoder(CityHit("Tampa", "Florida", "United States", 800.0))
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), here=(27.95, -82.46)
    )
    out = await _tool(handle, geocoder)({}, ctx)
    assert isinstance(out, ToolOutput) and out.view is not None
    assert requested == ["Tampa"]  # the bare city name, not the raw coordinate


async def test_here_with_no_nearby_city_is_recoverable() -> None:
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), here=(0.0, -150.0))
    out = await _tool(_geo_then(_STORMS_OK), FakeGeocoder(None))({}, ctx)
    assert isinstance(out, str) and "nearby city" in out


async def test_geocode_upstream_error_is_recoverable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            return httpx.Response(503)
        return httpx.Response(200, json=_STORMS_OK)

    out = await _tool(handle)({"location": "Tampa"}, CTX)
    assert isinstance(out, str) and "unavailable" in out


def test_weather_error_type_is_reused_for_geocode() -> None:
    # The handler catches WeatherError from the shared geocoder; assert the type is
    # importable so the catch in build_hurricane_handlers stays honest.
    assert issubclass(WeatherError, Exception)
