"""The pure geofence hysteresis state machine."""

from jbrain.locations.geofence import FenceObs, FenceState, evaluate

_OUT = FenceObs(inside=False, inside_buffered=False)
_IN = FenceObs(inside=True, inside_buffered=True)
_EDGE = FenceObs(inside=False, inside_buffered=True)  # in the exit buffer, not inside


def test_first_outside_observation_settles_silently() -> None:
    state, transition = evaluate(FenceState("unknown", 0), _OUT)
    assert (state.state, transition) == ("outside", None)


def test_entering_requires_two_confirming_fixes() -> None:
    s1, t1 = evaluate(FenceState("outside", 0), _IN)
    assert (s1.state, t1) == ("outside", None)  # one inside fix: not yet
    s2, t2 = evaluate(s1, _IN)
    assert (s2.state, t2) == ("inside", "enter")  # second confirms


def test_a_single_stray_inside_does_not_flip() -> None:
    s1, _ = evaluate(FenceState("outside", 0), _IN)  # confirming = 1
    s2, t2 = evaluate(s1, _OUT)  # then leaves again
    assert (s2.state, t2) == ("outside", None)  # reset, no spurious enter


def test_staying_inside_within_the_buffer_does_not_exit() -> None:
    state, transition = evaluate(FenceState("inside", 0), _EDGE)
    assert (state.state, transition) == ("inside", None)


def test_leaving_requires_two_clearly_outside_fixes() -> None:
    s1, t1 = evaluate(FenceState("inside", 0), _OUT)
    assert (s1.state, t1) == ("inside", None)  # one outside fix: not yet
    s2, t2 = evaluate(s1, _OUT)
    assert (s2.state, t2) == ("outside", "exit")


def test_exit_debounce_resets_if_it_comes_back_into_buffer() -> None:
    s1, _ = evaluate(FenceState("inside", 0), _OUT)  # confirming = 1 toward exit
    s2, t2 = evaluate(s1, _EDGE)  # back within buffer
    assert (s2.state, s2.confirming, t2) == ("inside", 0, None)
