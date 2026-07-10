"""jerv's `weather_history` tool + the Open-Meteo Archive client
(docs/reference/ASSISTANT.md "Agent selection"). HTTP is faked via MockTransport — no
live network, like the forecast weather tool and the web/search adapters. The NWS
heat-index math is checked against the published reference values."""

from datetime import date

import httpx

from jbrain.agent.loop import ToolContext
from jbrain.agent.weatherhistorytools import build_weather_history_handlers
from jbrain.citygeocode import CityHit
from jbrain.db.session import SessionContext
from jbrain.web.weather import WeatherClient, WeatherError
from jbrain.web.weather_history import (
    WeatherHistoryClient,
    _daily_series,
    _hourly_series,
    _reduce,
    heat_index_f,
    parse_iso_date,
)

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

_GEO_OK = {
    "results": [
        {
            "name": "Titusville",
            "admin1": "Florida",
            "country": "United States",
            "latitude": 28.6122,
            "longitude": -80.8075,
        }
    ]
}

# Two days of hourly data: a hot/humid day (peaks in the Danger band) and a mild day
# (heat index tracks temperature). Enough to exercise the daily-peak grouping and every
# reduced dimension (dew point, precip, wind, gusts, cloud, pressure, sunshine).
_ARCHIVE_OK = {
    "hourly": {
        "time": [
            "2023-07-01T00:00",
            "2023-07-01T12:00",
            "2023-07-01T15:00",
            "2023-07-02T06:00",
            "2023-07-02T14:00",
        ],
        "temperature_2m": [80, 95, 96, 74, 84],
        "relative_humidity_2m": [80, 65, 70, 90, 55],
        "dew_point_2m": [73, 74, 75, 71, 66],
        "precipitation": [0.0, 0.1, 0.2, 0.0, 0.0],
        "wind_speed_10m": [4, 8, 10, 3, 12],
        "wind_gusts_10m": [9, 18, 22, 6, 20],
        "cloud_cover": [40, 60, 80, 20, 30],
        "surface_pressure": [1015, 1013, 1012, 1016, 1014],
    },
    "daily": {
        "time": ["2023-07-01", "2023-07-02"],
        "temperature_2m_max": [96, 88],
        "temperature_2m_min": [76, 72],
        "precipitation_sum": [0.30, 0.0],
        "rain_sum": [0.30, 0.0],
        "snowfall_sum": [0.0, 0.0],
        "precipitation_hours": [2, 0],
        "wind_speed_10m_max": [10, 12],
        "wind_gusts_10m_max": [22, 20],
        "wind_direction_10m_dominant": [90, 110],
        "sunshine_duration": [36000, 43200],  # 10 h, 12 h
    },
}


# --- heat index (NWS reference values) -------------------------------------


def test_heat_index_matches_nws_reference() -> None:
    # Published NWS heat-index chart cells (±1°F rounding of the Rothfusz regression).
    assert round(heat_index_f(96, 65)) == 121
    assert round(heat_index_f(90, 70)) == 106
    assert round(heat_index_f(80, 80)) == 84
    # Below ~80°F apparent, the Steadman form applies and HI stays near the air temp.
    assert round(heat_index_f(70, 50)) == 69


def test_heat_index_low_humidity_adjustment_reduces_it() -> None:
    # In the hot + very dry corner the NWS subtracts an adjustment, so HI < the raw
    # regression — and below the air temperature.
    assert heat_index_f(100, 10) < 100


# --- the archive client / reducer ------------------------------------------


def _client(handler) -> WeatherHistoryClient:  # type: ignore[no-untyped-def]
    return WeatherHistoryClient(
        "https://archive.open-meteo.test", transport=httpx.MockTransport(handler)
    )


def test_reduce_computes_aggregates_and_daily_peaks() -> None:
    stats = _reduce("Titusville, Florida", date(2023, 7, 1), date(2023, 7, 31), _ARCHIVE_OK)
    assert stats.days == 2  # two distinct calendar days
    assert stats.avg_high_f == 92.0 and stats.avg_low_f == 74.0  # (96+88)/2, (76+72)/2
    assert stats.max_temp_f == 96.0 and stats.min_temp_f == 74.0  # hourly extremes
    # The single peak is day 1's hottest hour; the average daily peak blends both days'
    # peaks, so it sits below the single peak.
    assert stats.peak_hi_f > stats.avg_high_hi_f
    assert stats.peak_hi_f == max(heat_index_f(95, 65), heat_index_f(96, 70))
    assert stats.danger_days >= 1  # day 1 reaches the ≥103°F Danger band


def test_reduce_computes_every_dimension() -> None:
    stats = _reduce("X", date(2023, 7, 1), date(2023, 7, 31), _ARCHIVE_OK)
    assert stats.avg_dew_point_f == 71.8  # (73+74+75+71+66)/5
    assert round(stats.total_precip_in, 2) == 0.30  # sum of daily precipitation_sum
    assert stats.rainy_days == 1 and round(stats.max_daily_precip_in, 2) == 0.30
    assert stats.total_snow_in == 0.0
    assert stats.avg_wind_mph == 7.4  # (4+8+10+3+12)/5
    assert stats.max_gust_mph == 22.0  # the daily gust max
    assert stats.wind_dir == "E"  # circular mean of 90° and 110° → ~100° → E
    assert stats.avg_cloud_cover == 46  # round((40+60+80+20+30)/5)
    assert stats.avg_sunshine_hours == 11.0  # (10h + 12h)/2
    assert stats.avg_pressure_mb == 1014.0  # (1015+1013+1012+1016+1014)/5


def test_reduce_omits_absent_dimensions() -> None:
    # Only temp + humidity present (the minimum) — every other dimension reads absent,
    # never crashes.
    import math

    body = {
        "hourly": {
            "time": ["2023-07-01T12:00"],
            "temperature_2m": [95],
            "relative_humidity_2m": [65],
        }
    }
    stats = _reduce("X", date(2023, 7, 1), date(2023, 7, 1), body)
    assert math.isnan(stats.total_precip_in) and math.isnan(stats.avg_wind_mph)
    assert stats.wind_dir is None and stats.total_snow_in == 0.0


def test_reduce_skips_null_hours() -> None:
    body = {
        "hourly": {
            "time": ["2023-07-01T12:00", "2023-07-01T13:00"],
            "temperature_2m": [95, None],
            "relative_humidity_2m": [65, 70],
        }
    }
    stats = _reduce("X", date(2023, 7, 1), date(2023, 7, 1), body)
    assert stats.avg_temp_f == 95.0  # the null hour is dropped, not counted as 0


async def test_archive_fetches_range_and_reduces() -> None:
    requested: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        requested.update(request.url.params)
        return httpx.Response(200, json=_ARCHIVE_OK)

    from jbrain.web.weather import GeoHit

    hit = GeoHit("Titusville, Florida", 28.6122, -80.8075)
    stats = await _client(handle).archive(hit, date(2023, 7, 1), date(2023, 7, 31))
    assert requested["start_date"] == "2023-07-01" and requested["end_date"] == "2023-07-31"
    assert requested["temperature_unit"] == "fahrenheit"
    assert stats.place == "Titusville, Florida" and stats.days == 2


async def test_archive_http_error_is_recoverable() -> None:
    from jbrain.web.weather import GeoHit

    client = _client(lambda r: httpx.Response(503))
    try:
        await client.archive(GeoHit("X", 1.0, 2.0), date(2023, 7, 1), date(2023, 7, 2))
    except WeatherError as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected WeatherError")


def test_hourly_series_computes_per_hour_heat_index() -> None:
    rows = _hourly_series(_ARCHIVE_OK)
    assert len(rows) == 5  # every hour (none dropped)
    first = rows[0]
    assert first.time == "2023-07-01 00:00"  # the T separator is flattened
    assert (first.temp_f, first.humidity) == (80, 80)
    assert first.hi_f == round(heat_index_f(80, 80))
    # The hot hour lands in the Danger band.
    assert max(r.hi_f for r in rows) == round(heat_index_f(96, 70))


def test_hourly_series_skips_null_hours() -> None:
    body = {
        "hourly": {
            "time": ["2023-07-01T12:00", "2023-07-01T13:00"],
            "temperature_2m": [95, None],
            "relative_humidity_2m": [65, 70],
        }
    }
    rows = _hourly_series(body)
    assert len(rows) == 1 and rows[0].temp_f == 95


def test_daily_series_rolls_up_per_day() -> None:
    rows = _daily_series(_ARCHIVE_OK)
    assert [r.date for r in rows] == ["2023-07-01", "2023-07-02"]  # sorted
    day1 = rows[0]
    assert day1.high_f == 96 and day1.low_f == 76  # from the daily block
    assert day1.peak_hi_f == round(max(heat_index_f(95, 65), heat_index_f(96, 70)))


def test_parse_iso_date() -> None:
    assert parse_iso_date("2023-07-01") == date(2023, 7, 1)
    assert parse_iso_date("2023-07-01T12:00") == date(2023, 7, 1)
    assert parse_iso_date("last July") is None
    assert parse_iso_date("") is None


# --- the tool handler ------------------------------------------------------


class FakeGeocoder:
    def __init__(self, hit: CityHit | None) -> None:
        self._hit = hit

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        return self._hit


def _weather_client(handler) -> WeatherClient:  # type: ignore[no-untyped-def]
    return WeatherClient(
        "https://api.open-meteo.test",
        "https://geo.open-meteo.test",
        transport=httpx.MockTransport(handler),
    )


def _tool(archive_handler, geo_handler, geocoder=None):  # type: ignore[no-untyped-def]
    geocoder = geocoder or FakeGeocoder(None)
    history = _client(archive_handler)
    weather = _weather_client(geo_handler)
    return build_weather_history_handlers(history, weather, geocoder)[  # type: ignore[arg-type]
        "weather_history"
    ]


async def test_named_place_range_returns_computed_summary() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )({"location": "Titusville, FL", "start_date": "2023-07-01", "end_date": "2023-07-31"}, CTX)
    assert isinstance(out, str)
    assert "Titusville, Florida, United States" in out
    assert "Heat index" in out and "average daily peak" in out
    assert "2023-07-01 to 2023-07-31" in out
    # The expanded dimensions all surface in the summary.
    assert "Precipitation:" in out and "Wind:" in out
    assert "dew point" in out and "sunshine" in out and "pressure" in out


async def test_missing_dates_is_recoverable() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )({"location": "Titusville, FL"}, CTX)
    assert isinstance(out, str) and "YYYY-MM-DD" in out


async def test_reversed_range_is_rejected() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )({"location": "X", "start_date": "2023-07-31", "end_date": "2023-07-01"}, CTX)
    assert isinstance(out, str) and "before" in out


async def test_range_over_a_year_is_rejected() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )({"location": "X", "start_date": "2020-01-01", "end_date": "2023-01-01"}, CTX)
    assert isinstance(out, str) and "once per year" in out


async def test_future_range_is_rejected() -> None:
    # 2099 is always in the future regardless of when the test runs.
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )({"location": "X", "start_date": "2099-07-01", "end_date": "2099-07-31"}, CTX)
    assert isinstance(out, str) and "past" in out


async def test_not_configured_reports_cleanly() -> None:
    history = WeatherHistoryClient("")  # empty archive URL disables the tool
    weather = _weather_client(lambda r: httpx.Response(200, json=_GEO_OK))
    tool = build_weather_history_handlers(history, weather, FakeGeocoder(None))[  # type: ignore[arg-type]
        "weather_history"
    ]
    out = await tool({"start_date": "2023-07-01", "end_date": "2023-07-31"}, CTX)
    assert isinstance(out, str) and "configured" in out


async def test_here_resolves_to_nearest_city_then_looks_up() -> None:
    requested: list[str] = []

    def geo(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params.get("name", ""))
        return httpx.Response(200, json=_GEO_OK)

    geocoder = FakeGeocoder(CityHit("Titusville", "Florida", "United States", 1200.0))
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), here=(28.61, -80.81)
    )
    out = await _tool(lambda r: httpx.Response(200, json=_ARCHIVE_OK), geo, geocoder)(
        {"start_date": "2023-07-01", "end_date": "2023-07-31"}, ctx
    )
    assert isinstance(out, str) and "Titusville" in out
    assert requested == ["Titusville"]  # the bare city name, never the raw coordinate


async def test_unknown_place_reports_not_found() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json={"results": []}),
    )({"location": "Atlantis", "start_date": "2023-07-01", "end_date": "2023-07-31"}, CTX)
    assert isinstance(out, str) and "Atlantis" in out


async def test_hourly_detail_returns_a_per_hour_table() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )(
        {
            "location": "Titusville, FL",
            "start_date": "2023-07-01",
            "end_date": "2023-07-02",
            "detail": "hourly",
        },
        CTX,
    )
    assert isinstance(out, str)
    assert "hour-by-hour heat index" in out
    # One line per hour, each carrying the computed heat index.
    assert out.count("heat index") >= 5
    assert "2023-07-01 00:00:" in out and "% RH" in out


async def test_daily_detail_returns_a_per_day_table() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )(
        {
            "location": "Titusville, FL",
            "start_date": "2023-07-01",
            "end_date": "2023-07-31",
            "detail": "daily",
        },
        CTX,
    )
    assert isinstance(out, str)
    assert "daily heat index" in out
    assert "2023-07-01: high 96°F / low 76°F" in out


async def test_hourly_detail_over_a_week_is_capped() -> None:
    # An hour-by-hour list over more than a week is refused before any fetch.
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )(
        {
            "location": "X",
            "start_date": "2023-07-01",
            "end_date": "2023-07-31",
            "detail": "hourly",
        },
        CTX,
    )
    assert isinstance(out, str) and "capped at 7 days" in out


async def test_unknown_detail_falls_back_to_summary() -> None:
    out = await _tool(
        lambda r: httpx.Response(200, json=_ARCHIVE_OK),
        lambda r: httpx.Response(200, json=_GEO_OK),
    )(
        {
            "location": "Titusville, FL",
            "start_date": "2023-07-01",
            "end_date": "2023-07-31",
            "detail": "nonsense",
        },
        CTX,
    )
    assert isinstance(out, str) and "Heat index: average across all hours" in out
