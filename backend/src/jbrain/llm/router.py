"""Task-profile routing: every LLM call happens under a named task.

OWNER DECISION (recorded verbatim): every LLM call happens under a named task
profile. Tasks include agent.turn, entity.disambiguate, fact.adjudicate,
correction_note.extract, vision.ocr, vision.caption. Each task maps to
"provider:model" and is INDIVIDUALLY configurable; the default for EVERY task
is "xai:grok-4.3". Config via pydantic-settings: a JBRAIN_LLM_TASKS env var
holding a JSON object of overrides ({"agent.turn":
"anthropic:claude-sonnet-4-6"}) merged over the defaults.

The "local" provider must exist now so going all-local is config, not
refactor — docs/reference/ANALYSIS.md "Privacy routing".
"""

import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx
import structlog

from jbrain import box_events
from jbrain.config import Settings
from jbrain.llm import engine as engines
from jbrain.llm import kv_pool_guard as kv_pool_guard_mod
from jbrain.llm import kv_prefix as kv_prefix_mod
from jbrain.llm import local_catalog, model_sampling, prefill, slot_roles
from jbrain.llm.anthropic import AnthropicClient
from jbrain.llm.engine_effort import EngineEfforts
from jbrain.llm.errors import LlmBadResponseError, LlmError, LlmStreamTruncatedError
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.slot_roles import SlotRole
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmClient,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    LlmVideo,
    ReasoningChunk,
    Sampling,
    StreamPart,
    TextChunk,
    ToolResultMessage,
    UsageRecorder,
)

log = structlog.get_logger()

XAI_BASE_URL = "https://api.x.ai/v1"

TASK_DEFAULTS: dict[str, str] = {
    "entity.disambiguate": "xai:grok-4.3",
    "fact.adjudicate": "xai:grok-4.3",
    "correction_note.extract": "xai:grok-4.3",
    "vision.ocr": "xai:grok-4.3",
    "vision.caption": "xai:grok-4.3",
    # The tool-using personal agent's turn (docs/reference/ASSISTANT.md). Strong tier by
    # default — agent reasoning over tools is the high-stakes path.
    "agent.turn": "xai:grok-4.3",
    # jerv's `analyze_image` tool: a vision read the turn delegates so a text-only
    # agent model (e.g. local gpt-oss) can still "see" an attached/generated image.
    # Defaults to the multimodal cloud model; an on-box operator overrides it to
    # the local vision model (local:qwen3-vl-30b-a3b) so the image never leaves the box.
    "agent.vision": "xai:grok-4.3",
    # Guided-intake materialization: read a captured submission's UNTRUSTED transcript
    # and propose per-claim leaves for the owner to approve (docs/archive/GUIDED_INTAKE_PLAN.md).
    # Strong tier — it reasons over adversarial input behind a strict data/instruction
    # boundary, so the attribution it can influence is the leaf TEXT only.
    "intake.materialize": "xai:grok-4.3",
    # analyze_video's reduce step: fold a clip's frame-caption + transcript timeline
    # into one summary (docs/archive/VIDEO_ANALYSIS_PLAN.md). Text-only — the per-frame
    # captioning is the separate `agent.vision` route. Individually routable so the
    # summary can run on a cheaper/local model than the vision pass.
    "video.summarize": "xai:grok-4.3",
    # Titling a deep-research report (external.report_titler): distill a run's raw question
    # into a short Research Library heading. It FOLLOWS agent.turn (`_FOLLOW_PRIMARY_MODEL`),
    # so this default is just the fresh-box fallback — a re-routed agent.turn (e.g. to a local
    # model) carries the title with it, closing the "off-box title on a local box" gap.
    #
    # (There is no `session.title` twin any more. Naming a CHAT was a separate completion that
    # followed agent.turn onto the interactive model, where its ~200-token prompt evicted the
    # ~32k primed prefix and made the real turn behind it pay a ~100 s cold prefill. jerv now
    # names its own chat mid-turn through the `name_session` tool, so there is no second call
    # to route at all. A report has no turn to name itself from, so this one stays.)
    "research.title": "xai:grok-4.3",
    # The Phase-6 wiki builder (docs/plans/PHASE6_WIKI_PLAN.md): `wiki.rewrite` drafts a
    # type-guided article from an entity's cited facts; `wiki.ground` is the strict
    # grounding verifier (the entity graph wins on conflict). Without these the
    # builder's router.complete() raises `unknown LLM task` and every build aborts.
    # Individually routable so an on-box operator can point them at a local model.
    "wiki.rewrite": "xai:grok-4.3",
    "wiki.ground": "xai:grok-4.3",
    # The Phase-6 wiki HEALTH sweep (docs/archive/WIKI_LINT_PLAN.md, Wave B):
    # `wiki.lint.contradiction` adjudicates whether two firewall-compatible subjects' facts
    # contradict; `wiki.lint.stale` judges whether an article frames a superseded fact as current.
    # Metered against the SEPARATE wiki-lint budget; individually routable to a local model.
    "wiki.lint.contradiction": "xai:grok-4.3",
    "wiki.lint.stale": "xai:grok-4.3",
    # The archivist's `triage_inbox` sweep (docs/archive/EMAIL_ARCHIVIST_PLAN.md): classify a
    # batch of inbox emails into priority buckets from sender/subject/snippet alone.
    # The prompt declares the `low` tier (a cheap one-shot judgment over many emails);
    # individually routable so an on-box operator can point it at a local model.
    "triage.classify": "xai:grok-4.3",
    # JPet — the family wall pet (docs/archive/JPET_PLAN.md). `pet.turn` answers a child
    # in character; `pet.thought` is the idle daydream. Cheap and snappy; an on-box
    # operator points these at the local model via the JPet settings card so the pet
    # never spends API budget and always takes second seat.
    "pet.turn": "xai:grok-4.3",
    "pet.thought": "xai:grok-4.3",
    # `pet.statue` sculpts a voxel model of a requested subject for the wall's field
    # "build a statue of X". Reasoning-bound (it plans a recognisable 3D shape voxel by
    # voxel), so it defaults to the strong model + high effort; individually routable so an
    # on-box operator can point it at a local reasoning model like the other pet tasks.
    "pet.statue": "xai:grok-4.3",
}

# Each task's DEFAULT reasoning effort — the Settings bucket it sits in, so a fresh
# box is "right by default" and a stored per-task effort is a deliberate override.
# One source of truth for both the router (what it sends) and the settings screen
# (what it shows). Buckets: high = async, reasoning-bound, correctness-critical work
# (the knowledge-graph arbiters); low = deterministic one-shots; medium = everything
# else that thinks. Vision tasks carry no effort (their model has no thinking channel).
TASK_REASONING_BUCKET: dict[str, str] = {
    # High reasoning
    "fact.adjudicate": "high",
    "wiki.ground": "high",
    "wiki.lint.contradiction": "high",
    "wiki.lint.stale": "high",
    "pet.statue": "high",
    # Medium reasoning
    "agent.turn": "medium",
    "correction_note.extract": "medium",
    "video.summarize": "medium",
    "wiki.rewrite": "medium",
    "intake.materialize": "medium",
    # Low reasoning
    "entity.disambiguate": "low",
    "research.title": "low",
    "triage.classify": "low",
    "pet.turn": "low",
    "pet.thought": "low",
}

# The deviations the router must ACTIVELY put on the wire. Medium is omitted on
# purpose: it is the reasoning model's own built-in default, and pinning it would
# override the sub-agent spawner's contract that "no chosen effort → the child
# model's default" (a plain child must reach the client with reasoning_effort=None).
# So a medium-bucket task resolves to None and lets the model use its native medium.
TASK_REASONING_DEFAULTS: dict[str, str] = {
    task: effort for task, effort in TASK_REASONING_BUCKET.items() if effort != "medium"
}

# The settings screen's role groups ("tiers"): the three reasoning buckets plus vision. A
# per-engine reasoning level can be set on a tier and every task in it inherits it unless it
# has its own (jbrain.llm.engine_effort). Not the prompt `strength` tiers below — those pick a
# MODEL; these only group tasks for the effort a picked model runs at.
# "code" is code mode's two roles: not router tasks (the jcode proxy forwards the sandbox's
# own requests, nothing here routes them), but they run on the engine's model all the same, so
# the owner sets their level beside the rest (api.jcode_llm applies it).
CODE_TIER = "code"
EFFORT_TIERS: tuple[str, ...] = ("high", "medium", "low", "vision", CODE_TIER)

# Code mode's roles as engine-effort task keys: the executor is grok's default model, the
# planner its `plan` subagent. The proxy tells them apart by the model a request names.
JCODE_EXECUTOR_TASK = "jcode.executor"
JCODE_PLANNER_TASK = "jcode.planner"
CODE_TASKS: tuple[str, ...] = (JCODE_EXECUTOR_TASK, JCODE_PLANNER_TASK)


def is_vision_task(task: str) -> bool:
    """A task that sends image content, so it needs a vision-capable model."""
    return task.startswith("vision.") or task == "agent.vision"


def task_tier(task: str) -> str | None:
    """The role group `task` sits in on the settings screen, or None for a task in none. The
    hidden title task counts as low: it follows the chat MODEL but keeps its own low effort."""
    if task in CODE_TASKS:
        return CODE_TIER
    if is_vision_task(task):
        return "vision"
    return TASK_REASONING_BUCKET.get(task)


# The one task whose model is "the model the operator is using" — the chat agent's turn.
_PRIMARY_MODEL_TASK = "agent.turn"
# Tasks that FOLLOW the primary chat model instead of carrying their own routing. Both are
# cheap one-shot titles: a fresh box runs them wherever `agent.turn` runs (same TASK_DEFAULTS
# spec, so unchanged out of the box), and the moment the operator re-routes `agent.turn` — e.g.
# points it at a local model — the titles move with it, with no separate override to remember
# (the gap that left `research.title` alone on the off-box default on a local-only box). They
# keep their OWN low/none reasoning effort, and an explicit per-task pin still wins over the
# follow (see `_resolve_live`). A prompt that passes a `strength` tier opts out.
_FOLLOW_PRIMARY_MODEL = frozenset({"research.title"})

# Capability tiers (a prompt's `strength:`) → "provider:model". A prompt names a
# tier, never a model, so swapping the model behind a tier is config, not a
# prompt edit (docs/reference/ANALYSIS.md "Privacy routing"). Today every tier resolves to
# the same default as the tasks; the "embedding" tier is served by the embed
# container, not this completion router, so it is not listed here.
TIER_DEFAULTS: dict[str, str] = {
    "high": "xai:grok-4.3",
    "low": "xai:grok-4.3",
    "vision": "xai:grok-4.3",
}

PROVIDERS = ("anthropic", "xai", "local")

# Context-window sizes (tokens) for the non-local models, keyed by served model
# name — the denominator the PWA's context-usage meter divides by. Local windows
# come from the catalog (the gateway's `-c`); these cover the cloud providers. A
# model not listed falls back to DEFAULT_CONTEXT_WINDOW, an honest conservative
# estimate rather than a wrong-but-precise one.
DEFAULT_CONTEXT_WINDOW = 128_000
CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic Claude 4.x family.
    "claude-opus-4-8": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    # xAI Grok.
    "grok-4.3": 256_000,
}

# Below this a prompt is mostly chat-template overhead, whose tokens have no characters behind
# them in the estimate; calibrating on it would skew the ratio for the long prompts caps guard.
_MIN_CALIBRATION_TOKENS = 2048

JSON_NUDGE = (
    "\n\nYour previous reply was not valid JSON."
    " Return only valid JSON matching the requested schema — no prose, no code fences."
)


def spec_on_engine(spec: str, engine: engines.Engine, fallback_spec: str) -> str:
    """The spec a stored/default `spec` actually runs as on `engine` — the module-level twin
    of `LlmRouter._on_engine` for callers that resolve a spec without a router (the chat
    capabilities read). A local spec of the other engine becomes the active engine's sole
    model, or `fallback_spec` (the task default) when it has none."""
    provider, _, model = spec.partition(":")
    if provider != local_catalog.LOCAL_PROVIDER:
        return spec
    mapped = local_catalog.remap_for_engine(model, engine)
    if mapped is not None:
        return f"{provider}:{mapped}"
    return fallback_spec


def context_window_for_spec(spec: str, engine: engines.Engine) -> int:
    """The total context window for a raw "provider:model" spec, WITHOUT resolving
    live overrides — the spec-based twin of LlmRouter.context_window. The capabilities
    endpoint uses it to seed the composer's context meter before the first turn, so the
    window reads consistently with the vision flag (both off the same resolved spec).
    A local window comes from the catalog default; a live per-model `-c` override only
    takes effect once a turn actually streams (the meter corrects itself then). Cloud
    windows come from CONTEXT_WINDOWS, with the conservative default for an unlisted
    model so the meter degrades gracefully rather than misreports.

    `engine` is the active engine, explicit because this is module-level: a local spec of
    the other engine reads as the model it is remapped onto (plan §4c)."""
    provider, _, model = spec.partition(":")
    if provider == "local":
        served = local_catalog.remap_for_engine(model, engine) or model
        pool = local_catalog.pool_of(served)
        if pool is not None:
            # The spec seeds the chat's meter, and the chat runs in the interactive slot.
            return pool.cap(slot_roles.SlotRole.INTERACTIVE)
        return local_catalog.context_window(served)
    return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def _split_spec(label: str, spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    if not sep or not provider or not model:
        raise LlmError(f"malformed LLM spec for {label!r}: {spec!r}")
    if provider not in PROVIDERS:
        raise LlmError(f"unknown LLM provider for {label!r}: {provider!r}")
    return provider, model


def warm_reasoning_effort(
    task: str, served_model: str, stored: str | None, engine_level: str | None = None
) -> str | None:
    """The reasoning effort a `task` turn would carry on a LOCAL `served_model` — for the
    gateway's load-time warm-up, which must render the exact prompt a routed turn will
    send (the effort lands in the prompt's leading tokens; see
    `openai_compat.apply_local_reasoning`). Mirrors `_resolve_live`'s stored-override-else-
    default fold, then its per-engine level (`engine_level`, the caller's resolution for the
    engine `served_model` runs on), then gates on the model itself: the model being LOADED is
    not always the model the task routes to, and a non-reasoning model must not carry the
    field."""
    effort = TASK_REASONING_DEFAULTS.get(task)
    if stored:
        effort = stored
    if engine_level is not None:
        effort = engine_level
    if not _reasoning_capable(local_catalog.LOCAL_PROVIDER, served_model):
        return None
    return effort


def _reasoning_capable(provider: str, model: str) -> bool:
    """Whether (provider, model) honors `reasoning_effort` / emits a thinking trace:
    xAI Grok, or a local reasoning model (gpt-oss/GLM). A stored effort is dropped
    for anything else so a non-reasoning model never receives the param."""
    return provider == "xai" or (
        provider == "local" and model in local_catalog.REASONING_SERVED_MODELS
    )


# Task names this repo once routed and has since deleted. A stale pin naming one is
# DROPPED, never fatal — the only override map that is not editable from the PWA is the
# `JBRAIN_LLM_TASKS` env var in `/opt/jbrain2/.env`, on a box whose owner has no terminal
# (CLAUDE.md #10). `build_router` runs inside both the API lifespan and the worker, so a
# raise there is a box that comes back from Ops -> Update dead, unrecoverably. The
# DB-stored overrides need no such set: `_resolve_live` reads them per task
# (`overrides.get(task)`), so a stale stored key is already inert.
#
# No TIER has ever been retired, which is why `resolve_tiers` carries no twin; retiring
# one needs the same set, for the same reason.
RETIRED_TASKS: frozenset[str] = frozenset({"note.extract", "integrate.note"})


def resolve_tasks(overrides: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Merge overrides over TASK_DEFAULTS and split each "provider:model".

    Strict on unknown tasks, unknown providers, and malformed specs — a typo
    in routing config should fail at startup, not silently fall back. A name in
    RETIRED_TASKS is the one exception, and the asymmetry is deliberate: a typo is a
    config bug with no deployed history, while a retired name is config that WAS valid
    and would otherwise turn an update into an unbootable box.
    """
    merged = dict(TASK_DEFAULTS)
    for task, spec in overrides.items():
        if task in RETIRED_TASKS:
            log.warning("llm.task_override_retired", task=task, spec=spec)
            continue
        if task not in TASK_DEFAULTS:
            raise LlmError(f"unknown LLM task in overrides: {task!r}")
        merged[task] = spec
    return {task: _split_spec(task, spec) for task, spec in merged.items()}


def resolve_tiers(overrides: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Merge overrides over TIER_DEFAULTS and split each "provider:model".
    Strict on unknown tiers, same as task resolution."""
    merged = dict(TIER_DEFAULTS)
    for tier, spec in overrides.items():
        if tier not in TIER_DEFAULTS:
            raise LlmError(f"unknown LLM tier in overrides: {tier!r}")
        merged[tier] = spec
    return {tier: _split_spec(tier, spec) for tier, spec in merged.items()}


class LocalAdmitter(Protocol):
    """The one thing the router needs from residency: make room for a served model
    before it loads. `jbrain.llm.residency.ResidencyCoordinator` satisfies it; the
    router depends on this narrow shape (not the concrete class) so there's no import
    cycle and test fakes stay trivial. The router holds one unconditionally — an inert
    coordinator on a cloud-only box — so a local load can never bypass admission and
    co-load past the unified-memory budget."""

    async def ensure_room(self, served_model: str) -> str | None: ...


def _reading(messages: Sequence[LlmMessage]) -> str:
    """What the model is eating while a turn sits in prefill, in the words the status line
    and the vitals row will show.

    "Your prompt" is only true on the first round. Every later round of a tool loop resends
    the same conversation, so the KV cache carries all of it and the ONLY thing the box has
    to read is the result that just came back — a fetched page is nine thousand tokens of it.
    The owner watched "Reading your prompt" through a 33 s wait that was the box reading a
    Wikipedia article it had fetched a second earlier, and said so: naming the wrong thing
    makes the wait look unexplained even when the box is telling the truth about there being
    one.

    The tool is named where one name covers the round, because "what web_fetch returned" is
    the sentence that answers the question. The results carry only their call ids, so the
    names come off the assistant turn that requested them."""
    last = messages[-1] if messages else None
    if not isinstance(last, ToolResultMessage) or not last.results:
        return "your prompt"
    prior = messages[-2] if len(messages) > 1 else None
    names = (
        {call.id: call.name for call in prior.tool_calls}
        if isinstance(prior, AssistantMessage)
        else {}
    )
    called = {name for result in last.results if (name := names.get(result.tool_call_id))}
    if len(called) == 1:
        return f"what {next(iter(called))} returned"
    return "what the tool returned" if len(last.results) == 1 else "what the tools returned"


class LlmRouter:
    """The single entry point for application LLM calls.

    Resolves task → (provider, model), delegates to the provider client, and
    owns the one JSON re-ask. Logs task/provider/model/usage per call — never
    prompt contents (notes are private data).
    """

    def __init__(
        self,
        clients: Mapping[str, LlmClient],
        tasks: Mapping[str, tuple[str, str]],
        recorder: UsageRecorder | None = None,
        tiers: Mapping[str, tuple[str, str]] | None = None,
        pinned: frozenset[str] = frozenset(),
        overrides_loader: Callable[[], Awaitable[Mapping[str, Mapping[str, str]]]] | None = None,
        local_windows_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
        residency: LocalAdmitter | None = None,
        local_enabled: bool = True,
        slots_probe: prefill.SlotsReader | None = None,
        kv_prefix: "kv_prefix_mod.KvPrefixStore | None" = None,
        engine_loader: Callable[[], Awaitable[engines.Engine]] | None = None,
        admission_gate: Callable[[], Awaitable[bool]] | None = None,
        pool_guard: kv_pool_guard_mod.KvPoolGuard | None = None,
        engine_efforts_loader: Callable[[], Awaitable[EngineEfforts]] | None = None,
    ):
        self._clients = clients
        # The owner's reasoning levels for a non-Standard engine (jbrain.llm.engine_effort),
        # read through a short TTL cache. None -> every call keeps its Standard effort.
        self._engine_efforts_loader = engine_efforts_loader
        # Keeps a shared KV pool (Flash-Next) from overrunning under a pinned call, and
        # checks the live slot layout before an `id_slot` is trusted (jbrain.llm.kv_pool_guard).
        # None on a bare test router: pool calls are then pinned and capped off the catalog
        # alone.
        self._pool_guard = pool_guard
        # The EFFECTIVE on-box engine, read per resolution (TTL-cached by the caller's
        # ActiveEngine). While Flash-Next serves, every `local:*` route is remapped onto it;
        # while Standard serves, a Flash-Next pick falls back to the task's own route
        # (FLASH_NEXT_ENGINE_PLAN §4c). None -> no remap (tests, cloud-only).
        self._engine_loader = engine_loader
        # The cross-process local-admission gate (jbrain.llm.drain): a local call waits while
        # an engine switch drains, then RE-RESOLVES, because the engine may have changed under
        # it. None -> always open.
        self._admission_gate = admission_gate
        self._tasks = tasks
        # The disk layer for the agent-turn prefix (jbrain.llm.kv_prefix). The router is
        # where a turn is late enough to know its model and early enough to fix the cache:
        # between admission and dispatch, a lost prefix is restored in ~2 s instead of the
        # turn paying a ~60 s prefill. Optional; None keeps the prior behaviour.
        self._kv_prefix = kv_prefix
        self._recorder = recorder
        # When local hosting is off, a stale stored `local:` override (saved while
        # it was on, then disabled) is ignored rather than routed at a dead
        # gateway — defense-in-depth behind the API's PUT guard. Defaults True so
        # test fakes behave as before.
        self._local_enabled = local_enabled
        # Capability-tier → (provider, model), and the set of tasks a human
        # explicitly pinned in config (an explicit pin outranks a prompt's tier).
        # Default to TIER_DEFAULTS so any router (including test fakes that pass
        # only tasks) can resolve a prompt's declared strength.
        self._tiers = dict(tiers) if tiers is not None else resolve_tiers({})
        self._pinned = pinned
        # Loads the live DB-backed per-task overrides (spec + reasoning_effort).
        # None in tests/fakes → behaves exactly as the static config did.
        self._overrides_loader = overrides_loader
        # Loads the live per-model context-window overrides (catalog id → tokens)
        # so the meter reports the operator's chosen `-c`, not just the catalog
        # default. None → fall back to the catalog window.
        self._local_windows_loader = local_windows_loader
        # Residency admission: before a LOCAL completion the router calls ensure_room so
        # the memory budget evicts to hold the free-RAM floor. The gateway is configured
        # never to self-evict (llama_swap_config `swap: false`), so this is the ONLY thing
        # keeping a local load from co-loading past the unified-memory budget and
        # hard-locking the box — build_router REQUIRES one from its caller (it used to fall
        # back to a default that was quietly the weaker gate; c76288f deleted it), so there is
        # no unmanaged local path. None only on a bare test router with fake providers, which
        # never routes to `local`. ensure_room swallows its own
        # housekeeping hiccups, but the deliberate over-box refusal (ResidencyError, when a
        # model can't fit the box even after evicting everything) propagates and fails the
        # turn/job by design — better one failed call than an OOM hard-lock.
        self._residency = residency
        # Reads llama-server's `/slots` for the prefill fraction — how far a turn has got
        # through eating its prompt (jbrain.llm.prefill). Only the streamed path takes it, and
        # only on a local route; None everywhere else, where the watch starts no task.
        self._slots_probe = slots_probe

    async def _ensure_agent_prefix(
        self,
        task: str,
        provider: str,
        model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> None:
        """Between admission and dispatch, put the agent-turn prefix back from disk if no
        slot holds it — ~2 s against the ~60 s prefill the turn would otherwise pay. Only
        the interactive task, only a local model, always best-effort: any failure leaves
        the turn to prefill exactly as it would have without the store."""
        if (
            self._kv_prefix is None
            or task != kv_prefix_mod.AGENT_TURN_TASK
            or provider != local_catalog.LOCAL_PROVIDER
        ):
            return
        try:
            await self._kv_prefix.restore_if_lost(
                model, system, tools, reasoning_effort=reasoning_effort
            )
        except Exception:  # noqa: BLE001 — the disk layer must never fail a turn
            log.warning("llm.kv_restore_failed", model=model, exc_info=True)

    def _note_agent_turn(
        self,
        task: str,
        provider: str,
        model: str,
        input_tokens: int,
        *,
        system: str = "",
        tools: Sequence[LlmTool] = (),
        reasoning_effort: str | None = None,
    ) -> None:
        """Tell the store a real turn's prompt size, so the slot that conversation grew keeps
        reading as 'prefix present' (restoring over it would wipe cached history to re-plant a
        prefix the conversation already extends).

        Named by IDENTITY, not just by task. `agent.turn` is not an interactive lane — the
        daily briefing, deep research and every spawned sub-agent run under it with a
        different system prompt and tool set — so passing the turn's own identity is what
        stops a background completion retiring a restore the owner's turn has not used."""
        if (
            self._kv_prefix is None
            or task != kv_prefix_mod.AGENT_TURN_TASK
            or provider != local_catalog.LOCAL_PROVIDER
        ):
            return
        fingerprint: str | None = None
        with contextlib.suppress(Exception):  # identity is best-effort; never fail a turn
            fingerprint = self._kv_prefix.identity_of(model, system, tools, reasoning_effort)
        self._kv_prefix.note_agent_turn(model, input_tokens, fingerprint=fingerprint)

    def _note_prefix_used(
        self,
        task: str,
        provider: str,
        model: str,
        system: str,
        tools: Sequence[LlmTool],
        reasoning_effort: str | None,
    ) -> None:
        """Retire the store's restored-but-unused memo for a request that reached the model
        without completing — an abandoned stream. Best-effort in every direction."""
        if (
            self._kv_prefix is None
            or task != kv_prefix_mod.AGENT_TURN_TASK
            or provider != local_catalog.LOCAL_PROVIDER
        ):
            return
        with contextlib.suppress(Exception):
            fingerprint = self._kv_prefix.identity_of(model, system, tools, reasoning_effort)
            self._kv_prefix.note_prefix_used(model, fingerprint)

    async def _admit_local(self, provider: str, model: str) -> str:
        """Admit a local model; the served name residency actually admitted (it differs only
        when the engine changed under the call — a remap). None from a fake means "as asked"."""
        if provider == local_catalog.LOCAL_PROVIDER and self._residency is not None:
            return await self._residency.ensure_room(model) or model
        return model

    async def _admitted(
        self,
        task: str,
        strength: str | None,
        spec_override: str | None,
        resolved: tuple[str, str, str | None],
    ) -> tuple[str, str, str | None]:
        """Admit the resolved route and return the one to SEND. When residency admitted a
        different model than the router resolved (the engine switched between the two reads),
        resolve again — the route now sees the same engine — and if the two still disagree, send
        the admitted name with the effort re-gated on it. Never send one name and admit another."""
        provider, model, effort = resolved
        admitted = await self._admit_local(provider, model)
        if admitted == model:
            return resolved
        again = await self._resolve_live(task, strength, spec_override)
        if again[:2] == (provider, admitted):
            return again
        return provider, admitted, effort if _reasoning_capable(provider, admitted) else None

    async def admit_local_load(self, served_model: str) -> str:
        """Make room for a local model a CALLER is about to load itself, through the same
        admission a routed completion gets.

        For the warm keeper, which must bring weights up before it can restore a saved KV slot
        (a cold model has no slots to restore into) and so cannot let the priming completion be
        what loads it. Without this it would either skip admission — co-loading past the
        unified-memory budget, on a box that hard-locks when that happens — or reach into
        `_admit_local`, which is the same bypass with extra steps. Returns the served name
        admitted, which is the one to load."""
        return await self._admit_local(local_catalog.LOCAL_PROVIDER, served_model)

    def _resolve(self, task: str, strength: str | None) -> tuple[str, str]:
        """Precedence: an explicit per-task pin (JBRAIN_LLM_TASKS) wins; else the
        prompt's capability tier (`strength`); else the task default. So a prompt
        selects model strength by declaring a tier, while an operator can still
        override a single task to a specific model."""
        if task in self._pinned:
            return self._tasks[task]
        if strength is not None:
            try:
                return self._tiers[strength]
            except KeyError:
                raise LlmError(f"unknown LLM strength tier: {strength!r}") from None
        try:
            return self._tasks[task]
        except KeyError:
            raise LlmError(f"unknown LLM task: {task!r}") from None

    async def primary_local_served_model(self) -> str | None:
        """The served-model name `agent.turn` resolves to WHEN it routes local — folding in
        the live DB override, the env pin/default, and the local-hosting gate — else None
        (a cloud route or local hosting off). The WarmKeeper reads this to decide which model
        to keep resident+primed, so it tracks a re-route the same way a real turn does. Never
        raises: a bad override degrades to the static route, exactly as a real call would."""
        overrides: Mapping[str, Mapping[str, str]] = {}
        if self._overrides_loader is not None:
            try:
                overrides = await self._overrides_loader()
            except Exception:  # noqa: BLE001 — a settings read hiccup must not wedge the keeper
                overrides = {}
        provider, model = self._followed_primary_model(overrides)
        provider, model = await self._on_engine(_PRIMARY_MODEL_TASK, None, provider, model)
        return model if provider == local_catalog.LOCAL_PROVIDER else None

    async def _active_engine(self) -> engines.Engine | None:
        if self._engine_loader is None:
            return None
        try:
            return await self._engine_loader()
        except Exception:  # noqa: BLE001 — a settings hiccup reads as the engine every box runs
            return engines.DEFAULT_ENGINE

    async def _on_engine(
        self, task: str, strength: str | None, provider: str, model: str
    ) -> tuple[str, str]:
        """The (provider, model) a resolved route actually runs on with the active engine.

        A local route of the other engine is remapped onto the active engine's sole model
        (Flash-Next on: everything local goes to it). When the active engine has no sole model
        (Standard on, Flash-Next picked) the task falls back to its STATIC route — env pin,
        tier or default — never refused. Stored picks are untouched; this is per call."""
        if provider != local_catalog.LOCAL_PROVIDER:
            return provider, model
        active = await self._active_engine()
        if active is None:
            return provider, model
        mapped = local_catalog.remap_for_engine(model, active)
        if mapped is not None:
            return provider, mapped
        fallback_provider, fallback_model = self._resolve(task, strength)
        if fallback_provider != local_catalog.LOCAL_PROVIDER:
            return fallback_provider, fallback_model
        fallback_mapped = local_catalog.remap_for_engine(fallback_model, active)
        # Even the static route is the other engine's: leave it, and residency refuses it with
        # the "switch engines" sentence rather than this guessing a model.
        return provider, fallback_mapped or model

    def _followed_primary_model(
        self, overrides: Mapping[str, Mapping[str, str]]
    ) -> tuple[str, str]:
        """The (provider, model) `agent.turn` resolves to from PERSISTENT config — its env
        pin/default plus a stored DB override, but NOT a per-call `spec_override` (a title is a
        background job with no per-conversation model pick). This is the route the title tasks
        follow. A malformed or can't-serve-local `agent.turn` override degrades to its static
        route, exactly as a direct call to `agent.turn` would, so a title never breaks."""
        provider, model = self._resolve(_PRIMARY_MODEL_TASK, None)
        spec = (overrides.get(_PRIMARY_MODEL_TASK) or {}).get("spec")
        if spec is not None:
            try:
                sp, sm = _split_spec(_PRIMARY_MODEL_TASK, spec)
            except LlmError:
                log.warning("llm.override_bad_spec", task=_PRIMARY_MODEL_TASK, spec=spec)
            else:
                if sp == "local" and not self._local_enabled:
                    log.warning("llm.local_override_ignored", task=_PRIMARY_MODEL_TASK, spec=spec)
                else:
                    provider, model = sp, sm
        return provider, model

    async def _resolve_live(
        self, task: str, strength: str | None, spec_override: str | None = None
    ) -> tuple[str, str, str | None]:
        """Resolve (provider, model, reasoning_effort) folding in the live DB
        overrides. A stored `spec` is the HIGHEST-precedence PERSISTENT selector —
        above an env pin, the strength tier, and the task default — because the
        settings screen is the operator's live control surface and must win over any
        deploy-time config. A stored `reasoning_effort` applies only when the
        resolved provider+model is reasoning-capable (xai Grok, or a local reasoning
        model like gpt-oss/GLM); for anything else it is dropped. Malformed stored
        entries are ignored: a bad saved setting must never break a call.

        `spec_override` is a per-CALL selector (the omnibox's per-conversation agent
        model pick) that outranks even the stored spec — it is the caller saying
        "run THIS turn on that model". Same guards as the stored spec: a malformed or
        can't-serve-local override is ignored (the call falls back to the resolved
        route) rather than breaking the turn. When it lands, the reasoning effort is
        re-gated on the overridden model, so picking a non-reasoning local model
        drops the effort param the resolved route would have carried.

        Effort precedence, lowest first: the task's bucket default, a stored Standard
        effort, then — only when the route lands on a non-Standard engine's model after the
        remap — the owner's per-engine level for the task, else its tier
        (`_engine_effort`). A per-call `effort_override` is applied by the callers on top of
        all of these (`converse`, `converse_stream`, `effective_reasoning_effort`)."""
        provider, model = self._resolve(task, strength)
        # The task's bucket default (high/low deviations only) unless a stored
        # override replaces it below — so a fresh box runs at the right effort.
        reasoning_effort: str | None = TASK_REASONING_DEFAULTS.get(task)
        if self._overrides_loader is not None:
            overrides = await self._overrides_loader()
            # A title follows the primary chat model (agent.turn) rather than its own default, so
            # re-routing agent.turn moves the titles with it and no title needs separate config. A
            # prompt tier (strength) or an env pin on the title opts out; an explicit per-task pin
            # below still wins over the follow. Skipped when agent.turn isn't a configured task
            # (a minimal/test router), so the title just keeps its own route.
            followed = (
                task in _FOLLOW_PRIMARY_MODEL
                and strength is None
                and task not in self._pinned
                and _PRIMARY_MODEL_TASK in self._tasks
            )
            if followed:
                provider, model = self._followed_primary_model(overrides)
            entry = overrides.get(task) or {}
            spec = entry.get("spec")
            # A follow task (a title) ALWAYS tracks agent.turn — a stored own-task spec (a stale
            # pin from before the title tasks left the picker, or one set via a direct PUT) must
            # NOT redirect it to a separate model, which would reintroduce the very "title swaps
            # in a different model" problem this follow exists to prevent. So the own-task spec
            # (and stored effort) are ignored while following; the per-call spec_override (the
            # chat's own model) still wins below.
            if spec is not None and not followed:
                try:
                    sp, sm = _split_spec(task, spec)
                except LlmError:
                    log.warning("llm.override_bad_spec", task=task, spec=spec)
                else:
                    # Ignore a local override the operator can no longer serve.
                    if sp == "local" and not self._local_enabled:
                        log.warning("llm.local_override_ignored", task=task, spec=spec)
                    else:
                        provider, model = sp, sm
            stored_effort = entry.get("reasoning_effort")
            if stored_effort and not followed:
                reasoning_effort = stored_effort
        if spec_override is not None:
            try:
                sp, sm = _split_spec(task, spec_override)
            except LlmError:
                log.warning("llm.call_override_bad_spec", task=task, spec=spec_override)
            else:
                if sp == "local" and not self._local_enabled:
                    log.warning("llm.local_call_override_ignored", task=task, spec=spec_override)
                else:
                    provider, model = sp, sm
        # After every selector, so a stored pick, an env pin and a per-call override are all
        # remapped alike, and BEFORE the effort gate, so the effort is judged on the model
        # that will actually run (Flash-Next reasons; a remapped vision model did not).
        provider, model = await self._on_engine(task, strength, provider, model)
        if provider == local_catalog.LOCAL_PROVIDER:
            reasoning_effort = await self._engine_effort(task, model, reasoning_effort)
        if not _reasoning_capable(provider, model):
            reasoning_effort = None
        return provider, model, reasoning_effort

    async def _engine_effort(self, task: str, model: str, effort: str | None) -> str | None:
        """`effort`, replaced by the owner's level for `task` on the engine `model` runs on —
        the task's own row, else its tier's (jbrain.llm.engine_effort). Keyed off the model that
        will run, AFTER the remap, so a stored Standard pick, an env pin, a tier and a per-call
        `spec_override` that all land on Flash-Next get Flash-Next's level alike. A Standard
        model never reads the table, so Standard routing is exactly what it was; a failed read
        keeps `effort` rather than failing the call."""
        if self._engine_efforts_loader is None:
            return effort
        engine = local_catalog.engine_of(model)
        if engine == engines.STANDARD:
            return effort
        try:
            efforts = await self._engine_efforts_loader()
        except Exception:  # noqa: BLE001 — EngineEffortCache never raises; a bare loader might
            return effort
        level, _scope = efforts.resolve(engine, task, task_tier(task))
        return level if level is not None else effort

    async def _route(
        self, task: str, strength: str | None, spec_override: str | None
    ) -> tuple[str, str, str | None]:
        """`_resolve_live`, held at the local-admission gate. A local call that had to wait
        for an engine switch resolves again: the engine it resolved against may be gone."""
        resolved = await self._resolve_live(task, strength, spec_override)
        gate = self._admission_gate
        if resolved[0] == local_catalog.LOCAL_PROVIDER and gate is not None and await gate():
            resolved = await self._resolve_live(task, strength, spec_override)
        return resolved

    async def context_window(
        self,
        task: str,
        strength: str | None = None,
        spec_override: str | None = None,
        slot_role: SlotRole | None = None,
    ) -> int:
        """The total context window (tokens) the `task` will actually run against
        after live overrides — the denominator for the PWA's context-usage meter. A
        local model's window comes from the catalog (the gateway's `-c`); a cloud
        model's from CONTEXT_WINDOWS, falling back to a conservative default for an
        unlisted model so the meter degrades gracefully rather than misreports.
        `spec_override` (the per-conversation model pick) makes the window reflect
        the model the turn will actually run on, not the resolved default."""
        provider, model, _ = await self._resolve_live(task, strength, spec_override)
        if provider == "local":
            pool = local_catalog.pool_of(model)
            if pool is not None:
                # A pooled model's window is the call's slot cap, whatever a stored `-c`
                # override says — one saved before the pool existed would overstate it.
                return pool.cap(slot_roles.role_for(task, slot_role))
            if self._local_windows_loader is not None:
                windows = await self._local_windows_loader()
                cat_id = local_catalog.id_for_served(model)
                if cat_id is not None and cat_id in windows:
                    return windows[cat_id]
            return local_catalog.context_window(model)
        return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)

    async def supports_vision(
        self, task: str, strength: str | None = None, spec_override: str | None = None
    ) -> bool:
        """Whether the model `task` actually resolves to (after live overrides) can
        accept image content in a turn. A local model declares it in the catalog —
        a text-only gateway model like gpt-oss has no vision projector; the cloud
        providers we wire (Grok, Claude 4.x) are all multimodal, so any non-local
        route is vision-capable. The agent path consults this to DROP image bytes a
        non-vision model can't read (the model still sees the attachment's id as
        text, so it can edit it or analyze it by reference). `spec_override` (the
        per-conversation pick) makes the check reflect the turn's actual model."""
        provider, model, _ = await self._resolve_live(task, strength, spec_override)
        if provider == "local":
            return local_catalog.supports_vision(model)
        return True

    async def effective_reasoning_effort(
        self,
        task: str,
        strength: str | None = None,
        spec_override: str | None = None,
        effort_override: str | None = None,
    ) -> str | None:
        """The reasoning effort a `task` will actually run with after live overrides —
        None when the resolved model isn't reasoning-capable. Lets a caller (e.g. the
        agent loop) size its budget to how hard the model is set to think.
        `spec_override` (the per-conversation pick) re-gates the effort on the
        overridden model, so a turn steered onto a non-reasoning local model reports
        None rather than the resolved route's effort. `effort_override` (the pick's
        reasoning level) wins over the stored effort and any per-engine level under the
        same capability gate — matching what `converse`/`converse_stream` will actually
        send."""
        provider, model, effort = await self._resolve_live(task, strength, spec_override)
        if effort_override is not None and _reasoning_capable(provider, model):
            return effort_override
        return effort

    def spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        """The (provider, model) a task resolves to from STATIC config alone — env
        pin, prompt tier, or task default. It does NOT see the live DB overrides, so
        it must not stamp provenance for an operator-overridable task; use it only
        where the live route can't matter (e.g. a routability probe). Pass the
        prompt's `strength` so a tier resolves the way `complete` would."""
        return self._resolve(task, strength)

    async def effective_spec(
        self, task: str, strength: str | None = None, spec_override: str | None = None
    ) -> tuple[str, str]:
        """The (provider, model) a task will ACTUALLY run on after folding in the live
        DB overrides — the override-aware sibling of `spec()`. Provenance stamps
        (`extractor`, an extract's `tool`) MUST use this so the recorded model matches
        the one `complete` used; `spec()` would mis-stamp the static default for any
        task the operator re-routed in Settings. `spec_override` (the per-conversation
        model pick) reports the model the turn actually runs on, matching what its
        sibling resolvers here already do — without it, a turn steered onto another
        model would be stamped with the default route's name."""
        return (await self._resolve_live(task, strength, spec_override))[:2]

    @staticmethod
    def _toks_per_s(output_tokens: int, elapsed_s: float) -> float | None:
        """End-to-end output tokens/sec (prefill included) — the throughput a caller
        actually feels, which is why a bandwidth-bound local model (a dense one, or a
        big-active-param MoE) reads low. None for a zero/negative interval. Logged per
        call so 'ask vs response time and t/s' is visible in the api log without
        llama-server's own timings (llama-swap doesn't surface those)."""
        return round(output_tokens / elapsed_s, 1) if elapsed_s > 0 else None

    @staticmethod
    def _resolve_sampling(
        provider: str, model: str, reasoning_effort: str | None, override: Sampling | None
    ) -> Sampling:
        """The sampling a call actually runs with: the resolved model's recommended
        defaults (jbrain.llm.model_sampling) with the caller's per-task `.prompt`
        override merged on top. Applied to EVERY call, so a model runs at its card's
        values even when no override is given — the fix for the whole-catalog gap."""
        return model_sampling.default_sampling(provider, model, reasoning_effort).merge(override)

    async def _record(self, task: str, provider: str, model: str, usage: LlmUsage) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.record(task=task, provider=provider, model=model, usage=usage)
        except Exception as exc:  # noqa: BLE001 - accounting must never fail or slow a call
            log.warning("llm.usage_record_failed", task=task, error=repr(exc))

    @contextlib.asynccontextmanager
    async def _slot_pin(
        self,
        task: str,
        slot_role: SlotRole | None,
        provider: str,
        model: str,
        *,
        chars: int,
        n_images: int,
        max_tokens: int,
        video_tokens: int = 0,
    ) -> AsyncIterator[tuple[int | None, int]]:
        """The slot to pin and the output budget to send, held for the duration of the call.

        Only a pooled local model is pinned. Its call is admitted against its role's cap first
        — `SlotCapError` before anything is sent, or the output clamped — then the pool guard
        makes room for it and checks the live layout (unpinned on a mismatch)."""
        pool = local_catalog.pool_of(model) if provider == local_catalog.LOCAL_PROVIDER else None
        if pool is None:
            yield None, max_tokens
            return
        role = slot_roles.role_for(task, slot_role)
        prompt_tokens = slot_roles.estimate_prompt_tokens(
            model, chars=chars, n_images=n_images, video_tokens=video_tokens
        )
        if self._pool_guard is None:
            admission = slot_roles.admit(
                pool, role, prompt_tokens=prompt_tokens, max_tokens=max_tokens
            )
            yield admission.slot, admission.max_tokens
            return
        # The owner is watching the interactive turn; it gives up on a full pool far sooner
        # than a background job, which the worker defers and retries anyway.
        wait_s = kv_pool_guard_mod.INTERACTIVE_WAIT_S if role is SlotRole.INTERACTIVE else None
        async with self._pool_guard.placed(
            model, pool, role, prompt_tokens=prompt_tokens, max_tokens=max_tokens, wait_s=wait_s
        ) as placement:
            yield placement.slot, placement.max_tokens

    @staticmethod
    def _calibrate(provider: str, model: str, chars: int, n_images: int, usage: LlmUsage) -> None:
        # Every local call's real prompt size tightens the estimate the slot caps are checked
        # with. An image's tokens have no characters behind them, and on a short prompt the
        # chat template's fixed overhead dominates the ratio, so those calls are skipped.
        # This applies on the Standard engine too, where only streamed turns calibrated before
        # F3b: the same ratio drives its prefill bar, and more real samples only sharpen it.
        if (
            provider == local_catalog.LOCAL_PROVIDER
            and n_images == 0
            and usage.input_tokens >= _MIN_CALIBRATION_TOKENS
        ):
            prefill.calibrate(model, chars, usage.input_tokens)

    async def complete(
        self,
        task: str,
        *,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        videos: Sequence[LlmVideo] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        strength: str | None = None,
        spec_override: str | None = None,
        sampling: Sampling | None = None,
        slot_role: SlotRole | None = None,
    ) -> LlmResult:
        # `spec_override` is the per-call model pick (the omnibox's per-conversation
        # agent model) — same precedence as in converse_stream, so a background
        # completion (e.g. the research-report titler) can run on the exact model the chat
        # turn will use, no separate route and no model swap.
        provider, model, reasoning_effort = await self._admitted(
            task, strength, spec_override, await self._route(task, strength, spec_override)
        )
        # `sampling` is the prompt's per-task override (its `.prompt` `config: sampling:`
        # block); it merges over the resolved model's recommended defaults.
        resolved_sampling = self._resolve_sampling(provider, model, reasoning_effort, sampling)
        client = self._clients[provider]
        chars = len(system) + len(user_text)
        start = time.perf_counter()
        async with self._slot_pin(
            task,
            slot_role,
            provider,
            model,
            chars=chars,
            n_images=len(images),
            max_tokens=max_tokens,
            video_tokens=sum(slot_roles.video_tokens_charge(v.seconds) for v in videos),
        ) as (id_slot, max_tokens):
            # Only when pinned: test fakes and older clients do not take the keyword.
            slot_kw: dict[str, Any] = {} if id_slot is None else {"id_slot": id_slot}
            # Only when sent, for the same reason: no existing fake takes `videos`.
            if videos:
                slot_kw["videos"] = videos
            result = await client.complete(
                model=model,
                system=system,
                user_text=user_text,
                images=images,
                json_schema=json_schema,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                sampling=resolved_sampling,
                **slot_kw,
            )
            # A video's frames are image tokens with no characters behind them.
            self._calibrate(provider, model, chars, len(images) + len(videos), result.usage)
            # Recorded per provider call (the re-ask spends tokens too): the
            # ledger tracks what was billed, not what was usable.
            await self._record(task, provider, model, result.usage)
            if json_schema is not None and result.parsed is None:
                log.warning("llm.json_reask", task=task, provider=provider, model=model)
                result = await client.complete(
                    model=model,
                    system=system,
                    user_text=user_text + JSON_NUDGE,
                    images=images,
                    json_schema=json_schema,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    sampling=resolved_sampling,
                    **slot_kw,
                )
                await self._record(task, provider, model, result.usage)
                if result.parsed is None:
                    raise LlmBadResponseError(
                        f"{provider}: invalid JSON for task {task!r} after re-ask"
                    )
        elapsed = time.perf_counter() - start
        log.info(
            "llm.complete",
            task=task,
            provider=provider,
            model=model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            # A one-shot that spent its budget on a hidden thinking trace shows up as
            # empty text + large reasoning_chars — the signature of a starved budget.
            reasoning_chars=len(result.reasoning),
            elapsed_ms=round(elapsed * 1000),
            output_tokens_per_s=self._toks_per_s(result.usage.output_tokens, elapsed),
        )
        return result

    async def converse(
        self,
        task: str,
        *,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        strength: str | None = None,
        effort_override: str | None = None,
        spec_override: str | None = None,
        sampling: Sampling | None = None,
        slot_role: SlotRole | None = None,
    ) -> LlmTurn:
        """One tool-aware turn for the agent loop. Unlike `complete` there is no
        JSON re-ask — tool calls are structured by the provider, and the loop
        owns retry/continuation. Usage is recorded per call like everything else.

        `effort_override` lets a caller steer how hard the model thinks for THIS
        turn (the sub-agent spawner sets it per child); it wins over the resolved
        effort but is still dropped for a non-reasoning model — same gate as a
        stored override, so a non-reasoning route never receives the param.
        `spec_override` steers the MODEL for this turn (the omnibox's per-conversation
        pick), outranking the resolved route; a malformed/can't-serve override is
        ignored."""
        provider, model, reasoning_effort = await self._admitted(
            task, strength, spec_override, await self._route(task, strength, spec_override)
        )
        if effort_override is not None and _reasoning_capable(provider, model):
            reasoning_effort = effort_override
        resolved_sampling = self._resolve_sampling(provider, model, reasoning_effort, sampling)
        client = self._clients[provider]
        await self._ensure_agent_prefix(task, provider, model, system, tools, reasoning_effort)
        chars = slot_roles.prompt_chars(system, messages, tools)
        n_images = slot_roles.image_count(messages)
        start = time.perf_counter()
        async with self._slot_pin(
            task,
            slot_role,
            provider,
            model,
            chars=chars,
            n_images=n_images,
            max_tokens=max_tokens,
        ) as (id_slot, max_tokens):
            turn = await client.converse(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                sampling=resolved_sampling,
                **({} if id_slot is None else {"id_slot": id_slot}),
            )
        elapsed = time.perf_counter() - start
        self._calibrate(provider, model, chars, n_images, turn.usage)
        self._note_agent_turn(
            task,
            provider,
            model,
            turn.usage.input_tokens,
            system=system,
            tools=tools,
            reasoning_effort=reasoning_effort,
        )
        await self._record(task, provider, model, turn.usage)
        log.info(
            "llm.converse",
            task=task,
            provider=provider,
            model=model,
            stop_reason=turn.stop_reason,
            tool_calls=len(turn.tool_calls),
            input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            elapsed_ms=round(elapsed * 1000),
            output_tokens_per_s=self._toks_per_s(turn.usage.output_tokens, elapsed),
        )
        return turn

    async def converse_stream(
        self,
        task: str,
        *,
        system: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[LlmTool] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        strength: str | None = None,
        effort_override: str | None = None,
        spec_override: str | None = None,
        sampling: Sampling | None = None,
        slot_role: SlotRole | None = None,
    ) -> AsyncIterator[StreamPart]:
        """Stream a tool-aware turn for the agent loop (StreamPart events). Usage
        is recorded once from the closing LlmTurn — the streamed text chunks
        carry no usage, only the final turn does. `effort_override` steers the
        model's reasoning for this turn (gated to reasoning-capable models, like
        `converse`); `spec_override` steers the MODEL (the per-conversation pick),
        outranking the resolved route."""
        provider, model, reasoning_effort = await self._admitted(
            task, strength, spec_override, await self._route(task, strength, spec_override)
        )
        if effort_override is not None and _reasoning_capable(provider, model):
            reasoning_effort = effort_override
        resolved_sampling = self._resolve_sampling(provider, model, reasoning_effort, sampling)
        client = self._clients[provider]
        final: LlmTurn | None = None
        first_part = True
        start = time.perf_counter()
        # The gap before the first part is PREFILL — the model eating the prompt, and the
        # longest silence a local turn has. `watch` publishes how far in it is while the gap
        # runs, and stops the moment anything streams. The denominator is an estimate off the
        # prompt's own size, which `calibrate` below corrects from the turn's real usage —
        # less the prefix the KV cache already holds, which on a tool round is nearly all of
        # it (`prefill._fraction`).
        await self._ensure_agent_prefix(task, provider, model, system, tools, reasoning_effort)
        probe = self._slots_probe if provider == local_catalog.LOCAL_PROVIDER else None
        prompt_chars = slot_roles.prompt_chars(system, messages, tools)
        n_images = slot_roles.image_count(messages)
        # The row is opened by the first fraction that shows a wait, so a turn that answers
        # off a primed prefix — which is most of them — writes nothing at all.
        async with (
            self._slot_pin(
                task,
                slot_role,
                provider,
                model,
                chars=prompt_chars,
                n_images=n_images,
                max_tokens=max_tokens,
            ) as (id_slot, max_tokens),
            box_events.lazy_span(box_events.PREFILL, model, detail=_reading(messages)) as (
                publish,
                prefill_done,
            ),
            prefill.watch(
                probe,
                model,
                prompt_chars=prompt_chars,
                on_progress=publish if probe is not None else None,
                slot_id=id_slot,
            ) as streaming,
        ):
            slot_kw: dict[str, int] = {} if id_slot is None else {"id_slot": id_slot}
            # Tracked so a truncated stream can be recovered ONLY when nothing visible has
            # been shown yet: re-issuing after answer text has streamed would replay it to
            # the reader. Reasoning chunks don't count — they are a scratch channel the PWA
            # renders as transient thinking, not the answer.
            answered = False
            reasoned = 0  # chars of reasoning already streamed, so a recovery can't repeat it
            try:
                async for part in client.converse_stream(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    sampling=resolved_sampling,
                    **slot_kw,
                ):
                    if first_part:
                        first_part = False
                        # Prefill ended HERE, not when this block does. The row has to be
                        # settled at the moment the wait it describes is over, or the status
                        # line reads "Reading your prompt…" for the whole answer (measured on
                        # the box).
                        await prefill_done()
                    streaming()
                    if isinstance(part, LlmTurn):
                        final = part
                    elif isinstance(part, TextChunk):
                        answered = True
                    elif isinstance(part, ReasoningChunk):
                        reasoned += len(part.text)
                    yield part
            except LlmStreamTruncatedError:
                # The stream was cut before any finish_reason (llm/errors.py). The ROUND is
                # intact — the prompt is unchanged and nothing was committed — and the same
                # call non-streaming is reliable where the streaming tool-call path is not
                # (measured on the box: 12/12 versus ~44%). So re-issue it once, unstreamed,
                # and replay the completed turn as parts. This is the difference between an
                # agent losing a sitting and an agent taking one slower step.
                if answered:
                    raise  # answer text already reached the reader; a retry would duplicate it
                log.warning("llm.stream_truncated_retry", task=task, provider=provider, model=model)
                turn = await client.converse(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    sampling=resolved_sampling,
                    **slot_kw,
                )
                if first_part:
                    first_part = False
                    await prefill_done()
                streaming()
                # Replayed in the order a live stream would have produced them, so a consumer
                # that switches on part type cannot tell the recovered turn from a clean one.
                # Only the part that never streamed. The truncated attempt already emitted
                # its reasoning prefix, and replaying the whole thing would show the reader
                # (and the transcript) the same thinking twice.
                if turn.reasoning and len(turn.reasoning) > reasoned:
                    yield ReasoningChunk(text=turn.reasoning[reasoned:])
                if turn.text:
                    yield TextChunk(text=turn.text)
                final = turn
                yield turn
            finally:
                # A stream the owner STOPS never reaches the tail below: GeneratorExit is
                # thrown at a `yield`, so `_note_agent_turn` and everything after it is
                # skipped. The prompt was still sent and the slot still grown, so the store's
                # restored-but-unused memo would stay set for the rest of this process — and
                # `restore_if_lost` returns False on it before it reads /slots at all, making
                # the owner's NEXT turn pay the prefill this store exists to prevent.
                # `first_part` is still True only if nothing ever arrived, in which case no
                # slot was touched and there is nothing to retire.
                if final is None and not first_part:
                    self._note_prefix_used(task, provider, model, system, tools, reasoning_effort)
        if final is not None:
            elapsed = time.perf_counter() - start
            # The exact token count for the characters we just sent — the only free, exact
            # calibration this box offers, and it arrives on every turn.
            self._calibrate(provider, model, prompt_chars, n_images, final.usage)
            self._note_agent_turn(
                task,
                provider,
                model,
                final.usage.input_tokens,
                system=system,
                tools=tools,
                reasoning_effort=reasoning_effort,
            )
            await self._record(task, provider, model, final.usage)
            log.info(
                "llm.converse_stream",
                task=task,
                provider=provider,
                model=model,
                stop_reason=final.stop_reason,
                tool_calls=len(final.tool_calls),
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
                elapsed_ms=round(elapsed * 1000),
                output_tokens_per_s=self._toks_per_s(final.usage.output_tokens, elapsed),
            )


def build_router(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    recorder: UsageRecorder | None = None,
    overrides_loader: Callable[[], Awaitable[Mapping[str, Mapping[str, str]]]] | None = None,
    local_windows_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
    residency: LocalAdmitter,
    slots_probe: prefill.SlotsReader | None = None,
    kv_prefix: "kv_prefix_mod.KvPrefixStore | None" = None,
    engine_loader: Callable[[], Awaitable[engines.Engine]] | None = None,
    admission_gate: Callable[[], Awaitable[bool]] | None = None,
    pool_guard: kv_pool_guard_mod.KvPoolGuard | None = None,
    engine_efforts_loader: Callable[[], Awaitable[EngineEfforts]] | None = None,
) -> LlmRouter:
    """Wire the three providers from settings; transport/sleep injectable for tests.
    `overrides_loader` supplies the live DB-backed per-task overrides;
    `local_windows_loader` the live per-model context-window overrides.

    Residency admission (evict-to-make-room before a local load) is NOT an opt-in the
    caller can forget: the gateway never self-evicts (`swap: false`), so an unadmitted
    local load co-loads past the unified-memory budget and hard-locks the box (it did —
    the worker used to build an unadmitted router). `residency` is therefore REQUIRED.

    It used to be optional, with a `_default_residency` built from `settings` for callers
    that passed none. That default was the weaker of two gates: it carried no `hold_loader`,
    so `_held_names()` returned an empty set — and empty means *not held*, i.e. admit
    everything. An operator reservation could be enforced on the API's coordinator and be
    invisible on this one. It had no `box_lock` either, so its `ensure_room` evicted without
    loading and left the client to trigger the load unserialized.

    Nothing in production ever used it: `main.py` and `worker.py` are the only two
    `build_router` callers and both pass their own fully-wired coordinator. It existed only
    to be silently wrong for whoever forgot. Deleting it is what makes "one way in" true by
    construction rather than by convention — a caller that has no coordinator now fails to
    compile instead of getting a gate that answers differently.

    `slots_probe` is the gateway's `/slots` reader, and is what turns the prefill diagnostic
    on (jbrain.llm.prefill). Optional in the way admission is NOT: this one only reads,
    so a caller that omits it loses a log line, not the box.

    `engine_loader` (the process's ActiveEngine) turns on the engine remap (plan §4c) and
    `admission_gate` the engine switch's drain (jbrain.llm.drain); both production callers
    pass the same instances their residency coordinator reads.

    `pool_guard` (jbrain.llm.kv_pool_guard) keeps a pooled model's shared KV from overrunning
    and catches a stale slot layout. One per process, shared with anything else that pins slots
    there (the api's jcode proxy), so its decisions serialize and its pending calls are seen.
    Without it a pooled model's calls are still pinned and capped, off the catalog alone.

    `engine_efforts_loader` (an `engine_effort.EngineEffortCache`'s `get`) applies the owner's
    per-engine reasoning levels to calls that run on Flash-Next; without it they keep their
    Standard effort."""
    extra: dict[str, Any] = {"transport": transport}
    if sleep is not None:
        extra["sleep"] = sleep
    clients: dict[str, LlmClient] = {
        "anthropic": AnthropicClient(settings.anthropic_api_key, **extra),
        "xai": OpenAiCompatClient(XAI_BASE_URL, settings.xai_api_key, provider="xai", **extra),
        "local": OpenAiCompatClient(
            settings.local_llm_url,
            "",
            provider="local",
            timeout=settings.local_llm_timeout,
            **extra,
        ),
    }
    return LlmRouter(
        clients,
        resolve_tasks(settings.llm_tasks),
        recorder=recorder,
        tiers=resolve_tiers(settings.llm_tiers),
        pinned=frozenset(settings.llm_tasks),
        overrides_loader=overrides_loader,
        local_windows_loader=local_windows_loader,
        residency=residency,
        slots_probe=slots_probe,
        local_enabled=settings.local_llm_enabled,
        kv_prefix=kv_prefix,
        engine_loader=engine_loader,
        admission_gate=admission_gate,
        pool_guard=pool_guard,
        engine_efforts_loader=engine_efforts_loader,
    )
