"""Scoring a simulated night from its record (JMOLT_LEDGER_ENGINE_PLAN.md, S1).

The scores are computed from the outbox rows, the ledger and the believed writes — never from
the transcript — because a night that narrated four posts and made one is the failure being
measured, not a source of truth about it.
"""

from datetime import UTC, datetime

import pytest

from jbrain.agent.jmolt_score import score_night, summarize
from jbrain.agent.jmolt_sim import SimNight
from jbrain.agent.jmolt_sim_client import SimWrite
from jbrain.models.jmolt_outbox import OutboxRow

pytestmark = pytest.mark.anyio

AT = datetime(2026, 8, 29, 7, 0, tzinfo=UTC)


def _row(kind: str, payload: dict, *, status: str = "published", mid: str = "") -> OutboxRow:
    return OutboxRow(
        id=f"row-{kind}-{len(payload)}-{mid}",
        kind=kind,
        payload=payload,
        status=status,
        publish_at=None,
        moltbook_id=mid or None,
        error=None,
        created_at=AT,
        published_at=AT,
        sim=True,
    )


def _night(rows: list[OutboxRow], writes: list[SimWrite] | None = None) -> SimNight:
    return SimNight(
        principal_id="p",
        session_id="s",
        started_at=AT,
        writes=writes or [],
        outbox=rows,
        ledger=[],
    )


class _Embed:
    """Vectors keyed by exact text, so a test states similarity rather than depending on a
    model's opinion of it."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._v = vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._v[t] for t in texts]


async def test_a_reply_to_its_own_comment_is_a_self_reply() -> None:
    write = SimWrite(seq=1, kind="comment", at=AT, payload={}, sim_id="sim_comment_1")
    night = _night(
        [
            _row("comment", {"post_id": "p1", "content": "first"}, mid="sim_comment_1"),
            _row("comment", {"post_id": "p1", "parent_id": "sim_comment_1", "content": "again"}),
        ],
        writes=[write],
    )
    assert (await score_night(night)).self_replies == 1


async def test_a_comment_on_its_own_fresh_post_is_a_self_reply_too() -> None:
    """The other half of the observed failure: it commented on a post it had just made, and
    that reply has no parent_id to catch it by."""
    night = _night(
        [
            _row("post", {"title": "mine", "content": "x"}, mid="sim_post_1"),
            _row("comment", {"post_id": "sim_post_1", "content": "adding to my own"}),
        ]
    )
    assert (await score_night(night)).self_replies == 1


async def test_repeat_threads_counts_the_excess_not_the_thread() -> None:
    """Seventeen comments on one post is one thread and sixteen repeats; the second number is
    the one that describes what went wrong."""
    night = _night(
        [_row("comment", {"post_id": "p1", "content": f"c{i}"}) for i in range(4)]
        + [_row("comment", {"post_id": "p2", "content": "one only"})]
    )
    assert (await score_night(night)).repeat_threads == 3


async def test_a_night_that_published_nothing_is_silent_not_broken() -> None:
    night = _night([_row("post", {"title": "t", "content": "c"}, status="queued")])
    score = await score_night(night)
    assert score.published == 0 and score.silent and not score.died


async def test_a_night_that_died_is_recorded_as_such() -> None:
    night = _night([])
    night.error = "RuntimeError: boom"
    assert (await score_night(night)).died


async def test_restatement_scores_against_the_agents_own_history() -> None:
    night = _night([_row("post", {"title": "", "content": "tonight"})])
    said_again = _Embed({"last night": [1.0, 0.0], "tonight": [1.0, 0.0]})
    score = await score_night(night, prior=["last night"], embed=said_again)
    assert score.restatement == pytest.approx(1.0, abs=1e-6)

    something_new = _Embed({"last night": [1.0, 0.0], "tonight": [0.0, 1.0]})
    score = await score_night(night, prior=["last night"], embed=something_new)
    assert score.restatement == pytest.approx(0.0, abs=1e-6)


async def test_restatement_counts_tonights_own_earlier_item_as_prior() -> None:
    """A second post restating the first is a restatement, even though the first was not
    there when the night began."""
    v = [1.0, 0.0]
    night = _night(
        [
            _row("post", {"title": "", "content": "a thought"}),
            _row("post", {"title": "", "content": "the same thought"}),
        ]
    )
    embed = _Embed({"a thought": v, "the same thought": v})
    score = await score_night(night, embed=embed)
    assert score.restatement == pytest.approx(1.0, abs=1e-6)


async def test_an_item_with_nothing_before_it_is_not_scored_as_original() -> None:
    """On a first night that is every item, and averaging those zeroes in would report the
    emptiest arm as the most original one."""
    night = _night([_row("post", {"title": "", "content": "the only thing said"})])
    assert (
        await score_night(night, embed=_Embed({"the only thing said": [1.0, 0.0]}))
    ).restatement is None


async def test_restatement_is_none_rather_than_zero_when_it_was_not_measured() -> None:
    """An unmeasured metric must not read on the scoreboard like a measured zero."""
    night = _night([_row("post", {"title": "t", "content": "c"})])
    assert (await score_night(night)).restatement is None  # no embedder
    assert (await score_night(_night([]), embed=_Embed({}))).restatement is None  # nothing said


async def test_an_arm_is_summarised_as_distributions() -> None:
    scores = [
        await score_night(_night([_row("post", {"title": "t", "content": str(i)})]))
        for i in range(3)
    ] + [await score_night(_night([]))]
    arm = summarize("baseline", scores)
    assert arm.n == 4
    assert arm.stats["posts"]["median"] == 1.0
    assert arm.stats["silent_share"] == 0.25
    assert "restatement" not in arm.stats  # nothing was measured, so nothing is reported


async def test_summarising_no_nights_reports_nothing_rather_than_zeroes() -> None:
    arm = summarize("empty", [])
    assert arm.n == 0 and arm.stats == {}
