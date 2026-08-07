"""The continuation-turn seed (docs/plans/JERV_PLANNING_TOOL_PLAN.md §9). A pure function, so
it needs no DB. Regression guard for the observed failure where every continuation turn wrote a
transition line and the final step asked "shall I write the guide?" instead of writing it — so
a plan reached "complete" with no deliverable. The seed must re-inject the recorded results
(reuse, don't re-gather) and direct jerv to produce real output, writing the FINAL deliverable
in full rather than asking permission."""

from jbrain.agent.continuation import _continuation_conversation
from jbrain.agent.plantools import format_plan_results


def test_seed_reinjects_the_scratchpad_and_directs_the_deliverable() -> None:
    msgs = _continuation_conversation(
        "- [ ] one\n- [x] two",
        [{"heading": "Step 2", "note": "found A and B"}],
    )
    text = "\n".join(m.text for m in msgs)
    # The recorded results ride along so a later step reuses them (never re-gathers).
    assert "found A and B" in text
    assert "Step results recorded so far" in text
    assert "REUSE the recorded results" in text
    # Record each step's synthesis to the scratchpad.
    assert "write_plan_result" in text
    # A NON-final step still gives the owner a visible one-line recap of what it just did (so the
    # turn isn't empty), while the full findings stay in the recorded result.
    assert "ONE short sentence confirming" in text
    # The final-step directive: write the deliverable IN FULL, and never ask permission.
    assert "WRITE THAT DELIVERABLE IN FULL" in text
    assert "do NOT ask the owner for permission" in text


def test_seed_without_results_omits_the_scratchpad_block() -> None:
    text = "\n".join(m.text for m in _continuation_conversation("- [ ] one", None))
    assert "Step results recorded so far" not in text
    assert format_plan_results(None) == ""
