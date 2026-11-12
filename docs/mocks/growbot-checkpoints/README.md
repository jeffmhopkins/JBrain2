# GrowBot → a persona's Checkpoints tab

Kind: Living (mock set) · Status: **picked — B, grouped by day (owner, 2026-11-12)**; binding spec in `docs/reference/DESIGN.md` "GrowBot — the desk robot's screen", *A persona's page* → *Checkpoints* · Last verified: 2026-11-12

The owner's ask (2026-11-12): *"Managing checkpoints should have the ability to delete them as
well. … overall this GUI looks pretty not good. I think maybe we need to borrow the Gui from how
sessions work in j brain's agent conversations?"*, then *"once we learn how that aesthetic works,
we might want to apply that to more places in the growbot GUI"*.

It replaces the G4 mock's checkpoints list ([`../growbot-personas/`](../growbot-personas/README.md),
B) — a name field over tall cards each with *Restore* and *New persona from it* buttons — with
the Chats panel's pieces (`frontend/src/agent/SessionsPanel.tsx`): a slim bar with a ‹ chevron
instead of the *Back* button, tabs with count pills, a dashed *＋ Save a checkpoint* row (the
Chats panel's *＋ New chat*), and one card of compact one-line rows with a swipe-left action rail.

Open any of the three directly in a browser; they share `shared.css` (tokens copied from
`frontend/src/styles/tokens.css`) and `shared.js` (the state, the bar, the tabs, the swipe rail,
the sheets, the toast), so they differ only in how the list is laid out. Drag a row left with the
mouse to open its rail.

- **A — the Chats list** (`a-chats-list.html`): *All · Named · Automatic* segments with counts (the
  Chats panel's *Today · Older · Archived*), then one card of rows.
- **B — grouped by day** (`b-by-day.html`) **[picked]**: the rows under *Today*, *Yesterday* and
  dated headings, named and automatic together; the same rail and row sheet; one armed *Delete
  the N automatic ones* at the end.
- **C — a row opens a sheet** (`c-row-sheet.html`): no swipe; two-line rows (date and why under the
  name), and every action lives in the sheet a tap opens.

## Where else this look fits on the GrowBot screen

The pieces are shared now: `frontend/src/components/SwipeRow.tsx` (the swipe row and its rail),
the `.swipe-*` rules in `styles.css`, and the `.gb-new-row` / `.gb-cp-group` / `.gb-n` pill rules
in `screens/growbot.css`. Candidates for the same treatment, none built yet:

- **Personas tab** — the rows could swipe for *Rename · Duplicate · Delete* (today only on the
  page's *Manage*), with *＋ New persona* as the dashed row instead of the foot button.
- **Phones tab** — swipe a phone for *Sign out this phone* (today only *Sign out every phone*).
- **Memories tab** — a compact list view grouped by day beside the picture grid, swiping to
  delete, for a creature that has met many people.
- **Add a robot → Wi-Fi it should join** — the remembered networks as swipe rows to drop one.
