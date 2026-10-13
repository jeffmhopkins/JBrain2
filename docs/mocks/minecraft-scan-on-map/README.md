# GUI gate — the Tricorder's live scan on the Minecraft map (M25 follow-up)

> **Status:** Living · **Last verified:** 2026-10-13
>
> **Open — waiting for the owner's pick.** Three variants. An independent review is due
> before the pick. Behaviour sources: `docs/plans/MINECRAFT_BEDROCK_PLAN.md`:
> - §M25 ("The Tricorder's live scan"). Its *The map (§M21)* note says the scan isn't shown,
>   because its target never reaches the backend.
> - §M21 (the Tricorder target on the map).
> - §M26 (beacons and gates, whose marker language this keeps). DESIGN.md has it as "Beacons
>   and gates on the Minecraft map" and "The Tricorder target on the Minecraft map".

*"Dave, scan for diamonds"* puts a player's Tricorder in scan mode:

- Every 10 s the pack finds the nearest matching block in a **33 × 33 × 33 box** round the
  player. That is `SCAN_REACH = 16`, a block each way, up and down too; nearest is by 3D
  distance.
- The held Tricorder's arrow points at the block found.
- With none in reach it reads *Scanning: diamond · none nearby*.
- The scan ends only on *stop scanning*, a cleared arrow, or a new waypoint. The waypoint
  under it is kept and comes back when the scan stops.

Today the map keeps drawing the player's waypoint during a scan, because the scan's target
lives only in the pack. These are three mocks of how an active scan shows:

- on the full map;
- in the tapped player's card;
- in Dave's chat answer.

Open each `.html` in a browser:

- Each one is self-contained and works offline. It is built on the binding map and markers
  mocks.
- The terrain is drawn procedurally. Its colours are raw RGB inside the picture.
- It is phone-framed and follows the system theme. The halo, ink and void stay dark in both
  themes, and only the label plates follow the theme (DESIGN "Map-only tokens").
- **Mock controls** (dashed) are reviewer chrome. States open by hash: `view=found | none |
  overview | card` and `theme=dark|light`.

The fixture: BlockyFox is down a mine at y −52, scanning for diamond. The last scan, 40 s
ago, found one **14 blocks north-east, at y −58** (6 below them). Their waypoint, *Tree
farm*, waits under the scan. Mira_P is at the surface by her No-Spawn Beacon. Gate 1 is
nearby, open.

## What is the same in all three

- **The found block** uses the Tricorder target's own grammar (§M21): two rings and a centre
  dot in the player's colour over a halo. In game it *is* the arrow's target, so it looks
  the same as a waypoint's target. The **bearing** is §M21's dashed line (8 on, 6 off, the
  player's colour over a halo), drawn for the tapped player. At 4 px a block it is short,
  because the block is at most 16 blocks away.
- **The waypoint under the scan** isn't drawn and has no bearing while the scan is on. The
  in-game arrow doesn't point at it, and two bearings from one player would be a lie about
  where the Tricorder points. The card says *Their waypoint, Tree farm, comes back when the
  scan stops.*
- **The scan's area is a square, not a circle.** The pack searches a 33 × 33 × 33 box, so the
  map draws the 33 × 33 block square, centred on the player, to the block.
  - It reads as a sensor's field: **viewfinder corners** (2.5 px over a 5 px halo, in the
    player's colour) joined by a 1 px hairline at 70 %.
  - That keeps it apart from a beacon's protected square (a 2 px `--green` dash, 6 on, 4 off)
    and from the chunk grid, even for a green player (Mira_P).
  - A light 10 % wash of the player's colour fills it while a block is found.
- **The player card's line** is the shipped `.mc-map-det-tri` line, carrying the pack's own
  label, so the app and the held Tricorder say the same words:
  - *Tricorder · Scanning: diamond · 14 blocks north‑east · y −58*;
  - with nothing in reach, *Tricorder · Scanning: diamond · none nearby*.

  Under it, *Found 40 s ago. Their waypoint, Tree farm, comes back when the scan stops.* The
  glyph is a radar (Lucide `radar`) instead of the crosshair, so a scan reads apart from a
  waypoint at a glance.
- **When the panel is short**, the map keeps 190 px, the panel stops there, and the card
  scrolls inside it. The camera frames the tapped scanner so the whole square, and room for
  its plate, sits between the top row and the scale bar. On a 320 × 568 phone only **Back**
  is left up top (DESIGN's under-230 px rule).
- **Not drawn** on the timeline's replay (a scan isn't recorded), nor while the player is
  offline: the scan is kept for them, but nothing scans until they rejoin. The card then
  says *Scanning: diamond — resumes when BlockyFox is back on* (not drawn). Shared maps
  follow the Tricorder target's rule and show it the same way.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-square-always.html` | *The scan square, always drawn.* Every scanning player wears their square with a *Scanning: diamond* plate on its top edge (*Scanning: diamond · none nearby* when empty), and the block it found. Tapping the player adds the bearing and the card's line. Dave's chat answer is the shipped `minecraft_map` card, with a new focus kind `scan`. |
| **B** | `b-badge-on-tap.html` | *A badge, the rest on tap.* A scanning player's disc carries a small scan badge (two arcs in their colour on a halo dot). Nothing else is drawn until they're tapped, which is §M21's rule for the waypoint target. Then the square, the block and the bearing appear, and the words live only in the card. Dave answers in words, with no map card. |
| **C** | `c-radar-card.html` | *The radar in the card.* The map stays quiet: the square and the block show only for the tapped player, with no plate. The card carries a top-down **radar** of the 33 × 33 blocks: the player in the middle, the block where it lies, north up. Beside it are the words, the distance, and the **depth** (*↓ 6 blocks below them · y −58*), which a top-down map can't show. Dave's answer carries the same radar instead of a map card. |

- **A — for:** you can see who is scanning, for what, and whether it's finding anything,
  with no tap. *None nearby* is on the map, which is the state worth noticing (*they're
  digging in the wrong place*). It's the M26 owner pick's spirit: always drawn, answered
  without a tap.
  **Against:** at the overview zoom, 33 blocks is about 40 px, so the square is small and
  the found block nearly sits on the disc. The plate competes with the player's label and
  drops first. On a 320 × 568 phone with the player tapped, the *none nearby* plate has no
  room and is dropped by the label rule, so only the card says it.
- **B — for:** the calmest map, and the same rule as the Tricorder target it extends: tap the
  player to see where the Tricorder points. The badge still says *scanning* at any zoom.
  **Against:** the badge is small (15 px) and can't say *none nearby*, so you have to tap.
  With no chat card, the chat answer can't be opened on the map.
- **C — for:** the only variant that shows the third dimension. Diamonds are deep, and *6
  below you* is the useful fact. The radar is legible at any map zoom, because it's drawn at
  its own fixed scale (about 3 px a block, 2.3 under 360 px), and the same card works in chat.
  **Against:** the radar is a new component: a small canvas in the card, and in chat a new
  tool-view. On a 320 × 568 phone the card scrolls inside the panel (the Follow and Sessions
  row is below the fold). The map alone says nothing about the scan until you tap.

## Recommendation

**A, with C's depth line in its card.**

- The scan is a *state of the player*: *is BlockyFox's Tricorder finding anything?* A answers
  that on the map with no tap, the way the owner chose for beacons and gates.
- The square and plate are small and appear only for players who are scanning, which is few
  at a time.
- C's one real gain is the depth. Folding *↓ 6 blocks below them* into A's card line (the
  *y −58* is there already; the *below them* is the missing reading) gets it without a new
  component.
- B's badge is too small to carry *none nearby*, which is the state worth seeing.

## Data path (what the map needs; nothing is built)

The scan's target is found in the pack every 10 s and never leaves it (§M25). Showing it
needs the M26 pipe: structured pack lines, the sidecar's `PACK_EVENTS`, and a backend row.

1. **Pack:** one JSON line **on change only**, never on every scan, so a steady target costs
   nothing: `[jbrain] {"ev":"scan","op":"set"|"found"|"none"|"stop","player","label","ids",
   "dim","x","y","z"}`.
   - `found` carries the block, and is sent when the nearest block changes.
   - `none` is sent when the scan goes empty.
   - `stop` is sent on a stop, a waypoint set, or a clear with `end_scan`.
   - On load, one `{"ev":"scans","all":[…]}` snapshot from the `jb:scan:<gamertag>`
     properties (labels only; the targets come with the first scan).
2. **Sidecar:** add `scan` and `scans` to `PACK_EVENTS`. Validate like M26: `op` from its
   list, `dim` from the enum, ints clamped, `label` cut to 40, `ids` capped at 8, snapshots
   capped.
3. **Backend:** a scan row that **doesn't overwrite** the waypoint in
   `app.mc_tricorder_targets`. Either nullable `scan_label`, `scan_x/y/z`, `scan_dim` and
   `scan_at` columns, or a sibling table `app.mc_tricorder_scans`. Owner-scoped RLS with its
   isolation test. `world_replaced` clears it. The map's existing player payload gains
   `scan: {label, found: {x,y,z,dim}|null, at}`.
4. **Map:** a `scanSquare` draw next to `bearingLine` and `targetMark` in
   `minecraftMapDraw.ts`. The `.mc-map-det-tri` line takes the scan's label. In A, the
   `minecraft_map` card gains the focus kind `scan`.

Positions are live while the world runs, like the players'. *Found 40 s ago* is the event's
own time. A mined block sends the next `found` or `none` within a second (§M25: *the scan
runs again within a second*).

## Open questions for the owner

1. Which variant: A (the square always drawn, with a plate), B (a badge, details on tap), or
   C (a radar in the card)?
2. Is drawing the scan's area as the true 33 × 33 square right? Or would you rather see a
   circle, which reads as "within 16 blocks" but isn't the box the pack searches?
3. While a scan is on, should the map hide the waiting waypoint entirely, as drawn, or show
   its target faintly with no bearing?
4. Should the card say how deep the block is relative to the player (*6 blocks below them*),
   as C does, whichever variant is picked?
5. Should Dave's chat answer about a scan carry a map card (A), a radar (C), or words only
   (B)?
6. Should an offline player's kept scan show anywhere in the app (*Scanning: diamond — resumes
   when BlockyFox is back on*), or only while they're online?
7. Is the new pack event worth it? It is one console line per change of target, and the
   cost is the M26-style pipe and a scan row in the database.

## Rendered (`png/`, 390 × 844 at 2×, and 320 × 568 at 2×, dark and light)

For each variant `v` in `a`, `b`, `c`:

- `png/{v}-1-found-{theme}.png` — BlockyFox tapped, a diamond found;
- `png/{v}-2-none-nearby-{theme}.png` — BlockyFox tapped, none in reach;
- `png/{v}-3-overview-{theme}.png` — the overview at 1.25 px a block, nobody tapped;
- `png/{v}-4-chat-{theme}.png` — Dave's answer to *"Is BlockyFox's Tricorder still
  scanning?"*;
- `png/{v}-5-found-320-{theme}.png` — found, on a 320 × 568 phone;
- `png/{v}-6-none-320-{theme}.png` — none nearby, on a 320 × 568 phone.

The render script audited every shot:

- no horizontal overflow;
- every DOM control's hit area at least 44 px;
- no console errors;
- no marker under a floating control, the scale bar or the panel.

All passed. The canvas labels were checked by eye against the drop rule.
