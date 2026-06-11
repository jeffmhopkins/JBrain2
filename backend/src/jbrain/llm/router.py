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

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx
import structlog

from jbrain.config import Settings
from jbrain.llm.anthropic import AnthropicClient
from jbrain.llm.errors import LlmBadResponseError, LlmError
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    LlmClient,
    LlmImage,
    LlmResult,
    LlmUsage,
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
    ):
        self._clients = clients
        self._tasks = tasks
        self._recorder = recorder
        # Capability-tier → (provider, model), and the set of tasks a human
        # explicitly pinned in config (an explicit pin outranks a prompt's tier).
        # Default to TIER_DEFAULTS so any router (including test fakes that pass
        # only tasks) can resolve a prompt's declared strength.
        self._tiers = dict(tiers) if tiers is not None else resolve_tiers({})
        self._pinned = pinned

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
        provider, model = self._resolve(task, strength)
        client = self._clients[provider]
        result = await client.complete(
            model=model,
            system=system,
            user_text=user_text,
            images=images,
            json_schema=json_schema,
            max_tokens=max_tokens,
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


def build_router(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    recorder: UsageRecorder | None = None,
) -> LlmRouter:
    """Wire the three providers from settings; transport/sleep injectable for tests."""
    extra: dict[str, Any] = {"transport": transport}
    if sleep is not None:
        extra["sleep"] = sleep
    clients: dict[str, LlmClient] = {
        "anthropic": AnthropicClient(settings.anthropic_api_key, **extra),
        "xai": OpenAiCompatClient(XAI_BASE_URL, settings.xai_api_key, provider="xai", **extra),
        "local": OpenAiCompatClient(settings.local_llm_url, "", provider="local", **extra),
    }
    return LlmRouter(
        clients,
        resolve_tasks(settings.llm_tasks),
        recorder=recorder,
        tiers=resolve_tiers(settings.llm_tiers),
        pinned=frozenset(settings.llm_tasks),
    )
