"""The timeline's trail shaping, without a database (plan §M8a, `minecraft/timeline.py`).

Pinned: the point budget holds even when there are more segments than it can bound;
segments split on a session, a dimension, a respawn and a long silence outside any
session; and a joined or left event finds the sample on its own side of the moment.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from jbrain.minecraft import timeline as tl


def _row(at: float, dim: str = "overworld", x: float = 0.0) -> Any:
    return SimpleNamespace(at=at, dim=dim, x=x, y=64.0, z=0.0)


def _pts(*times: float) -> list[list[float]]:
    return [[t, 0.0, 0.0, 0.0] for t in times]


def test_a_short_trail_is_sent_whole() -> None:
    segs = [tl._Seg("s", "overworld", _pts(*range(5)))]
    kept, cut = tl.downsample(segs, cap=10)
    assert kept is segs and cut is False


def test_more_segments_than_the_budget_keeps_the_newest_ends() -> None:
    segs = [tl._Seg(str(i), "overworld", _pts(i, i + 0.5)) for i in range(10)]
    segs.append(tl._Seg("lone", "nether", _pts(20.0)))
    kept, cut = tl.downsample(segs, cap=5)
    assert cut is True
    assert sum(len(g.points) for g in kept) <= 5
    assert [g.session for g in kept] == ["8", "9", "lone"]


def test_the_budget_is_shared_in_proportion_and_ends_survive() -> None:
    long = tl._Seg("a", "overworld", _pts(*range(1_000)))
    short = tl._Seg("a", "nether", _pts(*(2_000.0 + i for i in range(100))))
    kept, cut = tl.downsample([long, short], cap=110)
    assert cut is True and sum(len(g.points) for g in kept) <= 110
    assert kept[0].points[0][0] == 0.0 and kept[0].points[-1][0] == 999.0
    assert kept[1].points[0][0] == 2_000.0 and kept[1].points[-1][0] == 2_099.0
    assert len(kept[0].points) > 5 * len(kept[1].points)
    times = [p[0] for p in kept[0].points]
    assert times == sorted(times) and len(set(times)) == len(times)


def test_segments_split_on_session_dimension_respawn_and_loose_silence() -> None:
    sessions = [(0.0, 100.0, "one"), (200.0, 300.0, "two")]
    rows = [
        _row(10),
        _row(20, "nether"),  # a portal
        _row(30, "nether"),
        _row(50, "nether"),  # after the respawn at 40
        _row(210),  # the next session
        _row(400),  # no session
        _row(400 + tl.LOOSE_GAP_S + 1),  # and quiet for a long while
    ]
    segs = tl.segment(rows, sessions, respawns=[40.0])
    assert [(g.session, g.dim, len(g.points)) for g in segs] == [
        ("one", "overworld", 1),
        ("one", "nether", 2),
        ("one", "nether", 1),
        ("two", "overworld", 1),
        (None, "overworld", 1),
        (None, "overworld", 1),
    ]


def _ev(kind: str, at: float, **kw: Any) -> dict[str, Any]:
    return {"kind": kind, "at": at, "x": None, "y": None, "z": None, "dim": None, **kw}


def test_an_event_without_a_place_sits_on_its_own_side_of_the_moment() -> None:
    samples = [[10.0, 1.0, 64.0, 1.0], [20.0, 2.0, 64.0, 2.0]]
    dims = ["overworld", "nether"]
    join = _ev("join", 15.0)
    tl._place(join, samples, dims)
    assert (join["x"], join["dim"]) == (2.0, "nether")
    leave = _ev("leave", 15.0)
    tl._place(leave, samples, dims)
    assert (leave["x"], leave["dim"]) == (1.0, "overworld")
    early = _ev("leave", 1.0)
    tl._place(early, samples, dims)  # nothing before it: the first sample after
    assert early["x"] == 1.0
    placed = _ev("death", 15.0, x=9.0, y=1.0, z=9.0, dim="the_end")
    tl._place(placed, samples, dims)
    assert (placed["x"], placed["dim"]) == (9.0, "the_end")
    lonely = _ev("join", 15.0)
    tl._place(lonely, [], [])
    assert lonely["x"] is None
