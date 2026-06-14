"""Unit tests for the Integrator driver (Wave 1 Track B, B2).

Local: the model call is faked (FakeLlmClient), so the whole produce-an-intent
path is exercised with no provider and no DB.
"""

import json

import pytest

from jbrain.analysis.extraction import ExtractedFact, ExtractedMention, Extraction
from jbrain.analysis.integrate import INTEGRATOR_VERSION, Integrator
from jbrain.analysis.integrate_prompt import INTEGRATE_PROMPT_VERSION, build_integrate_prompt
from jbrain.analysis.intent_parse import IntentParseError
from jbrain.llm import FakeLlmClient, LlmRouter


def _extraction() -> Extraction:
    return Extraction(
        title="",
        tags=[],
        mentions=[ExtractedMention(name="Celine", kind="Person", surface_text="my wife Celine")],
        facts=[
            ExtractedFact(
                predicate="spouse",
                qualifier="",
                kind="relationship",
                statement="married to Celine",
                value_json=None,
                assertion="asserted",
                entity_ref="Me",
                object_entity_ref="Celine",
                temporal=None,
                domain="",
                confidence=1.0,
            )
        ],
        tokens=[],
    )


def _router(response: str) -> LlmRouter:
    return LlmRouter(
        {"xai": FakeLlmClient(responses=[response])},
        {"integrate.note": ("xai", "grok-4.3")},
    )


async def test_integrator_parses_model_intent():
    payload = json.dumps(
        {
            "resolutions": [
                {"mention_ref": "Me", "mode": "existing", "entity_id": "me-id"},
                {
                    "mention_ref": "Celine",
                    "mode": "new",
                    "new_kind": "Person",
                    "new_name": "Celine",
                },
            ],
            "facts": [
                {
                    "entity_ref": "Me",
                    "predicate": "spouse",
                    "kind": "relationship",
                    "assertion": "asserted",
                    "statement": "married to Celine",
                    "object_entity_ref": "Celine",
                    "self_confidence": 0.9,
                }
            ],
        }
    )
    intent = await Integrator(_router(payload)).integrate(
        note_id="n1", extraction=_extraction(), graph_context="", schema_version=1
    )
    assert len(intent.entity_resolutions) == 2
    assert len(intent.facts) == 1
    assert intent.facts[0].object_entity_ref == "Celine"
    assert intent.prompt_version == INTEGRATE_PROMPT_VERSION
    assert intent.integrator_version == INTEGRATOR_VERSION
    assert intent.note_id == "n1"


async def test_integrator_sends_system_and_extraction_and_context():
    fake = FakeLlmClient(responses=['{"resolutions": [], "facts": []}'])
    router = LlmRouter({"xai": fake}, {"integrate.note": ("xai", "grok-4.3")})
    await Integrator(router).integrate(
        note_id="n1",
        extraction=_extraction(),
        graph_context="Celine (Person): spouse -> Me",
        schema_version=1,
    )
    call = fake.calls[0]
    assert "IntegrationIntent" in call["system"]  # the system prompt went out
    assert "Celine" in call["user_text"]  # the extraction is in the user text
    assert "graph_context" in call["user_text"]  # context block present
    assert call["json_schema"] is not None  # constrained to the intent schema


async def test_integrator_raises_on_wrong_shape_output():
    # Valid JSON but not the intent shape → IntentParseError (permanent failure).
    with pytest.raises(IntentParseError):
        await Integrator(_router('{"foo": 1}')).integrate(
            note_id="n1", extraction=_extraction(), graph_context="", schema_version=1
        )


def test_build_integrate_prompt_includes_mentions_facts_and_context_fallback():
    text = build_integrate_prompt(_extraction(), "")
    assert "Celine" in text
    assert "spouse" in text
    assert "no related entities" in text  # empty-context fallback
