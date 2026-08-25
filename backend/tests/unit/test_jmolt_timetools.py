"""jmolt's `time_left` tool (docs/plans/JMOLT_SITTINGS_PLAN.md).

The pure message builder + the handler over a fake settings store. Proves it reports the
minutes remaining from the stored night deadline, clamps to 0 once the hour is over, and
says "not running" when no night is in flight — all on the trusted local clock.
"""

from datetime import UTC, datetime, timedelta

from jbrain.agent.jmolttimetools import build_jmolt_time_handlers, time_left_message
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from tests.unit.fakes import FakeSettingsStore

_NOW = datetime(2026, 8, 26, 3, 20, tzinfo=UTC)


def test_message_reports_minutes_remaining() -> None:
    deadline = datetime(2026, 8, 26, 4, 0, tzinfo=UTC).isoformat()  # 40 min out
    out = time_left_message(deadline, "UTC", _NOW)
    assert "03:20 (UTC)" in out
    assert "About 40 minute(s) remain" in out


def test_message_uses_the_local_timezone() -> None:
    deadline = datetime(2026, 8, 26, 4, 0, tzinfo=UTC).isoformat()
    out = time_left_message(deadline, "America/New_York", _NOW)
    assert "23:20 (America/New_York)" in out  # 03:20 UTC is 23:20 the previous evening EDT


def test_message_clamps_past_deadline_to_over() -> None:
    deadline = datetime(2026, 8, 26, 3, 0, tzinfo=UTC).isoformat()  # already passed
    out = time_left_message(deadline, "UTC", _NOW)
    assert "over" in out.lower() and "remain" not in out


def test_message_blank_deadline_is_not_running() -> None:
    assert "not running" in time_left_message("", "UTC", _NOW).lower()
    # A malformed stored value is treated the same, never a 500.
    assert "not running" in time_left_message("not-a-timestamp", "UTC", _NOW).lower()


async def test_handler_reads_the_stored_deadline_and_timezone() -> None:
    store = FakeSettingsStore()
    store.values["owner_timezone"] = "UTC"
    store.values["moltbook_night_deadline"] = (
        datetime.now(UTC) + timedelta(minutes=25)
    ).isoformat()
    handlers = build_jmolt_time_handlers(maker=None, settings_store=store)  # type: ignore[arg-type]
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    out = await handlers["time_left"]({}, ctx)
    assert "(UTC)" in out and "minute(s) remain" in out


async def test_handler_when_no_night_is_running() -> None:
    store = FakeSettingsStore()  # no deadline stamped
    handlers = build_jmolt_time_handlers(maker=None, settings_store=store)  # type: ignore[arg-type]
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    assert "not running" in (await handlers["time_left"]({}, ctx)).lower()
