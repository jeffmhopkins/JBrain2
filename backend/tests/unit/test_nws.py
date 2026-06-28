"""The NWS client (alerts + gridpoint timeline) behind the hurricane card's US-coverage
half (docs/HURRICANE_TABS_PLAN.md §1/§3/§8). HTTP is faked via MockTransport — no live
network and no real clock, like the weather and hurricane adapters. Every time label is
derived from an upstream `validTime` string through `zoneinfo`, never `datetime.now`.
"""

import httpx

from jbrain.web.nws import (
    Alert,
    NwsClient,
    NwsOutOfCoverage,
    NwsUnavailable,
    Timeline,
    _convert,
    _expand_series,
    _hour_label,
    _iso_pt_hours,
)

# All gridpoint validTimes are anchored so that, in America/New_York (EDT = UTC-4 in
# September), the first covered hour is 21:00 → "9 PM" on the prior calendar day. This
# pins the place-local label assertions without touching a real clock.
_TZ = "America/New_York"

_LAT, _LON = 27.95, -82.46


def _points_body(grid_url: str = "https://api.weather.test/gridpoints/TBW/63,63") -> dict:
    return {
        "properties": {
            "forecastGridData": grid_url,
            "timeZone": _TZ,
        }
    }


def _grid_body(
    *,
    wind_values: list | None = None,
    gust_values: list | None = None,
    qpf_values: list | None = None,
    include_gust: bool = True,
) -> dict:
    props: dict = {
        "windSpeed": {
            "uom": "wmoUnit:km_h-1",
            "values": wind_values if wind_values is not None else [],
        },
        "quantitativePrecipitation": {
            "uom": "wmoUnit:mm",
            "values": qpf_values if qpf_values is not None else [],
        },
    }
    if include_gust:
        props["windGust"] = {
            "uom": "wmoUnit:km_h-1",
            "values": gust_values if gust_values is not None else [],
        }
    return {"properties": props}


def _route(*, points=None, grid=None, alerts=None):  # type: ignore[no-untyped-def]
    """A MockTransport handler routing by path: /points, the gridpoints URL, and
    /alerts/active. Each arg is a (status, json) tuple or a plain dict (→ 200)."""

    def _resp(spec):  # type: ignore[no-untyped-def]
        if isinstance(spec, tuple):
            status, body = spec
            return httpx.Response(status, json=body)
        return httpx.Response(200, json=spec)

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/alerts/active" in path and alerts is not None:
            return _resp(alerts)
        if "/points/" in path and points is not None:
            return _resp(points)
        if "/gridpoints/" in path and grid is not None:
            return _resp(grid)
        return httpx.Response(500, json={})

    return httpx.MockTransport(handle)


def _client(transport: httpx.MockTransport) -> NwsClient:
    return NwsClient("https://api.weather.test", transport=transport)


# --- pure helpers ----------------------------------------------------------


def test_hour_label_is_compact_12h() -> None:
    from datetime import datetime

    assert _hour_label(datetime(2026, 9, 10, 21, 0)) == "9 PM"
    assert _hour_label(datetime(2026, 9, 11, 0, 0)) == "12 AM"
    assert _hour_label(datetime(2026, 9, 11, 12, 0)) == "12 PM"


def test_iso_pt_hours_parses_durations() -> None:
    assert _iso_pt_hours("PT1H") == 1
    assert _iso_pt_hours("PT6H") == 6
    assert _iso_pt_hours("P1DT3H") == 27  # days fold into hours defensively
    assert _iso_pt_hours("PT30M") == 0  # sub-hour floors to zero
    assert _iso_pt_hours("garbage") == 0


def test_convert_units() -> None:
    # 64.37 km/h ≈ 40 mph; 25.4 mm = 1 inch.
    assert round(_convert(64.37, "wmoUnit:km_h-1", kmh_to_mph=True)) == 40
    assert round(_convert(25.4, "wmoUnit:mm", kmh_to_mph=False), 3) == 1.0
    # Already-imperial uoms pass through unscaled (defensive).
    assert _convert(40.0, "mph", kmh_to_mph=True) == 40.0
    assert _convert(1.0, "wmoUnit:in", kmh_to_mph=False) == 1.0


def test_expand_replicates_wind_but_divides_rain() -> None:
    # A single PT6H entry. Instantaneous wind replicates the value into every covered
    # hour; accumulated rain divides it evenly.
    interval = [{"validTime": "2026-09-11T01:00:00+00:00/PT6H", "value": 60.0}]
    wind = _expand_series({"uom": "x", "values": interval}, accumulate=False)
    rain = _expand_series({"uom": "x", "values": interval}, accumulate=True)
    assert len(wind) == 6 and set(wind.values()) == {60.0}  # replicated
    assert len(rain) == 6 and all(v == 10.0 for v in rain.values())  # 60 / 6 hr


def test_expand_missing_series_is_empty() -> None:
    assert _expand_series(None, accumulate=False) == {}
    assert _expand_series({"uom": "x"}, accumulate=False) == {}  # no values key
    assert _expand_series({"uom": "x", "values": []}, accumulate=True) == {}


# --- alerts ----------------------------------------------------------------


async def test_alerts_keeps_only_tropical_and_maps_event() -> None:
    feed = {
        "features": [
            {
                "properties": {
                    "event": "Hurricane Warning",
                    "headline": "Hurricane Warning in effect for the coast",
                }
            },
            {"properties": {"event": "Flood Watch", "headline": "ignore me"}},
        ]
    }
    client = _client(_route(alerts=feed))
    out = await client.alerts(_LAT, _LON)
    assert out == (
        Alert(
            event="Hurricane Warning",
            level="warning",
            kind="hurricane",
            headline="Hurricane Warning in effect for the coast",
        ),
    )


async def test_alerts_surge_and_tropical_storm_mappings() -> None:
    feed = {
        "features": [
            {"properties": {"event": "Storm Surge Warning"}},
            {"properties": {"event": "Tropical Storm Watch"}},
            {"properties": {"event": "Extreme Wind Warning"}},
        ]
    }
    client = _client(_route(alerts=feed))
    out = await client.alerts(_LAT, _LON)
    assert [(a.level, a.kind) for a in out] == [
        ("warning", "surge"),
        ("watch", "tropical-storm"),
        ("warning", "other"),
    ]
    assert all(a.headline == "" for a in out)  # absent headline reads as empty text


async def test_alerts_empty_feed_returns_nothing() -> None:
    client = _client(_route(alerts={"features": []}))
    assert await client.alerts(_LAT, _LON) == ()


# --- timeline --------------------------------------------------------------


def _grid_with_intervals() -> dict:
    """A gridpoint with PT1H + PT3H + PT6H windSpeed/gust runs and a divided QPF run.
    Wind is held at 64.37 km/h (≈ 40 mph) across the whole 10h so replication is
    visible; QPF is 6 mm over the PT6H tail → 1 mm/hr."""
    wind = [
        {"validTime": "2026-09-11T01:00:00+00:00/PT1H", "value": 64.37},
        {"validTime": "2026-09-11T02:00:00+00:00/PT3H", "value": 64.37},
        {"validTime": "2026-09-11T05:00:00+00:00/PT6H", "value": 64.37},
    ]
    gust = [
        {"validTime": "2026-09-11T01:00:00+00:00/PT3H", "value": 96.56},  # ≈ 60 mph
    ]
    qpf = [
        {"validTime": "2026-09-11T05:00:00+00:00/PT6H", "value": 6.0},  # 6 mm / 6 hr
    ]
    return _grid_body(wind_values=wind, gust_values=gust, qpf_values=qpf)


async def test_timeline_replicates_wind_divides_rain_converts_units() -> None:
    transport = _route(points=_points_body(), grid=_grid_with_intervals())
    tl = await _client(transport).timeline(_LAT, _LON)
    assert isinstance(tl, Timeline)
    assert tl.tz == _TZ

    # The first three covered hours all read 40 mph — the replicated wind survives
    # downsampling as the bucket peak (km/h → mph conversion confirmed).
    assert tl.cells[0].wind_mph == 40
    # The first bucket spans the PT3H gust run → ≈ 60 mph; later buckets have no gust → 0.
    assert tl.cells[0].gust_mph == 60
    # QPF: 6 mm divided to 1 mm/hr over the PT6H tail (05:00–11:00 UTC). Summed back
    # across the buckets it totals 6 mm = 6 / 25.4 ≈ 0.236 in, and the bucket fully
    # inside the tail (07:00–09:00) sums 3 mm = 3 / 25.4 ≈ 0.12 in — confirming the
    # accumulation was divided, not replicated (which would have multiplied it).
    total_rain = sum(c.rain_in for c in tl.cells)
    assert round(total_rain, 2) == 0.24
    assert 0.12 in [round(c.rain_in, 2) for c in tl.cells]


async def test_timeline_missing_gust_series_yields_zero_not_error() -> None:
    wind = [{"validTime": "2026-09-11T01:00:00+00:00/PT6H", "value": 64.37}]
    grid = _grid_body(wind_values=wind, include_gust=False)  # windGust key absent
    tl = await _client(_route(points=_points_body(), grid=grid)).timeline(_LAT, _LON)
    assert all(c.gust_mph == 0 for c in tl.cells)
    assert any(c.wind_mph == 40 for c in tl.cells)  # wind still parsed


async def test_timeline_downsamples_to_3hourly_with_place_local_labels() -> None:
    # 36 hours of windSpeed, one PT36H run anchored at 01:00 UTC. The downsample yields
    # 12 cells (36 / 3) and the first label is place-local: 01:00 UTC = 21:00 EDT = 9 PM.
    wind = [{"validTime": "2026-09-11T01:00:00+00:00/PT36H", "value": 32.0}]
    grid = _grid_body(wind_values=wind)
    tl = await _client(_route(points=_points_body(), grid=grid)).timeline(_LAT, _LON)
    assert len(tl.cells) == 12
    assert tl.cells[0].label == "9 PM"  # place-local, not "1 AM" UTC
    assert tl.cells[1].label == "12 AM"  # +3 h, next calendar day in NY


# --- arrival ---------------------------------------------------------------


async def test_arrival_labels_cross_39_then_74() -> None:
    # Sustained wind ramps: <39 for the first hours, ≥39 at hour 3 (04:00 UTC = 12 AM
    # EDT), ≥74 at hour 5 (06:00 UTC = 2 AM EDT). 1 mph ≈ 1.609 km/h.
    def kmh(mph: float) -> float:
        return mph / 0.621371

    wind = [
        {"validTime": "2026-09-11T01:00:00+00:00/PT1H", "value": kmh(20)},
        {"validTime": "2026-09-11T02:00:00+00:00/PT1H", "value": kmh(30)},
        {"validTime": "2026-09-11T03:00:00+00:00/PT1H", "value": kmh(35)},
        {"validTime": "2026-09-11T04:00:00+00:00/PT1H", "value": kmh(45)},  # ≥39 here
        {"validTime": "2026-09-11T05:00:00+00:00/PT1H", "value": kmh(60)},
        {"validTime": "2026-09-11T06:00:00+00:00/PT1H", "value": kmh(80)},  # ≥74 here
    ]
    grid = _grid_body(wind_values=wind)
    tl = await _client(_route(points=_points_body(), grid=grid)).timeline(_LAT, _LON)
    assert tl.ts_force_label == "12 AM"  # 04:00 UTC → 00:00 EDT
    assert tl.hurricane_force_label == "2 AM"  # 06:00 UTC → 02:00 EDT


async def test_arrival_none_when_never_reaching_39() -> None:
    def kmh(mph: float) -> float:
        return mph / 0.621371

    wind = [
        {"validTime": "2026-09-11T01:00:00+00:00/PT3H", "value": kmh(20)},
        {"validTime": "2026-09-11T04:00:00+00:00/PT3H", "value": kmh(30)},
    ]
    grid = _grid_body(wind_values=wind)
    tl = await _client(_route(points=_points_body(), grid=grid)).timeline(_LAT, _LON)
    assert tl.ts_force_label is None and tl.hurricane_force_label is None


async def test_timeline_empty_series_yields_no_cells() -> None:
    grid = _grid_body()  # all series empty
    tl = await _client(_route(points=_points_body(), grid=grid)).timeline(_LAT, _LON)
    assert tl.cells == () and tl.ts_force_label is None


# --- coverage / errors -----------------------------------------------------


async def test_points_404_is_out_of_coverage_without_coordinate() -> None:
    transport = _route(points=(404, {"detail": "not found"}))
    try:
        await _client(transport).timeline(_LAT, _LON)
    except NwsOutOfCoverage as exc:
        _assert_no_coordinate(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected NwsOutOfCoverage")


async def test_points_503_is_unavailable_without_coordinate() -> None:
    transport = _route(points=(503, {"detail": "boom"}))
    try:
        await _client(transport).timeline(_LAT, _LON)
    except NwsUnavailable as exc:
        _assert_no_coordinate(exc)
    else:  # pragma: no cover
        raise AssertionError("expected NwsUnavailable")


async def test_alerts_404_is_out_of_coverage() -> None:
    transport = _route(alerts=(404, {"detail": "not found"}))
    try:
        await _client(transport).alerts(_LAT, _LON)
    except NwsOutOfCoverage as exc:
        _assert_no_coordinate(exc)
    else:  # pragma: no cover
        raise AssertionError("expected NwsOutOfCoverage")


def _assert_no_coordinate(exc: Exception) -> None:
    """The location firewall: a surfaced error must carry neither latitude nor
    longitude substring (§5 `[r-S2-sec]`)."""
    text = str(exc)
    assert str(_LAT) not in text
    assert str(_LON) not in text
    assert "27.9" not in text and "82.4" not in text


async def test_timeline_points_without_grid_url_is_unavailable() -> None:
    transport = _route(points={"properties": {"timeZone": _TZ}})  # no forecastGridData
    try:
        await _client(transport).timeline(_LAT, _LON)
    except NwsUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected NwsUnavailable")


async def test_timeline_malformed_point_body_is_unavailable() -> None:
    transport = _route(points={"unexpected": 1})  # no properties dict
    try:
        await _client(transport).timeline(_LAT, _LON)
    except NwsUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected NwsUnavailable")


async def test_timeline_bad_json_is_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handle)
    try:
        await _client(transport).timeline(_LAT, _LON)
    except NwsUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected NwsUnavailable")


async def test_timeline_unknown_tz_falls_back_to_utc_labels() -> None:
    # An unknown IANA zone must not crash the timeline — labels fall back to UTC, so
    # 01:00 UTC reads "1 AM" (not the EDT "9 PM").
    wind = [{"validTime": "2026-09-11T01:00:00+00:00/PT3H", "value": 32.0}]
    grid = _grid_body(wind_values=wind)
    points = {"properties": {"forecastGridData": "https://api.weather.test/gridpoints/X/1,1"}}
    tl = await _client(_route(points=points, grid=grid)).timeline(_LAT, _LON)
    assert tl.tz == "UTC"
    assert tl.cells[0].label == "1 AM"


def test_not_configured_when_base_empty() -> None:
    assert NwsClient("").configured is False
    assert NwsClient("https://api.weather.test").configured is True
