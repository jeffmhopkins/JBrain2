"""Render the integrate.note graph context: the existing entities and their
current facts near a note's mentions, as the deterministic text block the
integrate agent reads to resolve identities, propose merges, and propose
supersessions (plan Wave-1 Track B, the `graph_context` the prompt consumes).

Two layers keep the DB out of the rendering logic: the pure ranking/rendering
here (no DB, fully unit-testable) and the DB retrieval in `build_graph_context`
(B2). The text shape matches what the integrate prompt was calibrated against —
an `Owner/author:` line naming the owner's id (so first person resolves), then
`Known entities:` blocks each carrying the entity's id (the agent echoes these
ids back in `resolutions[].entity_id` and `merge_proposals`), its kind, aliases,
and a bounded set of active facts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

# Identity-anchoring predicates surfaced FIRST within an entity, so the facts
# that actually settle "who is this / has this changed" survive the per-entity
# fact cap even for a fact-heavy entity. Order here is the surfacing order.
_IDENTITY_PREDICATES: tuple[str, ...] = (
    "name.legal",
    "name.preferred",
    "name.nickname",
    "gender",
    "spouse",
    "partner",
    "parent",
    "children",
    "sibling",
    "worksFor",
    "employer",
    "homeLocation",
    "birthDate",
)

DEFAULT_TOTAL_CAP = 15
DEFAULT_FACTS_PER_ENTITY = 8


@dataclass(frozen=True)
class FactLine:
    """One active fact on a candidate entity, already firewall-filtered (B2)."""

    predicate: str
    qualifier: str
    kind: str
    assertion: str
    # The rendered right-hand side: an object entity's name for a link, else the
    # attribute/measurement value. Empty renders as "-".
    value: str
    valid_from: datetime | None = None


@dataclass(frozen=True)
class CandidateEntity:
    """An existing entity offered to the agent as a resolution/merge target.

    `entity_id` is the stable id the agent must be able to echo verbatim, so it
    is never normalized or truncated in rendering.
    """

    entity_id: str
    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    facts: tuple[FactLine, ...] = ()


def _fact_sort_key(fact: FactLine) -> tuple[int, float]:
    # Identity predicates first (by their declared order); the rest newest-first
    # so the most recent state of a property is what the agent sees.
    try:
        rank = _IDENTITY_PREDICATES.index(fact.predicate)
    except ValueError:
        rank = len(_IDENTITY_PREDICATES)
    recency = fact.valid_from.timestamp() if fact.valid_from is not None else 0.0
    return (rank, -recency)


def _select_facts(facts: tuple[FactLine, ...], cap: int) -> tuple[FactLine, ...]:
    return tuple(sorted(facts, key=_fact_sort_key)[:cap])


def _clean(value: str) -> str:
    # The block is line-oriented: a stray newline in a name/alias/value would
    # split one fact across lines and read as a dangling fragment to the agent.
    # Collapse internal whitespace so every rendered datum stays on its own line.
    # (entity_id is never cleaned — it must round-trip verbatim.)
    return " ".join(value.split())


def rank_and_bound(
    owner: CandidateEntity | None,
    candidates: list[CandidateEntity],
    *,
    total_cap: int = DEFAULT_TOTAL_CAP,
    facts_per_entity: int = DEFAULT_FACTS_PER_ENTITY,
) -> list[CandidateEntity]:
    """De-dup by id (first occurrence wins — retrieval supplies candidates in
    priority order), pin the owner first and never drop it, cap the entity count,
    and bound each entity's facts. Pure: no DB, no I/O.
    """
    seen: set[str] = set()
    ordered: list[CandidateEntity] = []
    for cand in ([owner] if owner is not None else []) + candidates:
        if cand is None or cand.entity_id in seen:
            continue
        seen.add(cand.entity_id)
        ordered.append(cand)
    ordered = ordered[:total_cap]
    return [replace(c, facts=_select_facts(c.facts, facts_per_entity)) for c in ordered]


def _render_fact(subject: str, fact: FactLine) -> str:
    predicate = f"{fact.predicate}.{fact.qualifier}" if fact.qualifier else fact.predicate
    when = f", valid_from {fact.valid_from.date().isoformat()}" if fact.valid_from else ""
    value = _clean(fact.value) or "-"
    return f"  fact {_clean(subject)}.{predicate} -> {value} [{fact.kind}/{fact.assertion}]{when}"


def _render_entity(cand: CandidateEntity) -> list[str]:
    alias = (", alias " + ", ".join(f"'{_clean(a)}'" for a in cand.aliases)) if cand.aliases else ""
    lines = [f"- id '{cand.entity_id}' name '{_clean(cand.name)}' ({cand.kind}){alias}"]
    lines += [_render_fact(cand.name, f) for f in cand.facts]
    return lines


def render_graph_context(ranked: list[CandidateEntity]) -> str:
    """The text block for the integrate prompt's `<graph_context>`. `ranked[0]`
    is the owner (rank_and_bound pins it first). Returns "" only when there is
    no owner at all, so the prompt's "(no related entities found)" fallback fires.
    """
    if not ranked:
        return ""
    owner, others = ranked[0], ranked[1:]
    lines = [
        f"Owner/author: entity id '{owner.entity_id}' name '{_clean(owner.name)}' ({owner.kind})."
    ]
    lines += [_render_fact(owner.name, f) for f in owner.facts]
    if others:
        lines.append("Known entities:")
        for cand in others:
            lines += _render_entity(cand)
    return "\n".join(lines)
