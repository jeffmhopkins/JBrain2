"""`_is_empty_sitting` — the predicate that decides whether a jmolt sitting did any work.

It gates the night's retry budget, so getting it wrong is expensive in both directions: too
loose and a wedged model spins the hour on retries, too tight and real faults silently eat
the night. The `cost` arm exists because that second failure actually happened — see the
docstring in `jmolt_night.py`.
"""

from jbrain.agent.jmolt_night import _is_empty_sitting


def test_zero_cost_is_empty_whatever_the_steps_say() -> None:
    # A turn that billed nothing did not happen — on this box a missing usage chunk means
    # the stream was cut before the model's real output reached us. Nine such turns were
    # recorded as successful nights' work before this arm existed.
    assert _is_empty_sitting("I looked around.", 4, "end_turn", 0)


def test_a_real_turn_of_the_same_shape_is_not_empty() -> None:
    assert not _is_empty_sitting("I looked around.", 4, "end_turn", 900)


def test_unknown_cost_falls_back_to_the_text_and_step_reading() -> None:
    # The default keeps any caller that cannot supply a cost on the original behaviour.
    assert _is_empty_sitting("", 1, "end_turn")
    assert not _is_empty_sitting("", 4, "end_turn")
    assert not _is_empty_sitting("I sat with a thread.", 1, "end_turn")


def test_a_tool_call_is_never_empty() -> None:
    # steps>1 means a tool ran, which is evidence of a real sitting however quiet it was.
    assert not _is_empty_sitting("", 2, "end_turn", 500)


def test_an_abnormal_stop_is_not_treated_as_the_empty_quirk() -> None:
    # max_tokens/error stops are their own failures and must not be silently retried as if
    # the model had merely said nothing.
    assert not _is_empty_sitting("", 1, "max_tokens", 500)
