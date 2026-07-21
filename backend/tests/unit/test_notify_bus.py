"""The NotifyBus in-process fan-out (notify/bus.py): fan-out to all subscribers,
overflow drops the oldest, unsubscribe stops delivery, and notify_owner is a safe no-op
without a bus. No DB, no I/O."""

import asyncio

from jbrain.notify import Notification, NotifyBus, notify_owner

NOTE = Notification(kind="task_run", title="Task ready", body="done", ref="sess-1")


def test_fan_out_to_every_subscriber() -> None:
    bus = NotifyBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish(NOTE)
    assert a.get_nowait() is NOTE
    assert b.get_nowait() is NOTE


def test_unsubscribe_stops_delivery() -> None:
    bus = NotifyBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish(NOTE)
    assert q.empty()
    assert bus.subscriber_count == 0


def test_overflow_drops_the_oldest() -> None:
    bus = NotifyBus(maxsize=2)
    q = bus.subscribe()
    first = Notification(kind="task_run", title="1", body="", ref="")
    second = Notification(kind="task_run", title="2", body="", ref="")
    third = Notification(kind="task_run", title="3", body="", ref="")
    bus.publish(first)
    bus.publish(second)
    bus.publish(third)  # full → the oldest (first) is dropped to make room
    assert [q.get_nowait().title for _ in range(2)] == ["2", "3"]
    assert q.empty()


def test_notify_owner_is_a_safe_noop_without_a_bus() -> None:
    # No bus configured → no error, nothing delivered.
    notify_owner(None, NOTE)


def test_notify_owner_publishes_to_a_bus() -> None:
    bus = NotifyBus()
    q = bus.subscribe()
    notify_owner(bus, NOTE)
    assert q.get_nowait() is NOTE


def test_subscriber_count_tracks_lifecycle() -> None:
    bus = NotifyBus()
    assert bus.subscriber_count == 0
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    assert bus.subscriber_count == 2
    bus.unsubscribe(q1)
    bus.unsubscribe(q2)
    assert bus.subscriber_count == 0


def test_queue_is_asyncio_queue() -> None:
    # The SSE endpoint awaits q.get(); confirm the subscribe() handle is awaitable-shaped.
    bus = NotifyBus()
    q = bus.subscribe()
    assert isinstance(q, asyncio.Queue)
