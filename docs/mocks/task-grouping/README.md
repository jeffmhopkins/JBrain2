# Tasks — grouping & reordering — four directions

Interactive mockups for letting the owner **organize the Tasks surface from the front end**:
sort tasks into **custom, user-named groups** and **reorder** them within (and across) those
groups. Today `TasksScreen` auto-buckets tasks into two fixed, system-defined sections
(**Scheduled** / **On demand**) with no user control over order or membership — these four
directions each propose a different interaction model for owner-defined organization.

Per `docs/reference/DESIGN.md` §"UI development process", a new surface gets a **3–4 variant
review before any wiring** — even when it reuses settled paradigms (the task card, the enable
toggle, the shared bottom `Sheet`, the filter-chip row from Runs). This is that round; **no
decision is baked into `DESIGN.md` yet** — pick one and the reasoning gets written up there in
the same PR.

All four are single-file, dark-first with a working theme toggle, phone-framed (390×820),
tokens-only (no raw hex outside the token sheet), Lucide-style outline icons, no emoji. Each is
**genuinely interactive** — real pointer drag-and-drop where the direction calls for it, live
group counts / status dots, working toggles, and the actual move flows. Same six tasks and three
seed groups (Morning routine · Money · Household) across all four so they compare like-for-like.

## The four directions

### A — drag handles + custom collapsible groups (`a-drag-groups.html`)
**Direct manipulation, everything on one scroll.** A **Reorder** button in the top bar reveals a
`⋮⋮` grab handle on every card and a **＋ New group** row. Drag a card to reorder it, or drop it
under a **different group header** to re-file it — one gesture does both order and membership.
Group headers **collapse** (tap) and **rename** (pencil). Closest evolution of the existing list;
the reorder affordance is gated behind an explicit mode so a normal tap still expands the card.
- *For:* one mental model (drag), membership + order in a single move, familiar iOS-list feel.
- *Against:* cross-group drag over a long list means autoscroll; drag is the only path (see D).

### B — group chips + per-card "Move to" sheet (`b-chips-move-sheet.html`)
**Grouping is a filter; reorder stays lightweight.** A scrollable **chip row** (reusing the Runs
`.filter-chip` pattern) is the group switch — **All** shows every task under its group header,
tapping a chip narrows to one group. Filing a task is a **⋯ → Move to…** bottom `Sheet` (the
settled sheet paradigm), never a drag across the screen. A **⇅** top-bar button arms a small
reorder mode that drags **only within the current view**. Separates the two concerns: *membership*
is a deliberate menu pick, *order* is an opt-in drag.
- *For:* no accidental cross-group moves; the chip row doubles as a fast group filter; menu-based
  move is unambiguous and reachable one-thumb.
- *Against:* moving is two taps not one drag; grouping feels more like filtering than arranging.

### C — swimlane board (`c-board-lanes.html`)
**One group per screen — a phone Kanban.** Each group is a full-width **lane** you swipe between
(pager dots up top); the last panel is **＋ New lane**. Drag a card to reorder within its lane, or
drag to the screen **edge** to carry it into the neighbouring group (edge-dwell pages the board).
Gives each group its own uncluttered space and the strongest "arranging" feel.
- *For:* distinct spatial model, each group breathes, satisfying for heavy organizers.
- *Against:* only one group visible at a time (poor overview), horizontal paging fights vertical
  drag, and it's the biggest departure from the current one-list surface — arguably over-built for
  a handful of tasks.

### D — organize manager (`d-organize-manager.html`)
**Button-driven, batch-friendly, no drag required.** An **Organize** toggle swaps the read list
for a compact editor: each row gets **▲▼** to nudge it within its group and a **Move** chip that
opens the group sheet. Tick several rows and the action bar **re-files them all at once**. This is
the accessible, deterministic path — how reorder works with a keyboard or when drag is unreliable
on-device — and it stands on its own or as the **fallback layer beneath A/B/C**.
- *For:* fully accessible, batch moves, no fragile gesture; reliable on any device.
- *Against:* less tactile; a dedicated mode rather than in-place manipulation.

## Cross-cutting notes

- **Data shape it implies.** All four assume a task gains a **`group`** (a user-named bucket, its
  own small entity: id + name + order) and a **position** within it — replacing today's derived
  Scheduled/On-demand split. That split could survive as a default seeding, or as a saved
  smart-view; worth settling alongside the pick. Reordering persists an explicit order (a
  fractional/int rank per task), so `GET /api/tasks` returns them pre-sorted.
- **Reuse ledger.** Task card, enable toggle + status dot, the shared bottom `Sheet` (B, D), and
  the filter-chip row (B) are all settled paradigms — the variants differ in *interaction*, not in
  new chrome, which is exactly what the "no reuse exemption" clause asks for.
- **Not yet decided:** whether membership and order are one gesture (A/C) or two concerns (B/D);
  whether grouping replaces or augments the Scheduled/On-demand buckets; and whether a drag
  direction ships **with** D's button path as its accessible equivalent (recommended regardless of
  the visual pick).

## Try them
Open any file in a browser. Toggle dark/light top-left. In **A** tap *Reorder* then drag a card's
`⋮⋮` handle across a group boundary. In **B** tap a chip to filter, or a card's *⋯* to move it. In
**C** swipe between lanes and drag a card (vertical drag reorders; the pager dots track your
position). In **D** tap *Organize*, nudge with *▲▼*, tick rows and *Move to…*.
