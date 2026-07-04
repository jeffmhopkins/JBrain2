"""The JPet broadcaster (docs/plans/JPET_PLAN.md W1) — in-process fan-out.

Proves a published state reaches every subscriber, a slow subscriber drops its
oldest snapshot instead of blocking, and unsubscribe removes it.
"""

from datetime import UTC, datetime

from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.service import Drives, PetStateInfo


def _state(food: float) -> PetStateInfo:
    return PetStateInfo(
        id="p",
        name="Blink",
        domain="general",
        drives=Drives(food=food, energy=80, fun=70, love=70),
        mood="happy",
        emotion="happy",
        speech=None,
        asleep=False,
        pos_x=0,
        pos_z=0,
        target_x=0,
        target_z=0,
        facing=0,
        action="idle",
        last_tick_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_publish_reaches_every_subscriber() -> None:
    b = PetBroadcaster()
    a, c = b.subscribe(), b.subscribe()
    assert b.subscriber_count == 2
    b.publish(_state(50))
    assert (await a.get()).drives.food == 50
    assert (await c.get()).drives.food == 50


async def test_full_queue_drops_oldest() -> None:
    b = PetBroadcaster(maxsize=2)
    q = b.subscribe()
    for food in (10, 20, 30):  # 3 into a size-2 queue → oldest (10) dropped
        b.publish(_state(food))
    assert [(q.get_nowait()).drives.food for _ in range(2)] == [20, 30]


async def test_unsubscribe_stops_delivery() -> None:
    b = PetBroadcaster()
    q = b.subscribe()
    b.unsubscribe(q)
    assert b.subscriber_count == 0
    b.publish(_state(99))
    assert q.empty()
