"""The relationship-word → predicate vocabulary behind the relate tool."""

from jbrain.analysis.relationships import predicate_candidates


def test_gendered_spouse_words_map_to_the_spouse_predicate() -> None:
    for word in ("wife", "husband", "spouse", "Wife", "  wife's "):
        assert "spouse" in predicate_candidates(word)


def test_kinship_words_map_to_their_predicates() -> None:
    assert "parent" in predicate_candidates("mom")
    assert "parent" in predicate_candidates("father")
    assert "children" in predicate_candidates("son")
    assert "sibling" in predicate_candidates("sister")


def test_work_words_map_to_the_owners_outbound_edge() -> None:
    # "my boss" is Me.reportsTo → boss, not Me.manages.
    boss = predicate_candidates("boss")
    assert "reports_to" in boss and "reportsto" in boss
    assert "manages" not in boss
    employer = predicate_candidates("employer")
    assert "works_for" in employer and "worksfor" in employer


def test_an_exact_predicate_name_passes_through() -> None:
    # An unknown word is an attractor, never a gate: it still matches itself,
    # so a literal predicate like "owns" or "memberOf" works directly.
    assert "owns" in predicate_candidates("owns")
    cands = predicate_candidates("memberOf")
    assert "memberof" in cands  # separator-free, lowercased


def test_an_empty_or_blank_word_yields_no_candidates() -> None:
    assert predicate_candidates("") == ()
    assert predicate_candidates("   ") == ()
