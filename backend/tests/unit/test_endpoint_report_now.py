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
import re

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


class TestTheVolumeCeilingWasTheBugReport:
    """A clamp reached in normal use is not a safety limit, it is a dead control.

    `VOLUME_MAX` was 85, the stored value was 85, and every further increase clamped back to it —
    so the slider stopped at 85 and looked broken while hiding a real complaint underneath.
    """

    def test_the_owners_number_is_reachable(self) -> None:
        from jbrain.api.endpoint import VOLUME_MAX, EndpointSettings, _clamp

        assert VOLUME_MAX >= 95, "95 was asked for and must not clamp"
        assert _clamp(EndpointSettings(volume=95)).volume == 95

    def test_the_default_is_the_one_that_was_asked_for(self) -> None:
        from jbrain.api.endpoint import EndpointSettings

        assert EndpointSettings().volume == 95

    def test_the_range_the_panel_accepts_is_expressible(self) -> None:
        """`audio_set_levels` takes 0..100 and the slider should reach both ends; anything the
        codec then refuses comes back visibly as `levels` rather than being assumed."""
        from jbrain.api.endpoint import EndpointSettings, _clamp

        assert _clamp(EndpointSettings(volume=100)).volume == 100
        assert _clamp(EndpointSettings(volume=0)).volume == 0
        # Still clamped rather than rejected, which is what keeps a typo safe.
        assert _clamp(EndpointSettings(volume=140)).volume == 100
        assert _clamp(EndpointSettings(volume=-5)).volume == 0

    def test_the_panel_reports_why_it_could_not_reach_the_box(self) -> None:
        """THE FAULT THAT HID ALL OF THIS, AND THE TWO SIBLINGS ADDED AFTER IT HID ANOTHER.

        A request that dies ON the panel never appears in this box's log — the log is a record of
        what arrived, so the failures are exactly what is not in it. The only way to see one is
        for the panel to say so in telemetry.

        THREE PATHS, NOT ONE, because the interesting part is the difference between them. On
        2026-09-29 the settings fetch failed for an hour while the jpanel poll, on the panel's
        other task, kept succeeding against this same box — and that split is what ruled out the
        network. A single counter would have said "some requests fail", which is not a finding.

        Pinned from both sides, like every other field that crosses this boundary: the firmware
        must put it on the wire and the box must NAME it, or `model_dump()` drops it without a
        word — the defect that would have made the diagnostic invisible in precisely the way it
        was added to prevent."""
        import pathlib

        fw = pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main"
        main_c = (fw / "main.c").read_text(encoding="utf-8")
        for field in ("set_err", "poll_err", "talk_err", "box_quiet_s"):
            assert f'\\"{field}\\"' in main_c, f"{field} is no longer on the telemetry wire"

        from jbrain.api.endpoint import TelemetryIn

        got = TelemetryIn(
            version="0.3.33",
            uptime_ms=1,
            set_err="ESP_ERR_NO_MEM",
            set_fails=7,
            set_ago_s=12,
            poll_err="connect",
            poll_fails=2,
            poll_ago_s=340,
            talk_err="http-503",
            talk_fails=1,
            talk_ago_s=5,
            box_quiet_s=1800,
        )
        assert got.set_err == "ESP_ERR_NO_MEM" and got.set_fails == 7 and got.set_ago_s == 12
        assert got.poll_err == "connect" and got.poll_fails == 2 and got.poll_ago_s == 340
        assert got.talk_err == "http-503" and got.talk_fails == 1 and got.talk_ago_s == 5
        assert got.box_quiet_s == 1800

    def test_a_panel_that_has_never_reached_the_box_does_not_report_zero(self) -> None:
        """-1 MEANS NEVER AND 0 MEANS JUST NOW, and they are the opposite findings.

        A panel that has never once reached its box — a wrong key, a wrong URL, a box that was
        never up — would otherwise show the healthiest-looking number on the page. The default
        matters as much as the value: a panel too old to send the field must not be recorded as
        having just spoken to us."""
        from jbrain.api.endpoint import TelemetryIn

        assert TelemetryIn(version="0.3.32", uptime_ms=1).box_quiet_s == -1

    def test_the_panel_can_stop_believing_its_own_radio(self) -> None:
        """THE RECOVERY PATH THAT EXISTED AND COULD NEVER RUN.

        `main.c` set `joined` once at boot and nothing ever cleared it, so `net_retry` was
        reachable only on a panel that failed to join in the FIRST place. The case that actually
        happens is the half-open link — still associated, still holding an IP, no route to
        anything — and ESP-IDF fires no disconnect event for it, so the event handler's own
        reconnect never ran either. Both recovery paths idle while every request failed.

        Lydian's panel, 2026-09-29: silent from 04:24 for eight and a half hours, last report
        `wifi_drops: 0`. Not one disconnect the whole time.

        So the BOX is the link test now — `reach_quiet_ms`, which already existed for the
        telemetry — and the never-reached case is excluded, because a panel that has not once
        reached its box has a problem a re-join cannot fix and thrashing the radio would make a
        second one."""
        import pathlib

        main_c = (
            pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / "main.c"
        ).read_text(encoding="utf-8")
        # THE CONDITION, NOT A WORD THAT APPEARS NEARBY. The first version of this asserted
        # `"REACH_NEVER" in main_c` and passed with the guard deleted, because the comment
        # explaining the guard also says `REACH_NEVER` — a test that reads prose rather than code.
        guard = re.search(
            r"if \(joined && quiet_ms != REACH_NEVER && quiet_ms > LINK_SUSPECT_MS\)", main_c
        )
        assert guard, (
            "the link watchdog's condition changed. All three clauses carry weight: `joined` "
            "keeps it off a panel already retrying, `!= REACH_NEVER` keeps a panel that has "
            "NEVER found its box from dropping its association every period over a "
            "configuration problem a re-join cannot fix, and the threshold is what separates a "
            "broken link from a slow one"
        )
        # The counter, or a recovery that fires cannot be told from one that never does.
        from jbrain.api.endpoint import TelemetryIn

        assert TelemetryIn(version="0.3.33", uptime_ms=1, relinks=3).relinks == 3
        assert TelemetryIn(version="0.3.32", uptime_ms=1).relinks == 0

    def test_every_talk_failure_the_firmware_can_hit_carries_a_name(self) -> None:
        """THE RED DASH THE OWNER SAW, AND WHY IT SAID NOTHING. `talk.c` had six ways to fail and
        one of them — `esp_http_client_open` returning non-OK, the FAST one, the one behind *"it
        didn't time out"* — reported itself only to a serial console that does not exist in a
        bedroom (CLAUDE.md #10).

        Asserted as "no `goto done` is unnamed" rather than by listing the reasons, because a
        seventh failure path added later is exactly the one that would go unnamed, and a test
        that enumerates today's six would not notice."""
        import pathlib
        import re

        talk = (
            pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / "talk.c"
        ).read_text(encoding="utf-8")
        turn = talk[talk.index("static void turn(") : talk.index("static void talk_task(")]
        # Each `goto done` must be preceded, within its own branch, by a `why = ...`.
        for m in re.finditer(r"goto done;", turn):
            before = turn[max(0, m.start() - 400) : m.start()]
            assert "why =" in before, (
                "a failure path in talk.c reaches `done:` without naming itself, so the box would "
                "record the conversation as failed with no reason — the exact gap that made the "
                "red dash of 2026-09-29 unanswerable"
            )
        assert turn.count("goto done;") >= 5, "the failure paths moved; re-pin this test"


class TestTheButtonGridIsWiredToRealActions:
    """The grid is only worth having if pressing an icon reaches the same code the voice does.

    These read the firmware, because that is the half of this feature no Python test can run —
    and because every other cross-package bug in this project has been two files disagreeing
    with nothing looking at both.
    """

    def _fw(self, name: str) -> str:
        import pathlib

        return (pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / name).read_text(
            encoding="utf-8"
        )

    def test_the_button_toggles_and_is_debounced(self) -> None:
        """A TOGGLE IS ONLY SAFE BECAUSE OF THE DEBOUNCE, and that is the whole point of this
        test. The contact bounces — 15 deliberate presses were reported as 21 — so a raw edge
        toggle would undo itself on the same press that made it, and the panel would look like
        it was ignoring a child.

        WHAT IT TOGGLES CHANGED IN 0.3.26 and this test did not, which is why it sat red on
        `main` through two releases: the button opened the "who?" grid until a long press on the
        pet could do that, and the owner then asked for it as the power control — *"single press
        blinks the screen and stops listening... if I hold the button for say 5 seconds, it
        should go into the lowest power mode."* One gesture per job, and the button is the only
        control that can turn this unit off. The invariant is unchanged: both halves present, so
        a press can always get back out of whatever the last one did."""
        src = self._fw("display.c")
        assert "#define BOOT_DEBOUNCE_MS 250" in src, "the debounce window is gone"
        assert "s_boot_last_ms" in src, "nothing rejects a bounced edge any more"
        assert "if (boot_button_take()) {" in src, "the short press reaches nothing"
        # Both halves: into standby, and back out of it.
        assert "s_standby = true;" in src and 'sleep_wake("button")' in src, (
            "the button no longer both enters and leaves standby"
        )
        # And the hold is a separate edge, or the power-off would fire on every press.
        assert "boot_button_sleep_requested()" in src, "the five-second hold reaches nothing"

    def test_every_icon_reaches_the_same_path_the_voice_does(self) -> None:
        src = self._fw("display.c")
        # One helper each, so a refusal is identical however it was asked for.
        assert "start_send_recording(who == SENDTO_DAD" in src, "the people icons do not record"
        assert "who == SENDTO_PET" in src and "start_listening(now, speaking)" in src, (
            "the pet icon does not start a conversation"
        )

    def test_commands_are_muted_while_a_message_is_recorded(self) -> None:
        """The pet acting on words from inside a message is what put farts into a four-year-old's
        message to her sister. Derived from the state every frame rather than armed at the edges,
        because recording ends five ways and a mute left armed is a panel that stops listening."""
        assert "void speech_mute_commands(bool muted);" in self._fw("speech.h")
        # Matched on the DERIVATION, not on the exact condition: 0.3.26 added `|| s_standby`
        # (a panel told to be quiet must not answer to its name either) and this pin, spelled
        # as an equality, went red on `main` rather than noticing a deliberate widening. What
        # must not change is that the argument is computed from `s_talk` every frame — an
        # armed-at-the-edges mute leaks, because recording ends five ways.
        mute = re.search(r"speech_mute_commands\(([^;]*)\);", self._fw("display.c"))
        assert mute is not None, "nothing mutes the commands any more"
        assert "s_talk == TALK_RECORDING" in mute.group(1), (
            "the mute is no longer derived from the state and can leak"
        )
        # The ring must still fill: a recording is when what it hears is most interesting.
        assert "note_heard(v->phrase, r->prob[0], true, r->raw_string);" in self._fw("speech.c")


class TestTheBrightnessCommandIsDeliberatelyNotFramed:
    """THE FRAMING WAS CORRECT, IT WORKED, AND IT HAD TO BE TAKEN BACK OUT THE SAME DAY.

    The diagnosis still stands. The panel is opened in QSPI mode, where a command is a 32-bit
    prologue rather than a byte: CO5300 datasheet V0.01 p.21 — instruction `02h`, then
    `AD[23:0] = {8'h00, CMD[7:0], 8'h00}`. The vendor driver wraps every command it sends, which
    is why the init array's `0x51 = 0xFF` always worked; the two calls `display.c` made directly
    did not, so a bare `0x51` went out as instruction `0x00`, the controller discarded it, and
    `esp_lcd_panel_io_tx_param` returned `ESP_OK` because the bytes were clocked out regardless.
    The owner found it from the one symptom that ruled out everything else: the panel's OWN
    dim-on-sleep stage, which needs no network, never dimmed either.

    THEN 0.3.16 SHIPPED THE FIX AND BOTH PANELS DIED. Within minutes, in two bedrooms: black
    screen, no audio cue, no response to touch, and — the part that makes it unrecoverable — no
    reboot. Only a power cycle brought them back, and telemetry from both stopped too, so it was
    not merely the renderer.

    WHY THAT SHAPE OF FAILURE IS THE WORST ONE THIS FIRMWARE HAS. `sdkconfig` sets
    `CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT` with a zero delay, so a CRASH self-heals in seconds,
    while `CONFIG_ESP_TASK_WDT_PANIC` is NOT set — so a task merely BLOCKED prints a warning
    every 30 s and sits there forever. And ESP-IDF's `panel_io_spi_tx_param` waits
    `portMAX_DELAY` twice (`spi_device_acquire_bus`, then `spi_device_get_trans_result` for every
    in-flight transfer). Framing the writes turned a discarded no-op into a real call into that
    function, every 30 s, on a bus already carrying ~5 full frames a second.

    WHAT IS MEASURED, AND WHAT IS NOT. Measured: framed hangs the panels, unframed does not.
    NOT established: the trigger. `tx_param` drains in-flight transfers before it transmits, so
    it is not mid-frame corruption; and `display_set_brightness` only raises a flag, so it is not
    a cross-task call either. Both of those were guesses this class used to repeat, and the
    ESP-IDF source refuted them. One panel also ran 956 s healthy on 0.3.16 before dying, so the
    30-second re-assert is not sufficient on its own.

    SO THE ASSERTIONS BELOW ARE INVERTED ON PURPOSE, and this is not a test being loosened to get
    green — it is pinned just as tightly in the opposite direction, because the thing worth
    preventing changed. Brightness being stuck at full is a nuisance; a panel in a four-year-old's
    bedroom that answers nothing until someone walks in and pulls the cable is not, and it cannot
    be diagnosed remotely because the part that would report it is the part that died.

    RE-LANDING IT needs the command writes ordered against the frame blit AND
    `CONFIG_ESP_TASK_WDT_PANIC` on so a hang reboots instead of persisting — worked out on a bench
    panel with a cable, not deployed to a bedroom. Whoever does that will have to change this
    class, which is the point of it.
    """

    def _display_c(self) -> str:
        import pathlib

        return (
            pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / "display.c"
        ).read_text(encoding="utf-8")

    def test_the_opcode_stays_named_as_the_record_but_is_not_used(self) -> None:
        """The macro survives the revert deliberately: it is the datasheet's answer, worked out
        once, and deleting it would mean re-deriving it from p.21 next time. What must NOT come
        back is either call site using it."""
        src = self._display_c()
        assert "#define QSPI_WRITE_OPCODE 0x02" in src, "the datasheet's write opcode is gone"
        assert "QSPI_CMD(0x51)" not in src, "the brightness write is framed again; it hangs panels"
        assert "QSPI_CMD(0x29)" not in src, "the re-assert is framed again; it hangs panels"

    def test_both_runtime_writes_stay_unframed(self) -> None:
        """Pinned positively, which reads backwards until you know the history: these two
        deliberately-inert writes are what keeps the panels alive. A future reader who 'fixes'
        them is re-creating the outage, so the failure message has to say so rather than just
        naming a missing macro."""
        src = self._display_c()
        hung = "0.3.16 framed these and hung both panels"
        assert "tx_param(s_io, 0x51" in src, f"brightness is framed again; {hung}"
        assert "tx_param(s_io, 0x29" in src, f"the re-assert is framed again; {hung}"

    def test_the_retreat_is_explained_where_the_writes_are(self) -> None:
        """The reason lives next to the code, not only here. A bare `0x51` with no comment is
        indistinguishable from the original defect, and the next person to notice brightness is
        broken will re-fix it exactly as I did."""
        src = self._display_c()
        assert "UNFRAMED ON PURPOSE" in src, "the deliberate retreat is no longer explained"

    def test_the_registers_the_datasheet_cleared_are_left_alone(self) -> None:
        """Three plausible-sounding suspects the datasheet acquitted, pinned so nobody 'fixes'
        them later on the same reasoning: `0x53 = 0x20` is sufficient because bit 5 (BCTRL) is
        what gates `0x51` and the CO5300 has no backlight bit (p.186); `0x51` takes one byte
        (p.184); `0x63` is HBM brightness and is inert while `0x66`'s HBM_EN stays 0 (p.196,
        p.199), which it does because nothing writes `0x66` at all."""
        src = self._display_c()
        assert "{0x53, (uint8_t[]){0x20}, 1, 0}," in src, "0x53 changed; 0x20 was correct"
        assert "{0x51, (uint8_t[]){0xFF}, 1, 0}," in src, "0x51 init changed"
        # A WRITE, not a mention: the comment above the fix cites 0x66 by name, and a naive
        # substring check would fail on the explanation of why it must stay unwritten.
        assert "{0x66," not in src, "0x66 is now in the init array; HBM would take over"
        assert "QSPI_CMD(0x66)" not in src, "something now writes 0x66 at runtime"


class TestEveryFieldThePanelSendsIsDeclaredHere:
    """A FIELD THIS MODEL DOES NOT DECLARE IS DISCARDED WITHOUT A WORD, and that has now cost
    this project three times: `tap` was sent for months into a model that dropped it, the
    telemetry arity once 422'd the whole post, and `int_free` — added to the firmware in 0.3.21
    precisely because it was the measurement a design argument had lacked — was silently thrown
    away for a release because only half the change was made.

    Pydantic's default is to ignore extras, so the panel gets a 204, the log line looks healthy,
    and the number simply is not there. Nothing fails. That is why this has to be a test rather
    than care."""

    def _telemetry_keys(self) -> set[str]:
        """The keys the firmware actually writes, read out of its format string rather than
        transcribed — a list maintained by hand is a list that drifts."""
        import pathlib
        import re

        src = (
            pathlib.Path(__file__).resolve().parents[3] / "firmware" / "main" / "main.c"
        ).read_text(encoding="utf-8")
        start = src.index('"{\\"version\\":')
        end = src.index('"pmu_history\\":[', start)
        return set(re.findall(r'\\"([a-z_0-9]+)\\":', src[start:end]))

    def test_the_model_declares_all_of_them(self) -> None:
        from jbrain.api.endpoint import TelemetryIn

        sent = self._telemetry_keys()
        declared = set(TelemetryIn.model_fields)
        missing = sorted(sent - declared)
        assert not missing, (
            f"the panel sends {missing} and this model drops them silently — "
            "add them to TelemetryIn, or the number you added them to the firmware for "
            "will never reach a log"
        )

    def test_the_scan_found_a_realistic_number_of_fields(self) -> None:
        """Guards the guard: if the format string is restructured and the slice above stops
        matching, `_telemetry_keys` would return a handful of keys and the test above would
        pass by finding nothing to check."""
        assert len(self._telemetry_keys()) > 20
