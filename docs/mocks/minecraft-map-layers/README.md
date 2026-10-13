# GUI gate — saved places, villagers and pets, and beds on the Minecraft map

> **Status:** Living · **Last verified:** 2026-10-13
>
> **Decided for places and for villagers and pets (owner, 2026-10-13); built.** Beds are a
> separate build (see *Beds and respawn points*). The owner asked for these
> three map follow-ups on 2026-10-13; the independent review is `REVIEW.md`. Behaviour sources:
> `docs/plans/MINECRAFT_BEDROCK_PLAN.md`:
> - the saved places (`app.mc_places`, migration 0228's docstring) and §M24's goal chests;
> - §M4e, the entity index (*Not built (later): pets and villagers on the map*);
> - §M26, beacons and gates. DESIGN.md has it as "Beacons and gates on the Minecraft
>   map", and its marker language is kept here.

## Decided (owner, 2026-10-13)

1. **Saved places: variant A** — pins in the player's current colour, always named, merging
   into a count under 40 px, with an *In this world* list by player in Layers. The card says
   whose and when it was saved (`updated_at`), the coordinates, how far its player is, *Protected
   by …'s No-Spawn Beacon* (`covered_by`), *Wormhole gate N leads here*, and the goal chests
   near it with **the 48-block circle drawn** on tap, worded as this place's share (*9 of the
   23 iron blocks are in chests here*). **No "Point the Tricorder here" button.** A gate's
   destination is matched to a place **by position** (the review's X1); where they differ both
   are drawn.
2. **Villagers and pets: variant A** — a village is one tile with its count zoomed out (the
   located village keeps its amber tile and gains the count; wandering traders aren't
   counted), splitting into job discs from 3 px a block; pets and named mobs drawn; filter
   chips in Layers (*Sells Mending*, *Librarians*, a player's pets…) with C's ranked list
   under a chip. The villager card lists **every trade it offers now, by level**. The pet card
   says whose (or that the owner isn't known yet), its coat, and whether it's sitting. **No
   "Read the world again"**: the automatic re-read only, and the age is worded for what it
   is (the newest write to the world's files).
3. **Defaults:** saved places on; villagers off; pets and named mobs off.
4. **Owner-only:** none of these on share links, and none on the timeline's replay.
5. **Dave's chat card:** `villager` and `pet` focus kinds (`place` already existed).

The review's blocking findings X1, X2 and X5 are fixed in the build; DESIGN.md "Saved places,
villagers and pets on the Minecraft map" is the binding record. Questions 1-9 and 13 below are
answered by the above; the beds questions (10-12) are their own.

**Where the build departs from these mocks** (checked in headless Chromium at 390 × 844 and
320 × 568, dark and light, against `png/places-a-*` and `png/mobs-a-*`):
- The place pin is the map's shipped Lucide `map-pin` (no centre dot), as M26's destination
  pin already was; the card and list icons use the same glyph.
- The goal line names each open counted goal with any count here (*· 9 of the 23 iron blocks
  are in chests here*), *none here yet*, *counting…* or *can't count right now*, and a
  footnote says each chest counts toward its nearest place or the player (S2).
- A gate aimed where a re-saved place used to be adds *Wormhole gate 1 still leads to where
  Tree farm was, X … · Z …* with a `--warn-text` glyph (X1's case, not in the mock fixture).
- The age on a live world has no number: *as of the world's last save, re-read every minute*;
  a stopped world says *…, 6 min ago* (S3).
- The villager chips come from the world: *All jobs*, *Sells Mending* when anyone sells it,
  then the five commonest jobs. The pets chips are *Everyone's* and each owner the world
  knows (*BlockyFox's*), and an owner's chip lists their pets nearest them, as a villager
  chip lists the matches (C's list).
- Tapping a village tile zooms in until it splits; there's no village card.
- A pet whose owner is unknown is ringed neutral and its card says so (S6); the fixture adds
  one (a tabby cat).
- The village count badge moves to the first corner no gate line crosses (A1).
- At 320 × 568 with a place's card open the visible map is about 170 px, so the framed
  48-block circle is small.

The shipped map's Layers tab lists **Saved places** with a *later* badge. **Beds & spawn**
says *beds aren't recorded yet*. Villagers and pets aren't on the map at all. These mocks
show how each of the three arrives, on the full map as a pushed page. They are built on the
binding map and markers mocks:
- `../minecraft-map/a-full-bleed.html`, shipped as `frontend/src/screens/MinecraftMap.tsx`
  and `minecraftMapDraw.ts`;
- `../minecraft-map-markers/a-always-drawn.html`.

## Why three sections, not one combined set

Each section gets its own variants, and the owner picks each on its own. The three
questions are different in kind:
- **Places** are a few named spots per player, written on purpose. The question is how to
  name them without crowding.
- **Villagers** come by the dozen. The question is how to aggregate them.
- **Beds** are one point per player. The question is when to show them, and how they tie to
  the deaths already on the map.

A single variant language like *always drawn / on tap / listed* would force the same
answer on all three. In this world it is right for beds and wrong for villagers. What
the sections share is the M26 grammar, stated once below, so any three picks still read as
one map.

Open each `.html` in a browser:

- Each one is self-contained and works offline.
- The terrain is drawn procedurally on a canvas. Explored ground is in full colour and the
  satellite survey is dimmed. Terrain colours are raw RGB inside the picture.
- It is phone-framed and follows the system theme. The halo, ink and void stay dark in both
  themes, and only the label plates follow the theme (DESIGN "Map-only tokens").
- **Mock controls** (dashed) are reviewer chrome. States open by hash, for example
  `#view=place&theme=light`:
  - places: `view=map | place | farm | many | layers`;
  - villagers and pets: `view=map | villager | pet | filter | layers`;
  - beds: `view=map | player | bed | death | death2 | layers`.

**The fixture** is the markers mocks' world, Castle Hill:
- **Players:** BlockyFox (steel) and Mira_P (green) are online. Steve42 (amber) is offline,
  so he is a hollow disc.
- **From M26:** Mira's beacon and BlockyFox's two beacons (one a sky base), and gates 1 to
  3. Gate 1 leads to BlockyFox's **Tree farm**.
- **Already on the map:** the world spawn, a located Village, and three deaths.
- **Saved places:**
  - BlockyFox: **Home**, **Tree farm** and **Nether base**;
  - Mira_P: **Bakery** and **Melon farm**;
  - Steve42: **Mine outpost**, down at Y −12, and **Lookout**, up at Y 140.

  *A busy base* adds eleven more places round BlockyFox's and Mira's homes.
- **Villagers and pets:**
  - a village of 23 villagers and a group of 8 elsewhere;
  - five pets: Rex, Whiskers, Kiwi, Biscuit and Luna;
  - two named mobs, Clover the cow and Hopper the rabbit.
- **Respawn points:**
  - BlockyFox has a bed by Home.
  - Mira_P set a respawn anchor in the Nether.
  - Steve42's bed was broken, so he wakes at the world spawn.

## The same in every variant

**The M26 grammar, extended.**
- Things built or found *in the world* stay **square halo tiles**: structures, beacons,
  gates, and now beds, pets and named mobs. Each tile has a 1.5 px ring and a `--map-ink`
  Lucide glyph.
- A *place someone named* is a **round halo pin**: the disc and pin that M26 already draws
  where a gate leads.
- Every marker has a 46 px hit circle.
- **Ring colour says whose.** A player's thing is ringed in their colour (`--steel`,
  `--green`, `--amber`, `--violet`), the same colour as their disc, chips and trail.
  Something nobody owns takes a neutral ring, `--map-ink` at 55 %: a villager, a named mob,
  or a village with no located structure.
- No new colour tokens.

**Draw order:**
1. beacon squares, gate lines, and this round's lines: the goal-chest circle round a tapped
   place, and the respawn curve;
2. tiles: structures, beacons and gates;
3. spawn and deaths;
4. this round's markers;
5. the selection ring;
6. players on top.

**Labels** keep the map's rule: right, else left, else below, else above, else **dropped**,
never drawn under or over a marker, a line or a control. The order is the tapped thing,
players, this round's names, spawn, structures, then where a gate leads.

**The camera.** It frames the tapped thing in the middle of the visible map, clear of the
floating controls. The mocks enforce this with a dry run of the markers: small moves and
zooms round the framed spot, keeping the nearest one with nothing of this round under a
control (`mapScreen` in each file).

**Not drawn on the timeline's replay.** Places, villagers and beds are where they are now,
not where they were then. Places, villagers and pets are **owner-only**, like M26's markers.
The share link is question 4.

## 1 · Saved places

Places live in `app.mc_places`: per player and world folder (each place carries its
dimension), at most 100 a player a world. A player saves one through Dave where they stand (*"save this place as tree farm"*).
The Tricorder points at them (`mc_set_waypoint place=`), a gate opens to them, and goals
count chests within 48 blocks of them (§M24).

**The same in all three:**
- **The Saved places row** replaces the *later* badge. It is on by default.
- **Tapping a place** gives:
  - the pin on its player's tint, the name, and *BlockyFox's saved place · saved Oct 11 · in
    the Overworld*;
  - X · Y · Z with **Copy X Z**;
  - **Protected**, by their No-Spawn Beacon at X 90 · Z −10 (`--ok-text`), or *Not protected
    · no No-Spawn Beacon covers it*. This is M26's chunk check (`covered_by`).
  - *BlockyFox is 28 blocks west of it · online*;
  - *Wormhole gate 1 leads here, from X 30 · Z 14* (violet), when a gate is aimed at it;
  - ***Goals count chests within 48 blocks*** *(the circle) · 9 of 23 iron blocks toward Get
    23 iron blocks*. That is §M24's count. The **48-block circle** is drawn round the
    tapped place: a 1.5 px hairline in the player's colour over a halo, with a 10 % wash.
    It is round because M24's reach is measured across, and so it never reads as a
    beacon's dashed square or the scan's viewfinder corners.
  - **Point BlockyFox's Tricorder here**. This is an owner action that changes a player's
    game, so it is **question 2**, not a given.
- **A gate's destination is the saved place.** With the layer on, the violet pin M26 draws
  at the end of gate 1's line is replaced by Tree farm's own pin, and the line ends there.
  With the layer off, the violet pin comes back.
- **Pins merge with pins only**, never with tiles. A base's beacon tile stays visible beside
  the place it protects.
- **Nether places** are drawn on the Nether map and listed *in the Nether*.

| Variant | File | Thesis |
|---|---|---|
| **A** | `places-a-colour-pins.html` | *Pins in the player's colour, always named.* Every pin is ringed in its player's colour and labelled with its name. Pins closer than 40 px merge into an ink-ringed count. The Layers tab gains an **In this world** list of every place, by player, each row saying *protected* or *gate 1 leads here*. |
| **B** | `places-b-one-pin.html` | *One quiet pin; whose by a dot.* Every pin has the same neutral ring, with a small dot in its player's colour. Names show from 2 px a block, or on a tap. Only one player's pins merge, into a badge ringed in their colour. There is no list. |
| **C** | `places-c-whose-pill.html` | *Pick whose, then named.* Pins are drawn as A's, but unnamed. A **whose places** pill on the map (*Everyone's places ▾*) picks one player: only their pins show, all named. The same chips sit under the Layers row, and the list is A's. |

- **A — for:**
  - It answers *where's Mira's bakery?* with no tap.
  - A place's colour is its player's colour everywhere else on the map.
  - The merge rule handles a busy base (`png/places-a-colour-pins-4-many-*`).
  - Names are what places are for.

  **Against:**
  - The busiest overview.
  - Green, amber and violet players' pins share their ring colours with beacons, structures
    and gates. The round shape against a square tile carries the difference, at 24 px.
- **B — for:**
  - The calmest overview.
  - Its neutral pin never collides in meaning with an accent.
  - Per-player badges say *4 of BlockyFox's places here*.

  **Against:**
  - You can't read a place's name without zooming or tapping, and the overview is where the
    owner looks first.
  - The 8 px dot is small for telling whose.
  - It has no list, so a place that is off screen or underground is hard to find.
- **C — for:**
  - It scales to a player with a hundred places.
  - *Show me Steve's places* is one tap.
  - Named pins only appear for the player you're looking at.

  **Against:**
  - It adds a floating control on top of the map's own.
  - *Everyone* shows unnamed pins, which are A's clutter without A's names.
  - The pill duplicates the Layers chips.

**Recommendation: A.** Players save a handful of places each, and the name is the
information. A keeps the M26 promise of answering with no tap. Its list finds the
underground mine and the sky lookout. If a world ever gets crowded, C's chips can be added
under the row later without changing the pins.

## 2 · Villagers and pets

The §M4e entity index reads villagers, pets and named mobs from the **saved world**. It
gives each villager's job, level, whether its job is locked in, and its trades now. Pets
come with kind, name, owner, sitting and coat. So the data is *as of N minutes ago*, never
live. In the app the owner sees everything: Dave's in-game rules (own pets first, the
48-block free radius, deep-scan cost) are the players', not the owner's map's.

**The same in all three:**
- **The age is always said.**
  - The row reads *as of 6 min ago*, and so does the legend line.
  - A villager card says *As of 6 min ago, from the saved world*.
  - A pet card says *Where Rex was when the world last saved … one free to roam may have
    wandered since*.
  - While the world is live, **Read the world again** asks the box to save and re-read
    (§M23's `flush`, at most every 30 s). That is question 8.
- **The markers.**
  - A **villager** is a neutral disc with its job's glyph. Thirteen jobs share seven
    glyphs:
    - book: librarian;
    - flask: cleric;
    - wheat: farmer;
    - fish: fisherman;
    - hammer: the three smiths;
    - map: cartographer;
    - person: everyone else, unemployed and nitwit included.
  - A **pet** is a paw tile ringed in its owner's colour. A **named mob** is a neutral tag
    tile. Both are labelled with their names.
- **A villager card** gives:
  - the job glyph, ***Librarian · Expert***, a *job locked in* pill (it has been traded
    with), *in the village of 23*;
  - X · Y · Z, and its distance from the nearest online player;
  - **its trades now**, grouped by level (Novice to Expert):
    - what it sells, *for* what;
    - *2 of 12 used*, or *used up · restocks* in `--warn-text`;
    - a trade that matched the filter is tinted.
  - A footnote says that Master trades are told when it gets there. §M4e decision 2: locked
    trades are dropped at parse and never shown.
- **A pet card** gives the paw on its owner's tint, ***Rex***, *BlockyFox's dog · pale ·
  sitting*, X · Y · Z, *48 blocks south-east of BlockyFox · online*, and the age note.
- **Default off** (both rows). These are many, they move, and they're as old as the last
  save. They're turned on to look, and Dave's card for *"who sells Mending?"* would open
  with them on.

| Variant | File | Thesis |
|---|---|---|
| **A** | `mobs-a-village-tiles.html` | *A village is one tile.* Zoomed out, a village's villagers fold into one tile with a count on its corner: *Village · 23 villagers*. A village the server's `locate` already found keeps its amber structure tile and gains the count, rather than a second tile beside it. A group with no located village gets a neutral house tile (*8 villagers*). From 3 px a block they split into job discs, merged under 22 px. Pets and named mobs are always drawn. **Filter chips** live under the rows: *All jobs · Librarians · Sells Mending · Clerics …* and *Everyone's · BlockyFox's …*. A filter retitles the tile, as in *Village · 4 librarians*. |
| **B** | `mobs-b-job-glyphs.html` | *Every villager by its job.* Each villager is its own job disc at every zoom, merged by the map's 40 px rule into ink count badges. The located village's tile merges into the count too, as any point marker does. One row of **chips floats on the map** under the dimension switch: *All · Librarians · Sells Mending · Pets · Named · Clerics*. A chip shows only that kind. |
| **C** | `mobs-c-find-first.html` | *Nothing until you ask.* The layer draws nothing by itself. The panel holds a **find** row of chips: *Librarians · Sells Mending · Clerics · Pets · Named · More…*. Its matches are drawn as **numbered** discs, nearest to BlockyFox first, labelled (*1 · Mending · Expert*), and listed in the panel with distance and price. The map frames the matches. This is what Dave's `mc_find_villager` / `mc_find_pet` already answer, in map form. |

- **A — for:**
  - A village of dozens is one readable thing at the zoom you see the world at.
  - It reuses the village tile the map already draws.
  - The count is honest without drawing 23 discs.
  - It zooms into the real layout.
  - Pets, the things people name and look for, stay visible.

  **Against:**
  - Grouping is our own judgement: a village is the villagers within 48 blocks of each
    other, or BDS's own village records (see *Data path*).
  - A lone villager far from a village gets a tile of its own.
- **B — for:**
  - What you see is where each villager is, at every zoom.
  - The floating chips are one tap from the map.

  **Against:**
  - At the overview the village becomes a ring of 3, 4 and 16 badges sitting on the gate
    line (`png/mobs-b-job-glyphs-1-map-*`).
  - The job glyphs only matter zoomed in.
  - It adds a row of floating chrome that crowds a 320 px phone.
- **C — for:**
  - The calmest map.
  - It answers the real questions, *who sells Mending* and *where's my dog*, ranked by
    distance.
  - It matches Dave's tools.

  **Against:**
  - *What's in this village?* needs a query.
  - With nothing asked, the layer looks broken.
  - It duplicates the chat's job.

**Recommendation: A.** The owner's map is for *looking*, and the village is the unit people
think in. A keeps the overview calm, uses the tile that's already there, and splits into B's
job discs when you zoom in, so it gets B's detail where it's readable. C's ranked answer is
what Dave's chat already gives. A's *Sells Mending* chip retitles the tile, which covers the
look-up case on the map.

## 3 · Beds and respawn points

Each player has **one** respawn point: their bed, or a charged respawn anchor in the Nether.
Without one they wake at the world spawn.

**The same in both:**
- **A bed** is a square tile ringed in its player's colour, with the Lucide `bed` glyph. A
  **respawn anchor** uses Lucide `anchor` and is drawn on the Nether map. In the Overworld
  it is listed *in the Nether*.
- **The respawn curve.** Tapping a death draws a curved line from the skull to where its
  player woke: their bed, their anchor (no line; it's in the Nether), or the star. It is
  2 px in the player's colour over a halo, with an arrowhead. It is **curved** because it
  is a jump, not a path, so it never reads as a trail (solid, straight segments), the
  Tricorder bearing (8 / 6 dashed) or a gate line (dotted).
- **The death card gains one line:**
  - *Respawned at their bed · 164 blocks north-east · X 70 · Z 40 (the curve)*, with **Show
    the bed**;
  - *Respawned at their respawn anchor · in the Nether*;
  - or, in `--warn-text`, *Respawned at the world spawn · their bed was missing or blocked,
    so it was cleared*.
- **The player card gains one line:** *Bed · 32 blocks south-east · X 70 · Z 40*, *Respawn
  anchor · in the Nether*, or *No bed set · respawns at the world spawn*.
- **A bed card** gives:
  - the bed on the player's tint, ***BlockyFox's bed***, a *respawn point* pill, *set Oct 11,
    9:02 pm*;
  - X · Y · Z, and *BlockyFox is 32 blocks north-west of it · online*;
  - *Respawned here after 1 death since it was set · burned in lava, yesterday 4:48 pm (the
    curve)*;
  - *As the pack last sent it*.
- **The Beds & spawn row** loses *beds aren't recorded yet*. It stays on by default. **In
  this world** lists every respawn point, including *Steve42 · no bed set · respawns at the
  world spawn*.

| Variant | File | Thesis |
|---|---|---|
| **A** | `beds-a-always-drawn.html` | *Every respawn point, always.* Each player's bed is on the map whenever the row is on, labelled *BlockyFox's bed*. Tapping a death or a bed draws the curve. |
| **B** | `beds-b-with-the-player.html` | *A bed shows with its player.* Beds stay off the overview. Tapping a player draws their bed, labelled *Bed*, and adds **Show the bed** to their card. Tapping a death draws the bed and the curve. |

- **A — for:**
  - One tile per player, so a handful at most.
  - It answers *where does everyone respawn?* and *is Steve's bed gone?* with no tap.
  - It matches the always-drawn spawn star beside it.

  **Against:**
  - A bed sits right by its player's base, the most crowded part of the map.
- **B — for:**
  - The calmest base.
  - A bed is drawn exactly when you're thinking about that player.

  **Against:**
  - *Who has a bed?* needs the list or a tap per player.
  - The overview stops matching the Layers row's promise.

**Recommendation: A.** The count is tiny, the information is safety-relevant (*will they wake
up somewhere safe?*), and the curve on a tapped death is the payoff the owner asked for. It
works the same in both variants.

## Which layers default on

| Layer | Default | Why |
|---|---|---|
| Saved places | **on** | A few per player, named on purpose. Owners look for them. |
| Beds & spawn | **on** (as now) | One point per player plus the star. |
| Villagers | **off** | Dozens, as old as the last save. Turned on to look. |
| Pets & named mobs | **off** | They move, and they're as of the last save. Dave's card turns them on for its own answer. |

The existing defaults are unchanged: Players, Trails, Deaths, Structures, Beacons and Gates
on; Explored by and Grid off.

## Rendered (`png/`, 390 × 844 and 320 × 568 at 2×, dark and light)

For each file prefix `p` (the eight files' names without `.html`):

- **Places:**
  - `png/{p}-1-map-{theme}.png` — the overview;
  - `png/{p}-2-place-{theme}.png` — Home tapped: protected, the goal circle;
  - `png/{p}-3-farm-{theme}.png` — Tree farm tapped: not protected, gate 1 leads here;
  - `png/{p}-4-many-{theme}.png` — the busy base, where merging shows;
  - `png/{p}-5-layers-{theme}.png` — the Layers tab, opened on the new row.
- **Villagers and pets:**
  - `1-map`;
  - `2-villager`, the librarian's card and trades;
  - `3-pet`, Rex;
  - `4-filter`: *Librarians* (A, B), or *Pets* found (C);
  - `5-layers`.
- **Beds:**
  - `1-map`;
  - `2-player`, BlockyFox tapped;
  - `3-bed`;
  - `4-death`, the lava death and the curve to the bed;
  - `5-death2`, Steve42's death and the curve to the star;
  - `6-layers`.
- **320 × 568:** `png/{p}-320-map-{theme}.png`, plus the tapped state:
  `png/{p}-320-place-*`, `-320-villager-*` or `-320-death-*`.

**The main view per variant:**
- `png/places-a-colour-pins-1-map-dark.png`
- `png/places-b-one-pin-1-map-dark.png`
- `png/places-c-whose-pill-1-map-dark.png`
- `png/mobs-a-village-tiles-1-map-dark.png`
- `png/mobs-b-job-glyphs-1-map-dark.png`
- `png/mobs-c-find-first-1-map-dark.png`
- `png/beds-a-always-drawn-4-death-dark.png`
- `png/beds-b-with-the-player-4-death-dark.png`

**The audit.** Playwright with Chromium audited every one of the 116 shots, and all passed:
- no horizontal overflow;
- every DOM control's hit area at least 44 px;
- no console errors or warnings;
- none of this round's markers, and no tapped thing, under a floating control. The camera's
  dry run also steers the M26 tiles clear where it can.

The canvas labels were checked by eye against the drop rule. The PNGs are palette PNGs that
keep every frequent exact colour, as in the markers set.

## Data path

### Places: the data exists; one read route to build

**What exists.**
- **`app.mc_places`** (migration 0228, owner-only RLS with its isolation test) holds `xuid,
  gamertag, world` (the folder), `name` (1–40), `x, y?, z, dim, created_at, updated_at`.
  There are at most 100 per player per world (`PLACES_MAX`). Dave's `mc_place_save` writes
  it where the player stands, and `mc_place_remove` deletes.
- **What's already computed from it:**
  - M26's covered check: `covered_by` in `backend/src/jbrain/agent/minecraftmarkers.py`,
    chunk within one chunk of a beacon's;
  - §M24's chest counts: `goals.count_progress`, `NEAR_BLOCKS = 48`, measured across;
  - gate targets: `app.mc_world_gates.target_place`.

**To build.**
1. `GET /api/minecraft/map/places?slot=&dim=` → `{places: [{id, gamertag, xuid, name, x, y,
   z, dim, saved_at, covered: {x, z, placed_by} | null, gates: [id…]}], as_of}`.
   - One `SELECT` on `mc_places` for the slot's world folder, under the owner session.
   - `covered` comes from the beacon rows by the same pure function.
   - `gates` comes from the gate rows' `target_place` with the same planter.
   - No migration.
2. **The goal line on tap** reads the existing `GET /api/minecraft/goals/progress`
   (per player, counted near each place), loaded only when a place is tapped, because it
   reads the world's files.
3. **Map:** a draw function for pins, the merge rule, the circle and the list rows. The row
   loses *later*.
4. **If question 2 is yes:** `POST /api/minecraft/players/{gamertag}/waypoint {place}`, the
   code path of `mc_set_waypoint place=` (a `jb:waypoint` scriptevent). It only works
   while the world is live, and the button says so otherwise. The tool's `targets_for` book
   records it, so the map's §M21 target marker updates.

### Villagers and pets: the index exists; it can't list a map yet

**What exists.**
- `deploy/minecraft/entities.py`, the `EntityIndex`, reads the saved world in one shared
  sweep with the chest index. The 60 s re-read runs when the files change, and `flush=1`
  runs the save-hold dance first.
- **Per villager:** job, level 0–4, locked-in flag, and at most 32 trades with tier, buy,
  sell, enchantments and uses. Trades above its level are dropped at parse.
- **Per pet or named mob:** kind, name, owner id, sitting, baby and coat words. The
  dimension comes from `digp`.
- **Owners:** owner gamertags come from `player-ids.json`, learned when a player asks Dave
  anything (`owner_known: false` until then).
- **Sidecar routes:** `/map/villagers` and `/map/pets`, with debug passthroughs. They are
  **finders**: they *require* `x` and `z` and return the nearest `limit ≤ 20`
  (`deploy/minecraft/server.py`). There is no app (`/api/minecraft/…`) route.

**To build.**
1. **A sidecar listing:** `GET /map/entities?slot&dim[&x0&z0&x1&z1]`.
   - It returns every indexed villager, pet and named mob in the dimension, or in a box,
     compactly: `{villagers: [{id, x, y, z, job, level, locked, trades: n}], pets:
     [{id, kind, name, owner, owner_name, sitting, x, y, z}], named: […], as_of, age_s,
     counts}`.
   - Trades stay out of the list: a card asks `/map/villagers?…&id=` (a new `id`
     parameter), or the listing carries them under a cap.
   - The index's caps (20,000 villagers) mean the app must ask for a box at low zoom, or
     the sidecar returns per-village aggregates.
2. **Village grouping (A).**
   - **Recommended first:** single-link clustering at 48 blocks, done in the sidecar so the
     listing can return `villages: [{x, z, count, jobs: {librarian: 4, …}}]`.
   - **Later:** BDS's own `VILLAGE_<dim>_<uuid>_INFO` / `_DWELLERS` records give the game's
     own village bounds and members. The dev world's keys show them, but their format is
     unread here, so this needs a box check.
   - A located `village` structure within 48 blocks of a group lends it its amber tile.
3. **App routes:** `GET /api/minecraft/map/entities` passes through, owner-only like
   `/map/structures`, and works with the server stopped (the index reads the files).
   **Read the world again** passes `flush=1`, only while the world is live.
4. **Map:** two rows (A, B) or one (C), the draw functions, the cards and the chips. No new
   table: the index is in memory, as §M4e's is.

### Beds and respawn points: not recorded anywhere; a pack event to build

**What exists.**
- **The pack already reads the respawn point.** `bedOf()` in
  `deploy/minecraft/jbrain-pack/scripts/main.js` calls `player.getSpawnPoint()`. That is
  stable `@minecraft/server` 2.0.0, the module the manifest pins. It returns a
  `DimensionLocation`, or `undefined` with no bed or anchor set.
- **But only when the player asks Dave.** The `dave` event carries it as `spawn`, and
  `bds.py` validates it. The backend keeps it on the in-memory `Asker` (`door.py`: *Their
  bed is at …*) and never stores it.
- **Where each death's player woke is already recorded.** The pack emits `{"ev":
  "respawn", name, x, y, z, dim}` on every non-initial `playerSpawn`. The drain files it in
  `app.mc_player_events` (`sessions.py` `TIMELINE_KINDS`), and the map already uses it for
  *…died and respawned in the Overworld*.
- **The saved player record** (`player_server_<uuid>` in the world's LevelDB) is a Bedrock
  player compound. Bedrock's format puts the respawn point there as `SpawnX/SpawnY/SpawnZ`,
  `SpawnDimension` and `SpawnBlockPositionX/Y/Z`.
  - This is **not verified here**: none of the scratch test worlds has a player record (no
    client ever joined them).
  - §M4e found those records **carry no gamertag or xuid**, so the files alone can't say
    *whose* bed.
  - Like §M26's beacons, the pack is the path, and the file read at most a resync
    cross-check.
- Stable 2.0.0 has **no spawn-point-changed event**. The pack already subscribes to
  `world.afterEvents.playerInteractWithBlock` (`wormhole.js`), which fires on a bed or
  anchor tap.

**To build (the M26 pattern: structured pack lines, the death pipe).**
1. **Pack.** Emit `{"ev": "spawnpoint", "name", "x", "y", "z", "dim", "kind"}` with `kind` set
   to `bed`, `anchor` or `other`. It is read from the block at the point.
   `{"ev": "spawnpoint", "name", "clear": true}` is sent when it's gone. Send it:
   - a tick after `playerInteractWithBlock` on any `*_bed` or `respawn_anchor`;
   - from a **10 s poll** of `getSpawnPoint()` per online player, sent only when it changed.
     That catches `/spawnpoint`, an anchor run dry and a bed broken by someone else;
   - on join (`initialSpawn`), as the resync.
2. **Sidecar.** Add `spawnpoint` to `PACK_EVENTS`, validated like deaths: ints clamped, dim
   from the enum, kind from its list. The xuid comes from BDS's connect line.
3. **Backend.**
   - **Recommended:** a new kind `spawnpoint` in `app.mc_player_events`, which keeps the
     history. *Set Oct 11* and *Respawned here after 1 death since it was set* fall out of
     it, as does the death card's line: the death's next `respawn` event matched against
     the spawn point then (within 2 blocks: their bed; at the world spawn while one was set:
     *missing or blocked*). No new table, so no new RLS test. The current point per player
     is the latest row.
   - **Alternative:** a small `app.mc_spawn_points` table (owner RLS and its isolation
     test). It is simpler to read but loses the history.
   - `world_replaced` clears either one, as it clears the travel log.
4. **Route:** add `spawns: [{gamertag, kind, x, y, z, dim, set_at}]` to the existing `GET
   /api/minecraft/map/markers`, so beds arrive with beacons and gates. The death card's
   line comes from the timeline the map already loads.
5. **Box checks:**
   - that `getSpawnPoint()` is the bed's block, so `kind` can read it;
   - that breaking a bed shows on the poll before the next death;
   - what an anchor at zero charges reports.

## Open questions for the owner (each answerable on its own)

1. **Saved places: which marker?** A (pins in the player's colour, always named, with a
   list), B (one quiet pin with a whose-dot, names zoomed in) or C (pick whose, then named).
2. **"Point BlockyFox's Tricorder here" on a place's card** changes a player's game from
   the app. Should it be a button (as drawn; only while the world is live), **Ask Dave…**
   (opens the chat with the request typed, so Dave does it and says so), or left out (the
   card stays look-only)?
3. **The 48-block goal-chest circle** round a tapped place: keep it (as drawn), or say it in
   words only?
4. **Places, villagers, pets and beds on a share link** (`/mc/s/…`): owner-only like
   beacons and gates (recommended), or shown to whoever has the link?
5. **Villagers and pets: which design?** A (a village is one tile, with its count; filter
   chips in Layers), B (every villager by its job, chips on the map) or C (nothing until you
   ask; numbered matches).
6. **Villagers and pets default off?** Recommended, because they're as old as the last
   save. Or on?
7. **A villager card lists every trade it offers now**, grouped by level (as drawn). Or only
   the notable ones: enchanted books, and whatever the filter matched?
8. **Read the world again** on the layer row (forces a save, at most every 30 s, live world
   only): offer it, or rely on the automatic re-read?
9. **Village grouping:** our own 48-block grouping now (recommended), or wait for BDS's own
   village records (needs a box check of their format)?
10. **Beds: which design?** A (every respawn point, always) or B (a bed shows with its
    player).
11. **The beds data path:** approve the pack change (a `spawnpoint` event on bed or anchor
    use, a 10 s change poll, and a resend on join) and keeping it in the travel log as a
    new event kind (recommended), or a separate current-only table?
12. **The death card's inference** *their bed was missing or blocked* (from where they
    woke): say it like that, or only state where they woke?
13. **Dave's chat card** (`minecraft_map`): add the focus kinds `place`, `villager`, `pet`
    and `bed` in the same build, or later?
