"""jerv's owner-location tool: `current_location` — the `web`-gated, jerv-only
on-box read that reconstructs the full owner ctx to clear require_full_owner."""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.agent.loop import ToolContext
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.db.session import SessionContext
from jbrain.geocode import GeocodeResult
from jbrain.locations import FixPoint, LatestPlace, NearestFix


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
    def __init__(self, near: NearestFix | None, place: LatestPlace | None) -> None:
        self._near = near
        self._place = place

    async def device_activity(self, ctx):  # noqa: ANN001, ANN201
        return {}

    async def nearest_fix(self, ctx, *, subject_id, at, max_gap_seconds):  # noqa: ANN001, ANN201
        return self._near

    async def latest_place(self, ctx, *, subject_id):  # noqa: ANN001, ANN201
        return self._place


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


def _tool(loc: _Loc, dev: _Dev):  # noqa: ANN202
    return build_presence_handlers(loc, dev)["current_location"]  # type: ignore[arg-type]


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
async def test_current_location_prefers_the_live_pwa_fix_reverse_geocoded() -> None:
    # A turn carrying the PWA's live coords answers from them (on-box reverse-geocode),
    # never touching the OwnTracks device stack — coordinate-free output.
    geo = _Geo(GeocodeResult(label="Springfield, IL", latitude=39.8, longitude=-89.6))
    tool = build_presence_handlers(_Loc(None, None), _Dev([]), geo)["current_location"]
    out = await tool({}, _here_ctx(39.8, -89.6))
    assert "Springfield, IL" in out
    assert "39.8" not in out and "-89.6" not in out


@pytest.mark.asyncio
async def test_current_location_live_fix_geocoder_miss_stays_coordinate_free() -> None:
    tool = build_presence_handlers(_Loc(None, None), _Dev([]), _Geo(None))["current_location"]
    out = await tool({}, _here_ctx(39.8, -89.6))
    assert "couldn't resolve it to a place name" in out
    assert "39.8" not in out


@pytest.mark.asyncio
async def test_current_location_without_a_live_fix_falls_back_to_the_device() -> None:
    # No live coords on the turn → the OwnTracks device presence read (the prior path).
    tool = build_presence_handlers(_at_home(), _Dev(["s1"]), _Geo(None))["current_location"]
    out = await tool({}, _jerv_ctx())
    assert "currently at Home" in out


@pytest.mark.asyncio
async def test_current_location_refuses_a_non_owner_principal() -> None:
    session = SessionContext(principal_id="d", principal_kind="device_key")
    ctx = ToolContext(session=session, scopes=())
    out = await _tool(_at_home(), _Dev(["s1"]))({}, ctx)
    assert out == "I can't check the owner's location in this session."
