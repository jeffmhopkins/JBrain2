# GUI gate — the Minecraft map viewer (waves M8, M8a, P3)

> **Status:** Living · **Last verified:** 2026-10-11
>
> **Decision (binding, 2026-10-11): A, with session navigation 3 · Days** (the day strip
> and that day's session chips, with 1's List behind each player's **Sessions** button for
> older history). The owner picked A, said its sessions were *"a little hard to navigate"*,
> and chose 3 from revision 3's alternatives (*"3 is good"*). Picking A also approved the
> four *Decisions to approve with the pick* below, which were put to the owner with the
> choice. Revisions 2 and 3 answer the independent review in `REVIEW.md`; B, C and
> alternatives 1–2 stay here as the record of what was weighed.

These are three interactive mocks of one map component. It is used in three places:

- a full-screen map, reached from the binding Minecraft screen
  (`frontend/src/screens/MinecraftScreen.tsx`, chosen in `../minecraft-ops/` and
  `../minecraft-worlds/`);
- the same component with M8a's time scrubber;
- the compact card Minecraft_Dave shows in a chat (P3).

Open each `.html` in a browser. Each file:

- is self-contained and works offline, and fetches no images;
- draws its map procedurally on a `<canvas>`, as 256 × 256 px tiles rendered a few per frame,
  the way the box serves them;
- is phone-framed: 390 px, 560 px on desktop, full-bleed under 430 px, and holds at 320 × 568;
- follows the system theme on load, like the app does, with the mock controls to override it;
- copies the Minecraft screen's tokens and component styles in dark and light, with tokens only
  for chrome, and type on the token scale;
- uses the accent text tokens for accent-coloured words, and Lucide-style outline icons;
- keeps a 44 px tap floor on every control, map markers included (a 46 px hit circle; markers
  closer than that cluster);
- honours `prefers-reduced-motion`.

A dashed **Mock controls** strip above the phone is reviewer chrome, not design. It switches
between the states, triggers taps (*a death*, *a spot*, *a player*, *a survey spot*,
*a structure*, *long-press*, *Nether*, *End*) and switches the theme.

## Session navigation (revision 3)

The owner picked A. Revision 3 changes one thing in it: how you choose a session on the
Timeline. Open `a-full-bleed.html`. The mock controls have a **Session nav** row: **Rev 2
stepper**, **1 · List**, **2 · Lanes** and **3 · Days**. You can also add `#nav=0` to `#nav=3` to
the URL. The default is 3, the recommendation. Everything else in A is unchanged, including:

- the *Not live · …* status line while scrubbing;
- the satellite survey and explored ground;
- the 190 px map floor;
- structures;
- the long-press pin.

### Why revision 2's stepper is hard

The tray led with *‹ BlockyFox ▾ · Oct 9 · 3h 42m · 2.6 km ›*. Rendered and used, it fails in
six ways:

1. **One session at a time, with no overview.** The arrows show nothing of what lies behind them.
   To find Steve42's drowning on Friday you step through BlockyFox's sessions, cycle the name to
   Steve42, and step again, reading each one as it appears.
2. **Player and session are mixed in one control.** The name has a ▾ and looks like a menu, but a
   tap cycles to the next player. The new player's session is then picked silently, from wherever
   the scrubber happens to be.
3. **No sense of when.** The line gives a date but no time of day and no position, such as
   *2 of 6*. It can't show that Mira_P was on from 5:00 to 6:15 during BlockyFox's Oct 9
   session. That shows only as a faint band of a similar colour on the same 6 px rail, and the
   log-in and log-out glyphs don't say whose they are.
4. **The current session is derived from the scrub time.** Scrub past a session's end and **‹ ›**
   step from a different session than the one the line names.
5. **Small and ambiguous targets.** The name is 28 px tall and relies on a hit extension. At the
   ends, **‹** or **›** is just disabled, with no *oldest* or *newest*.
6. **Hidden presets, and two meanings of "Session".** **Session · Today · Week · All** and the
   player chips are one drag below the working height. The **Session** preset means *the live
   session now*, while the stepper's session means any session.

### The alternatives

All three move **Follow** into the transport row, beside the clock. Follow now follows the loaded
session's player, so player choice is no longer a separate control. Loading a session sets the
range to that session plus 2 minutes either side, puts the scrubber at its start, and centres the
map there.

- **1 · List.** A full-width **session button** replaces the stepper. It shows the player's
  disc, *BlockyFox · Yesterday*, *4:10–7:52 pm · 3h 42m · ☠ 1* and **Sessions**. Tapping it, or a
  player's **Sessions** button on the map, opens the shared Sheet:
  - a pinned header with **All players** and one chip per player, each with a count;
  - below it, the sessions newest first, grouped by day (*Today · Sat Oct 10 — 3 sessions*), in
    DESIGN's micro-row card;
  - each row gives the start–end time, *on now*, the duration and distance, and the player's
    name under *All players*, with a portal glyph for a Nether trip and a skull count;
  - the loaded session is highlighted and scrolled into view;
  - a tap loads that session and closes the sheet.

  *All players* interleaves everyone, which is where you see who played together.
  - **For:** the clearest and most scalable. It reads like DESIGN's chats picker, and it works
    the same for a week or a year of history.
  - **Against:** choosing is modal, and the sheet covers the map. Outside the sheet, *when*
    relative to everything else is still invisible, as it was in rev 2.
- **2 · Lanes.** The scrubber becomes **one lane per shown player** over the range, with each
  session as a bar in the player's colour and death ticks on the bars.
  - The loaded session's lane expands to 44 px. When the range is a day or less, its deaths and
    portal trips become the tappable glyphs from the old marker row.
  - A playhead runs through every lane down to the thumb. A tick row gives days, or hours.
  - Tap a bar to load that session. Drag the lanes to move the range; pinch or use the wheel to
    zoom.
  - Bars whose 44 px targets would overlap merge into a count bar, like map clusters. A tap zooms
    in until they separate.
  - **Session · Today · Week · All** sit above the lanes when the phone is tall enough, and one
    drag down at 320 × 568.
  - **For:** the only one that shows **concurrency**. Load BlockyFox's Oct 9 session and Mira_P's
    5:00–6:15 bar sits under it. It is also a true overview of the week.
  - **Against:**
    - At *Week* on a phone, the 44 px floor forces clustering. BlockyFox's lane reads *3* and
      *2* rather than five bars, so picking a session there takes two taps.
    - Bars an hour long are slivers. Four lanes cost about 125 px.
    - Zooming back out needs the range control (one drag down at 320 × 568) or a pinch.
    - It grows with the number of players.
- **3 · Days.** A strip of the days since the first session (**Sun 4 … Today 10**) replaces the
  stepper. It is edge to edge, with a coloured dot for each player who played that day. Under it
  is **that day's sessions as chips**: player disc, start time, and duration or *on now*.
  - Tap a day: the range becomes that day's activity, the scrubber shows its bands, and no chip
    is pressed.
  - Tap a chip to load that session.
  - The strip follows the scrubber, so a *Week* replay walks the selection across the days.
  - A player's **Sessions** button opens alternative 1's list, filtered to that player. That is
    the route to anything older than the strip.
  - **For:** always visible, no modal. Any session in the week is two taps away (day, then chip).
    The dots answer *when did people play* at a glance. Every target is at least 44 px, and it
    fits 320 × 568 with the scrubber in view.
  - **Against:**
    - The strip covers about a week. Older history needs paging or the list.
    - At 320 px three chips fill the row; a fourth session in one day would wrap or need to
      scroll.
    - Concurrency shows only as bands on the scrubber.

**Map left visible** (dark, px), from the audit:

| Phone | Rev 2 timeline | 1 · List | 2 · Lanes | 3 · Days |
|---|---|---|---|---|
| 390 × 844 | 488 | 484 | 397 (451 in the replay) | 458 |
| 320 × 568 | 216 | 212 | 188 (236 in the replay) | 188 |

Lanes and Days both reach the panel's cap at 320 × 568, so the map holds at the floor (the same
188 px as rev 2's timeline with a tapped death). In each, the working set (transport, the
picker, the scrubber) stays in view.

### Recommendation

**3 · Days, with 1's list behind it.** That hybrid is what the mock's alternative 3 already does.
The owner's world has four players and a handful of sessions a day. At that scale the day strip
answers *when*, the chips answer *which*, and nothing is modal. The list covers what the strip
can't: every session ever, filtered by player, reached from a player's **Sessions** button. In
the build it would also be reached from a paged strip.

Lanes are the most informative picture, and they answer *who was on together*. But on a phone
the 44 px floor turns a week into clusters, so they are a worse **picker** than the strip. If
concurrency matters, the cheap win is to keep 3 and draw the day's sessions as thin per-player
lanes inside the scrubber track (display only). That is a build-time refinement, not another
round.

## What changed since the review (revision 2)

| Review finding | Revision 2 |
|---|---|
| F1 Surface was the default layer | **Biomes** (the hillshaded biome atlas) is the only live layer and the default everywhere, chat card included. Surface and Height are listed, tagged *later*. Colours are the renderer's `BIOME_COLORS`. |
| F2 snapshots, the *Rendering* state | Gone. The status says the map is **live, up to a minute behind**. The Rendering state is replaced by **Zoomed out · 1 px/chunk**. |
| F3 zoom floor 1/8 | **1 px per chunk** (zoom 4) is the floor; up to 8 px/block client-side. The scale bar reads *1,024 blocks · 1 px = 1 chunk* there. |
| F4 tile URL | Corrected below (Facts). |
| F5 dark means unexplored | Replaced by the **satellite survey**: nothing is dark. Explored ground is full colour, the rest is the seed's prediction, dimmed. Fog of war is an optional overlay. |
| F6 four slots, *last played playing now* | Five slots, the empty one disabled. *Slot 2 · loaded · being played now*. |
| X1 / A1 / B3 status says Live while replaying | In time mode the line is **Not live · yesterday 4:48 pm**, in `--warn-text`, with **Back to live**. The audit checks it in every time-mode view. |
| X2 / A2 / B1 map and timeline at 320 × 568 | A: one panel whose working height is stepper, transport and scrubber; the map keeps 188–216 px; floating controls hide below 230 px of map. B: bars collapse to one row, the dock is capped at 50 %. C: the tray is capped at 52 %. |
| X3 no clustering, label collisions | Markers within 40 px cluster into a count badge; a tap zooms until they separate. Labels are clamped inside the visible map, kept clear of floating controls and the scale bar, or dropped. The tapped thing's label always wins. |
| X4 the chat card | Biomes, a legend (*Explored · Satellite survey* and the glyphs) and a scale bar. The tool-view carve-out is set out under *Decisions to approve with the pick*. |
| B2 callout covers the map | With the timeline open, or when it would cover half the map, the callout docks as a row along the bottom of the map. |
| B4 pressed Key at 1.3 : 1 | Pressed FABs paint the tint over `--surface` (`color-mix`), never over the canvas. |
| C1 no status line | The readout and the tray open with the status line. |
| C2 bespoke right-edge drawer | Replaced by the shared bottom **Sheet**. |
| C3 time mode drops the tapped thing | The crosshair's world point is held when the tray resizes the map; the time-mode readout shows the snapped marker's title; a snapped marker gets the selection ring and the crosshair steps aside. |

The review's non-blocking notes are also done: theme follows the system; type is on the
token scale (`--fs-chat` is 20 px); raw colours are tokens (two map-only additions, below);
stepper values use non-breaking spaces; the live view fits the online players; loading tiles are
shimmer-hatched, with their words drawn above the overlays; login and logout marks on the scrubber
are glyphs, not hairlines; A keeps the time state when you switch to Layers.

## Facts this round is built on

Owner decisions of 2026-10-10 and 2026-10-11, and the shipped renderer. They supersede the first
round's assumptions.

- **The map is a satellite survey.** In the server's lore, the AI connection comes from an AI
  satellite in orbit. Ground nobody has explored is not dark. It is drawn from the seed's
  prediction (`jbrain-predict`) **half-desaturated at 65 % brightness**: the survey
  (`SURVEY_SATURATION`, `SURVEY_BRIGHTNESS`). That holds in **all three dimensions**. Explored
  ground, meaning chunks the server has generated, is drawn in full colour on top. In the owner's
  words: *"I still want to show explored vs unexplored, but I want generated areas to still be
  pannable"*. So the whole world pans, and the legend reads **Explored · Satellite survey**.
  - The End's void is transparent. The mock shows `--map-void` with a faint starfield.
  - The prediction matches the real world at about 97 % of columns, and every miss is on a biome
    border.
- **Explored by &lt;player&gt;** is a separate, optional overlay. It is the travel log's fog of war
  (`mc_player_explored`, 4-chunk rule, first-seen time per player): a veil over the chunks the
  chosen player (or *anyone*) hasn't been near. On the timeline it lifts by first-seen time. The
  base tiles always show today's terrain, so the time-mode status says *terrain as of now*.
- **Tiles** are 256 × 256 px. Zoom 0 is 1 px per block; zoom *k* is 1 px per 2^*k* blocks, and
  pixels are point samples. Zoom 4, **1 px per chunk**, is the furthest out, at the owner's
  explicit ask. Zooming in past 1 px/block is client-side nearest-neighbour. Tile (0, 0) starts at
  block (0, 0) at every zoom.
  - Route: `GET /api/minecraft/map/tile/{dim}/{zoom}/{tx}/{tz}.png?slot=slotN`, with `dim` one of
    `overworld | nether | the_end`.
  - `GET /api/minecraft/map/info` gives `extent`, `tile_blocks`, `max_zoom`, `layers` (`biome`)
    and `predicted`.
- **The base layer** is the hillshaded biome atlas (`render_biome` / `render_sampled`), the only
  layer the server renders. True-colour Surface and Height come later.
- **No snapshots.** The renderer reads the live LevelDB at most every 60 s, and the tile route
  caches 60 s, so the map trails the world by up to a minute. Status copy:
  - live: *Live · 2 on — up to a minute behind the world · positions live*;
  - time mode: *Not live · yesterday 4:48 pm — replaying at 600× · positions from the travel log
    · terrain as of now*;
  - stopped: *Server stopped at 1:12 pm — map as of then*.
- **Tap a spot** for what the sidecar's `/map/point` returns: biome, Y (`~Y` for the survey's
  estimate) and **Explored** or **Satellite survey (predicted)**, with **Copy X Z**.
- **Structures** are exact, from the server's own `locate structure`:
  - Overworld: village, mansion, monument, pillager outpost, ancient city, trail ruins, trial
    chambers, stronghold, temple, ocean ruins, shipwreck, buried treasure, ruined portal,
    mineshaft;
  - Nether: fortress, bastion remnant, ruined portal;
  - End: End city.

  They are a **Structures** overlay, with Lucide-outline icons on an amber-ringed tile and
  clustering when zoomed out. `locate` gives no Y (`y?`), so a structure shows X Z only.
- **World slots: 5.** The empty one is listed, disabled.

## Shared fixture (identical in all three)

**World:** **Castle Hill** (slot 2, loaded). The procedural world is a stand-in for the box's two
sources, the prediction and the real chunks. It covers every block, so you can pan anywhere. The
explored part is the ground within 6 chunks (view distance) of every travel-log sample, plus
spawn, about 1,000 blocks across. It holds:

- the village around X 232 Z −304;
- the owner's castle on its hill at X −140 Z 90;
- the portal at X −40 Z 40;
- spawn at X 32 Z −8;
- the ocean to the south-east.

**Structures:** 19 in the Overworld, from the village next door to a woodland mansion about
4,600 blocks out, plus 4 in the Nether and 2 End cities. At 1 px per chunk most of them cluster.

The **Nether** has explored chunks around BlockyFox's trip: portal, fortress and the lava death.
**Nobody has been to the End**, so all of it is survey: the central island, the void ring, and
outer islands from about 1,000 blocks out.

**Now:** Sat Oct 10, 2:40 pm. The map is live, up to a minute behind.

| Player | Colour (token) | On now | Bed | Deaths in the log |
|---|---|---|---|---|
| BlockyFox | `--steel` | yes, since 1:58 pm, heading north | in the castle | fell from a high place (Oct 6, the mountains); killed by a Zombie (Oct 7); burned in lava (Oct 9, the Nether) |
| Mira_P | `--green` | yes, since 2:33 pm, exploring north-east | in the village | killed by a Creeper (Oct 8) |
| Steve42 | `--amber` | no, last seen yesterday 8:14 pm | — | drowned at the shipwreck (Oct 9) |
| Pebble_J | `--violet` | no, last seen Oct 8, 6:40 pm | — | killed by a Zombie (Oct 8) |

- **Travel log (T1):** 17 sessions from Oct 4 to now. Revision 3 added four to round 1's 13, so
  some days have two sessions from one player: BlockyFox on Oct 7 at 10:20 am, Pebble_J on Oct 5,
  Steve42 on Oct 7 and Mira_P on the morning of Oct 10. None of the four has a death. The longest is
  **BlockyFox, Fri Oct 9, 4:10–7:52 pm (3h 42m)**. He goes castle → portal → Nether fortress,
  dies in lava, respawns at his bed, then goes to the shipwreck, the village and home.
- **Saved places:** Castle keep, Nether portal, Mira's farm and Wreck dive in the Overworld; Blaze
  farm and Portal home in the Nether.
- **Other slots:**
  - **World** (slot 1, unplayed): 31 chunks explored around spawn, and the survey everywhere else.
  - **Creative test** (slot 3): a small explored patch.
  - **Skyblock run** (slot 4): never loaded, so there is no ground and no seed to survey.
  - **Slot 5:** empty.
- **Dave's chat:** *"where did I die yesterday?"*. The card shows the Nether death at X 41 Z −58,
  with *Overworld side: X 328 · Z −464*.

| State (mock controls) | What it shows |
|---|---|
| Entry | The Minecraft screen with the way in: a **Map** row in A and B, the **map card** in C. |
| Live · default | Fitted to the online players. Explored ground in colour over the survey; players (live dots pulse, offline ones hollow), trails, deaths, beds and spawn, saved places, structures. |
| Loading tiles | Tiles arrive nearest-first. Missing ones are shimmer-hatched squares marked *loading…*, drawn above the overlays. Status: *Loading map tiles · N of M*. Overlays still draw, because they come from the API. |
| Zoomed out · 1 px/chunk | The furthest zoom: about 6,000 blocks across the phone, the explored world a small bright patch, structures clustered into count badges. |
| Unplayed world | **World**: a small explored patch at spawn in a world of survey you can pan. *Hardly played*. |
| Server stopped | *Server stopped at 1:12 pm — map as of then*. Every player is hollow, *offline*, where they logged off. |
| Box offline | Only the tiles this phone saved are drawn; the rest are hatched, *not saved on this phone*. *Box unreachable — retrying…*. Pan and zoom still work. |
| Timeline · this session | M8a, scrubbed to 2:21 pm today: *Not live · today 2:21 pm*, dots with fading tails, event glyphs on the scrubber, the session stepper first, then the transport. |
| Long session replay | BlockyFox's 3h 42m session at **600×** with **Follow** on. The view jumps into the Nether and back. A toast says *BlockyFox went to the Nether*, then *…died and respawned in the Overworld*. |
| Chat card | The P3 tool-view in a Minecraft_Dave reply: Biomes, legend and scale bar. One finger scrolls the chat; a tap on the map expands it. |

## Behaviour common to all three

- **One map component.** A canvas that pans with one finger, pinch-zooms with two, and zooms with
  the wheel or + / −, from 8 px per block out to 1 px per chunk. `touch-action: none` on the full
  map only. A scale bar with **N↑** is always drawn, north (−Z) up, and states blocks per pixel
  once a pixel is two blocks or more.
- **Base layer:** **Biomes**, explored + satellite survey. **Surface** and **Height** are listed,
  disabled, *later*.
- **Overlays**, each a switch:
  - Players;
  - Trails;
  - Deaths;
  - Beds & spawn;
  - Saved places;
  - **Structures** (on by default);
  - **Explored by…**, off by default, with *Anyone* or one player;
  - Grid & coordinates.

  **Wormhole gates** (M12) is listed, *later*.
- **Clustering.** Point markers within 40 px merge into a count badge (rose-ringed when all are
  deaths). A tap zooms in until they separate, and markers on the very same block open the first.
  Players never cluster. Labels go in this order:
  1. the tapped thing;
  2. players;
  3. places;
  4. spawn;
  5. structures.

  A label that would collide with another label, a floating control or the scale bar is dropped,
  never drawn under it.
- **Dimension switch:** Overworld / Nether / End. The Nether shows its Overworld equivalent (×8)
  beside every coordinate.
- **Tap anything for its coordinates:**
  - a skull gives the cause and when, with **Replay the minutes before** (the timeline 3 minutes
    earlier, 10×, Follow on);
  - a player gives *online · here now* or *logged off here*, with **Follow** and **Sessions**;
  - a structure gives *Located by the server* and whether its ground is explored or survey;
  - a spot gives biome, Y and **Explored** or **Satellite survey (predicted)**.

  Every one has **Copy X Z**.
- **A legend:**
  - the *Explored · Satellite survey* pair;
  - the dimension's biome swatches;
  - the veil when Explored-by is on;
  - the glyphs, including the structure tile and a cluster badge.
- **Honest status, always visible.** In the live states the line names the map's age, *up to a
  minute behind*, and whether positions are live. In time mode it leads with **Not live** and
  the scrubbed time, in `--warn-text`. It never says Live while scrubbing or replaying.
- **Timeline (M8a), session-first:**
  - in A, the session picker set by **Session nav** (*Session navigation*, above). B and C keep
    revision 2's **stepper** *‹ BlockyFox · Oct 9 · 3h 42m · 2.6 km ›* with **Follow**;
  - then **play/pause**, **speed** (1× / 10× / 60× / 600×, one tap cycles), the time and
    **Now/Live**;
  - then the **scrubber**: a 28 px thumb on a 44 px track, coloured session bands, and event
    glyphs (skull, portal, log-in, log-out) that cluster below 44 px apart;
  - then the range presets **Session · Today · Week · All** and the player chips, one drag
    further.
- **The chat card (P3):**
  - a header (*Castle Hill · Nether · Biomes*) with an **expand** button;
  - a 220 px map with a scale bar;
  - the tapped item with Copy X Z;
  - overlay chips: Trail, Deaths, Places, Structures;
  - a one-line legend.

  **One finger scrolls the chat** (`touch-action: pan-y`) and two fingers pinch the card. A
  marker shows its coordinates, and a tap anywhere else opens the full map at that spot.
- **Any world slot:** the picker lists all five; only the loaded one is live.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-full-bleed.html` | *All map.* The map fills the phone. One persistent bottom panel holds status, layers, the timeline and the tapped item. Long-press drops a pin you can nudge. |
| **B** | `b-docked-bars.html` | *Fixed chrome, nothing over the map.* Bars on top, a docked timeline that pushes the map up, callouts on the map. |
| **C** | `c-card-drawer.html` | *A card that grows, aimed with a crosshair.* The map is a card on the Minecraft screen (the chat card's twin). Full screen, a fixed crosshair reads out what's under it. |

### A — full-bleed map, one persistent panel (recommended)

- **Floating on the map:**
  - back, top left;
  - the dimension segments, top right;
  - zoom in, zoom out and *show everyone*.

  Below 380 px of visible map the + / − go (pinch remains); below 230 px the segments and
  *show everyone* go too. Back always stays.
- **The panel** is a persistent bottom panel, not a modal: the graph Map's
  "Google-Maps style" precedent in DESIGN. It has three heights, set by dragging its top edge or
  with its chevron:
  - **peek**: world, the **Layers / Timeline** tabs, the status line;
  - **mid**: the layer grid, or the stepper, transport and scrubber;
  - **open**: 80 %, adding the overlays, legend, presets and chips.

  The map always re-centres in the part the panel leaves.
- **Time mode survives the tabs.** Open Layers mid-replay to switch on *Explored by* and watch
  the veil lift; the status line keeps saying *Not live* and offers **Back to live**.
- **What you tapped** sits above the tabs. In time mode it is a single row (icon, title,
  X · Z, Copy, close), so the stepper and scrubber keep their room.
- **Long-press** drops a pin, glides to at least 4 px/block so the block is visible, and
  offers **Nudge one block** (W / N / S / E). This is C's precise aiming, as a gesture.
- **Measured** (dark, map px left visible):

  | Phone | Live | Timeline | Timeline + tapped death |
  |---|---|---|---|
  | 390 × 844 | 723 | 488 | 416 |
  | 320 × 568 | 442 | 216 | 188 |

### B — docked bars, callouts on the map

- **Top:**
  - a bar: back, the world picker and a *2 on* badge;
  - the dimension segments and **Layers** (icon-only under 360 px);
  - a status strip: *Live · 2 on · up to a minute behind…*.
- **On the map:** **Key** (the legend card) and zoom. A tap opens a **callout** anchored to the
  thing. It docks as a bottom row instead when it would cover half the map.
- **The timeline is docked** and pushes the map up. Opening it collapses the top to **one
  56 px bar** (back, world, Layers; the dimension moves into the Layers sheet), and the status
  line moves into the dock's head, where it reads *Not live · …*. The dock is capped at 50 % and
  scrolls inside: stepper and transport first.
- **For:** nothing covers the map uninvited; the most predictable layout.
- **Against:** about 140 px of permanent chrome when idle. In time mode the dock's cap means the
  presets and chips are a scroll away. Callouts in the live view still hide neighbours.

### C — map card, crosshair readout, shared sheet

- **On the Minecraft screen:** a **Map** card with live dots, *Live · up to a minute behind*,
  **Open map** and **Replay today**.
- **Full screen:**
  - a top bar with back, *Castle Hill / Overworld · Biomes* (opens the sheet), **Timeline** and
    **Layers**;
  - a **fixed centre crosshair**;
  - a readout under the map. It starts with the status line, then describes what the crosshair
    is on. A marker within 24 px snaps it, gets the selection ring, and the crosshair steps aside.
- **World, dimension, layers and who's shown** live in the shared bottom **Sheet**. The
  right-edge drawer is gone.
- **The timeline tray** is session-first and capped at 52 %.
- **For:** the most precise way to get coordinates, and one component across the Minecraft
  screen, chat and full screen.
- **Against:**
  - the crosshair is a mode to learn;
  - the readout costs about 200 px at all times;
  - layers are two taps away;
  - the card lengthens the Minecraft screen and pushes **Update** down.

## Render check (2026-10-11)

**Revision 3** was rerun on the same rig: 390 × 844, 320 × 844 and 320 × 568, dark and light.

- A ran under each of the four **Session nav** settings. The three alternatives each covered all
  10 states and A's deep views, plus their own: the list open, *All players*, a row loaded, a
  player's **Sessions**; Week and All lanes, a lane bar, a cluster bar; a day, a chip, a day
  during a replay. Rev 2's stepper covered the time-mode views.
- B and C were rerun in full, because the fixture changed.

That made 870 views, all screenshotted. The comparison shots were looked at, in particular the
same paused moment (Fri Oct 9, 5:05 pm, mid-replay) in every alternative. The results:

- no text below 4.5 : 1;
- no target under 44 px (lane bars carry a 44 px hit extension);
- no overflow or truncation;
- no console errors;
- no time-mode status saying Live.

The self-test still caught the injected label and button. One finding was fixed along the way:
the list's screen-reader *Nether* text had inherited the glyph's violet, at 2.5 : 1.

Revision 2's check follows.

Headless Chromium (Playwright, `/opt/pw-browsers`) ran every variant at **390 × 844,
320 × 844 and 320 × 568**, in **dark and light**, with the page's colour scheme set so the
system-theme default was checked too. It covered:

- all 10 states;
- each variant's deep views:
  - a death, a player, a spot, a survey spot and a structure tapped;
  - a long-press pin (A);
  - the Explored-by overlay;
  - the Nether and the End;
  - the world picker and Skyblock;
  - A's open panel, B's Layers and Key, and C's sheet with and without the timeline;
  - a tapped scrubber death;
  - Layers during a replay;
  - a cluster tap (A);
  - expanding from the chat card.

That is 438 audited views, each screenshotted and the representative ones looked at. The audit
script was first shown to catch an injected low-contrast label and a 20 px button.

- **Contrast:** every DOM text clears **4.5 : 1** in both themes. Disabled controls and DESIGN's
  `.sect` headers are excluded, as before. Canvas text sits on `--map-plate`; canvas glyphs are
  `--map-ink` on `--map-halo`.
- **Tap targets:** every control is ≥ 44 px.
- **Overflow:** nothing runs past the phone or truncates.
- **Console:** no errors or warnings.
- **Honest status:** in every time-mode view, at every size and theme, the status line does not
  claim Live.

The per-view map heights are in A's table above. B at 320 × 568 keeps 227 px with the timeline
open (it had 0). C keeps 216 px (it had 92).

## Recommendation

**A.** It now carries the review's conditions:

- the status line is honest in time mode;
- the stepper, transport and scrubber share a 320 × 568 phone with about 200 px of map;
- markers cluster;
- C's precise aiming is there as a long-press pin.

A remains the only layout whose worst case degrades gradually, and it keeps every modal on the
shared shells.

- Keep **C's rule for the inline chat card**: one finger scrolls, two pinch, a tap opens. It now
  has the legend and scale bar P3 asks for.
- Choose **B** if nothing may ever float over the map. It now works at 320 × 568, at the cost of
  scrolling the dock for presets and chips.
- Choose **C** if precise coordinates matter more than the timeline. Note that A's long-press pin
  now covers most of that ground.

## Decisions to approve with the pick

These need the owner's sign-off, and a DESIGN extension in the implementing PR:

1. **`minecraft_map` tool-view carve-out.** DESIGN refuses map tiles in tool-views except the
   location domain's on-box proxy, and says *no other domain gets a tile view without its own
   decision*. Proposal: the P3 card is a registered `minecraft_map` view. The model fills only
   data slots (`slot`, `dim`, centre, zoom, overlay toggles, the item it is about) and authors
   **no URL**. The component builds tile sources itself from the on-box
   `/api/minecraft/map/tile` route. The plan's catalog had `minecraft_map` returning an image
   artifact; this replaces that with the live component.
2. **Map-only tokens:**
   - `--map-void`;
   - `--map-fog` (the Explored-by veil);
   - `--map-halo`;
   - `--map-plate`;
   - `--map-ink` and `--map-ink-on` (glyph ink and the dark initial on a player disc).

   The void, veil, halo and ink stay dark-on-light in both themes because they are part of the
   picture; only the plate follows the theme. Biome colours are the renderer's table, raw RGB
   inside the image, like other canvas content.
3. **Absolute-size exemption.** Add the map component's class prefix (`.mc-map*`) to DESIGN's
   list of canvas-drawing views whose sizes do not scale with the text-size setting. This covers
   the canvas and its floating chrome only; the panel, sheet and chat card use type tokens.
4. **A's panel is persistent, not a `<Sheet>`.** This is the graph Map's precedent. The world
   picker and B/C's layer surfaces use the shared Sheet.

## Assumptions still to confirm in M8

- **Structures index.** The overlay assumes the sidecar keeps the results of a `locate` sweep per
  world, so the markers draw even while the server is stopped. **Built in viewer wave 1**
  (2026-10-11): the sidecar saves a grid sweep per world folder and refreshes it when stale
  (plan §M8, *Viewer, wave 1*).
- **Fair play.** Survey-only structures are an answer *beyond the explored edge*, which the plan
  gates by fair play for players. The owner's PWA sees everything; the chat card's Structures
  chip should follow the asker's tier. **Settled in viewer wave 3** (2026-10-11): the card's
  tool is PWA-only and never offered in game, so its asker is always the owner and the chip
  shows every located structure (DESIGN.md "`minecraft_map` tool-view").
- **The offline cache** is per device: the tiles last viewed. How much to keep is open.
- **Copy X Z** copies `X Z` as two numbers. Markers know Y (structures don't), so *Copy X Y Z*
  for markers is a cheap later addition for `/tp`.
- **Not covered:** the true-colour Surface and Height layers' legends, `changes` and `slice@Y`
  (fair play), share links for players, and real gate data (M12).

## Known issues in this round

- The procedural world is a stand-in. Its biome patchwork at 1 px/chunk is busier than a real
  seed's, and the explored area matches the prediction exactly, where the real world differs at
  about 3 % of columns.
- At 320 px in time mode the status line drops its second line (*Replaying at 600× · positions
  from the travel log · terrain as of now*) to keep one row. The play/pause button carries the
  replaying-or-paused state there.
- In A at 320 × 568 with the timeline open, the dimension switch is hidden, along with the other
  floating controls. Lowering the panel brings it back.
- With a tapped item, the timeline and the range set to *All*, about 190 px of map is the
  minimum.
- In B at 320 × 568, a tapped death's full callout docks along the bottom and leaves about
  110 px of map above it. That is B's cost of keeping the detail on the map rather than in a
  panel.
- The live dots' pulse redraws the canvas every frame while visible. The build should pause it
  when the tab is hidden.

Decision: the owner picked **A** with session navigation **3 · Days** (2026-10-11). The four
decisions above were approved with the pick.
