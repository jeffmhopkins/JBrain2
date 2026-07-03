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

import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.router import LlmRouter
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import SqlSettingsStore
from jbrain.wiki.budget import WikiLintGate
from jbrain.wiki.builder import NOTABILITY_MIN_FACTS, NOTABILITY_MIN_NOTES
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

# ---- Wave B (LLM verifier) constants -----------------------------------------------------

# The candidate-pair hard cap per run (docs/plans/WIKI_LINT_PLAN.md §4) — the worst-case the
# wiki_lint budget is sized to. Deterministic ORDER BY (least,greatest) makes sampling stable.
MAX_CANDIDATE_PAIRS = 500
VERIFY_BATCH = 20  # candidate pairs per adapter call
# Conservative per-batch estimate checked against the remaining lint budget before spending.
LINT_VERIFY_ESTIMATE_TOKENS = 4_000

# Versioned system prompts. Pinned by a sha256 digest test (test_wiki_lint_prompts) so editing the
# prose without bumping the version is red CI — the same drift guard the .prompt digest pins give
# the extraction/rewrite prompts, kept as a module constant here (no rendered .prompt file).
CONTRADICTION_PROMPT_VERSION = "wiki-lint-contradiction-v1"
CONTRADICTION_SYSTEM = (
    "You compare two subjects from a personal knowledge wiki for a DIRECT factual contradiction "
    "between their stated facts (e.g. each names a different current spouse for the same marriage, "
    "or gives incompatible values for the same shared attribute). Return contradiction=true ONLY "
    "for a genuine, current, mutually-exclusive conflict — NOT for unrelated, complementary, or "
    "merely different facts, and NOT for historical values explicitly superseded. Be strict; if in "
    "doubt, contradiction=false. `summary` is one neutral sentence naming the conflict (no domain "
    "names, no verbatim note text)."
)
STALE_PROMPT_VERSION = "wiki-lint-stale-v1"
STALE_SYSTEM = (
    "An article section's prose is shown alongside a fact that has since been SUPERSEDED. Return "
    "framed_as_current=true ONLY if the prose presents the superseded fact as the subject's "
    "CURRENT state (not as history/past). Narrating it as a past fact is fine "
    "(framed_as_current=false). Be strict; if in doubt, framed_as_current=false. `summary` is one "
    "neutral sentence (no domain names, no verbatim note text)."
)

_CONTRADICTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "contradiction", "summary"],
                "properties": {
                    "index": {"type": "integer"},
                    "contradiction": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
            },
        }
    },
}
_STALE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "framed_as_current", "summary"],
                "properties": {
                    "index": {"type": "integer"},
                    "framed_as_current": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
            },
        }
    },
}


def contradiction_batch_line(index: int, a_claims: list[str], b_claims: list[str]) -> str:
    """One candidate pair rendered for the contradiction verifier prompt. Exposed so the
    calibration harness (`evals/wiki_lint_runner`) exercises the EXACT production wording."""
    a = "\n".join(f"    - {s}" for s in a_claims)
    b = "\n".join(f"    - {s}" for s in b_claims)
    return f"Pair [{index}]:\n  Subject A facts:\n{a}\n  Subject B facts:\n{b}"


def stale_batch_line(index: int, superseded_fact: str, prose: str) -> str:
    """One candidate rendered for the stale-claim verifier prompt (production wording)."""
    return f"Item [{index}]:\n  Superseded fact: {superseded_fact}\n  Article prose: {prose}"


def card_domain(d_a: str, d_b: str) -> str | None:
    """The firewall-safe `domain_code` to stamp a CROSS-article (two-domain) finding's review card
    with — NEVER `ratchet_domain`/`_review_card_domain` (order-dependent, leak across firewalls):
    the shared domain when equal; the restricted side when exactly one is `general`; and **None**
    (→ suppress, never a `review_items` row) when the two are DISTINCT restricted domains. A None
    result means a scoped reviewer of either restricted domain could see the other's content, so no
    single-`domain_code` card can safely carry it (docs/plans/WIKI_LINT_PLAN.md §5)."""
    if d_a == d_b:
        return d_a
    if d_a == "general":
        return d_b
    if d_b == "general":
        return d_a
    return None  # two distinct restricted → no card


WIKI_LINT_SPEC = ActionSpec(
    name="wiki_lint",
    version=1,
    handler="wiki_lint",
    domain_optional=True,
    # `mutating` is DB blast-radius (it flips the `wiki_built` dirty bit for the index-integrity
    # class + writes review_items), NOT article mutation — no wiki prose/revision/section is ever
    # written here.
    mutating=True,
    # 'expensive' (display-only) now Wave B's LLM verifier is wired; the deterministic checks are
    # cheap but the run may spend against the SEPARATE wiki-lint budget.
    cost_class="expensive",
    dedup_key_expr=None,
    description="Corpus-wide wiki health audit: report drift, re-dirty stale index, verify (LLM).",
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
    contradiction_cards: int = 0  # Wave B: cross-article contradiction review cards filed
    stale_claim_cards: int = 0  # Wave B: stale-claim (framed-as-current) review cards filed


class WikiLinter:
    """Runs the deterministic (Wave A) `wiki_lint` checks system-scoped. Read-only against the
    wiki except the optional index-integrity re-dirty (a plain `entities.wiki_built` flip)."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        embedding_model: str,
        redirty_index: bool = True,
        router: LlmRouter | None = None,
        settings: SqlSettingsStore | None = None,
    ):
        self._maker = maker
        self._model = embedding_model
        # §9-6a default: include the index-integrity re-dirty (the only convergent re-dirty leg).
        # When False, the sweep is a pure report — no wiki mutation at all.
        self._redirty_index = redirty_index
        # Wave B: the LLM verifier runs only when BOTH a router and a settings store are injected
        # (the worker wires them). Absent → deterministic-only (Wave A), no spend.
        self._router = router
        self._settings = settings
        self._gate = WikiLintGate(settings) if settings is not None else None
        self._ctx: SessionContext = SYSTEM_CTX

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
        if self._router is not None and self._gate is not None:
            report = await self._verify_llm(report)
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

    # ---- Wave B: the LLM verifier (contradiction + stale-claim) --------------------------

    async def _verify_llm(self, report: LintReport) -> LintReport:
        """Run the two LLM verifiers and file review cards. Fail-closed on budget: a batch refused
        by the gate stops verification (the rest waits for the next window) rather than spending."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            contradictions = await self._verify_contradictions(session)
            stale = await self._verify_stale(session)
            await session.commit()
        return _replace(report, contradiction_cards=contradictions, stale_claim_cards=stale)

    async def _verify_contradictions(self, session: AsyncSession) -> int:
        """Generate firewall-admitted candidate ENTITY pairs (both with an active article, linked by
        a relationship fact or a co-mention), GROUP them by `card_domain`, and batch each group
        through the adapter. Grouping by card_domain means a single adapter call never co-mingles
        two distinct restricted domains — the no-leak guarantee is structural. A `contradiction`
        verdict files a `wiki_contradiction` card stamped with the group's domain."""
        candidates = await self._contradiction_candidates(session)
        if not candidates:
            return 0
        by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for cand in candidates:
            by_domain[cand["domain"]].append(cand)
        filed = 0
        for domain, group in by_domain.items():
            for start in range(0, len(group), VERIFY_BATCH):
                batch = group[start : start + VERIFY_BATCH]
                user_text = "\n\n".join(
                    contradiction_batch_line(i, c["a_claims"], c["b_claims"])
                    for i, c in enumerate(batch)
                )
                verdicts = await self._verify_batch(
                    "wiki.lint.contradiction",
                    CONTRADICTION_SYSTEM,
                    user_text,
                    _CONTRADICTION_SCHEMA,
                )
                if verdicts is None:
                    return filed  # budget refused → fail-closed, stop
                for v in verdicts:
                    idx = v.get("index")
                    if not (isinstance(idx, int) and 0 <= idx < len(batch)):
                        continue
                    if v.get("contradiction") is not True:
                        continue
                    c = batch[idx]
                    if await self._file_card(
                        session,
                        kind="wiki_contradiction",
                        domain=domain,
                        payload={
                            "entity_ids": [str(c["a"]), str(c["b"])],
                            "summary": str(v.get("summary", "")),
                        },
                        dedup_ids=[str(c["a"]), str(c["b"])],
                    ):
                        filed += 1
        return filed

    async def _contradiction_candidates(self, session: AsyncSession) -> list[dict[str, Any]]:
        """DISTINCT unordered entity pairs (both with an active article), connected by a live
        relationship fact OR a co-mention, admitted by the per-arm ENTITY-row firewall and stamped
        with a non-None `card_domain`, hard-capped at MAX_CANDIDATE_PAIRS with a deterministic
        order. Each carries both sides' live fact statements for the prompt."""
        rows = (
            await session.execute(
                text(
                    "WITH arted AS ("
                    "  SELECT e.id, e.domain_code FROM app.entities e"
                    "  JOIN app.wiki_articles a ON a.entity_ref = e.id AND a.status = 'active'"
                    "), pairs AS ("
                    "  SELECT f.entity_id AS a, f.object_entity_id AS b FROM app.facts f"
                    "   WHERE f.object_entity_id IS NOT NULL"
                    "     AND f.status IN ('active', 'superseded')"
                    "  UNION"
                    "  SELECT m1.entity_id AS a, m2.entity_id AS b FROM app.entity_mentions m1"
                    "   JOIN app.entity_mentions m2"
                    "     ON m2.chunk_id = m1.chunk_id AND m2.entity_id <> m1.entity_id"
                    ")"
                    " SELECT DISTINCT least(p.a, p.b) AS a, greatest(p.a, p.b) AS b,"
                    " ea.domain_code AS d_a, eb.domain_code AS d_b"
                    " FROM pairs p"
                    " JOIN arted ea ON ea.id = least(p.a, p.b)"
                    " JOIN arted eb ON eb.id = greatest(p.a, p.b)"
                    " ORDER BY a, b"
                )
            )
        ).all()
        admitted: list[dict[str, Any]] = []
        involved: set[Any] = set()
        for r in rows:
            dom = card_domain(r.d_a, r.d_b)
            if dom is None:  # two distinct restricted → never generated (firewall)
                continue
            admitted.append({"a": r.a, "b": r.b, "domain": dom})
            involved.update((r.a, r.b))
            if len(admitted) >= MAX_CANDIDATE_PAIRS:
                break
        claims = await self._entity_claims(session, involved)
        for cand in admitted:
            cand["a_claims"] = claims.get(cand["a"], [])
            cand["b_claims"] = claims.get(cand["b"], [])
        return admitted

    async def _verify_stale(self, session: AsyncSession) -> int:
        """A `wiki_stale_claim` candidate is a citation (in a current revision) to a SUPERSEDED fact
        whose subject entity is marked built — the article may frame history as current. Single-
        entity: the card is stamped the subject's OWN `entities.domain_code` (never `card_domain`).
        Grouped by that domain so no adapter batch co-mingles two restricted domains."""
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT a.entity_ref AS entity_id, e.domain_code, f.id AS fact_id,"
                    " f.statement, coalesce(r.body, '') AS body"
                    " FROM app.wiki_citations c"
                    " JOIN app.facts f ON f.id = c.fact_id AND f.status = 'superseded'"
                    " JOIN app.wiki_revisions r ON r.id = c.revision_id"
                    " JOIN app.wiki_sections s"
                    "   ON s.id = r.section_id AND r.id = s.current_revision_id"
                    " JOIN app.wiki_articles a ON a.id = s.article_id AND a.status = 'active'"
                    " JOIN app.entities e ON e.id = a.entity_ref AND e.wiki_built"
                )
            )
        ).all()
        if not rows:
            return 0
        by_domain: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            by_domain[r.domain_code].append(r)
        filed = 0
        for _domain, group in by_domain.items():
            for start in range(0, len(group), VERIFY_BATCH):
                batch = group[start : start + VERIFY_BATCH]
                user_text = "\n\n".join(
                    stale_batch_line(i, r.statement, r.body) for i, r in enumerate(batch)
                )
                verdicts = await self._verify_batch(
                    "wiki.lint.stale", STALE_SYSTEM, user_text, _STALE_SCHEMA
                )
                if verdicts is None:
                    return filed
                for v in verdicts:
                    idx = v.get("index")
                    if not (isinstance(idx, int) and 0 <= idx < len(batch)):
                        continue
                    if v.get("framed_as_current") is not True:
                        continue
                    r = batch[idx]
                    if await self._file_card(
                        session,
                        kind="wiki_stale_claim",
                        domain=r.domain_code,
                        payload={
                            "entity_ids": [str(r.entity_id)],
                            "fact_id": str(r.fact_id),
                            "summary": str(v.get("summary", "")),
                        },
                        dedup_ids=[str(r.entity_id), str(r.fact_id)],
                    ):
                        filed += 1
        return filed

    async def _entity_claims(self, session: AsyncSession, ids: set[Any]) -> dict[Any, list[str]]:
        if not ids:
            return {}
        rows = (
            await session.execute(
                text(
                    "SELECT entity_id, statement FROM app.facts"
                    " WHERE entity_id = ANY(:ids) AND status IN ('active', 'superseded')"
                    " ORDER BY created_at"
                ),
                {"ids": list(ids)},
            )
        ).all()
        out: dict[Any, list[str]] = defaultdict(list)
        for r in rows:
            out[r.entity_id].append(r.statement)
        return out

    async def _verify_batch(
        self, task: str, system: str, user_text: str, schema: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        """Meter one adapter call through the fail-closed lint gate: refuse (return None) before
        spending when the gate says no; otherwise call, record the real spend, and return the parsed
        verdict list. Assumes the caller pre-grouped so `user_text` never co-mingles two restricted
        domains."""
        assert self._router is not None and self._gate is not None
        decision = await self._gate.check(self._ctx, estimated_tokens=LINT_VERIFY_ESTIMATE_TOKENS)
        if not decision.allowed:
            log.info("wiki_lint_budget_refused", reason=decision.reason)
            return None
        try:
            result = await self._router.complete(
                task, system=system, user_text=user_text, json_schema=schema
            )
        except Exception:  # noqa: BLE001 — a verifier failure must not abort the whole sweep
            log.warning("wiki_lint_verify_failed", task=task)
            return None
        await self._gate.record_spend(
            self._ctx, tokens=result.usage.input_tokens + result.usage.output_tokens
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else {}
        verdicts = parsed.get("verdicts", [])
        return [v for v in verdicts if isinstance(v, dict)]

    async def _file_card(
        self,
        session: AsyncSession,
        *,
        kind: str,
        domain: str,
        payload: dict[str, Any],
        dedup_ids: list[str],
    ) -> bool:
        """Insert an open review card, deduped on (kind, sorted entity/fact ids) so a re-run never
        multiplies an identical open item (the shipped `_maybe_flag_ambiguous` dedup pattern).
        Returns True when a card was written."""
        key = ",".join(sorted(dedup_ids))
        existing = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.review_items WHERE kind = :k AND status = 'open'"
                    " AND payload->>'dedup' = :key LIMIT 1"
                ),
                {"k": kind, "key": key},
            )
        ).first()
        if existing is not None:
            return False
        # `choices` drives the inbox action buttons (frontend payload.ts). Both verbs are the
        # universal `_apply_resolution` branches (no new resolution code): `dismiss` closes the
        # card; `correct` files an owner correction note (the #7 out-argue channel) that re-enters
        # ingestion and re-dirties the entity for the next build.
        card = {
            **payload,
            "dedup": key,
            "choices": [
                {"action": "dismiss", "label": "Dismiss"},
                {"action": "correct", "label": "File correction note"},
            ],
        }
        await session.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                " VALUES (:i, :k, cast(:p AS jsonb), :d)"
            ),
            {"i": str(uuid.uuid4()), "k": kind, "p": _json(card), "d": domain},
        )
        return True


def _json(obj: dict[str, Any]) -> str:
    import json

    return json.dumps(obj)


def _replace(report: LintReport, **changes: int) -> LintReport:
    return LintReport(**{**asdict(report), **changes})


def wiki_lint_handler(
    maker: async_sessionmaker[AsyncSession],
    *,
    embedding_model: str,
    redirty_index: bool = True,
    router: LlmRouter | None = None,
    settings: SqlSettingsStore | None = None,
) -> Any:
    """Worker dispatch entry for `wiki_lint` (payload-only Handler). Standalone factory (mirrors
    `analysis.hygiene.entity_hygiene_handler`) — deliberately NOT folded onto `wiki_handlers`, so
    the linter stays decoupled from `WikiBuilder`'s constructor and the builder's handler-set test
    is untouched. `router`+`settings` enable the Wave-B verifier; absent → deterministic only."""
    linter = WikiLinter(
        maker,
        embedding_model=embedding_model,
        redirty_index=redirty_index,
        router=router,
        settings=settings,
    )

    async def run(_payload: dict[str, Any]) -> None:
        await linter.run()

    return run
