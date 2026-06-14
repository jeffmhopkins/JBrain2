"""The graded-corpus case schema for the real-Grok eval harness.

A case is a note + a machine-checkable `expect` block. The harness runs the note
through the real production chain (extract -> integrate -> arbiter) against real
Grok and checks the produced IntegrationIntent + ArbiterPlan against `expect`
(see assertions.check_case). Cases live as JSON under corpus/; this module is the
typed loader so the schema is one source of truth.

`expect` keys (all optional):
  resolutions       - per mention: mode / entity_id / cross_subject
  facts             - per fact: entity.predicate[.qualifier] with value / object /
                      kind / assertion / inferred / domain / disposition
  forbidden_entities- names that must NOT be minted as their own entity
  absent_facts      - facts that must NOT appear
  supersede         - supersession proposals that must be present
  max_facts         - upper bound on total facts (over-extraction guard)
A case marked `advisory: true` is reported but never fails the gate (the
"correct" answer is genuinely debatable).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).parent / "corpus"

# Sentinel for "value not asserted" so an explicit expected value of None
# (a relationship/object fact) is distinguishable from "don't check".
UNSET: Any = object()


@dataclass(frozen=True)
class ExpectResolution:
    mention: str
    mode: str | None = None
    entity_id: str | None = None
    cross_subject: bool | None = None


@dataclass(frozen=True)
class ExpectFact:
    entity: str
    predicate: str
    qualifier: str = ""
    kind: str | None = None
    value: Any = UNSET
    object: str | None = None
    assertion: str | None = None
    inferred: bool | None = None
    domain: str | None = None
    disposition: str | None = None  # "commit" | "review"


@dataclass(frozen=True)
class Expect:
    resolutions: tuple[ExpectResolution, ...] = ()
    facts: tuple[ExpectFact, ...] = ()
    forbidden_entities: tuple[str, ...] = ()
    absent_facts: tuple[dict[str, Any], ...] = ()
    supersede: tuple[dict[str, Any], ...] = ()
    max_facts: int | None = None
    max_entities: int | None = None  # non-owner resolutions (the no-duplicate gate)
    rationale: str = ""


@dataclass(frozen=True)
class SeedFact:
    """A prior fact to materialize on a seeded entity before the case runs, so
    DB-mode can assert supersession/resolution against real rows. `object` is the
    symbolic id of another seeded entity (an edge); `value` becomes value_json."""

    predicate: str
    qualifier: str = ""
    kind: str = "state"
    value: Any = None
    object: str | None = None
    assertion: str = "asserted"
    valid_from: str | None = None  # ISO date/datetime


@dataclass(frozen=True)
class SeedEntity:
    """An existing entity to materialize before a DB-mode run. `id` is a symbolic
    handle the corpus references (e.g. 'ent-acme'); the runner maps it to the real
    minted UUID. `owner=True` attaches facts/aliases to the canonical "Me" entity
    instead of minting a new one."""

    id: str
    name: str = "Me"
    kind: str = "Person"
    domain: str = "general"
    owner: bool = False
    aliases: tuple[str, ...] = ()
    facts: tuple[SeedFact, ...] = ()


@dataclass(frozen=True)
class Case:
    id: str
    note_text: str
    category: str = ""
    domain: str = "general"
    graph_context: str = ""
    advisory: bool = False
    seed: tuple[SeedEntity, ...] = ()
    expect: Expect = field(default_factory=Expect)


# --- committed-state contract for DB-mode (pure data; no session/ORM) ---------


@dataclass(frozen=True)
class CommittedFact:
    """One app.facts row written by the case's note, flattened for assertion."""

    id: str
    entity_id: str
    entity_name: str
    predicate: str
    qualifier: str
    kind: str | None
    value_json: Any
    assertion: str | None
    status: str
    domain_code: str | None
    object_entity_id: str | None = None
    object_name: str | None = None


@dataclass(frozen=True)
class SeededFactState:
    """The post-apply state of a seeded prior fact — the supersession signal."""

    entity_symbolic: str
    entity_name: str
    predicate: str
    status: str
    superseded_by: str | None
    valid_to: str | None


@dataclass(frozen=True)
class DbCommit:
    """Everything a DB-mode case exposes for assertion, read back from committed
    rows. Pure data so check_case_db is testable without Postgres or Grok."""

    owner_id: str
    note_id: str
    seeded_ids: dict[str, str]  # symbolic id -> real UUID
    facts: tuple[CommittedFact, ...]  # facts this note wrote
    entities: dict[str, str]  # entity UUID -> canonical_name (referenced by this note)
    review_fact_ids: frozenset[str]  # fact ids carrying a low_confidence_inference card
    seeded_facts: tuple[SeededFactState, ...] = ()


def _expect(raw: dict[str, Any]) -> Expect:
    return Expect(
        resolutions=tuple(ExpectResolution(**r) for r in raw.get("resolutions", [])),
        facts=tuple(
            ExpectFact(**{**f, "value": f.get("value", UNSET)}) for f in raw.get("facts", [])
        ),
        forbidden_entities=tuple(raw.get("forbidden_entities", [])),
        absent_facts=tuple(raw.get("absent_facts", [])),
        supersede=tuple(raw.get("supersede", [])),
        max_facts=raw.get("max_facts"),
        max_entities=raw.get("max_entities"),
        rationale=raw.get("rationale", ""),
    )


def _seed(raw: list[dict[str, Any]]) -> tuple[SeedEntity, ...]:
    return tuple(
        SeedEntity(
            id=e["id"],
            name=e.get("name", "Me"),
            kind=e.get("kind", "Person"),
            domain=e.get("domain", "general"),
            owner=e.get("owner", False),
            aliases=tuple(e.get("aliases", [])),
            facts=tuple(SeedFact(**f) for f in e.get("facts", [])),
        )
        for e in raw
    )


def case_from_dict(raw: dict[str, Any]) -> Case:
    return Case(
        id=raw["id"],
        note_text=raw["note_text"],
        category=raw.get("category", ""),
        domain=raw.get("domain", "general"),
        graph_context=raw.get("graph_context", ""),
        advisory=raw.get("advisory", False),
        seed=_seed(raw.get("seed", [])),
        expect=_expect(raw.get("expect", {})),
    )


def load_corpus(directory: Path = CORPUS_DIR) -> list[Case]:
    """Every case across all corpus/*.json files, sorted by id (stable order)."""
    cases: list[Case] = []
    for path in sorted(directory.glob("*.json")):
        for raw in json.loads(path.read_text()):
            cases.append(case_from_dict(raw))
    return sorted(cases, key=lambda c: c.id)
