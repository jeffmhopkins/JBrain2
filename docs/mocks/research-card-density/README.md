# Research Library — report-card density variants (GUI gate)

A follow-up micro-review over the settled Research Library (`docs/mocks/research-library/`,
DESIGN.md §"Research Library"). The shipped report card reads **too tall on a phone** — it
stacks four vertical bands (38px amber disc + a two-line 15px title · a complexity-badge row ·
a provenance chip row · a `research · date` footer), so only ~4 cards clear the fold. The ask
is **more reports visible vertically** without losing the paradigm.

Open `compare.html` in a browser: it renders **all four variants side by side**, each in its
own phone frame over the **same seven reports**, with a dashed line marking a typical phone
fold so the density trade-off is visible at a glance. A dark/light toggle sits at the top.

Every variant holds the settled contract — the **amber** research accent, the **complexity**
colour (deep `--violet` / comparative `--steel` / simple `--green`), the per-row **⋯** action
menu, tap-title-to-open, and ≥44px tap targets. They differ in *treatment and density*, not
primitives:

| Variant | Treatment | Density |
|---|---|---|
| **A — Tightened current** | Keeps the amber disc + pill chips, but collapses badge/chips/footer into **one meta band** and trims the title to 14px. Most familiar, least risky. On long titles the pill+chips meta can wrap, softening the gain. | ~+1 card |
| **B — Text-meta** | Drops the pill chrome — complexity becomes a **coloured word** leading a single dotted plain-text meta line; the disc stays as the type cue. Quieter, reliably shorter (meta never wraps). | ~+1–2 cards |
| **C — Edge-accent rail** | Complexity moves to a **3px coloured left edge**, freeing the disc's width for the title; date **right-aligns** on the title line; meta is one plain line and can afford to re-add `sources`. A different visual read. | ~+1–2 cards |
| **D — List row** | **Maximum density** — email-style: a one-line clamped title over a muted subline, a small amber glyph, and a chevron. Most cards per screen; long questions lose their second line. | ~+3 cards |

**Not yet chosen.** Once the owner picks, the winner is promoted to the binding card spec, the
reasoning is folded into `docs/reference/DESIGN.md` §"Research Library", and the `.rl-card` /
`.rl-title` / `.rl-*` styles + `ReportRow` in `frontend/src/screens/ResearchScreen.tsx` are
updated to match. The losing variants stay here as the record of the review.
