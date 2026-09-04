"""Which radio a job gets.

Every case here is a way the box could quietly listen through the wrong antenna. The
one that motivated the module: `rtl_fm` and `rtl_power` are invoked with no `-d`, so
they open whichever librtlsdr enumerates first — measured 2026-09-03 with two NESDR
SMArt v5s attached, one of which had already moved node across a re-plug.
"""

from __future__ import annotations

from jbrain.sdr.roles import GENERAL, Choice, Radio, choose, conflicts

WHIP = "09022796"
WIRE = "77192819"
THIRD = "41550903"


def _radios(*radios: Radio) -> dict[str, Radio]:
    return {r.serial: r for r in radios}


class TestADedicatedRadio:
    def test_a_service_uses_the_radio_reserved_for_it(self) -> None:
        got = choose(
            _radios(
                Radio(WHIP, name="Desk whip", role="aprs"),
                Radio(WIRE, name="Long wire"),
            ),
            [WHIP, WIRE],
            "aprs",
        )

        assert got.serial == WHIP
        assert got.reason == "dedicated"

    def test_it_waits_rather_than_moving_when_its_radio_is_unplugged(self) -> None:
        """The refusal the module exists for.

        Substituting here would be silent: the service would keep running, through a
        different antenna, and the owner would find out from a worse signal rather than
        from a sentence. A measurement whose receive path changed without saying so is
        not comparable to the one before it."""
        got = choose(
            _radios(
                Radio(WHIP, name="Desk whip", role="aprs"),
                Radio(WIRE, name="Long wire"),
            ),
            [WIRE],  # the dedicated one is gone; a general one IS available
            "aprs",
        )

        assert got.serial is None
        assert got.reason == "waiting"
        assert "Desk whip" in got.detail

    def test_nothing_else_may_take_a_dedicated_radio(self) -> None:
        got = choose(_radios(Radio(WHIP, name="Desk whip", role="aprs")), [WHIP], GENERAL)

        assert got.serial is None
        assert got.reason == "none"

    def test_two_radios_on_one_service_is_refused_not_guessed(self) -> None:
        """Both would log the same frequency and every frame would be stored twice.
        Picking one silently would make which one arbitrary again."""
        got = choose(
            _radios(
                Radio(WHIP, name="Desk whip", role="aprs"),
                Radio(WIRE, name="Long wire", role="aprs"),
            ),
            [WHIP, WIRE],
            "aprs",
        )

        assert got.serial is None
        assert got.reason == "ambiguous"
        assert "Desk whip" in got.detail and "Long wire" in got.detail


class TestFallingBackWhereItIsSafe:
    def test_a_service_with_no_radio_of_its_own_shares_a_general_one(self) -> None:
        """Dedicated-and-absent WAITS; never-dedicated SHARES. Collapsing those two into
        one rule is what makes a service change antenna without saying so."""
        got = choose(_radios(Radio(WIRE, name="Long wire")), [WIRE], "aprs")

        assert got.serial == WIRE
        assert got.reason == "general"

    def test_an_undescribed_radio_is_usable_immediately(self) -> None:
        """Plugging in a new dongle must not need a settings visit before anything
        works — it is general use until the owner says otherwise."""
        got = choose({}, [THIRD], GENERAL)

        assert got.serial == THIRD
        assert got.reason == "general"

    def test_nothing_attached_says_so(self) -> None:
        got = choose(_radios(Radio(WHIP, name="Desk whip")), [], GENERAL)

        assert got == Choice(None, "none", "No radio attached.")


class TestPickingBetweenGeneralRadios:
    def test_the_choice_is_repeatable_rather_than_enumeration_order(self) -> None:
        """Arbitrary is fine; arbitrary-and-whatever-the-USB-stack-did-this-boot is the
        bug being fixed. Serial order is stable across reboots and re-plugs."""
        radios = _radios(Radio(WHIP, name="Desk whip"), Radio(WIRE, name="Long wire"))

        forwards = choose(radios, [WHIP, WIRE], GENERAL)
        backwards = choose(radios, [WIRE, WHIP], GENERAL)

        assert forwards.serial == backwards.serial == WHIP

    def test_a_dedicated_radio_is_never_the_general_pick(self) -> None:
        got = choose(
            _radios(
                Radio(WHIP, name="Desk whip", role="aprs"),
                Radio(WIRE, name="Long wire"),
            ),
            [WHIP, WIRE],
            GENERAL,
        )

        assert got.serial == WIRE


class TestNamingThemInSentences:
    def test_an_unnamed_radio_is_called_by_its_serial(self) -> None:
        """ "Waiting for " helps nobody, and an empty name is the state a new dongle
        arrives in."""
        got = choose(_radios(Radio(THIRD, role="aprs")), [], "aprs")

        assert THIRD in got.detail

    def test_a_whitespace_name_counts_as_unnamed(self) -> None:
        assert Radio(THIRD, name="   ").label == THIRD


class TestSpottingTheProblemBeforeAnythingRuns:
    def test_conflicts_names_every_double_booked_service(self) -> None:
        """The settings screen shows this on the radio cards; `choose` only meets it
        when a service tries to start, which is too late to be the only warning."""
        found = conflicts(
            _radios(
                Radio(WHIP, role="aprs"),
                Radio(WIRE, role="aprs"),
                Radio(THIRD, role="shortwave"),
            )
        )

        assert found == {"aprs": [WHIP, WIRE]}

    def test_general_radios_never_conflict_with_each_other(self) -> None:
        assert conflicts(_radios(Radio(WHIP), Radio(WIRE), Radio(THIRD))) == {}
