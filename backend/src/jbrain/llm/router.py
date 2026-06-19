"""Task-profile routing: every LLM call happens under a named task.

OWNER DECISION (recorded verbatim): every LLM call happens under a named task
profile. Initial tasks: note.extract, entity.disambiguate, fact.adjudicate,
correction_note.extract, vision.ocr, vision.caption. Each task maps to
"provider:model" and is INDIVIDUALLY configurable; the default for EVERY task
is "xai:grok-4.3". Config via pydantic-settings: a JBRAIN_LLM_TASKS env var
holding a JSON object of overrides ({"note.extract":
"anthropic:claude-sonnet-4-6"}) merged over the defaults.

The "local" provider must exist now so going all-local is config, not
refactor — docs/ANALYSIS.md "Privacy routing".
"""

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
    # The tool-using personal agent's turn (docs/ASSISTANT.md). Strong tier by
    # default — agent reasoning over tools is the high-stakes path.
    "agent.turn": "xai:grok-4.3",
    # The note→graph Integrator: graph-aware coreference/relationship/gender
    # judgment that produces an IntegrationIntent (docs/archive/INTEGRATOR_PLAN.md). Strong
    # tier — it owns the hard decisions the deterministic core then validates.
    "integrate.note": "xai:grok-4.3",
    # Auto-titling a chat from its first exchange — a cheap one-shot summary; the
    # prompt declares the `low` tier, this default is just the operator-override hook.
    "session.title": "xai:grok-4.3",
}

# Capability tiers (a prompt's `strength:`) → "provider:model". A prompt names a
# tier, never a model, so swapping the model behind a tier is config, not a
# prompt edit (docs/ANALYSIS.md "Privacy routing"). Today every tier resolves to
# the same default as the tasks; the "embedding" tier is served by the embed
# container, not this completion router, so it is not listed here.
TIER_DEFAULTS: dict[str, str] = {
    "high": "xai:grok-4.3",
    "low": "xai:grok-4.3",
    "vision": "xai:grok-4.3",
}

PROVIDERS = ("anthropic", "xai", "local")

JSON_NUDGE = (
    "\n\nYour previous reply was not valid JSON."
    " Return only valid JSON matching the requested schema — no prose, no code fences."
)


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
        reasoning_effort: str | None = None
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
            reasoning_effort = entry.get("reasoning_effort")
        if not _reasoning_capable(provider, model):
            reasoning_effort = None
        return provider, model, reasoning_effort

    async def effective_reasoning_effort(
        self, task: str, strength: str | None = None
    ) -> str | None:
        """The reasoning effort a `task` will actually run with after live overrides —
        None when the resolved model isn't reasoning-capable. Lets a caller (e.g. the
        agent loop) size its budget to how hard the model is set to think."""
        return (await self._resolve_live(task, strength))[2]

    def spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        """The (provider, model) a task resolves to — callers stamp it as
        fact provenance (`extractor`) without touching provider clients. Pass the
        prompt's `strength` so the stamp matches the model `complete` will use."""
        return self._resolve(task, strength)

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
        log.info(
            "llm.complete",
            task=task,
            provider=provider,
            model=model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
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
    ) -> LlmTurn:
        """One tool-aware turn for the agent loop. Unlike `complete` there is no
        JSON re-ask — tool calls are structured by the provider, and the loop
        owns retry/continuation. Usage is recorded per call like everything else."""
        provider, model, reasoning_effort = await self._resolve_live(task, strength)
        client = self._clients[provider]
        turn = await client.converse(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
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
    ) -> AsyncIterator[StreamPart]:
        """Stream a tool-aware turn for the agent loop (StreamPart events). Usage
        is recorded once from the closing LlmTurn — the streamed text chunks
        carry no usage, only the final turn does."""
        provider, model, reasoning_effort = await self._resolve_live(task, strength)
        client = self._clients[provider]
        final: LlmTurn | None = None
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
            )


def build_router(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    recorder: UsageRecorder | None = None,
    overrides_loader: Callable[[], Awaitable[Mapping[str, Mapping[str, str]]]] | None = None,
) -> LlmRouter:
    """Wire the three providers from settings; transport/sleep injectable for tests.
    `overrides_loader` supplies the live DB-backed per-task overrides (None keeps
    the static-config behavior)."""
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
        local_enabled=settings.local_llm_enabled,
    )
