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
    _linkify,
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


def test_linkify_wraps_first_occurrence_live_and_red() -> None:
    body = "Celine works at Globex and later left Globex. Tom married her.[2]"
    out = _linkify(body, [("Globex", "wiki:globex-ab12cd"), ("Tom", "redlink")])
    # The live target becomes a wiki link, the article-less one a redlink — first hit only,
    # and the trailing [2] citation marker is left untouched.
    assert "[Globex](wiki:globex-ab12cd)" in out
    assert out.count("[Globex](wiki:globex-ab12cd)") == 1
    assert "later left Globex." in out  # the second mention stays plain
    assert "[Tom](redlink)" in out
    assert out.endswith("married her.[2]")


def test_linkify_does_not_nest_an_anchor_inside_a_longer_one() -> None:
    # "Nair" must not be wrapped inside the "Nair Pediatrics" marker — longest-first + the
    # protected-span guard keep the markers flat.
    out = _linkify(
        "She founded Nair Pediatrics in Brookline.",
        [("Nair", "redlink"), ("Nair Pediatrics", "wiki:nair-pediatrics-9f")],
    )
    assert "[Nair Pediatrics](wiki:nair-pediatrics-9f)" in out
    assert "[Nair](redlink)" not in out


def test_linkify_leaves_prose_untouched_when_anchor_absent() -> None:
    # The grounded prose may phrase the relationship without the canonical name; no marker then.
    body = "Her younger sister is a physician."
    assert _linkify(body, [("Jordan Hale", "wiki:jordan-hale-77")]) == body


def test_linkify_respects_word_boundaries() -> None:
    # "May" the name must not match inside "Maybe"; the anchor is left unlinked here.
    assert _linkify("Maybe later.", [("May", "redlink")]) == "Maybe later."


def test_linkify_never_corrupts_an_existing_citation_marker() -> None:
    # An entity literally named "2" must NOT wrap inside the `[2]` citation marker (which would
    # corrupt both the citation and the link in the reader's parser).
    body = "Born here.[2] Lives there.[3]"
    assert _linkify(body, [("2", "redlink")]) == body


def test_linkify_refuses_anchors_carrying_marker_delimiters() -> None:
    # A name with `[`/`]`/`(`/`)` can't be embedded in `[label](target)` without breaking the
    # reader grammar (it would inject a phantom citation), so it's left unlinked.
    for bad in ("Section [9] team", "Acme (Holdings)", "a]b"):
        body = f"Saw {bad} today."
        assert _linkify(body, [(bad, "wiki:x-12")]) == body


def test_linkify_is_idempotent() -> None:
    # Re-running on already-linkified prose must not nest markers (the woven marker is protected).
    once = _linkify("Works at Globex.[1]", [("Globex", "wiki:globex-1a")])
    assert once == _linkify(once, [("Globex", "wiki:globex-1a")])
    assert once.count("(wiki:globex-1a)") == 1


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
