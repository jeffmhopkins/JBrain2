"""The owner-only location read tools (L2): where_is / where_was_i (#5),
device_status (#10), home_status (#11), nearby_now (#12).

Handler-level (no DB): the faked repos return shaped values; these assert the
registration-time full-owner WRAPPER refuses every tool for a non-full-owner
session BEFORE any read, plus field mapping, stale-fix flagging, and the
ambiguous/unlinked subject paths. The wrapper raises `LocationToolRefusal` (which
the loop surfaces as a safe error), so the refusal is proven by the raise."""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.agent.locationtools import build_location_handlers
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.locations import (
    DeviceActivity,
    FixPoint,
    LatestPlace,
    LocationToolRefusal,
    NearbyPlace,
    NearestFix,
    RosterEntry,
)

# A full owner, a narrowed agent owner, and a non-owner — the three the wrapper
# must distinguish (only the first is allowed).
FULL_OWNER = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
NARROWED_OWNER = ToolContext(
    session=SessionContext(principal_kind="owner", owner_scoped=True), scopes=()
)
NON_OWNER = ToolContext(session=SessionContext(principal_kind="capability_token"), scopes=())

_NOW = datetime.now(UTC)


class _LinkedPerson:
    def __init__(self, name: str) -> None:
        self.entity_id = "e-" + name
        self.canonical_name = name


class FakeDevices:
    """Stands in for SqlDeviceRepo: a name→entity→subject binding map and per-subject
    linked-person labels."""

    def __init__(
        self,
        *,
        subject_by_entity: dict[str, str | None] | None = None,
        linked: dict[str, str] | None = None,
    ) -> None:
        self.subject_by_entity = subject_by_entity or {}
        self.linked = linked or {}

    async def subject_for_person(self, ctx, entity_id):  # noqa: ANN001
        return self.subject_by_entity.get(entity_id)

    async def linked_person(self, ctx, subject_id):  # noqa: ANN001
        name = self.linked.get(subject_id)
        return _LinkedPerson(name) if name else None


class FakeEntities:
    """Name → candidate entity rows (the slice EntityResolver needs)."""

    def __init__(self, by_query: dict[str, list[dict]] | None = None) -> None:
        self.by_query = by_query or {}

    async def list_entities(self, ctx, q=None, kind=None, limit=200):  # noqa: ANN001
        return self.by_query.get(q or "", [])


class FakeLocations:
    def __init__(
        self,
        *,
        latest: LatestPlace | None = None,
        near: NearestFix | None = None,
        activity: dict[str, DeviceActivity] | None = None,
        roster: list[RosterEntry] | None = None,
        nearby: list[NearbyPlace] | None = None,
    ) -> None:
        self._latest = latest
        self._near = near
        self._activity = activity or {}
        self._roster = roster or []
        self._nearby = nearby or []
        self.nearby_calls: list[dict] = []

    async def latest_place(self, ctx, *, subject_id):  # noqa: ANN001
        return self._latest

    async def nearest_fix(self, ctx, *, subject_id, at, max_gap_seconds):  # noqa: ANN001
        return self._near

    async def device_activity(self, ctx):  # noqa: ANN001
        return self._activity

    async def home_roster(self, ctx):  # noqa: ANN001
        return self._roster

    async def nearby(self, ctx, *, subject_id=None, center=None, radius_m, limit):  # noqa: ANN001
        self.nearby_calls.append(
            {"subject_id": subject_id, "center": center, "radius_m": radius_m, "limit": limit}
        )
        return self._nearby


def _entity(eid: str, name: str, kind: str = "Device") -> dict:
    return {"id": eid, "canonical_name": name, "kind": kind, "domain": "location"}


def _near(captured_at: datetime, gap: float) -> NearestFix:
    return NearestFix(
        fix=FixPoint(
            captured_at=captured_at, latitude=0.0, longitude=0.0, accuracy_m=None, battery_pct=None
        ),
        gap_seconds=gap,
    )


def _handlers(*, locations=None, devices=None, entities=None):  # noqa: ANN001
    return build_location_handlers(
        locations or FakeLocations(),  # type: ignore[arg-type]
        devices or FakeDevices(),  # type: ignore[arg-type]
        entities or FakeEntities(),  # type: ignore[arg-type]
    )


# --- the un-forgettable registration-time wrapper -----------------------------


@pytest.mark.parametrize(
    "tool", ["where_is", "where_was_i", "device_status", "home_status", "nearby_now"]
)
@pytest.mark.parametrize("ctx", [NARROWED_OWNER, NON_OWNER])
async def test_every_tool_refuses_a_non_full_owner(tool: str, ctx: ToolContext) -> None:
    # The wrapper raises BEFORE the handler runs — proven both by the raise and by
    # the repos never being touched (a fresh FakeLocations records no nearby call).
    loc = FakeLocations()
    handlers = _handlers(locations=loc)
    with pytest.raises(LocationToolRefusal):
        await handlers[tool]({"subject": "Jeff"}, ctx)
    assert loc.nearby_calls == []


# --- where_is / where_was_i (#5) ----------------------------------------------


async def test_where_is_reports_current_place_and_freshness() -> None:
    devices = FakeDevices(subject_by_entity={"d1": "s1"})
    entities = FakeEntities({"Phone": [_entity("d1", "Phone")]})
    locations = FakeLocations(
        latest=LatestPlace("p1", "Office", _NOW - timedelta(minutes=2)),
        near=_near(_NOW - timedelta(minutes=1), 60.0),
    )
    handlers = _handlers(locations=locations, devices=devices, entities=entities)
    out = await handlers["where_is"]({"subject": "Phone"}, FULL_OWNER)
    assert "Phone is at Office" in out
    assert "STALE" not in out


async def test_where_is_flags_a_stale_fix() -> None:
    devices = FakeDevices(subject_by_entity={"d1": "s1"})
    entities = FakeEntities({"Phone": [_entity("d1", "Phone")]})
    # A fix from 2 hours ago, but the geofence still reports inside: must flag stale.
    locations = FakeLocations(
        latest=LatestPlace("p1", "Office", _NOW - timedelta(hours=3)),
        near=_near(_NOW - timedelta(hours=2), 7200.0),
    )
    handlers = _handlers(locations=locations, devices=devices, entities=entities)
    out = await handlers["where_is"]({"subject": "Phone"}, FULL_OWNER)
    assert "STALE" in out


async def test_where_is_unlinked_subject_is_handled() -> None:
    # The entity exists but has no device binding → graceful "no linked device".
    entities = FakeEntities({"Grandma": [_entity("p9", "Grandma", kind="Person")]})
    handlers = _handlers(entities=entities, devices=FakeDevices(subject_by_entity={"p9": None}))
    out = await handlers["where_is"]({"subject": "Grandma"}, FULL_OWNER)
    assert "no linked device" in out


async def test_where_is_unknown_subject_is_handled() -> None:
    handlers = _handlers(entities=FakeEntities({}))
    out = await handlers["where_is"]({"subject": "Nobody"}, FULL_OWNER)
    assert "No person or device named 'Nobody'" in out


async def test_where_is_ambiguous_subject_asks() -> None:
    devices = FakeDevices(subject_by_entity={"d1": "s1", "d2": "s2"})
    entities = FakeEntities({"Phone": [_entity("d1", "Phone"), _entity("d2", "Old Phone")]})
    handlers = _handlers(devices=devices, entities=entities)
    out = await handlers["where_is"]({"subject": "Phone"}, FULL_OWNER)
    assert "more than one" in out


async def test_where_is_needs_a_subject() -> None:
    handlers = _handlers()
    out = await handlers["where_is"]({}, FULL_OWNER)
    assert "needs a subject" in out


async def test_where_was_i_uses_the_owners_own_device() -> None:
    devices = FakeDevices(subject_by_entity={"me": "s-me"})
    entities = FakeEntities({"Me": [_entity("me", "Me", kind="Person")]})
    locations = FakeLocations(
        latest=LatestPlace("p1", "Home", _NOW - timedelta(minutes=1)),
        near=_near(_NOW - timedelta(seconds=30), 30.0),
    )
    handlers = _handlers(locations=locations, devices=devices, entities=entities)
    out = await handlers["where_was_i"]({}, FULL_OWNER)
    assert "You is at Home" in out  # label is "You"


async def test_where_was_i_unlinked_owner_device() -> None:
    handlers = _handlers(entities=FakeEntities({"Me": []}))
    out = await handlers["where_was_i"]({}, FULL_OWNER)
    assert "isn't linked" in out


# --- device_status (#10) ------------------------------------------------------


async def test_device_status_labels_and_tones() -> None:
    devices = FakeDevices(linked={"s1": "Jeff", "s2": "Celine"})
    activity = {
        "s1": DeviceActivity("s1", _NOW - timedelta(minutes=5), 12, "wifi", 100),  # fresh, low batt
        "s2": DeviceActivity("s2", _NOW - timedelta(hours=3), 80, "mobile", 5),  # stale, ok batt
    }
    locations = FakeLocations(activity=activity)
    handlers = _handlers(locations=locations, devices=devices)
    out = await handlers["device_status"]({}, FULL_OWNER)
    assert "Jeff" in out and "Celine" in out
    assert "fresh" in out and "stale" in out
    assert "(low)" in out and "(ok)" in out


async def test_device_status_unlinked_device_label() -> None:
    activity = {"s9": DeviceActivity("s9", _NOW, 50, "wifi", 1)}
    handlers = _handlers(locations=FakeLocations(activity=activity), devices=FakeDevices())
    out = await handlers["device_status"]({}, FULL_OWNER)
    assert "unlinked device" in out


async def test_device_status_empty() -> None:
    out = await _handlers()["device_status"]({}, FULL_OWNER)
    assert "No devices" in out


# --- home_status (#11) --------------------------------------------------------


async def test_home_status_reports_place_and_freshness() -> None:
    roster = [
        RosterEntry(
            "s1", "Jeff", "p1", "Home", _NOW - timedelta(minutes=10), _NOW - timedelta(minutes=2)
        ),
        RosterEntry("s2", "Celine", None, None, None, _NOW - timedelta(hours=4)),
    ]
    out = await _handlers(locations=FakeLocations(roster=roster))["home_status"]({}, FULL_OWNER)
    assert "Jeff: Home" in out
    assert "Celine: not at a saved place" in out
    # Celine's last fix is 4h old → flagged stale, never "here now".
    assert "STALE" in out


async def test_home_status_empty() -> None:
    out = await _handlers()["home_status"]({}, FULL_OWNER)
    assert "No one's location is known" in out


# --- nearby_now (#12) ---------------------------------------------------------


async def test_nearby_now_names_and_distances_only() -> None:
    devices = FakeDevices(subject_by_entity={"me": "s-me"})
    entities = FakeEntities({"Me": [_entity("me", "Me", kind="Person")]})
    locations = FakeLocations(
        nearby=[NearbyPlace("p1", "Cafe", 123.0), NearbyPlace("p2", "Gym", 980.0)]
    )
    handlers = _handlers(locations=locations, devices=devices, entities=entities)
    out = await handlers["nearby_now"]({"radius_m": 1500, "limit": 5}, FULL_OWNER)
    assert "Cafe: 120 m" in out and "Gym: 980 m" in out
    # The owner's subject drove the query; no coordinate was passed as a center.
    assert locations.nearby_calls[0]["subject_id"] == "s-me"
    assert locations.nearby_calls[0]["center"] is None


async def test_nearby_now_clamps_radius_and_limit() -> None:
    devices = FakeDevices(subject_by_entity={"me": "s-me"})
    entities = FakeEntities({"Me": [_entity("me", "Me")]})
    locations = FakeLocations()
    handlers = _handlers(locations=locations, devices=devices, entities=entities)
    await handlers["nearby_now"]({"radius_m": 10_000_000, "limit": 9999}, FULL_OWNER)
    call = locations.nearby_calls[0]
    assert call["radius_m"] == 50_000.0  # max radius
    assert call["limit"] == 20  # max limit


async def test_nearby_now_unlinked_owner_device() -> None:
    out = await _handlers(entities=FakeEntities({"Me": []}))["nearby_now"]({}, FULL_OWNER)
    assert "isn't linked" in out
