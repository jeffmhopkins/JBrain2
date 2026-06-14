"""Scenario schema, loader, and the declarative expectation checker.

Pure and dependency-light so both the pytest module and the standalone CLI use
the same matching rules — a scenario asserts the same thing however it's run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


@dataclass(frozen=True)
class Step:
    """One note: its capture metadata plus the extraction a model would emit,
    and (optionally) the Integrator intent — the agent's resolution/supersession
    judgment over that extraction."""

    body: str
    extraction: dict[str, Any]
    domain: str = "general"
    # ISO 8601 with offset — the reported_at/anchor every temporal + supersession
    # assertion turns on. Always set it explicitly; relative phrases in the
    # scripted extraction must already be resolved against it.
    created_at: str = "2026-06-10T12:00:00-06:00"
    # Re-analysis: when set, the runner re-runs the pipeline against the note
    # seeded by that earlier step (0-based index) instead of seeding a new
    # one — the step's body/domain/created_at are ignored, only its
    # extraction matters. This is how a scenario scripts "the model now reads
    # the same note differently" (re-extraction after an edit or upgrade).
    reanalyze_step: int | None = None
    # The agent's IntegrationIntent for this step. Omit it and the runner derives
    # a faithful default (name-match resolution against the live graph, every
    # surface-attested fact committed) — enough for scenarios whose point is the
    # arbiter/apply behavior, not a specific coreference call. Author it
    # explicitly when the resolution itself is under test (cross-subject holds,
    # an agent picking an existing entity by id, a proposed merge). Existing-mode
    # resolutions and merge/distinct pairs reference entities by NAME; the runner
    # resolves each to its live id at step time. See _compile_intent.
    intent: dict[str, Any] | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: list[Step]
    expect: dict[str, Any]
    description: str = ""
    # Set to a reason string when the scenario encodes behaviour a known-open
    # bug doesn't satisfy yet; the pytest run marks it xfail(strict) so it
    # flips to a hard failure — a passing reminder — the moment the fix lands.
    xfail: str | None = None
    source: str = ""


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text())
    return Scenario(
        name=raw["name"],
        description=raw.get("description", ""),
        xfail=raw.get("xfail"),
        steps=[
            Step(
                body=s["body"],
                extraction=s["extraction"],
                domain=s.get("domain", "general"),
                created_at=s.get("created_at", "2026-06-10T12:00:00-06:00"),
                reanalyze_step=s.get("reanalyze_step"),
                intent=s.get("intent"),
            )
            for s in raw["steps"]
        ],
        expect=raw["expect"],
        source=str(path.name),
    )


def load_all(directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    return [load_scenario(p) for p in sorted(directory.glob("*.json"))]


# --- Snapshot the runner produces and the checker consumes -----------------


@dataclass
class FactRow:
    entity: str
    predicate: str
    qualifier: str
    kind: str
    assertion: str
    status: str
    statement: str
    value_json: dict[str, Any] | None
    chained: bool
    pinned: bool
    domain: str


@dataclass
class ReviewRow:
    kind: str
    summary: str
    status: str
    domain: str


@dataclass
class EntityRow:
    name: str
    kind: str
    status: str


@dataclass
class Snapshot:
    facts: list[FactRow] = field(default_factory=list)
    reviews: list[ReviewRow] = field(default_factory=list)
    entities: list[EntityRow] = field(default_factory=list)


# --- Declarative matching ---------------------------------------------------


def _fact_matches(row: FactRow, spec: dict[str, Any]) -> bool:
    """A fact spec lists only the columns it cares about. `value_contains`
    matches anywhere in the rendered value_json + statement (case-insensitive)
    so a scenario need not mirror the exact value shape."""
    for key, want in spec.items():
        if key == "entity" and row.entity != want:
            return False
        if key == "predicate" and row.predicate != want:
            return False
        if key == "qualifier" and row.qualifier != want:
            return False
        if key == "kind" and row.kind != want:
            return False
        if key == "assertion" and row.assertion != want:
            return False
        if key == "status" and row.status != want:
            return False
        if key == "chained" and row.chained != want:
            return False
        if key == "pinned" and row.pinned != want:
            return False
        if key == "domain" and row.domain != want:
            return False
        if key == "value_contains":
            haystack = f"{json.dumps(row.value_json or {})} {row.statement}".casefold()
            if str(want).casefold() not in haystack:
                return False
    return True


def check(snapshot: Snapshot, expect: dict[str, Any]) -> list[str]:
    """Return a list of human-readable failures; empty means the scenario passed.

    expect keys (all optional):
      facts          - each spec must match >=1 (or `count`) facts
      absent_facts   - each spec must match 0 facts
      review_items   - each spec must match (by kind / summary_contains / count)
      entities       - each must exist with the given status/kind
    """
    failures: list[str] = []

    for spec in expect.get("facts", []):
        n = sum(1 for f in snapshot.facts if _fact_matches(f, spec))
        want = spec.get("count")
        if want is None and n == 0:
            failures.append(f"expected a fact matching {spec}, found none")
        elif want is not None and n != want:
            failures.append(f"expected {want} facts matching {spec}, found {n}")

    for spec in expect.get("absent_facts", []):
        n = sum(1 for f in snapshot.facts if _fact_matches(f, spec))
        if n:
            failures.append(f"expected NO fact matching {spec}, found {n}")

    for spec in expect.get("review_items", []):
        matched = [
            r
            for r in snapshot.reviews
            if ("kind" not in spec or r.kind == spec["kind"])
            and (
                "summary_contains" not in spec
                or str(spec["summary_contains"]).casefold() in r.summary.casefold()
            )
            and ("status" not in spec or r.status == spec["status"])
            and ("domain" not in spec or r.domain == spec["domain"])
        ]
        want = spec.get("count")
        if want is None and not matched:
            failures.append(f"expected a review item matching {spec}, found none")
        elif want is not None and len(matched) != want:
            failures.append(f"expected {want} review items matching {spec}, found {len(matched)}")

    for spec in expect.get("entities", []):
        match = next((e for e in snapshot.entities if e.name == spec["name"]), None)
        if match is None:
            failures.append(f"expected entity {spec['name']!r}, not found")
            continue
        if "status" in spec and match.status != spec["status"]:
            failures.append(
                f"entity {spec['name']!r} status {match.status!r} != {spec['status']!r}"
            )
        if "kind" in spec and match.kind != spec["kind"]:
            failures.append(f"entity {spec['name']!r} kind {match.kind!r} != {spec['kind']!r}")

    return failures
