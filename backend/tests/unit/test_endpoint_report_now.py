"""ASKING A PANEL TO SPEAK UP, and the package boundary that request crosses.

The decode ring — what the recogniser actually made of a room — rides the telemetry post,
which is on `CHECK_PERIOD_MS`: fifteen minutes. That interval cannot simply be shortened,
because a report at 6-7 s of uptime is how `ROOM_ENDPOINT_PLAN.md` §10.4bh proves a panel
just booted, and rebooting to force a report wipes `s_decode` — the fast path returns the
empty ring it was asked to fetch. So the measurement loop for "she said it; what did the
decoder hear?" was: say it, then wait up to a quarter of an hour.

`telemetry_seq` closes that to one poll. It is a COUNTER the box raises and the panel
remembers, not a flag the box clears: two panels poll independently, so a flag consumed by
the first is a flag the second never sees.

The coupling is the same one that has already cost this project a release twice — the
`/api/jpanel` route paths, and the fifth `heard` field that 422'd every report from an
upgraded panel. Two packages agreeing on a JSON key with nothing reading both. These tests
read both.
"""

from __future__ import annotations

import pathlib

_FW = pathlib.Path(__file__).resolve().parents[3] / "firmware"


def _fw(name: str) -> str:
    return (_FW / "main" / name).read_text(encoding="utf-8")


class TestTheCounterCrossesThePackageBoundary:
    def test_the_box_serves_the_key_the_firmware_reads(self) -> None:
        """One spelling, pinned from both sides. A rename on either is a panel that never
        reports on demand and a box that thinks it asked."""
        from jbrain.api.endpoint import EndpointSettings

        assert "telemetry_seq" in EndpointSettings.model_fields, (
            "the settings model no longer carries the counter the panel polls for"
        )
        assert '"telemetry_seq"' in _fw("ota.c"), (
            "the firmware no longer parses `telemetry_seq`; the box would raise it into silence"
        )

    def test_a_box_that_never_raised_it_serves_a_value_that_asks_for_nothing(self) -> None:
        """0 is the default and must stay inert. A panel adopting its first sighting means a
        non-zero default would be indistinguishable from a request nobody made."""
        from jbrain.api.endpoint import EndpointSettings

        assert EndpointSettings().telemetry_seq == 0

    def test_the_firmware_adopts_before_it_acts(self) -> None:
        """THE BOOT-LOOP BUG THIS PREVENTS. The panel posts when the counter CHANGES, so
        "I have never seen this number" must not count as a change — otherwise every boot,
        and every recovery from an unreachable box, posts an extra report for nothing.

        Pinned as the sentinel and the guard, because the behaviour lives in their pairing.
        """
        src = _fw("main.c")
        assert "int telem_seq = -1;" in src, "the baseline sentinel is gone"
        assert "if (telem_seq < 0) {" in src, (
            "the adopt-before-acting branch is gone; a panel would post once per boot for nothing"
        )

    def test_a_failed_fetch_cannot_look_like_a_change(self) -> None:
        """`apply_settings` writes the counter only on a fetch that SUCCEEDED, and the caller
        seeds its local from the baseline — so an unreachable box leaves the panel where it
        was rather than triggering a report the box never asked for."""
        src = _fw("main.c")
        assert "int seen = telem_seq;" in src, (
            "the slice loop no longer seeds from the baseline; a failed fetch could post"
        )


class TestTheRingReadsBackNamed:
    """`_heard_entry` is what turns the panel's positional tuple into something a person can
    read. It has to survive every arity at once, because a fleet upgrades one panel at a time.
    """

    def test_every_arity_the_fleet_can_be_running(self) -> None:
        from jbrain.api.debug import _heard_entry

        three = _heard_entry(["burp", 21, 1])
        assert three is not None
        assert (three.phrase, three.prob, three.fired) == ("burp", 21, True)
        assert three.count == 1, "a panel that predates the count means one, not zero"
        assert three.raw == "", "absent is not the same as a decode that produced none"

        four = _heard_entry(["dance", 9, 0, 3])
        assert four is not None and four.count == 3 and four.fired is False

        five = _heard_entry(["tell sister", 17, 0, 3, "TfL SgSTk"])
        assert five is not None and five.raw == "TfL SgSTk"

    def test_a_malformed_entry_is_dropped_rather_than_failing_the_read(self) -> None:
        """The whole answer is a diagnosis. One unreadable entry must not cost the other
        eleven — this route is reached precisely when something is already wrong."""
        from jbrain.api.debug import _heard_entry

        assert _heard_entry(["short", 1]) is None
        assert _heard_entry("not a tuple") is None
        assert _heard_entry(None) is None
