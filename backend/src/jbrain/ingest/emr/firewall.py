"""Layer-2 location firewall for the EMR importer (docs/plans/EMR_IMPORT_PLAN.md §3.6).

The health firewall on this path has NO domain-floor backstop: `domain_floor`
only ratchets a *general* note (`extracted_domain == "general"`), so on a health
note an `address`/`geo` fact would stay `health` rather than being pushed to the
`location` domain. The firewall therefore rests on two deliberate layers so a
single parser miss can't plant location whereabouts in the health domain:

  Layer 1 — the parsers strip postal addresses and geo, emitting only facility /
            provider NAMES (never an `address`/`geo` fact).
  Layer 2 — this guard, run at integration time BEFORE a candidate is lowered to
            an IntegrationIntent: any fact whose predicate is in the location-lock
            set, on a health EMR entity, is routed to a `low_confidence`
            (`subkind=firewall_address`) review card and NEVER committed. The card
            is filed by `integrate.file_firewall_cards` from the catches the
            importer returns — a hold the owner is never told about is half a
            control, so the two halves must stay wired together.

The lock set is deliberately the UNION of the Located-facet predicates
(`address`, `geo`) and the extraction floor dict (`geocoordinates`, `latitude`,
`longitude`, `gpscoordinates`) — because `geo` is NOT in the floor dict, a
floor-only guard would let a stray `geo` fact slip through. Building the set as
this explicit union closes that gap (§3.6).
"""

from __future__ import annotations

# The two Located-facet predicates (structured postal address + geo). These are
# NOT floored to `location` by the extraction floor dict — that is exactly why the
# lock set must include them explicitly (a floor-only guard would miss `geo`).
_LOCATED_FACET_LOCKS = frozenset({"address", "geo"})

# The precise-geo predicates the extraction floor ratchets to `location`
# (`extraction._DOMAIN_BY_PREDICATE`). Mirrored here — a drift guard test asserts
# each is still floored and that `address`/`geo` are still NOT, so the union stays
# necessary and sufficient.
_FLOOR_LOCKS = frozenset({"geocoordinates", "latitude", "longitude", "gpscoordinates"})

#: Predicates that must never land as a fact on a health EMR entity.
LOCATION_LOCK_PREDICATES = _LOCATED_FACET_LOCKS | _FLOOR_LOCKS

#: The EMR entity kinds the guard protects. Entity `kind` is stored as either the
#: schema type id or its schema.org name, so both spellings are covered by the
#: case-insensitive membership test (e.g. `lab_result`/`Observation`).
HEALTH_EMR_KINDS = frozenset(
    {"observation", "lab_result", "encounter", "person", "organization", "medical_condition"}
)

#: The review-card kind + payload discriminator a caught fact routes to (§6.6).
FIREWALL_REVIEW_KIND = "low_confidence"
FIREWALL_REVIEW_SUBKIND = "firewall_address"


def is_location_locked(predicate: str, entity_kind: str) -> bool:
    """True when a fact `predicate` on an entity of `entity_kind` is a location
    leak that must be held out of the health domain (Layer 2). Deterministic and
    total — the caller routes a True to a `firewall_address` review card and never
    commits the fact."""
    return (
        predicate.strip().lower() in LOCATION_LOCK_PREDICATES
        and entity_kind.strip().lower() in HEALTH_EMR_KINDS
    )
