"""What the two characters after a position mean.

The tests that carry weight are the OVERLAY ones. Four of the fifteen symbols measured on
the owner's box are overlaid, and a resolver that treats the overlay character as a table
gets all four wrong — including `I#`, which is how the busiest station on the channel says
it is an IGate.
"""

from __future__ import annotations

import pytest

from jbrain.sdr import symbols


@pytest.mark.parametrize(
    ("table", "code", "expected"),
    [
        ("I", "#", "IGate"),  # N4TDX — the IGate relaying three quarters of the channel
        ("W", "a", "Winlink gateway"),  # the WINLINK station
        ("D", "&", "D-STAR gateway"),  # KM4OSL-C, N1MPR-C
        ("D", "a", "D-STAR"),  # KM4OSL-S, N1MPR-S
    ],
)
def test_an_overlay_is_not_a_table(table: str, code: str, expected: str) -> None:
    """The distinction the whole module turns on.

    `I` is not a symbol table — it is a character drawn ON the alternate table's icon.
    Reading it as a table finds nothing and reports an unknown symbol, which is what the
    first cut of this feature did to the station that sends most of the traffic."""
    label, overlay = symbols.resolve(table, code)

    assert label == expected
    assert overlay == table


def test_the_two_real_tables_are_kept_apart() -> None:
    """`/$` is a Phone. `\\$` is a Bank or ATM. They are one character apart and they are
    on different tables, and confusing them filed a station moving at 33 knots as a
    branch of a bank."""
    assert symbols.label("/", "$") == "Phone"
    assert symbols.label("\\", "$") == "Bank or ATM"


def test_the_tables_are_the_2015_ones_not_the_2000_ones() -> None:
    """APRS101 Appendix 2 is stale on eighteen codes, two of them live on this channel.

    A card built from the 2000 spec calls KD4WLE's 442.850 repeater an "antenna" and a
    person a "jogger"."""
    assert symbols.label("/", "r") == "Repeater"  # was "Antenna" until 2007
    assert symbols.label("/", "[") == "Person"  # was "Jogger" until 2015


def test_an_undocumented_overlay_names_its_base_rather_than_guessing() -> None:
    # Any of 0-9A-Z is a legal overlay on any alternate code. Most combinations mean
    # nothing agreed, and the honest answer names what we DO know.
    label, overlay = symbols.resolve("Q", "a")

    assert label is not None
    assert label.startswith("Organisation")
    assert "Q overlay" in label
    assert overlay == "Q"


def test_an_unassigned_code_says_so() -> None:
    """The spec's own instruction for an unknown symbol is the circle-and-slash, "meaning
    NOT". The text has to be as honest as the drawing — a plausible wrong name is worse
    than an admitted gap, because nothing on screen would contradict it."""
    assert symbols.resolve("/", '"') == (None, None)
    assert symbols.label("/", '"') == 'unknown symbol /"'
    # And a frame too short to carry a symbol at all does not crash or invent one.
    assert symbols.label("", "") == "unknown symbol"


def test_every_symbol_on_the_air_resolves() -> None:
    """The fifteen measured over 600 packets on 144.390. This is the working set: if any
    of these regresses, the screen misnames traffic the owner actually receives."""
    measured = {
        "/`": "Dish antenna",
        "/_": "Weather station",
        "/S": "Space shuttle",
        "/r": "Repeater",
        "/-": "House (VHF home station)",
        "/[": "Person",
        "/?": "File server",
        "/k": "Truck",
        "/>": "Car",
        "/$": "Phone",
        "I#": "IGate",
        "Wa": "Winlink gateway",
        "D&": "D-STAR gateway",
        "Da": "D-STAR",
    }
    for pair, expected in measured.items():
        assert symbols.label(pair[0], pair[1]) == expected, pair


def test_both_tables_are_complete_and_hold_no_empty_strings() -> None:
    # 94 printable codes each. An empty string would read as a name on a card; the
    # absence of a meaning is `None` precisely so it cannot.
    for table in (symbols.PRIMARY, symbols.ALTERNATE):
        assert len(table) == 94
        assert all(v is None or v.strip() for v in table.values())
