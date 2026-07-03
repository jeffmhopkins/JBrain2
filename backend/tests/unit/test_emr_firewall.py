"""Layer-2 location firewall guard (docs/plans/EMR_IMPORT_PLAN.md §3.6). Pure —
proves the lock set is the {address, geo} ∪ floor-dict union, that it catches
both `address` and `geo` on a health entity, and that it is inert off-path.
"""

from __future__ import annotations

import pytest

from jbrain.analysis.extraction import domain_floor
from jbrain.ingest.emr.firewall import (
    _FLOOR_LOCKS,
    _LOCATED_FACET_LOCKS,
    LOCATION_LOCK_PREDICATES,
    is_location_locked,
)


@pytest.mark.parametrize(
    "kind",
    ["Observation", "lab_result", "encounter", "Person", "Organization", "medical_condition"],
)
@pytest.mark.parametrize(
    "predicate", ["address", "geo", "latitude", "longitude", "geocoordinates", "gpscoordinates"]
)
def test_locks_every_location_predicate_on_every_health_entity(kind: str, predicate: str) -> None:
    assert is_location_locked(predicate, kind)


def test_geo_is_caught_even_though_it_is_not_in_the_floor_dict() -> None:
    # The gap the union closes: `geo` is a location lock but the extraction floor
    # does NOT floor it, so a floor-only guard would let it through.
    assert "geo" in LOCATION_LOCK_PREDICATES
    assert domain_floor("geo") is None
    assert is_location_locked("geo", "Observation")


def test_case_and_whitespace_insensitive() -> None:
    assert is_location_locked("  Address ", "  observation ")


def test_non_location_predicate_on_health_entity_is_not_locked() -> None:
    assert not is_location_locked("value", "Observation")
    assert not is_location_locked("careUnit", "encounter")
    assert not is_location_locked("serviceProvider", "encounter")


def test_location_predicate_on_a_non_emr_entity_is_not_locked() -> None:
    # The guard is scoped to health EMR entities; a Place's address is legitimate.
    assert not is_location_locked("address", "Place")
    assert not is_location_locked("geo", "appointment")


def test_lock_set_matches_the_extraction_floor_drift_guard() -> None:
    # Every floor-dict predicate is genuinely floored to `location`...
    for p in _FLOOR_LOCKS:
        assert domain_floor(p) == "location", p
    # ...and the Located-facet predicates are NOT floored, so the union is necessary.
    for p in _LOCATED_FACET_LOCKS:
        assert domain_floor(p) is None, p
    assert LOCATION_LOCK_PREDICATES == _FLOOR_LOCKS | _LOCATED_FACET_LOCKS
