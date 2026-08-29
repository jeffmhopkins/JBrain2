"""The box's narration of its own GPU work (jbrain.box_events).

What matters here is the CONTRACT the vitals surface reads against: a slow piece of work
is recorded when it STARTS (so the screen can say "loading gpt-oss-120b…" during the
spike, not after it), the reason the caller supplied rides along, a failure is recorded as
a failure and still propagates, and — the load-bearing one — none of it can ever break the
work it narrates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jbrain import box_events


@pytest.fixture(autouse=True)
def _unwired() -> Any:
    """Every test starts with the writer unwired, and leaves it that way — this is
    process-global state, and a leaked maker would have the rest of the suite trying to
    open sessions against nothing."""
    box_events.reset()
    yield
    box_events.reset()


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture what would be written, so the recorder's contract can be pinned without a
    database. The real writes are covered end-to-end in the integration test."""
    captured: list[tuple[str, dict[str, Any]]] = []

    async def fake_write(sql: str, params: dict[str, Any]) -> None:
        captured.append((sql, params))

    monkeypatch.setattr(box_events, "_write", fake_write)
    return captured


async def test_a_slow_piece_of_work_is_recorded_when_it_starts(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    """The open row is the whole point: the surface must be able to say what is happening
    WHILE the GPU is pinned, not account for it once the trace has come back down."""
    async with box_events.span(box_events.MODEL_LOAD, "gpt-oss-120b"):
        opened = [p for _, p in writes]
        assert len(opened) == 1
        assert opened[0]["kind"] == box_events.MODEL_LOAD
        assert opened[0]["subject"] == "gpt-oss-120b"
        # The open row's status and null end are in the SQL, not the parameters — the row
        # exists, unfinished, for as long as the work runs.
        assert "'running'" in writes[0][0] and "NULL" in writes[0][0]

    settled = writes[-1][1]
    assert settled["status"] == "ok"
    assert settled["id"] == writes[0][1]["id"]  # the same row, closed


async def test_the_reason_the_caller_knew_rides_along(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    """`because` is the difference between a log and an explanation: the gateway records
    the unload, but only the evictor knows it was to make room for something."""
    with box_events.because("to make room for gpt-oss-120b"):
        await box_events.record(box_events.MODEL_UNLOAD, "qwen35")

    assert writes[0][1]["detail"] == "to make room for gpt-oss-120b"


async def test_the_reason_does_not_leak_past_its_block(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    with box_events.because("an image render needs the whole memory pool"):
        pass
    await box_events.record(box_events.MODEL_UNLOAD, "qwen35")

    assert writes[0][1]["detail"] == ""


async def test_a_failed_load_is_recorded_as_failed_and_still_raises(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    """A load that fails is the caller's problem — the narration says so and gets out of
    the way, rather than swallowing the error into a silent 'ok'."""
    with pytest.raises(RuntimeError):
        async with box_events.span(box_events.MODEL_LOAD, "gpt-oss-120b"):
            raise RuntimeError("gateway refused")

    settled = writes[-1][1]
    assert settled["status"] == "failed"
    assert "gateway refused" in str(settled["detail"])


async def test_an_over_long_reason_is_clipped(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    await box_events.record(box_events.MODEL_UNLOAD, "qwen35", detail="x" * 500)

    assert len(str(writes[0][1]["detail"])) == box_events._DETAIL_MAX


async def test_an_unwired_process_narrates_nothing_and_does_not_fail() -> None:
    """The CLI, the tests, a one-shot script — none of them configure a writer, and a
    model load there must behave exactly as it did before this existed."""
    async with box_events.span(box_events.MODEL_LOAD, "gpt-oss-120b"):
        pass
    await box_events.record(box_events.MODEL_UNLOAD, "qwen35")


async def test_a_database_failure_cannot_break_the_work_being_narrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard rule: this is a narration of work, never part of it. A load must not fail
    because the box could not write down that it was happening."""

    class Exploding:
        def __call__(self) -> Any:
            raise RuntimeError("database is down")

    monkeypatch.setattr(box_events, "_maker", Exploding())
    async with box_events.span(box_events.MODEL_LOAD, "gpt-oss-120b"):
        pass
    await box_events.record(box_events.MODEL_UNLOAD, "qwen35")


def test_a_row_left_open_by_a_dead_process_reads_as_stale_not_as_loading() -> None:
    """A screen that says "loading gpt-oss-120b…" forever is a worse lie than admitting we
    stopped watching — the api can be restarted mid-load, and its row never settles."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=30)
    abandoned = now - box_events.STALE_AFTER - timedelta(seconds=1)

    assert box_events._status("running", fresh, None, now) == "running"
    assert box_events._status("running", abandoned, None, now) == "stale"
    # A settled row is never re-judged, however old it is.
    assert box_events._status("ok", abandoned, now, now) == "ok"


async def test_a_first_reading_of_nothing_opens_no_row(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    """A prefill's first sample regularly lands before the first batch is through, and most
    turns then answer off their cached prefix. Opening on that zero wrote rows that lived a
    fifth of a second at 0% — five in five minutes on the box, 2026-08-28 — each one a flash
    of "Reading your prompt… 0%" on a status line for a wait that never happened."""
    async with box_events.lazy_span(box_events.PREFILL, "gpt-oss-120b") as (publish, _finish):
        await publish(0.0)
        assert writes == []
        await publish(0.25)
    kinds = [p.get("kind") for _, p in writes]
    assert kinds.count(box_events.PREFILL) == 1  # the row opened at the first real reading


async def test_the_caller_names_what_the_wait_is(
    writes: list[tuple[str, dict[str, Any]]],
) -> None:
    """Only the caller knows what the model is eating — the owner's question on the first
    round, the page a tool just fetched on every round after it (`llm.router._reading`)."""
    async with box_events.lazy_span(
        box_events.PREFILL, "gpt-oss-120b", detail="what web_fetch returned"
    ) as (publish, _finish):
        await publish(0.3)
    assert writes[0][1]["detail"] == "what web_fetch returned"
    assert writes[-1][1]["detail"] == "what web_fetch returned"  # and again on the settle
