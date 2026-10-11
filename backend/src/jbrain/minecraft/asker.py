"""Who is asking, during an in-game Dave turn (docs/plans/MINECRAFT_BEDROCK_PLAN.md §P2).

The in-game door (`minecraft/door.py`) runs a Minecraft_Dave turn for the player who
typed `/jb:dave`, and the tools must answer for that player only: their position is the
default for a saved place or a search, their bed is "home" when they never marked one,
and no gamertag the model passes can point a write at someone else.

That is turn state, not chat state: the same session can be reopened in the PWA by the
owner, whose turns are not the player's. So it is registered for exactly the length of a
door turn, keyed by the agent session, and the tools read it from the session id their
`ToolContext` already carries — a process-local map, because the door and the tool
handlers live in the same api process.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

DIMENSIONS = ("overworld", "nether", "the_end")


@dataclass(frozen=True)
class Spot:
    x: int
    y: int | None
    z: int
    dim: str


@dataclass(frozen=True)
class Asker:
    """The player behind an in-game turn, as the server reported them."""

    key: str  # the mc_* tables' player key: an xuid, or `name:<gamertag>`
    gamertag: str
    world: str  # the loaded world's folder, where their places are filed
    at: Spot  # the block they stand in
    # Their bed or respawn anchor, when the event carried one.
    spawn: Spot | None = None
    # The Old Maker items they have unlocked in this world (§M19, `minecraft/guide.py`).
    known: frozenset[str] = frozenset({"tricorder"})


_ASKERS: dict[str, Asker] = {}


@contextmanager
def asking(agent_session_id: str, asker: Asker) -> Iterator[None]:
    _ASKERS[agent_session_id] = asker
    try:
        yield
    finally:
        _ASKERS.pop(agent_session_id, None)


def asker_for(agent_session_id: str | None) -> Asker | None:
    return _ASKERS.get(agent_session_id) if agent_session_id else None
