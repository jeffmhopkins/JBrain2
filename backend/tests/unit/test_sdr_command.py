"""The credential a command carries.

This is the security core of the wave, and it is a pure function, so it gets tested
like one. What matters is not that a good code passes — it is that every way of getting
one WITHOUT the key fails, that a code cannot be used twice by anyone who overheard it,
and that a missed transmission does not wedge the system for ever.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jbrain.sdr.command import (
    CODE_LENGTH,
    LOOKAHEAD,
    MAX_FAILURES,
    Window,
    armed_at,
    code_for,
    key_from_text,
    key_to_text,
    new_key,
    normalise,
    parse_command,
    verify,
)

KEY = b"\x01" * 32
OTHER = b"\x02" * 32


def test_a_code_is_short_enough_to_key_in_by_hand() -> None:
    # The design must not require a phone app: a mobile radio's head is the floor.
    code = code_for(KEY, 1)

    assert len(code) == CODE_LENGTH
    assert code.isalnum() and code.isupper()


def test_the_same_key_and_counter_always_give_the_same_code() -> None:
    # Both ends compute it independently; if this drifted nothing would ever verify.
    assert code_for(KEY, 42) == code_for(KEY, 42)


def test_a_different_key_gives_a_different_code() -> None:
    assert code_for(KEY, 1) != code_for(OTHER, 1)


def test_consecutive_counters_give_unrelated_codes() -> None:
    codes = {code_for(KEY, n) for n in range(50)}

    # A predictable sequence would let anyone who heard one code compute the next.
    assert len(codes) > 45


def test_the_right_code_verifies() -> None:
    verdict = verify(KEY, 7, code_for(KEY, 7))

    assert verdict.accepted


def test_the_counter_moves_PAST_the_match_so_a_replay_fails() -> None:
    code = code_for(KEY, 7)
    verdict = verify(KEY, 7, code)

    assert verdict.next_counter == 8
    # Everyone in range heard that code. Accepting it twice is the whole attack.
    assert verify(KEY, verdict.next_counter, code).accepted is False


def test_a_code_from_the_wrong_key_never_verifies() -> None:
    assert verify(KEY, 3, code_for(OTHER, 3)).accepted is False


def test_a_code_the_sender_is_ahead_on_still_verifies_and_resyncs() -> None:
    # A transmission that never decoded advanced the SENDER and not the box. Without
    # this the two drift apart on the first missed packet and wedge for ever.
    verdict = verify(KEY, 10, code_for(KEY, 13))

    assert verdict.accepted
    assert verdict.skipped == 3
    assert verdict.next_counter == 14


def test_a_sender_further_ahead_than_the_window_does_not_verify() -> None:
    # The window has to end somewhere: an unbounded search would accept a code from any
    # counter, which is no better than no counter at all.
    assert verify(KEY, 10, code_for(KEY, 10 + LOOKAHEAD + 1)).accepted is False


def test_a_code_from_BEHIND_the_counter_never_verifies() -> None:
    # Forward-only. A recorded code from an earlier exchange must be worthless.
    assert verify(KEY, 20, code_for(KEY, 19)).accepted is False


@pytest.mark.parametrize("bad", ["", "ABC", "ABCDEF", "!!!!!", "12", "ABCDE" * 4])
def test_a_malformed_code_is_refused_without_touching_the_key(bad: str) -> None:
    assert verify(KEY, 1, bad).accepted is False


def test_case_and_spacing_are_the_operators_not_the_protocols() -> None:
    code = code_for(KEY, 5)

    assert verify(KEY, 5, code.lower()).accepted
    assert verify(KEY, 5, f" {code} ").accepted


def test_a_lockout_cannot_be_worn_down_by_guessing() -> None:
    # Checked BEFORE any comparison: once locked, even the RIGHT code is refused, so an
    # attacker cannot keep trying and a legitimate operator has to reset it deliberately.
    verdict = verify(KEY, 1, code_for(KEY, 1), failures=MAX_FAILURES)

    assert verdict.accepted is False
    assert "locked out" in verdict.reason


def test_one_failure_short_of_the_lockout_still_works() -> None:
    assert verify(KEY, 1, code_for(KEY, 1), failures=MAX_FAILURES - 1).accepted


def test_normalise_drops_what_the_alphabet_excludes() -> None:
    # I/O/0/1 are excluded because they misread when spoken or hand-copied; letting them
    # through would silently make a mis-keyed code look like a different valid one.
    assert "I" not in normalise("AIBOC")
    assert "0" not in normalise("A0B1C")


class TestParsingWhatCameOffTheAir:
    def test_a_command_and_a_code_are_split(self) -> None:
        assert parse_command("GATE 7K2M9") == ("GATE", "7K2M9")

    def test_the_command_word_is_case_insensitive(self) -> None:
        assert parse_command("gate 7K2M9") == ("GATE", "7K2M9")

    @pytest.mark.parametrize(
        "info",
        [
            "",
            "GATE",
            "GATE 7K2M9 extra",
            ":KE8XYZ-9 :net tonight 8pm{01",
            "!4129.96N/08141.66W>088/034",
            "GATE! 7K2M9",
            "A" * 40 + " 7K2M9",
        ],
    )
    def test_ordinary_traffic_is_not_treated_as_a_command(self, info: str) -> None:
        # A packet channel is mostly other people. Anything looser would try to verify
        # half of it — filling the attempt log with noise and burning the lockout on
        # strangers' beacons.
        assert parse_command(info) is None


def test_a_generated_key_round_trips_through_the_text_the_owner_copies() -> None:
    key = new_key()

    assert key_from_text(key_to_text(key)) == key
    assert len(key) == 32


def test_two_generated_keys_differ() -> None:
    assert new_key() != new_key()


# --- when the command is listening at all --------------------------------------------
# A window is a security control, not a convenience: outside its hours the command does
# not exist. Two things matter more than the happy path — that it is checked at VERIFY
# time (so an out-of-hours command is refused, never deferred), and that a window the
# owner typed wrong cannot lock them out of their own gate.


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_an_empty_window_is_always_armed() -> None:
    # The default. A command with no window is governed by the task being enabled.
    assert armed_at(Window(), _at("2026-09-02T03:17"))


def test_a_day_outside_the_window_is_not_armed() -> None:
    # 2026-09-02 is a Wednesday: Sunday=0 makes that 3.
    assert armed_at(Window(days=(3,)), _at("2026-09-02T12:00"))
    assert not armed_at(Window(days=(3,)), _at("2026-09-03T12:00"))


def test_a_time_range_admits_its_own_hours_and_refuses_the_rest() -> None:
    window = Window(start="07:00", end="09:00")
    assert armed_at(window, _at("2026-09-02T07:00"))
    assert armed_at(window, _at("2026-09-02T09:00"))  # inclusive: 09:00 is still armed
    assert not armed_at(window, _at("2026-09-02T06:59"))
    assert not armed_at(window, _at("2026-09-02T09:01"))


def test_a_window_that_wraps_midnight_is_two_ranges_not_an_empty_one() -> None:
    # The naive `start <= now <= end` makes a 22:00-02:00 window match NOTHING, which
    # fails closed — safe, but it silently breaks the overnight case the owner asked for.
    window = Window(start="22:00", end="02:00")
    assert armed_at(window, _at("2026-09-02T23:30"))
    assert armed_at(window, _at("2026-09-02T01:00"))
    assert not armed_at(window, _at("2026-09-02T12:00"))


def test_the_window_is_read_in_its_own_timezone_not_the_boxs() -> None:
    # 12:00Z is 08:00 in New York, inside a morning window; reading it as UTC would put
    # it outside and refuse a command the owner sent at a perfectly ordinary hour.
    window = Window(start="07:00", end="09:00", timezone="America/New_York")
    assert armed_at(window, _at("2026-09-02T12:00"))
    assert not armed_at(window, _at("2026-09-02T20:00"))


def test_days_and_hours_must_BOTH_admit_the_instant() -> None:
    window = Window(days=(3,), start="07:00", end="09:00")
    assert armed_at(window, _at("2026-09-02T08:00"))
    assert not armed_at(window, _at("2026-09-02T18:00"))  # right day, wrong hour
    assert not armed_at(window, _at("2026-09-03T08:00"))  # right hour, wrong day


def test_a_malformed_window_does_not_lock_the_owner_out() -> None:
    # A window is a narrowing, and a narrowing the box cannot read is not a reason to
    # refuse the owner's gate command from their truck. Fail open HERE precisely because
    # the code itself — which is the actual credential — still has to verify.
    for bad in (Window(start="oops", end="09:00"), Window(start="07:00", end="25:99")):
        assert armed_at(bad, _at("2026-09-02T12:00"))
    assert armed_at(
        Window(start="07:00", end="09:00", timezone="Mars/Olympus"), _at("2026-09-02T08:00")
    )


def test_half_a_range_is_no_range() -> None:
    # An editor that saved a start and no end must not become an all-day refusal.
    assert armed_at(Window(start="07:00"), _at("2026-09-02T23:00"))
    assert armed_at(Window(end="09:00"), _at("2026-09-02T23:00"))


# --- what the review of this wave found ----------------------------------------------


def test_a_code_from_behind_the_counter_is_named_as_SPENT_not_wrong() -> None:
    """The difference decides whether the owner gets locked out for succeeding.

    144.390 is digipeated, so one transmission arrives several times. Every copy after
    the first fails the forward-only check — and if that reads as a guess, five copies of
    a WORKING command spend the whole lockout budget."""
    verdict = verify(KEY, 5, code_for(KEY, 4))

    assert not verdict.accepted  # still refused: forward-only is the whole point
    assert verdict.spent  # but it is ours, so it must not count against the owner
    assert verdict.reason == "code already used"


def test_a_code_that_was_never_ours_is_not_forgiven() -> None:
    # The forgiveness must not quietly disable the lockout.
    assert not verify(KEY, 5, code_for(OTHER, 4)).spent
    assert not verify(KEY, 5, "AAAAA").spent


def test_a_code_from_before_the_lookback_is_treated_as_a_guess() -> None:
    # Bounded, so an attacker cannot mine an old capture for something the box will
    # forgive indefinitely.
    ancient = code_for(KEY, 1)
    assert not verify(KEY, 500, ancient).spent


def test_an_empty_key_is_refused_rather_than_being_a_public_one() -> None:
    """`hmac.new(b"", ...)` is perfectly valid, so an empty key does not raise — it makes
    the codes computable by anyone who has read this repository. A missing key can only
    safely mean refuse."""
    assert not verify(b"", 0, code_for(b"", 0)).accepted
    assert not verify(b"short", 0, code_for(b"short", 0)).accepted


def test_a_unicode_lookalike_cannot_be_recorded_as_a_word_it_is_not() -> None:
    # `str.upper()` is Unicode-aware: the ligature would upper-case into a word the
    # packet never contained, and the attempt log has to say what was transmitted.
    assert parse_command("\ufb05OP AAAAA") is None
    assert parse_command("STOP AAAAA") == ("STOP", "AAAAA")


def test_an_unreadable_timezone_widens_the_window_rather_than_narrowing_it() -> None:
    """A window is a narrowing, and one the box cannot read is not a reason to refuse —
    the code is still the credential.

    Falling back to UTC and evaluating anyway shifts every window by up to twelve hours
    and refuses the owner's own code, in a way that reads as their configuration error
    rather than a missing tzdata package on the box."""
    window = Window(start="08:00", end="09:00", timezone="Not/AZone")

    # 12:30Z is 08:30 in New York — inside the window the owner meant, outside UTC's.
    assert armed_at(window, datetime(2026, 9, 2, 12, 30, tzinfo=UTC))
