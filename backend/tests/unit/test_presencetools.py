"""jerv's owner-location tool: `current_location` — names the live PWA fix via the
offline city geocoder, escalating to the external geocoder only for a street address."""

import pytest

from jbrain.agent.loop import ToolContext
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.citygeocode import CityHit
from jbrain.db.session import SessionContext


class _City:
    """A stub CityGeocoder.nearest — returns a fixed hit (or None for 'no city near')."""

    def __init__(self, hit: CityHit | None) -> None:
        self._hit = hit
        self.calls: list[tuple[float, float]] = []

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        self.calls.append((lat, lon))
        return self._hit


class _Ext:
    def __init__(self, address: str | None) -> None:
        self._address = address
        self.calls = 0

    async def reverse(self, latitude: float, longitude: float) -> str | None:
        self.calls += 1
        return self._address


def _here_ctx(lat: float, lon: float) -> ToolContext:
    session = SessionContext(principal_id="own", principal_kind="owner", owner_scoped=True)
    return ToolContext(session=session, scopes=(), here=(lat, lon))


def _tool(city: _City, ext: "_Ext | None" = None):  # noqa: ANN202
    return build_presence_handlers(city, ext)["current_location"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_current_location_names_the_nearest_city_by_default() -> None:
    city = _City(
        CityHit(name="Springfield", region="Illinois", country="United States", distance_m=2400.0)
    )
    out = await _tool(city)({}, _here_ctx(39.8, -89.6))
    assert "near Springfield, Illinois, United States" in out
    assert "2 km" in out


@pytest.mark.asyncio
async def test_current_location_says_in_city_when_essentially_at_it() -> None:
    city = _City(CityHit(name="London", region="", country="United Kingdom", distance_m=300.0))
    out = await _tool(city)({}, _here_ctx(51.51, -0.13))
    assert "in London, United Kingdom" in out


@pytest.mark.asyncio
async def test_current_location_address_uses_the_external_geocoder() -> None:
    city = _City(CityHit(name="London", region="", country="United Kingdom", distance_m=300.0))
    ext = _Ext("221B Baker St, London NW1, United Kingdom")
    out = await _tool(city, ext)({"detail": "address"}, _here_ctx(51.52, -0.16))
    assert "221B Baker St" in out and ext.calls == 1


@pytest.mark.asyncio
async def test_current_location_address_without_external_falls_back_to_city() -> None:
    # No external geocoder configured → an address request still answers at city level.
    city = _City(CityHit(name="London", region="", country="United Kingdom", distance_m=300.0))
    out = await _tool(city, None)({"detail": "address"}, _here_ctx(51.52, -0.16))
    assert "in London, United Kingdom" in out


@pytest.mark.asyncio
async def test_current_location_coordinates_returns_raw_lat_lon_without_geocoding() -> None:
    # detail="coordinates" reports the raw fix and never touches either geocoder.
    city = _City(CityHit(name="London", region="", country="United Kingdom", distance_m=300.0))
    ext = _Ext("221B Baker St, London NW1, United Kingdom")
    out = await _tool(city, ext)({"detail": "coordinates"}, _here_ctx(51.52000, -0.16000))
    assert "51.52000" in out and "-0.16000" in out
    assert city.calls == [] and ext.calls == 0


@pytest.mark.asyncio
async def test_current_location_no_city_near_returns_coordinates() -> None:
    out = await _tool(_City(None))({}, _here_ctx(0.0, -40.0))
    assert "0.00000" in out and "-40.00000" in out


@pytest.mark.asyncio
async def test_current_location_without_a_live_fix_asks_to_share() -> None:
    session = SessionContext(principal_id="own", principal_kind="owner", owner_scoped=True)
    out = await _tool(_City(None))({}, ToolContext(session=session, scopes=()))
    assert "share it" in out
