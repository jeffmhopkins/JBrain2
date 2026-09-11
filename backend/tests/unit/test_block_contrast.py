"""DESIGN.md's OTHER binding number, over the block whose whole job is to be read.

*"Text contrast ≥ 4.5:1 against its surface in both themes"* is as binding as the 44px
`test_tap_targets.py` gates, and it went unchecked in exactly the way that rule is easiest
to break: not by choosing a bad colour, but by dimming a CONTAINER. R3f's frozen question
block carried `opacity: 0.72`, which multiplies every colour beneath it and cannot be undone
by a descendant — so the "still open" line computed 1.81:1 in light, the answer's own words
3.41:1 (`--text-2`, which DESIGN.md certifies as body text), and each had to be bought back
one at a time by re-colouring. Three review rounds read that sheet; none of them multiplied.

The block is where this matters most: it is the only thing on the owner's screen that
reports what a send did, and a line he cannot read is a line that says nothing about a row
the agent may be about to ask him again.

So this computes the real figures from the real tokens — sRGB relative luminance, WCAG 2.x
— rather than asserting that a particular token name appears. A future re-colour that keeps
the rule readable passes; one that does not, fails with the number it reached.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_STYLES = _REPO / "frontend" / "src" / "styles.css"
_TOKENS = _REPO / "frontend" / "src" / "styles" / "tokens.css"

# DESIGN.md "Accessibility". Not a preference: the two themes are both shipped, and the
# owner's is the only screen this system has.
FLOOR = 4.5


def _block(source: str, opener: str) -> str:
    """One rule's declarations, with its comments taken out — this file's own prose says
    `opacity: 0.72` several times over, and a gate that reads a comment as a declaration
    gates nothing."""
    at = source.index(opener)
    return re.sub(r"/\*.*?\*/", "", source[at : source.index("}", at)], flags=re.S)


def _theme_tokens() -> dict[str, dict[str, str]]:
    """The two shipped palettes, light resolved over dark's defaults the way the cascade
    does (`[data-theme="light"]` overrides a subset; everything else inherits `:root`)."""
    src = _TOKENS.read_text(encoding="utf-8")

    def decls(opener: str) -> dict[str, str]:
        return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", _block(src, opener)))

    dark = decls(":root {")
    light = {**dark, **decls('[data-theme="light"] {')}
    return {"dark": dark, "light": light}


def _colour(value: str, tokens: dict[str, str]) -> str:
    """One declaration's colour, resolved through `var()` to a hex literal."""
    seen = 0
    while (m := re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())) and seen < 8:
        value = tokens[m.group(1)]
        seen += 1
    value = value.strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"not a plain hex colour: {value!r}"
    return value


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _declared(rule: str, prop: str) -> str | None:
    m = re.search(rf"(?<![-\w]){prop}:\s*([^;]+);", rule)
    return m.group(1).strip() if m else None


def test_the_frozen_block_is_dimmed_by_token_and_never_by_opacity() -> None:
    """The mechanism, pinned where the arithmetic below cannot see it.

    Contrast is computed token-to-token, so an `opacity` on the block (or on the live one
    above it) makes every figure in this file wrong without changing a single colour. It is
    the one declaration that re-prices text it does not mention."""
    src = _STYLES.read_text(encoding="utf-8")
    for selector in (".fb-shell .fb-qblock {", ".fb-shell .fb-qblock-done {"):
        assert _declared(_block(src, selector), "opacity") is None, (
            f"{selector} dims by opacity, which multiplies every line inside it"
        )


def test_every_line_of_a_frozen_question_block_clears_the_floor() -> None:
    """The four lines R3f's opacity took under it, in both themes.

    Each is read off the sheet rather than named here: the rule's own `color`, over the
    block's own `background` (the frozen block's if it sets one, else the live block's).
    What the test knows is which lines MUST be readable — the count of what the send
    landed, the words a row was answered with, and the line that says a row was not."""
    src = _STYLES.read_text(encoding="utf-8")
    themes = _theme_tokens()
    ground = _declared(_block(src, ".fb-shell .fb-qblock {"), "background")
    done = _declared(_block(src, ".fb-shell .fb-qblock-done {"), "background") or ground
    assert done is not None
    lines = {
        "the header's count": _declared(
            _block(src, ".fb-shell .fb-qblock-done .fb-qblock-head {"), "color"
        ),
        "the answer's own words": _declared(_block(src, ".fb-shell .fb-q-answered {"), "color"),
        "still open — not answered in your reply": _declared(
            _block(src, ".fb-shell .fb-q-open {"), "color"
        ),
    }
    for theme, tokens in themes.items():
        bg = _colour(done, tokens)
        for what, declared in lines.items():
            assert declared is not None, f"{what} declares no colour"
            ratio = _contrast(_colour(declared, tokens), bg)
            assert ratio >= FLOOR, f"{theme}: {what} is {ratio:.2f}:1, under {FLOOR}:1"


def test_a_frozen_candidate_reads_better_than_it_did_dimmed() -> None:
    """The chips are how "spent" is now said: the unpicked ones give up the raised fill and
    drop to `--text-2` on the block's own ground, the picked one keeps its amber tint.

    `--text-2` is DESIGN.md's certified body text and is asserted against the floor; the
    detail line under it is `--text-3`, which is BELOW the floor app-wide and on a live
    block too — a design-system call filed separately, and not something this wave's
    rendering made worse. What is pinned here is that the label clears the floor and that
    the row is not dimmed by opacity."""
    src = _STYLES.read_text(encoding="utf-8")
    themes = _theme_tokens()
    rule = _block(src, ".fb-shell .fb-qblock-done .fb-q-opt:not(.fb-q-picked) {")
    assert _declared(rule, "opacity") is None
    ground = _declared(_block(src, ".fb-shell .fb-qblock {"), "background")
    assert ground is not None
    label = _declared(rule, "color")
    assert label is not None
    for theme, tokens in themes.items():
        ratio = _contrast(_colour(label, tokens), _colour(ground, tokens))
        assert ratio >= FLOOR, f"{theme}: a spent candidate's label is {ratio:.2f}:1"
