"""jerv's owner-location tool: `current_location` — the `web`-gated, jerv-only
on-box read that reconstructs the full owner ctx to clear require_full_owner."""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.agent.loop import ToolContext
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.db.session import SessionContext
from jbrain.geocode import GeocodeResult
from jbrain.locations import FixPoint, LatestPlace, NearbyPlace, NearestFix


class _Geo:
    def __init__(self, result: GeocodeResult | None) -> None:
        self._result = result

    async def reverse(self, latitude, longitude):  # noqa: ANN001, ANN201
        return self._result

    async def forward(self, query, limit=5):  # noqa: ANN001, ANN201
        return []


def _here_ctx(lat: float, lon: float) -> ToolContext:
    session = SessionContext(principal_id="own", principal_kind="owner", owner_scoped=True)
    return ToolContext(session=session, scopes=(), here=(lat, lon))


class _Loc:
    def __init__(
        self,
        near: NearestFix | None,
        place: LatestPlace | None,
        nearby: list[NearbyPlace] | None = None,
    ) -> None:
        self._near = near
        self._place = place
        self._nearby = nearby or []

    async def device_activity(self, ctx):  # noqa: ANN001, ANN201
        return {}

    async def nearest_fix(self, ctx, *, subject_id, at, max_gap_seconds):  # noqa: ANN001, ANN201
        return self._near

    async def latest_place(self, ctx, *, subject_id):  # noqa: ANN001, ANN201
        return self._place

    async def nearby(self, ctx, *, subject_id=None, center=None, radius_m, limit):  # noqa: ANN001, ANN201
        return self._nearby


class _Dev:
    def __init__(self, subs: list[str]) -> None:
        self._subs = subs

    async def owner_device_subjects(self, ctx):  # noqa: ANN001, ANN201
        return self._subs


def _jerv_ctx() -> ToolContext:
    # jerv's tool session is owner_scoped with empty scopes — the case the tool must
    # clear by reconstructing a full owner ctx.
    session = SessionContext(principal_id="own", principal_kind="owner", owner_scoped=True)
    return ToolContext(session=session, scopes=())


def _fresh_near() -> NearestFix:
    captured = datetime.now(UTC) - timedelta(minutes=3)
    return NearestFix(fix=FixPoint(captured, 40.0, -74.0, 10, 80), gap_seconds=180)


def _tool(loc: _Loc, dev: _Dev, geo: "_Geo | None" = None):  # noqa: ANN202
    return build_presence_handlers(loc, dev, geo)["current_location"]  # type: ignore[arg-type]


def _at_home() -> _Loc:
    return _Loc(_fresh_near(), LatestPlace("e", "Home", datetime.now(UTC)))


@pytest.mark.asyncio
async def test_current_location_returns_the_owner_place_coordinate_free() -> None:
    out = await _tool(_at_home(), _Dev(["s1"]))({}, _jerv_ctx())
    assert "currently at Home" in out
    # Never a coordinate.
    assert "40.0" not in out and "-74.0" not in out


@pytest.mark.asyncio
async def test_current_location_reports_no_recent_fix() -> None:
    out = await _tool(_Loc(None, None), _Dev(["s1"]))({}, _jerv_ctx())
    assert "don't have a recent location fix" in out


@pytest.mark.asyncio
async def test_current_location_prefers_a_saved_place_near_the_live_fix() -> None:
    # The most meaningful, geocoder-independent answer: a saved place near the live
    # point, named with a rounded distance — never a coordinate.
    loc = _Loc(None, None, nearby=[NearbyPlace(place_entity_id="p1", name="Home", distance_m=42.0)])
    out = await _tool(loc, _Dev([]), _Geo(None))({}, _here_ctx(39.8, -89.6))
    assert "at Home" in out
    assert "39.8" not in out and "-89.6" not in out


@pytest.mark.asyncio
async def test_current_location_reports_a_nearby_saved_place_with_distance() -> None:
    loc = _Loc(
        None, None, nearby=[NearbyPlace(place_entity_id="p2", name="Office", distance_m=823.0)]
    )
    out = await _tool(loc, _Dev([]), _Geo(None))({}, _here_ctx(39.8, -89.6))
    assert "near Office" in out and "820 m" in out


@pytest.mark.asyncio
async def test_current_location_falls_to_reverse_geocode_when_no_saved_place() -> None:
    # No saved place near the live fix → on-box reverse-geocode to an address label.
    geo = _Geo(GeocodeResult(label="Springfield, IL", latitude=39.8, longitude=-89.6))
    out = await _tool(_Loc(None, None), _Dev([]), geo)({}, _here_ctx(39.8, -89.6))
    assert "Springfield, IL" in out
    assert "39.8" not in out and "-89.6" not in out


@pytest.mark.asyncio
async def test_current_location_live_fix_no_place_and_no_geocoder_stays_honest() -> None:
    out = await _tool(_Loc(None, None), _Dev([]), _Geo(None))({}, _here_ctx(39.8, -89.6))
    assert "isn't near any of their saved places" in out
    assert "39.8" not in out


@pytest.mark.asyncio
async def test_current_location_without_a_live_fix_falls_back_to_the_device() -> None:
    # No live coords on the turn → the OwnTracks device presence read (the prior path).
    out = await _tool(_at_home(), _Dev(["s1"]), _Geo(None))({}, _jerv_ctx())
    assert "currently at Home" in out


@pytest.mark.asyncio
async def test_current_location_refuses_a_non_owner_principal() -> None:
    session = SessionContext(principal_id="d", principal_kind="device_key")
    ctx = ToolContext(session=session, scopes=())
    out = await _tool(_at_home(), _Dev(["s1"]))({}, ctx)
    assert out == "I can't check the owner's location in this session."
