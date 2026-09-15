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
    # And no out-of-flow hit area is hung off the chip ANYWHERE in the sheet, however the
    # rule is spelled. ⟲ This was two literal substring checks for `button.chip-ask::before`
    # / `::after` (R3f's third review, finding 4), which a rule written `.chip-ask::before`,
    # `button.chip-ask:before` (one colon) or `.note-chips .chip-ask::after` walks straight
    # past — the property is about the chip, not about one way of naming it.
    src = _STYLES.read_text(encoding="utf-8")
    for at in (m.end() for m in re.finditer(r"\bchip-ask", src)):
        brace = src.find("{", at)
        # Only this selector of a comma list, so a pseudo on a SIBLING selector is not
        # read as one on the chip.
        tail = src[at : brace if brace != -1 else len(src)].split(",")[0]
        assert not re.search(r"::?(before|after)", tail), (
            f"a pseudo-element hit area is hung off the ask chip: {src[at - 8 : brace]!r}"
        )


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


def test_the_answers_action_row_clears_the_floor() -> None:
    """The Thought / Worked chips and the copy and play controls at the foot of an
    answer — four adjacent controls that were 22-25px tall.

    This row is the ONE path to both the thinking trace and what the turn wrote to the
    graph, and the owner reported not being able to see either ("when first analysing
    the note, I can't see the thinking trace"; "I don't see how it actually added the
    entity to the database"). A control that small on the only road to the answer is
    part of that. `.fb-act-play` is also the LONG-PRESS target that arms auto-play — a
    gesture asked of a 25px box."""
    for selector in (
        ".fb-shell .fb-act-chip",
        ".fb-shell .fb-act-copy",
        ".fb-shell .fb-act-play",
    ):
        assert re.search(r"min-height:\s*44px", _rule(selector)), selector


def test_the_mode_row_clears_the_floor_at_every_text_size() -> None:
    """The app's PRIMARY navigation. Its padding is expressed in `em` of a scaled font,
    so at the shipped 0.75 default it computed to ~37px and at 65% to ~34.6px: lowering
    the text size was shrinking every tap target with it. The 44px floor is absolute and
    must not ride `--font-scale`, so the rule needs a `min-height` in px and not padding
    alone."""
    seg = _rule(".seg")
    assert re.search(r"min-height:\s*44px", seg)


def test_the_icon_button_is_a_44px_box_that_does_not_crowd_its_neighbour() -> None:
    """`.icon-btn` is the top bar's glyph button AND the composer's paperclip and send.
    It was 8px of padding around a 22-24px glyph — 38px, or 40px in the composer.

    The second half is the one the first pass of this gate would have missed: the rule
    pulls its layout box back with `margin: -8px`, so the hit area bleeds 8px past what
    the flex `gap` spaces. At `gap: 14px` the paperclip's and send's 44px boxes
    OVERLAPPED by 2px, and a near-miss on attach does not no-op — it sends the note. The
    gap has to clear twice the bleed with room to spare."""
    btn = _rule(".icon-btn")
    assert re.search(r"min-width:\s*44px", btn)
    assert re.search(r"min-height:\s*44px", btn)
    bleed = int(re.search(r"margin:\s*-(\d+)px", btn).group(1))
    for row in (".foot-icons", ".top-bar-right"):
        gap = int(re.search(r"gap:\s*(\d+)px", _rule(row)).group(1))
        assert gap - 2 * bleed >= 8, f"{row}: {gap - 2 * bleed}px between hit areas"


def test_the_older_notes_pill_is_a_button_with_a_buttons_floor() -> None:
    """DESIGN.md Buttons: "All 12px radius, 44px min height." It was ~22px — 9px of text
    in 5px of padding — and it is the only control in the empty upper half of the home
    screen."""
    assert re.search(r"min-height:\s*44px", _rule(".older-pill"))
