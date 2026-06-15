"""Unit tests for the PURE integration-persistence builders (analysis/persist.py).

The run-step trace and the resolution-pin set are pure projections of the intent,
plan, and chunk text — DB-free. Their purity is the convergence guarantee the
integration upsert relies on: the same inputs always yield the same pins, so a
re-run can never fork them (the silent-flip the pins design forbids, N10).
"""

import uuid
from dataclasses import dataclass

from jbrain.analysis.arbiter import ArbiterPlan, PlannedFact
from jbrain.analysis.entities import ResolvedEntity
from jbrain.analysis.intent import (
    AttestedSpan,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
)
from jbrain.analysis.persist import build_pins, build_run_steps


@dataclass(frozen=True)
class _Chunk:
    """A minimal ChunkText (id + text) — the structural protocol persist expects."""

    id: uuid.UUID
    text: str


_NOTE = "11111111-1111-1111-1111-111111111111"
_CHUNK = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ENTITY = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _fact(predicate: str, surface: str | None) -> IntentFact:
    return IntentFact(
        entity_ref="Globex",
        predicate=predicate,
        qualifier="",
        kind="attribute",
        statement=f"Globex {predicate}",
        value_json=None,
        assertion="asserted",
        object_entity_ref=None,
        temporal=None,
        attested_span=AttestedSpan(chunk_id="", surface=surface) if surface else None,
        self_confidence=0.95,
        inferred=False,
    )


def _intent(facts: list[IntentFact], *, resolution_surface: str | None = None) -> IntegrationIntent:
    return IntegrationIntent(
        note_id=_NOTE,
        schema_version=1,
        prompt_version="p",
        integrator_version="i",
        entity_resolutions=[
            EntityResolution(
                mention_ref="Globex",
                mode="new",
                new_kind="Organization",
                new_name="Globex",
                attested_span=(
                    AttestedSpan(chunk_id="", surface=resolution_surface)
                    if resolution_surface
                    else None
                ),
            )
        ],
        facts=facts,
    )


def _plan(facts: list[IntentFact], *, commit: bool = True) -> ArbiterPlan:
    return ArbiterPlan(
        rejected=False,
        fatal_violations=(),
        facts=tuple(
            PlannedFact(fact=f, weight=0.9, status="active" if commit else "pending_review")
            for f in facts
        ),
        merge_proposals=(),
        distinct_proposals=(),
    )


_RESOLVED = {"Globex": ResolvedEntity(id=_ENTITY, subject_id=None, created=True, method="llm")}
_CHUNKS = [_Chunk(id=_CHUNK, text="Globex is in tech.")]


def test_build_run_steps_names_the_three_pipeline_stages():
    facts = [_fact("industry", "Globex")]
    steps = build_run_steps(_intent(facts), _plan(facts))
    assert [(k, n, ok) for k, n, ok in steps] == [
        ("extraction", "note.extract", True),
        ("integration", "integrate.note", True),
        ("arbiter", "plan_intent", True),
    ]


def test_build_run_steps_arbiter_not_ok_when_rejected():
    rejected = ArbiterPlan(
        rejected=True, fatal_violations=(), facts=(), merge_proposals=(), distinct_proposals=()
    )
    steps = build_run_steps(_intent([]), rejected)
    assert steps[2] == ("arbiter", "plan_intent", False)


def test_predicate_key_pin_built_for_a_committed_fact():
    facts = [_fact("industry", "Globex")]
    pins = build_pins(_intent(facts), _plan(facts), _CHUNKS, resolved=_RESOLVED)
    keys = {p.decision_kind for p in pins}
    assert "predicate_key" in keys
    pin = next(p for p in pins if p.decision_kind == "predicate_key")
    assert pin.normalized_predicate == "industry"
    assert pin.chunk_id == str(_CHUNK)
    assert pin.occurrence_index == 0  # first occurrence of "Globex"
    assert pin.entity_id is None  # predicate_key pins carry no entity (CHECK)


def test_identity_pin_built_only_when_resolution_is_attested_and_committed():
    facts = [_fact("industry", "Globex")]
    # No resolution surface -> no identity pin (can't anchor it safely).
    no_ident = build_pins(_intent(facts), _plan(facts), _CHUNKS, resolved=_RESOLVED)
    assert all(p.decision_kind != "identity" for p in no_ident)

    # With an attested surface AND a committed entity -> an identity pin appears.
    with_ident = build_pins(
        _intent(facts, resolution_surface="Globex"), _plan(facts), _CHUNKS, resolved=_RESOLVED
    )
    ident = next(p for p in with_ident if p.decision_kind == "identity")
    assert ident.entity_id == str(_ENTITY)
    assert ident.normalized_predicate is None


def test_uncommitted_entity_yields_no_identity_pin():
    facts = [_fact("industry", "Globex")]
    pins = build_pins(
        _intent(facts, resolution_surface="Globex"),
        _plan(facts),
        _CHUNKS,
        resolved={"Globex": None},  # mention did not commit to an entity
    )
    assert all(p.decision_kind != "identity" for p in pins)


def test_held_fact_is_not_pinned():
    facts = [_fact("industry", "Globex")]
    pins = build_pins(_intent(facts), _plan(facts, commit=False), _CHUNKS, resolved=_RESOLVED)
    assert all(p.decision_kind != "predicate_key" for p in pins)


def test_unanchorable_surface_is_skipped_not_pinned():
    # A paraphrased surface absent from the chunk text cannot be pinned (N10).
    facts = [_fact("industry", "Acme")]
    pins = build_pins(_intent(facts), _plan(facts), _CHUNKS, resolved=_RESOLVED)
    assert all(p.decision_kind != "predicate_key" for p in pins)


def test_builders_are_pure_and_convergent():
    # Same inputs, twice -> byte-identical pin sets (the silent-flip guard, N10).
    facts = [_fact("industry", "Globex")]
    intent, plan = _intent(facts, resolution_surface="Globex"), _plan(facts)
    first = build_pins(intent, plan, _CHUNKS, resolved=_RESOLVED)
    second = build_pins(intent, plan, _CHUNKS, resolved=_RESOLVED)
    assert first == second
