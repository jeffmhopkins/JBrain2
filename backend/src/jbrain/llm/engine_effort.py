"""The reasoning level a task runs at ON a given engine, separate from its Standard pick.

While Flash-Next serves, every local call is remapped onto it and, by default, carries the
effort its Standard pick would have (FLASH_NEXT_ENGINE_PLAN §4c) — a level chosen for gpt-oss
or Grok. The owner sets Flash-Next's own levels here, per task and per tier (the settings
screen's role groups), in `app.llm_engine_effort` rather than in `llm_task_overrides`, so no
write to one can disturb the other and switching back to Standard restores every pick as it was.

Resolution on an engine: the task's own row, else its tier's row, else None — the caller keeps
today's effort. A per-call `effort_override` (an agent turn's picked level) is applied by the
router AFTER this and still wins.

Pure data and a cache; the SQL lives in `settings_store` (RLS-scoped, like every other read
the router makes) and the tier of a task in `router.task_tier`.
"""

import contextlib
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from jbrain.llm import engine as engines
from jbrain.llm import local_catalog

Scope = Literal["task", "tier"]
SCOPES: tuple[Scope, ...] = ("task", "tier")

# Every level any engine may honor, in the order the screen offers them. The DB CHECK is this
# same set; which subset an engine takes comes from its model (`levels_for`).
ALL_LEVELS: tuple[str, ...] = ("none", "low", "medium", "high")

RowKey = tuple[str, str, str]  # (engine, scope, key)


def levels_for(engine: engines.Engine) -> tuple[str, ...]:
    """The levels `engine`'s sole model honors, or () for an engine with no sole model or no
    thinking channel (Standard serves a roster, so a level cannot mean one thing there).

    Read off the catalog so the choices cannot drift from what the adapter sends: the keys of
    `thinking_effort_map` are the levels a hybrid's template is given, and "none" is real only
    on a hybrid, where it turns thinking off through the toggle."""
    model = local_catalog.sole_model(engine)
    if model is None or not model.supports_reasoning:
        return ()
    honored = set(model.thinking_effort_map)
    if model.hybrid_thinking:
        honored.add("none")
    return tuple(level for level in ALL_LEVELS if level in honored)


@dataclass(frozen=True)
class EngineEfforts:
    """Every stored level, keyed (engine, scope, key)."""

    rows: Mapping[RowKey, str] = field(default_factory=dict)

    def level(self, engine: str, scope: Scope, key: str) -> str | None:
        return self.rows.get((engine, scope, key))

    def resolve(self, engine: str, task: str, tier: str | None) -> tuple[str | None, Scope | None]:
        """(level, the scope it came from) for `task` on `engine`; (None, None) when neither the
        task nor its tier has a row and the caller's own effort stands."""
        own = self.level(engine, "task", task)
        if own is not None:
            return own, "task"
        if tier is not None:
            inherited = self.level(engine, "tier", tier)
            if inherited is not None:
                return inherited, "tier"
        return None, None


EMPTY = EngineEfforts()

# Short for the same reason as the engine read (`engine.ACTIVE_ENGINE_TTL_S`): the worker sees
# an owner's change only through expiry, and a burst of calls should cost one read, not one each.
ENGINE_EFFORT_TTL_S = 5.0

_LIVE: "weakref.WeakSet[EngineEffortCache]" = weakref.WeakSet()


class EngineEffortCache:
    """The stored levels, read through the settings store and trusted for `ttl_s` seconds, so a
    routed call does not hit the database each time. A failed read keeps the last value (EMPTY
    before any read): a settings hiccup must fall back to today's effort, never fail a call."""

    def __init__(
        self,
        load: Callable[[], Awaitable[EngineEfforts]],
        *,
        ttl_s: float = ENGINE_EFFORT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load = load
        self._ttl_s = ttl_s
        self._clock = clock
        self._value = EMPTY
        self._read_at: float | None = None
        _LIVE.add(self)

    async def get(self) -> EngineEfforts:
        now = self._clock()
        if self._read_at is None or now - self._read_at >= self._ttl_s:
            with contextlib.suppress(Exception):
                self._value = await self._load()
            self._read_at = now
        return self._value

    def invalidate(self) -> None:
        self._read_at = None


def invalidate_cached() -> None:
    """Expire every in-process cache, so the owner's write is seen by this process's next call
    rather than up to `ENGINE_EFFORT_TTL_S` later. Other processes catch up by expiry."""
    for cache in list(_LIVE):
        cache.invalidate()
