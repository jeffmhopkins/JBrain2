"""Which on-box LLM engine serves local calls: the standard gateway or Flash-Next.

Two engines, never both up (docs/plans/FLASH_NEXT_ENGINE_PLAN.md §4d): each runs in its own
compose service behind its own llama-swap config, and on a 128 GB box their footprints added
together are a freeze. Everything that starts, stops, renders or reads a gateway resolves the
engine through here, so "which container, which config file" has exactly one answer.

The active engine is an owner setting (`settings_store.LLM_LOCAL_ENGINE_KEY`), not an `.env`
flag: the owner has no shell, so the update script reads it through `jbrain.cli local-engine`.
"""

import os
from collections.abc import Iterable, Mapping
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
