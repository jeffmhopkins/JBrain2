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

# Four of the five notes R0 scored (the fifth, "Tuesdays until March", is BOUNDED and is
# a refusal here — see `test_a_bounded_rule_is_discarded_whole`), first, so the corpus
# starts where the measurement did.
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
        # A bare `every <unit>`, which had NO case here at all and for `month` was
        # unreachable: `mon(?:day)?s?` matched the first three letters of "month" and the
        # day-list clause won the tie, so a monthly blood-pressure check parsed as
        # FREQ=WEEKLY;BYDAY=MO — a weekly Monday event on the owner's subscribed calendar
        # for a note that said nothing of the kind, with a well-formed rule `parse_rrule`
        # could never catch.
        ("blood pressure check every month", "FREQ=MONTHLY"),
        ("mortgage payment every month on the 1st", "FREQ=MONTHLY"),
        ("shots every week", "FREQ=WEEKLY"),
        ("every day at 6am", "FREQ=DAILY"),
        ("service the furnace every year", "FREQ=YEARLY"),
        # The same missing boundary on the TRAILING day group: this was
        # INTERVAL=2;BYDAY=MO, off "Monterey".
        ("every two weeks Monterey trip", "FREQ=WEEKLY;INTERVAL=2"),
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
        # The holes a keyword list grows, each of which produced an UNBOUNDED rule: a
        # count word the list happened not to carry ("physio every two days" is in the
        # corpus above and this is one number-word away from it), a vague count, and the
        # three clause shapes that bound a rule without naming an end at all.
        "every Tuesday for seven weeks",
        "physio every day for nine days",
        "every Tuesday for a few weeks",
        "gym every Tuesday this month",
        "every Tuesday in March",
        "yoga Wednesdays over the summer",
        "every Monday while the cast is on",
        "infusions every three weeks during chemo",
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


@pytest.mark.parametrize(
    "quote",
    [
        "he called me the last two Tuesdays",
        "I worked the past three Saturdays",
        "she has been late these last few Mondays",
        "quiet the last two weeks",
    ],
)
def test_a_span_naming_past_occasions_is_refused(quote: str) -> None:
    """The refusal that is not a bound: a PLURAL weekday is this module's own marker for
    a rule, and "the last two Tuesdays" wears it while describing something already over.
    Reading a rule out of one puts a weekly event in the owner's future off a span about
    his past."""
    assert parse_recurrence(quote) is None


@pytest.mark.parametrize(
    ("quote", "rrule"),
    [
        # The singular ordinal survives it — "the last Friday of the month" is the
        # nth-of-month rule, not a retrospective.
        ("payday the last Friday of the month", "FREQ=MONTHLY;BYDAY=-1FR"),
    ],
)
def test_the_retrospective_refusal_spares_the_ordinal(quote: str, rrule: str) -> None:
    found = parse_recurrence(quote)
    assert found is not None and found.rrule == rrule


@pytest.mark.parametrize(
    "quote",
    [
        "semi-annual review",
        "tri-weekly staff meeting",
        # SPACED, which walked straight through the hyphen fix — the same word, the same
        # wrongness, one character apart.
        "semi annual review",
        "tri weekly standup",
        "bi weekly payroll",
    ],
)
def test_a_compound_is_a_different_word_hyphenated_or_spaced(quote: str) -> None:
    """A hyphen IS a word boundary, so `\b` let `semi-annual` match `annual` and
    `tri-weekly` match `weekly`. A compound built on the word means something the word
    does not — and nothing here can tell what — so it reads as no rule. The compounds
    this DOES know (`bi-weekly`) are spelled keys of their own."""
    assert parse_recurrence(quote) is None
    assert parse_recurrence("bi-weekly paycheck") is not None
    assert parse_recurrence("bimonthly newsletter") is not None


@pytest.mark.parametrize(
    "quote",
    [
        # The six that got past three rounds of blocklist, each a well-formed rule about
        # a different thing — and the first three are the worst shape this module has:
        # a span describing the owner's PAST becoming a forever-repeating calendar entry.
        "we met every tuesday last month",
        "standup every monday last week",
        "swim every tuesday last summer",
        "yoga every tuesday for the summer",
        "gym every tuesday next month",
        "every tuesday in the spring",
        # And the neighbours nobody listed, which is the point of inverting the check:
        # these were never enumerated anywhere and refuse because the PERIOD is left
        # over, not because the phrasing was foreseen.
        "every tuesday since march",
        "every monday through november",
        "every friday all summer",
        "every tuesday throughout the fall",
        "every tuesday over the holidays",
        "every monday until the spring",
        "every tuesday up to the summer",
        "every tuesday during the term",
        "every wednesday for the rest of the year",
        "every monday this semester",
        "physio every day next week",
    ],
)
def test_a_period_the_rule_did_not_consume_refuses(quote: str) -> None:
    """The check that closes the class instead of listing its members.

    Three rounds of this parser produced the same bug three times: a rule that is well
    formed and about a different thing, which `parse_rrule` can never catch, and each
    round the blocklist was one phrase short. Enumerating the ways English scopes a rule
    (`last`, `next`, `this`, `for the`, `in the`, `over the`, `since`, `all`, × every
    calendar noun × every determiner) is a cross product that will always have an empty
    cell. So the rule must CONSUME its span, and a calendar noun left over refuses — which
    needs only the closed lexical class of period words to be complete."""
    assert parse_recurrence(quote) is None


def test_the_unconsumed_check_reads_the_leftovers_not_the_whole_span() -> None:
    """The other half of the property, and the one that keeps it usable: a span may say
    plenty this parser does not model — a time, a place, a person, an unrelated occasion
    — and none of that is a reason to drop the rule. Only a PERIOD is."""
    for quote in (
        "Signed up at the Y on Oak St. Gym every Tuesday and Thursday at 6am.",
        "Therapy with Dr. Nunez every other week, Wednesdays at 4. His office moved to Pine Ave.",
        "I pay the mortgage on the first of every month",
        "in the office every Monday through Friday",
        # A rule AND an unrelated one-off: refusing this would cost a correct rule on a
        # very ordinary note, so a bare preposition does not refuse a weekday — while
        # `last Monday` and `next Monday`, which RE-TIME the rule, do.
        "Gym every Tuesday. Saw Dana on Monday.",
    ):
        assert parse_recurrence(quote) is not None, quote
    assert parse_recurrence("Gym every Tuesday. Saw Dana last Monday.") is None


def test_a_range_of_plural_days_is_an_accepted_over_refusal() -> None:
    """Pinned as a known limit rather than left to be discovered: the day-range expansion
    rewrites "Mondays through Wednesdays" into singular day names, which strips the plural
    marker the span was relying on, so it reads as no rule.

    That is the safe direction and it is deliberate — every refusal costs a schedule the
    handler does not write, and every false parse costs a phantom event on the owner's
    calendar — but it IS a miss, and a corpus that did not say so would be claiming the
    parser refuses only what it means to."""
    assert parse_recurrence("she works Mondays through Wednesdays") is None
    # Marked, it parses: the marker is what the expansion cannot supply for itself.
    found = parse_recurrence("she works every Monday through Wednesday")
    assert found is not None and found.rrule == "FREQ=WEEKLY;BYDAY=MO,TU,WE"


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
