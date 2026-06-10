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

PROVIDERS = ("anthropic", "xai", "local")

JSON_NUDGE = (
    "\n\nYour previous reply was not valid JSON."
    " Return only valid JSON matching the requested schema — no prose, no code fences."
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
    resolved: dict[str, tuple[str, str]] = {}
    for task, spec in merged.items():
        provider, sep, model = spec.partition(":")
        if not sep or not provider or not model:
            raise LlmError(f"malformed LLM task spec for {task!r}: {spec!r}")
        if provider not in PROVIDERS:
            raise LlmError(f"unknown LLM provider for {task!r}: {provider!r}")
        resolved[task] = (provider, model)
    return resolved


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
    ):
        self._clients = clients
        self._tasks = tasks
        self._recorder = recorder

    def spec(self, task: str) -> tuple[str, str]:
        """The (provider, model) a task resolves to — callers stamp it as
        fact provenance (`extractor`) without touching provider clients."""
        try:
            return self._tasks[task]
        except KeyError:
            raise LlmError(f"unknown LLM task: {task!r}") from None

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
    ) -> LlmResult:
        try:
            provider, model = self._tasks[task]
        except KeyError:
            raise LlmError(f"unknown LLM task: {task!r}") from None
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
    return LlmRouter(clients, resolve_tasks(settings.llm_tasks), recorder=recorder)
