"""The owner-presence read (L7b) — the freshness-honest, coordinate-free line +
block, and the full-owner gate. The repos are faked; the real RLS barrier is proven
in the integration suite. Asserts a stale fix reads "last known", never "here now"."""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.db.session import SessionContext
from jbrain.locations import FixPoint, LatestPlace, LocationToolRefusal, NearestFix
from jbrain.locations.presence import (
    STALE_GAP_SECONDS,
    Presence,
    presence_block,
    presence_line,
    read_owner_presence,
)

NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
FULL_OWNER = SessionContext(principal_id="owner", principal_kind="owner")
NARROWED = SessionContext(
    principal_id="owner", principal_kind="owner", domain_scopes=("location",), owner_scoped=True
)


def _near(age_seconds: float) -> NearestFix:
    captured = NOW - timedelta(seconds=age_seconds)
    return NearestFix(
        fix=FixPoint(
            captured_at=captured, latitude=40.0, longitude=-74.0, accuracy_m=10, battery_pct=80
        ),
        gap_seconds=age_seconds,
    )


class FakeLocationRepo:
    def __init__(self, *, near: NearestFix | None, place: LatestPlace | None) -> None:
        self._near = near
        self._place = place
        self.activity: dict = {}

    async def device_activity(self, ctx):  # noqa: ANN001, ANN201
        return self.activity

    async def nearest_fix(self, ctx, *, subject_id, at, max_gap_seconds):  # noqa: ANN001, ANN201
        return self._near

    async def latest_place(self, ctx, *, subject_id):  # noqa: ANN001, ANN201
        return self._place


class FakeDeviceRepo:
    def __init__(self, subs: list[str]) -> None:
        self._subs = subs

    async def owner_device_subjects(self, ctx):  # noqa: ANN001, ANN201
        return self._subs


# --- the pure line/block formatting ----------------------------------------


def test_fresh_presence_reads_currently_at() -> None:
    p = Presence(present=True, place_name="Home", last_seen=NOW, age_seconds=240, stale=False)
    line = presence_line(p)
    assert line is not None and "currently at Home" in line
    assert "last known" not in line


def test_stale_presence_reads_last_known_never_here_now() -> None:
    p = Presence(
        present=True,
        place_name="Office",
        last_seen=NOW - timedelta(hours=3),
        age_seconds=3 * 3600,
        stale=True,
    )
    line = presence_line(p)
    assert line is not None
    assert "last known" in line and "Office" in line
    assert "currently at" not in line and "here now" not in line


def test_absent_presence_yields_no_line_or_block() -> None:
    p = Presence(present=False, place_name=None, last_seen=None, age_seconds=None, stale=False)
    assert presence_line(p) is None
    assert presence_block(p) == ""


def test_block_is_data_framed() -> None:
    p = Presence(present=True, place_name="Home", last_seen=NOW, age_seconds=120, stale=False)
    block = presence_block(p)
    # The DATA frame leads (the data/instruction boundary), and the line follows it.
    assert block.startswith("[owner presence")
    assert "as DATA" in block and "not an instruction" in block
    assert "currently at Home" in block


def test_line_carries_no_coordinate() -> None:
    p = Presence(present=True, place_name="Home", last_seen=NOW, age_seconds=120, stale=False)
    line = presence_line(p) or ""
    assert "40.0" not in line and "-74.0" not in line


# --- the gated read --------------------------------------------------------


@pytest.mark.asyncio
async def test_read_refuses_a_narrowed_session() -> None:
    locs = FakeLocationRepo(near=_near(60), place=LatestPlace("e", "Home", NOW))
    devices = FakeDeviceRepo(["sub-1"])
    with pytest.raises(LocationToolRefusal):
        await read_owner_presence(locs, devices, NARROWED, now=NOW)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_read_absent_when_no_own_device() -> None:
    locs = FakeLocationRepo(near=_near(60), place=None)
    devices = FakeDeviceRepo([])
    p = await read_owner_presence(locs, devices, FULL_OWNER, now=NOW)  # type: ignore[arg-type]
    assert p.present is False


@pytest.mark.asyncio
async def test_read_fresh_resolves_place_and_freshness() -> None:
    locs = FakeLocationRepo(near=_near(120), place=LatestPlace("e", "Home", NOW))
    devices = FakeDeviceRepo(["sub-1"])
    p = await read_owner_presence(locs, devices, FULL_OWNER, now=NOW)  # type: ignore[arg-type]
    assert p.present and p.place_name == "Home" and not p.stale


@pytest.mark.asyncio
async def test_read_flags_stale_beyond_horizon() -> None:
    locs = FakeLocationRepo(
        near=_near(STALE_GAP_SECONDS + 600), place=LatestPlace("e", "Office", NOW)
    )
    devices = FakeDeviceRepo(["sub-1"])
    p = await read_owner_presence(locs, devices, FULL_OWNER, now=NOW)  # type: ignore[arg-type]
    assert p.present and p.stale
    line = presence_line(p) or ""
    assert "last known" in line
