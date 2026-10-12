"""The run announcements an open PWA chat attaches from (`agent.run_bus`, GET /chat/events).

What is pinned: a subscriber hears only its own principal's turns (the security property), a
slow one loses its oldest events instead of holding up a turn, announcing can never break the
turn that announces, and the event stream is SSE the PWA's parser reads — keep-alives through
silence, and the subscription released when the client goes away.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jbrain.agent.run_bus import RunBus, RunEvent, announce
from jbrain.api import agent as agent_api


def _event(principal: str = "owner", run: str = "r1") -> RunEvent:
    return RunEvent(principal, "s1", run, "started")


def test_a_subscriber_hears_only_its_own_principal_s_turns() -> None:
    bus = RunBus()
    mine, theirs = bus.subscribe("owner"), bus.subscribe("debug-token")
    bus.publish(_event("owner"))
    assert mine.get_nowait() == _event("owner")
    assert theirs.empty(), "another principal's session ids never reach this one"


def test_a_slow_subscriber_drops_its_oldest_event() -> None:
    bus = RunBus(maxsize=2)
    q = bus.subscribe("owner")
    for run in ("r1", "r2", "r3"):
        bus.publish(_event(run=run))
    assert [q.get_nowait().run_id, q.get_nowait().run_id] == ["r2", "r3"]


def test_unsubscribing_stops_delivery_and_is_idempotent() -> None:
    bus = RunBus()
    q = bus.subscribe("owner")
    assert bus.subscriber_count == 1
    bus.unsubscribe(q)
    bus.unsubscribe(q)
    bus.publish(_event())
    assert q.empty() and bus.subscriber_count == 0


def test_the_wire_names_the_run_never_the_principal() -> None:
    assert RunEvent("owner", "s1", "r1", "ended").wire() == {
        "type": "run_ended",
        "session_id": "s1",
        "run_id": "r1",
    }


def test_announcing_never_breaks_the_turn() -> None:
    announce(None, "owner", "s1", "r1", "started")
    bus = RunBus()
    q = bus.subscribe("owner")
    announce(bus, None, "s1", "r1", "started")
    assert q.empty(), "a turn with no principal announces nothing"

    class Broken(RunBus):
        def publish(self, event: RunEvent) -> None:
            raise RuntimeError("boom")

    announce(Broken(), "owner", "s1", "r1", "ended")
    announce(bus, "owner", "s1", "r1", "ended")
    assert q.get_nowait().phase == "ended"


def test_the_event_stream_is_sse_with_keepalives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_api, "RUN_EVENTS_KEEPALIVE_S", 0.01)

    async def go() -> list[bytes]:
        bus = RunBus()
        gen = agent_api._run_events(bus, "owner")
        out = [await gen.__anext__()]
        assert bus.subscriber_count == 1
        out.append(await gen.__anext__())
        bus.publish(_event("debug-token", "theirs"))
        bus.publish(_event("owner", "mine"))
        out.append(await gen.__anext__())
        await gen.aclose()
        assert bus.subscriber_count == 0, "a closed stream lets go of its queue"
        return out

    opened, alive, data = asyncio.run(go())
    assert opened == b": open\n\n" and alive == b": keepalive\n\n"
    assert data.startswith(b"data: ") and data.endswith(b"\n\n")
    assert json.loads(data[6:]) == {"type": "run_started", "session_id": "s1", "run_id": "mine"}
