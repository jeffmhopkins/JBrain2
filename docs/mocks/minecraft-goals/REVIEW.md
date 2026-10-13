# Independent review: Minecraft goals and their progress in the app (M24 follow-up)

> **Status:** Living · **Last verified:** 2026-10-13
>
> An independent review of `a-screen-section.html`, `b-player-sheet.html` and `c-map-tab.html`
> in `docs/mocks/minecraft-goals/` (branch `docs/mc-goal-scan-mocks`, commit `ac57585e`). The
> reviewer did not build them and had not seen the designer's reasoning (PROCESS.md, "GUI
> gate"). The README was read for the brief, the fixture and the states, and its claims were
> checked against the code. Every judgement of the design is of the rendered pages and PNGs,
> measured against `docs/reference/DESIGN.md` ("Principles", "Color tokens", "Accessibility",
> the Minecraft screen, the map, "The Tricorder target on the Minecraft map") and the plan
> (`docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M24, §M17). The format follows
> `../minecraft-map-markers/REVIEW.md`. Nothing in the mocks was changed.

## How it was checked

- **Every PNG was viewed**: `{a,b,c}-{1..7}-*-{dark,light}.png` and `b-1b-sheet-*`, 44 in all.
- **Every page was rendered again** in headless Chromium at 390 × 844 and 320 × 568, dark and
  light, for every hash state (`view=list|detail|finished|offline|empty`, plus B's `sheet`), 180
  renders. Each was audited in the DOM for horizontal overflow, hit areas under 44 px, text under
  4.5:1 against its painted background, and console errors.
  - **No console errors. No page-level overflow.** The only inner overflow is C's player chip
    strip at 320 (294 px in 288), which is a deliberate `overflow-x:auto` strip (C6).
  - **Hit areas:** every control is at least 44 px. The Players-table name buttons (`.prow`)
    measure 20 px tall on their own, but their `::after` overlay covers the row, as the shipped
    table's does.
  - **Contrast:** every word passes in both themes except one: C's offline player chip, whose
    green **M** is 1.9:1 on the light chip (C5).
- **Tokens:** no hex or `rgb()` outside the two token blocks in any of the three files. C's
  terrain is generated inside the picture, as the README says.
- **The light PNGs paint their tints** (the steel icon tiles, the *reached* and *not loaded*
  pills). The capture fault the last two reviews found is not present here.
- **Code checked for the README's claims**: `backend/src/jbrain/agent/minecrafttools.py`
  (`mc_goal_create`, `_goals`, `carry_count`, `chest_counts`, `mc_goal_progress`),
  `backend/migrations/versions/0226_minecraft_dave.py` and `0237_mc_goal_counts.py`, and
  `deploy/minecraft/server.py` (`map_stock`, `_flush_for_search`) with
  `deploy/minecraft/containers.py` (`stock`).
- **Lore:** `bleepbloop_city/notes/lore.md` (at `8e5a76e`). No variant puts words in Dave's
  in-game voice. The far-place line's *deep scans* matches canon ("The deep scan", "The
  Tricorder's short-range scan"). There is no canon line for goals, so nothing here needs one yet.

## Is the README true of the code?

Mostly. These are the places where it isn't:

- **Goals don't know their world.** `app.mc_goals` has a `world` column (0226), but
  `mc_goal_create` never writes it (its `INSERT` lists `xuid, gamertag, title, notes, item,
  item_count`), so every goal's `world` is `NULL`. So:
  - the Sheet's subtitle *BlockyFox's goal · **Castle Hill** · made Oct 12* can't be filled;
  - the **Glass for the sky bridge** state (*not loaded · In **Sky Islands**, which isn't the
    loaded world*) can't be detected, and the data path's `reason: not_loaded` has nothing to
    test;
  - today `mc_goal_progress` counts any goal against the **loaded** world's places, whatever world
    the player meant.
- **"Mine" has a third source.** Plan §M24 decision 2, and `mc_goal_progress`, count the chests
  within 48 blocks of the player's saved places **and of where they stand** (the `"you"` centre,
  added when their position is known). `/map/stock` gives each container to its nearest centre.
  So with BlockyFox 14 blocks from Home, a chest between them can land in the *you* bucket, not
  *Near Home*. The mock's *where the count comes from* has no *Near BlockyFox* row, and the README's
  data path (`chest_counts` over their saved places) leaves it out. The app's sum will differ
  from Dave's unless the endpoint and the Sheet carry it.
- **"Not loaded" is a choice, not a limit.** The README lists *a world that isn't loaded* among
  what the app *can't* count. `/map/stock` takes a `slot` and reads that slot's saved files
  (`_world_folder`). Only the flush is skipped (`"not running"`). An unloaded world's files are
  exact, because nobody is playing it, so the app *can* count it once goals know their world.
  Showing *— / 200* is a fair choice, but the README shouldn't call it impossible. That changes
  open question 6.
- **The far-place cost is per check, not per place.** The Sheet says *In game this place costs
  one of their deep scans*. §M24 decision 3, and the code's one `scans.charge` per call, make it
  **one** deep scan for all far places together, and a repeat from about the same spot is free.
  With two far places, the per-row sentence overstates the cost twice.
- **"Dave asks Tink42 before marking it done"** (`*-1-*`, the reached row) reads as if Dave will
  bring it up. He won't. He offers only when the player asks him for progress in game
  (`mc_goal_progress` adds the ask line to that answer only). Say *done only when Tink42 says so
  to Dave*, or similar.
- **The journal fixture has entries nothing writes.** *Goal made: Get 23 iron blocks
  (iron_block ×23)*, by Dave, isn't written by `mc_goal_create`, and no `auto` source exists yet
  (the plan says *later*). Either drop the entry or note that it needs a write.
- **True as stated:** carried counts only for the goal's own player while online (decision 4,
  the code's `who` check); the app never calls `scans.charge` (its `asker` is `None`, so no far
  list and no charge); the flush is at most every 30 s (`FLUSH_EVERY_S`); the reach is a true
  48-block circle (`math.dist` in `ContainerIndex.stock`), so C's dotted circle is the right
  shape; carried shulker boxes are counted as boxes, not opened. No new table is needed for
  the read-only view. A `world` write on create is a code change, not a migration.

## Cross-variant findings

### What holds in all three

- **The bar is honest and legible.** Solid steel for carried and steel at 45 % for chests, over
  the track, reads in both themes, and the words beside it (*9 carried · 5 in chests near
  Home*) say the same thing, so colour never carries it alone. *Reached* turning the bar green
  with a pill is clear. The hatched *— / 200* is a good "no number" picture.
- **The Sheet is the best thing in the round.** *14 of 23 iron blocks — 9 to go*, one row per
  source, the file age, **Count again**, the shulker caveat and *Counting here is free and never
  spends BlockyFox's deep scans* answer every "why is this number what it is?" a parent would
  ask. The *far* chip explains why Dave's in-game answer can be lower.
- **Read-only, and it says so**: *To change one, BlockyFox asks Dave.* Right for now (Q2).
- **The empty state** teaches the one sentence that makes a goal (*"Dave, remind me I need 23
  iron blocks"*).

### Blocking (whichever variant is picked)

- **X1. Goals have no world** (see above). Before build, `mc_goal_create` must write the loaded
  world, existing `NULL` goals must read as *this world* (or *unknown*), and the Sheet subtitle and
  the not-loaded state must come from that column. Otherwise the fixture's Sky Islands case is
  fiction.
- **X2. The *where they stand* bucket is missing.** Add a *Near BlockyFox (where they are)* row,
  or say in the README that the app counts places only and accept that it will differ from
  Dave. The first matches §M24's definition of *mine*. The offline player has no such row (no
  position), which the README should state.

### Non-blocking

- **X3. The far line overstates the cost** (above). Suggested: *In game, counting places this far
  takes a deep scan (one per check, for all far places). Here it's read from the world files,
  which never does.*
- **X4. *Dave asks … before marking it done*** promises a nudge that doesn't happen (above).
  This is the wording owner question 3 should settle.
- **X5. Breaks inside a value.** At 320 px, and in B's accordion at 390, *· 1 / shulker box not
  opened* wraps between the number and its noun (`a-7`, `b-2`). Use a non-breaking space. Also,
  *not opened* reads as "the player hasn't opened it". *what's inside isn't counted* is truer.
- **X6. The reached pill sits above the line it starts.** *✓ reached* is top-aligned while the
  text after it drops a few pixels (`a-1`, `a-3`, `c` at Tink42). Align it to the first line's
  baseline.
- **X7. The goal glyph is the Tricorder target's.** The item-goal tile, C's Goals tab and the
  empty state use concentric rings with a centre dot. That is exactly the map's Tricorder
  target mark (DESIGN, "The Tricorder target on the Minecraft map": *two rings and a centre
  dot*). On C's map, where both can be drawn, that is a real clash. Elsewhere it's a
  near-miss. A flag (already used for plain goals) or a checklist glyph would avoid it.
- **X8. Cost of counting on open.** *One request per counted open goal* means one `jb:carry`
  console round-trip per goal plus a `/worlds` and a `/map/stock` per dimension. A's section
  counts every player's goals at once. `jb:carry` already takes 1–8 item ids, so batch per
  player (one carry and one stock call for all that player's counted goals).
- **X9. The offline player's Sheet isn't mocked.** `*-4-offline-*` shows Mira_P's rows, but not
  her Sheet. Draw it once, with *On Mira_P · counts while they're on* as a row with no number,
  so the build has a picture of the source list with a missing source.

## Variant A — a Goals section on the Minecraft screen

**Verdict: pass with changes.**

Every player's open goals, with progress, in one look, on the screen where the players already
are. It reuses the screen's section head, card, 56 px row and Sheet, so nothing is new. It holds
at 320 (`a-6`, `a-7`).

**Blocking:** only X1 and X2.

**Non-blocking**
- **A1. Length.** With three players it's already a screen tall (`a-1`), and the README's own
  *against* (no cut-off drawn) stands. Decide the cap before build: for example, open goals
  only, finished goals folded (as drawn), and *Show all N* past about 6 rows.
- **A2. The player sub-header has no action.** *BlockyFox · on now* is a plain header (no
  chevron), which is honest. But it's the natural place to reach that player's Sheet (and B's
  Journal, see the recommendation). Consider making it the Players table's row target.

## Variant B — goals in the player's Sheet

**Verdict: fail as drawn** (sound against DESIGN, but its glance line misleads).

**Blocking**
- **B1. *14/23 iron blocks · 1 more* reads as "one more iron block".** The *1 more* means one
  more goal, but beside a count it reads as the shortfall, which is actually 9 (`b-1`, `b-6`).
  Mira_P's *40/64 oak logs · 1 more* has the same problem. Say *+1 goal*, or *· 2 goals*.
- **B2. All but one goal is hidden behind a tap**, which is the opposite of *each player's goals
  and progress* at a glance. The README admits it.

**Non-blocking**
- **B3. A card in a card.** The opened goal (`b-2`) puts the source list's bordered card inside
  the goals card inside the Sheet, three levels of border at 390 px, and the text column is
  noticeably narrower. The accordion in a Sheet is a new pattern for one surface.
- **B4. Names truncate at 320** (*Bloc…*, *Tink…*, `b-6`). That is the shipped table's column
  split, not B's, but B makes rows taller, so it shows more. Worth a look whichever variant wins.
- **Strength:** the per-player **Journal** under the goals (`b-1b`) belongs in the player's
  Sheet whatever is picked. It's where *what has BlockyFox been up to?* is answered.

## Variant C — a Goals tab on the map

**Verdict: pass with changes as a secondary view, not as the home of goals.**

It's the only variant that shows **which chests count**. The dotted 48-block circle round Home,
*Home · 5 in chests*, *BlockyFox · 9 carried* and the edge plate *Mine outpost · 0 · 620 blocks
NE* (`c-1`) answer *why didn't the outpost count?* in one look. The circle is the true shape
(`/map/stock` uses distance, not a square).

**Blocking**
- **C1. The tabs lose their words at every phone width** (`c-1`, `c-6`): Layers and Timeline become
  bare glyphs. That changes a shipped surface for everyone, to make room for a tab most map
  visits won't use. DESIGN's map has named tabs. Glyph-only tabs need the owner's explicit
  yes, not a side effect.
- **C2. Goals hidden behind the map.** Opening goals means Minecraft → Map → the third tab →
  a player chip. That fails the brief (*each player's goals and progress*) in the same way as B2.

**Non-blocking**
- **C3. The reach circle needs X7 fixed.** With the Tricorder target and the goal glyph sharing a
  shape, a scanning player's target and a goal tab would collide on the same map.
- **C4. A plain goal or one in another world draws nothing**, so the map tab is empty for most
  goals (*Beacon at the base*).
- **C5. The offline chip's letter fails contrast in light.** The green **M** on the light chip
  is 1.9:1 (`c-*-light`, Mira_P's chip). Use `--ok-text` for the letter, or the filled disc with
  `--map-ink-on`.
- **C6. The chip strip clips at 320** (*Mira_P* cut at the edge, `c-6`). It does scroll, but
  nothing shows that. Fade the edge, or wrap the chips.

## Cross-variant comparison

| | A — screen section | B — player Sheet | C — map tab |
|---|---|---|---|
| Every goal's progress with no tap | **Yes** | One per player (B2) | No (C2) |
| Glance line misreads | No | **Yes** (B1) | No |
| Shows *which chests count* | Words only | Words only | **Yes, on the ground** |
| New pattern | None | Accordion in a Sheet (B3) | Glyph-only map tabs (C1), reach circle |
| 320 × 568 | Good | Table names truncate (B4) | Chips clip (C6) |
| Tokens / contrast | Pass | Pass | One fail (C5) |
| Needs X1 / X2 | Yes | Yes | Yes |

## What must change before build (whichever is picked)

1. X1: write `world` on goal creation, and drive the subtitle and the not-loaded state from it.
2. X2: count, and show, the *where they stand* bucket, or document the deliberate difference.
3. X3 and X4: fix the far-cost sentence and the *Dave asks* promise.
4. X8: batch the counting per player.
5. For A: settle the cap (A1). For B: fix B1. For C: get an explicit yes on C1, and fix C5.

## Recommendation

**Agree with the builder: A**, with three additions:

- B's per-player **Journal** in the existing player Sheet, so *latest* lives with the person;
- C's reach circles kept for later as a **Show on the map** link from the Sheet's *Near Home*
  row (the README's question 4), drawn only after X7 gives goals their own glyph;
- X1 and X2 fixed before build, because they decide what numbers the app shows.

C is the most *interesting* picture, but the owner asked to see people's goals, and C hides
them behind the map and costs the map its tab labels. B's glance line is the right size, but
*· 1 more* reads as a count. If it's ever reused, it needs B1's fix.

## What the owner's open questions should account for

- **Q5 (count far places?)** In game, far places cost **one deep scan per check for all of
  them**, not one per place, and a repeat from the same spot is free. The app's free count will
  be *higher* than Dave's only when the player is out of scans. That's rarer than the question
  implies.
- **Q6 (not loaded):** this depends on X1. Today a goal has no world at all. The answer also
  isn't limited to *— / 200* or a stored snapshot: `/map/stock` can read an unloaded slot's
  saved files directly, which are exact for a world nobody is playing. No new column is needed
  for that.
- **Q7 (count on open?)** With X8's batching, counting on open costs one carry call per online
  player, and no forced save. Without it, the cost grows with the number of goals.
- **Q3 (reached):** Dave doesn't currently bring it up on his own (X4). If the owner wants a
  nudge, that's a new behaviour, such as Dave mentioning it the next time the player talks to
  him, and it isn't free.
