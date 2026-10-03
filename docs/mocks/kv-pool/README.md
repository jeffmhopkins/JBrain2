# GUI gate — Flash-Next KV pool view (wave F3b)

> **Status:** Plan · **Last verified:** 2026-10-03
>
> **Decided 2026-10-03: C — one quiet row line with eight slot ticks and "View slots →",
> opening a bottom sheet** is the binding spec (`c-slot-sheet.html`); A and B are kept as the
> rivals. Chosen because it keeps the model row as short as its neighbours while the whole
> pool is one tap away. The reasoning lands in `docs/reference/DESIGN.md` with the F3b
> frontend. Behaviour source: `docs/plans/FLASH_NEXT_ENGINE_PLAN.md`.

Three interactive mocks of the **Flash-Next row** in **LLM settings → On-box models**.
Flash-Next serves **one shared 1,048,576-token KV pool** across **8 role-pinned slots**.
The per-model **context window** and **slots** selects make no sense for it, and the
server refuses changes to them. For a pool model only, the mocks replace them with a
**read-only view of the pool**. Standard-engine rows keep their selects. One is shown
under the Flash-Next row for contrast. **Keep loaded** is unchanged. Open each `.html`
in a browser. Each has a dark/light toggle, a caption, and a dashed **scenario panel**
above the phone that drives every state. A dashed outline in the row marks the new part.

## Shared states (identical data in all three)

The data is the API's `kv_pool`, verbatim: `n_ctx` 1,048,576, and per slot
`{slot, role, label, cap}`:

| Slot | Role | Label | Cap |
|---|---|---|---|
| 0 | interactive | jerv (chat, omnibox) | 256K |
| 1 | ingest | Ingest and analysis | 128K |
| 2 | scheduled | Scheduled tasks | 256K |
| 3 | research | Research and sub-agents | 256K |
| 4 | jcode | jcode | 256K |
| 5 | workshop | Wiki, notes, intake | 128K |
| 6 | pet | Kid pet (full → spills to Small prompts) | 32K |
| 7 | small | Small prompts | 64K |

| State | How to reach it in the mock |
|---|---|
| Flash-Next serving, caps only (no live use) | default |
| Flash-Next not serving (the row is off-engine, so the pool shows the caps it *will* use) | **Engine → Standard serving** |
| Live per-slot use, **if the API reports it** (illustrative numbers that drift slightly while live) | **Live use → idle / busy** |
| Pool full: the router has **freed an idle slot** (slot 4 · jcode, 210K) so research could grow | **Live use → pool full** |
| Today's row: the window and slot selects, which the server refuses with a 409 | **Row → Before (today)** |

The overcommit appears in every variant, kept light. The caps are per-slot limits and
add up to **1.34M**, more than the **1M** pool, and the router frees idle slots when the
pool would overrun. Each variant also carries a lock and the words *set by the
engine*. Nothing in the view is editable.

Roles are colored in **paired hues by job family**: steel for you (interactive), amber
for background work (ingest, scheduled), violet for agents (research, jcode), green for
writing (workshop, pet) and grey for small. The second slot of a pair is lighter.

## The three variants

### A — `a-summary-table.html` — one-line summary that expands to a table
The two select rows become **one row in the same `llm-local-ctx` style**: *KV pool*
followed by a pill reading **8 slots · 1M shared**, which adds *· 681K in use* when live
use is available. A lock and *set by the engine* sit at the right. Tapping the pill opens
a **compact table** inline: slot number, label and role, cap, and an *In use* column when
live use is available. A **Pool 1M** total row closes the table. A one-line footer covers
the overcommit. In the pool-full state the footer instead names the slot the router freed.
*Trade-off:* the smallest change to today's row. When collapsed it takes exactly the
space of one select, and the table is the most precise. A table on a phone is dense,
though, and the overcommit is only a sentence. Nothing *shows* that the caps exceed the
pool.

### B — `b-stacked-bar.html` — the pool as one stacked bar of caps
Always open in the row. **One stacked bar of the eight caps** has a **1M pool marker**,
and the caps run past the marker into a **hatched "over by 352K" tail**. The overcommit
is drawn rather than explained. Tap a segment or a legend entry for that slot's detail
(cap, share of the pool, live use, and Kid pet's spill to Small prompts). With live use
on, a second thin bar shows **what is in use now** on the true 1M scale, with the free
remainder. A two-column legend lists every slot.
*Trade-off:* the best at conveying "one pool, eight shares, oversubscribed" at a glance,
and live use reads instantly. It is the tallest row (about 330 px) and adds a new chart
pattern to LLM settings. Small slots (Kid pet 32K) are slivers you can only reach
through the legend.

### C — `c-slot-sheet.html` — the row stays quiet and a Sheet lists the slots
The row keeps **one quiet line**: *KV pool · 1M shared · 8 slots*, a strip of **eight
slot ticks** (lit in the role color when a slot holds tokens, if available) and **View
slots →**. That opens a **bottom Sheet**. At the top are three stat tiles (Pool, Slots,
and *In use* or *Caps total*). Below them is **one row per slot** with the label, a
**role chip**, a state dot when live and a **cap bar** scaled to the largest cap, which
fills with live use when it is known. Tapping a slot adds one line on what it serves.
Kid pet's line links to Small prompts. A footer explains the overcommit.
*Trade-off:* it keeps the model row as short as today's, and gives the slots the most
room and the friendliest labels. The pool is one tap away, though, and the ticks are a
hint, not a reading. Of the three it adds the most UI (a Sheet), although
`docs/reference/DESIGN.md`'s modal system already has one.
