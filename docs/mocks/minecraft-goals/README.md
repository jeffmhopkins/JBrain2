# GUI gate — Minecraft goals and their progress in the app (M24 follow-up)

> **Status:** Living · **Last verified:** 2026-10-13
>
> **Decided (owner, 2026-10-13): B, "but expandable if clicked", look-only.** Built per
> `docs/reference/DESIGN.md` "Players' goals on the Minecraft screen" and plan §M24 "Goals in
> the app". Behaviour sources: `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M24 ("Goals that
> count") and "Goals and the progress log" (`app.mc_goals`, `app.mc_goal_log`). The
> independent review is `REVIEW.md`.

## Decision and what the build changed

The owner picked **B** with one change: tapping a player's glance line **expands it in
place** to show all their goals, each with its bar and *where from · as of* line, finished
goals folded under *N done · N abandoned*. A goal opens its detail in place (an accordion);
the row still opens the player Sheet, which gains the Goals section and the latest Journal.
Read-only: the app never edits a goal, and the footnote says players change goals by
asking Dave (open question 2: look-only).

The mocks below are kept as drawn. The build departs from them where the review found them
untrue of the code, or misleading:

- **The glance line** reads *14 / 23 iron blocks · +1 more goal*, not *14/23 iron blocks ·
  1 more* (review B1).
- **Goals know their world.** `mc_goal_create` now writes it; a goal with none reads as the
  loaded world's (X1). A world that isn't loaded **is** counted, from its saved files (which
  are exact while nobody plays it), with a *not loaded* pill and no pockets; the fixture's
  *— / 200* now applies only when nothing could be counted (open question 6).
- **Where they stand counts** (X2): the detail has a *Near {player}* row, so the app's total
  is Dave's.
- **The far cost** is one deep scan **per check, for all far places** (X3), said once under
  the source list; the app never pays it (`scans.charge` is never called).
- **Reached** says *{player} can tell Dave it's done*: Dave doesn't bring it up (X4, open
  question 3).
- **The journal** has no *Goal made* entry: nothing writes one.
- **Counting is batched per player** (X8): one request a player, made when the goals are
  shown (no forced save); **Count again** forces the save, at most every 30 s (open
  question 7).
- **Values never break** (X5): *1 shulker box* is one unit, and its line says *what's
  inside isn't counted*.
- **Glyphs** (X7): a cube for a counted goal, not the Tricorder target's rings.
- **The Sheet** keeps its shipped totals grid above Goals and Journal (the mock drew one
  totals line).

Each player's goals live in `app.mc_goals`. A goal has a title, a status (open, done or
abandoned), when it was made and finished, and, since §M24, an optional item and count
("iron_block ×23"). The progress log, `app.mc_goal_log`, holds what the player typed in game,
Dave's summaries, the owner's entries and, later, automatic events. `mc_goal_progress` counts
a goal's item two ways:

- what the player **carries**, through `jb:carry`, only while they're online;
- the **chests within 48 blocks of each of their saved places** in the loaded world, through
  the sidecar's `/map/stock`, read from the world's saved files.

None of this shows in the app today. These are three mocks of where and how it shows.

Open each `.html` in a browser:

- Each one is self-contained and works offline.
- It is phone-framed and follows the system theme.
- **Mock controls** (dashed) are reviewer chrome. States open by hash: `view=list | detail |
  finished | offline | empty` (B also has `sheet`), and `theme=dark|light`.
- C's map picture is drawn procedurally, like the map mocks. Its terrain colours are raw RGB
  inside the picture.

## What every variant shows (the same fixture)

| Goal | Player | What it shows |
|---|---|---|
| **Get 23 iron blocks** (iron_block ×23) | BlockyFox, online | An item goal partway: **14 / 23**. 9 are carried and 5 are in chests near Home, *as of 3 min ago*. A second place, Mine outpost, is **far** (620 blocks). |
| **Beacon at the base** | BlockyFox | A plain goal: no bar, *No count · made Oct 10*. |
| **20 obsidian for the portal** · **Tame a horse** | BlockyFox | A done goal and an abandoned one, folded under *1 done · 1 abandoned*. |
| **Get 16 torches** (torch ×16) | Tink42, online, no saved places | **Reached**, at 18 / 16. The bar turns green with a *reached* pill and *Dave asks Tink42 before marking it done* (§M24 decision 5: reached means asked, not done). It also says *no saved places, so no chests*. |
| **Get 64 oak logs** (oak_log ×64) | Mira_P, offline | A partial count: 40 / 64 from the chests near Tree farm. It says *what Mira_P carries counts while they're on*. The bar shows only the chests part. |
| **Glass for the sky bridge** (glass ×200) | Mira_P | Unknown: the goal is in **Sky Islands**, which isn't the loaded world. It reads *— / 200* over a hatched bar, with a *not loaded* pill: *counted again when it is*. |
| *(empty)* | — | *No goals yet*. Players make them by asking Dave in game, *"Dave, remind me I need 23 iron blocks"*, and he keeps count. |

**The bar** has two parts:

- what's **carried**, in solid `--steel`;
- what's **in chests near their places**, in `--steel` at 45 % (`color-mix`, no new token);
- the rest is the `--surface-2` track.

A reached goal is `--green` all through. A goal that can't be counted now is the track,
hatched in `--border`. The numbers are tabular and never break inside a value (*as of 3 min
ago* is one unit).

**A goal's detail** opens as a Sheet in A and C, and in place in B. It shows:

- the headline, *14 of 23 iron blocks — 9 to go*, over the big bar and its key;
- **where the count comes from**, one row per source:
  - *On BlockyFox* (inventory, armour and off-hand), 9. It adds *1 shulker box not opened*,
    because stable `@minecraft/server` can't see inside one (§M24);
  - *Near Home* (*chests within 48 blocks · 14 blocks from BlockyFox*), 5;
  - *Near Mine outpost* with a **far** chip. Its line reads *620 blocks from BlockyFox. In
    game this place costs one of their deep scans; here it's read from the world files, which
    never does.*
- *Chests as of 3 min ago (the world's saved files); carried counted just now. Counting here
  is free and never spends BlockyFox's deep scans.*, with **Count again**;
- the **Journal** for that goal (`app.mc_goal_log`, newest first). Each entry has its source:
  *in game* (with the player), *Dave*, *you* (the owner), or *auto*;
- *Goals and the journal are kept by Dave from what players tell him in game. To change one,
  BlockyFox asks Dave.*

## The app never spends a player's deep scans

This holds in every variant. A count in the app runs `mc_goal_progress`'s **app path**, which
the plan already has (§M24 decision 3, *"The app is unlimited"*):

- **Chests:** `/map/stock` reads the world's saved files. It's free and never touches the
  §M17 ledger.
- **Carried:** `jb:carry` runs only for the goal's own player, and only while they're online
  (§M24 decision 4).

So the view charges nothing:

- A **far** place (more than 48 blocks from where the player stands, or in another dimension)
  is counted here like any other. It is only *marked* far, so the owner can see why Dave's
  in-game answer can be lower once the player is out of scans.
- **Opening the screen** reads the files as they are. It doesn't force a save.
- **Count again** asks the sidecar to flush first (`flush=1`, at most every 30 s). That is one
  forced save of the running world, no worse than a hot snapshot, and it charges no player.

What the app *can't* count, it says so and shows no number for:

- a world that isn't loaded (*not loaded*);
- an offline player's pockets (*counts while they're on*).

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-screen-section.html` | *A Goals section on the Minecraft screen.* One card under Players, grouped by player (avatar, name, *on now* or *last on yesterday*). Each open goal is a 56 px row with its bar and its *where from · as of* line. Each player's finished goals fold under *1 done · 1 abandoned*. A row opens the goal's Sheet. |
| **B** | `b-player-sheet.html` | *Goals live in the player's Sheet.* The Players table gains one **glance line** under each player with goals: a 44 px mini bar and *14/23 iron blocks · 1 more*, or *Get 16 torches · reached*. The row opens the existing player Sheet. Under the shipped totals it gets a **Goals** section and the player's latest **Journal**. A goal opens **in place** (an accordion), not in a second Sheet. |
| **C** | `c-map-tab.html` | *A Goals tab on the map.* The map panel's tabs become **Layers · Timeline · Goals**. Goals has a radio chip per player, then that player's goal cards. The chosen counted goal draws on the map: a dotted 48-block **reach circle** in the player's colour round each saved place whose chests count, with *Home · 5 in chests*, the player's *BlockyFox · 9 carried* (or *offline, carried not counted*), and an edge plate for a far place (*Mine outpost · 0 · 620 blocks NE*). A card opens the same Sheet as A. |

- **A — for:** everyone's goals at a glance, where the owner already looks after people. It
  keeps the shipped screen's section and card grammar, the Sheet is the shared shell, and it
  holds up at 320 px.
  **Against:** it lengthens an already long screen by a whole card. With many players it
  needs its own scrolling, or a *show all* cut-off (not drawn).
- **B — for:** no new section. Goals sit with the person they belong to, beside their time
  and sessions, and the glance line still shows progress without a tap. The Sheet becomes a
  real per-player page, which is where the Journal belongs.
  **Against:** you see only one goal per player (the first counted one) without opening the
  Sheet. The accordion inside a Sheet is a new pattern there. The table row grows by a line.
- **C — for:** the only variant that shows **where** the stock is. The reach circles answer
  *"which chests count?"* and *"why didn't the Mine outpost count in game?"* on the ground
  itself. It reuses the map's panel, chips and label rule.
  **Against:** goals are about people, not places, and they're hidden behind the map. Three
  tabs don't fit beside the world picker below 400 px, so the tabs go to their glyphs,
  named for screen readers, at every phone width; that is a change to a shipped surface.
  A plain goal or a goal in another world draws nothing. The reach circle is a new map
  mark, dotted in the player's colour so it never reads as a beacon's dashed square.

## Recommendation

**A**, with C's reach circles left as a later *Show on the map* link from the Sheet's *Near
Home* row (open question 4).

- The owner asked to see *each player's goals and progress*. That is a list of people's goals,
  and A shows all of them in one look on the screen where the players already are.
- B hides all but one goal per player behind a tap.
- C hides goals behind the map and costs the tab labels at every phone width.
- A's Sheet carries everything the count needs to be honest: the sources, the far place, the
  file age, the shulker caveat and the no-deep-scan line. It needs no new pattern.

## Data path (as drawn; built as `GET /api/minecraft/goals` and `GET /api/minecraft/goals/progress?xuid=&flush=`, one request a player — see the plan)

- **Read the goals:** `GET /api/minecraft/goals?slot=`. It returns every player's goals (owner
  RLS, like the rest of `/api/minecraft`) with `item`, `item_count`, `status`, `created_at`
  and `finished_at`, plus the latest few `mc_goal_log` rows per goal. It is a cheap database
  read.
- **Count one goal:** `GET /api/minecraft/goals/{id}/progress?flush=`. It runs the app path of
  `mc_goal_progress`'s counting: `carry_count` for the goal's own player if online, and
  `chest_counts` over their saved places in the loaded world. It **never** calls
  `scans.charge`. It returns `{carried|null, chests:[{place, count, distance, far}], shulker,
  as_of, age_s, reason}`. `reason` is `not_loaded`, `offline` or `no_places`.
  - One request per counted open goal, made when the goals are shown, not on a timer.
  - Each place's distance is from where the player stands now, or from where they logged off.
- No new table. No migration. No pack change.

## Open questions for the owner

1. Which variant: A (a section on the Minecraft screen), B (inside each player's sheet), or C
   (a Goals tab on the map)?
2. Should the app stay read-only for goals, or should you be able to mark a goal done or
   abandoned, or write a journal entry, from the app? The mocks are read-only. The plan
   already has an `owner` source for the journal. A done or abandoned mark from you would be
   a change to the player's own record, which Dave normally makes only when the player says
   yes.
3. When a goal's count is reached, should the app show anything more than the green
   *reached* pill (for example a nudge to remind the player), given Dave asks before marking
   it done?
4. Should a counted goal's detail link to the map with the 48-block reach circles round each
   counted place (C's picture), even if C isn't the pick?
5. Should a far place be counted in the app at all, or shown as *not counted — in game this
   needs a deep scan* so the app always matches what the player would hear from Dave? The
   mocks count it, free, and mark it *far*.
6. For a world that isn't loaded, is *— / 200 · not loaded* right? Or should the app keep the
   last count it made (a small stored snapshot, a new column) and show *14 / 23 as of Oct 11,
   when it was last played*?
7. Should opening the goals count at once (reading the saved files, no forced save), with
   **Count again** forcing a save for an up-to-the-second number? Or should nothing be
   counted until you tap?
8. Should finished goals (done and abandoned) be folded per player, as drawn, or dropped
   from the app after some time (say 30 days)?

## Rendered (`png/`, 390 × 844 at 2×, and 320 × 568 at 2×, dark and light)

For each variant `v` in `a`, `b`, `c`:

- `png/{v}-1-list-{theme}.png` — the main view: A's section, B's table with glance lines,
  C's Goals tab with BlockyFox's iron goal drawn;
- `png/b-1b-sheet-{theme}.png` — B only: BlockyFox's sheet;
- `png/{v}-2-goal-{theme}.png` — the iron goal opened (Sheet in A and C, in place in B);
- `png/{v}-3-finished-{theme}.png` — the done and abandoned goals unfolded;
- `png/{v}-4-offline-{theme}.png` — Mira_P: offline pockets and the not-loaded world;
- `png/{v}-5-empty-{theme}.png` — no goals;
- `png/{v}-6-list-320-{theme}.png` — the main view on a 320 × 568 phone;
- `png/{v}-7-goal-320-{theme}.png` — the goal opened on a 320 × 568 phone.

The render script audited every shot: no horizontal overflow, every control's hit area at
least 44 px (a row's `::after` overlay counts, as the shipped CSS does), and no console
errors or warnings. All passed. The canvas labels in C were checked by eye.
