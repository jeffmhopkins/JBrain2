"""jpanel's addressing, and the coupling that decides whether a panel is reachable at all.

The routes themselves are exercised against real Postgres in
`tests/integration/test_jpanel_message_rls.py`, where the policies live. What is worth testing
without a database is the one piece of pure logic the whole feature hangs off: turning a
principal's LABEL into the name a four-year-old hears, because panels are ordinary `device_key`
principals and that label is the only thing marking one.
"""

import re
from pathlib import Path

from jbrain.api import jpanel


class TestDisplayName:
    def test_a_named_panel_keeps_its_name(self) -> None:
        assert jpanel._display_name("panel Ellie") == "Ellie"
        assert jpanel._display_name("panel  Rae ") == "Rae"

    def test_an_unnamed_panel_is_still_sayable(self) -> None:
        """A four-year-old told "a message from the other one" at least knows a message
        arrived. An empty name would draw a pop-up from nobody."""
        assert jpanel._display_name("room endpoint panel") == "the other one"
        assert jpanel._display_name("panel") == "the other one"

    def test_the_label_the_flash_route_actually_writes_is_one_this_understands(self) -> None:
        """THE COUPLING, PINNED. A panel is reachable only if its label matches what this
        module looks for, and the label is written somewhere else entirely — `/flash`, in
        `endpoint.py`. Nothing connects the two but this test.

        The first cut matched `label LIKE 'panel%'` and therefore silently lost every unit
        flashed WITHOUT a name, because that branch writes "room endpoint panel". The panel
        would have enrolled, polled, and simply never been addressable, with nothing anywhere
        saying why. So both branches of that expression are read out of the source and checked,
        rather than remembered."""
        src = (
            Path(__file__).resolve().parents[2] / "src" / "jbrain" / "api" / "endpoint.py"
        ).read_text()
        match = re.search(r'label = (f"[^"]+"[^\n]*?if name else "([^"]+)")', src)
        assert match is not None, "the /flash label expression moved; re-pin this test"
        named, unnamed = match.group(1), match.group(2)

        # The named branch: whatever prefix it uses must be one `_display_name` strips.
        assert "panel" in named, f"/flash no longer labels a named panel with 'panel': {named}"
        assert jpanel._display_name("panel Ellie") == "Ellie"

        # The unnamed branch, verbatim from the source rather than typed again here.
        assert unnamed == jpanel._UNNAMED_LABEL, (
            f"/flash labels an unnamed panel {unnamed!r} and jpanel looks for "
            f"{jpanel._UNNAMED_LABEL!r} — such a panel would never be addressable"
        )
        assert jpanel._display_name(unnamed) == "the other one"


class TestNameOf:
    def test_the_owner_is_dad(self) -> None:
        """Not "the owner", not a principal id: the word a child hears."""
        assert jpanel._name_of({}, "owner", None) == "Dad"

    def test_an_unknown_panel_still_gets_a_word(self) -> None:
        """A revoked or re-flashed panel leaves rows behind that still have to render. A name
        that resolves to nothing would put an empty string in a pop-up."""
        assert jpanel._name_of({}, "panel", "gone") == "the other one"

    def test_a_known_panel_is_named(self) -> None:
        assert jpanel._name_of({"p1": "Ellie"}, "panel", "p1") == "Ellie"


class TestMessageShape:
    def _row(self, sender_kind: str, sender_device: str | None):
        from datetime import UTC, datetime

        return (
            "11111111-1111-1111-1111-111111111111",
            sender_kind,
            sender_device,
            "owner" if sender_kind == "panel" else "panel",
            None if sender_kind == "panel" else "p1",
            "hello",
            "voice",
            1500,
            datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
            None,
        )

    def test_direction_is_relative_to_the_owner(self) -> None:
        """ "in" and "out" are the PWA's axis, and the owner is the other end of every
        conversation he can see — so the direction is decided by whether a panel sent it."""
        names = {"p1": "Ellie"}
        assert jpanel._row_to_message(self._row("panel", "p1"), names).direction == "in"
        assert jpanel._row_to_message(self._row("owner", None), names).direction == "out"

    def test_an_unplayed_message_reports_no_played_at(self) -> None:
        """`played_at is None` is the entire inbox query and the PWA's unplayed badge; a
        serialisation that turned it into a string would make every message look heard."""
        msg = jpanel._row_to_message(self._row("panel", "p1"), {"p1": "Ellie"})
        assert msg.played_at is None
        assert msg.from_name == "Ellie"
        assert msg.to_name == "Dad"
