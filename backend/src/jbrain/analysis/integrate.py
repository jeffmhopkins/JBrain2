"""The Integrator: turns a note's extraction + graph context into an
IntegrationIntent via one graph-aware structured model call (docs/archive/INTEGRATOR_PLAN.md
Track B).

This first cut is deterministic CONTEXT INJECTION, not a tool-traversal loop: the
caller retrieves the relevant graph context and the model produces the intent in
one constrained `complete` call. The bounded read-tool traversal loop (the agent
choosing what to read) is a later enhancement; the contract — model in, validated
IntegrationIntent out — is the same either way. The agent decides MEANING; the
deterministic arbiter (apply_intent) validates and commits.
"""

from __future__ import annotations

import uuid

from jbrain.analysis.extraction import Extraction
from jbrain.analysis.integrate_prompt import (
    INTEGRATE_MAX_TOKENS,
    INTEGRATE_PROMPT_VERSION,
    INTEGRATE_STRENGTH,
    INTEGRATE_SYSTEM,
    build_integrate_prompt,
)
from jbrain.analysis.intent import IntegrationIntent
from jbrain.analysis.intent_parse import INTENT_SCHEMA, parse_intent
from jbrain.llm import LlmRouter

# Stamped on every IntegrationIntent alongside the prompt version; CI-guarded
# bump when the driver's contract changes (mirrors PROMPT_VERSION discipline).
INTEGRATOR_VERSION = "integrator-v2"


class Integrator:
    def __init__(self, router: LlmRouter):
        self._router = router

    async def integrate(
        self,
        *,
        note_id: uuid.UUID | str,
        extraction: Extraction,
        graph_context: str,
        schema_version: int,
        note_text: str = "",
    ) -> IntegrationIntent:
        """Produce a validated IntegrationIntent for one note. Raises
        IntentParseError if the model output is unusable after the adapter's
        re-ask (a permanent failure the caller maps to PermanentJobError)."""
        result = await self._router.complete(
            "integrate.note",
            system=INTEGRATE_SYSTEM,
            user_text=build_integrate_prompt(extraction, graph_context, note_text),
            json_schema=INTENT_SCHEMA,
            max_tokens=INTEGRATE_MAX_TOKENS,
            strength=INTEGRATE_STRENGTH,
        )
        return parse_intent(
            result.parsed,
            note_id=str(note_id),
            schema_version=schema_version,
            prompt_version=INTEGRATE_PROMPT_VERSION,
            integrator_version=INTEGRATOR_VERSION,
        )
