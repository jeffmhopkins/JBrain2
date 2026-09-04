"""Which radio a job gets.

Every case here is a way the box could quietly listen through the wrong antenna. The
one that motivated the module: `rtl_fm` and `rtl_power` are invoked with no `-d`, so
they open whichever librtlsdr enumerates first — measured 2026-09-03 with two NESDR
SMArt v5s attached, one of which had already moved node across a re-plug.
"""

from __future__ import annotations

from jbrain.sdr.roles import GENERAL, Choice, Radio, choose, conflicts, named

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


class TestARadioSomeoneIsAlreadyOnIsNotAFreeOne:
    """`busy`, which came last and was the reason two dongles still took turns.

    MEASURED 2026-09-04: two undedicated NESDR SMArt v5s attached. APRS asked for a
    radio and got `generals[0]`; the tuner asked and got `generals[0]`. The second
    caller met the sidecar's new per-radio 409 naming the radio it had asked for, and
    the other dongle sat idle — the original symptom, through a per-radio sidecar.
    """

    WHIP, WIRE = "09022796", "77192819"

    def _two(self) -> dict[str, Radio]:
        return {self.WHIP: Radio(self.WHIP, "Desk whip"), self.WIRE: Radio(self.WIRE, "Long wire")}

    def test_a_second_job_takes_the_other_radio(self) -> None:
        attached = [self.WHIP, self.WIRE]
        first = choose(self._two(), attached, GENERAL)

        second = choose(self._two(), attached, "aprs", busy=[first.serial or ""])

        assert first.serial == self.WHIP  # serial order, unchanged
        assert second.serial == self.WIRE

    def test_serial_order_still_decides_between_FREE_radios(self) -> None:
        """`busy` only moves a caller off a radio it could not have had anyway. Which
        free radio it gets must stay arbitrary-but-repeatable, or the choice changes
        with whatever the USB stack did this boot."""
        assert choose(self._two(), [self.WIRE, self.WHIP], GENERAL, busy=[]).serial == self.WHIP

    def test_every_general_radio_busy_is_a_409_not_a_settings_problem(self) -> None:
        """Two states that need different words: "everything is in use" is something the
        owner fixes by releasing a session, and "there is no general radio" is something
        they fix in Settings. A busy radio is sorted last, never dropped, so this stays
        the first one."""
        choice = choose(self._two(), [self.WHIP, self.WIRE], GENERAL, busy=[self.WHIP, self.WIRE])

        assert choice.reason == "general"
        assert choice.serial == self.WHIP

    def test_a_DEDICATED_radio_is_still_this_service_s_even_when_busy(self) -> None:
        """The rule `busy` must not touch. Offering a service a different radio because
        its own is in use is the silent antenna change the whole module exists to stop —
        the honest answer is the sidecar's 409 naming that radio."""
        radios = {self.WIRE: Radio(self.WIRE, "Long wire", role="aprs")}

        choice = choose(radios, [self.WHIP, self.WIRE], "aprs", busy=[self.WIRE])

        assert choice.serial == self.WIRE and choice.reason == "dedicated"

    def test_an_unknown_busy_serial_changes_nothing(self) -> None:
        """The sidecar's list and the USB scan are read at different moments, so one can
        name a radio the other does not."""
        assert choose(self._two(), [self.WHIP], GENERAL, busy=["nosuchserial"]).serial == self.WHIP


class TestARadioTheOwnerPointedAt:
    """`choose` answers "which radio should do this"; `named` answers "may that one".

    Both are needed and neither is the other. The launcher's chosen shape makes the
    RADIO the object, so a tap on a radio card is a decision the api honours or refuses
    BY NAME — running the job on a different radio would be the same silent substitution
    this module exists to prevent, reached from the opposite direction.
    """

    def test_the_radio_asked_for_is_the_radio_used(self) -> None:
        got = named(
            _radios(Radio(WHIP, name="Desk whip"), Radio(WIRE, name="Long wire")),
            [WHIP, WIRE],
            GENERAL,
            WIRE,
        )

        # Not `generals[0]`, which is what `choose` would have answered here: serial
        # order picks the whip, and the owner tapped the wire.
        assert got.serial == WIRE
        assert "Long wire" in got.detail

    def test_a_radio_the_owner_has_not_described_is_general_use(self) -> None:
        # Plugging in a new dongle must not need a settings visit before anything works,
        # and that has to hold whether the radio was chosen for you or by you.
        got = named({}, [WHIP], GENERAL, WHIP)

        assert got.serial == WHIP

    def test_a_radio_reserved_for_something_else_is_refused_by_name(self) -> None:
        got = named(
            _radios(Radio(WHIP, name="Desk whip", role="aprs")),
            [WHIP],
            GENERAL,
            WHIP,
        )

        assert got.serial is None
        assert got.reason == "reserved"
        assert "Desk whip" in got.detail
        # The JOB in words, not the stored id: "reserved for aprs" reads like a bug.
        assert "APRS logging" in got.detail

    def test_a_radio_reserved_for_THIS_job_is_exactly_the_right_one(self) -> None:
        got = named(
            _radios(Radio(WHIP, name="Desk whip", role="aprs")),
            [WHIP],
            "aprs",
            WHIP,
        )

        assert got.serial == WHIP

    def test_an_unrecognised_role_still_reserves_the_radio(self) -> None:
        # `_generals` excludes anything that is not GENERAL, so a role this build does
        # not know still keeps the radio. The refusal has to name it rather than call it
        # general use — or crash reaching for a label that is not in the map.
        got = named(_radios(Radio(WHIP, name="Desk whip", role="ads-b")), [WHIP], GENERAL, WHIP)

        assert got.serial is None
        assert "ads-b" in got.detail

    def test_a_radio_that_is_not_attached_is_refused_rather_than_replaced(self) -> None:
        got = named(
            _radios(Radio(WHIP, name="Desk whip"), Radio(WIRE, name="Long wire")),
            [WHIP],
            GENERAL,
            WIRE,
        )

        assert got.serial is None
        assert got.reason == "waiting"
        # Named, because the owner tapped a specific card and "no radio available" would
        # be false — the other one is right there.
        assert "Long wire" in got.detail

    def test_being_busy_is_left_to_the_lease(self) -> None:
        """The sidecar holds one session per radio and answers with a 409 naming the job
        that has it. That is a better sentence than anything composable from a serial
        list, and the only one that cannot already be stale by the time it is read."""
        got = named(_radios(Radio(WHIP, name="Desk whip")), [WHIP], GENERAL, WHIP)

        assert got.serial == WHIP
