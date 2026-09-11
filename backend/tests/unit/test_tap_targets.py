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
reaches the minimum — and, for the one that shares a WRAPPING row with other tappables,
that it does so without taking their hit area with it (R3f's second review, finding 2:
a gate that asserts a declaration is not a gate that establishes the property named in
its own comment)."""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_STYLES = _REPO / "frontend" / "src" / "styles.css"


def _rule(selector: str) -> str:
    """The declaration block of one rule, by its exact selector text."""
    src = _STYLES.read_text(encoding="utf-8")
    at = src.index(f"\n{selector} {{")
    return src[at : src.index("}", at)]


def test_the_streams_ask_chip_is_a_44px_box_that_cannot_overlap_its_neighbours() -> None:
    """The 44px minimum, and — the half the first version of this gate did not reach —
    that reaching it costs no OTHER tappable on the row its own hit area.

    ⟲ **It used to assert only that `button.chip-ask::before` declared `min-height: 44px`,
    which was true and did not establish what its comment claimed** (R3f's second review,
    finding 2). `.note-chips` is `flex-wrap: wrap` and holds the attachment links BEFORE
    this chip. On one line the bleeding pseudo-element was contained by the row's own
    floor; on two lines the wrapped chip's 44px pseudo reached ~6px up into line 1, and —
    absolutely positioned on a relative button, so painted after the static flex items —
    it won the hit test. A tap on the bottom third of an attachment chip opened the thread
    instead of the attachment, at ~400px with two ordinary filenames. The old assertions
    all passed over that.

    So this pins the PROPERTY instead: the chip's tap area is its own IN-FLOW box, which
    is the only shape that cannot reach a sibling, whatever the row does. It is the same
    trade `.fb-q-opt` makes below and for the same stated reason — a wrapping row of
    tappables is where a bleeding hit area stops being free."""
    # The premise the whole finding rests on, pinned so it cannot quietly stop being true:
    # the row WRAPS, and it holds other tap targets — the attachment links, rendered into
    # `.note-chips` ahead of this chip (`Stream.tsx`).
    assert re.search(r"flex-wrap:\s*wrap", _rule(".note-chips"))
    stream = (_REPO / "frontend" / "src" / "components" / "Stream.tsx").read_text(encoding="utf-8")
    chips_row = stream[stream.index('<div className="note-chips">') :]
    assert chips_row.index("<a\n") < chips_row.index("<AskChip")

    chip = _rule("button.chip-ask")
    assert re.search(r"min-width:\s*44px", chip)
    assert re.search(r"min-height:\s*44px", chip)
    # IN FLOW: no positioning on the chip, and no out-of-flow hit area hung off it. A
    # `position: absolute` pseudo is exactly what reached the neighbouring chip, so its
    # absence is the property, not a style preference.
    assert not re.search(r"position:\s*(absolute|fixed|relative)", chip)
    src = _STYLES.read_text(encoding="utf-8")
    assert "button.chip-ask::before" not in src
    assert "button.chip-ask::after" not in src


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
