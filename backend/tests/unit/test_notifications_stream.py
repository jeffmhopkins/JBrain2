"""The notifications SSE stream logic (api/notifications.py), driven without the transport:
the opening comment, per-event frames, keepalive on idle, and unsubscribe on exit."""

import pytest

from jbrain.api.notifications import sse_event, stream_notifications
from jbrain.notify import Notification, NotifyBus

NOTE = Notification(kind="task_run", title="Task ready: Morning brief", body="done", ref="sess-1")


def test_sse_event_frame() -> None:
    frame = sse_event(NOTE).decode()
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert '"kind": "task_run"' in frame
    assert '"ref": "sess-1"' in frame


def _disconnect_after(n: int):  # type: ignore[no-untyped-def]
    """An is_disconnected() that returns False for the first `n` checks, then True."""
    calls = {"n": 0}

    async def check() -> bool:
        calls["n"] += 1
        return calls["n"] > n

    return check


@pytest.mark.asyncio
async def test_streams_connected_then_the_event_then_unsubscribes() -> None:
    bus = NotifyBus()
    # Publish before the generator subscribes? No — subscribe happens on first iteration,
    # so publish after we've pulled the opening comment.
    gen = stream_notifications(bus, _disconnect_after(1), keepalive_s=5.0)
    assert await gen.__anext__() == b": connected\n\n"  # opening comment, now subscribed
    bus.publish(NOTE)
    assert await gen.__anext__() == sse_event(NOTE)  # the event frame
    # The next loop check reports disconnected → the generator ends and unsubscribes.
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_keepalive_on_idle() -> None:
    bus = NotifyBus()
    gen = stream_notifications(bus, _disconnect_after(1), keepalive_s=0.01)
    assert await gen.__anext__() == b": connected\n\n"
    # No event published → the queue wait times out and a keepalive comment is yielded.
    assert await gen.__anext__() == b": keepalive\n\n"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_unsubscribes_even_if_the_consumer_aborts() -> None:
    bus = NotifyBus()
    gen = stream_notifications(bus, _disconnect_after(5), keepalive_s=5.0)
    assert await gen.__anext__() == b": connected\n\n"
    assert bus.subscriber_count == 1
    await gen.aclose()  # consumer goes away mid-stream
    assert bus.subscriber_count == 0
