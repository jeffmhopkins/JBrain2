"""The EmrImporter — lower typed parser candidates into IntegrationIntents
(docs/plans/EMR_IMPORT_PLAN.md §6.6).

Structured EMR data is deterministic, so the LLM Extractor and Integrator are
bypassed: this builder emits the SAME `IntegrationIntent` object the LLM
Integrator would, and the shipped deterministic core (`plan_intent` ->
`apply_intent`) validates, weighs, firewalls, supersedes, and commits it. The
importer populates the new `IntentFact.fhir_status` (§3.5) and runs the Layer-2
location firewall guard (§3.6) so a stray whereabouts fact can never reach the
graph.

Grouping: one intent per EPISODE (a facility-transfer's segments are linked by
`partOfEncounter`, so they must share an intent for the edge's `object_entity_ref`
to resolve intra-intent); a standalone encounter is its own episode. Every fact
cites the page-chunk it came from via the injected `chunk_for_anchor` resolver.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jbrain.analysis.intent import (
    AttestedSpan,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    IntentTemporal,
)
from jbrain.ingest.emr.candidates import CandidateEncounter, CandidateObservation, ParseResult
from jbrain.ingest.emr.firewall import FIREWALL_REVIEW_SUBKIND, is_location_locked

SCHEMA_VERSION = 1
INTEGRATOR_VERSION = "emr-importer-1"
PROMPT_VERSION = "deterministic"

# Entity kinds are the schema.org type NAMES so `predicate_for_kind` resolves them
# and the project_emr kind-filter matches on lower(kind) in {observation, encounter}.
KIND_OBSERVATION = "Observation"
KIND_ENCOUNTER = "Encounter"
KIND_PERSON = "Person"
KIND_ORGANIZATION = "Organization"
KIND_CONDITION = "MedicalCondition"

ChunkResolver = Callable[[str], str]  # "page N" -> chunk_id


@dataclass(frozen=True)
class FirewallCatch:
    """A fact held out of the graph by the Layer-2 guard (§3.6) — never committed."""

    entity_kind: str
    predicate: str
    anchor: str
    subkind: str = FIREWALL_REVIEW_SUBKIND


class _IntentBuilder:
    def __init__(self, note_id: str, chunk_for_anchor: ChunkResolver) -> None:
        self.note_id = note_id
        self.chunk_for = chunk_for_anchor
        self._resolutions: dict[str, EntityResolution] = {}
        self.facts: list[IntentFact] = []
        self.catches: list[FirewallCatch] = []

    def entity(self, ref: str, kind: str, name: str) -> str:
        if ref not in self._resolutions:
            self._resolutions[ref] = EntityResolution(
                mention_ref=ref, mode="new", new_kind=kind, new_name=name
            )
        return ref

    def fact(
        self,
        *,
        entity_ref: str,
        entity_kind: str,
        predicate: str,
        qualifier: str,
        kind: str,
        statement: str,
        anchor: str,
        value_json: dict | None = None,
        object_entity_ref: str | None = None,
        temporal: IntentTemporal | None = None,
        fhir_status: str | None = None,
    ) -> None:
        # Layer 2: a location-lock predicate on a health EMR entity is held out and
        # carded — never committed. The parsers never emit one; this is the
        # belt-and-suspenders that makes stripping not a single point of failure.
        if is_location_locked(predicate, entity_kind):
            self.catches.append(FirewallCatch(entity_kind=entity_kind, predicate=predicate,
                                              anchor=anchor))
            return
        self.facts.append(
            IntentFact(
                entity_ref=entity_ref,
                predicate=predicate,
                qualifier=qualifier,
                kind=kind,
                statement=statement,
                value_json=value_json,
                assertion="asserted",
                object_entity_ref=object_entity_ref,
                temporal=temporal,
                attested_span=AttestedSpan(chunk_id=self.chunk_for(anchor), surface=statement),
                self_confidence=1.0,
                inferred=False,
                fhir_status=fhir_status,
            )
        )

    def build(self) -> IntegrationIntent:
        return IntegrationIntent(
            note_id=self.note_id,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            integrator_version=INTEGRATOR_VERSION,
            entity_resolutions=list(self._resolutions.values()),
            facts=self.facts,
        )


def _analyte_ref(obs: CandidateObservation) -> str:
    return f"obs:{obs.analyte.code}"


def _add_observation(b: _IntentBuilder, obs: CandidateObservation) -> str:
    """Add one draw's fan of facts; return the analyte entity ref."""
    ref = b.entity(_analyte_ref(obs), KIND_OBSERVATION, obs.analyte.name)
    q = obs.qualifier
    disp = f"{obs.analyte.name} {obs.value_num if obs.value_num is not None else obs.value_text}"

    if obs.value_num is not None:
        b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="value", qualifier=q,
               kind="measurement", statement=f"{disp} {obs.unit or ''}".strip(),
               anchor=obs.source_anchor, fhir_status=obs.fhir_status,
               value_json={"value": obs.value_num, "unit": obs.unit})
    if obs.ref_low is not None and obs.ref_high is not None:
        b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="referenceRange",
               qualifier=q,
               kind="attribute", statement=f"reference {obs.ref_low}-{obs.ref_high}",
               anchor=obs.source_anchor,
               value_json={"low": {"value": obs.ref_low, "unit": obs.unit},
                           "high": {"value": obs.ref_high, "unit": obs.unit}})
    if obs.interpretation is not None:
        b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="interpretation",
               qualifier=q,
               kind="attribute", statement=obs.interpretation, anchor=obs.source_anchor,
               value_json={"value": obs.interpretation})
    if obs.specimen_id:
        b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="specimen", qualifier=q,
               kind="attribute", statement=obs.specimen_id, anchor=obs.source_anchor,
               value_json={"value": obs.specimen_id})
    # The clinically-relevant instant as a first-class fact with a deterministic
    # point token (§3.2): the arbiter mints the temporal_token from this IntentTemporal.
    b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="effectiveDate", qualifier=q,
           kind="event", statement=f"collected {obs.collected_at.isoformat()}",
           anchor=obs.source_anchor,
           temporal=IntentTemporal(phrase=obs.collected_at.isoformat(),
                                   resolved_start=obs.collected_at, resolved_end=None,
                                   precision=obs.precision))
    if obs.performing_lab:
        lab_ref = b.entity(f"org:{obs.performing_lab}", KIND_ORGANIZATION, obs.performing_lab)
        b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="performer", qualifier=q,
               kind="relationship", statement=f"performed by {obs.performing_lab}",
               anchor=obs.source_anchor, object_entity_ref=lab_ref)

    # Analyte-constant facts (one per analyte, NOT per draw) — a constant qualifier.
    if obs.analyte.loinc:
        b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="identifier",
               qualifier="loinc", kind="attribute", statement=f"LOINC {obs.analyte.loinc}",
               anchor=obs.source_anchor, value_json={"value": obs.analyte.loinc})
    b.fact(entity_ref=ref, entity_kind=KIND_OBSERVATION, predicate="category", qualifier="",
           kind="attribute", statement="laboratory", anchor=obs.source_anchor,
           value_json={"value": "laboratory"})
    return ref


def _add_encounter(
    b: _IntentBuilder, enc: CandidateEncounter, enc_ref_by_key: dict[str, str]
) -> None:
    enc_ref = enc_ref_by_key[enc.key]
    label = f"{enc.encounter_class} — {enc.facility or 'unknown'}"
    b.entity(enc_ref, KIND_ENCOUNTER, label)
    anchor = enc.source_anchor
    period_temporal = IntentTemporal(
        phrase=None, resolved_start=enc.admitted_at, resolved_end=enc.discharged_at, precision="day"
    )
    b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="period", qualifier="",
           kind="state", statement=f"{enc.encounter_class} stay", anchor=anchor,
           value_json={"value": enc.encounter_class}, temporal=period_temporal)
    b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="class", qualifier="",
           kind="attribute", statement=enc.encounter_class, anchor=anchor,
           value_json={"value": enc.encounter_class})
    if enc.care_unit:
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="careUnit", qualifier="",
               kind="attribute", statement=enc.care_unit, anchor=anchor,
               value_json={"value": enc.care_unit})
    if enc.disposition:
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="disposition",
               qualifier="",
               kind="attribute", statement=enc.disposition, anchor=anchor,
               value_json={"value": enc.disposition})
    if enc.facility:
        fac_ref = b.entity(f"org:{enc.facility}", KIND_ORGANIZATION, enc.facility)
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="serviceProvider",
               qualifier="", kind="relationship", statement=f"at {enc.facility}", anchor=anchor,
               object_entity_ref=fac_ref)
    for prov in enc.providers:
        p_ref = b.entity(f"person:{prov.name}", KIND_PERSON, prov.name)
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="attender",
               qualifier=prov.role, kind="relationship",
               statement=f"{prov.role}: {prov.name}", anchor=anchor, object_entity_ref=p_ref)
    for dx in enc.diagnoses:
        c_ref = b.entity(f"cond:{dx.icd10}", KIND_CONDITION, dx.label)
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="encounterDiagnosis",
               qualifier=dx.icd10, kind="relationship", statement=f"{dx.icd10} {dx.label}",
               anchor=anchor, object_entity_ref=c_ref)
    for tx in enc.transfusions:
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="transfusion",
               qualifier=tx.order_id, kind="event",
               statement=f"{tx.product} x{tx.units}: {tx.indication}", anchor=anchor,
               value_json={"product": tx.product, "units": tx.units, "indication": tx.indication})
    if enc.part_of_key and enc.part_of_key in enc_ref_by_key:
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="partOfEncounter",
               qualifier="", kind="relationship", statement="part of the same hospitalization",
               anchor=anchor, object_entity_ref=enc_ref_by_key[enc.part_of_key])
    # The lab<->encounter join: one hasObservation edge per draw-in-encounter.
    for obs in enc.observations:
        analyte_ref = _add_observation(b, obs)
        b.fact(entity_ref=enc_ref, entity_kind=KIND_ENCOUNTER, predicate="hasObservation",
               qualifier=obs.qualifier, kind="relationship",
               statement=f"drew {obs.analyte.name}", anchor=obs.source_anchor,
               object_entity_ref=analyte_ref)


def _episodes(encounters: list[CandidateEncounter]) -> list[list[CandidateEncounter]]:
    """Connected components over `part_of_key` — a facility-transfer's segments
    share one episode (hence one intent)."""
    by_key = {e.key: e for e in encounters}
    parent: dict[str, str] = {e.key: e.key for e in encounters}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for e in encounters:
        if e.part_of_key and e.part_of_key in by_key:
            parent[find(e.key)] = find(e.part_of_key)
    groups: dict[str, list[CandidateEncounter]] = {}
    for e in encounters:
        groups.setdefault(find(e.key), []).append(e)
    return list(groups.values())


def lower_parse_result(
    result: ParseResult, note_id: str, chunk_for_anchor: ChunkResolver
) -> tuple[list[IntegrationIntent], list[FirewallCatch]]:
    """Lower a parse result into one IntegrationIntent per episode (+ one for any
    orphan portal observations). Returns the intents and any firewall catches."""
    intents: list[IntegrationIntent] = []
    catches: list[FirewallCatch] = []

    for episode in _episodes(result.encounters):
        b = _IntentBuilder(note_id, chunk_for_anchor)
        enc_ref_by_key = {e.key: f"enc:{e.key}" for e in episode}
        for enc in episode:
            _add_encounter(b, enc, enc_ref_by_key)
        intents.append(b.build())
        catches.extend(b.catches)

    if result.orphan_observations:
        b = _IntentBuilder(note_id, chunk_for_anchor)
        for obs in result.orphan_observations:
            _add_observation(b, obs)
        intents.append(b.build())
        catches.extend(b.catches)

    return intents, catches
