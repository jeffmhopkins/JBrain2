"""The pure geofence-fact -> geometry mapping."""

from jbrain.analysis.geofence_projection import _geometry


def test_circle_maps_to_center_and_radius() -> None:
    value = {"center": {"latitude": 40.0, "longitude": -74.0}, "radiusMeters": 120}
    assert _geometry(value) == (40.0, -74.0, 120.0, None)


def test_polygon_maps_to_wkt_and_wins_over_a_circle() -> None:
    wkt = "POLYGON((-74 40, -74 41, -73 41, -73 40, -74 40))"
    value = {
        "center": {"latitude": 40.0, "longitude": -74.0},
        "radiusMeters": 120,
        "polygon": wkt,
    }
    assert _geometry(value) == (None, None, None, wkt)


def test_incomplete_or_nonsense_values_map_to_none() -> None:
    assert _geometry(None) is None
    assert _geometry("nope") is None
    assert _geometry({}) is None
    assert _geometry({"center": {"latitude": 40.0, "longitude": -74.0}}) is None  # no radius
    assert _geometry({"radiusMeters": 100}) is None  # no center
    assert _geometry({"center": {"latitude": 40.0}, "radiusMeters": 100}) is None  # no lon
