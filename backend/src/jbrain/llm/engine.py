"""Which on-box LLM engine serves local calls: the standard gateway or Flash-Next.

Two engines, never both up (docs/plans/FLASH_NEXT_ENGINE_PLAN.md §4d): each runs in its own
compose service behind its own llama-swap config, and on a 128 GB box their footprints added
together are a freeze. Everything that starts, stops, renders or reads a gateway resolves the
engine through here, so "which container, which config file" has exactly one answer.

Two settings, never an `.env` flag (the owner has no shell): the DESIRED engine
(`settings_store.LLM_LOCAL_ENGINE_KEY`, the owner's choice, read by the update script through
`jbrain.cli local-engine`) and the EFFECTIVE one (`LLM_LOCAL_ENGINE_EFFECTIVE_KEY`), written by
whatever actually starts an engine. They differ when Flash-Next is wanted but could not start
and the standard gateway serves instead; everything that loads, lists or re-stamps follows the
EFFECTIVE engine, so that fallback never leaves the api refusing the engine that is up.
"""

import contextlib
import os
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Literal

Engine = Literal["standard", "flash-next"]

STANDARD: Engine = "standard"
FLASH_NEXT: Engine = "flash-next"
ENGINES: tuple[Engine, ...] = (STANDARD, FLASH_NEXT)
DEFAULT_ENGINE: Engine = STANDARD

# Compose service per engine. Both carry the `local-llm` network alias so a client never
# needs to know which is up; the service NAME is what start/stop/logs address.
SERVICE: Mapping[Engine, str] = {STANDARD: "local-llm", FLASH_NEXT: "flash-next"}

# llama-swap config per engine, under the shared models root. Separate files so the standard
# gateway never renders (and tries to load) a model its build cannot serve, and vice versa.
CONFIG_FILE: Mapping[Engine, str] = {
    STANDARD: "llama-swap.yaml",
    FLASH_NEXT: "llama-swap.flash-next.yaml",
}


# Docker states in which an engine container holds (or is about to re-take) its memory. The
# one predicate shared — by value — with the supervisor (`gateway.ENGINE_UP_STATES`),
# deploy/local-engine.sh `_le_running` and the perplexity job. `restarting` counts: a
# crash-looping engine re-allocates on every loop, so it is stopped before the other starts.
UP_STATES = frozenset({"running", "paused", "restarting", "removing"})


def holds_memory(state: str) -> bool:
    return state in UP_STATES


def parse(value: object) -> Engine:
    """A stored or supplied engine name, falling back to the default for anything unknown —
    a malformed setting must never leave the box with no engine to start."""
    for engine in ENGINES:
        if value == engine:
            return engine
    return DEFAULT_ENGINE


def config_path(root: str, engine: Engine) -> str:
    return os.path.join(root, CONFIG_FILE[engine])


def models_for(
    engine: Engine, models: Iterable[Mapping[str, object]]
) -> list[Mapping[str, object]]:
    """The manifest dicts (catalog entries) the given engine serves. An entry without an
    `engine` key predates the field and belongs to the standard gateway."""
    return [m for m in models if parse(m.get("engine", DEFAULT_ENGINE)) == engine]


# How long a read of the engine setting is trusted. Short, because the F2 debug route and the
# F3 switch flip engines on a live api with no restart, and the worker process sees a flip only
# through expiry; long enough that a restore loop or a burst of loads costs one settings read.
ACTIVE_ENGINE_TTL_S = 5.0


class ActiveEngine:
    """The EFFECTIVE engine (the one actually up), read from the settings store and cached for
    `ttl_s` seconds.

    One per process, shared by everything that must agree on which gateway is running —
    residency's admission gate, the kv-prefix store's launch-line resolution and the jcode
    proxy's model list. A failed read keeps the last known value (the default before any
    read): a settings hiccup must not flip which engine the box believes it is running."""

    def __init__(
        self,
        load: Callable[[], Awaitable[object]],
        *,
        ttl_s: float = ACTIVE_ENGINE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load = load
        self._ttl_s = ttl_s
        self._clock = clock
        self._value: Engine = DEFAULT_ENGINE
        self._read_at: float | None = None
        _LIVE.add(self)

    async def get(self) -> Engine:
        now = self._clock()
        if self._read_at is None or now - self._read_at >= self._ttl_s:
            with contextlib.suppress(Exception):
                self._value = parse(await self._load())
            self._read_at = now
        return self._value

    def last_known(self) -> Engine:
        """The most recent value without a read — for sync code that runs right after an async
        caller refreshed it."""
        return self._value

    def invalidate(self) -> None:
        self._read_at = None


_LIVE: "weakref.WeakSet[ActiveEngine]" = weakref.WeakSet()


def invalidate_cached() -> None:
    """Expire every in-process cache, so a switch made in this process is seen on the next read
    rather than up to `ACTIVE_ENGINE_TTL_S` later. Other processes catch up by expiry."""
    for cache in list(_LIVE):
        cache.invalidate()
