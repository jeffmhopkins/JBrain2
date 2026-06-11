"""Entity resolution (docs/ANALYSIS.md "Alias resolution & separation").

Layered, cheapest first:

  layer 1   exact alias / canonical-name match, case- and diacritic-
            insensitive, plus the first-person -> "Me" rule.
  layer 2b  relationship hop for reference-shaped mentions ("Summer's rat",
            "my dentist", "the rat") — deterministic graph lookups, no LLM.
            Role references resolve through the relationship fact valid at
            the note's time (docs/ANALYSIS.md "Role references"), never
            static aliases.
  layer 2   embedding similarity vs entity name+aliases(+summary), only when
            an embed client is wired in.
  layer 3   batched cheap-LLM disambiguation (task entity.disambiguate) over
            candidates the earlier layers couldn't decide. The pipeline owns
            the adapter call; this module owns the prompt and parsing.

The gray zone returns AmbiguousEntity -> ambiguous_mention review item. A
wrong silent link is the one outcome no layer may produce, so every uncertain
path degrades to review, and the fuzzy layers never cross the domain firewall
(the pipeline runs owner-scoped, so the explicit domain predicates here ARE
the firewall for resolution).
"""

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.embed import EmbedClient, vector_literal
from jbrain.models.analysis import Entity, EntityAlias
from jbrain.models.core import Subject

# A resolution RULE, not aliases: pronouns are never stored in entity_aliases
# (docs/ANALYSIS.md "First person and the owner"). Extraction is instructed to
# emit "Me"; the raw pronouns are tolerated for robustness.
FIRST_PERSON = frozenset({"me", "i", "my", "myself", "mine"})


def normalize_alias(name: str) -> str:
    """Lowercase, diacritic-stripped, whitespace-collapsed alias key — the
    app-side normalization entity_aliases.alias_norm relies on (0006)."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


@dataclass(frozen=True)
class ResolvedEntity:
    id: uuid.UUID
    subject_id: uuid.UUID | None
    created: bool = False
    # Mention provenance. "relationship" (layer 2b) is wider than the 0006
    # link_method CHECK; the pipeline maps it when writing mentions.
    method: str = "exact_alias"
    confidence: float = 1.0


@dataclass(frozen=True)
class AmbiguousEntity:
    """Several candidates (or a role reference with no valid fact): no link,
    review-inbox item. candidate_ids may be empty — the card still surfaces
    the unresolved mention."""

    candidate_ids: list[uuid.UUID]


@dataclass(frozen=True)
class EntityCandidate:
    """What layer 3 needs to show the disambiguator about one candidate."""

    id: uuid.UUID
    subject_id: uuid.UUID | None
    name: str
    kind: str
    summary: str | None = None


@dataclass(frozen=True)
class NeedsDisambiguation:
    """Layers 1-2 narrowed to candidates but could not decide: layer 3's
    input. The pipeline batches all of these into ONE entity.disambiguate
    call per note, or files reviews when that task is not routed."""

    candidates: list[EntityCandidate]


# --- reference-shape parsing (layer 2b) -------------------------------------


@dataclass(frozen=True)
class Reference:
    shape: str  # "possessive" | "role" | "definite"
    owner: str | None  # possessive owner surface; None for role/definite
    noun: str


_POSSESSIVE_RE = re.compile(r"^(?P<owner>.+?)['’]s\s+(?P<noun>.+)$")


def parse_reference(name: str) -> Reference | None:
    """Classify a reference-shaped mention name.

    Pragmatic by design: extraction sees one note at a time, so it emits
    surface descriptions ("the rat", "my dentist"); the resolver is the only
    component that sees the whole graph, so it re-reads the shape here.
    Possessive-looking proper names ("Bob's Burgers") are tolerated — the hop
    only links when the apostrophe owner actually resolves and the graph
    holds a matching edge; otherwise the name falls through unchanged.
    """
    stripped = " ".join(name.split())
    low = stripped.casefold()
    if low.startswith("my ") and len(stripped) > 3:
        return Reference(shape="role", owner=None, noun=stripped[3:])
    if low.startswith("the ") and len(stripped) > 4:
        return Reference(shape="definite", owner=None, noun=stripped[4:])
    match = _POSSESSIVE_RE.match(stripped)
    if match:
        return Reference(shape="possessive", owner=match.group("owner"), noun=match.group("noun"))
    return None


def predicate_denotes_role(predicate: str, noun: str) -> bool:
    """Does a relationship predicate name this role ("dentist_of" ~ "dentist")?

    Token overlap between the noun and the snake/camelCase-split predicate.
    Crude on purpose: predicates follow schema.org-ish naming, so when the
    relationship IS the role, the role word appears in the predicate; a miss
    only costs a review card, never a wrong link.
    """
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", predicate)
    tokens = set(re.findall(r"[a-z]+", snake.casefold()))
    return bool(tokens & set(re.findall(r"[a-z]+", noun.casefold())))


def _word_regex(noun: str) -> str:
    # \m/\M are Postgres word boundaries; re.escape neutralizes regex
    # metacharacters so a model-emitted noun cannot break the query.
    return r"\m" + re.escape(noun) + r"\M"


# Relationship facts a hop may traverse: active asserted edges whose validity
# interval covers the note's time — a provider/ownership change can't
# misattribute later notes (docs/ANALYSIS.md "Role references").
_VALID_EDGE = """
    f.kind = 'relationship' AND f.status = 'active' AND f.assertion = 'asserted'
    AND (f.valid_from IS NULL OR f.valid_from <= :at)
    AND (f.valid_to IS NULL OR f.valid_to > :at)
    AND f.domain_code IN (:dom, 'general')
"""

# An entity "matches a noun" when (a) its kind is the noun or its bare plural
# — kind values are model-coined free text ("rat" vs "rats"), so strict
# equality is brittle; (b) the noun appears in one of its aliases; (c) an
# attribute/state fact ON IT mentions the noun ("Summer's rat is named Ricky"
# / {"species": "rat"}); or (d) an asserted relationship edge pointing AT it
# mentions the noun — live extractions often put the only species evidence in
# the introducing edge's statement ("Summer owns a rat named Ricky") and emit
# no facts on the animal itself. Edges FROM the entity stay excluded (their
# statements routinely name OTHER entities), and (d) is assertion-gated so a
# negated "does not own a rat" cannot read as rat-ness.
_NOUN_MATCH = """
    (lower(e.kind) IN (lower(:noun), lower(:nplural)) OR EXISTS (
        SELECT 1 FROM app.entity_aliases al
        WHERE al.entity_id = e.id AND al.alias_norm ~* :word
    ) OR EXISTS (
        SELECT 1 FROM app.facts d
        WHERE d.entity_id = e.id AND d.status = 'active'
          AND d.kind IN ('attribute', 'state')
          AND d.domain_code IN (:dom, 'general')
          AND (d.statement ~* :word OR d.value_json::text ~* :word)
    ) OR EXISTS (
        SELECT 1 FROM app.facts d
        WHERE d.object_entity_id = e.id AND d.status = 'active'
          AND d.kind = 'relationship' AND d.assertion = 'asserted'
          AND d.domain_code IN (:dom, 'general')
          AND (d.statement ~* :word OR d.value_json::text ~* :word)
    ))
"""

# Kind values models actually coin for household animals; used only to relax
# the definite-reference kind-hint filter inside this one vocabulary.
_GENERIC_CREATURE_KINDS = frozenset({"pet", "animal"})


def kind_hint_compatible(hint: str, kind: str, noun: str) -> bool:
    """Is an entity of `kind` a plausible referent for a definite mention
    carrying `hint`? Both values are model-coined free text: live extractions
    call the same rat "pet", "animal", or the species depending on the note,
    so demanding equality inside that creature vocabulary hides the one real
    candidate. Outside it, equality still rules — the hint is what keeps
    "the bank" (Organization) from matching a river bank (Place)."""
    if hint in ("", "Thing"):
        return True
    h, k = hint.casefold(), kind.casefold()
    if h == k:
        return True
    creature = _GENERIC_CREATURE_KINDS | {noun.casefold(), noun.casefold() + "s"}
    return h in creature and k in creature


async def _role_candidates(
    session: AsyncSession, owner_id: uuid.UUID, noun: str, *, at: datetime, domain: str
) -> list[EntityCandidate]:
    """Entities holding a role-shaped relationship to `owner` valid at the
    note's time: 'my dentist' -> entity of the dentist_of fact whose object
    is Me."""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT DISTINCT e.id, e.subject_id, e.canonical_name, e.kind,
                       e.summary, f.predicate
                FROM app.facts f JOIN app.entities e ON e.id = f.entity_id
                WHERE f.object_entity_id = :owner AND {_VALID_EDGE}
                  AND e.status != 'merged' AND e.domain_code IN (:dom, 'general')
                """
            ),
            {"owner": str(owner_id), "at": at, "dom": domain},
        )
    ).all()
    matched = {
        r.id: EntityCandidate(
            id=r.id, subject_id=r.subject_id, name=r.canonical_name, kind=r.kind, summary=r.summary
        )
        for r in rows
        if predicate_denotes_role(r.predicate, noun)
    }
    return list(matched.values())


async def _owned_candidates(
    session: AsyncSession, owner_id: uuid.UUID, noun: str, *, at: datetime, domain: str
) -> list[EntityCandidate]:
    """Objects of any active relationship edge FROM `owner`, valid at the
    note's time, that match the noun: "Summer's rat" -> the rat-shaped thing
    Summer has an edge to. The edge predicate is deliberately unconstrained
    (owns / pet / adopted all read as possession); the noun filter is what
    keeps the hop precise."""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT DISTINCT e.id, e.subject_id, e.canonical_name, e.kind, e.summary
                FROM app.facts f JOIN app.entities e ON e.id = f.object_entity_id
                WHERE f.entity_id = :owner AND {_VALID_EDGE}
                  AND e.status != 'merged' AND e.domain_code IN (:dom, 'general')
                  AND {_NOUN_MATCH}
                """
            ),
            {
                "owner": str(owner_id),
                "at": at,
                "dom": domain,
                "noun": noun,
                "nplural": noun + "s",
                "word": _word_regex(noun),
            },
        )
    ).all()
    return [
        EntityCandidate(
            id=r.id, subject_id=r.subject_id, name=r.canonical_name, kind=r.kind, summary=r.summary
        )
        for r in rows
    ]


async def _definite_candidates(
    session: AsyncSession, noun: str, *, kind_hint: str, domain: str
) -> list[EntityCandidate]:
    """Every entity a bare definite ("the rat") could denote. Auto-link is
    only safe when the graph knows exactly ONE such thing; the caller treats
    2+ as ambiguous. The mention's kind hint narrows the field when it is
    more specific than the generic Thing — applied in Python because the
    free-text tolerance (kind_hint_compatible) reads better here than SQL."""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT e.id, e.subject_id, e.canonical_name, e.kind, e.summary
                FROM app.entities e
                WHERE e.status != 'merged' AND e.domain_code IN (:dom, 'general')
                  AND lower(e.canonical_name) != 'me'
                  AND {_NOUN_MATCH}
                """
            ),
            {"dom": domain, "noun": noun, "nplural": noun + "s", "word": _word_regex(noun)},
        )
    ).all()
    return [
        EntityCandidate(
            id=r.id, subject_id=r.subject_id, name=r.canonical_name, kind=r.kind, summary=r.summary
        )
        for r in rows
        if kind_hint_compatible(kind_hint, r.kind, noun)
    ]


def _hop_outcome(
    candidates: list[EntityCandidate],
) -> ResolvedEntity | NeedsDisambiguation | None:
    if len(candidates) == 1:
        only = candidates[0]
        # Deterministic but inferred: below alias confidence, above fuzzy.
        return ResolvedEntity(
            id=only.id, subject_id=only.subject_id, method="relationship", confidence=0.9
        )
    if candidates:
        return NeedsDisambiguation(candidates=candidates)
    return None


async def _relationship_hop(
    session: AsyncSession, ref: Reference, *, kind_hint: str, domain: str, at: datetime
) -> ResolvedEntity | AmbiguousEntity | NeedsDisambiguation | None:
    """Layer 2b. None means "no graph signal": the name falls through to the
    remaining layers (and ultimately provisional creation), preserving the
    old behavior for possessive-looking proper names and novel definites."""
    if ref.shape == "role":
        me = await _find_me(session)
        candidates = (
            await _role_candidates(session, me.id, ref.noun, at=at, domain=domain) if me else []
        )
        outcome = _hop_outcome(candidates)
        if outcome is not None:
            return outcome
        # Spec: a role reference with no relationship fact valid at the
        # note's time goes to the review inbox. Minting a "my dentist"
        # entity would permanently fragment the provider's history.
        return AmbiguousEntity(candidate_ids=[])
    if ref.shape == "possessive":
        assert ref.owner is not None
        owners = await _exact_matches(session, normalize_alias(ref.owner))
        if len(owners) != 1:
            # Unknown or ambiguous owner: not a hop we can trust.
            return None
        owner_id = owners[0].id
        # Both graph shapes: "Summer's dentist" (role edge whose object is
        # Summer) and "Summer's rat" (possession edge from Summer).
        candidates = await _role_candidates(session, owner_id, ref.noun, at=at, domain=domain)
        seen = {c.id for c in candidates}
        candidates += [
            c
            for c in await _owned_candidates(session, owner_id, ref.noun, at=at, domain=domain)
            if c.id not in seen
        ]
        return _hop_outcome(candidates)
    candidates = await _definite_candidates(session, ref.noun, kind_hint=kind_hint, domain=domain)
    return _hop_outcome(candidates)


# --- layer 2: embedding similarity -------------------------------------------

# Cosine-similarity bands. One candidate at/above STRONG with no other
# candidate in range auto-links; anything in [WEAK, STRONG) — or several
# candidates — is layer 3's call. Below WEAK a neighbour is noise, not a
# candidate. Bands chosen for short name-vs-name text on bge-small; they err
# toward review, never toward a wrong link.
_EMBED_STRONG = 0.90
_EMBED_WEAK = 0.78
_EMBED_TOPK = 5
# Provisional entities usually lack summary_embedding (the nightly hygiene
# pass that writes summaries doesn't exist yet), so missing vectors are
# backfilled here from canonical_name + aliases — a one-time cost per entity
# that makes layer 2 useful from day one. Bounded per call, and narrowed to
# the mention's kind, so a resolution can't trigger a corpus-wide embed.
_EMBED_BACKFILL = 50


def _kind_filter(kind_hint: str) -> str:
    # "Thing" is extraction's catch-all, not a real constraint.
    return "" if kind_hint in ("", "Thing") else kind_hint.casefold()


async def _embedding_candidates(
    session: AsyncSession,
    name: str,
    *,
    kind_hint: str,
    domain: str,
    embedder: EmbedClient,
    embed_model: str,
) -> list[tuple[EntityCandidate, float]]:
    """Layer 2: nearest entities by name+aliases(+summary) vector, WEAK-banded
    and strongest first. Same-domain-or-general only — the embedding space
    knows nothing about the firewall, so the SQL must."""
    hint = _kind_filter(kind_hint)
    missing = (
        await session.execute(
            text(
                """
                SELECT e.id, e.canonical_name, e.summary,
                       coalesce(string_agg(a.alias, ', '), '') AS aliases
                FROM app.entities e
                LEFT JOIN app.entity_aliases a ON a.entity_id = e.id
                WHERE e.summary_embedding IS NULL AND e.status != 'merged'
                  AND e.domain_code IN (:dom, 'general')
                  AND (:hint = '' OR lower(e.kind) = :hint)
                GROUP BY e.id, e.canonical_name, e.summary
                LIMIT :lim
                """
            ),
            {"dom": domain, "hint": hint, "lim": _EMBED_BACKFILL},
        )
    ).all()
    if missing:
        texts = [
            "; ".join(part for part in (r.canonical_name, r.aliases, r.summary) if part)
            for r in missing
        ]
        vectors = await embedder.embed(texts)
        await session.execute(
            text(
                "UPDATE app.entities"
                " SET summary_embedding = cast(:emb AS vector), embedding_model = :model"
                " WHERE id = :id AND summary_embedding IS NULL"
            ),
            [
                {"id": str(r.id), "emb": vector_literal(vec), "model": embed_model}
                for r, vec in zip(missing, vectors, strict=True)
            ],
        )

    [target] = await embedder.embed([name])
    rows = (
        await session.execute(
            text(
                """
                SELECT e.id, e.subject_id, e.canonical_name, e.kind, e.summary,
                       1 - (e.summary_embedding <=> cast(:v AS vector)) AS sim
                FROM app.entities e
                WHERE e.summary_embedding IS NOT NULL AND e.status != 'merged'
                  AND e.domain_code IN (:dom, 'general')
                  AND (:hint = '' OR lower(e.kind) = :hint)
                  AND lower(e.canonical_name) != 'me'
                ORDER BY e.summary_embedding <=> cast(:v AS vector)
                LIMIT :k
                """
            ),
            {"v": vector_literal(target), "dom": domain, "hint": hint, "k": _EMBED_TOPK},
        )
    ).all()
    return [
        (
            EntityCandidate(
                id=r.id,
                subject_id=r.subject_id,
                name=r.canonical_name,
                kind=r.kind,
                summary=r.summary,
            ),
            float(r.sim),
        )
        for r in rows
        if r.sim >= _EMBED_WEAK
    ]


# --- layer 3: batched LLM disambiguation (prompt + parsing only) -------------

# The adapter call itself lives in the pipeline (it owns the router); these
# helpers keep the contract testable without a session or a router.
DISAMBIGUATE_TASK = "entity.disambiguate"
DISAMBIGUATE_MAX_TOKENS = 1024

DISAMBIGUATE_SYSTEM = (
    "You resolve mention strings from a personal note to entities already in a"
    " knowledge graph. For each mention, decide which candidate entity it"
    " denotes using the note context, or null if it is genuinely none of them."
    ' Reply with only JSON: {"choices": [{"name": "<mention name>",'
    ' "entity_id": "<candidate id>" | null}]}. Use each candidate id verbatim;'
    " never invent ids. When unsure between candidates, prefer null."
)

DISAMBIGUATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "choices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "entity_id": {"type": ["string", "null"]},
                },
                "required": ["name", "entity_id"],
            },
        }
    },
    "required": ["choices"],
}


def build_disambiguation_prompt(items: list[dict[str, Any]]) -> str:
    """One batched payload for every undecided mention in the note —
    entity.disambiguate is conditional and batched, never per-mention
    (docs/ANALYSIS.md "Model routing & cost")."""
    return json.dumps({"mentions": items}, ensure_ascii=False)


def parse_disambiguation(parsed: Any) -> dict[str, str | None]:
    """Mention name -> chosen candidate id (None = a genuinely new entity).

    Tolerant: malformed payloads or items yield no entry, and the caller
    routes every unanswered mention to the review inbox — degraded output is
    never allowed to become a guessed link.
    """
    if not isinstance(parsed, dict) or not isinstance(parsed.get("choices"), list):
        return {}
    out: dict[str, str | None] = {}
    for choice in parsed["choices"]:
        if isinstance(choice, dict) and isinstance(choice.get("name"), str):
            entity_id = choice.get("entity_id")
            out[choice["name"]] = entity_id if isinstance(entity_id, str) else None
    return out


# --- layer 1 + entry point ---------------------------------------------------


async def _exact_matches(session: AsyncSession, norm: str) -> list:
    return list(
        (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT e.id, e.subject_id
                    FROM app.entities e
                    LEFT JOIN app.entity_aliases a ON a.entity_id = e.id
                    WHERE e.status != 'merged'
                      AND (lower(e.canonical_name) = :norm OR a.alias_norm = :norm)
                    """
                ),
                {"norm": norm},
            )
        ).all()
    )


async def _find_me(session: AsyncSession):
    return (
        await session.execute(
            text(
                "SELECT id, subject_id FROM app.entities"
                " WHERE subject_id IS NOT NULL AND lower(canonical_name) = 'me'"
                " AND status != 'merged' LIMIT 1"
            )
        )
    ).first()


async def get_or_create_me(session: AsyncSession) -> ResolvedEntity:
    """The canonical "Me" entity, hard-linked to the owner's subject row —
    the implicit center of the graph, created exactly once."""
    row = await _find_me(session)
    if row is not None:
        return ResolvedEntity(id=row.id, subject_id=row.subject_id)
    # Explicit ids: the ORM column default fires at flush, too late for the
    # foreign keys built here.
    subject = Subject(id=uuid.uuid4(), display_name="Me", kind="person")
    session.add(subject)
    # No ORM relationship ties subjects to entities, so the unit of work
    # cannot order these inserts itself: flush the subject first.
    await session.flush()
    entity = Entity(
        id=uuid.uuid4(),
        kind="Person",
        canonical_name="Me",
        # The owner is not provisional: this entity is definitionally real.
        status="confirmed",
        subject_id=subject.id,
        domain_code="general",
    )
    session.add(entity)
    session.add(
        EntityAlias(
            id=uuid.uuid4(),
            entity_id=entity.id,
            alias="Me",
            alias_norm="me",
            domain_code="general",
        )
    )
    await session.flush()
    return ResolvedEntity(id=entity.id, subject_id=subject.id, created=True)


async def create_provisional(
    session: AsyncSession, name: str, *, kind_hint: str, domain: str
) -> ResolvedEntity:
    """Provisional entity, confirmed implicitly later. It inherits the
    creating note's domain (docs/ANALYSIS.md "Domain placement")."""
    entity = Entity(
        id=uuid.uuid4(),
        kind=kind_hint,
        canonical_name=name,
        status="provisional",
        domain_code=domain,
    )
    session.add(entity)
    session.add(
        EntityAlias(
            id=uuid.uuid4(),
            entity_id=entity.id,
            alias=name,
            alias_norm=normalize_alias(name),
            domain_code=domain,
        )
    )
    await session.flush()
    return ResolvedEntity(id=entity.id, subject_id=None, created=True)


async def resolve_entity(
    session: AsyncSession,
    name: str,
    *,
    kind_hint: str,
    domain: str,
    note_time: datetime | None = None,
    surface: str | None = None,
    embedder: EmbedClient | None = None,
    embed_model: str = "",
) -> ResolvedEntity | AmbiguousEntity | NeedsDisambiguation:
    """Layered resolution; creates a provisional entity when nothing matches.

    Returns AmbiguousEntity (no link) for the review inbox, or
    NeedsDisambiguation when candidates exist but only layer 3 can decide.
    note_time gates the relationship hop: without the note's capture time the
    "valid at the note's time" rule cannot hold, so reference shapes fall
    through to creation exactly as before. embedder gates layer 2 the same
    way — not wired in means skip straight on, never a degraded guess.
    surface is the mention's verbatim surface_text, the shape fallback when
    the model normalized the name.
    """
    if normalize_alias(name) in FIRST_PERSON or name == "Me":
        return await get_or_create_me(session)

    norm = normalize_alias(name)
    rows = await _exact_matches(session, norm)
    if len(rows) == 1:
        return ResolvedEntity(id=rows[0].id, subject_id=rows[0].subject_id)
    if len(rows) > 1:
        return AmbiguousEntity(candidate_ids=sorted(r.id for r in rows))

    ref = parse_reference(name)
    if ref is None and surface is not None:
        # Live models normalize reference mentions into invented proper names
        # ("the rat" -> name "Rat"), which reads as a plain name above. The
        # surface_text is verbatim note text, so it still carries the
        # reference shape the name lost. Trusted for SHAPE only — identity
        # still comes from the graph hop, never from the invented name.
        ref = parse_reference(surface)
    if ref is not None and note_time is not None:
        hop = await _relationship_hop(
            session, ref, kind_hint=kind_hint, domain=domain, at=note_time
        )
        if hop is not None:
            return hop

    # Layer 2 applies to plain names only: a reference surface ("Summer's
    # rat") is a description, not a name, so its vector matching an entity
    # NAME would be coincidence, not evidence.
    if ref is None and embedder is not None:
        scored = await _embedding_candidates(
            session,
            name,
            kind_hint=kind_hint,
            domain=domain,
            embedder=embedder,
            embed_model=embed_model,
        )
        if len(scored) == 1 and scored[0][1] >= _EMBED_STRONG:
            only, sim = scored[0]
            # Subject-bearing entities never auto-link on similarity alone:
            # cross-subject misattribution is a leak (docs/ANALYSIS.md
            # "Entities"), so those candidates always face layer 3 / review.
            if only.subject_id is None:
                return ResolvedEntity(
                    id=only.id, subject_id=only.subject_id, method="embedding", confidence=sim
                )
        if scored:
            return NeedsDisambiguation(candidates=[c for c, _ in scored])

    return await create_provisional(session, name, kind_hint=kind_hint, domain=domain)
