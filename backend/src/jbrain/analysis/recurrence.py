"""Recurrence, read out of the note's own words rather than asked of the model.

R1 of `docs/plans/AGENT_INGEST_REWRITE.md` §3.2, and it is a MEASURED design rather
than a preference. R0 put a `repeats` field on the reading schema in front of the live
model on five recurring notes, twice: an RRULE spelling parsed 0 times in 113 values, a
sharpened RRULE spelling 0 in 115, and the note's-own-phrase spelling said what the note
said 28 times in 118. The model writes the coarse English frequency and drops exactly
what makes a rule usable — `weekly` for "every Tuesday and Thursday", `monthly` for "the
first Monday of the month" — and it stamps the field on facts with no recurrence in them
about half the time. So the rule this repo already carries extends: **`required` buys
PRESENCE, not MEMBERSHIP, and a GRAMMAR is no more reachable through a tool description
than a word list was.**

What works is deterministic, and the evidence is in the same runs: parsing the model's
own attested `quote` recovered the right rule on 198 of 200 runs and was never wrong when
it parsed. So there is no `repeats` field anywhere in a tool sidecar; the reading writes
the fact and the span it rests on, and this module reads the rule out of that span.

**Two rules govern everything below, and both are the `_close_interval` discipline**
(`agent/graphwritetools.py`) applied to a second field the model over-applies:

1. **A rule is DISCARDED, never guessed.** Every refusal returns None and the fact
   commits with the dates it had. Nothing here is ever "held" or approximated.
2. **The built rule is VALIDATED before it is returned.** `appointments.rrule` is written
   and read as plain text everywhere on the box (`appointment_projection.py`, `ics.py`),
   so a malformed rule would reach the .ics feed the owner's phone subscribes to with
   nothing in between to catch it. `parse_rrule` is that catch, and it is the same strict
   RFC-5545 RECUR reader R0 scored the model's own output with.

**And the refusals are NOT in the probe's reference parser, because the probe never had
to be wrong.** Its five notes all recur, so a bare weekday could safely mean a weekly
rule; in production "coffee with Dana on Tuesday" is a single appointment, and reading a
weekly rule out of it would put a phantom every-Tuesday event on the owner's calendar
forever. So a recurrence MARKER is required — `every`/`each`, a PLURAL weekday, a
frequency adverb, `weekdays`/`weekends`, or an nth-of-the-month — and a singular weekday
alone is not one.

A marker is necessary and it is not sufficient, which is the correction this module took
on review: a plural weekday can name PAST occasions ("he called me the last two
Tuesdays"), and every marker can sit inside a span the note itself bounds in words no
parser can date ("every Tuesday in March", "every Monday while the cast is on"). Both are
refusals, `_RETROSPECTIVE` and `_BOUNDED`, and both fail toward silence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_FREQS = frozenset({"SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_RECUR_INTS = frozenset(
    {
        "INTERVAL",
        "COUNT",
        "BYSETPOS",
        "BYMONTH",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
    }
)


def _weekday_token(token: str) -> tuple[int, str] | None:
    """An RFC-5545 BYDAY token: a two-letter day with an optional signed ordinal."""
    day = token[-2:]
    if day not in _DAYS:
        return None
    prefix = token[:-2]
    if not prefix:
        return 0, day
    digits = prefix[1:] if prefix[0] in "+-" else prefix
    if not digits.isdigit() or int(digits) == 0:
        return None
    return (-int(digits) if prefix[0] == "-" else int(digits)), day


def _until_ok(raw: str) -> bool:
    """RFC-5545 UNTIL: a DATE or DATE-TIME in BASIC form (20270301, 20270301T090000Z)."""
    body = raw.split("T", 1)[0]
    return len(body) == 8 and body.isdigit()


def parse_rrule(value: str) -> dict[str, str] | None:
    """One RFC-5545 RECUR rule as its parts, or None for anything that is not one.

    Strict on purpose and in both directions: it is what validates a rule this module
    BUILDS before that rule is stored, and it is the reader anything downstream can use
    to check a rule that arrived from somewhere else."""
    body = value.strip()
    if not body:
        return None
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    parts: dict[str, str] = {}
    for chunk in body.split(";"):
        key, sep, raw = chunk.partition("=")
        key, raw = key.strip().upper(), raw.strip().upper()
        if not sep or not key or not raw or key in parts:
            return None
        parts[key] = raw
    if parts.get("FREQ") not in _FREQS:
        return None
    if "COUNT" in parts and "UNTIL" in parts:
        return None
    for key, raw in parts.items():
        if key == "FREQ":
            continue
        if key == "UNTIL":
            if not _until_ok(raw):
                return None
        elif key == "BYDAY":
            if any(_weekday_token(t) is None for t in raw.split(",")):
                return None
        elif key == "WKST":
            if raw not in _DAYS:
                return None
        elif key in _RECUR_INTS:
            for token in raw.split(","):
                digits = token[1:] if token[:1] in "+-" else token
                if not digits.isdigit() or (key in ("INTERVAL", "COUNT") and int(digits) < 1):
                    return None
        else:
            return None
    return parts


@dataclass(frozen=True)
class Recurrence:
    """A rule read out of a span, and the span it was read out of.

    `phrase` is the temporal token's `surface_phrase` — normalized (lowercased,
    whitespace collapsed) because it is cut out of normalized text, which `_locate`'s
    case-insensitive pass anchors anyway."""

    rrule: str
    phrase: str


# The weekday vocabulary, twice, and the difference between the two lists is the
# false-positive guard. `_ANY` admits the singular ("every tuesday" is a rule because
# `every` says so); `_PLURAL` is what stands on its own ("spanish class tuesdays"),
# and it is spelled out in full rather than as `tue(?:s(?:day)?)?s` — that pattern reads
# the ABBREVIATION "tues" as a plural, and "lunch with Sam Tues" is not a weekly rule.
_DAY_ANY: tuple[tuple[str, str], ...] = (
    ("MO", r"mon(?:day)?s?"),
    ("TU", r"tue(?:s(?:day)?)?s?"),
    ("WE", r"wed(?:nes(?:day)?)?s?"),
    ("TH", r"thu(?:r(?:s(?:day)?)?)?s?"),
    ("FR", r"fri(?:day)?s?"),
    ("SA", r"sat(?:urday)?s?"),
    ("SU", r"sun(?:day)?s?"),
)
_DAY_PLURAL: tuple[tuple[str, str], ...] = (
    ("MO", r"mondays"),
    ("TU", r"tuesdays"),
    ("WE", r"wednesdays"),
    ("TH", r"thursdays"),
    ("FR", r"fridays"),
    ("SA", r"saturdays"),
    ("SU", r"sundays"),
)
_ANY_DAY = "|".join(p for _, p in _DAY_ANY)
_ANY_PLURAL = "|".join(p for _, p in _DAY_PLURAL)
# EVERY day token carries its own trailing `\b`, and that word boundary is load-bearing
# rather than tidy: without it `mon(?:day)?s?` matches the first three letters of
# "month", so "blood pressure check every month" read as FREQ=WEEKLY;BYDAY=MO — a
# monthly check as a weekly Monday event on the owner's subscribed calendar, forever,
# reported back to the model as a rule it had stated. `parse_rrule` cannot catch that:
# the rule is well formed, it is just about a different thing. Same boundary, same
# reason, on the trailing-days group: "every two weeks Monterey trip" was BYDAY=MO.
_DAY_ONE = rf"(?:{_ANY_DAY})\b"
_PLURAL_ONE = rf"(?:{_ANY_PLURAL})\b"
_JOIN = r"(?:\s*(?:,|and|&|/|\+)\s*)"
_DAY_LIST = rf"{_DAY_ONE}(?:{_JOIN}{_DAY_ONE})*"
_PLURAL_LIST = rf"{_PLURAL_ONE}(?:{_JOIN}{_PLURAL_ONE})*"

_LONG_DAYS = dict(
    zip(
        _DAYS,
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        strict=True,
    )
)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}
_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_UNIT_FREQ = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}
_ADVERB: dict[str, tuple[str, int]] = {
    "daily": ("DAILY", 1),
    "nightly": ("DAILY", 1),
    "weekly": ("WEEKLY", 1),
    "biweekly": ("WEEKLY", 2),
    "bi-weekly": ("WEEKLY", 2),
    "fortnightly": ("WEEKLY", 2),
    "monthly": ("MONTHLY", 1),
    "bimonthly": ("MONTHLY", 2),
    "bi-monthly": ("MONTHLY", 2),
    "quarterly": ("MONTHLY", 3),
    "yearly": ("YEARLY", 1),
    "annually": ("YEARLY", 1),
}

# A clause that BOUNDS the repetition — "until March", "through the end of term", "for
# the next six weeks", "for three more sessions". Every one of them is refused, whole,
# and that is the sharpest decision in this module: the phrase parser recovers the
# FREQUENCY of a bounded rule easily, and resolving the BOUND to the DATE an RRULE
# `UNTIL` needs is date inference the handler must not do. Emitting the rule without its
# bound would be worse than emitting nothing — an unbounded rule states something the
# note does not say, and it says it on the owner's calendar forever. (The probe's
# reference parser kept these by putting the raw English in `UNTIL`, which `parse_rrule`
# rejects; that arm was scoring whether the INFORMATION survives, not writing rows.)
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_COUNTS = "|".join(
    ("next", "another", "rest", "remainder", "a", "few", r"\d+", "one", *_NUMBER_WORDS)
)
_BOUNDED = re.compile(
    r"\b(?:"
    # An explicit end.
    r"until|untill|til|till|through|thru|ending|ends|ended|stops|stopping|"
    # A CONDITION rather than a schedule — "while the cast is on", "during chemo".
    r"while|during|"
    # A count of repetitions. The number vocabulary is shared with `_NUMBER_WORDS` so
    # the two can never drift apart: "physio every two days" parses and "physio every
    # day for nine days" must not, and the second was one number-word away from the
    # first in this module's own corpus.
    rf"for\s+(?:the\s+)?(?:a\s+few|{_COUNTS})|"
    # A named window. "every Tuesday in March", "gym every Tuesday this month", "yoga
    # Wednesdays over the summer" — each is a rule with an end the note states in words
    # this cannot date.
    r"this\s+(?:week|month|year|term|semester|summer|winter|spring|fall|autumn|season)|"
    r"over\s+the\s+\w+|"
    rf"in\s+(?:{_MONTHS})"
    r")\b"
)

# THE UNCONSUMED-PERIOD REFUSAL, and it is a different SHAPE of check from the two
# blocklists around it rather than a longer version of them.
#
# Three rounds of this parser produced the same bug three times — a rule that is well
# formed and about a different thing, which `parse_rrule` can never catch — and each round
# the blocklist was one phrase short: "every tuesday last month" is a weekly Monday…
# sorry, a weekly TUESDAY entry in the owner's future, read out of a span about his past.
# Enumerating the ways English scopes a rule (`last`, `next`, `this`, `for the`, `in the`,
# `over the`, `since`, `all`, × every calendar noun × every determiner) is a cross product
# that will always be one cell short.
#
# So it is inverted: the rule must consume its span. Whatever the clause did NOT consume
# is scanned, and a PERIOD left over there refuses. What has to be complete for that to
# hold is not the open-ended set of scoping constructions but the CLOSED lexical class of
# calendar nouns — English has a fixed number of those, and they are listed here. Anything
# new in the scoping half ("throughout the fall", "up until spring") lands on the same
# refusal for free, which is the property the two blocklists could not have.
#
# WEEKDAYS are held to a narrower trigger than periods: "every Tuesday. Saw Dana on
# Monday." is a rule plus an unrelated occasion, and refusing that would cost a correct
# rule on a very ordinary note — so a bare preposition does not refuse a weekday, while
# `last Monday` / `next Monday` (which re-time the rule) do.
_PERIOD_NOUNS = (
    r"days?|weeks?|weekends?|weekdays?|fortnights?|months?|quarters?|years?|decades?|"
    r"seasons?|springs?|summers?|falls?|autumns?|winters?|terms?|semesters?|holidays|"
    rf"breaks?|{_MONTHS}"
)
_SCOPES = r"last|next|this|past|previous|coming|upcoming|remaining|rest\s+of|all\s+of|all"
_PREPS = (
    r"in|on|over|during|throughout|through|until|till|til|since|after|before|by|for|"
    r"within|from|to"
)
_DET = r"(?:the|a|an|this|that|my|his|her|our|their|next|last|coming|following|previous)\s+"
_UNCONSUMED = re.compile(
    rf"\b(?:"
    rf"(?:{_SCOPES}|{_PREPS})\s+(?:{_DET})?(?:(?:{_COUNTS})\s+)?(?:{_PERIOD_NOUNS})"
    rf"|(?:{_SCOPES})\s+(?:{_DET})?(?:(?:{_COUNTS})\s+)?(?:{_ANY_DAY})"
    rf")\b"
)

# The RETROSPECTIVE refusal, which is not a bound at all: it is a span that names PAST
# occasions. "He called me the last two Tuesdays" is a plural weekday — this module's
# own stated marker for a rule — describing something that has already stopped, and
# reading a rule out of it puts a weekly event in the owner's future. Singular
# ordinals survive ("the last Friday of the month" is an nth-of-month rule), which is
# why this requires a PLURAL day or a plural period.
_RETROSPECTIVE = re.compile(
    rf"\b(?:last|past|previous|these)\s+(?:(?:{'|'.join(_NUMBER_WORDS)}|\d+|few|several)\s+)?"
    rf"(?:{_ANY_PLURAL}|weeks|months|years)\b"
)


# A compound former standing in front of the word, SPACED rather than hyphenated. The
# hyphen fix used `(?<![\w-])` and "semi annual" walked straight through it — the same
# word, the same wrongness, one character apart. Fixed-width lookbehinds, one per former,
# because that is what Python's `re` allows.
_NOT_COMPOUND = "".join(
    rf"(?<!\b{former}\s)" for former in ("semi", "tri", "bi", "quad", "multi", "quasi")
)


def _phrase_days(text: str) -> list[str]:
    """The weekdays a phrase names, in week order, however it spells them."""
    return [code for code, pattern in _DAY_ANY if re.search(rf"\b{pattern}\b", text)]


def _expand_day_range(text: str) -> str:
    """ "Monday through Friday" as the days it names.

    Run BEFORE the bounding clause is read, or the range's own "through" reads as an end
    date and the rule is discarded for a bound the note never stated."""
    span = re.search(rf"\b({_ANY_DAY})\s*(?:-|–|to|through|thru)\s*({_ANY_DAY})\b", text)
    if span is None:
        return text
    first, last = _phrase_days(span.group(1)), _phrase_days(span.group(2))
    if not first or not last:
        return text
    start, end = _DAYS.index(first[0]), _DAYS.index(last[0])
    days = _DAYS[start : end + 1] if start <= end else _DAYS[start:] + _DAYS[: end + 1]
    # Comma-joined, not space-joined: the day-list grammar below reads a LIST, so a
    # space-separated expansion would leave "every monday through friday" matching
    # nothing at all.
    return text[: span.start()] + ", ".join(_LONG_DAYS[d] for d in days) + text[span.end() :]


def _byday(text: str) -> str:
    return ",".join(_phrase_days(text))


def _rule(freq: str, *, interval: int = 1, byday: str = "") -> str:
    parts = [f"FREQ={freq}"]
    if interval > 1:
        parts.append(f"INTERVAL={interval}")
    if byday:
        parts.append(f"BYDAY={byday}")
    return ";".join(parts)


# The clause grammar, in priority order. Each entry is (pattern, builder): the pattern's
# whole match is the PHRASE the token carries, and the builder turns its groups into a
# rule string (or into None, which discards). Ordered most specific first — an
# nth-of-the-month clause contains a weekday, and a "every other week" clause contains
# "every".
_TRAILING_DAYS = rf"(?:\s*,?\s*(?:on\s+)?(?P<days>{_DAY_LIST}))?"


def _nth_of_month(m: re.Match[str]) -> str | None:
    days = _phrase_days(m.group("day"))
    if len(days) != 1:
        return None
    return _rule("MONTHLY", byday=f"{_ORDINALS[m.group('ord')]}{days[0]}")


def _every_other(m: re.Match[str]) -> str | None:
    unit = m.group("unit")
    if unit in _UNIT_FREQ:
        return _rule(_UNIT_FREQ[unit], interval=2, byday=_byday(m.group("days") or ""))
    days = _byday(unit)
    return _rule("WEEKLY", interval=2, byday=days) if days else None


def _every_n(m: re.Match[str]) -> str | None:
    raw = m.group("n")
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 0)
    unit = _UNIT_FREQ.get(m.group("unit"))
    if count < 2 or unit is None:
        return None
    return _rule(unit, interval=count, byday=_byday(m.group("days") or ""))


def _every_days(m: re.Match[str]) -> str | None:
    days = _byday(m.group("days"))
    return _rule("WEEKLY", byday=days) if days else None


def _every_unit(m: re.Match[str]) -> str | None:
    unit = _UNIT_FREQ.get(m.group("unit"))
    return _rule(unit) if unit else None


def _adverb(m: re.Match[str]) -> str | None:
    freq, interval = _ADVERB[m.group("word")]
    return _rule(freq, interval=interval, byday=_byday(m.group("days") or ""))


_CLAUSES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str | None]], ...] = (
    (
        re.compile(
            rf"\b(?:the\s+)?(?P<ord>{'|'.join(_ORDINALS)})\s+(?P<day>{_ANY_DAY})"
            r"\s+of\s+(?:the|each|every)\s+month\b"
        ),
        _nth_of_month,
    ),
    (
        re.compile(
            rf"\b(?:every|each)\s+other\s+(?P<unit>week|day|month|year|{_DAY_LIST})\b"
            rf"{_TRAILING_DAYS}"
        ),
        _every_other,
    ),
    (
        re.compile(
            rf"\b(?:every|each)\s+(?P<n>\d+|{'|'.join(_NUMBER_WORDS)})\s+"
            rf"(?P<unit>day|week|month|year)s?{_TRAILING_DAYS}"
        ),
        _every_n,
    ),
    (re.compile(rf"\b(?:every|each)\s+(?P<days>{_DAY_LIST})"), _every_days),
    (re.compile(r"\b(?:every|each)\s+(?P<unit>day|week|month|year)\b"), _every_unit),
    (
        re.compile(r"\b(?:(?:every|each)\s+weekday|weekdays)\b"),
        lambda _m: _rule("WEEKLY", byday=",".join(_DAYS[:5])),
    ),
    # Plural, or `every`-marked: "last weekend" is an occasion and "weekends" is a rule,
    # the same distinction the two weekday vocabularies above draw.
    (
        re.compile(r"\b(?:(?:every|each)\s+weekend|weekends)\b"),
        lambda _m: _rule("WEEKLY", byday="SA,SU"),
    ),
    # `(?<![\w-])` and not `\b`: a hyphen IS a word boundary, so `semi-annual` matched
    # `annual` and `tri-weekly` matched `weekly` — a compound built on the word means
    # something the word does not, and none of them are rules this can read. `_NOT_COMPOUND`
    # is the same rule for the SPACED spelling, which walked through the hyphen fix
    # unchanged. The compounds this module really knows (`bi-weekly`) are keys of their own
    # and match at their own first letter, where both guards see a space.
    (
        re.compile(
            rf"(?<![\w-]){_NOT_COMPOUND}(?P<word>{'|'.join(_ADVERB)})\b"
            rf"(?:\s+on\s+(?P<days>{_DAY_LIST}))?"
        ),
        _adverb,
    ),
    (re.compile(rf"\b(?P<days>{_PLURAL_LIST})"), _every_days),
    (re.compile(rf"(?<![\w-]){_NOT_COMPOUND}annual\b"), lambda _m: _rule("YEARLY")),
)


@dataclass(frozen=True)
class _Clause:
    """One clause match: the rule it builds, the phrase it reads as, and the SPAN of the
    text it consumed. The span is what makes the unconsumed-period check possible — it is
    the difference between "the rule accounts for this text" and "a rule was found
    somewhere inside it"."""

    rule: str
    phrase: str
    start: int
    end: int


def _first_clause(text: str) -> _Clause | None:
    """The earliest clause in the text. Earliest rather than first-pattern-that-matches:
    the clause list is ordered by specificity, so scanning it in order would read "every
    other week, Wednesdays" out of a span whose actual subject is an nth-of-the-month rule
    two words earlier."""
    best: _Clause | None = None
    for pattern, build in _CLAUSES:
        match = pattern.search(text)
        if match is None:
            continue
        rule = build(match)
        if rule is None:
            continue
        if best is None or match.start() < best.start:
            best = _Clause(rule, match.group(0).strip(), match.start(), match.end())
    return best


def parse_recurrence(text: str) -> Recurrence | None:
    """The recurrence rule a span states, or None.

    None is the answer for everything this cannot read with certainty, and the caller
    treats it as "the note stated no schedule" — the fact still commits, undated or with
    whatever dates it had. The refusals, each of which has its own test:

    - **no clause.** The clause grammar IS the marker: every entry in it needs an
      `every`/`each`, a PLURAL weekday, a frequency adverb, `weekdays`/`weekends` or an
      nth-of-the-month shape, so a span that states an occasion ("coffee with Dana on
      Tuesday") matches none of them and reads as no rule at all.
    - **a bound this cannot date.** "Tuesdays until March", "every day for nine days",
      "every Tuesday in March", "every Monday while the cast is on" — see `_BOUNDED`.
    - **a span that names PAST occasions.** "He called me the last two Tuesdays" wears
      this module's own marker for a rule and describes something already over — see
      `_RETROSPECTIVE`.
    - **a PERIOD the rule did not consume.** "every Tuesday last month", "every Tuesday
      for the summer", "every Tuesday in the spring": the clause is a real rule and the
      rest of the span scopes it to a window this cannot date. `_UNCONSUMED` is the check
      that closes that class rather than listing its members.
    - **two different rules in one span.** A span that states both "every Tuesday" and
      "the first Monday of the month" is a fact the reading should have split; picking
      one of them silently is the failure mode this module exists to avoid.
    - **anything whose built rule is not a valid RRULE**, which is the backstop the
      builders are allowed to be simple against.
    """
    body = _expand_day_range(" ".join(text.lower().split()))
    if not body or _BOUNDED.search(body) is not None or _RETROSPECTIVE.search(body) is not None:
        return None
    first = _first_clause(body)
    if first is None:
        return None
    # What the rule did NOT account for. A period left over there re-times the rule the
    # clause found, and the span is then stating something this cannot write down.
    if _UNCONSUMED.search(f"{body[: first.start]} {body[first.end :]}") is not None:
        return None
    second = _first_clause(body[first.end :])
    if second is not None and second.rule != first.rule:
        return None
    return Recurrence(first.rule, first.phrase) if parse_rrule(first.rule) else None
