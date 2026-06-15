"""Unit tests for the pure graph-context ranking + rendering (B1).

No DB, no LLM: the retrieval layer (B2) supplies CandidateEntity objects; here we
prove the caps, the owner pinning, the identity-fact preference, and that the
rendered block matches the shape the integrate prompt was calibrated against —
crucially, that entity ids round-trip verbatim (the agent echoes them back).
"""

from datetime import UTC, datetime

from jbrain.analysis.graph_context import (
    CandidateEntity,
    FactLine,
    rank_and_bound,
    render_graph_context,
)


def _owner() -> CandidateEntity:
    return CandidateEntity(entity_id="owner-1", name="Me", kind="Person")


def _fact(
    predicate: str, value: str, *, kind: str = "relationship", year: int | None = None
) -> FactLine:
    return FactLine(
        predicate=predicate,
        qualifier="",
        kind=kind,
        assertion="asserted",
        value=value,
        valid_from=datetime(year, 1, 1, tzinfo=UTC) if year else None,
    )


def test_former_relationship_is_rendered_as_former():
    """A closed (valid_to) edge tells the integrator it is FORMER, so a past job
    is not mistaken for the current employer during resolution."""
    owner = CandidateEntity(
        entity_id="owner-1",
        name="Me",
        kind="Person",
        facts=(
            FactLine(
                predicate="worksFor",
                qualifier="",
                kind="relationship",
                assertion="asserted",
                value="US army",
                valid_to=datetime(2026, 6, 15, tzinfo=UTC),
            ),
        ),
    )
    out = render_graph_context([owner])
    assert "fact Me.worksFor -> US army [relationship/asserted], former (ended 2026-06-15)" in out


def test_empty_renders_empty_string():
    assert render_graph_context([]) == ""


def test_owner_is_rendered_first_with_its_id():
    ranked = rank_and_bound(_owner(), [])
    out = render_graph_context(ranked)
    assert out.startswith("Owner/author: entity id 'owner-1' name 'Me' (Person).")
    assert "Known entities:" not in out  # nothing else to show


def test_known_entity_block_carries_id_kind_alias_and_facts():
    celine = CandidateEntity(
        entity_id="ent-celine",
        name="Celine",
        kind="Person",
        aliases=("Cel", "Celine H"),
        facts=(_fact("gender", "female", kind="state"),),
    )
    out = render_graph_context(rank_and_bound(_owner(), [celine]))
    assert "Known entities:" in out
    assert "- id 'ent-celine' name 'Celine' (Person), alias 'Cel', 'Celine H'" in out
    assert "fact Celine.gender -> female [state/asserted]" in out


def test_owner_facts_render_under_the_owner_line():
    owner = CandidateEntity(
        entity_id="owner-1", name="Me", kind="Person", facts=(_fact("spouse", "Celine", year=2024),)
    )
    out = render_graph_context(rank_and_bound(owner, []))
    assert "fact Me.spouse -> Celine [relationship/asserted], valid_from 2024-01-01" in out


def test_owner_is_deduped_when_also_in_candidates():
    owner = _owner()
    dup = CandidateEntity(entity_id="owner-1", name="Me", kind="Person")
    ranked = rank_and_bound(
        owner, [dup, CandidateEntity(entity_id="ent-x", name="X", kind="Person")]
    )
    ids = [c.entity_id for c in ranked]
    assert ids == ["owner-1", "ent-x"]  # owner once, pinned first


def test_total_cap_keeps_owner_plus_first_n():
    cands = [CandidateEntity(entity_id=f"ent-{i}", name=f"E{i}", kind="Person") for i in range(20)]
    ranked = rank_and_bound(_owner(), cands, total_cap=5)
    assert len(ranked) == 5
    assert ranked[0].entity_id == "owner-1"  # owner never dropped
    assert [c.entity_id for c in ranked[1:]] == [f"ent-{i}" for i in range(4)]


def test_facts_per_entity_cap_prefers_identity_predicates():
    facts = (
        _fact("favoriteColor", "blue", kind="attribute"),
        _fact("mood", "good", kind="state"),
        _fact("spouse", "Celine"),  # identity predicate — must survive the cap
    )
    ent = CandidateEntity(entity_id="ent-1", name="Pat", kind="Person", facts=facts)
    ranked = rank_and_bound(_owner(), [ent], facts_per_entity=1)
    kept = ranked[1].facts
    assert len(kept) == 1 and kept[0].predicate == "spouse"


def test_non_identity_facts_break_ties_by_recency():
    facts = (
        _fact("note", "old", kind="state", year=2020),
        _fact("note", "new", kind="state", year=2025),
    )
    ent = CandidateEntity(entity_id="ent-1", name="Pat", kind="Person", facts=facts)
    kept = rank_and_bound(_owner(), [ent], facts_per_entity=1)[1].facts
    assert kept[0].value == "new"  # newest valid_from wins


def test_ids_round_trip_verbatim_uuid_style():
    # The agent echoes the id back; a uuid with hyphens must be preserved exactly.
    uid = "9f1c2e4a-0b3d-4f56-8a7b-1c2d3e4f5a6b"
    ent = CandidateEntity(entity_id=uid, name="Dr. Okafor", kind="Person")
    out = render_graph_context(rank_and_bound(_owner(), [ent]))
    assert f"- id '{uid}' name 'Dr. Okafor' (Person)" in out


def test_empty_value_renders_as_dash():
    ent = CandidateEntity(
        entity_id="ent-1", name="Pat", kind="Person", facts=(_fact("retired", "", kind="state"),)
    )
    out = render_graph_context(rank_and_bound(_owner(), [ent]))
    assert "fact Pat.retired -> - [state/asserted]" in out


def test_dotted_identity_predicate_survives_the_cap():
    # name.full is a canonical DOTTED predicate (stored predicate="name.full",
    # qualifier=""), the dispositive identity signal. It must outrank filler and
    # survive a tight per-entity fact cap, and render as name.full.
    facts = (
        _fact("favoriteColor", "blue", kind="attribute"),
        _fact("name.full", "Patricia Vance", kind="attribute"),
    )
    ent = CandidateEntity(entity_id="ent-1", name="Pat", kind="Person", facts=facts)
    kept = rank_and_bound(_owner(), [ent], facts_per_entity=1)[1].facts
    assert len(kept) == 1 and kept[0].predicate == "name.full"
    out = render_graph_context(rank_and_bound(_owner(), [ent], facts_per_entity=1))
    assert "fact Pat.name.full -> Patricia Vance [attribute/asserted]" in out


def test_qualifier_bearing_name_predicate_is_identity_and_renders_dotted():
    # name.nickname.kids = predicate "name.nickname" + qualifier "kids": the BARE
    # predicate is the identity match, the rendered edge appends the qualifier.
    fact = FactLine(
        predicate="name.nickname",
        qualifier="kids",
        kind="attribute",
        assertion="asserted",
        value="Dad",
    )
    ent = CandidateEntity(
        entity_id="ent-1",
        name="Pat",
        kind="Person",
        facts=(_fact("mood", "good", kind="state"), fact),
    )
    kept = rank_and_bound(_owner(), [ent], facts_per_entity=1)[1].facts
    assert kept[0].predicate == "name.nickname"  # identity beat the filler state
    out = render_graph_context(rank_and_bound(_owner(), [ent], facts_per_entity=1))
    assert "fact Pat.name.nickname.kids -> Dad [attribute/asserted]" in out


def test_newline_in_value_or_name_is_collapsed_to_stay_one_line():
    ent = CandidateEntity(
        entity_id="ent-1",
        name="Pat\nVance",
        kind="Person",
        facts=(_fact("note", "line1\nline2", kind="state"),),
    )
    out = render_graph_context(rank_and_bound(_owner(), [ent]))
    assert "name 'Pat Vance'" in out
    assert "fact Pat Vance.note -> line1 line2 [state/asserted]" in out
    # every fact stays on exactly one line (no orphaned fragment)
    assert all(line.strip() for line in out.splitlines())


def test_zero_caps_are_handled():
    cands = [CandidateEntity(entity_id="ent-1", name="X", kind="Person", facts=(_fact("a", "b"),))]
    assert rank_and_bound(_owner(), cands, total_cap=0) == []  # nothing, not even owner
    kept = rank_and_bound(_owner(), cands, facts_per_entity=0)[1].facts
    assert kept == ()
