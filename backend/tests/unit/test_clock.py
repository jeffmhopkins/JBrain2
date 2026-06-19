"""The agent clock: the ambient date/time block and the `current_time` tool."""

from datetime import UTC, datetime

import pytest

from jbrain.agent.clock import _CLOCK_FRAME, build_clock_handlers, now_block
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext

FIXED = datetime(2026, 6, 19, 18, 30, tzinfo=UTC)  # a Friday


def _ctx(tz: str | None) -> ToolContext:
    session = SessionContext(principal_id="p", principal_kind="owner")
    return ToolContext(session=session, scopes=(), timezone=tz)


def test_now_block_is_data_framed_and_names_the_day() -> None:
    block = now_block(None, now=FIXED)
    assert block.startswith(_CLOCK_FRAME)
    # Friday in UTC, with the zone labelled.
    assert "Friday, June 19, 2026, 18:30 (UTC)" in block


def test_now_block_localizes_to_the_owner_zone() -> None:
    # 18:30 UTC is 03:30 the next day (Saturday) in Tokyo (+9).
    block = now_block("Asia/Tokyo", now=FIXED)
    assert "Saturday, June 20, 2026, 03:30 (Asia/Tokyo)" in block


def test_now_block_falls_back_to_utc_for_an_unknown_zone() -> None:
    block = now_block("Mars/Phobos", now=FIXED)
    assert "(UTC)" in block


@pytest.mark.asyncio
async def test_current_time_uses_the_owner_zone_by_default() -> None:
    handler = build_clock_handlers()["current_time"]
    out = await handler({}, _ctx("America/New_York"))
    assert "(America/New_York)" in out


@pytest.mark.asyncio
async def test_current_time_converts_to_a_requested_zone() -> None:
    handler = build_clock_handlers()["current_time"]
    out = await handler({"timezone": "Europe/London"}, _ctx("America/New_York"))
    assert "(Europe/London)" in out


@pytest.mark.asyncio
async def test_current_time_reports_an_unknown_requested_zone_in_utc() -> None:
    handler = build_clock_handlers()["current_time"]
    out = await handler({"timezone": "Nowhere/Bogus"}, _ctx("America/New_York"))
    assert "isn't a known IANA timezone" in out and "(UTC)" in out
