# Independent review: the Minecraft map viewer mocks (M8, M8a, P3)

> **Status:** Living · **Last verified:** 2026-10-11
>
> An adversarial review of `a-full-bleed.html`, `b-docked-bars.html`, `c-card-drawer.html` and
> `README.md` in this folder, written by a reviewer who did not build them (PROCESS.md, "GUI
> gate" and "Delegation"). It is measured against `docs/reference/DESIGN.md`,
> `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §T1, §M8, §M8a and P3, and the renderer that has
> shipped (`deploy/minecraft/mapping.py`, `backend/src/jbrain/api/minecraft.py`). Nothing in
> the mocks was changed.

## How it was checked

- **Rendering.** Headless Chromium (Playwright, `/opt/pw-browsers`) at **390×844 and 320×844**,
  dark and light. That is all 10 states, plus these deep views:
  - taps on a death, a spot, a player and the Nether, outside and inside the timeline;
  - the session stepper, the next-player tap, a scrubber marker and *All*;
  - the replay after 2.5 s of play;
  - the world picker, Skyblock, the End and the Nether;
  - every variant's layer surface and the Explored layer;
  - B's Key and dock, and C's drawer with and without the timeline;
  - the chat card, scrolled and expanded.

  That makes 376 screenshots, each one looked at.
- **Real 320 px phone.** A 320-wide phone is **320×568** (iPhone SE 1st gen; small Androids are
  about 320×640), not 320×844. The README's render check only used 844 tall. The live, timeline
  and replay states were re-shot at 320×568.
- **Interactions and audit.**
  - The interactions were driven with real CDP touch events: one-finger pan on the full map, and
    vertical and horizontal one-finger drags on the chat card.
  - Every button was clicked, and the zoom was taken to both limits.
  - A DOM audit measured hit areas (including `::after` overlays), contrast token-to-surface,
    overflow, truncation and console errors. It was first shown to catch an injected 1.6:1
    label and a 20 px button.
  - Controls that float over the canvas were checked separately, because the picture is their
    real background.
- **Screenshots** are in the reviewer's session scratchpad (`…/scratchpad/map-review/`, named
  `{variant}-{width}-{theme}-{view}.png`). They are not committed.

**What passed everywhere:**
- no console errors or warnings;
- no horizontal page overflow;
- every DOM control ≥ 44 px, including chips and scrubber markers;
- all DOM text ≥ 4.5:1 in both themes, except B's pressed **Key** (below);
- one-finger drag pans the full map and never scrolls the page (`touch-action: none` holds);
- in the chat, a one-finger drag on the card scrolls the chat, and neither a vertical nor a
  horizontal drag opens the full map (`pan-y` holds);
- reduced motion is honoured.

The README's own audit claims hold *at 320×844*. The failures below are mostly what the audit
could not see: canvas text, real phone heights, and whether the words are true.

## Facts that supersede the README

The renderer has shipped, and these mocks were built on assumptions it contradicts. Each of these
is **blocking for all three variants**, because the owner would be choosing between pictures and
states the product cannot produce.

| # | The mocks / README say | What is true | Where it shows | Fix |
|---|---|---|---|---|
| F1 | Default base layer is **Surface** (true-colour top block: roofs, farms, paths, a village), with Biomes, Height, Explored as siblings. | The only base layer the server renders is a **hillshaded biome atlas** (`mapping.py` `render_biome`). Surface and Height are not built. | Every map view, every variant; the chat card header reads *Nether · Surface*; C's title reads *Overworld · Surface*. | Draw the fixture as the biome atlas (flat `BIOME_COLORS`, hillshaded NW, water darkened by depth) and make **Biomes the default and only live layer**. List Surface and Height with the *later* tag that Structures already uses. The legend becomes biome names. In the Nether the atlas shows biomes, not lava lakes or a fortress, so drop those from the picture. |
| F2 | *Map from the 2:30 pm snapshot*: tiles are rebuilt from a snapshot taken every 30 min. A **Rendering** state hatches *new since 2:30 pm* chunks (*Rendering 23 new chunks…*). | The renderer reads the live LevelDB while BDS runs and re-reads it at most every 60 s (`REREAD_S`), and the tile route caches 60 s. **The map trails the running world by up to a minute.** There are no snapshots and no rendering backlog. | The status copy in every state of every variant; the whole **Rendering** state. | Replace the snapshot wording with the map's real age: *Map updated 2:39 pm · positions live*, or *Map can be a minute behind*. Delete the Rendering state. In its place, show a live dot drawn over not-yet-drawn ground, which is the honest version of that minute. A stopped server reads *Map as of 1:12 pm, when the server stopped*. |
| F3 | Zoom runs **8 px/block → 1 px per 8 blocks** (`clamp(s, .125, 8)` in all three). The pyramid is z−1…z−3, built by averaging. | Zoom 0 = 1 px/block, up to **zoom 4 = 1 px per chunk** (4096 blocks a tile). **The owner explicitly asked for that furthest zoom.** Zoomed-out pixels are point samples, not averages. Zoom-in past 1 px/block is client-side nearest-neighbour (the mocks do that correctly). | The zoom-out limit in every variant; the README's "Assumptions → Tiles". | Set the floor to 1/16 px/block and show it in the mock: the whole fixture becomes a 64×64 px patch, so the empty world around it is part of the picture. The scale bar must read *1 chunk/px* or *2048 blocks* there. |
| F4 | Tile URL `/{slot}/{dim}/{z}/{tx}/{tz}.png`; dimensions `o/n/e`. | `GET /api/minecraft/map/tile/{dim}/{zoom}/{tx}/{tz}.png?slot=slotN` with `overworld \| nether \| the_end`, plus `GET /map/info` for the extent. Tile (0, 0) starts at block (0, 0) at every zoom. | README "Assumptions". | Correct the README. Note that `map/info`'s `extent` is what *fit*, *show everyone* and the empty-world state should use. |
| F5 | *Unexplored chunks are dark on every layer*, and *dark means nobody has been there*. | Transparent tile pixels mean **not generated**. The server generates everything within view distance (5–32 chunks) of a player, which is far more than the 4-chunk *explored* rule in `mc_player_explored`. So the base layer shows land nobody has "explored"; only the **Explored** overlay is fog of war. **An unplayed world is nearly empty:** just the spawn area. | Copy in the Unexplored state (all three) and the Layers note; the fog lifting during a replay is drawn on the base layer too. | Treat three regions as distinct: *not generated* (the void), *generated, not explored* (the base map) and *explored by X* (the overlay). Say *dark means the world hasn't been generated there yet*. Keep the timeline's fog-lift on the Explored overlay only, because the base tiles show today's terrain at every scrubbed time. Then say so while scrubbing: *terrain as of now*. |
| F6 | README: *the world picker lists all five slots*. | Its fixture lists four (no empty slot 5). The A and B picker also reads *loaded · last played playing now*. | A and B's world sheet, C's drawer. | Add the empty slot as disabled (*empty*), and fix the copy. |

## Cross-variant findings

### Blocking

1. **The status line lies during time travel (A and B).**
   - `statusInfo()` ignores the timeline. While scrubbed to 2:21 pm, while stepping to Wed Oct 7,
     and while replaying yesterday at 600×, A's peek and B's strip both read *Live · 2 on … positions
     live*.
   - Seen in A and B, timeline / replay / tl-next-sess, both widths, both themes.
   - This breaks DESIGN principle 5, and it is the exact status the README recommends borrowing
     from B.
   - **Fix:** in time mode the line becomes *Replaying Fri Oct 9 · 4:29 pm — not live*, with
     **Back to live**, in `--warn-text`.
2. **The map and the timeline do not coexist on a real 320 px phone.** Measured at 320×568:
   - **A:** the map keeps 227 px, minus the three FABs and the segmented dimensions over it. The
     **session stepper is below the sheet's mid height**, so stepping sessions (an M8a must) needs
     the sheet at *open*, which leaves about 110 px of map.
   - **B:** the docked timeline takes **all** of the map (0 px visible). Key and zoom then float
     over the play button and **Now**.
   - **C:** 92 px of map (74 px in replay).

   **Fix,** for whichever is chosen:
   - one compact timeline block (stepper row, transport and scrubber in about 190 px) at the
     working height;
   - presets and chips one drag further;
   - floating controls hidden below about 200 px of map;
   - and the gate re-run at 320×568.
3. **The 1 px-per-chunk zoom needs marker clustering.**
   - At the current floor (1/8), the 46 px hit circles, skulls, beds and places already pile into a
     single blob over the 1,000-block world (`*-zoomed-out-max.png`). At the owner's floor (1/16)
     every marker overlaps.
   - The README's *labels never overlap* is false today:
     - *Fortress* sits on the tapped skull (chat card, A's Nether tap);
     - offline labels collide with the scale bar (A, offline, 320);
     - canvas labels clip at the edges (*le keep*, *Sh*, *M*), and under the FABs (*Nether po*).
   - **Fix:**
     - cluster markers by screen distance into a count badge that zooms in on tap;
     - clamp labels inside the map rect, clear of the floating controls;
     - and let a tapped marker's own label win.
4. **The P3 inline card needs its own DESIGN decision.**
   - DESIGN "Agent tool views" refuses map tiles in tool-views. The location carve-out ends *No
     other domain gets a tile view without its own decision*.
   - The plan's tool catalog also has `minecraft_map` returning **an image**, while the mock's card
     is a live tile component.
   - **Fix:** the README must say the card is a registered `minecraft_map` view whose
     tiles come only from the on-box `/api/minecraft/map/tile` route (no model-authored URL), and
     record that carve-out for the owner to approve with the pick.
   - Also add the legend P3 requires (*a legend for the layers*): the card has none, and no scale
     bar either (`scale: false`).

### Non-blocking

- **Theme ignores the system.** All three hard-code `data-theme="dark"`, so with
  `prefers-color-scheme: light` they still open dark. DESIGN: default follows the system. Read
  `matchMedia` on load.
- **Type off the scale.** Raw sizes are 10.5, 11, 11.5, 12.5, 13 and 15 px, against DESIGN's
  12/14/16/18/22/28.
  - The mocks draw at 100%, but the app defaults to 75%, so 10.5 px chips and stepper meta render
    near 8 px. `--fs-chat` is copied as 15 px; the token is 20 px.
  - **Fix:** use `--fs-micro`/`--fs-head`/`--fs-secondary`. Add the map class prefix to the
    absolute-size exemption (`.loc-map*`, `.tv-wx*`…) for canvas-adjacent chrome only.
- **Raw colour.** `.det-ic.pl{color:#0E0F11}` (all three); C has `#fff` and `rgba(8,9,11,.85)` in
  CSS; B has `rgba(0,0,0,.25)`. Use tokens. The proposed `--map-*` tokens are reasonable.
- **Stepper meta wraps mid-unit.** *Sat Oct 10 · 42 / min · 0.8 km* (B and C at 320). Use
  non-breaking spaces inside each value.
- **Default framing misses a live player.** At 320, Mira_P (online) is off-screen in A's and B's
  Live state. Fit the live players with `map/info`'s extent on open.
- **Loading tiles and the void look almost alike in dark.** The *loading…* text is overdrawn by
  trails and clipped at tile edges. Draw the placeholder text above overlays, or use a shimmer.
- **Copy X Z only copies X Z.** Y is known for every marker, and `/tp` wants `X Y Z`. Consider
  **Copy X Y Z** for markers and `X ~ Z` for spots.
- **Event ticks are 1 px hairlines.** Join and leave ticks above the rail read as noise at 320.
  Give them a glyph, or drop them from the *All* and *Week* ranges.

## Variant A — full-bleed map, one bottom sheet

**Blocking**
- **A1.** The status line lies in time mode (cross-variant 1).
  - Seen in timeline, replay and tl-next-sess, at 390 and 320, in both themes.
  - Fix as above, and in the peek, where B's line was meant to go.
- **A2.** The session stepper is unreachable at the timeline's working height (cross-variant 2).
  - At 320×844 mid, the stepper and the chips are below the fold. At 320×568 the map is
    227 px with the stepper hidden.
  - With a death tapped plus the timeline, A's sheet leaves about 270 px of map at 320×844 mid
    (about 150 px at *open*), and the tapped skull is not on screen (`a-320-*-tl-tap-death`).
  - **Fix:**
    - put the stepper in the *mid* content above the scrubber;
    - render the tapped item as a one-line row (cause · coords · Copy) in time mode instead of
      the full detail card;
    - and recentre the map on the selection within the area left above the sheet.

**Non-blocking**
- **Switching to Layers silently ends the replay.**
  - Tapping the **Layers** tab sets the map back to live. Picking the Explored layer to
    watch the fog lift, the thing M8a is for, therefore loses your place.
  - **Fix:** keep the time state across tabs, and show the time-mode status line on both.
- **The top of the map is a stretch.** Back, dimensions and the three zoom FABs are all in the top
  third. Zoom has pinch, so drop **+ / −** on touch, or move *show everyone* to the sheet edge.
- At *open* (80%) the floating dimension segments and FABs cover the remaining strip of map
  entirely (`a-320-*-layers-open`). Hide them above about 60% sheet height.
- The world sheet reads *last played playing now* (F6).

## Variant B — docked bars, callouts on the map

**Blocking**
- **B1.** At 320×568 with the timeline open, **the map is 0 px**. Key and zoom are drawn over the
  play button and **Now**, so they hijack the transport's taps.
  - Seen in timeline and replay, dark.
  - B's thesis, "nothing covers the map", inverts: the chrome *is* the screen.
  - **Fix:** collapse the bars to one 56 px row while the timeline is open (world · dims as a
    menu · layers icon), and cap the dock at 50% of the phone with internal scroll.
- **B2.** With the timeline open at 320×844, a tapped item's **callout covers the whole remaining
  map** (≈250 px).
  - Its arrow points at a skull that is off the visible map, and the callout hides the scale bar
    and the zoom out.
  - Seen in tl-tap-death at 320, both themes; in Live at 320 it also hides **−**.
  - **Fix:** when the callout would cover over 50% of the map, dock it as a row above the
    timeline instead.
- **B3.** The status strip lies in time mode (cross-variant 1).
  - The dock header's *back to live* is not enough when the strip directly above says
    *positions live*.
- **B4.** The pressed **Key** FAB uses the 13% `--steel-tint` with no opaque base, directly on
  the canvas.
  - Over the light-theme void, `--steel-text` on it measures about 1.3:1 (`b-320-light-key`).
  - **Fix:** `.fab[aria-pressed]` keeps `--surface` under the tint (a layered background), as
    every other floating control does.

**Non-blocking**
- About 140 px of permanent top chrome grows to 170 px when the strip wraps to three lines at 320
  (offline, rendering, unexplored).
  - Cap the strip at one line, with the detail on tap.
- All the top controls (world, dims, layers, Key) are out of thumb reach. That is acceptable
  only because they are infrequent.
- At 320 the **Layers** button goes icon-only, as designed. Its accessible name still says
  *Surface* (F1).

## Variant C — map card, side drawer, crosshair readout

**Blocking**
- **C1.** There is **no status line in the normal states.**
  - The `c-strip` renders only for loading, stopped, offline, unexplored, the End and empty.
    Live and the timeline say nothing about live versus not, or the map's age.
  - This breaks DESIGN principle 5 ("surfaced persistently … never hidden behind a tap"), and
    the README's *honest status* claims it for all three.
  - **Fix:** add the one-line status to the readout bar. In time mode, use the cross-variant 1
    wording.
- **C2.** **The side drawer is a bespoke modal.**
  - DESIGN "Modal system": one shared `<Sheet>` and one `<Dialog>`, and *building a bespoke
    modal is a design-doc violation*. The drawer brings its own scrim, focus and swipe-to-close.
  - On Android, a drawer opened from the right edge and swiped right sits against the system
    back gesture.
  - **Fix:** use the shared bottom Sheet (as B's Layers does), or put a DESIGN extension in front
    of the owner with the pick.
- **C3.** **In time mode the readout drops what you tapped.**
  - A death tapped while scrubbing reads *Forest · X 150 Z −200*: no cause, no *Replay the
    minutes before*.
  - The crosshair is not on the skull either. The tray resizes the map after centring, so the
    centre moves (`c-320-dark-tl-tap-death`).
  - **Fix:** re-centre after any change to the map's height, and let the mini readout show the
    snapped marker's title.

**Non-blocking**
- **The canvas does not track its own box.**
  - Going from Live to Stopped left the backing store at 320×759 inside a 608 px box, so the
    picture, skulls and labels were vertically squashed until the next resize. The chat card is
    230 drawn into 228.
  - This is a mock bug, but the component spec should say *ResizeObserver*.
- **The crosshair hides the selection.** It sits on top of the snapped skull or player, so you
  cannot see what you selected (`c-390-light-tap-death`). Draw it as an open ring around a snapped
  marker.
- **Hard reach.** Timeline and Layers are both icon buttons in the top-right corner, the hardest
  reach on the phone, and changing a layer is two taps plus a drawer.
- **The card pushes Update down.** The Minecraft-screen card puts **Update** (where the binding
  screen shows the *you're behind* banner's action) below the fold at 390. Put the Map card after
  Players, or keep A/B's one-line row and save the card for chat.
- Centring on a player at the world's edge leaves half the map as void (`c-390-light-tap-player`).
  That is inherent to the fixed crosshair.

## The README's recommendation

The README recommends **A, with B's status line and C's no-one-finger-pan inline card**.

**I agree with A as the base, on conditions.** It is not ready to be picked as drawn.

- **Why A is still right.**
  - It is the only layout whose worst case degrades *gradually*: one drag trades map for
    controls.
  - It follows DESIGN's own precedent for a map: the graph Map's "persistent bottom panel (not
    the modal `<Sheet>` … Google-Maps style)".
  - It keeps every modal on the shared shells.
  - B fails outright at 320×568 (B1), and C breaks two binding DESIGN rules (C1, C2).
- **Why "with B's status line" is the wrong half of B to borrow.**
  - A already has the line in its peek. The problem is that, in both A and B, *the line
    lies* during replay (cross-variant 1).
  - Borrow the line *and fix it*. That is the condition, not a nicety.
- **Agree on C's inline-card rule.** One finger scrolls, two pinch, and a tap opens. It verified
  correctly under real touch events in all three. Add the legend and scale bar P3 requires
  (cross-variant 4).
- **Take one thing from C that the README leaves on the table.** Use C's **snap readout** as A's
  "tap a spot" result. Long-press drops a crosshair pin that can be nudged, so *the exact block to
  type* does not need a whole layout built around it.

**Before the owner chooses,** re-issue the mocks with:
- **F1–F5**, so the owner sees the biome atlas, the 1 px-per-chunk floor, and status copy that
  matches a map at most a minute old;
- **cross-variant 1–4**;
- **A1–A2;**
- and a render pass at **320×568**.

B and C can stay as the record without being fixed. If the owner prefers B or C anyway, B1–B4 or
C1–C3 become blocking for that variant.

## Revision 2 — the builder's response (2026-10-11)

> Written by the agent that built the mocks, answering each blocking finding above. The mocks were
> re-issued as revision 2, and `README.md` carries the full account. The owner settled several facts
> after this review, and they go further than F1–F6: the map is a **satellite survey**, and nothing is
> dark any more. Where those facts changed the answer, the answer says so.

### Facts F1–F6

- **F1 — fixed.** Biomes, the hillshaded atlas, is the only live layer and the default everywhere:
  the full map, the Minecraft-screen card and the chat card (*Castle Hill · Nether · Biomes*).
  - The colours are `mapping.py`'s `BIOME_COLORS`, NW hillshade, with water darkened by depth.
  - Surface and Height are listed, disabled, *later*.
  - The Nether picture shows biomes only; lava lakes and the fortress are gone from it, and the
    fortress is now a structure marker.
- **F2 — fixed.** Every snapshot phrase is gone, and so is the Rendering state.
  - The line reads *Live · 2 on — up to a minute behind the world · positions live*.
  - Stopped reads *Server stopped at 1:12 pm — map as of then*.
  - The Rendering slot in the mock controls now shows the 1 px/chunk zoom (F3).
  - The suggested live dot over not-yet-drawn ground was not added: with the survey there is no
    undrawn ground, only ground not yet explored.
- **F3 — fixed.** The zoom runs 1/16 to 8 px/block (`S_MIN`, `S_MAX`).
  - Tiles are 256 px, and zoom *k* tiles point-sample every 2^*k* blocks.
  - At the floor the scale bar reads *1,024 blocks · 1 px = 1 chunk*, and the *Zoomed out* state
    shows it.
- **F4 — fixed** in the README: the routes, `dim` names, `map/info` fields and the tile origin.
  - The live view now opens fitted to the online players.
  - It uses the fixture, not `extent`, because with the survey the drawable world is unbounded.
    `extent` still bounds *explored*.
- **F5 — superseded, then fixed.** The owner chose the satellite survey, so the three regions are
  now different:
  - **explored** (generated chunks): full colour;
  - **survey** (the seed's prediction): half-desaturated at 65 %, in all three dimensions;
  - **void** (the End's emptiness only): transparent.

  The travel log's fog of war survives as an optional **Explored by…** overlay, a veil that lifts
  by first-seen time on the timeline. The base tiles never change with time, and the time-mode
  status says *terrain as of now*.
- **F6 — fixed.** The picker lists five slots, and slot 5 is *empty* and disabled. The loaded one
  reads *Slot 2 · loaded · being played now*.

### Cross-variant blocking

1. **The status line lies in time mode — fixed in all three.**
   - `statusInfo()` checks the timeline first. Whenever the scrubbed time is not now, the line
     leads with **Not live · yesterday 4:48 pm**, in `--warn-text`. Its second line is *Replaying
     at 600×* or *Paused*, then *positions from the travel log · terrain as of now*.
   - It updates as the replay plays.
   - A offers **Back to live** beside it when you are on the Layers tab.
   - B moves the line into the dock's head; C puts it at the top of the tray.
   - The render audit now fails any time-mode view whose status says Live without *Not live*. It
     found none in 72 time-mode views (12 views × 3 sizes × 2 themes).
2. **Map and timeline at 320 × 568 — fixed.** Re-measured, as map pixels left visible:
   - **A:**
     - the timeline's working height is the stepper, the transport and the scrubber;
     - presets and chips are one drag further;
     - the panel's mid height is capped to leave at least 190 px of map;
     - floating controls hide below 230 px of map.

     Timeline: 216 px. Timeline with a tapped death: 188 px.
   - **B:**
     - the bars collapse to one 56 px row while the dock is open;
     - the dock is capped at 50 % and scrolls inside, stepper first;
     - Key and zoom hide below 220 px of map.

     Timeline: 227 px (was 0).
   - **C:** the tray is capped at 52 % and scrolls, stepper first. Timeline: 216 px
     (was 92).
3. **Clustering — fixed.**
   - Point markers (structures, deaths, beds, spawn, places, log-in and log-out marks) within
     40 px merge into a count badge. A tap glides in until they separate; markers on the same
     block open the first.
   - Players never cluster.
   - Labels are clamped inside the visible map, the part not under the panel. They are tested
     against the floating controls (read from the DOM each frame), the scale bar and each other,
     and a label that collides is dropped.
   - A tapped marker's label goes first and wins: *Mira_P died here* now shows where *Fortress*
     used to sit on it.
4. **The P3 card's DESIGN decision — set out for the owner, not decided here.**
   - README "Decisions to approve with the pick" item 1 proposes the carve-out. The card is a
     registered `minecraft_map` view whose data slots carry no URL, and whose tiles come only
     from the on-box `/api/minecraft/map/tile` route. It replaces the catalog's image-returning
     `minecraft_map`.
   - The card now has a legend (*Explored · Satellite survey*, death, place, structure) and a
     scale bar.

### Variant A

- **A1 — fixed** (cross-variant 1).
- **A2 — fixed.**
  - The stepper is the first thing in *mid*.
  - In time mode the tapped item is a single row: icon, title, X · Z, an icon-only Copy, and close.
  - The map re-centres in the area the panel leaves. The centre accounts for the panel's height
    and the floating controls, so a tapped scrubber death lands mid-map above the panel
    (`a-320x568-*-tl-death`).
- The non-blocking notes are done:
  - time mode survives switching to Layers;
  - + / − hide below 380 px of map;
  - controls hide when the panel is high;
  - *last played playing now* is gone.

### Variant B

- **B1 — fixed** (cross-variant 2). Key and zoom can no longer land on the transport, because they
  hide below 220 px of map and the dock never takes more than half the phone.
- **B2 — fixed.** The callout docks as a row along the bottom of the map when the timeline is open,
  or when it would cover more than half the map. The map's centre then accounts for it.
- **B3 — fixed** (cross-variant 1). With the dock open, the strip is hidden and the line lives in
  the dock's head, so nothing above it says *positions live*.
- **B4 — fixed.** Pressed FABs paint `color-mix(--steel 13%, --surface)`: an opaque surface, so
  the canvas never shows through.

### Variant C

- **C1 — fixed.** The status line is the first line of the readout, and of the tray in time mode.
- **C2 — fixed by replacing it.** World, dimension, layers and *who's shown* now open in the
  shared bottom Sheet: its scrim, grip, swipe-down and Escape. There is no right-edge drawer, so
  there is no clash with the Android back gesture.
- **C3 — fixed.**
  - `renderBottom()` holds the world point under the crosshair across the tray's resize.
  - The time-mode readout is the single-row form, with the snapped marker's title.
  - A snapped marker gets the selection ring and the DOM crosshair steps aside, which covers the
    non-blocking *crosshair hides the selection*.

### Not done, with reasons

- **C's reach, and Update below the card on the Minecraft screen.** These are unchanged. C is not
  the recommendation, and A's long-press pin now carries C's precise aiming.
- **Copy X Y Z for markers.** The owner's facts for this round name **Copy X Z**, so it stays.
  README "Assumptions" lists it as a cheap later addition.
- **The live dot pulse redraws every frame.** It is fine for a mock, and noted in README "Known
  issues" for the build.

### Re-run

The render audit ran on 2026-10-11 at **390 × 844, 320 × 844 and 320 × 568**, dark and light,
with the system theme driving the default:

- all 10 states plus each variant's deep views, 438 views in all;
- no contrast failures below 4.5 : 1;
- no target under 44 px;
- no overflow or truncation;
- no console errors or warnings;
- no time-mode status claiming Live.

The self-test caught an injected 1.6 : 1 label and a 20 px button. Screenshots are in the
builder's scratchpad (`…/scratchpad/map-mocks-r2/`, `{variant}-{w}x{h}-{theme}-{view}.png`), not
committed.

## Revision 3 — session navigation in A (2026-10-11)

The owner picked **A** and found its sessions *"a little hard to navigate"*. This is a builder's
note, not a review: revision 3 has not had an independent pass.

- **Diagnosis.** README *Why revision 2's stepper is hard* lists six problems:
  - one session at a time, with no overview;
  - player and session mixed in one control that cycles on tap;
  - no time of day, no position and no view of concurrency;
  - a current session derived from the scrub time;
  - a 28 px name target and dead-end arrows;
  - presets one drag down, and *Session* meaning two things.

  None of them was raised in round 1. The review checked that the stepper fitted, not that it
  navigated.
- **Change.** `a-full-bleed.html` has a mock-only **Session nav** switch: Rev 2, 1 · List,
  2 · Lanes and 3 · Days. The fixture grew from 13 to 17 sessions, so some days have two
  sessions from one player.
  - In `core.js` the timeline is split into transport, track, marks, ends and range pieces, with
    a shared `loadSession()`. B and C render the same as before.
- **Recommendation.** 3 · Days, with 1's list behind a player's **Sessions** button. The reasons
  are in README *Recommendation* under *Session navigation*.
- **Re-run.** 870 views; clean on contrast, targets, overflow, console and honest status
  (README *Render check*). Screenshots are in `…/scratchpad/map-mocks-r3/`, named
  `a{nav}-{w}x{h}-{theme}-{view}.png`, and are not committed.
- **Worth an independent look** before the pick is binding:
  - the lanes' cluster rule at *Week*;
  - whether the day strip needs paging in the build;
  - the 188 px map floor that Lanes and Days reach at 320 × 568 with the timeline open.
