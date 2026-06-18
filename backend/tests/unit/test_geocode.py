"""The Photon geocoder client + its address flattening (Phase 7 Wave 4). HTTP is
faked with an httpx MockTransport — CI never runs a real Photon."""

import httpx

from jbrain.geocode import PhotonGeocoderClient, format_address


def _feature(props: dict, lon: float, lat: float) -> dict:
    return {"properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def test_format_address_joins_populated_parts_in_order() -> None:
    props = {
        "name": "Home",
        "housenumber": "10",
        "street": "Main St",
        "city": "Townsville",
        "state": "NY",
        "postcode": "12345",
        "country": "USA",
    }
    assert format_address(props) == "Home, 10 Main St, Townsville, NY, 12345, USA"


def test_format_address_drops_blanks_and_adjacent_repeats() -> None:
    # Photon often echoes name == street; the dup collapses, blanks vanish.
    assert format_address({"name": "Cafe", "street": "Cafe", "city": "Metropolis"}) == (
        "Cafe, Metropolis"
    )
    assert format_address({}) == ""


async def test_reverse_returns_the_first_usable_feature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/reverse"
        assert request.url.params["lat"] == "40.0"
        return httpx.Response(
            200, json=_fc(_feature({"name": "Home", "city": "Townsville"}, -74.0, 40.0))
        )

    client = PhotonGeocoderClient("http://geocoder", transport=httpx.MockTransport(handler))
    result = await client.reverse(40.0, -74.0)
    assert result is not None
    assert result.label == "Home, Townsville"
    assert (result.latitude, result.longitude) == (40.0, -74.0)


async def test_reverse_returns_none_when_no_hit() -> None:
    client = PhotonGeocoderClient(
        "http://geocoder",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=_fc())),
    )
    assert await client.reverse(0.0, 0.0) is None


async def test_forward_returns_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api"
        assert request.url.params["q"] == "office"
        return httpx.Response(
            200,
            json=_fc(
                _feature({"name": "Office", "city": "Metropolis"}, -75.0, 41.0),
                _feature({}, -76.0, 42.0),  # no usable label → skipped
            ),
        )

    client = PhotonGeocoderClient("http://geocoder", transport=httpx.MockTransport(handler))
    results = await client.forward("office")
    assert [r.label for r in results] == ["Office, Metropolis"]
