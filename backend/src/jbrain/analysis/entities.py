"""Entity resolution v1 (docs/ANALYSIS.md "Alias resolution & separation").

Layered, cheapest first. This module implements layer 1 — exact alias /
canonical-name match, case- and diacritic-insensitive — plus provisional
creation and the canonical "Me" entity. The remaining layers are deliberate
seams, not stubs to flesh out here:

TODO(analysis): layer 2 — embedding similarity vs entity name+summary
    (docs/ANALYSIS.md "Alias resolution & separation"); requires entity
    summary embeddings from the nightly hygiene pass.
TODO(analysis): layer 3 — batched cheap-LLM disambiguation over candidates
    (task entity.disambiguate), constrained by entity_distinctions; gray zone
    falls through to the review inbox.
"""

import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


@dataclass(frozen=True)
class AmbiguousEntity:
    """Layer 1 found several candidates: no link, review-inbox item."""

    candidate_ids: list[uuid.UUID]


async def get_or_create_me(session: AsyncSession) -> ResolvedEntity:
    """The canonical "Me" entity, hard-linked to the owner's subject row —
    the implicit center of the graph, created exactly once."""
    row = (
        await session.execute(
            text(
                "SELECT id, subject_id FROM app.entities"
                " WHERE subject_id IS NOT NULL AND lower(canonical_name) = 'me'"
                " AND status != 'merged' LIMIT 1"
            )
        )
    ).first()
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


async def resolve_entity(
    session: AsyncSession, name: str, *, kind_hint: str, domain: str
) -> ResolvedEntity | AmbiguousEntity:
    """Layer 1 resolution; creates a provisional entity when nothing matches.

    Returns AmbiguousEntity (no link) when several entities answer to the
    name — the caller files the ambiguous_mention review item.
    """
    if normalize_alias(name) in FIRST_PERSON or name == "Me":
        return await get_or_create_me(session)

    norm = normalize_alias(name)
    rows = (
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
    if len(rows) == 1:
        return ResolvedEntity(id=rows[0].id, subject_id=rows[0].subject_id)
    if len(rows) > 1:
        return AmbiguousEntity(candidate_ids=sorted(r.id for r in rows))

    # No match: provisional entity, confirmed implicitly later. It inherits
    # the creating note's domain (docs/ANALYSIS.md "Domain placement").
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
            id=uuid.uuid4(), entity_id=entity.id, alias=name, alias_norm=norm, domain_code=domain
        )
    )
    await session.flush()
    return ResolvedEntity(id=entity.id, subject_id=None, created=True)
