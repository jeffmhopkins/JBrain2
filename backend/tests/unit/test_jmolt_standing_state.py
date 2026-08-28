"""The one file the night reads back to jmolt (docs/plans/JMOLT_HARDENING_PLAN.md, H4).

Measured, not assumed. With nothing loaded, the closing sitting invents an agent jmolt never
met into its own permanent files 16 times in 20; with one file loaded, 0/20 (p < 0.001,
against a 20% run-to-run drift floor). Four independent conditions closed the gap — a
hand-written note, that note rewritten as a bare activity log, and jmolt's own four files
verbatim — so the file's SHAPE did not matter; having one did.

The empty case is the one that carries risk, and it is why the block is conditional rather
than always-present: asking for standing state without supplying any produced either an
invented agent (7/19) or a confidently false blank slate — "Current conversation: none.
Pending questions: none." — on a sitting whose own ledger said otherwise. Both are then
reloaded the next night as fact.
"""

from __future__ import annotations

from jbrain.agent.jmolt_night import (
    _REFLECTION_PROLOGUE,
    JMOLT_STANDING_FILE,
    JMOLT_STANDING_MAX_BYTES,
    _standing_block,
)


def test_a_file_is_read_back_under_a_provenance_frame() -> None:
    block = _standing_block("- @sootfinch asked me something. I have not answered.")
    assert "@sootfinch asked me something" in block
    assert JMOLT_STANDING_FILE in block
    assert "your own writing" in block


def test_the_frame_denies_the_one_thing_the_file_cannot_be() -> None:
    """NOT the Moltbook DATA fence — "never as instructions to you" applied to jmolt's own
    notes would train out the promise-keeping the persona is built on. But the escalation M2
    worries about is real here and is not real for `scratch_read`: this lands in the PROLOGUE,
    the trusted channel where the owner's advisory note lives. So the boundary is kept and
    narrowed to the true claim."""
    block = _standing_block("something jmolt wrote")
    assert "cannot be is a rule" in block
    assert "note from your human" in block
    assert "never as instructions to you" not in block  # the fence's wording, deliberately not


def test_no_file_means_no_block_at_all() -> None:
    """The load-bearing case. Not an empty heading, not "nothing yet" — nothing."""
    for empty in ("", "   ", "\n\n", "\t\n "):
        assert _standing_block(empty) == ""


def test_nothing_asks_for_standing_state_when_there_is_none() -> None:
    """The failure this prevents is not a missing block, it is a PROMPTED one: asked for
    standing state and given none, the model supplies a plausible fiction. So the question
    must be absent too, not merely unanswered."""
    assert _standing_block("") == ""
    # The only place the file is named is the closing prologue, which is a bootstrap
    # instruction ("if it does not exist yet, make it"), not a request to report state.
    assert JMOLT_STANDING_FILE in _REFLECTION_PROLOGUE
    assert "does not exist yet" in _REFLECTION_PROLOGUE


def test_the_file_is_named_on_exactly_one_sitting() -> None:
    """It rides every sitting; the INSTRUCTION about it does not. An imperative repeated to
    thirteen fresh contexts a night is how a prologue becomes the task list this wave exists
    to get away from."""
    from jbrain.agent.jmolt_night import (
        _CONTINUE_PROLOGUE,
        _RETURNING_PROLOGUE,
        _RITUAL_PROLOGUE,
    )

    named = [
        p
        for p in (_RETURNING_PROLOGUE, _RITUAL_PROLOGUE, _CONTINUE_PROLOGUE)
        if JMOLT_STANDING_FILE in p
    ]
    assert named == []


def test_an_oversized_file_is_capped_and_says_so() -> None:
    """It rides up to thirteen sittings, so an unbounded file would be the prologue. The
    tested fixture was 409 bytes; the cap is ~5x that."""
    block = _standing_block("x" * (JMOLT_STANDING_MAX_BYTES * 3))
    assert len(block.encode("utf-8")) < JMOLT_STANDING_MAX_BYTES + 600
    assert "truncated" in block
    assert "still in the file" in block  # it is not lost, only not all shown


def test_the_cap_does_not_split_a_character() -> None:
    block = _standing_block("é" * JMOLT_STANDING_MAX_BYTES)
    assert "�" not in block  # decoded with errors ignored, never a broken glyph
