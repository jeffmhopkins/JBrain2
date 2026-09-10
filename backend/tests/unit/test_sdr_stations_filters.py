"""What the roster's chip filters COMPOSE into, without a database.

The integration suite proves the SQL against real Postgres; this proves the two things a
seeded fixture is bad at showing — that the provenance ids can only ever reach the
statement as one of three fixed fragments, and that the two chip rows combine as AND
between them and OR within one. A capture where every station happens to match both
readings agrees with either, and that is most captures.

Monkeypatched session, no TestClient: the point here is the text of the statement and the
shape of the answer, so a fake session that records what it was asked is the whole harness
(the same style `test_sdr_aprs_routes.py` uses for the routes).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain.api import sdr as sdr_api
from jbrain.sdr import stations as st

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

# A roster row as the grouping query returns it, provenance flags included.
_ROW: dict[str, Any] = {
    "call": "N1MPR-C",
    "packets": 3,
    "last_heard_at": NOW,
    "kinds": ["Position"],
    "gated": True,
    "direct": False,
    "via": "N4TDX",
    "any_direct": True,
    "any_gated": True,
    "any_rf": False,
    "info": "!2835.06ND08048.98W&RNG0001",
    "raw": "",
    "path": [],
    "n_source": "N4TDX",
    "audio_level": None,
}

_COUNTS = {
    "w_1d": 12,
    "w_3d": 20,
    "w_1w": 40,
    "unclassified": 0,
    "stations": 4,
    "has_older": False,
    "older": None,
}
_ARRIVALS = {"p_direct": 3, "p_gated": 2, "p_rf": 1}


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one(self) -> dict[str, Any]:
        return self._rows[0]


class _Session:
    """Answers each of the roster's four reads by what it recognisably asks for, and
    keeps every statement so a test can read what was sent."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.sql: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        self._rows = rows

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.sql.append(sql)
        self.params.append(params)
        if "p_direct" in sql:
            return _Result([_ARRIVALS])
        if "GROUP BY kind" in sql:
            return _Result([{"kind": "Position", "stations": 2}])
        if "w_1d" in sql:
            return _Result([_COUNTS])
        return _Result(self._rows)

    @property
    def roster_sql(self) -> str:
        """The grouping query — the only one with a HAVING to inspect."""
        return next(s for s in self.sql if "GROUP BY origin_call" in s)


async def _roster(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], _Session]:
    session = _Session(rows if rows is not None else [_ROW])

    @contextlib.asynccontextmanager
    async def scoped(_maker: Any, _ctx: Any) -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(st, "scoped_session", scoped)
    out = await st.StationsReader(None).roster(object(), **kwargs)  # type: ignore[arg-type]
    return out, session


# --- what may reach the statement ------------------------------------------------------


def test_the_three_provenance_states_are_the_whole_vocabulary() -> None:
    """Three, not two. `direct` and `gated` are what the owner asked for, and `rf` is the
    remainder the packet row already draws as its own badge — a two-state filter could
    not name a station heard through a digipeater, which is most of this band."""
    assert list(st.PROVENANCE) == ["direct", "gated", "rf"]


def test_only_a_known_provenance_reaches_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """These become SQL rather than a bound parameter, so the key check IS the guard."""
    assert sdr_api._provenance("direct,gated") == ["direct", "gated"]
    assert sdr_api._provenance(" rf , direct ") == ["rf", "direct"]
    assert sdr_api._provenance("gated'; DROP TABLE app.aprs_packets --") == []


def test_an_unknown_provenance_shows_everything_rather_than_erroring() -> None:
    """A stale PWA asking for a state we have since renamed gets the unfiltered roster,
    which is the screen it wanted, rather than an error page on a phone."""
    assert sdr_api._provenance("igate") == []
    assert sdr_api._provenance("") == []
    assert sdr_api._provenance(None) == []
    assert sdr_api._provenance("igate,gated") == ["gated"]


async def test_the_reader_refuses_an_unknown_id_even_if_the_route_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whitelist is repeated in the reader on purpose: `_provenance` guards the HTTP
    surface, and this guards every other caller of the reader. A string that is not a key
    contributes no fragment at all, so the roster comes back unfiltered."""
    _, session = await _roster(monkeypatch, window="1d", provenance=["gated'; DROP--"])

    assert "HAVING" not in session.roster_sql
    assert "DROP" not in session.roster_sql


# --- how the two chip rows combine -----------------------------------------------------


async def test_one_provenance_chip_filters_on_the_whole_window_not_the_last_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bool_or`, not the newest frame's flag. A station heard direct an hour ago and
    gated since is still a station the owner heard direct in this range, and a filter
    reading only the latest packet would drop it."""
    _, session = await _roster(monkeypatch, window="1d", provenance=["direct"])

    assert "HAVING (bool_or((COALESCE(heard_direct, false))))" in session.roster_sql


async def test_two_provenance_chips_are_a_union(monkeypatch: pytest.MonkeyPatch) -> None:
    """Within one row of chips, pressing a second one WIDENS. Direct and gated are
    exclusive per packet, so an AND here would return the empty list on a band where
    nothing arrives both ways in a single frame."""
    _, session = await _roster(monkeypatch, window="1d", provenance=["direct", "gated"])

    having = session.roster_sql.split("HAVING ")[1]
    assert having.startswith(
        "(bool_or((COALESCE(heard_direct, false))) OR bool_or((COALESCE(gated, false))))"
    )


async def test_a_kind_chip_and_a_provenance_chip_NARROW_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across the two rows it is AND: "who is putting out weather" and "who am I hearing
    direct" are two questions, and pressing one in each asks for the stations that answer
    both. OR here would make the second row add stations the first had excluded."""
    _, session = await _roster(monkeypatch, window="1d", kinds=["Weather"], provenance=["direct"])

    having = session.roster_sql.split("HAVING ")[1]
    assert having.startswith("bool_or(kind = ANY(:kinds)) AND (bool_or(")


async def test_rf_is_the_remainder_and_never_claims_an_unswept_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`rf` is defined by what a frame is NOT, so a NULL derived column would satisfy it
    under three-valued logic and file a packet nobody has classified yet as heard on the
    air. Those rows are reported as `unclassified` instead, which is the one honest
    answer for a roster that is still filling in."""
    _, session = await _roster(monkeypatch, window="1d", provenance=["rf"])

    assert (
        "bool_or((NOT COALESCE(gated, false) AND NOT COALESCE(heard_direct, false)))"
        in session.roster_sql
    )


# --- the counts ------------------------------------------------------------------------


async def test_the_counts_are_computed_over_the_window_unfiltered_by_the_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precedent this follows, in as many words: a chip must be able to say how much
    it would show BEFORE you press it, or the row rearranges itself as you use it."""
    plain, session = await _roster(monkeypatch, window="1d")
    filtered, filtered_session = await _roster(monkeypatch, window="1d", provenance=["gated"])

    assert plain["provenance_stations"] == {"direct": 3, "gated": 2, "rf": 1}
    assert filtered["provenance_stations"] == plain["provenance_stations"]
    # ...because the counting query carries no HAVING and no chip fragment at all: it is
    # the window and the classified predicate, nothing else.
    counts = next(s for s in filtered_session.sql if "p_direct" in s)
    assert "HAVING" not in counts
    assert counts.endswith(
        "FROM app.aprs_packets WHERE origin_call IS NOT NULL"
        " AND heard_at >= now() - interval '1 day'"
    )


async def test_the_counts_are_stations_rather_than_packets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chip reading 1500 beside a list of four stations would be lying about what
    pressing it does — the same reason the kind counts are stations."""
    _, session = await _roster(monkeypatch, window="1d")

    counts = next(s for s in session.sql if "p_direct" in s)
    assert counts.count("count(DISTINCT origin_call) FILTER") == 3


async def test_a_provenance_with_nothing_in_the_window_reports_zero_rather_than_vanishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner has no terminal (CLAUDE.md rule 10), so "the filter matched nothing" and
    "the filter never reached the box" have to look different on a phone. Every state is
    always present in the payload, and a zero is what says which of the two it is."""
    monkeypatch.setitem(_ARRIVALS, "p_gated", 0)
    out, _ = await _roster(monkeypatch, window="1d", provenance=["gated"])

    assert out["provenance_stations"] == {"direct": 3, "gated": 0, "rf": 1}


# --- what a row carries ----------------------------------------------------------------


async def test_a_station_says_every_way_it_arrived_not_just_its_last_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interesting case, and the one that would make the filter read as a lie: this
    station's newest frame is gated, but it was also heard direct inside the range. The
    row has to be able to say so, or a station the Direct chip returned reads
    "gated via N4TDX" and contradicts the chip that returned it."""
    out, _ = await _roster(monkeypatch, window="1d", provenance=["direct"])

    station = out["stations"][0]
    assert station["heard"] == ["direct", "gated"]
    # ...while the row's own line still describes the NEWEST frame, which is a different
    # fact and stays one.
    assert station["gated"] is True
    assert station["direct"] is False


async def test_a_station_heard_only_one_way_says_only_that(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {**_ROW, "any_direct": False, "any_gated": False, "any_rf": True}

    out, _ = await _roster(monkeypatch, rows=[row], window="1d")

    assert out["stations"][0]["heard"] == ["rf"]
