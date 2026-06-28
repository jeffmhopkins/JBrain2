"""NhcSurgeClient — the NHC Peak-Surge band lookup (docs/HURRICANE_TABS_PLAN.md §8).
HTTP is faked via MockTransport — no live network, like the hurricane/weather
clients. The surge band lives as text inside the feature's `popupinfo` HTML, so the
fixtures mirror that shape."""

import httpx

from jbrain.web.nhc_surge import NhcSurgeClient, NhcSurgeError, _band_feet

_BASE = "https://mapservices.test/tropical/rest/services/tropical"


def _client(handler):  # type: ignore[no-untyped-def]
    return NhcSurgeClient(_BASE, transport=httpx.MockTransport(handler))


def _features(*popups: str) -> dict:
    """A GeoJSON FeatureCollection whose features carry the given `popupinfo` blobs —
    the renderer's band label is buried in the HTML, as it is on the live service."""
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"popupinfo": p}, "geometry": None} for p in popups
        ],
    }


# --- pure helper -----------------------------------------------------------


def test_band_feet_maps_bands_for_sorting() -> None:
    assert _band_feet("Up to 3 ft") == 3
    assert _band_feet("Up to 9 ft") == 9
    assert _band_feet("Up to 12 ft") == 12
    # "Above 12 ft" is open-ended and must sort above the banded values.
    assert _band_feet("Above 12 ft") == 13
    assert _band_feet("Above 12 ft") > _band_feet("Up to 12 ft")
    assert _band_feet("nonsense") == 0


# --- peak_band -------------------------------------------------------------


async def test_peak_band_parses_popupinfo() -> None:
    popup = "<table><tr><td>Peak Storm Surge</td><td>Up to 9 ft</td></tr></table>"
    out = await _client(lambda r: httpx.Response(200, json=_features(popup))).peak_band(27.9, -82.5)
    assert out == "Up to 9 ft"


async def test_peak_band_returns_highest_of_several_features() -> None:
    feats = _features("…Up to 3 ft above ground…", "…Up to 9 ft above ground…")
    out = await _client(lambda r: httpx.Response(200, json=feats)).peak_band(27.9, -82.5)
    assert out == "Up to 9 ft"


async def test_peak_band_above_band_sorts_highest() -> None:
    feats = _features("Up to 12 ft", "Above 12 ft", "Up to 9 ft")
    out = await _client(lambda r: httpx.Response(200, json=feats)).peak_band(27.9, -82.5)
    assert out == "Above 12 ft"


async def test_peak_band_no_features_is_none() -> None:
    out = await _client(lambda r: httpx.Response(200, json=_features())).peak_band(40.0, -100.0)
    assert out is None


async def test_peak_band_unparseable_feature_is_none() -> None:
    feats = _features("<div>No surge information available here.</div>")
    out = await _client(lambda r: httpx.Response(200, json=feats)).peak_band(27.9, -82.5)
    assert out is None


async def test_peak_band_sends_lon_lat_point_query() -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=_features("Up to 6 ft"))

    out = await _client(handle).peak_band(27.95, -82.46)
    assert out == "Up to 6 ft"
    assert seen["geometry"] == "-82.46,27.95"  # lon,lat order
    assert seen["geometryType"] == "esriGeometryPoint"
    assert seen["inSR"] == "4326"
    assert seen["f"] == "geojson"


async def test_peak_band_http_error_is_recoverable_without_coordinate() -> None:
    client = _client(lambda r: httpx.Response(503))
    try:
        await client.peak_band(27.95, -82.46)
    except NhcSurgeError as exc:
        assert "unavailable" in str(exc)
        # §5: the surfaced error must not leak the coordinate.
        assert "27.95" not in str(exc) and "82.46" not in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected NhcSurgeError")


async def test_peak_band_malformed_body_is_none() -> None:
    out = await _client(lambda r: httpx.Response(200, json={"no": "features"})).peak_band(1.0, 2.0)
    assert out is None


async def test_peak_band_non_json_body_is_recoverable() -> None:
    # A 200 with a non-JSON body raises on .json() (ValueError) → the recoverable error.
    client = _client(lambda r: httpx.Response(200, content=b"<html>not json</html>"))
    try:
        await client.peak_band(27.95, -82.46)
    except NhcSurgeError as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected NhcSurgeError")


async def test_peak_band_skips_malformed_features() -> None:
    # Features of the wrong shape (non-dict, no properties, non-string popupinfo) are
    # skipped defensively, leaving the one good band.
    feats = {
        "features": [
            "not a dict",
            {"geometry": None},
            {"properties": {"popupinfo": 123}},
            {"properties": {"popupinfo": "Up to 6 ft"}},
        ]
    }
    out = await _client(lambda r: httpx.Response(200, json=feats)).peak_band(27.9, -82.5)
    assert out == "Up to 6 ft"


def test_not_configured_returns_false() -> None:
    assert NhcSurgeClient("").configured is False


async def test_not_configured_peak_band_is_none() -> None:
    assert await NhcSurgeClient("").peak_band(27.9, -82.5) is None
