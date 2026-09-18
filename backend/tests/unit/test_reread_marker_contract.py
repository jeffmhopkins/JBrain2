"""The re-reading's transcript marker, pinned across the two languages that spell it.

`analysis/converse._reread_marker()` writes the turn; `frontend/src/agent/noteFrame.ts`
decides how it renders. The PWA matches it WHOLE — a prefix test ran against every user
message in every session, so an owner message that merely began "[re-read]" rendered as a
channel event with the marker stripped, which is his own words wearing the system's voice.

Equality is only tighter than a prefix while the two spellings agree, and they live in
different languages with no shared fixture. This is the gate that keeps them honest: the
same shape `test_tap_targets.py` uses, a Python test reading the frontend source that is
the single source of truth for its half.
"""

from pathlib import Path

from jbrain.analysis.converse import REREAD_MARK, _reread_marker

_NOTE_FRAME = Path(__file__).resolve().parents[3] / "frontend" / "src" / "agent" / "noteFrame.ts"


def test_the_pwa_spells_the_marker_exactly_as_the_worker_writes_it() -> None:
    src = _NOTE_FRAME.read_text(encoding="utf-8")
    assert f'export const REREAD_MARK = "{REREAD_MARK}";' in src
    # The TS builds the sentence by interpolating the mark, so the half that can drift is
    # the tail — assert THAT, rather than a literal the template never contains.
    tail = _reread_marker().removeprefix(REREAD_MARK)
    assert f"export const REREAD_TURN = `${{REREAD_MARK}}{tail}`;" in src


def test_the_marker_is_a_whole_turn_and_not_a_prefix_of_one() -> None:
    """The property the exact match depends on: the marker turn carries nothing variable
    — no note title, no timestamp, no body — so there is a fixed string to compare
    against. A marker that grew a variable tail would silently stop rendering."""
    assert _reread_marker() == f"{REREAD_MARK} the note changed, so it was read again"
