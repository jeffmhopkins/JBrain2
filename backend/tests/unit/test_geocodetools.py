"""The on-box geocoding agent tool: `geocode_reverse`, now over the offline
nearest-city geocoder. The geocoder is faked; these assert the handler contract."""

from jbrain.agent.geocodetools import build_geocode_handlers
from jbrain.agent.loop import ToolContext
from jbrain.citygeocode import CityHit
from jbrain.db.session import SessionContext

OWNER = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())


class FakeCityGeocoder:
    def __init__(self, hit: CityHit | None) -> None:
        self._hit = hit
        self.calls = 0
        self.raise_on_call = False

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("geocode down")
        return self._hit


def _reverse(geo: FakeCityGeocoder):  # noqa: ANN202
    return build_geocode_handlers(geo)["geocode_reverse"]  # type: ignore[arg-type]


async def test_reverse_names_the_nearest_city() -> None:
    geo = FakeCityGeocoder(
        CityHit(name="Townsville", region="New York", country="United States", distance_m=1500.0)
    )
    out = await _reverse(geo)({"latitude": 40.0, "longitude": -74.0}, OWNER)
    assert "Townsville, New York, United States" in out and "2 km" in out


async def test_reverse_rejects_non_numeric_coords() -> None:
    geo = FakeCityGeocoder(None)
    out = await _reverse(geo)({"latitude": "north", "longitude": -74.0}, OWNER)
    assert "numeric" in out
    assert geo.calls == 0


async def test_reverse_reports_no_nearby_place() -> None:
    out = await _reverse(FakeCityGeocoder(None))({"latitude": 0.0, "longitude": -40.0}, OWNER)
    assert "No populated place" in out


async def test_reverse_survives_a_geocoder_outage() -> None:
    geo = FakeCityGeocoder(None)
    geo.raise_on_call = True
    out = await _reverse(geo)({"latitude": 40.0, "longitude": -74.0}, OWNER)
    assert "unavailable" in out
