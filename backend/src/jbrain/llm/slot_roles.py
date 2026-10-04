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

import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypedDict

from jbrain.llm import prefill
from jbrain.llm.errors import LlmContextOverflowError
from jbrain.llm.types import (
    AssistantMessage,
    LlmMessage,
    LlmTool,
    ToolResultMessage,
    UserMessage,
    current_turn_start,
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
    # The pool sizes an operator may pick without a release (`resized`); `n_ctx` is the default.
    # Empty = fixed. Kept to sizes measured or worth measuring on the box, never free-form: a
    # unified pool allocates every cell at load, so a typo here is a load the guard aborts.
    cell_choices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.cell_choices and self.n_ctx not in self.cell_choices:
            raise ValueError("the default pool size must be one of its choices")
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

    def resized(self, cells: int | None) -> "KvPool":
        """This pool at a saved size, or unchanged when `cells` is not one of its choices.

        The saved size rides in the per-model context-window override map (catalog id ->
        tokens), which for a pooled model has no other meaning. Anything else stored there (F2
        saved per-sequence windows before the pool existed) reads as the default rather than
        re-sizing the pool to a number nobody chose for it."""
        if cells is None or cells == self.n_ctx or cells not in self.cell_choices:
            return self
        return dataclasses.replace(self, n_ctx=cells)


# Eight slots, one per frequently-used prefix, so the hot prompts stop evicting each other (owner,
# 2026-10-03). 512k cells by default, not the 1M first shipped: a unified pool allocates all its
# cells at load, and on the box the 1M load drove host free memory to 5.3 GB, under the load
# guard's 6 GB floor, so it was aborted every time. 512k is the 2 x 262k size F2 measured loading
# cleanly (74.2 GiB). 1M stays selectable without a release (FLASH_NEXT_ENGINE_PLAN §3a): that
# abort was page cache from the mmap load, which `--load-mode none` and the range-aware drop
# (local_weights) now take away, so it earns another measured attempt from the debug console.
FLASH_NEXT_POOL_CELLS: Final = (524_288, 1_048_576)
FLASH_NEXT_POOL: Final = KvPool(
    n_ctx=524_288,
    cell_choices=FLASH_NEXT_POOL_CELLS,
    reservations=(
        RoleReservation(SlotRole.INTERACTIVE, 0, 262_144, 7, "jerv (chat, omnibox)"),
        RoleReservation(SlotRole.INGEST, 1, 131_072, 6, "Ingest and analysis"),
        RoleReservation(SlotRole.SCHEDULED, 2, 262_144, 5, "Scheduled tasks"),
        # A research run and a scheduled news run share this slot; spilling to the workshop
        # slot keeps the second from queueing behind the first inside llama-server, where the
        # wait counts against the HTTP timeout.
        RoleReservation(
            SlotRole.RESEARCH,
            3,
            262_144,
            3,
            "Research and sub-agents",
            overflow=SlotRole.WORKSHOP,
        ),
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
# The engine samples video at this rate server-wide (Flash-Next's `--video-fps` is rendered
# from it) and merges each two consecutive frames into one image's tokens. Unknown length is
# charged as the longest clip sent natively, so an unprobed video never under-books its slot.
VIDEO_FPS: Final = 1.0
VIDEO_UNKNOWN_SECONDS: Final = 60.0
# A clamp that would leave less output than this refuses instead — a reply cut to a few hundred
# tokens is a worse failure than a clear "too long for its slot".
MIN_CLAMPED_OUTPUT: Final = 1024


def role_for(task: str, override: SlotRole | None = None) -> SlotRole:
    if override is not None:
        return override
    return TASK_ROLES.get(task, UNKNOWN_TASK_ROLE)


class SlotPin(TypedDict, total=False):
    slot_role: SlotRole


def slot_pin(role: SlotRole | None) -> SlotPin:
    """The `slot_role` keyword for a router call, or none when no role is named — so a caller
    that names none (and a test fake without the keyword) sees the call exactly as before."""
    return {"slot_role": role} if role is not None else {}


def layout_matches(pool: KvPool, slots: Sequence[object]) -> bool:
    """Whether a live `/slots` read is this pool's layout. A server still on a pre-pool config
    (fewer slots, until the next re-stamp) WRAPS an `id_slot` past its count onto some other
    slot with no error, so nothing is pinned unless this holds."""
    return len(slots) == pool.n_slots


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
) -> SlotAdmission:
    """Fit a call into its role's cap: unchanged when it fits, output clamped when only the
    output overruns and enough of it survives, else `SlotCapError`."""
    r = pool.reservation(role)
    room = r.cap_tokens - prompt_tokens
    if max_tokens <= room:
        return SlotAdmission(r.slot, role, r.cap_tokens, max_tokens, clamped=False)
    if room >= max(MIN_CLAMPED_OUTPUT, max_tokens // 4):
        return SlotAdmission(r.slot, role, r.cap_tokens, room, clamped=True)
    raise SlotCapError(role, cap=r.cap_tokens, prompt_tokens=prompt_tokens, max_tokens=max_tokens)


def video_tokens_charge(seconds: float | None) -> int:
    """One clip's charge: each merged frame pair is sized like an image. ffmpeg's fps filter
    can emit one frame past `seconds x fps` (61 for a minute at 1 fps), so one is added."""
    span = VIDEO_UNKNOWN_SECONDS if seconds is None or seconds <= 0 else seconds
    return math.ceil((span * VIDEO_FPS + 1) / 2) * IMAGE_TOKENS_CHARGE


def estimate_prompt_tokens(
    served_model: str, *, chars: int, n_images: int = 0, video_tokens: int = 0
) -> int:
    return (
        int(prefill.estimate_tokens(served_model, chars))
        + n_images * IMAGE_TOKENS_CHARGE
        + video_tokens
    )


def pool_shape(manifest: Mapping[str, object], saved: int | None = None) -> tuple[int, int] | None:
    """(n_ctx, n_slots) of a catalog entry read back from its `asdict` manifest, or None for an
    entry without a pool. `saved` is the operator's stored size, honoured only when it is one of
    the entry's `cell_choices` — the same rule as `KvPool.resized`."""
    pool = manifest.get("kv_pool")
    if not isinstance(pool, Mapping):
        return None
    reservations = pool.get("reservations")
    n_ctx = pool.get("n_ctx")
    if not isinstance(n_ctx, int) or not isinstance(reservations, Sequence):
        return None
    choices = pool.get("cell_choices")
    if saved is not None and isinstance(choices, Sequence) and saved in choices:
        n_ctx = saved
    return n_ctx, len(reservations)


def tool_call_chars(name: str, arguments: object) -> int:
    """A tool call's name and arguments, which OpenAI bodies carry as a JSON string."""
    rendered = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
    return len(name) + len(rendered)


def tool_schema_chars(name: str, description: str, schema: object) -> int:
    return len(name) + len(description) + len(json.dumps(schema, default=str))


def prompt_chars(
    system: str,
    messages: Sequence[LlmMessage],
    tools: Sequence[LlmTool],
    *,
    replay_reasoning: bool = False,
) -> int:
    """Roughly how much text this turn puts in front of the model, in characters.

    The input to `prefill`'s token estimate, so it wants to be proportional to the real
    prompt rather than exactly equal to it — a constant factor washes out in the calibration
    (`prefill.calibrate`), a MISSING TERM does not. Hence tools and tool results are counted:
    on this box the rendered tool schemas are the bulk of a primed prefix (27,787 tokens
    measured), and a turn deep in a tool loop is mostly its own transcript.

    Images are counted as their encoded size deliberately not at all: a vision model prices
    them per tile, not per byte, so their characters would swamp the estimate.

    `replay_reasoning` mirrors the adapter's replay to a preserving model: the turn in
    flight's own thinking goes back into the prompt, and a deep tool loop's traces can
    outweigh its transcript — left out, the pool guard would book a slot short."""
    total = len(system)
    turn_start = current_turn_start(messages) if replay_reasoning else len(messages)
    for index, message in enumerate(messages):
        if isinstance(message, UserMessage):
            total += len(message.text)
        elif isinstance(message, AssistantMessage):
            total += len(message.text) + sum(
                tool_call_chars(call.name, call.arguments) for call in message.tool_calls
            )
            if index >= turn_start:
                total += len(message.reasoning)
        elif isinstance(message, ToolResultMessage):
            total += sum(len(str(result.content)) for result in message.results)
    for tool in tools:
        total += tool_schema_chars(tool.name, tool.description, tool.input_schema)
    return total


def image_count(messages: Sequence[LlmMessage]) -> int:
    return sum(len(m.images) for m in messages if isinstance(m, UserMessage))
