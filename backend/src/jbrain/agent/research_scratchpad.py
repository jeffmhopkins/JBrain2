"""The run-scoped findings ledger for a `deep_research` run
(docs/plans/DEEP_RESEARCH_SCRATCHPAD_PLAN.md, Phase 1).

A deep-research run is a SINGLE in-request coroutine: plan → gather → analyze →
reflect → (refill) → synthesize → critique → revise all execute in one process, holding
every stage's output in local variables. This is the structural opposite of the jerv
Planning Tool's step-results scratchpad, which is a durable `results jsonb` column
BECAUSE a plan spans many owner turns and each continuation step is a fresh, context-less
turn — the DB is its only channel between steps. A deep-research run has no turn boundary
to survive, so its scratchpad is an in-memory object that lives and dies with the run: no
table, no migration, no RLS surface (which a durable version would mandate per CLAUDE.md
#3, for zero benefit here).

What the ledger buys over threading raw `[*gather, *analyst, *refill]` lists between
stages:

  * an explicit, queryable VISIBILITY model. Each entry carries a `scope`, and a consuming
    stage declares which scopes it reads. This makes the owner's rule first-class: gather
    researchers are spawned in isolation — a child never receives a sibling's entry, so
    there is no crosstalk — while the analyst, synthesizer, critique, and revise stages
    read the FULL research set. The rule was already true of the flat fan; the ledger makes
    it inspectable and testable instead of implicit in which list each call happened to be
    handed.
  * ONE source collection. The citation registry is built once from the ledger's children
    (`_collect_sources(scratch.children({RESEARCH, ANALYSIS}))`) instead of being recomputed
    over ad-hoc concatenations at three points in the pipeline.
  * source identity carried on each entry — the substrate for per-researcher partitioning
    (a comparison run tagging each candidate's researcher so findings can't bleed) without
    new machinery.

It is a STRUCTURING LAYER, never a replacement for the feeding-waves envelope
(`briefs.compose_feed_block`): the entries a stage reads are still serialized through that
envelope, which neutralizes boundary sentinels in attacker-influenced fetched text and
size-caps each summary. The ledger decides WHICH entries a stage sees; the envelope decides
how they are safely rendered.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from jbrain.agent.contracts import WebSource
from jbrain.agent.spawn import _ChildResult

# The visibility classes an entry can carry. A stage reads a set of these; the researcher
# isolation the owner asked for is the ABSENCE of a read — a gather child is spawned with no
# ledger access at all, so RESEARCH entries are never handed sideways to a sibling.
RESEARCH = "research"  # a gather/refill producer's findings — the material every later stage reads
ANALYSIS = "analysis"  # the cross-check analyst's structured read of the research set
DRAFT = "draft"  # the synthesized report the critique reads and the revise pass reads back
CRITIQUE = "critique"  # the draft critique folded into the revise pass


@dataclass(frozen=True)
class ScratchEntry:
    """One recorded stage output. A producer/review CHILD is recorded with its backing
    `_ChildResult` (so roster, `ok`, `truncated`, and its own reached sources ride along);
    a text-only stage output (the synthesized DRAFT) is recorded with `result=None`."""

    author: str  # the feed label — the child's row label, or "draft report"
    persona: str  # research / review / synthesis (mirrors `_ChildResult.persona`)
    stage: str  # gather | refill | analysis | draft | critique (provenance, not visibility)
    scope: str  # one of the visibility classes above
    text: str  # the findings summary / draft / critique body
    sources: tuple[WebSource, ...] = ()  # the real pages THIS entry's researcher reached
    result: _ChildResult | None = None  # the backing child; None for a text-only entry

    @property
    def ok(self) -> bool:
        """Whether this entry carries usable content — a successful child, or non-blank
        text for a text-only entry. Mirrors the `r.ok and r.summary.strip()` filter the
        feed composition applied to raw children, so an entry-based feed is byte-identical."""
        return self.result.ok if self.result is not None else bool(self.text.strip())

    @property
    def truncated(self) -> bool:
        return bool(self.result is not None and self.result.truncated)


class Scratchpad:
    """The append-ordered ledger threaded through one `_run`. Insertion order IS run order
    (gather → analyst → refill rounds → draft → critique), so `children()` reproduces the
    run's roster and `_collect_sources` over it keeps the first-seen citation numbering."""

    def __init__(self) -> None:
        self._entries: list[ScratchEntry] = []

    def add_child(self, result: _ChildResult, *, stage: str, scope: str) -> None:
        """Record one producer/review child as an entry in `scope`."""
        self._entries.append(
            ScratchEntry(
                author=result.label,
                persona=result.persona,
                stage=stage,
                scope=scope,
                text=result.summary,
                sources=tuple(result.web_sources),
                result=result,
            )
        )

    def add_children(self, results: Iterable[_ChildResult], *, stage: str, scope: str) -> None:
        for r in results:
            self.add_child(r, stage=stage, scope=scope)

    def add_text(self, *, author: str, persona: str, stage: str, scope: str, text: str) -> None:
        """Record a text-only stage output (the synthesized DRAFT) — no backing child, so it
        never enters the roster or the source registry, but it makes the DRAFT scope real for
        the critique/revise read model."""
        self._entries.append(
            ScratchEntry(author=author, persona=persona, stage=stage, scope=scope, text=text)
        )

    def read(self, scopes: Iterable[str]) -> list[ScratchEntry]:
        """The entries visible to a stage that reads `scopes`, in run order. This is the
        visibility model: a stage sees exactly the scopes it names and nothing else."""
        wanted = frozenset(scopes)
        return [e for e in self._entries if e.scope in wanted]

    def children(self, scopes: Iterable[str] | None = None) -> list[_ChildResult]:
        """The backing children of the entries in `scopes` (all scopes if None), in run
        order — for the source registry and the roster. Text-only entries are skipped."""
        entries = self._entries if scopes is None else self.read(scopes)
        return [e.result for e in entries if e.result is not None]
