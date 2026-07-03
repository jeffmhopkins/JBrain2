"""The wiki builder engine (docs/plans/PHASE6_WIKI_PLAN.md §3).

The builder is the *mechanism*: dirty-bit scan → source the entity's citable facts (minting a
same-domain derived chunk for a ratcheted fact) → enact a redirect for a merged entity → write
type-guided single-domain sections, append-only revisions, clause citations, wiki links, and the
per-section embedding index → emit the lead blurb → mark the entity built. The prose-generation
step is an injected `Rewriter` seam: the `StubRewriter` here is the deterministic, no-LLM
rendering used by tests; the live `LlmRewriter` (wiki/rewriter.py) drives the LLM behind a
grounding gate + the wiki-build budget and is what the worker injects.

Every write goes through the storage/firewall the way the contract requires: claims are sourced
at the fact's domain and cite a SAME-DOMAIN chunk (a ratcheted fact gets a minted derived chunk
in its own domain), so a section, its revisions, its citations, and its index row are all one
domain and the Postgres firewall triggers (migration 0046) accept them. The builder runs
system-scoped (SYSTEM_CTX): it legitimately crosses every domain to assemble a cross-domain
article, and the per-section RLS is what keeps a scoped reader from seeing an out-of-scope
section.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.canonical import name_fact_value, project_display_name
from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.queue import SYSTEM_CTX
from jbrain.schema import get_registry
from jbrain.wiki.budget import WikiBudgetExceeded

log = structlog.get_logger()


@dataclass(frozen=True)
class _BuildLogNote:
    """A one-line Build-log post the builder emits for an article it just built/redirected. Posted
    in its own transaction AFTER the build commits (best-effort) so a cosmetic post can never roll
    back a real build. `summary` is domain-neutral (counts + subject kind, never a domain name)."""

    article_id: uuid.UUID
    summary: str


class WikiGroundingError(Exception):
    """The grounding verifier failed (unparseable/ill-shaped verdict). Fail-closed: the build is
    abandoned for this entity (nothing published, the entity stays dirty for retry) rather than
    publishing unverified prose. Caught per-entity by the builder loop so one bad entity doesn't
    fail the whole run. Defined here (not the rewriter) so the builder can catch it cycle-free."""


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
    # The cited chunk's text — what the rewriter writes prose from and the grounding gate
    # checks each clause against. (For a ratcheted fact, the same text in a minted derived chunk.)
    chunk_text: str = ""


@dataclass(frozen=True)
class SourcedEntity:
    entity_id: uuid.UUID
    name: str
    kind: str
    domain_code: str
    claims: list[Claim]
    note_count: int
    # The owner-set profile image sha (entity metadata, not a claim); copied onto the article so a
    # scoped reader never reads the single-domain entity row across the firewall. None when unset.
    image_sha: str | None = None


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


async def _headword(
    session: AsyncSession,
    entity_id: uuid.UUID,
    kind: str,
    canonical: str,
    subject_id: uuid.UUID | None,
) -> str:
    """The article's title. Every entity already carries its projected display name as
    `canonical_name` EXCEPT the owner, who is pinned to "Me" app-wide (the KB is
    first-person, analysis/canonical.reproject_canonical_name). The wiki is an
    encyclopedia surface, so the owner's article is titled by their real name —
    projected from active name.* facts (name.given+name.family / name.full ...) — and
    falls back to "Me" only when no name fact resolves. Non-owner entities are
    untouched (the cheap casefold guard skips the fact query entirely)."""
    if subject_id is None or canonical.strip().casefold() != "me":
        return canonical
    facts = (
        await session.execute(
            text(
                "SELECT predicate, value_json FROM app.facts WHERE entity_id = :e"
                " AND status = 'active' AND valid_to IS NULL AND assertion = 'asserted'"
            ),
            {"e": entity_id},
        )
    ).all()
    values: dict[str, str] = {}
    for fact in facts:
        nm = name_fact_value(fact.predicate, fact.value_json)
        if nm is not None:
            values.setdefault(fact.predicate, nm)
    etype = get_registry().by_kind.get(kind)
    if etype is None:
        return canonical
    return project_display_name(etype.display_name, values) or canonical


# The reader's inline-marker grammar (mirrors the frontend citations.tsx INLINE_RE): a `[n]`
# citation or a `[label](target)` link. `_linkify` seeds these spans as protected and refuses any
# anchor carrying the grammar's delimiters, so a woven link can never corrupt a citation/link.
_MARKER_RE = re.compile(r"\[\d+\]|\[[^\]]+\]\([^)]+\)")
_UNSAFE_ANCHOR = re.compile(r"[\[\]()]")


def _linkify(body: str, links: list[tuple[str, str]]) -> str:
    """Weave inline wiki→wiki link markers into prose: wrap the FIRST whole-word occurrence of
    each link anchor as `[anchor](target)` (target `wiki:<slug>` for a live article, `redlink`
    when the target has none yet), which is what the reader renders as a live/red cross-link.

    Pre-existing citation/link markers are seeded as protected spans and the longest anchors go
    first, so an anchor never lands inside a `[n]` citation (an entity literally named "2") nor
    nests inside another's freshly-woven marker ("Nair" inside "[Nair Pediatrics](…)"); seeding the
    markers also makes a re-run idempotent. An anchor carrying the marker delimiters, or one not
    found verbatim (the grounded prose may phrase the relationship differently), is left unlinked —
    the `wiki_links` row still records the connection regardless. The body is always fresh rewriter
    output here, never a previously-linkified body."""
    seen: dict[str, str] = {}
    for anchor, target in links:
        if anchor and not _UNSAFE_ANCHOR.search(anchor) and anchor not in seen:
            seen[anchor] = target
    protected: list[tuple[int, int]] = [m.span() for m in _MARKER_RE.finditer(body)]
    for anchor in sorted(seen, key=len, reverse=True):
        for m in re.finditer(rf"\b{re.escape(anchor)}\b", body):
            if any(s < m.end() and m.start() < e for s, e in protected):
                continue  # this occurrence sits inside a citation or an already-woven link marker
            marker = f"[{anchor}]({seen[anchor]})"
            delta = len(marker) - (m.end() - m.start())
            protected = [
                (s + delta if s >= m.end() else s, e + delta if e >= m.end() else e)
                for s, e in protected
            ]
            protected.append((m.start(), m.start() + len(marker)))
            body = body[: m.start()] + marker + body[m.end() :]
            break  # one link per anchor; the rest of the prose stays plain
    return body


# Notability (docs/plans/PHASE6_WIKI_PLAN.md §6 #6): an entity earns an article with ≥3 cited facts
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
            # Merged entities are dirty too (the merge flips the bit) and need their article
            # turned into a redirect, so they are NOT excluded here.
            dirty = list(
                (
                    await session.execute(
                        text("SELECT id FROM app.entities WHERE NOT wiki_built ORDER BY created_at")
                    )
                ).scalars()
            )
        processed = 0
        for entity_id in dirty:
            try:
                async with scoped_session(self._maker, SYSTEM_CTX) as session:
                    note = await self._build_entity(session, entity_id)
                    await session.commit()
            except WikiBudgetExceeded:
                break  # out of budget for today — leave the rest dirty for the next window
            except WikiGroundingError:
                continue  # one entity's verifier failed — leave it dirty, keep building the rest
            await self._post_build_log(note)  # best-effort, post-commit (never aborts the build)
            processed += 1
        return processed

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
        processed = 0
        for entity_id in refs:
            try:
                async with scoped_session(self._maker, SYSTEM_CTX) as session:
                    note = await self._build_entity(session, entity_id)
                    await session.commit()
            except WikiBudgetExceeded:
                break
            except WikiGroundingError:
                continue
            await self._post_build_log(note)  # best-effort, post-commit
            processed += 1
        return processed

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
        """Archive articles whose anchor entity is GONE (purged). A merged entity still exists
        and is handled by the redirect path (refresh), so it is deliberately NOT pruned — else a
        merge whose redirect refresh hasn't reached yet would be wrongly archived (the redirect
        lost)."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            orphans = list(
                (
                    await session.execute(
                        text(
                            "SELECT a.id FROM app.wiki_articles a WHERE a.status = 'active'"
                            " AND a.entity_ref IS NOT NULL AND NOT EXISTS ("
                            "   SELECT 1 FROM app.entities e WHERE e.id = a.entity_ref)"
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

    async def _build_entity(
        self, session: AsyncSession, entity_id: uuid.UUID
    ) -> _BuildLogNote | None:
        """Build one entity's article; return the Build-log note to post post-commit (None when
        nothing was built — missing/not-notable/empty, or a redirect with no survivor article)."""
        merged_into = (
            await session.execute(
                text("SELECT merged_into_id FROM app.entities WHERE id = :e"), {"e": entity_id}
            )
        ).scalar()
        if merged_into is not None:
            # The entity was folded into another (the merge dirtied it via the 0046 trigger):
            # turn its article into a reversible redirect rather than rebuilding it.
            survivor_article, gone_name = await self._enact_redirect(
                session, entity_id, merged_into
            )
            await self._mark_built(session, entity_id)
            # Record the merge on the SURVIVOR's (active, readable) Build-log — only when both the
            # merged entity had an article and the survivor has one to post against.
            if survivor_article is not None:
                return _BuildLogNote(survivor_article, f"Merged in {gone_name}.")
            return None
        sourced = await self._source(session, entity_id)
        if sourced is None:
            return None  # missing entity — nothing to build
        if not is_notable(sourced):
            await self._mark_built(session, entity_id)
            return None
        plan = await self._rewriter.plan(sourced)
        # A notable entity can still yield zero sections — e.g. all its facts were dropped by the
        # same-domain-chunk skip, or it's notable only via mentions. Don't strand an empty article
        # in the landing/search rails; mark it built so it isn't re-scanned until it changes.
        if not plan.sections:
            await self._mark_built(session, entity_id)
            return None
        article_id, created = await self._ensure_article(session, sourced, plan)
        for seq, section in enumerate(plan.sections):
            await self._write_section(session, article_id, section, seq=seq)
        await self._mark_built(session, entity_id)
        n_domains = len({s.domain_code for s in plan.sections})
        verb = "Created" if created else "Rebuilt"
        return _BuildLogNote(
            article_id,
            f"{verb} article ({sourced.kind} guide);"
            f" {len(sourced.claims)} facts across {n_domains} domains.",
        )

    async def _enact_redirect(
        self, session: AsyncSession, gone_entity: uuid.UUID, survivor_entity: uuid.UUID
    ) -> tuple[uuid.UUID | None, str]:
        """Make the merged entity's article a reversible redirect to the survivor's article
        (status='merged', merged_into_id). The redirect-followable-only-if-the-survivor-has-an-
        in-scope-section firewall is enforced when the redirect is *followed* (a read concern);
        recording it here is firewall-neutral (owner-only `wiki_articles`).

        Returns `(survivor_article_id, gone_name)` for the Build-log: the survivor article id is
        None (no Build-log post) when the merged entity had no article or the survivor has none."""
        gone = (
            await session.execute(
                text(
                    "SELECT id, (SELECT canonical_name FROM app.entities WHERE id = :e) AS name"
                    " FROM app.wiki_articles WHERE entity_ref = :e"
                ),
                {"e": gone_entity},
            )
        ).first()
        gone_name = (gone.name if gone is not None else None) or "another entity"
        if gone is None:
            return None, gone_name  # the merged entity never had an article — nothing to redirect
        survivor_article = (
            await session.execute(
                text("SELECT id FROM app.wiki_articles WHERE entity_ref = :e"),
                {"e": survivor_entity},
            )
        ).scalar()
        await session.execute(
            text(
                "UPDATE app.wiki_articles SET status = 'merged', merged_into_id = :s,"
                " updated_at = now() WHERE id = :a"
            ),
            {"s": survivor_article, "a": gone.id},
        )
        return survivor_article, gone_name

    async def _source(self, session: AsyncSession, entity_id: uuid.UUID) -> SourcedEntity | None:
        ent = (
            await session.execute(
                text(
                    "SELECT canonical_name, kind, domain_code, merged_into_id, image_sha,"
                    " subject_id FROM app.entities WHERE id = :e"
                ),
                {"e": entity_id},
            )
        ).first()
        if ent is None or ent.merged_into_id is not None:
            return None

        excluded_notes, excluded_facts = await self._exclusions(session)
        # Pull each citable fact with its backing chunk's domain/note/text. A fact whose chunk
        # is a LOWER domain (a ratcheted fact) gets a minted same-domain derived chunk below, so
        # the citation firewall (citation.domain = section = chunk, note = chunk.note) holds.
        rows = (
            await session.execute(
                text(
                    "SELECT f.id AS fact_id, f.statement, f.domain_code AS fact_domain,"
                    " f.chunk_id, c.domain_code AS chunk_domain, c.note_id AS note_id,"
                    " c.text AS chunk_text, f.object_entity_id, oe.canonical_name AS object_name"
                    " FROM app.facts f"
                    " JOIN app.chunks c ON c.id = f.chunk_id"
                    " LEFT JOIN app.entities oe ON oe.id = f.object_entity_id"
                    # active + superseded (historical) stay citable per §3; retracted is gone and
                    # pending_review/flagged facts (incl. cross_subject_link) are NOT published.
                    " WHERE f.entity_id = :e AND f.status IN ('active', 'superseded')"
                    " ORDER BY f.created_at"
                ),
                {"e": entity_id},
            )
        ).all()
        claims: list[Claim] = []
        for r in rows:
            if r.note_id in excluded_notes or r.fact_id in excluded_facts:
                continue
            cite_chunk = r.chunk_id
            if r.chunk_domain != r.fact_domain:
                # Ratcheted fact: mint/reuse a same-domain derived chunk to cite (mirrors
                # AnalysisPipeline._citation_chunk) — never cite the lower-domain chunk.
                cite_chunk = await self._derived_chunk(
                    session,
                    source_chunk_id=r.chunk_id,
                    fact_domain=r.fact_domain,
                    note_id=r.note_id,
                )
            claims.append(
                Claim(
                    statement=r.statement,
                    domain_code=r.fact_domain,
                    chunk_id=cite_chunk,
                    note_id=r.note_id,
                    fact_id=r.fact_id,
                    object_entity_id=r.object_entity_id,
                    object_name=r.object_name,
                    chunk_text=r.chunk_text,
                )
            )
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
            name=await _headword(session, entity_id, ent.kind, ent.canonical_name, ent.subject_id),
            kind=ent.kind,
            domain_code=ent.domain_code,
            claims=claims,
            note_count=len(notes),
            image_sha=ent.image_sha,
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

    async def _derived_chunk(
        self,
        session: AsyncSession,
        *,
        source_chunk_id: uuid.UUID,
        fact_domain: str,
        note_id: uuid.UUID,
    ) -> uuid.UUID:
        """Get-or-create a same-domain `derived` copy of the source chunk (mirrors
        AnalysisPipeline._citation_chunk): the citable chunk for a ratcheted fact. Keyed on
        (note, fact_domain, source_anchor) so a rebuild reuses rather than duplicates. No
        embedding — derived chunks are citation backing, excluded from search.

        BELT-AND-SUSPENDERS: the analysis pipeline already re-points a ratcheted fact's
        `chunk_id` to its same-domain derived chunk at materialization (contract §3), so in
        normal flow `_source` sees chunk.domain == fact.domain and never calls this. It only
        fires for a fact whose stored chunk is somehow still lower-domain — the firewall would
        otherwise reject the citation — so it is kept as a guarantee, not the primary path."""
        src = str(source_chunk_id)
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM app.chunks WHERE note_id = :n AND domain_code = :d"
                    " AND source_kind = 'derived' AND source_anchor = :src LIMIT 1"
                ),
                {"n": str(note_id), "d": fact_domain, "src": src},
            )
        ).scalar()
        if existing is not None:
            return existing
        minted = (
            await session.execute(
                text(
                    "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq,"
                    " char_start, char_end, source_kind, source_anchor, text)"
                    " SELECT :new, note_id, :d, granularity, seq, char_start, char_end, 'derived',"
                    " :anchor, text FROM app.chunks WHERE id = :src RETURNING id"
                ),
                {"new": str(uuid.uuid4()), "d": fact_domain, "anchor": src, "src": src},
            )
        ).scalar()
        assert minted is not None  # INSERT ... RETURNING always yields the id
        return minted

    async def _ensure_article(
        self, session: AsyncSession, sourced: SourcedEntity, plan: PlannedArticle
    ) -> tuple[uuid.UUID, bool]:
        """Upsert the article shell; return `(article_id, created)` — `created` is False on a
        rebuild of an existing article (drives the Build-log's Created/Rebuilt verb)."""
        existing = (
            await session.execute(
                text("SELECT id FROM app.wiki_articles WHERE entity_ref = :e"),
                {"e": sourced.entity_id},
            )
        ).scalar()
        lead_vec = vector_literal((await self._embed.embed([plan.lead_summary]))[0])
        if existing is not None:
            # Rebuilding an existing article reactivates it: if it had been a merged redirect and
            # the entity was since un-merged, clear status/merged_into_id so the redirect is truly
            # reversible (otherwise a rebuilt article would stay a redirect forever).
            await session.execute(
                text(
                    "UPDATE app.wiki_articles SET title = :t, kind = :k, lead_summary = :ls,"
                    " lead_embedding = cast(:v AS vector), image_sha = :img, status = 'active',"
                    " merged_into_id = NULL, updated_at = now() WHERE id = :a"
                ),
                {
                    "t": sourced.name,
                    "k": sourced.kind,
                    "ls": plan.lead_summary,
                    "v": lead_vec,
                    "img": sourced.image_sha,
                    "a": existing,
                },
            )
            return existing, False
        created = (
            await session.execute(
                text(
                    "INSERT INTO app.wiki_articles (entity_ref, title, kind, slug, lead_summary,"
                    " lead_embedding, image_sha)"
                    " VALUES (:e, :t, :k, :sl, :ls, cast(:v AS vector), :img) RETURNING id"
                ),
                {
                    "e": sourced.entity_id,
                    "t": sourced.name,
                    "k": sourced.kind,
                    "sl": _slug(sourced.name),
                    "ls": plan.lead_summary,
                    "v": lead_vec,
                    "img": sourced.image_sha,
                },
            )
        ).scalar()
        assert created is not None  # INSERT ... RETURNING always yields the id
        return created, True

    async def _write_section(
        self, session: AsyncSession, article_id: uuid.UUID, section: PlannedSection, *, seq: int
    ) -> None:
        # Find-or-create the section by (article, domain, heading) — a domain has several
        # sections (Person → Early life / Career / …), so the heading is its identity. Revisions
        # are append-only: a rebuild adds a new revision and re-points current_revision_id,
        # preserving the full diff history; `seq` reorders the section to the plan's order.
        section_id = (
            await session.execute(
                text(
                    "SELECT id FROM app.wiki_sections"
                    " WHERE article_id = :a AND domain_code = :d AND heading = :h"
                    " AND parent_section_id IS NULL LIMIT 1"
                ),
                {"a": article_id, "d": section.domain_code, "h": section.heading},
            )
        ).scalar()
        if section_id is None:
            section_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.wiki_sections (article_id, domain_code, heading, seq)"
                        " VALUES (:a, :d, :h, :s) RETURNING id"
                    ),
                    {"a": article_id, "d": section.domain_code, "h": section.heading, "s": seq},
                )
            ).scalar()
        else:
            await session.execute(
                text("UPDATE app.wiki_sections SET seq = :s WHERE id = :id"),
                {"s": seq, "id": section_id},
            )
        assert section_id is not None  # found or just inserted
        # Resolve each link's target article up front (system-scoped): its id powers the landing's
        # hub count + a future article→article jump, and its slug — or a redlink when the target
        # has no article yet — is woven into the prose below as an inline wiki link.
        resolved: list[tuple[PlannedLink, uuid.UUID | None]] = []
        anchor_targets: list[tuple[str, str]] = []
        for link in section.links:
            row = (
                await session.execute(
                    text(
                        "SELECT id, slug FROM app.wiki_articles"
                        " WHERE entity_ref = :e AND status = 'active'"
                    ),
                    {"e": link.to_entity_id},
                )
            ).first()
            resolved.append((link, row.id if row is not None else None))
            if link.anchor:
                anchor_targets.append(
                    (link.anchor, f"wiki:{row.slug}" if row is not None else "redlink")
                )
        body = _linkify(section.body, anchor_targets)
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
                {"s": section_id, "seq": next_seq, "b": body, "sum": section.summary},
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
        # Links are per-section (not per-revision): replace them on each build, reusing the
        # targets already resolved above (to_article_id is null for a red-link target).
        await session.execute(
            text("DELETE FROM app.wiki_links WHERE from_section_id = :s"), {"s": section_id}
        )
        for link, to_article in resolved:
            await session.execute(
                text(
                    "INSERT INTO app.wiki_links (from_section_id, to_entity_id, to_article_id,"
                    " anchor, domain_code) VALUES (:s, :e, :a, :anc, :d)"
                ),
                {
                    "s": section_id,
                    "e": link.to_entity_id,
                    "a": to_article,
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

    async def _post_build_log(self, note: _BuildLogNote | None) -> None:
        """Post one Build-log entry for a just-built article, in its OWN transaction (the build is
        already committed). Best-effort: the Build log is cosmetic editorial metadata, so a failure
        here is logged and swallowed — it must never undo a real build. Find-or-creates the
        article's single `build_log` topic SELECT-first (the common sequential path reuses it), with
        a bare `ON CONFLICT DO NOTHING` + re-select backstop for the rare two-concurrent-runs race
        (the partial unique index is the integrity guard); then appends the builder post and bumps
        `last_post_at` in the same transaction (post + bump stay atomic)."""
        if note is None:
            return
        try:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                topic_id = await self._build_log_topic(session, note.article_id)
                await session.execute(
                    text(
                        "INSERT INTO app.wiki_talk_posts (topic_id, author, body)"
                        " VALUES (:t, 'builder', :b)"
                    ),
                    {"t": topic_id, "b": note.summary},
                )
                await session.execute(
                    text("UPDATE app.wiki_talk_topics SET last_post_at = now() WHERE id = :t"),
                    {"t": topic_id},
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — cosmetic Build-log post must never fail a build
            log.warning("wiki_build_log_post_failed", article_id=str(note.article_id))

    @staticmethod
    async def _build_log_topic(session: AsyncSession, article_id: uuid.UUID) -> uuid.UUID:
        """Find-or-create the article's single `build_log` topic. SELECT-first so a rebuild reuses
        the existing topic; the bare ON CONFLICT DO NOTHING + re-select only matters if two builds
        of the same article race (the partial unique index keeps it to one topic regardless)."""
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM app.wiki_talk_topics"
                    " WHERE article_id = :a AND kind = 'build_log'"
                ),
                {"a": article_id},
            )
        ).scalar()
        if existing is not None:
            return existing
        inserted = (
            await session.execute(
                text(
                    "INSERT INTO app.wiki_talk_topics (article_id, kind, title)"
                    " VALUES (:a, 'build_log', 'Build log') ON CONFLICT DO NOTHING RETURNING id"
                ),
                {"a": article_id},
            )
        ).scalar()
        if inserted is not None:
            return inserted
        # Lost the race: the concurrent run created it — re-select the winner's id.
        won = (
            await session.execute(
                text(
                    "SELECT id FROM app.wiki_talk_topics"
                    " WHERE article_id = :a AND kind = 'build_log'"
                ),
                {"a": article_id},
            )
        ).scalar()
        assert won is not None  # a row exists: either ours or the concurrent winner's
        return won
