"""jerv's `weather` tool + the Open-Meteo client (docs/DESIGN.md "weather_card
tool-view"). HTTP is faked via MockTransport — no live network, like the web/search
and connector adapters."""

import httpx

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.weathertools import build_weather_handlers
from jbrain.citygeocode import CityHit
from jbrain.db.session import SessionContext
from jbrain.web.weather import WeatherClient, WeatherError, _compass, _hour_label, describe_code

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

_GEO_OK = {
    "results": [
        {
            "name": "Cocoa",
            "admin1": "Florida",
            "country": "United States",
            "latitude": 28.3861,
            "longitude": -80.7420,
        },
    ]
}

_FORECAST_OK = {
    "timezone_abbreviation": "EDT",
    "current": {
        "time": "2026-06-26T13:14",
        "temperature_2m": 90.4,
        "apparent_temperature": 101.8,
        "relative_humidity_2m": 71,
        "weather_code": 95,
        "wind_speed_10m": 8.1,
        "wind_direction_10m": 135,
        "is_day": 1,
    },
    "hourly": {
        "time": [
            "2026-06-26T13:00",
            "2026-06-26T14:00",
            "2026-06-26T15:00",
            "2026-06-26T23:00",
            "2026-06-27T00:00",
        ],
        "temperature_2m": [90, 91, 91, 81, 80],
        "apparent_temperature": [102, 103, 103, 87, 86],
        "weather_code": [95, 95, 80, 1, 1],
        "precipitation_probability": [20, 25, 35, 0, 0],
        "wind_speed_10m": [8, 9, 10, 3, 3],
        "wind_direction_10m": [135, 135, 140, 180, 185],
        "is_day": [1, 1, 1, 0, 0],
    },
    "daily": {"temperature_2m_max": [92], "temperature_2m_min": [80]},
}


def _client(handler) -> WeatherClient:  # type: ignore[no-untyped-def]
    return WeatherClient(
        "https://api.open-meteo.test",
        "https://geo.open-meteo.test",
        transport=httpx.MockTransport(handler),
    )


def _both_ok(request: httpx.Request) -> httpx.Response:
    if "geocoding" in request.url.host or "geo." in request.url.host:
        return httpx.Response(200, json=_GEO_OK)
    return httpx.Response(200, json=_FORECAST_OK)


# --- pure helpers ----------------------------------------------------------


def test_compass_and_codes() -> None:
    assert _compass(135) == "SE"
    assert _compass(0) == "N"
    assert _compass(360) == "N"
    assert describe_code(95) == ("storm", "Thunderstorms")
    assert describe_code(0) == ("clear", "Clear")
    assert describe_code(99999) == ("cloudy", "Cloudy")  # unknown → cloudy fallback


def test_hour_label_midnight_and_noon() -> None:
    from datetime import datetime

    assert _hour_label(datetime(2026, 6, 26, 0, 0)) == "12a"
    assert _hour_label(datetime(2026, 6, 26, 13, 0)) == "1p"
    assert _hour_label(datetime(2026, 6, 26, 12, 0)) == "12p"


# --- WeatherClient ---------------------------------------------------------


async def test_geocode_builds_a_display_name() -> None:
    hit = await _client(lambda r: httpx.Response(200, json=_GEO_OK)).geocode("Cocoa")
    assert hit is not None
    assert hit.name == "Cocoa, Florida, United States"
    assert round(hit.latitude, 2) == 28.39


async def test_geocode_none_when_no_results() -> None:
    hit = await _client(lambda r: httpx.Response(200, json={"results": []})).geocode("Nowhere")
    assert hit is None


async def test_forecast_shapes_current_hourly_and_hilo() -> None:
    client = _client(_both_ok)
    hit = await client.geocode("Cocoa")
    assert hit is not None
    w = await client.forecast(hit)
    assert (w.temp_f, w.feels_f, w.humidity) == (90, 102, 71)
    assert (w.cond, w.label) == ("storm", "Thunderstorms")
    assert w.is_day is True
    assert (w.wind_mph, w.wind_dir) == (8, "SE")
    assert (w.hi_f, w.lo_f) == (92, 80)
    assert w.as_of == "1:14 PM" and w.tz_abbr == "EDT"
    # The strip starts at the current hour (13:00), not before it.
    assert w.hours[0].label == "1p" and w.hours[0].temp_f == 90
    # Night hours carry is_day False so the component can pick a night glyph.
    assert w.hours[-1].label == "12a" and w.hours[-1].is_day is False


async def test_forecast_http_error_is_recoverable() -> None:
    client = _client(lambda r: httpx.Response(503))
    try:
        await client.geocode("Cocoa")
    except WeatherError as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected WeatherError")


async def test_forecast_malformed_body_raises() -> None:
    hit = await _client(lambda r: httpx.Response(200, json=_GEO_OK)).geocode("Cocoa")
    assert hit is not None
    client = _client(lambda r: httpx.Response(200, json={"current": {}}))
    try:
        await client.forecast(hit)
    except WeatherError as exc:
        assert "incomplete" in str(exc) or "unexpected" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected WeatherError")


# --- the tool handler ------------------------------------------------------


class FakeGeocoder:
    def __init__(self, hit: CityHit | None) -> None:
        self._hit = hit

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        return self._hit


def _tool(handler, geocoder=None):  # type: ignore[no-untyped-def]
    geocoder = geocoder or FakeGeocoder(None)
    return build_weather_handlers(_client(handler), geocoder)["weather"]


async def test_named_place_returns_summary_and_view() -> None:
    out = await _tool(_both_ok)({"location": "Cocoa, FL"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "Cocoa, Florida, United States" in out
    assert "feels 102" in out and "high 92" in out
    assert out.view is not None and out.view.view == "weather_card"
    data = out.view.data
    assert data["now"]["cond"] == "storm"
    assert data["hi_f"] == 92 and data["lo_f"] == 80
    assert data["hours"][0]["label"] == "1p"
    # No coordinate rides the data-only payload (#9) — names + numbers only.
    assert "latitude" not in data and "lon" not in str(data)


async def test_unknown_place_reports_not_found() -> None:
    out = await _tool(lambda r: httpx.Response(200, json={"results": []}))(
        {"location": "Atlantis"}, CTX
    )
    assert isinstance(out, str) and "Atlantis" in out


async def test_no_location_and_no_fix_asks_for_one() -> None:
    out = await _tool(_both_ok)({}, CTX)  # CTX.here is None
    assert isinstance(out, str) and "name a city" in out


async def test_here_resolves_to_nearest_city_then_forecasts() -> None:
    # The owner's fix is resolved to a city NAME on-box first; only that public name
    # is geocoded — the precise fix never reaches the upstream (the location firewall).
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "geo." in request.url.host:
            requested.append(request.url.params.get("name", ""))
            return httpx.Response(200, json=_GEO_OK)
        return httpx.Response(200, json=_FORECAST_OK)

    geocoder = FakeGeocoder(CityHit("Cocoa", "Florida", "United States", 1200.0))
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), here=(28.41, -80.74)
    )
    out = await _tool(handle, geocoder)({}, ctx)
    assert isinstance(out, ToolOutput) and out.view is not None
    assert requested == ["Cocoa"]  # the bare city name, not the raw coordinate


async def test_here_with_no_nearby_city_is_recoverable() -> None:
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), here=(0.0, -150.0))
    out = await _tool(_both_ok, FakeGeocoder(None))({}, ctx)
    assert isinstance(out, str) and "nearby city" in out
