# GUI gate — No-Spawn Beacons and wormhole gates on the Minecraft map (M16, M12)

> **Status:** Living · **Last verified:** 2026-10-12
>
> **Decided — the owner picked A, as revised (2026-10-12).** `a-always-drawn.html` and its
> `png/a-*` shots are the binding spec, built in plan §M26 and written into DESIGN.md
> ("Beacons and gates on the Minecraft map"); B and C stay as the round's record. The
> independent review is `REVIEW.md` in this folder: it picks **A** with changes, and
> revision 2 (below) makes them. Behaviour sources:
> `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M16, §M12, §M8 and P3 (the map and its chat card),
> and §M21 (the Tricorder target's marker grammar).

The plan says protected areas and gates *show on the M8 map*. The shipped map's Layers tab
lists **Wormhole gates** with a *later* badge, and beacons aren't on it at all. These are
three mocks of how both arrive. They cover the full map, as a pushed page, and Dave's
`minecraft_map` chat card. They are built on the binding map
(`../minecraft-map/a-full-bleed.html`, shipped as `frontend/src/screens/MinecraftMap.tsx` and
`minecraftMapDraw.ts`).

Open each `.html` in a browser:

- Each one is self-contained and works offline.
- The terrain is drawn procedurally on a canvas. Explored ground is in full colour and the
  satellite survey is dimmed. The terrain colours are raw RGB inside the picture, as the
  renderer's biome table is.
- It is phone-framed and follows the system theme. The map picture's halo, ink and fog stay
  dark in both themes, and only the label plates follow the theme (DESIGN "Map-only
  tokens").
- **Mock controls** (dashed) are reviewer chrome. States open by hash: `view=map | gate |
  gate3 | beacon | layers | card` and `theme=dark|light`.

## The marker grammar (identical in all three)

Both kinds follow the structures' grammar: a dark `--map-halo` tile, a 1.5 px accent ring
and a `--map-ink` Lucide glyph. Each has a 46 px hit circle and clusters with the other
point markers under 40 px. Each kind's colour carries a meaning the map already uses:

| Marker | Colour | Glyph | Why that colour |
|---|---|---|---|
| **No-Spawn Beacon** | `--green` ring | shield | *safe*: green is ok/healthy. Its area is green too. |
| **Protected area** (the 3 × 3 chunks, 48 × 48 blocks) | `--green` 2 px dashed outline over a halo, optional 16 % green fill | — | the chunk-aligned square, drawn to the block. Two overlapping beacons show both squares. |
| **Gate · open** | `--violet` solid ring, the portal filled violet with its swirl | portal (the app's `PortalIcon`) | violet is the scrubber's portal glyph, so travel reads the same |
| **Gate · dormant** | `--violet` **dashed** ring, the same portal and swirl, unfilled | portal + swirl | the same glyph, so it still reads as a gate; the dashed ring and the empty frame carry *not open* |
| **Where an open gate leads** | a dotted `--violet` line (1 on, 6 off, 3 px over a 6 px halo) with an arrowhead, ending at a violet-ringed pin labelled with the place | pin | dotted, so it never reads as a solid trail or as the Tricorder's dashed bearing (8/6) |
| **A gate into another dimension** | no line; a label plate *to Nether base · in the Nether* by the gate | — | the line would end in another dimension's coordinates; no arrow, which would read as a bearing |

**Draw order:** areas, gate lines, place pins, beacons, gates, the selection ring, then
**player discs on top**. A player is never under a gate, beacon or selection ring.

**Labels** follow the map's label rule, **extended**: to the right, else the left, else below,
else above. A label that would still hit something is **dropped, never drawn under or over
it**. Things it must not touch:
- the edge, a floating control, the panel or the scale bar;
- another label, a marker tile or a player disc;
- **any drawn gate line** other than its own (and the Tricorder bearing, when shown), so a
  plate never sits on another gate's line and appears to say where that line goes.

Markers are kept clear of the floating chrome by the camera. The mocks also follow DESIGN's
rule that the + / − go under 380 px of visible map, and the segments and *show everyone*
under 230 px.

**Values** use a real minus sign (*X −160*) and never break inside a number, a unit or a
time.

**Proposed token: `--violet-text`.** DESIGN's accent-text tokens have no violet. These mocks
use `--violet-text` for the *open* pill, the gate's panel icon and the *Leads to* arrow. In
dark it is `var(--violet)`; in light it is `color-mix(in srgb, var(--violet) 52%,
var(--text))`, the same pattern as `--steel-text`. The review measured it at 6.5:1 on white.
Add it to `tokens.css` and to DESIGN's "Accent text tokens" list with the pick.

**Tapping a gate** opens the panel's *what you tapped* block:

- the portal on a violet tint, **Wormhole gate 1**, then *open* or *dormant*, *BlockyFox's*,
  *planted Oct 11, aimed Oct 12*;
- *X · Y · Z* with **Copy X Z**;
- *Leads to **Tree farm** · 245 blocks north-west · X −160 · Z −140*, or *· in the Nether ·
  X −20 · Z 40*;
- **Show Tree farm** (frame both ends) or **Show it in the Nether** (switch dimension). A
  dormant gate says *Not open yet. BlockyFox can ask Dave to open it to one of their saved
  places, standing within 4 blocks.*

**Tapping a beacon** gives:

- the shield on a green tint, **No-Spawn Beacon**, *protecting*, *placed by Mira_P, Oct 11*;
- the coordinates with Copy;
- *48 × 48 blocks, floor to sky — X 32 to 79, Z −64 to −17 (the 3 × 3 chunks round it) ·
  overlaps 1 other beacon. Wild and hostile mobs vanish there; tamed, named, fed and bred
  ones stay.*

**Honesty.** These positions are the pack's records, sent when they change (see *Data path*).
On a world that isn't loaded they are *as of {date}, when it was last played*, under the
map's existing status line. They never claim *live* for a world that isn't running.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-always-drawn.html` | *Always drawn.* Two overlay rows, **No-Spawn Beacons** and **Wormhole gates**. With them on, every protected square is outlined, and every open gate's dotted line and destination are on the map. A tapped beacon's square is filled. The Layers tab carries an **In this world** list. |
| **B** | `b-on-tap.html` | *Quiet until tapped.* Glyphs only. An open gate wears a small violet arrow badge. A tap draws that beacon's square, or that gate's line, pin and label. A sub-choice under Beacons, **Areas when tapped · All areas**, lets the owner see every square. |
| **C** | `c-one-layer-list.html` | *One layer, listed.* A single **Beacons & gates** row. Areas are outlines only (filled when tapped), and gates carry a *to Tree farm · NW* label instead of a line (the chat card too). An **In this world** list in the Layers panel flies to each one. |

- **A — for:** it answers *is my base covered?* and *where does this gate go?* with no tap.
  It's the plan's wording made literal, and it matches the Tricorder's always-drawn bearing.
  **Against:** it's the busiest map. Long gate lines cross a whole base, and three or more
  open gates at a hub become a fan of dotted lines. The squares tint the terrain under them.
- **B — for:** it's the calmest map. It scales to many gates, and it keeps the trails and the
  Tricorder bearing legible. The arrow badge still says which gates lead somewhere.
  **Against:** coverage needs a tap or the *All areas* setting. You can't see where a gate
  goes without tapping it, which is the question the owner asked. The badge is small at 24
  px.
- **C — for:** the list finds a gate or beacon that's off screen, or up at Y 210 in a sky
  base, without panning. Labels say where a gate goes without lines. It's one toggle.
  **Against:** labels crowd faster than lines when gates cluster, and they drop first under
  the collision rule. One toggle can't show beacons without gates. The list repeats what the
  map shows.

**The chat card** (`view=card`) is the same in all three. Dave's answer to *"where's
BlockyFox's gate?"* is the shipped `minecraft_map` card with a new focus kind (`gate` or
`beacon`), drawn with the gate selected (line, pin and label). Under it are the *what it
shows* row and the overlay chips. The chips gain **Beacons · Gates** (A and B) or **Beacons &
gates** (C). The card stays owner-only, as DESIGN says, so it shows every player's gates.

## Rendered (`png/`, 390 × 844 at 2×, dark and light; plus 320 × 568)

For each variant `v` in `a`, `b`, `c`:

- `png/{v}-1-map-{theme}.png` — the overview;
- `png/{v}-2-gate-{theme}.png` — gate 1 tapped;
- `png/{v}-3-gate-nether-{theme}.png` — gate 3, into the Nether;
- `png/{v}-4-beacon-{theme}.png` — a beacon tapped, zoomed;
- `png/{v}-5-layers-{theme}.png` — the Layers tab at 80 %;
- `png/{v}-6-chat-card-{theme}.png` — Dave's card;
- `png/{v}-7-map-320x568-dark.png` — the overview on a 320 × 568 phone;
- `png/{v}-8-busy-hub-{theme}.png` — a hub of three more open gates by spawn. It is A's
  stress case: four dotted lines fan out, and labels drop rather than land on a line.

**Revision 2** answers `REVIEW.md` in all three variants:
- **X1:** players are drawn on top.
- **X2:** the dormant glyph is the portal, unfilled.
- **A1 / C1:** gate lines and player discs are label obstacles.
- **C2:** a non-directional *to … · NW*.
- **X3:** the cameras are re-framed. The render script now warns when a marker sits under a
  floating control, and none does.
- **X4:** the fixture says *two gates open*.
- **X5:** real minus signs and no breaks inside values.
- **X6:** the legend swatches sit on the neutral `--map-void`.
- **A2 / A3 / A5 / A4:** A outlines its squares and fills the tapped one, keeps no arrow
  badge, and gains the list and the hub state.
- **C5:** C's chat card uses its labels.
- **Light PNGs:** re-taken. The first set lost the 13 % tints when reduced to 256 colours;
  they are now palette PNGs that keep every frequent exact colour.

The render script audited every shot: no horizontal overflow, every DOM control's hit area at
least 44 px, and no console errors. All passed. The canvas labels were checked by eye against
the drop rule.

## Recommendation

**A, as revised** — the independent review's pick too: lines are label obstacles, players
are on top, the dormant gate is still a portal, squares are outlined with the tapped one
filled, the **In this world** list is in Layers, and there is **no arrow badge**. The solid
versus dashed ring and the line already tell open from dormant. The owner's two questions are *what's protected* and *where
does it go*, and A is the only variant that answers both without a tap. The volume is small:
the costs (a Power Pack and 8 echo shards per beacon; a seed, 90 diamonds and a pack per
gate) keep counts in the single digits. The dotted line is distinct from the trails and the
Tricorder bearing. The *Wormhole gates* toggle is there for the day it gets busy. The list
costs little and is the only way to find a sky-base beacon the overview doesn't frame.

## Data path (how positions reach the backend and the map)

**What exists.**

- **Beacons** live only in the world, as dynamic properties `jb:beacon:<dim>@x,y,z` →
  `{dim,x,y,z}`, with **no placer recorded**.
- **Gates** live in `jb:gates` (the id list) and `jb:gate:<id>` → `{id, frame…, planter,
  planterId, target: {place,x,y,z,dim}|null, opened}`.
- Nothing outside the game can read either. The pack only prints free-text console lines:
  `[jbrain] no-spawn beacon on/off at x y z (dim)`, `wormhole planted: gate N by X at …`,
  `collapsed …`, `opened/aimed: gate N … leads to {place} at x y z (dim)`, and `wormhole
  forgot {place} for {player}: n gate(s) went dormant`.
- The sidecar (`deploy/minecraft/bds.py`) already turns **JSON** pack lines
  `[jbrain] {"ev": …}` into queued events for a closed list (`PACK_EVENTS = death, respawn,
  dave, wreck`). It type-checks every field and takes the xuid from BDS's own connect line.
- The backend drains `/events` into `app.mc_player_events` and friends
  (`backend/src/jbrain/minecraft/sessions.py`).

**Recommended, the simplest robust path.** It is the same pipe deaths already use, with
nothing new to poll.

1. **Pack: emit structured lines next to the existing prose lines** (the prose stays for
   Dave's tools, which read it back).
   - `{"ev":"beacon","op":"on"|"off","dim","x","y","z","by"}`: `by` is the placer, newly
     captured from `playerPlaceBlock`. It is left empty when the record dropped on a sweep.
   - `{"ev":"gate","op":"planted"|"opened"|"aimed"|"dormant"|"collapsed","id","dim","x","y","z","axis","by","target":{place,dim,x,y,z}|null}`.
   - **A resync on load:** after the first tick (a script can't read world properties while
     it first loads, as `beacon.js` notes), one `{"ev":"beacons","all":[…]}` and one
     `{"ev":"gates","all":[…]}` snapshot. That covers a restore, an import or a missed event.
     A snapshot replaces the world's set; an event patches it.
2. **Sidecar:** add `beacon`, `gate`, `beacons` and `gates` to `PACK_EVENTS`. Validate them
   like deaths: ints clamped to ±30,000,000, the dim from the closed enum, `op` from its list,
   strings cut to 40, snapshots capped (say 256). Queue them with the current `world` folder.
   They need no xuid. `by` is a gamertag and is matched to the roster by the backend.
3. **Backend:**
   - Add two tables, `app.mc_world_beacons (world, dim, x, y, z, placed_by, placed_at)` and
     `app.mc_world_gates (world, gate_id, dim, x, y, z, axis, planter, state, target_place,
     target_dim, target_x, target_y, target_z, planted_at, aimed_at)`. Both are owner-scoped
     with RLS, like `app.mc_tricorder_targets`, and each needs its isolation test.
   - The existing event drain upserts and deletes rows, and replaces a world's rows on a
     snapshot. `world_replaced` (reset or import) clears them, as it clears the travel log.
   - Add one route, `GET /api/minecraft/map/markers?slot=&dim=` → `{beacons:[…], gates:[…],
     as_of}`, read by the map and the chat card. It works with the server stopped or the
     world unloaded, because the rows outlive the session (*as of* the last report).
4. **Map:** two draw functions in `minecraftMapDraw.ts` (or one, for C). The *Wormhole gates*
   row loses its *later* badge, and `minecraft_map` gains the focus kinds `gate` and `beacon`.
5. **Dave:**
   - The same rows answer *"where's my gate?"*, *"where does gate 2 go?"* and *"is my base
     covered?"*.
   - Add a read-only tool such as `mc_world_tech` that lists the asker's gates and beacons,
     or the M18 fast path for the plain asks.
   - Covered checks are a pure function: a saved place is covered if its chunk is within one
     chunk of a beacon's chunk in the same dimension.
   - This needs no new pack round-trip. Gates already carry their planter, and beacons get
     theirs from step 1.

**Why not the alternatives.** Parsing the prose lines is fragile (the words are owner-editable
copy) and can't resync. Reading the world's LevelDB from the sidecar needs a LevelDB parser
and gives stale reads while the server holds the files. Keeping the state only in the sidecar
(like the structure sweep) would work for the map, but Dave's answers and owner scoping
belong in Postgres with the rest of the travel log.
