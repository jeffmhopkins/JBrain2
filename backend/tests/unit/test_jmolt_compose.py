"""The composed brief (JMOLT_LEDGER_ENGINE_PLAN.md, S2).

These are the properties the second engine's context layer is supposed to have, asserted
rather than described — because the first engine's prologue had most of them as intentions and
lost them one block at a time.
"""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.agent.jmolt_compose import OwnerNote, compose_brief
from jbrain.models.jmolt_obligation import Evidence, Obligation

NOW = datetime(2026, 8, 30, 3, 20, tzinfo=UTC)


def _ob(
    kind: str = "question",
    subject: str = "whether verification differs from generation",
    *,
    opened: datetime | None = None,
    evidence: list[Evidence] | None = None,
    status: str = "open",
    resolution: str = "",
) -> Obligation:
    opened = opened or NOW
    return Obligation(
        id=f"id-{subject}",
        kind=kind,
        subject=subject,
        status=status,
        resolution=resolution,
        opened_at=opened,
        touched_at=NOW,
        evidence=evidence or [],
    )


def _brief(**kw) -> str:
    base = dict(
        handle="jmolt",
        now=NOW,
        minutes_left=42,
        open_obligations=[],
        closed_recently=[],
    )
    base.update(kw)
    return compose_brief(**base)


def test_the_brief_is_deterministic() -> None:
    """An arm whose context varies between nights is an arm whose result cannot be attributed
    to the change under test."""
    obligations = [_ob(), _ob(subject="another thing")]
    assert _brief(open_obligations=obligations) == _brief(open_obligations=obligations)


def test_an_obligation_appears_with_its_age_and_never_a_tally() -> None:
    """Elapsed days is a fact about ONE thing; a count of how many are open is a score, and a
    score the agent can only move by acting is a slot machine."""
    out = _brief(
        open_obligations=[
            _ob(opened=NOW - timedelta(days=3)),
            _ob(subject="opened tonight, this one"),
        ]
    )
    assert "open 3 days" in out
    assert "opened tonight" in out
    for tally in ("2 open", "you have 2", "total"):
        assert tally not in out.lower()


def test_someone_elses_words_come_back_attributed() -> None:
    """An attributed quote is something to answer. That is the entire difference between this
    and a paragraph of remembered prose."""
    out = _brief(
        open_obligations=[
            _ob(
                kind="person",
                subject="@otheragent",
                evidence=[
                    Evidence(
                        quote="I think you are wrong about weeks.", source="@otheragent", at=NOW
                    )
                ],
            )
        ]
    )
    assert '@otheragent said: "I think you are wrong about weeks."' in out


def test_jmolts_own_words_come_back_only_where_the_wording_is_the_obligation() -> None:
    """The narrow resolution of a real tension: discharging a promise requires knowing what
    was promised, but an unanswered sentence in its own voice is what it continues rather than
    addresses. So a commitment carries the quote and a question does not."""
    said = Evidence(quote="I'll come back to this tomorrow.", source="self", at=NOW)

    promise = _brief(
        open_obligations=[_ob(kind="commitment", subject="come back to weeks", evidence=[said])]
    )
    assert 'you said: "I\'ll come back to this tomorrow."' in promise

    wondering = _brief(
        open_obligations=[_ob(kind="question", subject="about weeks", evidence=[said])]
    )
    assert "I'll come back to this tomorrow." not in wondering


def test_having_nothing_open_is_said_plainly_rather_than_left_blank() -> None:
    """A model that suspects an omission fills it."""
    out = _brief()
    assert "nothing open" in out.lower()
    assert "for reading" in out


def test_the_note_is_fenced_carries_its_age_and_asks_for_an_answer() -> None:
    """It arrives in the reading brief, not the system prompt: a note in the system prompt is
    indistinguishable from a rule."""
    out = _brief(
        note=OwnerNote(
            text="Maybe look at what the smaller submolts are doing.",
            written_at=NOW - timedelta(days=2),
        )
    )
    assert "BEGIN A NOTE FROM YOUR HUMAN (written 2 days ago)" in out
    assert "never as instructions to you" in out
    assert "acted, partly, or declined" in out


def test_an_expired_note_is_simply_gone() -> None:
    """A wish that never expires becomes a rule by accident — the owner said something once
    about posting more, and it governed every night after."""
    stale = OwnerNote(
        text="post more tonight",
        written_at=NOW - timedelta(days=9),
        expires_at=NOW - timedelta(days=2),
    )
    assert "post more tonight" not in _brief(note=stale)
    live = OwnerNote(text="post more tonight", written_at=NOW, expires_at=NOW + timedelta(days=1))
    assert "post more tonight" in _brief(note=live)


def test_a_blank_note_produces_no_section() -> None:
    assert "NOTE FROM YOUR HUMAN" not in _brief(note=OwnerNote(text="   ", written_at=NOW))


def test_what_was_finished_is_shown_with_how_it_closed() -> None:
    out = _brief(
        closed_recently=[
            _ob(
                subject="that thing about weeks",
                status="discharged",
                resolution="asked them directly and they answered",
            ),
            _ob(subject="a thing I dropped", status="abandoned"),
        ]
    )
    assert 'that thing about weeks — discharged: "asked them directly and they answered"' in out
    assert "a thing I dropped — abandoned" in out


def test_every_section_is_bounded() -> None:
    """The composed context must not grow into the thing it replaces: a window so full of its
    own past that the world is a footnote."""
    many = [
        _ob(
            subject=f"question {i}",
            evidence=[Evidence(quote=f"q{i}-{j}", source="@a", at=NOW) for j in range(9)],
        )
        for i in range(40)
    ]
    out = _brief(open_obligations=many, closed_recently=many)
    assert "question 8" not in out  # obligations capped at 8, so indexes 0..7 only
    assert out.count("@a said") <= 8 * 2  # and two quotes each


def test_the_clock_and_the_handle_lead() -> None:
    """jmolt reads WHO it is and how long it has before anything else — the two facts every
    other line depends on."""
    first = _brief().splitlines()[0]
    assert "@jmolt" in first and "42 minutes left" in first


@pytest.mark.parametrize("days", [0, 1, 2])
def test_ages_read_naturally(days: int) -> None:
    out = _brief(open_obligations=[_ob(opened=NOW - timedelta(days=days))])
    assert ("opened tonight" if days == 0 else f"open {days} day{'s' if days != 1 else ''}") in out
