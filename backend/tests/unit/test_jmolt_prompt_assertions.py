"""Every factual claim jmolt's prompt makes about its own situation, mapped to the code that
makes it true (docs/plans/JMOLT_HARDENING_PLAN.md, H1 — G5).

This is the enumeration, and it is a test rather than a document so it cannot drift. The
class of failure it closes: the prompt, the prologues and three module docstrings each
asserted mechanisms that were never built — an index file handed over at the start of a
night, a five-minute warning, a fenced reload, a "molt" ritual — and one of the docstrings
claimed the reload happened *as fenced DATA (M2)*, which was the strongest-looking evidence
in the repo for a mechanism with no implementation at all. Nobody was lying; each claim was
written when it was going to be true, and nothing failed when it stopped being.

So: a claim is allowed in the prompt only if a line here names what implements it. A claim
that loses its implementation has to lose its sentence, and this test is what notices.
"""

from __future__ import annotations

import pytest

from jbrain.agent.agents import AGENTS

PROMPT = AGENTS["jmolt"].prompt

# (phrase that must appear, what implements it). The phrases are deliberately short — this
# pins the CLAIM, not the prose around it, so the persona can be reworded without a
# false failure while a struck mechanism still trips.
IMPLEMENTED = [
    ("one hour each night", "JMOLT_NIGHT_WALL_CLOCK_S, enforced by the night loop"),
    ("scratch_list shows you", "jmoltscratchtools.scratch_list reports files and bytes used"),
    ("time_left", "jmolttimetools, reading the night deadline in settings"),
    ("told to you at the start of every\nsitting", "jmolt_night._release_block, live switch"),
    ("you are told that too, by name", "jmolt_night._failed_block, from the outbox"),
    ("small math problem", "the platform's verification challenge; jmolt_sweep solves it"),
    ("note at the start of a night", "jmolt_night._advisory_block, from app.settings"),
    ("fixed honest", "settings_store.moltbook_disclosure, prepended on profile update"),
]

# Claims that were REMOVED because nothing implements them. Each stays listed so that
# re-adding one is a test failure rather than a nice-sounding sentence nobody checks.
STRUCK = [
    ("you are handed your index file", "nothing loads a file into the prologue"),
    ("five-minute warning", "no warning is emitted; the countdown is in every preamble"),
    ("open a night with a molt", "no molt mechanism exists; the advisory note is the channel"),
    ("16\nfiles, 128 KB total", "a hardcoded quota in prose — DOC_LIFECYCLE bans the pattern"),
    ("If something you wrote never\nappeared, that is why", "trained the misattribution in G4"),
]


@pytest.mark.parametrize(("phrase", "implementation"), IMPLEMENTED)
def test_every_prompt_claim_has_an_implementation(phrase: str, implementation: str) -> None:
    assert phrase in PROMPT, f"claim gone from the prompt but still pinned to: {implementation}"


@pytest.mark.parametrize(("phrase", "why"), STRUCK)
def test_struck_claims_have_not_come_back(phrase: str, why: str) -> None:
    assert phrase not in PROMPT, f"unbuilt claim is back in the prompt: {why}"


def test_the_prompt_does_not_hardcode_a_quota_number() -> None:
    """`docs/DOC_LIFECYCLE.md` bans a volatile counter in prose, and the prompt is prose the
    model treats as fact. The quota lives in `models/jmolt.py` and is reported by the tools,
    so the prompt says there is a budget and where to see it."""
    for number in ("16 files", "128 KB", "24 KB"):
        assert number not in PROMPT
