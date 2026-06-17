"""Unit coverage for the wiki builder's pure logic (no DB): the StubRewriter's deterministic
plan, article-wide citation numbering, per-domain sectioning, link emission, and the
notability gate."""

import uuid

import pytest

from jbrain.wiki.builder import (
    NOTABILITY_MIN_FACTS,
    Claim,
    SourcedEntity,
    StubRewriter,
    is_notable,
)


def _claim(domain: str, statement: str, *, object_id: uuid.UUID | None = None) -> Claim:
    return Claim(
        statement=statement,
        domain_code=domain,
        chunk_id=uuid.uuid4(),
        note_id=uuid.uuid4(),
        fact_id=uuid.uuid4(),
        object_entity_id=object_id,
        object_name="Obj" if object_id else None,
    )


def _sourced(claims: list[Claim], *, domain: str = "general", notes: int = 0) -> SourcedEntity:
    return SourcedEntity(
        entity_id=uuid.uuid4(),
        name="Subj",
        kind="Person",
        domain_code=domain,
        claims=claims,
        note_count=notes or len(claims),
    )


def test_notability_gate() -> None:
    assert is_notable(_sourced([_claim("general", "a")], notes=2)) is True  # 2 notes
    assert is_notable(_sourced([_claim("general", f"c{i}") for i in range(3)])) is True  # 3 facts
    assert is_notable(_sourced([_claim("general", "a")], notes=1)) is False
    # The threshold is the documented constant, not a magic number.
    facts = [_claim("general", f"c{i}") for i in range(NOTABILITY_MIN_FACTS)]
    assert is_notable(_sourced(facts, notes=1)) is True


async def test_stub_groups_by_domain_with_entity_domain_first() -> None:
    sourced = _sourced(
        [
            _claim("health", "has an allergy"),
            _claim("general", "lives in town"),
            _claim("finance", "owns shares"),
        ],
        domain="general",
    )
    plan = await StubRewriter().plan(sourced)
    # The entity's own domain (general) leads; the rest are alphabetical.
    assert [s.domain_code for s in plan.sections] == ["general", "finance", "health"]
    assert [s.heading for s in plan.sections] == ["Overview", "Finances", "Health"]


async def test_stub_numbers_citations_article_wide_and_matches_body() -> None:
    sourced = _sourced(
        [_claim("general", "first"), _claim("general", "second"), _claim("health", "third")]
    )
    plan = await StubRewriter().plan(sourced)
    seqs = [c.seq for s in plan.sections for c in s.citations]
    assert seqs == [1, 2, 3]  # unique, article-wide, in order
    # Each section body carries the [n] markers for its own citations.
    general = plan.sections[0]
    assert "[1]" in general.body and "[2]" in general.body
    health = plan.sections[1]
    assert "[3]" in health.body


async def test_stub_emits_links_for_relationship_facts_only() -> None:
    obj = uuid.uuid4()
    sourced = _sourced([_claim("general", "knows", object_id=obj), _claim("general", "plain")])
    plan = await StubRewriter().plan(sourced)
    links = [link for s in plan.sections for link in s.links]
    assert len(links) == 1
    assert links[0].to_entity_id == obj


async def test_stub_lead_summary_names_the_entity() -> None:
    plan = await StubRewriter().plan(_sourced([_claim("general", "x")], notes=4))
    assert "Subj" in plan.lead_summary
    assert "4 note" in plan.lead_summary


@pytest.mark.parametrize("kind", ["Person", "Organization", "Place"])
async def test_stub_handles_each_kind(kind: str) -> None:
    sourced = SourcedEntity(
        entity_id=uuid.uuid4(),
        name="X",
        kind=kind,
        domain_code="general",
        claims=[_claim("general", "a")],
        note_count=2,
    )
    plan = await StubRewriter().plan(sourced)
    assert kind.lower() in plan.lead_summary.lower()
