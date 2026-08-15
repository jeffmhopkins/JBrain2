# Top-bar vitals — the GUI gate

The top bar's right cluster used to hold an 8px sync dot and a lightning-bolt
launcher button. Both are gone; it now shows the box's live vitals — tokens/sec
while a turn streams, and GPU busy %, resampled once a second.

**Chosen: `e-instrument.html`.** It is the binding spec for
`frontend/src/components/TopBarVitals.tsx`; the reasoning lives in
`docs/reference/DESIGN.md` under "Vitals chart". The rivals are kept because
`DESIGN.md` records *what was chosen over what*, and a rejected variant is the
cheapest way to re-read that decision later.

| Mock | Variant | Outcome |
|---|---|---|
| `e-instrument.html` | 12-second strip chart; sync as the baseline rule | **Chosen** |
| `d-typographic.html` | The numerals are the component; sync as a micro-caps word | Rejected |
| `f-stateful-chip.html` | One 2×2 block, glance layer over literal layer | Rejected |

A first round (A/B/C, in the session that produced these) kept the launcher alive
by making the readout its tap target. The owner ruled that out: the bolt goes
entirely and the launcher is reached by swiping up on the omnibox, which left the
whole cluster free for the readout. These three were briefed against that.

Each file is standalone and interactive — open it directly, no build step. The
control desk drives turn state, box load, sync state, theme, and the
no-amdgpu-gauge case.
