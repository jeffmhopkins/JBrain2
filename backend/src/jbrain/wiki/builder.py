"""The wiki builder engine (docs/PHASE6_WIKI_PLAN.md §3, Wave C2a — engine + sourcing).

This wave lands the *mechanism*: dirty-bit scan → source the entity's citable facts (and
their same-domain chunks) → write type-guided single-domain sections, append-only revisions,
clause citations, wiki links, and the per-section embedding index → emit the lead blurb →
mark the entity built. The prose-generation step is an injected `Rewriter` seam: C2a ships a
deterministic, non-LLM `StubRewriter` (real cited output, just terse) so the whole write path
is exercised end-to-end and firewall-correct; the LLM rewrite + grounding gate + type guides +
merge/split enactment + the token budget land in Wave C2b, swapping the rewriter only.

Every write goes through the storage/firewall the way the contract requires: claims are sourced
at the fact's domain and cite a SAME-DOMAIN chunk (a fact whose backing chunk sits in another
domain is skipped here — derived-chunk minting for the ratcheted/chunk-only case is C2b), so a
section, its revisions, its citations, and its index row are all one domain and the Postgres
firewall triggers (migration 0046) accept them. The builder runs system-scoped (SYSTEM_CTX): it
legitimately crosses every domain to assemble a cross-domain article, and the per-section RLS is
what keeps a scoped reader from seeing an out-of-scope section.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.queue import SYSTEM_CTX


# A cited fact reduced to what the article needs: the claim text, the domain it belongs in,
# the same-domain chunk + note that back it, the fact (when fact-backed), and the object entity
# of a relationship fact (so the section can link to it).
@dataclass(frozen=True)
class Claim:
    statement: str
    domain_code: str
    chunk_id: uuid.UUID
    note_id: uuid.UUID
    fact_id: uuid.UUID | None
    object_entity_id: uuid.UUID | None
    object_name: str | None


@dataclass(frozen=True)
class SourcedEntity:
    entity_id: uuid.UUID
    name: str
    kind: str
    domain_code: str
    claims: list[Claim]
    note_count: int


# What a `Rewriter` returns. A citation's `seq` is the article-wide [n] number; a link points
# at an entity (soft) and/or a known article, anchored on the section it sits in.
@dataclass(frozen=True)
class PlannedCitation:
    seq: int
    fact_id: uuid.UUID | None
    chunk_id: uuid.UUID
    note_id: uuid.UUID
    domain_code: str


@dataclass(frozen=True)
class PlannedLink:
    to_entity_id: uuid.UUID
    anchor: str


@dataclass(frozen=True)
class PlannedSection:
    heading: str
    domain_code: str
    body: str
    summary: str
    citations: list[PlannedCitation] = field(default_factory=list)
    links: list[PlannedLink] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedArticle:
    lead_summary: str
    sections: list[PlannedSection]


class Rewriter(Protocol):
    """The prose seam. The live (C2b) implementation drives `router.complete` per the type
    guide with a grounding gate; the C2a stub composes a deterministic cited rendering."""

    async def plan(self, sourced: SourcedEntity) -> PlannedArticle: ...


# Per-domain section headings for the stub (C2b replaces these with type-guided sections).
_DOMAIN_HEADING = {
    "general": "Overview",
    "health": "Health",
    "finance": "Finances",
    "location": "Places",
}


class StubRewriter:
    """A deterministic, non-LLM rewriter: one section per domain, each claim a sentence with an
    article-wide [n] citation. Honest (every clause cites its real note), just not prose — the
    seam that lets C2a ship the whole write path before the LLM rewrite (C2b) is wired."""

    async def plan(self, sourced: SourcedEntity) -> PlannedArticle:
        by_domain: dict[str, list[Claim]] = defaultdict(list)
        for claim in sourced.claims:
            by_domain[claim.domain_code].append(claim)

        sections: list[PlannedSection] = []
        seq = 0
        # Stable order: the entity's own domain first, then the rest alphabetically.
        domains = sorted(by_domain, key=lambda d: (d != sourced.domain_code, d))
        for domain in domains:
            claims = by_domain[domain]
            citations: list[PlannedCitation] = []
            links: list[PlannedLink] = []
            parts: list[str] = []
            for claim in claims:
                seq += 1
                parts.append(f"{claim.statement.rstrip('.')}.[{seq}]")
                citations.append(
                    PlannedCitation(
                        seq=seq,
                        fact_id=claim.fact_id,
                        chunk_id=claim.chunk_id,
                        note_id=claim.note_id,
                        domain_code=domain,
                    )
                )
                if claim.object_entity_id is not None:
                    links.append(
                        PlannedLink(
                            to_entity_id=claim.object_entity_id,
                            anchor=claim.object_name or "",
                        )
                    )
            sections.append(
                PlannedSection(
                    heading=_DOMAIN_HEADING.get(domain, domain.title()),
                    domain_code=domain,
                    body=" ".join(parts),
                    summary=f"{sourced.name}: {_DOMAIN_HEADING.get(domain, domain).lower()}.",
                    citations=citations,
                    links=links,
                )
            )
        lead = (
            f"{sourced.name} is a {sourced.kind.lower()} described by {sourced.note_count} note(s)."
        )
        return PlannedArticle(lead_summary=lead, sections=sections)


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "article"
    return f"{base}-{uuid.uuid4().hex[:6]}"


# Notability (docs/PHASE6_WIKI_PLAN.md §6 #6): an entity earns an article with ≥3 cited facts
# OR ≥2 distinct source notes. Tunable in editorial config later; a constant for C2a.
NOTABILITY_MIN_FACTS = 3
NOTABILITY_MIN_NOTES = 2


def is_notable(sourced: SourcedEntity) -> bool:
    return len(sourced.claims) >= NOTABILITY_MIN_FACTS or sourced.note_count >= NOTABILITY_MIN_NOTES


class WikiBuilder:
    """Runs the four wiki actions. System-scoped throughout; commits per entity/article so one
    bad build never strands the rest."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        embed: EmbedClient,
        rewriter: Rewriter,
        embedding_model: str,
    ):
        self._maker = maker
        self._embed = embed
        self._rewriter = rewriter
        self._model = embedding_model

    # ---- actions -------------------------------------------------------------------------

    async def refresh(self) -> int:
        """Incremental: build every dirty (`wiki_built=false`) entity, mark it built. Returns
        the number of entities processed."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            dirty = list(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM app.entities"
                            " WHERE NOT wiki_built AND merged_into_id IS NULL"
                            " ORDER BY created_at"
                        )
                    )
                ).scalars()
            )
        for entity_id in dirty:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                await self._build_entity(session, entity_id)
                await session.commit()
        return len(dirty)

    async def rebuild(self, target: str) -> int:
        """Full re-derive ignoring the dirty bit: one article by id, or every active article
        when target == 'all'."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            if target == "all":
                refs = list(
                    (
                        await session.execute(
                            text(
                                "SELECT entity_ref FROM app.wiki_articles"
                                " WHERE status = 'active' AND entity_ref IS NOT NULL"
                            )
                        )
                    ).scalars()
                )
            else:
                refs = list(
                    (
                        await session.execute(
                            text(
                                "SELECT entity_ref FROM app.wiki_articles"
                                " WHERE id = :a AND entity_ref IS NOT NULL"
                            ),
                            {"a": target},
                        )
                    ).scalars()
                )
        for entity_id in refs:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                await self._build_entity(session, entity_id)
                await session.commit()
        return len(refs)

    async def reindex(self) -> int:
        """Re-embed every `wiki_index` summary (after an embedding-model swap)."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            rows = (await session.execute(text("SELECT id, summary FROM app.wiki_index"))).all()
        if not rows:
            return 0
        vectors = await self._embed.embed([r.summary for r in rows])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            for row, vec in zip(rows, vectors, strict=True):
                await session.execute(
                    text(
                        "UPDATE app.wiki_index SET summary_embedding = cast(:v AS vector),"
                        " embedding_model = :m, last_updated_at = now() WHERE id = :i"
                    ),
                    {"v": vector_literal(vec), "m": self._model, "i": row.id},
                )
            await session.commit()
        return len(rows)

    async def prune(self) -> int:
        """Archive articles whose anchor entity is gone or merged away (orphans)."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            orphans = list(
                (
                    await session.execute(
                        text(
                            "SELECT a.id FROM app.wiki_articles a WHERE a.status = 'active'"
                            " AND a.entity_ref IS NOT NULL AND NOT EXISTS ("
                            "   SELECT 1 FROM app.entities e"
                            "   WHERE e.id = a.entity_ref AND e.merged_into_id IS NULL)"
                        )
                    )
                ).scalars()
            )
            for article_id in orphans:
                await session.execute(
                    text(
                        "UPDATE app.wiki_articles SET status = 'archived', updated_at = now()"
                        " WHERE id = :a"
                    ),
                    {"a": article_id},
                )
            await session.commit()
        return len(orphans)

    # ---- the per-entity build ------------------------------------------------------------

    async def _build_entity(self, session: AsyncSession, entity_id: uuid.UUID) -> None:
        sourced = await self._source(session, entity_id)
        if sourced is None:
            return  # merged/missing entity — nothing to build
        if not is_notable(sourced):
            await self._mark_built(session, entity_id)
            return
        plan = await self._rewriter.plan(sourced)
        # A notable entity can still yield zero sections — e.g. all its facts were dropped by the
        # same-domain-chunk skip, or it's notable only via mentions. Don't strand an empty article
        # in the landing/search rails; mark it built so it isn't re-scanned until it changes.
        if not plan.sections:
            await self._mark_built(session, entity_id)
            return
        article_id = await self._ensure_article(session, sourced, plan)
        for section in plan.sections:
            await self._write_section(session, article_id, section)
        await self._mark_built(session, entity_id)

    async def _source(self, session: AsyncSession, entity_id: uuid.UUID) -> SourcedEntity | None:
        ent = (
            await session.execute(
                text(
                    "SELECT canonical_name, kind, domain_code, merged_into_id"
                    " FROM app.entities WHERE id = :e"
                ),
                {"e": entity_id},
            )
        ).first()
        if ent is None or ent.merged_into_id is not None:
            return None

        excluded_notes, excluded_facts = await self._exclusions(session)
        # Cite a SAME-DOMAIN chunk (c.domain_code = f.domain_code) and the chunk's own note, so
        # the citation firewall (citation.domain = section = chunk, note = chunk.note) accepts it.
        rows = (
            await session.execute(
                text(
                    "SELECT f.id AS fact_id, f.statement, f.domain_code, f.chunk_id,"
                    " c.note_id AS note_id, f.object_entity_id, oe.canonical_name AS object_name"
                    " FROM app.facts f"
                    " JOIN app.chunks c ON c.id = f.chunk_id AND c.domain_code = f.domain_code"
                    " LEFT JOIN app.entities oe ON oe.id = f.object_entity_id"
                    # active + superseded (historical) stay citable per §3; retracted is gone and
                    # pending_review/flagged facts (incl. cross_subject_link) are NOT published.
                    " WHERE f.entity_id = :e AND f.status IN ('active', 'superseded')"
                    " ORDER BY f.created_at"
                ),
                {"e": entity_id},
            )
        ).all()
        claims = [
            Claim(
                statement=r.statement,
                domain_code=r.domain_code,
                chunk_id=r.chunk_id,
                note_id=r.note_id,
                fact_id=r.fact_id,
                object_entity_id=r.object_entity_id,
                object_name=r.object_name,
            )
            for r in rows
            if r.note_id not in excluded_notes and r.fact_id not in excluded_facts
        ]
        notes = {c.note_id for c in claims}
        # A mention-only entity still counts its source notes toward notability.
        mention_notes = set(
            (
                await session.execute(
                    text("SELECT DISTINCT note_id FROM app.entity_mentions WHERE entity_id = :e"),
                    {"e": entity_id},
                )
            ).scalars()
        )
        notes |= mention_notes
        return SourcedEntity(
            entity_id=entity_id,
            name=ent.canonical_name,
            kind=ent.kind,
            domain_code=ent.domain_code,
            claims=claims,
            note_count=len(notes),
        )

    async def _exclusions(self, session: AsyncSession) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Global owner source-exclusions (article-scoped ones are applied per article later)."""
        rows = (
            await session.execute(
                text(
                    "SELECT note_id, fact_id FROM app.wiki_source_exclusions"
                    " WHERE article_id IS NULL"
                )
            )
        ).all()
        notes = {r.note_id for r in rows if r.note_id is not None}
        facts = {r.fact_id for r in rows if r.fact_id is not None}
        return notes, facts

    async def _ensure_article(
        self, session: AsyncSession, sourced: SourcedEntity, plan: PlannedArticle
    ) -> uuid.UUID:
        existing = (
            await session.execute(
                text("SELECT id FROM app.wiki_articles WHERE entity_ref = :e"),
                {"e": sourced.entity_id},
            )
        ).scalar()
        lead_vec = vector_literal((await self._embed.embed([plan.lead_summary]))[0])
        if existing is not None:
            await session.execute(
                text(
                    "UPDATE app.wiki_articles SET title = :t, lead_summary = :ls,"
                    " lead_embedding = cast(:v AS vector), updated_at = now() WHERE id = :a"
                ),
                {"t": sourced.name, "ls": plan.lead_summary, "v": lead_vec, "a": existing},
            )
            return existing
        created = (
            await session.execute(
                text(
                    "INSERT INTO app.wiki_articles (entity_ref, title, slug, lead_summary,"
                    " lead_embedding) VALUES (:e, :t, :sl, :ls, cast(:v AS vector)) RETURNING id"
                ),
                {
                    "e": sourced.entity_id,
                    "t": sourced.name,
                    "sl": _slug(sourced.name),
                    "ls": plan.lead_summary,
                    "v": lead_vec,
                },
            )
        ).scalar()
        assert created is not None  # INSERT ... RETURNING always yields the id
        return created

    async def _write_section(
        self, session: AsyncSession, article_id: uuid.UUID, section: PlannedSection
    ) -> None:
        # Find-or-create the (article, heading, domain) section; revisions are append-only, so a
        # rebuild adds a new revision and re-points current_revision_id — the full diff history
        # the owner asked for is preserved.
        section_id = (
            await session.execute(
                text(
                    "SELECT id FROM app.wiki_sections"
                    " WHERE article_id = :a AND domain_code = :d AND parent_section_id IS NULL"
                    " ORDER BY seq LIMIT 1"
                ),
                {"a": article_id, "d": section.domain_code},
            )
        ).scalar()
        if section_id is None:
            section_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.wiki_sections (article_id, domain_code, seq)"
                        " VALUES (:a, :d, :s) RETURNING id"
                    ),
                    {"a": article_id, "d": section.domain_code, "s": 0},
                )
            ).scalar()
        assert section_id is not None  # found or just inserted
        next_seq = (
            await session.execute(
                text(
                    "SELECT coalesce(max(seq), 0) + 1 FROM app.wiki_revisions WHERE section_id = :s"
                ),
                {"s": section_id},
            )
        ).scalar()
        revision_id = (
            await session.execute(
                text(
                    "INSERT INTO app.wiki_revisions (section_id, seq, body, summary)"
                    " VALUES (:s, :seq, :b, :sum) RETURNING id"
                ),
                {"s": section_id, "seq": next_seq, "b": section.body, "sum": section.summary},
            )
        ).scalar()
        await session.execute(
            text("UPDATE app.wiki_sections SET current_revision_id = :r WHERE id = :s"),
            {"r": revision_id, "s": section_id},
        )
        for cit in section.citations:
            await session.execute(
                text(
                    "INSERT INTO app.wiki_citations (revision_id, fact_id, chunk_id, note_id,"
                    " seq, domain_code) VALUES (:r, :f, :c, :n, :seq, :d)"
                ),
                {
                    "r": revision_id,
                    "f": cit.fact_id,
                    "c": cit.chunk_id,
                    "n": cit.note_id,
                    "seq": cit.seq,
                    "d": cit.domain_code,
                },
            )
        # Links are per-section (not per-revision): replace them on each build.
        await session.execute(
            text("DELETE FROM app.wiki_links WHERE from_section_id = :s"), {"s": section_id}
        )
        for link in section.links:
            await session.execute(
                text(
                    "INSERT INTO app.wiki_links (from_section_id, to_entity_id, anchor,"
                    " domain_code) VALUES (:s, :e, :anc, :d)"
                ),
                {
                    "s": section_id,
                    "e": link.to_entity_id,
                    "anc": link.anchor,
                    "d": section.domain_code,
                },
            )
        await self._upsert_index(session, section_id, section)

    async def _upsert_index(
        self, session: AsyncSession, section_id: uuid.UUID, section: PlannedSection
    ) -> None:
        vec = vector_literal((await self._embed.embed([section.summary]))[0])
        await session.execute(
            text(
                "INSERT INTO app.wiki_index (section_id, domain_code, summary, summary_embedding,"
                " embedding_model) VALUES (:s, :d, :sum, cast(:v AS vector), :m)"
                " ON CONFLICT (section_id) DO UPDATE SET summary = excluded.summary,"
                " summary_embedding = excluded.summary_embedding,"
                " embedding_model = excluded.embedding_model, last_updated_at = now()"
            ),
            {
                "s": section_id,
                "d": section.domain_code,
                "sum": section.summary,
                "v": vec,
                "m": self._model,
            },
        )

    async def _mark_built(self, session: AsyncSession, entity_id: uuid.UUID) -> None:
        await session.execute(
            text("UPDATE app.entities SET wiki_built = true WHERE id = :e"), {"e": entity_id}
        )
