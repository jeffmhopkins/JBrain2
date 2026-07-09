# Runs page — filtering & agent-turn visibility (mock round)

> **Status:** Plan · **Last verified:** 2026-07-09
>
> **Decided: B — multi-select show/hide chips + filter sheet** (owner pick).
> Shipped in `frontend/src/screens/RunsScreen.tsx`; the settled pattern +
> reasoning live in `docs/reference/DESIGN.md` ("Runs — filtering"). A and C are
> retained below as the record.

Three interactive mockups for adding **show/hide filtering** to the Runs surface
(`frontend/src/screens/RunsScreen.tsx`), per the DESIGN.md "3–4 distinct
variants" rule. Open each `.html` directly in a browser; each carries a
dark/light toggle and a caption. Tokens only, phone-frame, matched to the
built Direction-C Runs dashboard.

## The problem

On device the recent-runs list is dominated by the scheduler's housekeeping:
`reconcile_pending_integration`, `reconcile_unembedded_notes`,
`reconcile_pending_notes`, and `geofence_sweep` fire every few minutes at
**0 tokens** and **bury the runs that carry signal** — agent turns and note
integrations. There is currently **no filter or search** on the page.

Owner priorities for this round:

1. **Show/hide pipeline vs. other runs** — get the reconcile noise out of the way.
2. **Seeing agent turns is high priority** — a turn should be one tap away, never buried.
3. Nice-to-have: **date range + result-count limit**.

All three mocks use the same seeded dataset (a few agent/integration runs
drowning in reconcile pipeline rows) so the burying is visible and each
approach can be judged on the same data.

## The three variants

### A — `runs-filter-a-segmented.html` — kind segmented control + range/limit bar
A **segmented control** (`All · Agent · Pipeline · Integration`, each with a
live count pill) picks **one lane at a time** — the settled Review-inbox /
Chats-picker paradigm. **Agent is a first-class tab**, so a turn is always one
tap away. A thin sub-line carries the **date range** (Today / 7d / 30d / All)
and a **result limit** select. Exclusive by nature: you always see exactly one
kind. Cheapest to reason about; the trade is you can't see agent + integration
*together* without dropping to All (which re-mixes the noise).

### B — `runs-filter-b-chips-sheet.html` — multi-select show/hide chips + filter sheet
A row of **toggle chips** (`Agent`, `Integration`, `Pipeline`) — the settled
EntityList / Search filter-chip pattern. Tap **Pipeline** to strike it out and
hide the reconcile noise while keeping Agent + Integration; **any combination is
legal**. The sliders button opens a **filter sheet** for date range, result
limit, and a one-tap **"hide reconcile sweeps"** convenience (drops the
0-token `reconcile_*` / `geofence` housekeeping without hiding real pipelines
like `nightly_predicate_sweep`). Additive — you sculpt exactly what stays. Most
expressive; slightly more chrome, and the "hide sweeps" toggle encodes a
name-pattern the backend would need to mark.

### C — `runs-filter-c-grouped.html` — collapsible groups, agent turns pinned
**No filter to operate** — the list is **restructured into collapsible group
cards** (the settled Ops disclosure shell). **Agent turns pin at the top,
expanded by default**; Integrations and the reconcile-heavy Pipelines collapse
to a **one-line roll-up** (`12 runs · all done · 800 tok`, worst-wins state
dot). The noise is one tap away but never in the way. Range + a per-group limit
ride the header. Solves the burying **structurally** rather than by filtering —
nothing to configure, always shows what matters. The trade: a strictly
chronological cross-kind view (what B's "all on" gives) is no longer the
default; time ordering lives *within* each group.

## Open questions for the review

- Is **exclusive-lane (A)**, **additive-chips (B)**, or **structural-grouping
  (C)** the right mental model for this surface?
- Do we need the **date range + count limit** at all in v1, or is "today, last
  50" a fine fixed default (all three degrade gracefully without it)?
- Should the reconcile housekeeping be **hideable by name-pattern** (B's "hide
  sweeps"), by **kind** (A/B), or **folded by grouping** (C) — or should those
  0-token reconcile runs simply **not be logged as first-class runs** at all
  (a backend change that would shrink the problem upstream)? **Resolved:** both —
  B's server-side filter shipped, *and* the upstream cleanup landed: the worker
  **reaps a sweep's run when the fire reconciled nothing**, so idle reconcile /
  geofence fires no longer create runs at all (productive fires still do). See the
  "Runs — filtering" subsection in `docs/reference/DESIGN.md`.

The chosen pattern + reasoning live in the "Runs — filtering" subsection of
`docs/reference/DESIGN.md`; the loser mocks stay here as the record.
