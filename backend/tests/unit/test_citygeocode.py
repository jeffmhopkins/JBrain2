"""The offline nearest-city reverse geocoder over the bundled GeoNames cities."""

from jbrain.citygeocode import CityGeocoder

# One shared instance: the first lookup lazy-loads ~32k cities (a few hundred ms);
# reusing it across the module keeps the suite fast.
_GEO = CityGeocoder()


def test_resolves_a_coordinate_to_the_nearest_city() -> None:
    # Central London → London, United Kingdom, within a kilometre or two.
    hit = _GEO.nearest(51.5074, -0.1278)
    assert hit is not None
    assert hit.name == "London"
    assert hit.country == "United Kingdom"
    assert hit.distance_m < 5_000


def test_us_hit_carries_the_state_as_region() -> None:
    # Near downtown Chicago → the region is the US state name (GeoNames admin1).
    hit = _GEO.nearest(41.8781, -87.6298)
    assert hit is not None
    assert hit.region == "Illinois"
    assert hit.country == "United States"


def test_open_ocean_has_no_city_within_range() -> None:
    # Mid South Atlantic — the nearest populated place is far beyond the max radius.
    assert CityGeocoder(max_km=150.0).nearest(-30.0, -20.0) is None
