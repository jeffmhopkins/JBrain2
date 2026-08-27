"""The prologue must not assert mechanisms that do not exist, or explanations that are not
true (docs/plans/JMOLT_HARDENING_PLAN.md, H1 — E1/G4/G5/G16).

The class of bug these cover is not a crash: it is jmolt being told something false about
its own situation, in the one channel the persona is instructed to trust, every night, and
then writing that belief into the notes it reloads as fact. Two live instances:

- The returning prologue asserted "your human reviews and releases it while the autonomy
  switch is off" as a CONSTANT. The switch has been on since 2026-08-27, so every write made
  since was told a review gate stood in front of it that did not.
- The same sentence pre-supplied an explanation for a missing write — "if something you wrote
  never appears, that is why" — aimed at the one cause that is reassuring. Eight of
  forty-five writes died in the outbox and reached Moltbook never; none were the owner.
"""

from __future__ import annotations

from jbrain.agent.jmolt_night import (
    _RETURNING_PROLOGUE,
    _RITUAL_PROLOGUE,
    _failed_block,
    _release_block,
)


def test_release_block_tells_the_truth_when_autonomy_is_on() -> None:
    block = _release_block(True)
    assert "ON" in block
    assert "without your human reading it first" in block
    assert "reviews" not in block


def test_release_block_tells_the_truth_when_autonomy_is_off() -> None:
    block = _release_block(False)
    assert "OFF" in block
    assert "releases it before it goes anywhere" in block


def test_no_prologue_asserts_the_switch_position_as_a_constant() -> None:
    """The regression itself. Whatever these say about release must come from
    `_release_block`, which reads the live setting, and never from a fixed string."""
    for prologue in (_RETURNING_PROLOGUE, _RITUAL_PROLOGUE):
        assert "autonomy switch is off" not in prologue
        assert "your human reviews" not in prologue


def test_no_prologue_pre_attributes_a_missing_write_to_the_owner() -> None:
    """`_failed_block` supplies the real answer; nothing may supply a comforting one."""
    for prologue in (_RETURNING_PROLOGUE, _RITUAL_PROLOGUE):
        assert "never appears" not in prologue


def test_failed_block_is_empty_when_nothing_failed() -> None:
    assert _failed_block([]) == ""


def test_failed_block_names_the_writes_and_denies_the_owner_explanation() -> None:
    block = _failed_block([("comment", "post-abc", "429 rate limited")])
    assert "post-abc" in block
    assert "429 rate limited" in block
    assert "NOT on the site" in block
    # The explicit correction of what the prologue used to train.
    assert "not your human holding them back" in block


def test_failed_block_caps_its_length() -> None:
    """It rides every sitting of every night and failures are terminal, so the list only
    grows. An uncapped block would eventually be the prologue."""
    many = [("comment", f"post-{i}", "gone") for i in range(60)]
    block = _failed_block(many)
    assert "and 40 more" in block
    assert block.count("\n- ") <= 21
