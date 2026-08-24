# Vitals level 1 under load — the GUI gate

The vitals detail screen was specified against a quiet box (`../vitals-detail/`,
option I). On a busy one it carries three lists at once — the box's own work, the
turns running, the turns finished in the window — and at the 15-minute range that
is routinely thirty or more rows stacked under a graph that already owns the first
screenful.

A defect made that worse and is fixed separately (`.vitals-detail` is a column flex
container, so its sections were shrinking to fit instead of overflowing into the
scroller: ten box events rendered as one and a half rows and the screen did not
scroll at all). These three mocks are about what the screen should do once it
*does* scroll.

**Status: awaiting the owner's pick.** Whichever wins becomes the binding spec for
`frontend/src/screens/VitalsScreen.tsx` and is recorded in
`docs/reference/DESIGN.md` under "Vitals detail"; the rivals stay because that doc
records *what was chosen over what*.

| Mock | Variant | Shape | Outcome |
|---|---|---|---|
| `j-capped-lists.html` | Capped lists | Four rows per section, the rest behind "Show all N"; sticky headings | — |
| `k-collapsing-header.html` | Collapsing header | Nothing hidden; the graph gives way to a sticky strip carrying the reading and the range pills | — |
| `l-segmented-lists.html` | Segmented lists | Three counted segments, one list rendered at a time, opening on Running | — |

Each file is standalone and interactive — open it directly, no build step. The
control desk drives the **load** (quiet 2/1/3, busy 10/1/6, flood 34/4/26 — the
busy figures are the ones from the owner's report) and the theme.

## Constraints they were built against

- **The fix is assumed, not re-litigated.** Every mock carries
  `.body > * { flex-shrink: 0 }`; the sections are mounted as real children of the
  scroller, never inside a `display: contents` wrapper, which would put them out of
  that rule's reach.
- **The range control decides what the lists contain.** It drives the roster as well
  as the plot, so a treatment that scrolls it out of reach for thirty rows is
  hiding a control, not just a graph.
- **The honesty case survives.** GPU busy covers the whole box, so the note
  explaining that a high reading is not proof the turns are the cause has to stay
  reachable — it is the reason the box-events list exists at all.
- **Live rows are the point of opening the screen.** A running turn and a load in
  flight are what the owner tapped the chart to find; no treatment may put them
  behind more work than a settled row.
- **≥44px targets** for anything new that is tappable, per `docs/reference/DESIGN.md`.
