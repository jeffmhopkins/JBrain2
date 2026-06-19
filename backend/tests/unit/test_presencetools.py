"""jerv's owner-location tool: `current_location` — the `web`-gated, jerv-only
on-box read that reconstructs the full owner ctx to clear require_full_owner."""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.agent.loop import ToolContext
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.db.session import SessionContext
from jbrain.locations import FixPoint, LatestPlace, NearestFix


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
async def test_current_location_refuses_a_non_owner_principal() -> None:
    session = SessionContext(principal_id="d", principal_kind="device_key")
    ctx = ToolContext(session=session, scopes=())
    out = await _tool(_at_home(), _Dev(["s1"]))({}, ctx)
    assert out == "I can't check the owner's location in this session."
