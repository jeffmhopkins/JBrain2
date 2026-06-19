"""The external reverse-geocoder client + its address flattening (Phase 7 Wave 4b).
HTTP is faked with an httpx MockTransport — CI never calls a real geocoder."""

import httpx

from jbrain.geocode import NominatimReverseClient, format_address


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
    # A feed often echoes name == street; the dup collapses, blanks vanish.
    assert format_address({"name": "Cafe", "street": "Cafe", "city": "Metropolis"}) == (
        "Cafe, Metropolis"
    )
    assert format_address({}) == ""


def test_external_reverse_disabled_when_unconfigured() -> None:
    client = NominatimReverseClient("")
    assert client.enabled is False


async def test_external_reverse_returns_display_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/reverse"
        assert request.url.params["lat"] == "40.0"
        return httpx.Response(200, json={"display_name": "10 Main St, Townsville, NY"})

    client = NominatimReverseClient("http://geo", transport=httpx.MockTransport(handler))
    assert client.enabled is True
    assert await client.reverse(40.0, -74.0) == "10 Main St, Townsville, NY"


async def test_external_reverse_understands_geojson() -> None:
    feature = {"properties": {"name": "Office", "city": "Metropolis"}}
    client = NominatimReverseClient(
        "http://geo",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"features": [feature]})),
    )
    assert await client.reverse(41.0, -75.0) == "Office, Metropolis"


async def test_external_reverse_none_on_error_or_miss() -> None:
    boom = NominatimReverseClient(
        "http://geo", transport=httpx.MockTransport(lambda _r: httpx.Response(502))
    )
    assert await boom.reverse(0.0, 0.0) is None
    empty = NominatimReverseClient(
        "http://geo", transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
    )
    assert await empty.reverse(0.0, 0.0) is None
