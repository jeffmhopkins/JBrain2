"""The 44px rule, gated on the controls R3f added (frontend/src/styles.css).

`docs/reference/DESIGN.md` is binding on two lines: *"Touch targets ≥ 44×44px"*, and
*"compact-variant rows may reduce to 36px height but never shrink tap areas below 44px
including padding."* Nothing enforced it, and R3f's review found three controls under it
— a `.chip` made a `<button>` is 3px of padding around 9px of text, about 17px of box,
and it sits INSIDE a stream row whose own tap opens the note screen, so a near-miss does
not no-op, it opens the wrong screen.

**Why the gate lives in pytest and reads a CSS file.** It is the shape
`test_tool_step_polish.py` and `test_live_phase_labels.py` already use — a Python test
parsing a frontend source that is the single source of truth. The frontend's own suite
cannot do it: jsdom computes no layout, so no rendering test can measure a tap target,
and vitest stubs a `?raw` stylesheet import to the empty string.

**Scoped to this wave's controls, not to every button in the app**, for the reason
§3b I4 gave when it scoped the live-phase gate: a gate that lands red over hundreds of
existing selectors is a gate that gets skipped. What it pins is that each of these three
declares the minimum, which is exactly what a later edit would drop silently."""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_STYLES = _REPO / "frontend" / "src" / "styles.css"


def _rule(selector: str) -> str:
    """The declaration block of one rule, by its exact selector text."""
    src = _STYLES.read_text(encoding="utf-8")
    at = src.index(f"\n{selector} {{")
    return src[at : src.index("}", at)]


def test_the_streams_ask_chip_is_tappable_without_being_redrawn() -> None:
    """The drawing is untouched and only the HIT AREA grows — DESIGN.md's own precedent
    for making a small thing a control (the vitals chart: "the drawing is untouched … and
    only the hit area grows to clear the 44px minimum"). The chip has to keep the stream
    row's density: it is one chip in a wrapping row inside a taller row."""
    hit = _rule("button.chip-ask::before")
    assert re.search(r"position:\s*absolute", hit)
    assert re.search(r"min-width:\s*44px", hit)
    assert re.search(r"min-height:\s*44px", hit)
    # The chips row claims the height the hit area bleeds into, so the growth cannot
    # reach the note body above it — whose tap opens the note screen instead.
    assert re.search(r"min-height:\s*44px", _rule(".note-chips:has(.chip-ask)"))


def test_a_question_candidate_is_a_44px_box() -> None:
    """Here the BOX grows rather than a bleeding hit area, which is the one place this
    departs from the vitals precedent and does so on purpose: the candidates WRAP, so a
    hit area 44px tall centred on a 26px chip reaches ~9px into the row above and below,
    and a tap near the edge then lands on the WRONG candidate. Offering a mistaken pick
    is the mispairing this block exists to refuse, and the block has the vertical room a
    top bar does not."""
    opt = _rule(".fb-shell .fb-q-opt")
    assert re.search(r"min-width:\s*44px", opt)
    assert re.search(r"min-height:\s*44px", opt)


def test_a_questions_typed_field_is_44px_tall() -> None:
    """The field a no-candidate question gets, and the one "Something else" reveals."""
    assert re.search(r"min-height:\s*44px", _rule(".fb-shell .fb-q-input"))
