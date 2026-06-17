"""The live wiki rewriter + grounding gate (Wave C2b), with a faked LLM router and an in-memory
settings store: rewrite → resolve (cross-domain/uncitable drop) → ground (drop unsupported,
fail-closed) → assemble (article-wide [n]), plus the budget refusal path."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain.db.session import SessionContext
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.settings_store import WIKI_BUILD_KILL_SWITCH_KEY, WIKI_BUILD_SPEND_PREFIX
from jbrain.wiki.budget import WikiBudgetExceeded
from jbrain.wiki.builder import Claim, SourcedEntity, WikiGroundingError
from jbrain.wiki.rewriter import LlmRewriter

CTX = SessionContext(principal_id="owner", principal_kind="owner")


class FakeSettings:
    """In-memory SqlSettingsStore stand-in: compose the real typed wiki getters over a dict so
    their fail-closed coercion is exercised, not bypassed."""

    def __init__(self, store: dict[str, Any] | None = None):
        self._store = dict(store or {})

    async def get(self, ctx: SessionContext, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    async def upsert(self, ctx: SessionContext, key: str, value: Any) -> None:
        self._store[key] = value

    from jbrain.settings_store import SqlSettingsStore as _S

    wiki_build_kill_switch = _S.wiki_build_kill_switch
    wiki_build_daily_budget = _S.wiki_build_daily_budget
    wiki_build_spent_today = _S.wiki_build_spent_today
    record_wiki_build_spend = _S.record_wiki_build_spend


def _claim(domain: str, statement: str, *, obj: uuid.UUID | None = None) -> Claim:
    return Claim(
        statement=statement,
        domain_code=domain,
        chunk_id=uuid.uuid4(),
        note_id=uuid.uuid4(),
        fact_id=uuid.uuid4(),
        object_entity_id=obj,
        object_name="Obj" if obj else None,
        chunk_text=f"chunk for {statement}",
    )


def _sourced(claims: list[Claim]) -> SourcedEntity:
    return SourcedEntity(
        entity_id=uuid.uuid4(),
        name="Priya",
        kind="Person",
        domain_code="general",
        claims=claims,
        note_count=len(claims),
    )


def _rewriter(rewrite: dict[str, Any], ground: dict[str, Any], store=None) -> LlmRewriter:
    fake = FakeLlmClient(responses=[json.dumps(rewrite), json.dumps(ground)])
    router = LlmRouter(
        {"xai": fake},
        {"wiki.rewrite": ("xai", "m"), "wiki.ground": ("xai", "m")},
    )
    return LlmRewriter(router, settings=FakeSettings(store), ctx=CTX)  # type: ignore[arg-type]


def _all_supported(n: int) -> dict[str, Any]:
    return {"verdicts": [{"index": i, "supported": True} for i in range(n)]}


async def test_happy_path_assembles_article_wide_citations() -> None:
    sourced = _sourced([_claim("general", "founded the clinic"), _claim("general", "lives here")])
    rewrite = {
        "lead_summary": "Priya is a pediatrician.",
        "sections": [
            {
                "heading": "Overview",
                "domain": "general",
                "clauses": [
                    {"text": "She founded the clinic", "claim_ids": [0]},
                    {"text": "She lives here", "claim_ids": [1]},
                ],
            }
        ],
    }
    plan = await _rewriter(rewrite, _all_supported(2)).plan(sourced)
    assert plan.lead_summary == "Priya is a pediatrician."
    assert len(plan.sections) == 1
    sec = plan.sections[0]
    assert sec.domain_code == "general"
    assert "[1]" in sec.body and "[2]" in sec.body  # article-wide numbering
    assert [c.seq for c in sec.citations] == [1, 2]


async def test_cross_domain_citation_is_dropped() -> None:
    # A general section clause that cites a HEALTH claim is uncitable → the clause is dropped.
    sourced = _sourced([_claim("general", "ok claim"), _claim("health", "allergy")])
    rewrite = {
        "lead_summary": "x",
        "sections": [
            {
                "heading": "Overview",
                "domain": "general",
                "clauses": [
                    {"text": "good", "claim_ids": [0]},
                    {"text": "leaky", "claim_ids": [1]},  # health claim in a general section
                ],
            }
        ],
    }
    plan = await _rewriter(rewrite, _all_supported(1)).plan(sourced)
    assert len(plan.sections) == 1
    assert len(plan.sections[0].citations) == 1
    assert "good" in plan.sections[0].body
    assert "leaky" not in plan.sections[0].body


async def test_ungrounded_clause_is_dropped() -> None:
    sourced = _sourced([_claim("general", "a"), _claim("general", "b")])
    rewrite = {
        "lead_summary": "x",
        "sections": [
            {
                "heading": "Overview",
                "domain": "general",
                "clauses": [
                    {"text": "grounded", "claim_ids": [0]},
                    {"text": "hallucinated", "claim_ids": [1]},
                ],
            }
        ],
    }
    ground = {"verdicts": [{"index": 0, "supported": True}, {"index": 1, "supported": False}]}
    plan = await _rewriter(rewrite, ground).plan(sourced)
    assert "grounded" in plan.sections[0].body
    assert "hallucinated" not in plan.sections[0].body


async def test_all_clauses_dropped_yields_no_sections() -> None:
    sourced = _sourced([_claim("general", "a")])
    rewrite = {
        "lead_summary": "x",
        "sections": [
            {
                "heading": "Overview",
                "domain": "general",
                "clauses": [{"text": "c", "claim_ids": [0]}],
            }
        ],
    }
    plan = await _rewriter(rewrite, {"verdicts": [{"index": 0, "supported": False}]}).plan(sourced)
    assert plan.sections == []
    assert plan.lead_summary == ""  # no sections → no lead


async def test_empty_claims_returns_empty_article() -> None:
    plan = await _rewriter({"lead_summary": "x", "sections": []}, {"verdicts": []}).plan(
        _sourced([])
    )
    assert plan.sections == []


async def test_kill_switch_refuses_before_any_spend() -> None:
    rw = _rewriter(
        {"lead_summary": "x", "sections": []},
        {"verdicts": []},
        store={WIKI_BUILD_KILL_SWITCH_KEY: True},
    )
    with pytest.raises(WikiBudgetExceeded):
        await rw.plan(_sourced([_claim("general", "a")]))


async def test_exhausted_budget_refuses() -> None:
    day = datetime.now(UTC).date().isoformat()
    rw = _rewriter(
        {"lead_summary": "x", "sections": []},
        {"verdicts": []},
        store={WIKI_BUILD_SPEND_PREFIX + day: 10_000_000},
    )
    with pytest.raises(WikiBudgetExceeded):
        await rw.plan(_sourced([_claim("general", "a")]))


async def test_grounding_failclosed_on_non_dict_verdict() -> None:
    # The ground call returns a JSON list (valid JSON, wrong shape) → the rewriter's guard
    # fail-closes with a raise rather than publishing unverified prose.
    rewrite = {
        "lead_summary": "x",
        "sections": [
            {
                "heading": "Overview",
                "domain": "general",
                "clauses": [{"text": "c", "claim_ids": [0]}],
            }
        ],
    }
    fake = FakeLlmClient(responses=[json.dumps(rewrite), "[]"])
    router = LlmRouter({"xai": fake}, {"wiki.rewrite": ("xai", "m"), "wiki.ground": ("xai", "m")})
    settings = FakeSettings()
    rw = LlmRewriter(router, settings=settings, ctx=CTX)  # type: ignore[arg-type]
    with pytest.raises(WikiGroundingError):
        await rw.plan(_sourced([_claim("general", "a")]))
    # Fail-closed still meters: the draft + ground tokens burned are recorded against the budget.
    day = datetime.now(UTC).date().isoformat()
    assert await settings.wiki_build_spent_today(CTX, day=day) > 0
