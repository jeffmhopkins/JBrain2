"""The recurrence parser — the handler step that replaced a model field.

R1 of `docs/plans/AGENT_INGEST_REWRITE.md` §3.2. R0 measured `repeats` unfillable at any
sharpness (0 parseable RRULEs in 113 values, 0 in 115 sharpened, and the phrase spelling
said what the note said 28 times in 118) and measured the alternative in the same runs:
parsing the model's own attested `quote` recovered the right rule on 198 of 200. So the
reading carries no recurrence field and this reads the rule out of the span.

The probe's reference parser was written against the five phrasings it was scored on, and
the plan says so: it is evidence the information survives, not an accuracy estimate. What
is tested here is the production parser against a wider corpus, and — the half the probe
could not have — **what it must REFUSE**. Its five notes all recur, so a bare weekday could
safely mean a weekly rule; in a real corpus "coffee with Dana on Tuesday" is one
appointment, and a phantom every-Tuesday event on the owner's calendar is worse than no
recurrence at all.
"""

import pytest

from jbrain.analysis.recurrence import parse_recurrence, parse_rrule

# The five R0 scored, first, so the corpus starts where the measurement did.
R0_NOTES = [
    (
        "Signed up at the Y on Oak St. Gym every Tuesday and Thursday at 6am.",
        "FREQ=WEEKLY;BYDAY=TU,TH",
    ),
    ("Book club meets the first Monday of the month at Dana's place.", "FREQ=MONTHLY;BYDAY=1MO"),
    (
        "Therapy with Dr. Nunez every other week, Wednesdays at 4.",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE",
    ),
    ("Standup at 9:15 on weekdays. Kendra runs it now.", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
]


@pytest.mark.parametrize(("quote", "rrule"), R0_NOTES)
def test_the_phrasings_r0_measured_are_recovered(quote: str, rrule: str) -> None:
    found = parse_recurrence(quote)
    assert found is not None
    assert found.rrule == rrule


@pytest.mark.parametrize(
    ("quote", "rrule"),
    [
        # Frequency adverbs — what the model wrote INSTEAD of a rule, and what a note
        # says when the schedule really is that coarse.
        ("rent is due monthly", "FREQ=MONTHLY"),
        ("takes the statin nightly", "FREQ=DAILY"),
        ("quarterly estimated tax payment", "FREQ=MONTHLY;INTERVAL=3"),
        ("bi-weekly paycheck", "FREQ=WEEKLY;INTERVAL=2"),
        ("annual physical with Dr. Lowe", "FREQ=YEARLY"),
        # Plural weekdays, which stand alone without an `every`.
        ("yoga Mondays and Wednesdays", "FREQ=WEEKLY;BYDAY=MO,WE"),
        ("Spanish class Tuesdays", "FREQ=WEEKLY;BYDAY=TU"),
        # Intervals, spelled both ways.
        ("trash pickup every other Wednesday", "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE"),
        ("infusion every 3 weeks", "FREQ=WEEKLY;INTERVAL=3"),
        ("physio every two days", "FREQ=DAILY;INTERVAL=2"),
        # Ordinals of the month, including the negative one.
        ("payday the last Friday of the month", "FREQ=MONTHLY;BYDAY=-1FR"),
        ("board meets the third Thursday of every month", "FREQ=MONTHLY;BYDAY=3TH"),
        # Ranges — expanded before anything else reads the span, or the range's own
        # "through" reads as an end date and the rule is discarded for a bound the note
        # never stated.
        ("in the office every Monday through Friday", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        ("weekends at the cabin", "FREQ=WEEKLY;BYDAY=SA,SU"),
    ],
)
def test_the_wider_corpus_parses(quote: str, rrule: str) -> None:
    found = parse_recurrence(quote)
    assert found is not None, quote
    assert found.rrule == rrule


@pytest.mark.parametrize(
    "quote",
    [
        # A single occasion. This is the refusal the probe never had to make, and the one
        # that matters most on a real corpus: every one of these would otherwise become a
        # repeating appointment nobody asked for.
        "coffee with Dana at Ritual this morning",
        "I saw her Tuesday",
        "lunch with Sam Tues",
        "flight lands Friday night",
        "we went to the beach last weekend",
        "swim practice Monday through Friday",
        # A closed interval, which `when_end` already owns and this must not re-read.
        "we lived in Oakland from 2019 until 2023",
        # Nothing at all.
        "",
        "   ",
        "she is allergic to shellfish",
    ],
)
def test_a_span_with_no_rule_in_it_is_refused(quote: str) -> None:
    assert parse_recurrence(quote) is None


@pytest.mark.parametrize(
    "quote",
    [
        "Spanish class Tuesdays until March",
        "gym every Tuesday and Thursday through the end of term",
        "physio every day for two weeks",
        "standup on weekdays until the launch",
        "chemo every three weeks for the next six months",
    ],
)
def test_a_bounded_rule_is_discarded_whole(quote: str) -> None:
    """The sharpest decision in the parser, and the one place it deliberately refuses
    something the probe's reference parser admitted.

    The frequency of a bounded rule is easy to recover; resolving the BOUND to the date an
    RRULE `UNTIL` needs is date inference, which is exactly what this repo refuses to do
    from a phrase (`_close_interval`'s "not a date, no end"). Emitting the rule WITHOUT its
    bound would be worse than emitting nothing — an unbounded rule states something the
    note does not say, and it says it on the owner's calendar forever — so the whole rule
    is discarded and the fact commits with the dates it had."""
    assert parse_recurrence(quote) is None


def test_two_different_rules_in_one_span_are_refused() -> None:
    """A span stating two schedules is a fact the reading should have split. Picking one
    of them silently is the shape of failure the discard discipline exists to avoid."""
    assert parse_recurrence("book club the first Monday of the month, and yoga Wednesdays") is None
    # Two clauses that say the SAME thing are not a conflict.
    found = parse_recurrence("weekdays — every weekday, without fail")
    assert found is not None and found.rrule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"


def test_the_phrase_is_the_span_the_rule_was_read_out_of() -> None:
    """The phrase becomes the temporal token's `surface_phrase`, which `_locate` anchors
    in the note's chunks — so it has to be the note's own words, not a rendering of the
    rule."""
    found = parse_recurrence("Gym EVERY Tuesday and Thursday at 6am")
    assert found is not None
    assert found.phrase == "every tuesday and thursday"


def test_every_rule_this_builds_is_a_valid_rrule() -> None:
    """The backstop that lets the clause builders stay simple. `appointments.rrule` is
    written and read as plain text everywhere on the box, so a malformed rule would reach
    the .ics feed the owner's phone subscribes to with nothing in between to catch it."""
    for quote, _ in R0_NOTES:
        found = parse_recurrence(quote)
        assert found is not None
        assert parse_rrule(found.rrule) is not None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "weekly",
        "every Tuesday and Thursday",
        "FREQ=FORTNIGHTLY",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=WEEKLY;UNTIL=MARCH",
        "FREQ=WEEKLY;INTERVAL=0",
        "FREQ=WEEKLY;COUNT=4;UNTIL=20270301",
        "FREQ=WEEKLY;NONSENSE=1",
        "BYDAY=TU",
    ],
)
def test_parse_rrule_refuses_what_is_not_a_rule(value: str) -> None:
    """Including the model's own two spellings from R0 — the coarse English word and the
    phrase — because a validator that admitted either would have made the measurement
    read as a pass."""
    assert parse_rrule(value) is None


def test_parse_rrule_reads_a_real_rule_with_or_without_its_prefix() -> None:
    assert parse_rrule("FREQ=WEEKLY;BYDAY=TU,TH") == {"FREQ": "WEEKLY", "BYDAY": "TU,TH"}
    assert parse_rrule("RRULE:FREQ=MONTHLY;BYDAY=-1FR;UNTIL=20270301") == {
        "FREQ": "MONTHLY",
        "BYDAY": "-1FR",
        "UNTIL": "20270301",
    }
