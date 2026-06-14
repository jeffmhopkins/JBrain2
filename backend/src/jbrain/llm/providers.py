"""Selectable LLM providers for the runtime settings screen.

The router and env config speak raw "provider:model" specs (TASK_DEFAULTS,
JBRAIN_LLM_TASKS). The settings UI needs a smaller, stable vocabulary: a fixed
set of curated choices with human labels and a "supports reasoning" flag, each
mapping to one spec. This module is that mapping — the one place that knows
which specs the UI offers — so the API and the router stay spec-native while the
screen stays id-native. Unknown stored specs reverse-map to id None so the API
can still surface the raw spec rather than crashing.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from jbrain.config import Settings

# Grok is the only reasoning-capable provider here; xAI's `reasoning_effort` has
# no analogue for the local server, and Anthropic uses a separate thinking
# mechanism that is out of scope for this control surface.
REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high")
# xAI's own default when the param is unset. The UI shows this for Grok tasks
# that have no stored override so the displayed value matches what gets sent.
REASONING_DEFAULT = "low"


@dataclass(frozen=True)
class ProviderChoice:
    id: str
    label: str
    spec: str
    supports_reasoning: bool


def provider_choices(settings: Settings) -> tuple[ProviderChoice, ...]:
    """The selectable providers in UI order. `local`'s model comes from settings
    so the local spec names a concrete model rather than a placeholder."""
    return (
        ProviderChoice("grok", "Grok 4.3", "xai:grok-4.3", supports_reasoning=True),
        ProviderChoice(
            "claude", "Claude Sonnet 4.6", "anthropic:claude-sonnet-4-6", supports_reasoning=False
        ),
        ProviderChoice("local", "Local", f"local:{settings.local_llm_model}", False),
    )


def _by_id(settings: Settings) -> Mapping[str, ProviderChoice]:
    return {c.id: c for c in provider_choices(settings)}


def spec_for_id(settings: Settings, provider_id: str) -> str | None:
    """The "provider:model" spec a UI provider id maps to, or None if unknown."""
    choice = _by_id(settings).get(provider_id)
    return choice.spec if choice else None


def id_for_spec(settings: Settings, spec: str) -> str | None:
    """Reverse map a spec to its UI id; None when no curated choice matches
    (e.g. an env pin to an off-menu model) so callers surface the raw spec."""
    for choice in provider_choices(settings):
        if choice.spec == spec:
            return choice.id
    return None


def supports_reasoning(settings: Settings, provider_id: str) -> bool:
    choice = _by_id(settings).get(provider_id)
    return bool(choice and choice.supports_reasoning)
