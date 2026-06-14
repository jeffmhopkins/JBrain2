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

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the proven candidate engines rather than reinvent name/vector matching.
# Underscore-private, but same-package: graph_context is a sibling of entities.
from jbrain.analysis.entities import (
    _embedding_candidates,
    _exact_matches,
    normalize_alias,
)
from jbrain.analysis.extraction import ExtractedMention
from jbrain.embed import EmbedClient

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
DEFAULT_PER_MENTION_CAP = 3
_ALIAS_CAP = 5


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


# --- DB retrieval (B2) -------------------------------------------------------
# The integrate job runs under the all-seeing SYSTEM_CTX, so RLS does NOT scope
# these reads. Every query therefore carries an EXPLICIT `domain_code IN
# (note_domain, 'general')` filter — the firewall input must come from code, not
# the session (mirrors _embedding_candidates / _existing_facts). A general note
# never sees a restricted-domain entity or fact; the safe failure is an
# occasional duplicate-mint the merge-proposal path reconciles, never a leak.


def _fact_value(object_name: str | None, value_json_text: str | None) -> str:
    """The rendered right-hand side of a fact: the object entity's name for a
    link, else the displayable datum pulled from value_json (cast to text in SQL,
    so this is driver-independent)."""
    if object_name:
        return object_name
    if not value_json_text:
        return ""
    try:
        data = json.loads(value_json_text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return str(data)
    if "value" in data:
        unit = data.get("unit")
        return f"{data['value']} {unit}" if unit else str(data["value"])
    for key in ("place", "start"):  # homeLocation place, appointment start
        if key in data:
            return str(data[key])
    return ", ".join(f"{k}={v}" for k, v in data.items())


async def _owner_neighbor_ids(
    session: AsyncSession, owner_id: uuid.UUID, note_domain: str
) -> list[str]:
    """Owner ego-graph at depth 1: ids one active asserted relationship hop from
    the owner in either direction, domain-filtered. These are the highest-value
    resolution targets ("my wife" → the existing spouse) even without a name match.
    Inlined (not repo.ego_graph) because we already hold the session."""
    rows = (
        await session.execute(
            text(
                """
                SELECT f.object_entity_id::text AS nid FROM app.facts f
                JOIN app.entities oe ON oe.id = f.object_entity_id
                WHERE f.entity_id = :oid AND f.object_entity_id IS NOT NULL
                  AND f.status = 'active' AND f.assertion = 'asserted'
                  AND oe.status != 'merged' AND f.domain_code IN (:dom, 'general')
                UNION
                SELECT f.entity_id::text AS nid FROM app.facts f
                JOIN app.entities se ON se.id = f.entity_id
                WHERE f.object_entity_id = :oid
                  AND f.status = 'active' AND f.assertion = 'asserted'
                  AND se.status != 'merged' AND f.domain_code IN (:dom, 'general')
                """
            ),
            {"oid": str(owner_id), "dom": note_domain},
        )
    ).all()
    return [r.nid for r in rows]


async def _load_entity(
    session: AsyncSession, entity_id: str | uuid.UUID, note_domain: str
) -> CandidateEntity | None:
    """One entity as a CandidateEntity: its row, in-domain aliases, and active
    in-domain facts (newest-first; rank_and_bound re-orders by identity)."""
    ent = (
        await session.execute(
            text(
                "SELECT id::text AS id, canonical_name, kind FROM app.entities"
                " WHERE id = :id AND status != 'merged'"
            ),
            {"id": str(entity_id)},
        )
    ).first()
    if ent is None:
        return None
    aliases = tuple(
        r.alias
        for r in (
            await session.execute(
                text(
                    "SELECT alias FROM app.entity_aliases"
                    " WHERE entity_id = :id AND domain_code IN (:dom, 'general')"
                    " AND lower(alias) != lower(:name) ORDER BY alias LIMIT :lim"
                ),
                {"id": ent.id, "dom": note_domain, "name": ent.canonical_name, "lim": _ALIAS_CAP},
            )
        ).all()
    )
    rows = (
        await session.execute(
            text(
                """
                SELECT f.predicate, f.qualifier, f.kind, f.assertion, f.valid_from,
                       f.value_json::text AS value_json_text,
                       oe.canonical_name AS object_name
                FROM app.facts f
                LEFT JOIN app.entities oe
                  ON oe.id = f.object_entity_id AND oe.status != 'merged'
                WHERE f.entity_id = :id AND f.status = 'active'
                  AND f.domain_code IN (:dom, 'general')
                ORDER BY f.valid_from DESC NULLS LAST, f.id
                """
            ),
            {"id": ent.id, "dom": note_domain},
        )
    ).all()
    facts = tuple(
        FactLine(
            predicate=r.predicate,
            qualifier=r.qualifier or "",
            kind=r.kind,
            assertion=r.assertion,
            value=_fact_value(r.object_name, r.value_json_text),
            valid_from=r.valid_from,
        )
        for r in rows
    )
    return CandidateEntity(
        entity_id=ent.id, name=ent.canonical_name, kind=ent.kind, aliases=aliases, facts=facts
    )


async def build_graph_context(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    mentions: list[ExtractedMention],
    note_domain: str,
    embedder: EmbedClient | None,
    embed_model: str,
    per_mention_cap: int = DEFAULT_PER_MENTION_CAP,
    total_cap: int = DEFAULT_TOTAL_CAP,
    facts_per_entity: int = DEFAULT_FACTS_PER_ENTITY,
) -> str:
    """The `<graph_context>` block for one note: the owner plus the existing
    entities most likely to be the note's mentions — by exact/alias name match,
    vector similarity (when an embedder is configured), and owner ego-graph
    proximity — each with their in-domain active facts. Pure rendering lives in
    rank_and_bound/render_graph_context; this layer only fetches, firewall-first.

    `owner_id` is resolved by the caller (get_or_create_me) so retrieval creates
    nothing. Returns "" only if the owner row is gone (degenerate)."""
    seen: set[str] = {str(owner_id)}
    ordered_ids: list[str] = []
    for mention in mentions:
        norm = normalize_alias(mention.name)
        if not norm or norm == "me":
            continue
        picks = [str(row.id) for row in await _exact_matches(session, norm)]
        if embedder is not None and len(picks) < per_mention_cap:
            scored = await _embedding_candidates(
                session,
                mention.name,
                kind_hint=mention.kind,
                domain=note_domain,
                embedder=embedder,
                embed_model=embed_model,
            )
            picks += [str(cand.id) for cand, _sim in scored]
        for pid in picks[:per_mention_cap]:
            if pid not in seen:
                seen.add(pid)
                ordered_ids.append(pid)
    for nid in await _owner_neighbor_ids(session, owner_id, note_domain):
        if nid not in seen:
            seen.add(nid)
            ordered_ids.append(nid)

    owner = await _load_entity(session, owner_id, note_domain)
    candidates = [
        ce for cid in ordered_ids if (ce := await _load_entity(session, cid, note_domain))
    ]
    ranked = rank_and_bound(
        owner, candidates, total_cap=total_cap, facts_per_entity=facts_per_entity
    )
    return render_graph_context(ranked)
