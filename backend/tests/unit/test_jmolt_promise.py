"""Promise extraction (JMOLT_LEDGER_ENGINE_PLAN.md, S2).

The point is an obligation opened whether or not the model remembers making it — so the tests
that matter are about what it must NOT catch. A false positive is an obligation jmolt did not
make, which it can abandon in one move; a rule that fires on everything makes the composed
brief noise, and noise in the brief is the failure the ledger exists to fix.
"""

import pytest

from jbrain.agent.jmolt_promise import MAX_SUBJECT, find_promises


def _subjects(text: str) -> list[str]:
    return [p.subject for p in find_promises(text)]


@pytest.mark.parametrize(
    "sentence",
    [
        "I'll come back to this tomorrow.",
        "I’ll come back to this tomorrow.",  # a curly apostrophe is the common case in posts
        "I will read the paper before I say more.",
        "I plan to test this against the logs.",
        "I intend to follow up with them.",
        "I am going to check whether that holds.",
        "I'm going to try it the other way.",
        "Next time I read that submolt I'll count them.",
        "Tomorrow I want to see if it repeats.",
        "Let me check that before I answer.",
    ],
)
def test_a_conventional_promise_is_found(sentence: str) -> None:
    assert _subjects(f"Some throat-clearing first. {sentence}") == [sentence]


@pytest.mark.parametrize(
    "sentence",
    [
        "You should look into this.",  # advice to someone else, not a commitment
        "They said they'll come back to it.",
        "It will probably rain.",
        "I'll never understand that.",
        "I won't be doing that again.",
        "If I'll be honest, it was thin.",
        "Someone will have to check.",
    ],
)
def test_something_that_is_not_a_commitment_is_left_alone(sentence: str) -> None:
    """An agent that opened an obligation from every suggestion it made would be taking on
    the whole platform's homework."""
    assert _subjects(sentence) == []


def test_the_verbatim_sentence_is_kept_as_evidence() -> None:
    """The subject is a handle the composer prints; the quote is what makes the obligation
    checkable, and it has to survive intact."""
    [promise] = find_promises("I'll come back to this tomorrow with something better.")
    assert promise.quote == "I'll come back to this tomorrow with something better."


def test_the_same_promise_twice_in_one_post_is_one_obligation() -> None:
    """Two identical obligations would print as two lines in tomorrow's brief — the exact
    repetition this engine exists to stop showing itself."""
    text = "I'll come back to this. Some other thought. I'll come back to this."
    assert _subjects(text) == ["I'll come back to this."]


def test_several_distinct_promises_come_back_in_the_order_they_were_made() -> None:
    text = "I'll read the paper. Then a thought. I plan to test it against the logs."
    assert _subjects(text) == ["I'll read the paper.", "I plan to test it against the logs."]


def test_a_long_promise_is_clipped_to_a_handle_at_a_word_boundary() -> None:
    """A truncation, not a summary. A summary is prose about prose, which is what the ledger
    exists to avoid."""
    # Long enough to need clipping to a handle, short enough to be a real sentence rather
    # than the parse failure the runaway cap rejects.
    long = "I'll " + " ".join(["something"] * 25) + "."
    [promise] = find_promises(long)
    assert len(promise.subject) <= MAX_SUBJECT + 1  # the ellipsis
    assert promise.subject.endswith("…")
    assert not promise.subject.endswith("someth…")  # cut between words, not through one
    assert promise.quote == long  # and the evidence is still whole


def test_a_runaway_sentence_is_a_parse_failure_not_a_commitment() -> None:
    assert find_promises("I'll " + "x" * 500) == []


def test_empty_and_absent_text_are_quiet() -> None:
    assert find_promises("") == []
    assert find_promises("   \n  ") == []


def test_a_promise_split_across_lines_is_still_found() -> None:
    """Posts are written with line breaks, not tidy sentences."""
    assert _subjects("A thought about weeks\nI'll come back to this tomorrow") == [
        "I'll come back to this tomorrow"
    ]
