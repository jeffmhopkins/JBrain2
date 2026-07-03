"""The `wiki_lint` engine action (docs/plans/WIKI_LINT_PLAN.md) — Wave A, deterministic slice.

A periodic corpus-wide wiki HEALTH audit: the "third leg" alongside ingest
(`wiki_refresh`/`wiki_rebuild`) and query (`search`/`agent`). The per-build grounding gate
(`wiki/rewriter.py`) is single-entity/single-build — it never compares two articles or the
corpus against itself — so standing cross-article drift is invisible to it. This sweep catches
the deterministic, no-LLM slice of that drift (the LLM contradiction/stale verifier is Wave B).

Read-only against the wiki: it never writes a revision/section/prose (CLAUDE.md #7 — the wiki
stays machine-written). Its only outputs are:
  1. a structured lint REPORT (counts of the weak-signal classes) emitted to structlog, captured
     in the `runs` run-log for the fire (`finalize_job_step`);
  2. an optional RE-DIRTY of `entities.wiki_built` for the index-integrity class only, so the next
     nightly `wiki_refresh` self-heals it.

Convergence discipline (the sharpest failure mode, designed out): a re-dirty is prescribed ONLY
for a class where (i) no 0046 trigger already heals it, (ii) a rebuild re-derives the missing
artifact deterministically, and (iii) the artifact lives on a section the next plan reproduces.
Link/index rows are rewritten by the builder ONLY per section present in the new plan
(`builder._write_section`, called only over `plan.sections`), with NO section reconciliation — so
every LLM-drafting-dependent OR section-reappearance-dependent class (red-link-became-notable,
fact-backed missing-xref, bare co-mention, stale-missing-inbound, coverage gaps) is a
REPORT-ONLY weak signal, never re-dirtied, else re-dirtying re-runs the expensive LLM builder to
no effect and the finding reappears forever. The only re-dirty leg is the index-integrity class,
scoped to entities that still yield a citable section (`_buildable_entities`), which is the
`check-3` sourcing filter — the orphaned-section residue is excluded because a non-buildable
entity is never re-dirtied.

Firewall under SYSTEM_CTX: like `WikiBuilder`, this runs system-scoped (it legitimately crosses
every domain), so RLS does not filter its reads — the firewall input comes from CODE. Every
cross-article pair (checks 4, 5) is admitted only when both endpoints' OWN `entities.domain_code`
(never `entity_mentions.domain_code`, which is the divergent note domain) satisfy the per-arm
rule `d_a == d_b OR d_a == 'general' OR d_b == 'general'`, so a two-distinct-restricted pair
(health×finance) is never counted and never has its identities co-mingled into a finding.

Talk build-log note (docs deviation): the plan named a Talk build-log summary as an output, but
`wiki_talk_topics.article_id` is NOT NULL (FK to a single article) — a corpus-wide summary has no
article to anchor to, and making that column nullable would be a non-additive schema change. So
the report goes to structlog + the `runs` log (the shipped audit surface) instead. Per-article
findings could anchor to their article's `build_log` topic in a later wave if wanted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.wiki.builder import NOTABILITY_MIN_FACTS, NOTABILITY_MIN_NOTES
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

WIKI_LINT_SPEC = ActionSpec(
    name="wiki_lint",
    version=1,
    handler="wiki_lint",
    domain_optional=True,
    # `mutating` is DB blast-radius (it flips the `wiki_built` dirty bit for the index-integrity
    # class), NOT article mutation — no wiki prose/revision/section is ever written here.
    mutating=True,
    cost_class="cheap",  # Wave A is pure SQL, no LLM (Wave B's verifier is 'expensive').
    dedup_key_expr=None,
    description="Corpus-wide wiki health audit: report drift, re-dirty stale index.",
)


def _firewall_ok(d_a: str, d_b: str) -> bool:
    """The per-arm firewall rule, evaluated on each endpoint's OWN `entities.domain_code`. A pair
    spanning two DISTINCT restricted domains is dropped (never counted, never co-mingled); a
    same-domain or general-touching pair is admitted. Mirrors the `graph_context` star-filter's
    transitive closure at the pair boundary."""
    return d_a == d_b or d_a == "general" or d_b == "general"


@dataclass(frozen=True)
class LintReport:
    """The corpus-health snapshot one `wiki_lint` fire produces. All fields are counts (the
    weak-signal classes), plus `redirtied` (entities queued for the next `wiki_refresh`). Emitted
    to structlog and captured by the run-log; carries no titles/bodies/domain names."""

    coverage_gaps: int = 0  # check 3: notable, ≥1 citable fact, no active article
    redlink_became_notable: int = 0  # check 5a: red-link whose target now has an article
    stale_missing_inbound: int = 0  # check 5b: zero-inbound article with a live source fact
    missing_xref_fact_backed: int = 0  # check 4a: co-mentioned + relationship fact, no wiki link
    missing_xref_bare_comention: int = 0  # check 4b: co-mentioned, no relationship fact, no link
    index_problems: int = 0  # index-integrity sections (missing/stale/model-drift)
    redirtied: int = 0  # entities flipped wiki_built=false (index class, buildable only)


class WikiLinter:
    """Runs the deterministic (Wave A) `wiki_lint` checks system-scoped. Read-only against the
    wiki except the optional index-integrity re-dirty (a plain `entities.wiki_built` flip)."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        embedding_model: str,
        redirty_index: bool = True,
    ):
        self._maker = maker
        self._model = embedding_model
        # §9-6a default: include the index-integrity re-dirty (the only convergent re-dirty leg).
        # When False, the sweep is a pure report — no wiki mutation at all.
        self._redirty_index = redirty_index

    async def run(self) -> LintReport:
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            buildable = await self._buildable_entities(session)
            report = LintReport(
                coverage_gaps=await self._coverage_gaps(session, buildable),
                redlink_became_notable=await self._redlink_became_notable(session),
                stale_missing_inbound=await self._stale_missing_inbound(session),
                index_problems=len(await self._index_problem_sections(session)),
            )
            fact_backed, bare = await self._missing_xrefs(session)
            report = _replace(
                report,
                missing_xref_fact_backed=fact_backed,
                missing_xref_bare_comention=bare,
            )
            if self._redirty_index:
                redirtied = await self._redirty_index_problems(session, buildable)
                report = _replace(report, redirtied=redirtied)
            await session.commit()
        log.info("wiki_lint_report", **asdict(report))
        return report

    # ---- shared: the "buildable" set (check-3 sourcing filter) ---------------------------

    async def _buildable_entities(self, session: AsyncSession) -> set[Any]:
        """Entities that currently yield ≥1 citable section — i.e. ≥1 published, non-globally-
        excluded fact backed by a resolvable chunk, counted exactly as `builder._source` counts
        (JOIN app.chunks; a NULL/unresolved chunk_id fact is dropped). This is the guard that keeps
        the index re-dirty off the deliberate notable-but-sectionless class (a rebuild there
        re-derives zero sections and would loop)."""
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT f.entity_id"
                    " FROM app.facts f JOIN app.chunks c ON c.id = f.chunk_id"
                    " WHERE f.status IN ('active', 'superseded')"
                    "   AND f.id NOT IN (SELECT fact_id FROM app.wiki_source_exclusions"
                    "                     WHERE fact_id IS NOT NULL)"
                    "   AND c.note_id NOT IN (SELECT note_id FROM app.wiki_source_exclusions"
                    "                          WHERE note_id IS NOT NULL)"
                )
            )
        ).scalars()
        return set(rows)

    # ---- check 3: coverage gaps (report-only, never re-dirtied) --------------------------

    async def _coverage_gaps(self, session: AsyncSession, buildable: set[Any]) -> int:
        """Notable entities (≥3 published facts OR ≥2 distinct source notes, computed exactly as
        `builder.is_notable`) with no active article AND ≥1 citable fact (the default filter that
        suppresses the deliberate notable-but-sectionless class). Report-only: re-dirtying a
        coverage gap is forbidden — a rebuild honours notability then non-empty sections, so a
        sectionless entity would re-derive nothing and loop."""
        rows = (
            await session.execute(
                text(
                    "WITH excl AS (SELECT note_id, fact_id FROM app.wiki_source_exclusions"
                    "               WHERE article_id IS NULL),"
                    " pub AS ("
                    "   SELECT f.entity_id, f.id AS fact_id, c.note_id"
                    "   FROM app.facts f JOIN app.chunks c ON c.id = f.chunk_id"
                    "   WHERE f.status IN ('active', 'superseded')"
                    "     AND f.id NOT IN (SELECT fact_id FROM excl WHERE fact_id IS NOT NULL)"
                    "     AND c.note_id NOT IN (SELECT note_id FROM excl WHERE note_id IS NOT NULL)"
                    " ),"
                    " fc AS (SELECT entity_id, count(*) AS facts FROM pub GROUP BY entity_id),"
                    # notes = distinct( published-fact notes ∪ ALL mention notes ) — mirrors
                    # builder._source, whose mention_notes are not exclusion-filtered.
                    " nt AS (SELECT entity_id, count(DISTINCT note_id) AS notes FROM ("
                    "          SELECT entity_id, note_id FROM pub"
                    "          UNION"
                    "          SELECT entity_id, note_id FROM app.entity_mentions"
                    "        ) u GROUP BY entity_id)"
                    " SELECT count(*) FROM app.entities e"
                    " LEFT JOIN fc ON fc.entity_id = e.id"
                    " LEFT JOIN nt ON nt.entity_id = e.id"
                    " LEFT JOIN app.wiki_articles a"
                    "   ON a.entity_ref = e.id AND a.status = 'active'"
                    " WHERE e.merged_into_id IS NULL"
                    "   AND a.id IS NULL"
                    "   AND coalesce(fc.facts, 0) >= 1"  # default: ≥1 citable section
                    "   AND (coalesce(fc.facts, 0) >= :minf OR coalesce(nt.notes, 0) >= :minn)"
                ),
                {"minf": NOTABILITY_MIN_FACTS, "minn": NOTABILITY_MIN_NOTES},
            )
        ).scalar()
        return int(rows or 0)

    # ---- check 5a: red-link whose target became notable (report-only) --------------------

    async def _redlink_became_notable(self, session: AsyncSession) -> int:
        """A red-link (`to_article_id IS NULL`) whose `to_entity_id` now has an active article.
        Report-only (NON-convergent): a rebuild replaces the source's links only for sections the
        new plan reproduces, and the carrying clause may be restructured away — re-dirtying loops.
        Firewall-filtered on both endpoints' entity-row domains; count distinct source entities."""
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT src_a.entity_ref AS src_id, src_e.domain_code AS d_src,"
                    " tgt_e.domain_code AS d_tgt"
                    " FROM app.wiki_links l"
                    " JOIN app.wiki_sections s ON s.id = l.from_section_id"
                    " JOIN app.wiki_articles src_a ON src_a.id = s.article_id"
                    " JOIN app.entities src_e ON src_e.id = src_a.entity_ref"
                    " JOIN app.entities tgt_e ON tgt_e.id = l.to_entity_id"
                    " JOIN app.wiki_articles tgt_a"
                    "   ON tgt_a.entity_ref = l.to_entity_id AND tgt_a.status = 'active'"
                    " WHERE l.to_article_id IS NULL"
                )
            )
        ).all()
        return len({r.src_id for r in rows if _firewall_ok(r.d_src, r.d_tgt)})

    # ---- check 5b: zero-inbound article with a live source fact (report-only) -------------

    async def _stale_missing_inbound(self, session: AsyncSession) -> int:
        """An active article with zero cross-article inbound `wiki_links` that ANOTHER live entity
        holds a relationship fact toward (`facts.object_entity_id = this`). Report-only
        (NON-convergent): the inbound link comes from the OTHER entity's build, which emits it only
        if the LLM drafted+grounded the clause. Count distinct targets with ≥1 firewall-compatible
        source."""
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT tgt_e.id AS tgt_id, tgt_e.domain_code AS d_tgt,"
                    " src_e.domain_code AS d_src"
                    " FROM app.wiki_articles a"
                    " JOIN app.entities tgt_e ON tgt_e.id = a.entity_ref"
                    " JOIN app.facts f"
                    "   ON f.object_entity_id = tgt_e.id AND f.status IN ('active', 'superseded')"
                    " JOIN app.entities src_e ON src_e.id = f.entity_id"
                    " WHERE a.status = 'active'"
                    "   AND src_e.id <> tgt_e.id"
                    "   AND NOT EXISTS ("
                    "     SELECT 1 FROM app.wiki_links l"
                    "     JOIN app.wiki_sections fs ON fs.id = l.from_section_id"
                    "     WHERE l.to_article_id = a.id AND fs.article_id <> a.id)"
                )
            )
        ).all()
        return len({r.tgt_id for r in rows if _firewall_ok(r.d_src, r.d_tgt)})

    # ---- check 4: missing cross-references (report-only) ---------------------------------

    async def _missing_xrefs(self, session: AsyncSession) -> tuple[int, int]:
        """Co-mentioned entity pairs (same chunk) not connected by a `wiki_link`, partitioned:
        4a a live relationship fact links them (link merely absent) vs 4b a bare co-mention (no
        fact). Both report-only (NON-convergent): 4a's link needs the LLM to re-draft the clause;
        4b's only real fix mints a relationship fact (graph mutation lint must not do). Firewall on
        both entity rows. Returns `(fact_backed, bare)` counts of DISTINCT unordered pairs."""
        # Existing wiki-link entity pairs (source article's entity ↔ link target), unordered.
        linked = {
            frozenset((r.a, r.b))
            for r in (
                await session.execute(
                    text(
                        "SELECT fa.entity_ref AS a, l.to_entity_id AS b"
                        " FROM app.wiki_links l"
                        " JOIN app.wiki_sections fs ON fs.id = l.from_section_id"
                        " JOIN app.wiki_articles fa ON fa.id = fs.article_id"
                        " WHERE fa.entity_ref IS NOT NULL"
                    )
                )
            ).all()
        }
        # Live relationship-fact entity pairs (subject ↔ object), unordered.
        fact_pairs = {
            frozenset((r.a, r.b))
            for r in (
                await session.execute(
                    text(
                        "SELECT f.entity_id AS a, f.object_entity_id AS b FROM app.facts f"
                        " WHERE f.object_entity_id IS NOT NULL"
                        "   AND f.status IN ('active', 'superseded')"
                    )
                )
            ).all()
        }
        # Co-mention pairs (same chunk), each with both endpoints' entity-row domains, deduped
        # unordered by least/greatest so a pair is examined once.
        comention = (
            await session.execute(
                text(
                    "SELECT DISTINCT least(m1.entity_id, m2.entity_id) AS a,"
                    " greatest(m1.entity_id, m2.entity_id) AS b,"
                    " ea.domain_code AS d_a, eb.domain_code AS d_b"
                    " FROM app.entity_mentions m1"
                    " JOIN app.entity_mentions m2"
                    "   ON m2.chunk_id = m1.chunk_id AND m2.entity_id <> m1.entity_id"
                    " JOIN app.entities ea ON ea.id = least(m1.entity_id, m2.entity_id)"
                    " JOIN app.entities eb ON eb.id = greatest(m1.entity_id, m2.entity_id)"
                    " WHERE ea.merged_into_id IS NULL AND eb.merged_into_id IS NULL"
                )
            )
        ).all()
        fact_backed = 0
        bare = 0
        for r in comention:
            if not _firewall_ok(r.d_a, r.d_b):
                continue  # never co-mingle two distinct restricted domains into a finding
            pair = frozenset((r.a, r.b))
            if pair in linked:
                continue  # already cross-linked — not missing
            if pair in fact_pairs:
                fact_backed += 1
            else:
                bare += 1
        return fact_backed, bare
        # NOTE: reciprocal-asymmetry (a relationship fact whose inverse/symmetric twin is absent,
        # via supersession.INVERSE_PAIRS/SYMMETRIC_PREDICATES) is a further weak-signal count named
        # in the plan; deferred from this v1 slice (never a card, minting the reciprocal is graph
        # mutation) — add as a report field when the report has a consumer that surfaces it.

    # ---- index-integrity: the only re-dirty leg (§9-6a) ----------------------------------

    async def _index_problem_sections(self, session: AsyncSession) -> list[Any]:
        """Live sections whose `wiki_index` row is missing, stale (older than the section's current
        revision), or model-drifted (embedding_model != the configured model). Returns the anchor
        entity ids (article.entity_ref) — one per problem section."""
        rows = (
            await session.execute(
                text(
                    "SELECT a.entity_ref AS entity_id"
                    " FROM app.wiki_sections s"
                    " JOIN app.wiki_articles a ON a.id = s.article_id AND a.status = 'active'"
                    " LEFT JOIN app.wiki_index i ON i.section_id = s.id"
                    " LEFT JOIN app.wiki_revisions r ON r.id = s.current_revision_id"
                    " WHERE a.entity_ref IS NOT NULL"
                    "   AND (i.id IS NULL"
                    "        OR (r.created_at IS NOT NULL AND i.last_updated_at < r.created_at)"
                    "        OR i.embedding_model <> :m)"
                ),
                {"m": self._model},
            )
        ).all()
        return [r.entity_id for r in rows]

    async def _redirty_index_problems(self, session: AsyncSession, buildable: set[Any]) -> int:
        """Re-dirty entities with an index-integrity problem, SCOPED to buildable entities (still
        yield ≥1 citable section) so a rebuild reproduces the section and the `_upsert_index` write
        converges — the orphaned-section residue (a non-buildable entity) is excluded. Idempotent:
        `AND wiki_built` no-ops an already-dirty row."""
        targets = {eid for eid in await self._index_problem_sections(session) if eid in buildable}
        if not targets:
            return 0
        await session.execute(
            text("UPDATE app.entities SET wiki_built = false WHERE id = ANY(:ids) AND wiki_built"),
            {"ids": list(targets)},
        )
        return len(targets)


def _replace(report: LintReport, **changes: int) -> LintReport:
    return LintReport(**{**asdict(report), **changes})


def wiki_lint_handler(
    maker: async_sessionmaker[AsyncSession],
    *,
    embedding_model: str,
    redirty_index: bool = True,
) -> Any:
    """Worker dispatch entry for `wiki_lint` (payload-only Handler). Standalone factory (mirrors
    `analysis.hygiene.entity_hygiene_handler`) — deliberately NOT folded onto `wiki_handlers`, so
    the linter stays decoupled from `WikiBuilder`'s constructor and the builder's handler-set test
    is untouched."""
    linter = WikiLinter(maker, embedding_model=embedding_model, redirty_index=redirty_index)

    async def run(_payload: dict[str, Any]) -> None:
        await linter.run()

    return run
