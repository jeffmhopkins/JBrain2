# Independent review: the Tricorder's live scan on the Minecraft map (M25 follow-up)

> **Status:** Living · **Last verified:** 2026-10-13
>
> An independent review of `a-square-always.html`, `b-badge-on-tap.html` and
> `c-radar-card.html` in `docs/mocks/minecraft-scan-on-map/` (branch `docs/mc-goal-scan-mocks`,
> commit `ac57585e`). The reviewer did not build them and had not seen the designer's reasoning
> (PROCESS.md, "GUI gate"). The README was read for the brief, the fixture and the states, and
> its claims were checked against the code. Every judgement of the design is of the rendered
> pages and PNGs, measured against `docs/reference/DESIGN.md` ("Map-only tokens",
> "Accessibility", "The Minecraft map": markers, labels and the panel; "Beacons and gates on the
> Minecraft map"; "The Tricorder target on the Minecraft map") and the plan
> (`docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M25, §M21, §M26). The format follows
> `../minecraft-map-markers/REVIEW.md`. Nothing in the mocks was changed.

## How it was checked

- **Every PNG was viewed**: `{a,b,c}-{1..6}-*-{dark,light}.png`, 36 in all.
- **Every page was rendered again** in headless Chromium at 390 × 844 and 320 × 568, dark and
  light, for every hash state (`view=found|none|overview|card`), 48 renders. Each was audited in
  the DOM for horizontal overflow, hit areas under 44 px, text under 4.5:1 against its painted
  background, and console errors. **All clean.** No overflow, every DOM control is at least
  44 px, no contrast failure, and no console errors. The canvas hit circles can't be judged from
  a picture.
- **Tokens:** no hex outside the two token blocks. The terrain is generated inside the picture.
  One stray literal: C's radar grid is `rgba(242,242,242,.07)` (line 497 in all three files, which
  differ only in `const V`). That's `--map-ink` at 7 %, so derive it from the token (S8).
- **The light PNGs paint their tints and plates correctly.** The capture fault the last two reviews
  found isn't here.
- **Code checked for the README's claims**: `deploy/minecraft/jbrain-pack/scripts/scan.js`,
  `rules.js` (`SCAN_REACH`, `scanBox`, `nearestBlock`, `scanLine`, `trackerLine`) and `main.js`
  (the arrow loop); `deploy/minecraft/bds.py` (`PACK_EVENTS`);
  `frontend/src/minecraftMap.ts` (`tricorderLine`), `frontend/src/screens/minecraftMapDraw.ts`
  (`bearingLine`, `targetMark`, `protectedArea`) and `MinecraftMap.tsx` (`.mc-map-det-tri`).
- **Lore:** `bleepbloop_city/notes/lore.md` (at `8e5a76e`), "The Tricorder's live scan"
  (canon, 2026-10-12). Findings are under S5 and S6.

## Is the README true of the code?

Mostly. These are the places where it isn't:

- **The distance isn't the one the Tricorder shows.** The mocks compute `d3`, a 3D distance,
  for the card line, C's radar headline and Dave's words: *14 blocks north-east*. Both the
  held Tricorder (`trackerLine`, via `arrowFor`) and the shipped card line (`tricorderLine`)
  use **horizontal** distance to the block's centre. For the fixture (BlockyFox at 32, −70, the
  diamond at 41, −79) that is **13**. So the README's claim that *the app and the held Tricorder
  say the same words* is false by one block in every variant. Depth already has its own words
  (*y −58*, and C's *6 blocks below them*).
- **"Within a second" holds only while the Tricorder is held.** The mined-block check and the
  walked-away hide run in `scan.line()`, which `main.js` calls only for a player holding their
  own Tricorder. The 10-second `scan.step()` runs for every online scanner. So for a player not
  holding it, the next `found` or `none` comes at the next scan (up to 10 s), not within a
  second. The pack event has to be sent from `step()` as well as from `line()`.
- **"Found 40 s ago" will drift on a steady target.** With events *on change only*, a block
  found 10 minutes ago and still the nearest reads *Found 10 min ago*, though the pack has
  confirmed it 60 times since. Either send a cheap `found` heartbeat (for example once a
  minute), or word it as *since*: *Pointing here since 7:40 pm*.
- **Dave can't answer the chat fixture today, and the data path doesn't give him a way.**
  `mc_tricorder_scan` only starts and stops. No tool reports whether a scan is on or what it
  found. Every variant's `*-4-chat-*` needs one: B's step even reads *Checked BlockyFox's
  Tricorder*. The data path lists the pack, sidecar, backend and map steps, but not Dave's read.
  Add it, for example `mc_tricorder_scan` with a status form or the scan on the player-locating
  tool's answer, with its own isolation test where it reads the new row.
- **"Ends on a cleared arrow"** is true only of Dave's clear (`end_scan: true`). The track
  drain's arrival clear doesn't end a scan (§M25). The README's opening list should say *Dave's
  clear*.
- **"Their waypoint, Tree farm, comes back when the scan stops"** is true of *stop scanning*. A
  new waypoint also ends the scan, and then Tree farm is gone. Dave's clear sends them to spawn.
  The card's line is fine as the common case, but the build shouldn't treat it as always true.
- **True as stated:** a 33 × 33 × 33 box (`SCAN_REACH = 16`, `scanBox` floors the player's
  position, ±16, y clamped), nearest by 3D distance to the block's centre (`nearestBlock`),
  *Scanning: diamond · none nearby* verbatim (`scanLine`), the scan kept in `jb:scan:<gamertag>`
  and run only for online players (`step()` forgets offline keys), and the waypoint row
  untouched. The M26 pipe (`PACK_EVENTS` in `bds.py`) is the right road. The square drawn
  centred to the block is the true footprint of the search.

## Cross-variant findings

### What holds in all three

- **The found block reuses §M21's grammar**, so a parent reads it as *where the Tricorder
  points*: two rings and a dot in the player's colour, with the 8 / 6 dashed bearing.
- **The square, not a circle, is right.** It's the box the pack searches, and the viewfinder
  corners keep it apart from a beacon's dashed green square, even for Mira_P's green (the
  overview shows both, `a-3`). Q2's circle would be a nicer shape that lies about the search.
- **Hiding the waiting waypoint and its bearing is right.** Two bearings from one player would
  misstate where the arrow points.
- **The card line reuses `.mc-map-det-tri`** and the pack's own label. The radar glyph in place
  of the crosshair is a good, cheap tell.

### Blocking (whichever variant is picked)

- **X1. Use the horizontal distance** (above). The card, C's headline and Dave's words must
  say what the held Tricorder says (*13 blocks*). That means calling the shipped
  `tricorderLine` with the scan as its target, not a new formula. Re-render the fixture.
- **X2. Add Dave's read path to the data path** (above). Without it, none of the three chat
  answers can be built, and owner question 5 is moot.

### Non-blocking

- **S3. Event cadence.** Send the change event from `step()` as well as `line()` (above), and
  decide the *found N ago* wording, or add a heartbeat.
- **S4. The offline line is undrawn.** *Scanning: diamond — resumes when BlockyFox is back on*
  is only in prose. Draw it once (card only, no square) so the build has the picture.
- **S5. Canon check: the offline wording.** Canon says the scan *keeps scanning, even after you
  log off*. The code (and the README) say it is kept but runs only while they're on. *Resumes when
  they're back on* is the truthful app line, but it reads against the lore page's wording. Since
  the lore page is canon, the owner should either reword canon (*it stays on, even after you log
  off*) or accept the difference. Don't ship the app line until they agree (Q6).
- **S6. Canon check: Dave's chat words.** *It keeps looking every 10 seconds until they ask me to
  stop* drops canon's *or ask him for a new heading*. Use the lore page's ending, or a shorter
  line that doesn't claim *only*.
- **S7. The overview target sits under the disc.** At the overview zoom, the found block's rings
  are half under the player disc (`a-3`, at about (465, 630)). The README notes this. It's
  acceptable because players are drawn on top (X1 of the markers review), but it means *found*
  vs *none* isn't readable at that zoom without A's plate.
- **S8. The radar grid literal** (above). Use `--map-ink` with `globalAlpha`.

## Variant A — the square, always drawn

**Verdict: pass with changes.**

It's the only variant where *is anyone scanning, and is it finding anything?* reads with no
tap. *Scanning: diamond · none nearby* on the map (`a-2`) is the state worth noticing. It follows
the owner's M26 pick (always drawn).

**Blocking**
- **A1. The scan plate pushes out the player's name.** In the overview (`a-3`), BlockyFox has
  **no name label**. The *Scanning: diamond* plate took its place, while Mira_P keeps hers. B's
  overview (`b-3`) shows *BlockyFox*. The same happens in A's chat card (`a-4`). This is the
  reverse of the README (*the plate competes with the player's label and drops first*) and of
  DESIGN's label order (*tapped thing, players, spawn, structures*). On a map whose first job is
  *where is my child*, a scanning player must not lose their name. **Fix:** the scan plate ranks
  below player labels, or it carries the name (*BlockyFox · scanning: diamond*), which replaces
  two plates with one.

**Non-blocking**
- **A2. *None nearby* is lost exactly where it matters.** On a 320 × 568 phone with the player
  tapped (`a-6`), the plate is dropped and only the card says it. The README admits this.
  Acceptable, but it makes A's edge over C smaller on small phones.
- **A3. Fold in C's depth line**, as the README recommends: *↓ 6 blocks below them · y −58*
  under the card line. It's a few words, and it's the one fact a top-down map can't show.

## Variant B — a badge, the rest on tap

**Verdict: pass against DESIGN, weakest against the brief.**

It's the calmest map, and it follows §M21's rule (tap to see where the Tricorder points). The two-arc badge (`b-3`) does
read as *scanning* next to the disc.

**Non-blocking**
- **B1. The badge can't say *none nearby*.** It's about 15 px, the same for found and empty, and
  the same for diamond and for stone. The state worth noticing needs a tap.
- **B2. No chat card** means Dave's answer can't be opened on the map, unlike his other
  location answers.
- **Strength:** if many players scan at once, B is the only variant that stays clean. Its badge
  is a good fallback for A at a zoom where the square is under about 20 px.

## Variant C — the radar in the card

**Verdict: pass with changes.**

The radar is well drawn and honest. It's at its own fixed scale with north up. *None within
16 blocks* with *16 blocks each way and up and down* (`c-2`) is the clearest statement of the
box in the round, and *↓ 6 blocks below them* is the most useful fact in the set.

**Non-blocking**
- **C1. Two pictures of one thing.** With the player tapped, the map draws the square, block and
  bearing, and the card draws them again at a different scale (`c-1`). At 390 px they're
  separated by a scroll, so it reads as repetition.
- **C2. On 320 the card pushes Follow and Sessions below the fold** (`c-5`). Those are the
  shipped card's primary actions.
- **C3. A new component in two places** (a canvas in the card, and a new chat tool-view), for a
  gain that A gets with one line of text (A3).
- **C4. The map says nothing until you tap**, like B, but without B's badge.

## Cross-variant comparison

| | A — square always | B — badge on tap | C — radar card |
|---|---|---|---|
| *Who is scanning* with no tap | **Yes** (square and plate) | Yes (badge) | No |
| *None nearby* with no tap | **Yes** (at 390). Dropped at 320 tapped | No | No |
| Depth (*below them*) | Only with A3 | No | **Yes** |
| Player's name kept | **No** (A1) | Yes | Yes |
| New component | Plate, square | Badge, square | Radar canvas, chat view |
| Chat answer opens on the map | Yes | No | No (a radar) |
| 320 × 568 | Usable | Clean | Card scrolls (C2) |
| Distance matches the held Tricorder | No (X1) | No (X1) | No (X1) |
| Tokens / contrast | Pass | Pass | Pass (S8 literal) |

## What must change before build (whichever is picked)

1. X1: horizontal distance via `tricorderLine`, and re-render the fixture as *13 blocks*.
2. X2: Dave's read path in the data path (tool, wording and test).
3. S3: send events from `step()` as well, and settle the *found N ago* wording.
4. S5 and S6: settle the offline and *until they ask me to stop* wording with the lore page
   before any Dave line ships.
5. For A: fix A1 (the player's label wins, or the plate carries the name), and add A3.

## Recommendation

**Agree with the builder: A, with C's depth line in its card**, on the condition that A1 is
fixed. The best fix is one plate that carries both, *BlockyFox · Scanning: diamond* (*· none
nearby*), drawn where the name label would go, so a scanning player never has fewer words on
the map than an idle one.

B's badge is worth keeping as A's fallback when the square is too small to read. C's radar
isn't worth a new component once A3 carries the depth.

## What the owner's open questions should account for

- **Q2 (square or circle):** the square is the search. A circle would claim a 16-block radius
  the pack doesn't use, and would miss the corners it does search.
- **Q4 (depth):** it's free in any variant: the event already carries `y`, and the player's `y`
  is on the map payload.
- **Q5 (chat card):** whichever is picked needs X2 first. Today Dave can't tell whether a scan is
  on at all.
- **Q6 (offline):** canon currently says the scan *keeps scanning* after log-off. Answering Q6
  also means settling S5 with the lore page.
- **Q7 (is the pipe worth it?):** the cost is a little higher than *one console line per change*
  (S3: events from `step()` as well, and possibly a heartbeat). It's still small, and well below
  the M26 marker traffic.
