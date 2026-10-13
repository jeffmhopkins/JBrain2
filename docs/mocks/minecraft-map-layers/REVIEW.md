# Independent review: saved places, villagers and pets, and beds on the Minecraft map

> **Status:** Living · **Last verified:** 2026-10-13
>
> An independent review of the eight variant pages in `docs/mocks/minecraft-map-layers/`:
> `places-{a-colour-pins,b-one-pin,c-whose-pill}.html`,
> `mobs-{a-village-tiles,b-job-glyphs,c-find-first}.html` and
> `beds-{a-always-drawn,b-with-the-player}.html` (branch `docs/mc-map-layers-mocks`, commit
> `ce620898`). The reviewer did not build them and had not seen the designer's reasoning
> (PROCESS.md, "GUI gate"). The README was read for the brief, the fixture and the states, and
> its claims about the data path were checked against the code. Every design judgement is of the
> rendered pages and PNGs, measured against `docs/reference/DESIGN.md` ("Map-only tokens",
> "Accessibility", "The Minecraft map", "Beacons and gates on the Minecraft map", "The Tricorder's
> live scan on the Minecraft map", "The Minecraft map share") and the shipped map code. The format
> follows `../minecraft-map-markers/REVIEW.md` and `../minecraft-scan-on-map/REVIEW.md`.

## How it was checked

- **The PNGs were viewed**: every section's overview, a tapped state per variant, the busy base,
  the Layers tabs, and the 320 × 568 shots, dark and light. The README's count holds (116 files).
- **Every page was rendered again** in headless Chromium at 390 × 844 and 320 × 568, dark and
  light, for every hash state (5 or 6 views a file), **172 renders**. Each was audited in the DOM
  for horizontal overflow, hit areas under 44 px, text under 4.5:1 against its painted background,
  and console errors. **Clean**: no page overflow, every DOM control is at least 44 px (the chips
  through their `::after` hit extension), no text contrast failure, no console errors. The only
  flag is the card head's 10 px overhang from the close button's negative right margin, which is
  the shipped and sibling markup, not a defect. The canvas can't be audited from the DOM; it was
  judged by eye.
- **Tokens:** the eight files differ only in their title and `SEC`/`V` line, so these hold for all
  eight. Three literals sit outside the token blocks: `NEUTRAL = "rgba(242,242,242,.55)"` (that's
  `--map-ink` at 55 %, so derive it), the legend's `rgba(127,167,201,.18)` (`--steel` at 18 %), and
  `.det-ic.rose{background:rgba(207,138,143,.13);color:var(--rose)}`. The last is the one that
  matters: the shipped `.mc-map-det-ic.death` is `var(--rose-tint)` with `--danger-text`. Plain
  `--rose` on the light card is about 2.4:1 for the skull glyph, under the 3:1 a meaningful icon
  needs (visible in `beds-*-5-death2-light`). Use the shipped tokens (S9).
- **Code read for the README's claims:** `backend/migrations/versions/0228_mc_places.py`,
  `0230_mc_share_links.py`, `0240_mc_world_markers.py`; `backend/src/jbrain/agent/minecrafttools.py`
  (`mc_place_save`, `mc_places`, `_places`, `resolve_player`, `PLACES_MAX`, `NEAR_BLOCKS`,
  `stock_near`, `loaded_world`), `minecraftmarkers.py` (`covered_by`), `minecraftmap.py`
  (`FOCUS_KINDS`); `backend/src/jbrain/minecraft/goals.py` (`count_progress`), `markers.py`,
  `sessions.py` (`TIMELINE_KINDS`, `_timeline`), `timeline.py`; `backend/src/jbrain/api/minecraft.py`
  (`/goals/progress`, `/map/markers`, `players_view`); `deploy/minecraft/entities.py`,
  `containers.py` (`stock`), `server.py` (`/map/villagers`, `/map/pets`, `_fresh`, `_freshness`,
  `_flush_for_search`), `bds.py` (`PACK_EVENTS`); the pack's `manifest.json`, `scripts/main.js`
  (`bedOf`, `playerSpawn`, the `jb:waypoint` handler) and `scripts/wormhole.js`; the frontend's
  `MinecraftMap.tsx`, `minecraftMapDraw.ts` (`placePin`, `drawWorldMarkers`, `clusterBadge`),
  `minecraftMap.ts` (`clusterMarkers`) and `minecraftMapMarkers.ts`.
- **`@minecraft/server` 2.0.0** was checked against the published package's own `index.d.ts`
  (fetched to scratch, not installed): `Player.getSpawnPoint(): DimensionLocation | undefined` and
  `setSpawnPoint` exist; `WorldAfterEvents` has `playerSpawn`, `playerInteractWithBlock`,
  `playerBreakBlock` and `playerDimensionChange`, and **no spawn-point-changed event**. The README is
  right.
- **Lore:** `bleepbloop_city/notes/lore.md` (at `2fce94c`): saved places and transport beacons,
  *"Villagers and pets"* (signed off 2026-10-13), the No-Spawn Beacon, the crash site's bed. The
  mocks carry no Dave-voice lines. The app's words are consistent with canon (S8 has one fixture
  gap).

## Is the README true of the code?

Mostly. These are the places where it isn't, or says less than the build needs.

**Places**
- **Keying.** Places are keyed **per player (xuid, or `name:<gamertag>` until the server has seen
  them) and per world folder**. The dimension is a column, not part of the key: the name is unique
  per player and world across all three dimensions, and `PLACES_MAX = 100` is per player per world.
  The section intro said *per world folder and dimension, at most 100 each*; the Data path said it
  right. Fixed in the README (below).
- **"Saved Oct 11."** Saving a name again **moves** the place (`UPDATE … updated_at = now()`). The
  route's `saved_at` should be `updated_at`, and the card should say *saved* or *moved* honestly.
- **`gamertag` in the row is as of the last save.** The listing should colour and name places by
  the player's current gamertag (the sessions' latest), not the row's, or a renamed player's pins
  take a stranger's colour.
- **`covered_by`** is true as stated: a pure function in `agent/minecraftmarkers.py` over
  `markers.covers` (the place's chunk within one chunk of the beacon's, same dimension). It returns
  a list, and the beacon's `placed_by` can be empty for one placed before the pack kept placers;
  the card needs *a No-Spawn Beacon* for that case (the tool already words it so).
- **A gate's destination is a snapshot, not a link.** `app.mc_world_gates` keeps `target_place`
  **and its own `target_x/y/z`** from when the gate was aimed. Re-saving a place moves it but never
  re-aims the gate (`mc_place_save` sends nothing to the pack; only `mc_place_remove` sends
  `forget`). So the README's rule *the violet pin is replaced by Tree farm's own pin, and the line
  ends there*, matched by name and planter, would draw the line to where the place is now while the
  gate still sends you to where it was. Blocking X1.
- **`/goals/progress`** exists, takes one `xuid`, and counts **every** open counted goal of that
  player in every world, with a per-place breakdown (`chests[].place`, `count`). So the place card's
  number is available. But:
  - each container counts once **toward its nearest centre**, and the centres are every saved place
    *and where the player stands* (`containers.stock`). A chest inside the drawn 48-block circle can
    be counted toward another place or toward the player. The circle is *the reach*, not *the
    chests that count here*;
  - *9 of 23 iron blocks toward Get 23 iron blocks* reads as the goal's total. The 9 is only this
    place's share; the goal's total adds pockets, other places and *here*. Reword (S2);
  - for an online player in the loaded world, the call also asks the game for their pocket
    (`carry_pocket`), and it reads every dimension's containers for every goal. Loading it on tap,
    as proposed, is right; caching it for the open card is needed.
- **The Tricorder button.** `POST …/waypoint {place}` on the `mc_set_waypoint` path is buildable.
  Two facts the owner's question 2 should carry: the pack's `jb:waypoint` handler only logs
  (`say(...)` to the console). **The player is told nothing in game; their arrow just changes.** And
  a new waypoint **ends a live scan** (§M25). The *Ask Dave…* option changes neither fact: Dave's
  confirmation would be in the owner's chat, not in game.
- **Owner-only holds today:** `mc_places` is owner-RLS only (`mc_places_owner`); the share principal
  has no grant. Showing places on a share link would need a new `_share` policy and its isolation
  test.

**Villagers and pets**
- **True as stated:** one shared sweep with the chest index (`refresh_together`), re-read at most
  every 60 s when the files change (`REREAD_S`), `flush=1` at most every 30 s and only for the
  loaded, running world (`FLUSH_EVERY_S`); per villager job, `TradeTier` 0–4, *locked in*
  (`TradeExperience > 0`), at most 32 trades, those above its tier dropped and counted; per pet kind,
  name, owner id, sitting, baby, coat words; dimension from `digp`; owner names from
  `player-ids.json`, learned on a `/dave`; `/map/villagers` and `/map/pets` require `x` and `z`
  and cap `limit` at 20; debug passthroughs exist; no app route. The caps (20,000 / 5,000 / 5,000)
  are right.
- **The age the sidecar reports is not the age of the entities.** `_fresh`/`_freshness` is the
  newest mtime of **any** file in the world's `db/`. On a live world the log is written every few
  seconds, so the route will say *as of a few seconds ago* while a ticking chunk's pet may not have
  been written for much longer. The fixture's *as of 6 min ago* on a **Live** world can't occur as
  specified, and if it did it would understate the age. Either word it without a number on a live
  world (*from the saved world · re-read every minute*), or treat the age as honest only after a
  flush. A box check of how often BDS writes actors in loaded chunks settles which (S3).
- **Villager records carry no id today.** `_villager_out` doesn't emit `uid`, though the `Mob` has
  it (and it can be 0 when absent). The README's new `id` parameter is right; it needs a fallback
  key.
- **Wandering traders are indexed as villagers** (`job = "wandering trader"`). The job-glyph table
  has no entry for them, and grouping them into a village is wrong: canon has them as visitors the
  beacon beams away. Exclude them from village counts and give them their own glyph or leave them
  out (S5).
- **Named mobs and pets overlap.** A named, tamed animal (Rex) is in both `pets` and `named`. The
  listing must de-duplicate, and a pet whose owner isn't in `player-ids.json` (they never asked
  Dave) has no colour. The card and ring for *owner unknown* aren't drawn (S6).
- **"Thirteen jobs share seven glyphs."** Bedrock has thirteen professions plus *unemployed* and
  *nitwit*. The list's *person: everyone else* covers shepherd, fletcher, butcher, leatherworker and
  mason as well, which is fine, but say so.

**Beds**
- **True as stated:** `bedOf()` calls `player.getSpawnPoint()` (stable 2.0.0, the manifest's pin),
  floors it, and is sent only on a `dave` event as `spawn`; `bds.py` validates it; the backend keeps
  it on the in-memory asker (`door.py`: *Their bed is at …*); a non-initial `playerSpawn` emits
  `respawn`, filed in `mc_player_events` (`TIMELINE_KINDS`); `wormhole.js` already subscribes to
  `playerInteractWithBlock`. `mc_player_events.kind` has no CHECK, so a new kind needs no migration.
- **"No new table, so no new RLS test" is wrong for this table.** `mc_player_events` is one of the
  three travel-log tables **migration 0230 opens to share links** (`mc_player_events_share`, every
  row of the link's world). A `spawnpoint` kind written there is readable by any share visitor at
  the database level; only the app's `kind IN (…)` filter in `timeline.py` would keep it out.
  CLAUDE.md #3 puts firewalls in Postgres. If beds stay owner-only (question 4), the recommended
  path needs either a migration narrowing the share policy to the four timeline kinds, with its
  isolation test, or the separate owner-only table. Blocking X3.
- **A broken bed probably isn't visible to the poll.** In Bedrock the spawn point stays set after
  the bed is broken; the game checks, and clears it, only at the next respawn. The README lists this
  as a box check, but the design leans on it: Beds A's *is Steve's bed gone?* with no tap, the bed
  tile drawn where the bed was, and *no bed set*. Until the box check says otherwise, the pack must
  read the block at the point (it can only when that chunk is loaded) and the map needs a *bed not
  seen there* state, or it will draw beds that aren't there (X4).
- **Respawns aren't *at* the bed or the star.** A player wakes beside the bed, and at the world
  spawn somewhere inside the `spawnradius`. The *within 2 blocks* match needs the box check plus the
  spawn radius for the star case, or the inference *missing or blocked* will misfire (question 12).

**Cross-cutting**
- **Question 13 is half done already.** `FOCUS_KINDS` in `agent/minecraftmap.py` already has
  **`place`** (with `saved_place`). Only `villager`, `pet` and `bed` are new.
- **Cross-references were off by one.** The README sent the share link to *question 5* (it is 4)
  and *Read the world again* to *question 9* (it is 8). Fixed (below).

## Cross-variant findings

### What holds in all eight

- **The grammar extension is right.** Square tile for a thing in the world (bed, pet, named mob),
  round pin for a place someone named, ring colour for whose, neutral ring for nobody's, no new
  tokens. It reads as one map with M26 in every overview.
- **The round goal circle** (hairline, 10 % wash) never reads as a beacon's dashed square or the
  scan's viewfinder corners, even round Mira's green beacons (`places-*-2-place`).
- **The curved respawn line** is a good mark. It can't be read as a trail, a bearing or a gate line,
  and the arrowhead says which end is the waking.
- **The age is always said** for villagers and pets, and the pet card's *a sitting pet stays put;
  one free to roam may have wandered since* is the honest line.
- **Values** use real minus signs and no-break spaces (*Z&nbsp;−10*), as M26 requires.

### Blocking (whichever variants are picked)

- **X1. Match a gate to a place by position, not by name.** Replace the gate's violet pin with the
  place's pin only when the gate's `target_x/z` (and dimension) equal the place's. Otherwise draw
  both: the gate's own violet pin where it actually leads, labelled as M26 does, and the place
  where it is now, with the place card saying *Gate 1 still leads to where Tree farm was*.
- **X2. This round's markers must join the map's clustering and label obstacles.** DESIGN: markers
  within 40 px merge with the other point markers. The mocks draw the base tiles (structures,
  beacons, gates, deaths) unclustered, and pets, named mobs and beds never cluster at all. The
  result is overlapping tiles: Kiwi's paw on a gate tile, Whiskers' on Mira's beacon tile, at the overview
  and at 320 (`mobs-a-village-tiles-320-map-*`, `mobs-b-job-glyphs-320-map-*`). The README's
  *pins merge with pins only, never with tiles* is a new rule, and it isn't stated for pets or
  beds. Decide it per kind and put it in DESIGN. The simplest rule that matches the shipped map:
  everything but players goes through the one `clusterMarkers` pass.
- **X3. Beds' storage and the share firewall** (above). Pick the storage with question 4's answer
  in hand, and in RLS.
- **X4. A bed the game still remembers but that's gone** needs a state, and the box checks need to
  run before Beds is built (above).
- **X5. Don't copy the mock's label obstacle keys.** In `label()`, a label skips every box with the
  same `own` key, and the keys are per kind (`"pet"`, `"pl"`, `"vv"`, `"nm"`, `"bed"`, and `"p"`
  for players). A pet's label therefore ignores every other pet tile. That's why **Biscuit's plate
  covers Luna's paw** at 320 (`mobs-a-*-320-map-*`, `mobs-b-*-320-map-*`). The shipped
  `placeLabels` keys by marker; the build must too. This is a mock defect, not a design choice,
  and it wasn't fixed here because every PNG would need re-rendering.

### Non-blocking

- **S1. Draw order changes M26's.** DESIGN draws gate-destination pins **under** the tiles
  (*areas, gate lines, place pins, beacons and gates*); the README puts this round's markers above
  tiles, spawn and deaths. Places above tiles is defensible (they are what you look for), but it's
  an amendment to a binding order and needs saying so in DESIGN.
- **S2. The goal line.** Say what the number is: *9 iron blocks in chests here · 14 / 23 toward Get
  23 iron blocks*, and add that each chest counts toward its nearest place (or the player). With
  several open counted goals, list each one that has any count here, not only the first.
- **S3. The live age** (above): no minutes on a live world, or flush first.
- **S4. The camera doesn't frame the goal circle.** *Tap Tree farm* puts the circle under the
  dimension switch (`places-a-colour-pins-3-farm-*`). The card refers to *(the circle)*, so frame
  it with M26's 72 px margin, or say *the circle* only when it fits.
- **S5. Wandering traders** (above).
- **S6. Pets whose owner is unknown** need a ring (neutral) and card words (*owner not known yet:
  they haven't asked Dave anything*).
- **S7. A label whose marker is under the panel is drawn.** *Mine outpost*'s plate sits above the
  panel's edge with its pin hidden under it (`places-a-colour-pins-3-farm-light`); Steve42's plate
  stays while his disc is off the left edge (`mobs-c-find-first-4-filter-dark`). The shipped rule
  is that the panel is an obstacle; a label for a marker out of view should go with it.
- **S8. Fixture gap: shared and pod beds.** The crash site gives every new player a bed at spawn
  (lore, *Crash site*), and Bedrock lets several players set their spawn at one bed. The likeliest
  real overview is two or three beds on one block beside the star. Beds A never shows that case.
  Draw it (one tile with a count, a card listing whose).
- **S9. Tokens** (above): the death icon's tint and colour, and the two `rgba` literals.
- **S10. The world-spawn warn line** colours only its icon `--warn-text`; the README says the line
  is in `--warn-text`. Pick one. The icon-only version matches how `.ln.ok` and `.ln.gate` work.
- **S11. Replay.** *Not drawn on the timeline's replay* is right for places, pets and beds. But the
  death card exists on the timeline. Say whether the respawn curve is drawn there (it can be: it
  comes from the travel log, not from the current bed).

## 1 · Saved places

### Variant A — pins in the player's colour, always named

**Verdict: pass with changes.**

The overview answers *where's Mira's bakery?* with no tap (`places-a-colour-pins-1-map-*`). The busy
base (`-4-many-`) merges cleanly into ink-ringed counts with the named outliers kept. The **In this
world** list is what finds Steve's underground mine and sky lookout, and it reuses M26's list rows.
The ring-colour clash the README admits (green places beside green beacons, amber places beside the
amber village tile) is real but carried by the shape at this size: round pin against square tile.

- **Must change:** X1, X2, X5 and S2. The pins should join the one cluster pass (X2), not a
  pins-only one.

### Variant B — one quiet pin, whose by a dot

**Verdict: fail as drawn.**

- **B1 (blocking). It unnames the gate's destination.** With the layer on, the gate's pin *is*
  Tree farm's pin, and in B that pin is unlabelled at overview zoom (`places-b-one-pin-1-map-*`).
  The shipped M26 map labels it *Tree farm* today. Turning on a layer must not take words off the
  map. If B were picked, a place a gate leads to would have to keep its label.
- **B2.** The 8 px whose-dot is hard to read against terrain, and it's the only cue to whose.
- **B3.** With no list, nothing finds the place underground or in the sky.

### Variant C — pick whose, then named

**Verdict: pass against DESIGN, weakest against the brief.**

- **C1 (blocking if picked).** The same as B1: under *Everyone's places*, Tree farm's pin is
  unnamed (`places-c-whose-pill-1-map-*`).
- **C2.** The pill is a new floating control in the top-left, the one corner the shipped map keeps
  for Back alone. At 320 it competes with the dimension switch for the top 120 px.
- **C3.** Its real strength, *show me Steve's places*, is A's list grouped by player plus one more
  tap, so the pill duplicates it.

### Recommendation

**Agree with the builder: A.** Fix X1, X2, X5 and S2. If a world ever has a hundred places a
player, C's chips can go under the Layers row later (not on the map).

## 2 · Villagers and pets

### Variant A — a village is one tile

**Verdict: pass with changes.**

*Village · 23 villagers* on the amber tile is the most readable overview in the section, and the
village keeps the tile the map already draws. The filter retitling (*Village · 4 librarians*) puts
*who sells Mending* on the map without numbers. Zoomed in, the job discs separate into the real
layout (`mobs-a-village-tiles-2-villager-*`).

- **Must change:** X2 and X5 (the pet tiles overlap and Biscuit's label covers Luna, at 320), S3
  (the age), S5 (traders out of the count), S6.
- **A1.** The count badge on the village tile sits on gate 1's dotted line at the overview. Make the
  corner count an obstacle for lines, or move it to the side away from them.
- **A2.** Grouping is ours (48-block single link, in the sidecar). That's fine to start; it isn't an
  owner question (below).

### Variant B — every villager by its job

**Verdict: fail on the overview.**

- **B1.** At the overview the village is three or four ink count badges (*3*, *19*, *2*) on the
  gate line, and the located village's tile is swallowed by the count, so the map loses the word
  *Village* (`mobs-b-job-glyphs-1-map-*`). That's worse than the shipped map without the layer.
- **B2.** The floating chip row is a second row of chrome under the dimension switch. At 320 it
  shows two and a half chips beside the zoom buttons (`mobs-b-job-glyphs-320-map-*`).
- **B3.** It has the same X2 and X5 overlaps as A.

### Variant C — nothing until you ask

**Verdict: pass, but it's the chat's job.**

Clean and honest. The numbered matches with *208 blocks north-west · 12 emeralds and a book* are the
best answer to *who sells Mending* in the round (`mobs-c-find-first-1-map-*`). But the layer draws
nothing by itself, which reads as broken on a map whose other layers draw on their own. And it
repeats `mc_find_villager` / `mc_find_pet`, which the chat card can already show.

- **C1.** The *1* disc sits on the Village tile. Numbered results must avoid tiles, or join the
  cluster pass (X2).

### Recommendation

**Agree with the builder: A**, with C's ranked list kept as what a filter chip shows in the panel
under the row (*Sells Mending · nearest BlockyFox first*). It costs one list on top of A, and it
gives A's retitled tile a way to the actual villager.

## 3 · Beds and respawn points

### Variant A — every respawn point, always

**Verdict: pass with changes.**

One tile per player is cheap, and *where will everyone wake?* is a safety question the owner should
be able to answer from the overview. The curve on a tapped death (`beds-a-always-drawn-4-death-*`)
is the payoff, and the warn-text world-spawn line (`-5-death2-`) says the thing a parent wants
to know.

- **Must change:** X3, X4, S8 (shared and pod beds near the star), S9 (the skull icon in light).
- **A1.** Because it's always drawn, a stale bed (X4) is always on screen. That makes X4 more urgent
  for A than for B.

### Variant B — a bed shows with its player

**Verdict: pass, weaker against the brief.**

Calm, and *Show the bed* on the player card is a good action (`beds-b-with-the-player-2-player-*`).
But the Layers row is called *Beds & spawn* and is **on**, and the overview shows no beds: the row
promises what the map doesn't draw. And *is Steve's bed gone?* needs the list.

- **B1.** If B is picked, rename the row (*Spawn · beds on tap*) or turn the beds part into a
  sub-option.

### Recommendation

**Agree with the builder: A**, on the condition that X4's box check runs first. If the game can't
tell the pack that a bed is gone before the next death, A should draw a remembered bed it hasn't
confirmed with a dashed ring (*bed not seen there since …*), M26's language for *not active*.

## Cross-variant comparison

| | Places A | Places B | Places C | Mobs A | Mobs B | Mobs C | Beds A | Beds B |
|---|---|---|---|---|---|---|---|---|
| Answers with no tap | **Yes** | No (zoom) | No (pick) | **Yes** (count) | Partly | No | **Yes** | No |
| Keeps M26's words | Yes | **No** (B1) | **No** (C1) | Yes | **No** (village) | Yes | Yes | Yes |
| New floating chrome | None | None | Pill | None | Chip row | None | None | None |
| 320 × 568 | Good | Good | Crowded top | Overlaps (X2) | Overlaps, chips | Good | Good | Good |
| DOM audit | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| Verdict | **Pass w/ changes** | Fail | Pass, weakest | **Pass w/ changes** | Fail | Pass | **Pass w/ changes** | Pass |

## What must change before build

1. X1: gate ↔ place matched by position; both drawn when they differ.
2. X2: one cluster pass for every point marker but players; the rule written into DESIGN with S1's
   draw-order amendment.
3. X3: beds' storage decided with the share answer, firewalled in RLS with its isolation test.
4. X4 and the README's box checks, before Beds is built.
5. X5: label obstacles keyed per marker (the shipped `placeLabels` behaviour).
6. S2 (goal line words), S3 (no fake age on a live world), S5 and S6 (traders, unknown owners),
   S9 (tokens).
7. Places: `saved_at` from `updated_at`; colours and names from the current gamertag.

## Changes made in this review

Only mechanical README fixes, no design change and no mock change:
- *The share link is question 5* → **4**; *That is question 9* → **8**.
- *per player, per world folder and dimension, at most 100 each* → *per player and world folder
  (each place carries its dimension), at most 100 a player a world*, which is what the unique index
  and `PLACES_MAX` do.

The token literals (S9) and the mock's label keys (X5) were left as found, because fixing them
would leave the 116 PNGs out of date.

## The owner's questions

The thirteen are mostly right. These should change:
- **Q2** should say what really happens in game: the arrow changes with no word from Dave, and a
  live scan ends. *Ask Dave…* doesn't change that; it only puts the confirmation in the app chat.
- **Q4** should note that the respawn spot after each death is already on share links (the
  timeline), so *owner-only beds* hides the bed, not where they woke.
- **Q6** is better as one question about all four defaults (places on, beds on, villagers and pets
  off).
- **Q9 (village grouping)** is an engineering choice the owner can't judge without the box check.
  Drop it: build the 48-block grouping, and switch to BDS's records later if they read cleanly.
- **Q11** mixes an owner decision (a 10 s poll in their players' game) with a storage choice whose
  only owner-visible effect is the share firewall (X3). Keep the first part; the storage follows
  from Q4.
- **Q12** should say the inference may misfire until the box check: Bedrock keeps a broken bed's
  spawn until the next death, and wakes players beside it, not on it.
- **Q13**: `place` is already a focus kind; only `villager`, `pet` and `bed` are new.

The order to ask them, each answerable on its own:

1. Saved places: A (named pins in the player's colour, with a list), B (one quiet pin, a whose-dot)
   or C (pick whose, then named)?
2. The 48-block goal-chest circle round a tapped place: drawn, or words only?
3. *Point BlockyFox's Tricorder here* on a place's card (live world only): a button, *Ask Dave…*, or
   left out? Either way the player's arrow changes with no word in game, and a running scan stops.
4. Villagers and pets: A (a village is one tile with its count, filter chips in Layers), B (every
   villager by job, chips on the map) or C (nothing until you ask, numbered matches)?
5. A villager's card: every trade it offers now, by level (as drawn), or only the notable ones
   (enchanted books, and what the filter matched)?
6. *Read the world again* on the villagers and pets row (forces a save, at most every 30 s, live
   world only): offer it, or rely on the automatic re-read?
7. Beds: A (every respawn point, always) or B (a bed shows with its player)?
8. The pack change for beds: a `spawnpoint` line on bed or anchor use, on join, and from a check of
   each online player every 10 s. Approve?
9. A death card's line when they woke at the world spawn though a bed was set: *their bed was
   missing or blocked* (it may misfire until we've tested it on the box), or only *Respawned at the
   world spawn*?
10. Layer defaults: saved places on, beds on, villagers off, pets and named mobs off?
11. Saved places, villagers, pets and beds on a share link (`/mc/s/…`): owner-only like beacons and
    gates (recommended), or shown to whoever has the link? (Where each player woke after a death is
    already on the link's timeline.)
12. Dave's chat card: add `villager`, `pet` and `bed` as things it can open on (`place` already
    exists) in this build, or later?
