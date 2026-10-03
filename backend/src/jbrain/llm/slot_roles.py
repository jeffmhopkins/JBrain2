"""Which llama-server slot a call lands in on a pooled model, and how much of the pool it may use.

Flash-Next serves one shared `--kv-unified` pool to eight slots (FLASH_NEXT_ENGINE_PLAN §4a).
Slots are prefix caches, not concurrency: each keeps the last prompt it ran warm, so the slot a
call is pinned to decides whose prefix it reuses and whose it would evict. Pinning is by ROLE,
and a role comes from the task name — except `agent.turn`, which the interactive chat and every
background agent share, so those callers name their role explicitly (`role_for`'s override).

Each role carries a cap (prompt + output). Caps are per slot, not reservations: they add up to
more than the pool, because most slots hold small prompts most of the time. What keeps the pool
from filling is the router's pool guard, which frees idle slots in `eviction_rank` order before
a call would overrun the pool — never silently in the engine's own order.

Imports nothing from `local_catalog`, which embeds `FLASH_NEXT_POOL`, so there is no cycle.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jbrain.llm import prefill
from jbrain.llm.errors import LlmContextOverflowError
from jbrain.llm.types import (
    AssistantMessage,
    LlmMessage,
    LlmTool,
    ToolResultMessage,
    UserMessage,
)

# No single sequence may exceed what the model was trained on, whatever the pool holds.
FLASH_NEXT_CTX_TRAIN: Final = 262_144


class SlotRole(StrEnum):
    INTERACTIVE = "interactive"  # jerv: the chat and omnibox turns, and the warm prime
    INGEST = "ingest"  # note analysis and extraction: disambiguate, adjudicate, OCR, captions
    SCHEDULED = "scheduled"  # scheduled tasks, plan continuations, jmolt night, daily briefing
    RESEARCH = "research"  # sub-agents and deep research
    JCODE = "jcode"  # the jcode proxy
    WORKSHOP = "workshop"  # wiki, note conversations, guided intake, video summaries
    PET = "pet"  # the jpanel kid pet
    SMALL = "small"  # titles, triage, one-shot vision reads, probes; the pet's overflow


@dataclass(frozen=True)
class RoleReservation:
    role: SlotRole
    slot: int
    cap_tokens: int
    # Lower is freed first when the pool guard needs cells. The interactive prefix is the
    # costliest to lose (a ~29k persona the owner waits on), so it goes last.
    eviction_rank: int
    label: str
    # Where a call goes when this role's slot is busy and the overflow slot is idle.
    overflow: SlotRole | None = None


@dataclass(frozen=True)
class KvPool:
    """One `--kv-unified` pool and the role pinned to each of its slots (index == slot id)."""

    n_ctx: int
    reservations: tuple[RoleReservation, ...]
    ctx_train: int = FLASH_NEXT_CTX_TRAIN

    def __post_init__(self) -> None:
        slots = [r.slot for r in self.reservations]
        if slots != list(range(len(slots))):
            raise ValueError("pool slots must be 0..n-1 in order")
        roles = [r.role for r in self.reservations]
        if len(set(roles)) != len(roles):
            raise ValueError("each role pins exactly one slot")
        ranks = [r.eviction_rank for r in self.reservations]
        if len(set(ranks)) != len(ranks):
            raise ValueError("eviction ranks must be distinct")
        for r in self.reservations:
            if not 0 < r.cap_tokens <= min(self.ctx_train, self.n_ctx):
                raise ValueError(f"{r.role} cap {r.cap_tokens} exceeds the trained context")
            if r.overflow is not None and r.overflow not in roles:
                raise ValueError(f"{r.role} overflows to a role the pool lacks")

    @property
    def n_slots(self) -> int:
        return len(self.reservations)

    def reservation(self, role: SlotRole) -> RoleReservation:
        for r in self.reservations:
            if r.role == role:
                return r
        raise KeyError(role)

    def slot(self, role: SlotRole) -> int:
        return self.reservation(role).slot

    def cap(self, role: SlotRole) -> int:
        return self.reservation(role).cap_tokens

    def by_slot(self, slot: int) -> RoleReservation:
        return self.reservations[slot]

    def eviction_order(self) -> list[int]:
        """Slot ids, the first to free when the pool needs room first."""
        return [r.slot for r in sorted(self.reservations, key=lambda r: r.eviction_rank)]


# Owner decision 2026-10-03: a 1M pool (the same cells as the 4 x 262k layout measured live)
# across eight slots, one per frequently-used prefix, so the hot prompts stop evicting each other.
FLASH_NEXT_POOL: Final = KvPool(
    n_ctx=1_048_576,
    reservations=(
        RoleReservation(SlotRole.INTERACTIVE, 0, 262_144, 7, "jerv (chat, omnibox)"),
        RoleReservation(SlotRole.INGEST, 1, 131_072, 6, "Ingest and analysis"),
        RoleReservation(SlotRole.SCHEDULED, 2, 262_144, 5, "Scheduled tasks"),
        RoleReservation(SlotRole.RESEARCH, 3, 262_144, 3, "Research and sub-agents"),
        RoleReservation(SlotRole.JCODE, 4, 262_144, 4, "jcode"),
        RoleReservation(SlotRole.WORKSHOP, 5, 131_072, 2, "Wiki, notes, intake"),
        RoleReservation(SlotRole.PET, 6, 32_768, 1, "Kid pet", overflow=SlotRole.SMALL),
        RoleReservation(SlotRole.SMALL, 7, 65_536, 0, "Small prompts"),
    ),
)

# The role a task lands in when its caller names none. `agent.turn` defaults to the interactive
# slot: the chat is its one caller that cannot name itself (the AgentLoop default), so every
# background caller must pass its own role, or it evicts jerv's prefix.
TASK_ROLES: Final[Mapping[str, SlotRole]] = {
    "agent.turn": SlotRole.INTERACTIVE,
    "entity.disambiguate": SlotRole.INGEST,
    "fact.adjudicate": SlotRole.INGEST,
    "correction_note.extract": SlotRole.INGEST,
    "vision.ocr": SlotRole.INGEST,
    "vision.caption": SlotRole.INGEST,
    "emr.pathology_diagnosis": SlotRole.INGEST,
    "intake.materialize": SlotRole.WORKSHOP,
    "video.summarize": SlotRole.WORKSHOP,
    "wiki.rewrite": SlotRole.WORKSHOP,
    "wiki.ground": SlotRole.WORKSHOP,
    "wiki.lint.contradiction": SlotRole.WORKSHOP,
    "wiki.lint.stale": SlotRole.WORKSHOP,
    # A one-shot image read with no reusable prefix: in the interactive slot it would overwrite
    # the live conversation between tool rounds.
    "agent.vision": SlotRole.SMALL,
    "research.title": SlotRole.SMALL,
    "triage.classify": SlotRole.SMALL,
    "pet.turn": SlotRole.PET,
    "pet.thought": SlotRole.PET,
    "pet.statue": SlotRole.PET,
}

# Unknown names (debug routes, a task added without a row) get a big cap in a slot whose prefix
# is cheap to lose, rather than a refusal.
UNKNOWN_TASK_ROLE: Final = SlotRole.WORKSHOP
JCODE_ROLE: Final = SlotRole.JCODE
WARM_ROLE: Final = SlotRole.INTERACTIVE
PROBE_ROLE: Final = SlotRole.SMALL

# What one image costs against a cap: the projector's tokens per image at the catalog floor and
# above. Images are not in the character estimate (`prompt_chars`), so they are charged flat.
IMAGE_TOKENS_CHARGE: Final = 4096
# A clamp that would leave less output than this refuses instead — a reply cut to a few hundred
# tokens is a worse failure than a clear "too long for its slot".
MIN_CLAMPED_OUTPUT: Final = 1024


def role_for(task: str, override: SlotRole | None = None) -> SlotRole:
    if override is not None:
        return override
    return TASK_ROLES.get(task, UNKNOWN_TASK_ROLE)


class SlotCapError(LlmContextOverflowError):
    """A call's prompt plus output would exceed its slot's cap. A context overflow to every
    caller (the chat already renders `context_overflow`), raised before anything is sent.
    Carries sizes only, never prompt text."""

    def __init__(self, role: SlotRole, *, cap: int, prompt_tokens: int, max_tokens: int) -> None:
        super().__init__(
            f"{role} slot holds {cap} tokens; this call needs ~{prompt_tokens} prompt + "
            f"{max_tokens} output"
        )
        self.role = role
        self.cap = cap
        self.prompt_tokens = prompt_tokens
        self.max_tokens = max_tokens


@dataclass(frozen=True)
class SlotAdmission:
    slot: int
    role: SlotRole
    cap: int
    max_tokens: int
    clamped: bool


def admit(
    pool: KvPool,
    role: SlotRole,
    *,
    prompt_tokens: int,
    max_tokens: int,
    allow_clamp: bool = True,
) -> SlotAdmission:
    """Fit a call into its role's cap: unchanged when it fits, output clamped when only the
    output overruns and enough of it survives, else `SlotCapError`."""
    r = pool.reservation(role)
    room = r.cap_tokens - prompt_tokens
    if max_tokens <= room:
        return SlotAdmission(r.slot, role, r.cap_tokens, max_tokens, clamped=False)
    if allow_clamp and room >= max(MIN_CLAMPED_OUTPUT, max_tokens // 4):
        return SlotAdmission(r.slot, role, r.cap_tokens, room, clamped=True)
    raise SlotCapError(role, cap=r.cap_tokens, prompt_tokens=prompt_tokens, max_tokens=max_tokens)


def estimate_prompt_tokens(served_model: str, *, chars: int, n_images: int = 0) -> int:
    return int(prefill.estimate_tokens(served_model, chars)) + n_images * IMAGE_TOKENS_CHARGE


def pool_shape(manifest: Mapping[str, object]) -> tuple[int, int] | None:
    """(n_ctx, n_slots) of a catalog entry read back from its `asdict` manifest, or None for an
    entry without a pool."""
    pool = manifest.get("kv_pool")
    if not isinstance(pool, Mapping):
        return None
    reservations = pool.get("reservations")
    n_ctx = pool.get("n_ctx")
    if not isinstance(n_ctx, int) or not isinstance(reservations, Sequence):
        return None
    return n_ctx, len(reservations)


def prompt_chars(system: str, messages: Sequence[LlmMessage], tools: Sequence[LlmTool]) -> int:
    """Roughly how much text this turn puts in front of the model, in characters.

    The input to `prefill`'s token estimate, so it wants to be proportional to the real
    prompt rather than exactly equal to it — a constant factor washes out in the calibration
    (`prefill.calibrate`), a MISSING TERM does not. Hence tools and tool results are counted:
    on this box the rendered tool schemas are the bulk of a primed prefix (27,787 tokens
    measured), and a turn deep in a tool loop is mostly its own transcript.

    Images are counted as their encoded size deliberately not at all: a vision model prices
    them per tile, not per byte, so their characters would swamp the estimate."""
    total = len(system)
    for message in messages:
        if isinstance(message, UserMessage):
            total += len(message.text)
        elif isinstance(message, AssistantMessage):
            total += len(message.text) + sum(
                len(call.name) + len(json.dumps(call.arguments, default=str))
                for call in message.tool_calls
            )
        elif isinstance(message, ToolResultMessage):
            total += sum(len(str(result.content)) for result in message.results)
    for tool in tools:
        total += len(tool.name) + len(tool.description)
        total += len(json.dumps(tool.input_schema, default=str))
    return total


def image_count(messages: Sequence[LlmMessage]) -> int:
    return sum(len(m.images) for m in messages if isinstance(m, UserMessage))
