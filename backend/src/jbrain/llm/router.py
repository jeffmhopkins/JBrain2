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

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx
import structlog

from jbrain.config import Settings
from jbrain.llm import local_catalog
from jbrain.llm.anthropic import AnthropicClient
from jbrain.llm.errors import LlmBadResponseError, LlmError
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    LlmClient,
    LlmImage,
    LlmMessage,
    LlmResult,
    LlmTool,
    LlmTurn,
    LlmUsage,
    StreamPart,
    UsageRecorder,
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
    # Auto-titling a chat from its first exchange — a cheap one-shot summary; the
    # prompt declares the `low` tier (a reasoning model, so its prompt budgets
    # tokens for the thinking trace, not just the title). This default is just the
    # operator-override hook.
    "session.title": "xai:grok-4.3",
    # The Phase-6 wiki builder (docs/plans/PHASE6_WIKI_PLAN.md): `wiki.rewrite` drafts a
    # type-guided article from an entity's cited facts; `wiki.ground` is the strict
    # grounding verifier (the entity graph wins on conflict). Without these the
    # builder's router.complete() raises `unknown LLM task` and every build aborts.
    # Individually routable so an on-box operator can point them at a local model.
    "wiki.rewrite": "xai:grok-4.3",
    "wiki.ground": "xai:grok-4.3",
    # The Phase-6 wiki HEALTH sweep (docs/plans/WIKI_LINT_PLAN.md, Wave B):
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
    # Medium reasoning
    "agent.turn": "medium",
    "note.extract": "medium",
    "correction_note.extract": "medium",
    "video.summarize": "medium",
    "wiki.rewrite": "medium",
    "intake.materialize": "medium",
    # Low reasoning
    "entity.disambiguate": "low",
    "session.title": "low",
    "triage.classify": "low",
}

# The deviations the router must ACTIVELY put on the wire. Medium is omitted on
# purpose: it is the reasoning model's own built-in default, and pinning it would
# override the sub-agent spawner's contract that "no chosen effort → the child
# model's default" (a plain child must reach the client with reasoning_effort=None).
# So a medium-bucket task resolves to None and lets the model use its native medium.
TASK_REASONING_DEFAULTS: dict[str, str] = {
    task: effort for task, effort in TASK_REASONING_BUCKET.items() if effort != "medium"
}

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
        local_enabled: bool = True,
    ):
        self._clients = clients
        self._tasks = tasks
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

    async def _resolve_live(self, task: str, strength: str | None) -> tuple[str, str, str | None]:
        """Resolve (provider, model, reasoning_effort) folding in the live DB
        overrides. A stored `spec` is the HIGHEST-precedence selector — above an
        env pin, the strength tier, and the task default — because the settings
        screen is the operator's live control surface and must win over any
        deploy-time config. A stored `reasoning_effort` applies only when the
        resolved provider+model is reasoning-capable (xai Grok, or a local reasoning
        model like gpt-oss/GLM); for anything else it is dropped. Malformed stored
        entries are ignored: a bad saved setting must never break a call."""
        provider, model = self._resolve(task, strength)
        # The task's bucket default (high/low deviations only) unless a stored
        # override replaces it below — so a fresh box runs at the right effort.
        reasoning_effort: str | None = TASK_REASONING_DEFAULTS.get(task)
        if self._overrides_loader is not None:
            overrides = await self._overrides_loader()
            entry = overrides.get(task) or {}
            spec = entry.get("spec")
            if spec is not None:
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
            if stored_effort:
                reasoning_effort = stored_effort
        if not _reasoning_capable(provider, model):
            reasoning_effort = None
        return provider, model, reasoning_effort

    async def context_window(self, task: str, strength: str | None = None) -> int:
        """The total context window (tokens) the `task` will actually run against
        after live overrides — the denominator for the PWA's context-usage meter. A
        local model's window comes from the catalog (the gateway's `-c`); a cloud
        model's from CONTEXT_WINDOWS, falling back to a conservative default for an
        unlisted model so the meter degrades gracefully rather than misreports."""
        provider, model, _ = await self._resolve_live(task, strength)
        if provider == "local":
            if self._local_windows_loader is not None:
                windows = await self._local_windows_loader()
                cat_id = local_catalog.id_for_served(model)
                if cat_id is not None and cat_id in windows:
                    return windows[cat_id]
            return local_catalog.context_window(model)
        return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)

    async def supports_vision(self, task: str, strength: str | None = None) -> bool:
        """Whether the model `task` actually resolves to (after live overrides) can
        accept image content in a turn. A local model declares it in the catalog —
        a text-only gateway model like gpt-oss has no vision projector; the cloud
        providers we wire (Grok, Claude 4.x) are all multimodal, so any non-local
        route is vision-capable. The agent path consults this to DROP image bytes a
        non-vision model can't read (the model still sees the attachment's id as
        text, so it can edit it or analyze it by reference)."""
        provider, model, _ = await self._resolve_live(task, strength)
        if provider == "local":
            return local_catalog.supports_vision(model)
        return True

    async def effective_reasoning_effort(
        self, task: str, strength: str | None = None
    ) -> str | None:
        """The reasoning effort a `task` will actually run with after live overrides —
        None when the resolved model isn't reasoning-capable. Lets a caller (e.g. the
        agent loop) size its budget to how hard the model is set to think."""
        return (await self._resolve_live(task, strength))[2]

    def spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        """The (provider, model) a task resolves to from STATIC config alone — env
        pin, prompt tier, or task default. It does NOT see the live DB overrides, so
        it must not stamp provenance for an operator-overridable task; use it only
        where the live route can't matter (e.g. a routability probe). Pass the
        prompt's `strength` so a tier resolves the way `complete` would."""
        return self._resolve(task, strength)

    async def effective_spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        """The (provider, model) a task will ACTUALLY run on after folding in the live
        DB overrides — the override-aware sibling of `spec()`. Provenance stamps
        (`extractor`, an extract's `tool`) MUST use this so the recorded model matches
        the one `complete` used; `spec()` would mis-stamp the static default for any
        task the operator re-routed in Settings."""
        return (await self._resolve_live(task, strength))[:2]

    @staticmethod
    def _toks_per_s(output_tokens: int, elapsed_s: float) -> float | None:
        """End-to-end output tokens/sec (prefill included) — the throughput a caller
        actually feels, which is why a big-active-param local model like the 235B
        reads low. None for a zero/negative interval. Logged per call so 'ask vs
        response time and t/s' is visible in the api log without llama-server's own
        timings (llama-swap doesn't surface those)."""
        return round(output_tokens / elapsed_s, 1) if elapsed_s > 0 else None

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
    ) -> LlmResult:
        provider, model, reasoning_effort = await self._resolve_live(task, strength)
        client = self._clients[provider]
        start = time.perf_counter()
        result = await client.complete(
            model=model,
            system=system,
            user_text=user_text,
            images=images,
            json_schema=json_schema,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
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
    ) -> LlmTurn:
        """One tool-aware turn for the agent loop. Unlike `complete` there is no
        JSON re-ask — tool calls are structured by the provider, and the loop
        owns retry/continuation. Usage is recorded per call like everything else.

        `effort_override` lets a caller steer how hard the model thinks for THIS
        turn (the sub-agent spawner sets it per child); it wins over the resolved
        effort but is still dropped for a non-reasoning model — same gate as a
        stored override, so a non-reasoning route never receives the param."""
        provider, model, reasoning_effort = await self._resolve_live(task, strength)
        if effort_override is not None and _reasoning_capable(provider, model):
            reasoning_effort = effort_override
        client = self._clients[provider]
        start = time.perf_counter()
        turn = await client.converse(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        elapsed = time.perf_counter() - start
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
    ) -> AsyncIterator[StreamPart]:
        """Stream a tool-aware turn for the agent loop (StreamPart events). Usage
        is recorded once from the closing LlmTurn — the streamed text chunks
        carry no usage, only the final turn does. `effort_override` steers the
        model's reasoning for this turn (gated to reasoning-capable models, like
        `converse`)."""
        provider, model, reasoning_effort = await self._resolve_live(task, strength)
        if effort_override is not None and _reasoning_capable(provider, model):
            reasoning_effort = effort_override
        client = self._clients[provider]
        final: LlmTurn | None = None
        start = time.perf_counter()
        async for part in client.converse_stream(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        ):
            if isinstance(part, LlmTurn):
                final = part
            yield part
        if final is not None:
            elapsed = time.perf_counter() - start
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
) -> LlmRouter:
    """Wire the three providers from settings; transport/sleep injectable for tests.
    `overrides_loader` supplies the live DB-backed per-task overrides;
    `local_windows_loader` the live per-model context-window overrides (both None
    keep the static-config behavior)."""
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
        local_enabled=settings.local_llm_enabled,
    )
