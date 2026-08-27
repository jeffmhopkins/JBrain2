"""Task-profile routing: every LLM call happens under a named task.

OWNER DECISION (recorded verbatim): every LLM call happens under a named task
profile. Initial tasks: note.extract, entity.disambiguate, fact.adjudicate,
correction_note.extract, vision.ocr, vision.caption. Each task maps to
"provider:model" and is INDIVIDUALLY configurable; the default for EVERY task
is "xai:grok-4.3". Config via pydantic-settings: a JBRAIN_LLM_TASKS env var
holding a JSON object of overrides ({"note.extract":
"anthropic:claude-sonnet-4-6"}) merged over the defaults.

The "local" provider must exist now so going all-local is config, not
refactor — docs/reference/ANALYSIS.md "Privacy routing".
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx
import structlog

from jbrain import box_events
from jbrain.config import Settings
from jbrain.llm import kv_prefix as kv_prefix_mod
from jbrain.llm import local_catalog, model_sampling, prefill
from jbrain.llm.anthropic import AnthropicClient
from jbrain.llm.errors import LlmBadResponseError, LlmError, LlmStreamTruncatedError
from jbrain.llm.openai_compat import OpenAiCompatClient
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
    ReasoningChunk,
    Sampling,
    StreamPart,
    TextChunk,
    ToolResultMessage,
    UsageRecorder,
    UserMessage,
)

log = structlog.get_logger()

XAI_BASE_URL = "https://api.x.ai/v1"

TASK_DEFAULTS: dict[str, str] = {
    "note.extract": "xai:grok-4.3",
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
    # The note→graph Integrator: graph-aware coreference/relationship/gender
    # judgment that produces an IntegrationIntent (docs/archive/INTEGRATOR_PLAN.md). Strong
    # tier — it owns the hard decisions the deterministic core then validates.
    "integrate.note": "xai:grok-4.3",
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
    "integrate.note": "high",
    "fact.adjudicate": "high",
    "wiki.ground": "high",
    "wiki.lint.contradiction": "high",
    "wiki.lint.stale": "high",
    "pet.statue": "high",
    # Medium reasoning
    "agent.turn": "medium",
    "note.extract": "medium",
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

JSON_NUDGE = (
    "\n\nYour previous reply was not valid JSON."
    " Return only valid JSON matching the requested schema — no prose, no code fences."
)


def context_window_for_spec(spec: str) -> int:
    """The total context window for a raw "provider:model" spec, WITHOUT resolving
    live overrides — the spec-based twin of LlmRouter.context_window. The capabilities
    endpoint uses it to seed the composer's context meter before the first turn, so the
    window reads consistently with the vision flag (both off the same resolved spec).
    A local window comes from the catalog default; a live per-model `-c` override only
    takes effect once a turn actually streams (the meter corrects itself then). Cloud
    windows come from CONTEXT_WINDOWS, with the conservative default for an unlisted
    model so the meter degrades gracefully rather than misreports."""
    provider, _, model = spec.partition(":")
    if provider == "local":
        return local_catalog.context_window(model)
    return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def _split_spec(label: str, spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    if not sep or not provider or not model:
        raise LlmError(f"malformed LLM spec for {label!r}: {spec!r}")
    if provider not in PROVIDERS:
        raise LlmError(f"unknown LLM provider for {label!r}: {provider!r}")
    return provider, model


def warm_reasoning_effort(task: str, served_model: str, stored: str | None) -> str | None:
    """The reasoning effort a `task` turn would carry on a LOCAL `served_model` — for the
    gateway's load-time warm-up, which must render the exact prompt a routed turn will
    send (the effort lands in the prompt's leading tokens; see
    `openai_compat.apply_local_reasoning`). Mirrors `_resolve_live`'s stored-override-else-
    default fold, then gates on the model itself: the model being LOADED is not always the
    model the task routes to, and a non-reasoning model must not carry the field."""
    effort = TASK_REASONING_DEFAULTS.get(task)
    if stored:
        effort = stored
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


def resolve_tasks(overrides: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Merge overrides over TASK_DEFAULTS and split each "provider:model".

    Strict on unknown tasks, unknown providers, and malformed specs — a typo
    in routing config should fail at startup, not silently fall back.
    """
    merged = dict(TASK_DEFAULTS)
    for task, spec in overrides.items():
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

    async def ensure_room(self, served_model: str) -> None: ...


def _prompt_chars(system: str, messages: Sequence[LlmMessage], tools: Sequence[LlmTool]) -> int:
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
    ):
        self._clients = clients
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

    def _note_agent_turn(self, task: str, provider: str, model: str, input_tokens: int) -> None:
        """Tell the store a real jerv turn's prompt size, so the slot that conversation
        grew keeps reading as 'prefix present' (restoring over it would wipe cached
        history to re-plant a prefix the conversation already extends)."""
        if (
            self._kv_prefix is not None
            and task == kv_prefix_mod.AGENT_TURN_TASK
            and provider == local_catalog.LOCAL_PROVIDER
        ):
            self._kv_prefix.note_agent_turn(model, input_tokens)

    async def _admit_local(self, provider: str, model: str) -> None:
        if provider == local_catalog.LOCAL_PROVIDER and self._residency is not None:
            await self._residency.ensure_room(model)

    async def admit_local_load(self, served_model: str) -> None:
        """Make room for a local model a CALLER is about to load itself, through the same
        admission a routed completion gets.

        For the warm keeper, which must bring weights up before it can restore a saved KV slot
        (a cold model has no slots to restore into) and so cannot let the priming completion be
        what loads it. Without this it would either skip admission — co-loading past the
        unified-memory budget, on a box that hard-locks when that happens — or reach into
        `_admit_local`, which is the same bypass with extra steps."""
        await self._admit_local(local_catalog.LOCAL_PROVIDER, served_model)

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
        return model if provider == local_catalog.LOCAL_PROVIDER else None

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
        drops the effort param the resolved route would have carried."""
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
        if not _reasoning_capable(provider, model):
            reasoning_effort = None
        return provider, model, reasoning_effort

    async def context_window(
        self, task: str, strength: str | None = None, spec_override: str | None = None
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
        reasoning level) wins over the stored effort under the same capability gate —
        matching what `converse`/`converse_stream` will actually send."""
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

    async def complete(
        self,
        task: str,
        *,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        strength: str | None = None,
        spec_override: str | None = None,
        sampling: Sampling | None = None,
    ) -> LlmResult:
        # `spec_override` is the per-call model pick (the omnibox's per-conversation
        # agent model) — same precedence as in converse_stream, so a background
        # completion (e.g. the research-report titler) can run on the exact model the chat
        # turn will use, no separate route and no model swap.
        provider, model, reasoning_effort = await self._resolve_live(task, strength, spec_override)
        # `sampling` is the prompt's per-task override (its `.prompt` `config: sampling:`
        # block); it merges over the resolved model's recommended defaults.
        resolved_sampling = self._resolve_sampling(provider, model, reasoning_effort, sampling)
        client = self._clients[provider]
        await self._admit_local(provider, model)
        start = time.perf_counter()
        result = await client.complete(
            model=model,
            system=system,
            user_text=user_text,
            images=images,
            json_schema=json_schema,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            sampling=resolved_sampling,
        )
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
        provider, model, reasoning_effort = await self._resolve_live(task, strength, spec_override)
        if effort_override is not None and _reasoning_capable(provider, model):
            reasoning_effort = effort_override
        resolved_sampling = self._resolve_sampling(provider, model, reasoning_effort, sampling)
        client = self._clients[provider]
        await self._admit_local(provider, model)
        await self._ensure_agent_prefix(task, provider, model, system, tools, reasoning_effort)
        start = time.perf_counter()
        turn = await client.converse(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            sampling=resolved_sampling,
        )
        elapsed = time.perf_counter() - start
        self._note_agent_turn(task, provider, model, turn.usage.input_tokens)
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
    ) -> AsyncIterator[StreamPart]:
        """Stream a tool-aware turn for the agent loop (StreamPart events). Usage
        is recorded once from the closing LlmTurn — the streamed text chunks
        carry no usage, only the final turn does. `effort_override` steers the
        model's reasoning for this turn (gated to reasoning-capable models, like
        `converse`); `spec_override` steers the MODEL (the per-conversation pick),
        outranking the resolved route."""
        provider, model, reasoning_effort = await self._resolve_live(task, strength, spec_override)
        if effort_override is not None and _reasoning_capable(provider, model):
            reasoning_effort = effort_override
        resolved_sampling = self._resolve_sampling(provider, model, reasoning_effort, sampling)
        client = self._clients[provider]
        await self._admit_local(provider, model)
        final: LlmTurn | None = None
        first_part = True
        start = time.perf_counter()
        # The gap before the first part is PREFILL — the model eating the prompt, and the
        # longest silence a local turn has. `watch` publishes how far in it is while the gap
        # runs, and stops the moment anything streams. The denominator is an estimate off the
        # prompt's own size, which `calibrate` below corrects from the turn's real usage.
        await self._ensure_agent_prefix(task, provider, model, system, tools, reasoning_effort)
        probe = self._slots_probe if provider == local_catalog.LOCAL_PROVIDER else None
        prompt_chars = _prompt_chars(system, messages, tools)
        # The row is opened by the first published fraction, so a turn that answers off a
        # primed prefix — which is most of them — writes nothing at all.
        async with (
            box_events.lazy_span(box_events.PREFILL, model) as (publish, prefill_done),
            prefill.watch(
                probe,
                model,
                prompt_chars=prompt_chars,
                on_progress=publish if probe is not None else None,
            ) as streaming,
        ):
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
        if final is not None:
            elapsed = time.perf_counter() - start
            # The exact token count for the characters we just sent — the only free, exact
            # calibration this box offers, and it arrives on every turn.
            if probe is not None:
                prefill.calibrate(model, prompt_chars, final.usage.input_tokens)
            self._note_agent_turn(task, provider, model, final.usage.input_tokens)
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
    so a caller that omits it loses a log line, not the box."""
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
    )
