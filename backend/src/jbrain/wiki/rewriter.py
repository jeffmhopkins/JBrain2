"""The live LLM rewriter + grounding gate (docs/PHASE6_WIKI_PLAN.md §3 steps 4-6, Wave C2b).

The rewriter turns a `SourcedEntity` into a `PlannedArticle` by driving the LLM adapter per the
type guide, then passes every drafted clause through a **grounding gate** before it is written:

- **Rewrite** (`router.complete(task="wiki.rewrite", json_schema=…)`): the model writes
  type-guided sections as clauses, each citing one or more of the PROVIDED claim ids — it can
  cite nothing else, so a claim out of the sourced set can't appear.
- **Resolve + same-domain check**: each clause's claim ids are mapped back to the sourced claims;
  a citation whose domain ≠ the section's domain is dropped, and a clause left with no citation is
  dropped (the "drop any uncitable claim" rule).
- **Ground** (`router.complete(task="wiki.ground", …)`): a verifier asserts each surviving clause
  is entailed by its cited chunk(s) and consistent with the entity's current fact set; the
  **entity graph wins** (a clause contradicting the facts is dropped). Fail-closed: a verifier
  error raises, so the entity stays dirty and is retried rather than published unverified.

Both calls are metered against the SEPARATE wiki-build budget (`WikiBuildGate`); the builder is
refused (fail-closed) before it spends when the kill-switch is on or the day's budget is gone.
The LLM is injected (a `FakeLlmClient`-backed router in CI), never a provider SDK.
"""

from __future__ import annotations

from typing import Any

from jbrain.db.session import SessionContext
from jbrain.llm.router import LlmRouter
from jbrain.settings_store import SqlSettingsStore
from jbrain.wiki.budget import WikiBudgetExceeded, WikiBuildGate
from jbrain.wiki.builder import (
    Claim,
    PlannedArticle,
    PlannedCitation,
    PlannedLink,
    PlannedSection,
    SourcedEntity,
    WikiGroundingError,
)
from jbrain.wiki.typeguides import STYLE_PROMPT, guide_for

# Conservative per-build estimate checked against the remaining budget before spending; the real
# cost is recorded after from the two calls' reported usage.
WIKI_BUILD_ESTIMATE_TOKENS = 8_000

_REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["lead_summary", "sections"],
    "properties": {
        "lead_summary": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["heading", "domain", "clauses"],
                "properties": {
                    "heading": {"type": "string"},
                    "domain": {"type": "string"},
                    "clauses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["text", "claim_ids"],
                            "properties": {
                                "text": {"type": "string"},
                                "claim_ids": {"type": "array", "items": {"type": "integer"}},
                            },
                        },
                    },
                },
            },
        },
    },
}

_GROUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "supported"],
                "properties": {
                    "index": {"type": "integer"},
                    "supported": {"type": "boolean"},
                },
            },
        }
    },
}


class LlmRewriter:
    """The `Rewriter` (live). Holds the LLM router, the budget gate, and the system context."""

    def __init__(
        self,
        router: LlmRouter,
        *,
        settings: SqlSettingsStore,
        ctx: SessionContext,
    ):
        self._router = router
        self._gate = WikiBuildGate(settings)
        self._settings = settings
        self._ctx = ctx

    async def plan(self, sourced: SourcedEntity) -> PlannedArticle:
        decision = await self._gate.check(self._ctx, estimated_tokens=WIKI_BUILD_ESTIMATE_TOKENS)
        if not decision.allowed:
            raise WikiBudgetExceeded(decision.reason)
        if not sourced.claims:
            return PlannedArticle(lead_summary="", sections=[])

        # _draft and _ground each record their own spend the instant the router returns — BEFORE
        # any validation — so a fail-closed grounding raise still meters the tokens it burned.
        draft = await self._draft(sourced)
        resolved = self._resolve(sourced, draft)
        kept = await self._ground(sourced, resolved)
        return self._assemble(draft, kept)

    # ---- the two LLM calls ---------------------------------------------------------------

    async def _draft(self, sourced: SourcedEntity) -> dict[str, Any]:
        guide = guide_for(sourced.kind)
        section_plan = "\n".join(
            f"- {s.name} (domain={s.domain}): include if {s.include_if}" for s in guide.sections
        )
        claims_text = "\n".join(
            f"[{i}] (domain={c.domain_code}) {c.statement}\n    chunk: {c.chunk_text}"
            for i, c in enumerate(sourced.claims)
        )
        system = (
            f"{STYLE_PROMPT}\n\nLead: {guide.lead}\nStyle: {guide.style}\n"
            "Write only sections from this plan (omit a section with no supporting claims); a "
            "section's domain MUST match the domain of the claims it uses. Each clause must cite "
            "one or more claim ids (the [i] below) in claim_ids — cite nothing outside this list.\n"
            f"Section plan:\n{section_plan}"
        )
        user_text = (
            f"Subject: {sourced.name} (kind={sourced.kind},"
            f" primary domain={sourced.domain_code}).\nClaims:\n{claims_text}"
        )
        result = await self._router.complete(
            "wiki.rewrite", system=system, user_text=user_text, json_schema=_REWRITE_SCHEMA
        )
        await self._gate.record_spend(
            self._ctx, tokens=result.usage.input_tokens + result.usage.output_tokens
        )
        return (
            result.parsed
            if isinstance(result.parsed, dict)
            else {"lead_summary": "", "sections": []}
        )

    async def _ground(self, sourced: SourcedEntity, resolved: list[_Clause]) -> list[_Clause]:
        if not resolved:
            return []
        facts = "\n".join(f"- {c.statement}" for c in sourced.claims)
        clauses = "\n".join(
            f"[{i}] {cl.text}\n    cited chunks: {' || '.join(cl.chunk_texts)}"
            for i, cl in enumerate(resolved)
        )
        system = (
            "You are a grounding verifier. For each clause, return supported=true ONLY if the "
            "clause is entailed by its cited chunk text(s) AND not contradicted by the subject's "
            "current facts. The entity graph wins: a clause contradicting a current fact is "
            "supported=false. Be strict; if in doubt, supported=false."
        )
        user_text = f"Current facts:\n{facts}\n\nClauses:\n{clauses}"
        result = await self._router.complete(
            "wiki.ground", system=system, user_text=user_text, json_schema=_GROUND_SCHEMA
        )
        await self._gate.record_spend(
            self._ctx, tokens=result.usage.input_tokens + result.usage.output_tokens
        )
        if not isinstance(result.parsed, dict):
            raise WikiGroundingError("grounding verifier returned no parseable verdict")
        supported = {
            v["index"]
            for v in result.parsed.get("verdicts", [])
            if isinstance(v, dict)
            and v.get("supported") is True
            and isinstance(v.get("index"), int)
        }
        return [cl for i, cl in enumerate(resolved) if i in supported]

    # ---- pure assembly -------------------------------------------------------------------

    def _resolve(self, sourced: SourcedEntity, draft: dict[str, Any]) -> list[_Clause]:
        """Flatten the draft's sections into clauses with their resolved same-domain citations.
        Drops out-of-range ids, cross-domain citations, and clauses left uncitable."""
        out: list[_Clause] = []
        for section in draft.get("sections", []):
            if not isinstance(section, dict):
                continue
            domain = section.get("domain", "")
            heading = section.get("heading", "")
            for clause in section.get("clauses", []):
                if not isinstance(clause, dict):
                    continue
                cited: list[Claim] = []
                for cid in clause.get("claim_ids", []):
                    if isinstance(cid, int) and 0 <= cid < len(sourced.claims):
                        claim = sourced.claims[cid]
                        if claim.domain_code == domain:  # firewall: citation domain = section
                            cited.append(claim)
                if not cited:
                    continue  # drop an uncitable clause
                out.append(
                    _Clause(
                        heading=heading,
                        domain=domain,
                        text=str(clause.get("text", "")),
                        claims=cited,
                    )
                )
        return out

    def _assemble(self, draft: dict[str, Any], kept: list[_Clause]) -> PlannedArticle:
        """Build the final PlannedArticle from the surviving clauses, numbering [n] article-wide
        and grouping clauses back into their sections (in first-seen order)."""
        order: list[tuple[str, str]] = []
        by_section: dict[tuple[str, str], list[_Clause]] = {}
        for cl in kept:
            key = (cl.heading, cl.domain)
            if key not in by_section:
                by_section[key] = []
                order.append(key)
            by_section[key].append(cl)

        sections: list[PlannedSection] = []
        seq = 0
        for heading, domain in order:
            parts: list[str] = []
            citations: list[PlannedCitation] = []
            links: list[PlannedLink] = []
            for cl in by_section[(heading, domain)]:
                markers: list[str] = []
                for claim in cl.claims:
                    seq += 1
                    markers.append(f"[{seq}]")
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
                                to_entity_id=claim.object_entity_id, anchor=claim.object_name or ""
                            )
                        )
                parts.append(f"{cl.text.rstrip('.')}.{''.join(markers)}")
            sections.append(
                PlannedSection(
                    heading=heading,
                    domain_code=domain,
                    body=" ".join(parts),
                    summary=f"{heading}.",
                    citations=citations,
                    links=links,
                )
            )
        lead = str(draft.get("lead_summary", "")) if sections else ""
        return PlannedArticle(lead_summary=lead, sections=sections)


class _Clause:
    """A drafted clause after citation resolution (internal to the rewriter)."""

    __slots__ = ("heading", "domain", "text", "claims", "chunk_texts")

    def __init__(self, *, heading: str, domain: str, text: str, claims: list[Claim]):
        self.heading = heading
        self.domain = domain
        self.text = text
        self.claims = claims
        self.chunk_texts = [c.chunk_text for c in claims]
