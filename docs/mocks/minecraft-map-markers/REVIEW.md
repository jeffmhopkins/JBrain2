# Independent review: No-Spawn Beacons and wormhole gates on the Minecraft map (M16, M12)

> **Status:** Living · **Last verified:** 2026-10-12
>
> An independent review of `a-always-drawn.html`, `b-on-tap.html` and `c-one-layer-list.html` in
> `docs/mocks/minecraft-map-markers/` (branch `feat/mc-mocks-transport-map`, commit `08ce0603`).
> The reviewer did not build them and had not seen the designer's reasoning (PROCESS.md, "GUI
> gate"). The README was read only for the brief, the marker grammar and the states. Every
> judgement below is of the rendered PNGs, measured against `docs/reference/DESIGN.md`
> ("Principles", "Map-only tokens", "Accessibility", "The Minecraft map": markers, labels,
> Layers, the panel and the chat card; "The Tricorder target on the Minecraft map"). The format
> follows `../minecraft-keep-inventory/REVIEW.md`. Nothing in the mocks was changed.

## How it was checked

- **Every PNG was viewed**: `{a,b,c}-{1..6}-*-{dark,light}.png` (390 × 844 at 2×) and
  `{a,b,c}-7-map-320x568-dark.png`, 39 in all.
- **The owner's two questions were asked of each overview** (`*-1-map-*`, `*-7-*`), without
  tapping:
  - *what's protected?*
  - *where does this gate lead?*
- **Contrast was measured** on the light PNGs: the panel words, the steel buttons and chips, and
  `--violet-text` *open* and `--ok-text` *protecting*. All are between **5.2:1 and 6.5:1**.
  Label plates are `--text` on `--map-plate` in both themes, and pass by eye. The canvas glyphs
  are `--map-ink` on `--map-halo`, high contrast at every zoom shown.
- **A PNG artefact, not a design fault.** In the light PNGs, the violet and green tints don't
  paint:
  - the gate and beacon icon tiles in the panel;
  - the *open* / *protecting* pills;
  - the green halo round the status dot.

  The glyphs float on white (`a-2-gate-light.png`, `a-4-beacon-light.png`). A headless-Chromium
  render of `a-always-drawn.html#view=gate&theme=light` paints them correctly, so it's the
  capture. Re-take the light set before the owner looks.
- **Taken as given:** the README's DOM audit (no overflow, every DOM control ≥ 44 px, no console
  errors). Canvas hit circles can't be judged from a PNG.

## Cross-variant findings

### What holds in all three

- **The grammar is right for the map.**
  - Beacons are a `--green` ring and a shield, and gates a `--violet` ring and the portal, both on
    the shared dark halo tile, so they read as the structures' cousins.
  - Violet ties gates to the scrubber's portal glyph. Green ties a beacon to *safe*.
  - The dotted gate line (1 on, 6 off) reads differently from a solid trail and from the
    Tricorder's 8 / 6 dash.
- **The panel's *what you tapped* block reads well for a parent.**
  - *Leads to **Tree farm** · 245 blocks north-west*, and **Show it in the Nether** for a
    cross-dimension gate (`*-3-gate-nether-*`).
  - *Wild and hostile mobs vanish there; tamed, named, fed and bred ones stay* for a beacon
    (`*-4-beacon-*`). It's the plainest sentence in the round.
- **Honest data source.** The Layers legend says *Positions are the pack's own records, sent
  when a beacon or gate changes and again at every start* (`a-5`, `b-5`).

### Blocking

- **X1. Gate markers cover player discs.**
  - BlockyFox's **B** disc sits half under gate 1 in every overview: `a-1`, `b-1` and `c-1` at
    about (575, 880), and the 320 shots.
  - With gate 1 selected, its white selection ring hides the disc almost completely: `a-2`,
    `b-2`, `c-2` and every chat card (`*-6-*`).
  - DESIGN puts players first in the map's order (*labels go tapped thing, players, …*; players
    *never cluster*). A parent looking for their child must never find them under a gate.
  - **Fix:** draw player discs above beacon and gate tiles, and above the selection ring, or
    offset the tile when they share a spot.
- **X2. The dormant gate doesn't read as a gate.**
  - The dormant glyph is an empty rounded rectangle in a dashed ring (`a-1` at (290, 985); the
    legend's *Gate · dormant* in `a-5` and `b-5`). At map size it reads as a **phone** or a door,
    not as the portal.
  - The owner can't tell it's BlockyFox's unopened gate without tapping.
  - **Fix:** keep the portal glyph with its swirl, unfilled. The dashed ring already carries
    *dormant*.

### Non-blocking

- **X3. Markers slide under the floating chrome.**
  - A beacon tile and its square sit under the scale bar (`a-2`, `b-2` and `c-2` at about
    (170, 940); every 320 shot at about (18, 795)).
  - In the Layers state a gate tile peeks from under the dimension segments (`a-5`, `b-5` and
    `c-5` at about (475, 135)).
  - Mira_P's squares run under the *show everyone* button at 320 (`a-7`, `c-7`).
  - DESIGN's drop rule covers labels only. Extend it to markers' visible area: nudge the camera,
    or accept the overlap but never under a control people tap.
- **X4. The chat-card fixture contradicts itself.** Dave says *BlockyFox has one gate open in
  Castle Hill… Their gate 3 goes to the Nether base* (`*-6-chat-card-*`), and Layers says *2
  open*. Fix the prose to *two gates open*.
- **X5. Breaks inside numbers and units.**
  - *X / -160* (`*-2-gate-*`), *the 3 × / 3 chunks*, and *6:12 / pm* (`*-4-beacon-*`) wrap
    mid-value.
  - The coordinates use a hyphen where the README writes a minus sign (−160).
  - **Fix:** use non-breaking spaces in the value runs.
- **X6. The legend's *Where it leads* swatch** is a violet dotted line on a **green** square
  (`a-5`, `b-5`). It borrows the protected-area colour. Use the neutral legend swatch.
- **X7. A new accent text token.** The mocks use `--violet-text` (*open*, the tile glyph), which
  DESIGN's accent-text list (`--ok-text`, `--warn-text`, `--danger-text`, `--steel-text`) doesn't
  have. It measures 6.5:1 in light, so it's sound. Add it to `tokens.css` and DESIGN with the
  pick.

## Variant A — always drawn

**Verdict: pass with changes.**

Does it answer the two questions at a glance? **Yes, both.**
- Every protected square is visible in `a-1`.
- Gate 1's destination is a pin and a label at the end of a line.
- The Nether gate carries *→ Nether base · Nether*.

**Blocking**
- **A1. The gate line runs under another gate's label, and reads as going there.**
  - In `a-1`, `a-2`, `a-6` (chat card) and `a-7` (320), gate 1's dotted line passes straight
    through the **→ Nether base · Nether** plate that belongs to gate 3.
  - The picture says *this line leads to the Nether base*, which is the wrong answer to the exact
    question A exists to answer.
  - DESIGN's label rule checks collisions with labels, controls, the panel and the scale bar, but
    not with drawn lines.
  - **Fix:** add gate lines (and the Tricorder bearing) to the label collision set, so a plate
    moves to its next slot or drops. Never draw a plate over another gate's line.

**Non-blocking**
- **A2. The tapped beacon's square looks the same as its neighbour's** (`a-4-beacon-*`). The
  panel says *overlaps 1 other beacon*, but the two dashed squares are identical. Borrow C's
  rule: fill the tapped one at 16 %.
- **A3. The squares are weakest over plains and sand.** The green dash on its halo is clear over
  forest and water (`a-1`, bottom left) and faint over light-green plains (`a-1`, the right-hand
  pair). Fill on tap, as above, is the cheap fix. Don't fill them all, because that would tint
  the map.
- **A4. The busy case isn't mocked.** The README's own *against* is three or more open gates at
  a hub fanning lines across a base. Render one hub state, or `a-7`'s density is the only
  evidence. At 320 it's already a line, two plates, three squares and five markers.
- **A5. B's arrow badge isn't needed in A**, despite the README's recommendation. A already
  separates open from dormant by a solid versus dashed ring, plus the line. A badge adds a third
  signal and more ink at 24 px. Keep it only for the state where *Wormhole gates* is on and lines
  are hidden, if that state ever exists.

## Variant B — quiet until tapped

**Verdict: fail against the brief** (clean against DESIGN).

The map is the calmest of the three and every token is right. But neither of the owner's
questions reads at a glance.

**Blocking**
- **B1. *What's protected* needs a tap or a setting.**
  - `b-1` shows three shields and no area. Whether Mira_P's base is covered can't be seen.
  - The answer is the Layers sub-choice **Areas when tapped · All areas** (`b-5`), one more
    control the owner has to know exists.
- **B2. *Where does a gate lead* isn't on the map.**
  - The arrow badge is about 14 CSS px, a violet arrow on a dark disc at the tile's corner
    (`b-1` at (497, 611) and (562, 841)).
  - It says only *leads somewhere*. It's the same badge for an Overworld place and for the
    Nether, so the owner can't even tell which gate crosses dimensions.

**Non-blocking**
- **B3. The tapped beacon's square is drawn alone** (`b-4`). The overlapping neighbour the panel
  mentions isn't drawn, so the covered union can't be seen.
- **Strength:** it scales to many gates, and it keeps trails and the Tricorder bearing legible.
  The Layers copy is good: *tap one for where it leads*.

## Variant C — one layer, listed

**Verdict: pass with changes.**

Does it answer the two questions at a glance? **Protected: yes** (outlines, `c-1`). **Gate
destination: partly.** The label names the place, but nothing shows where it is.

**Blocking**
- **C1. The destination label covers a player and a protected edge.**
  - **→ Tree farm** sits to the right of gate 1 and covers the top half of BlockyFox's **B** disc
    (`c-1`, `c-2`, `c-7`). In `c-3` it also sits on the bottom edge of Mira_P's square.
  - That breaks DESIGN's label rule (*a label that would collide with another… moves below its
    marker or is dropped*), and it compounds X1.
  - **Fix:** put player discs in the collision set for gate labels.
- **C2. The "→" reads as a bearing.**
  - On a map, *→ Tree farm* to the right of a gate says *east*, but Tree farm is 245 blocks
    **north-west** (`c-1`, as the panel in `c-2` confirms).
  - A's line shows direction. C's label suggests a wrong one.
  - **Fix:** use a non-directional lead-in (the portal glyph, or *to Tree farm*), or put the
    compass word in the plate: *Tree farm · NW*.

**Non-blocking**
- **C3. One toggle for two things.** **Beacons & gates** (`c-5`) can't show the protection
  without the gates.
- **C4. The legend is pushed below the fold.** At 80 % the *In this world* list (5+ rows)
  displaces it (`c-5`).
- **C5. The chat card uses A's grammar.** It draws the dotted line and pin (`c-6`) while C's own
  map uses labels. If C is picked, the card should match it, or the owner learns two grammars.
- **C6. *Filled when tapped* isn't visible.** The tapped beacon's square in `c-4` looks the same
  as `a-4`. Make the fill visible at 16 %.
- **Strength:** the **In this world** list (`c-5`) is the only way to reach the sky-base beacon
  (*up at Y 210*) or an off-screen gate without panning. Each row states the gate's state and
  destination (*open → Tree farm · X 30 · Z 14*). It's worth keeping whichever variant wins.

## Cross-variant comparison

| | A — always drawn | B — on tap | C — one layer, listed |
|---|---|---|---|
| *What's protected* with no tap | **Yes** (dashed squares) | **No** (B1) | **Yes** (outlines) |
| *Where does it lead* with no tap | **Yes**, line plus pin | **No** (B2) | Name only. The arrow **misleads** (C2) |
| Cross-dimension gate distinguished | Yes (plate) | No (same badge) | Yes (plate) |
| Clutter on the overview | Highest. Lines cross labels (A1) | Lowest | Medium. Labels crowd players (C1) |
| Players visible over gates | No (X1) | No (X1) | No (X1, worse with C1) |
| Dormant gate recognisable | No (X2) | No (X2) | No (X2) |
| Find an off-screen or sky marker | Pan | Pan | **List** |
| Separate toggles | Yes | Yes, plus the areas setting | No (C3) |
| 320 × 568 | Busy but usable | Clean | Usable |
| Contrast / tokens | Pass | Pass | Pass |

## Recommendation

1. **A**, as the README recommends, but with these changes:
   - **A1:** lines join the label collision set;
   - **X1:** players are drawn above gates and beacons;
   - **X2:** the dormant glyph stays a portal;
   - **C's *In this world* list** goes in the Layers tab;
   - **C's tapped-square fill** is used (A2 / A3);
   - B's arrow badge is **not** carried over (A5).

   A is the only variant that answers both of the owner's questions with no tap, and the costs
   keep gates and beacons few. Before it's binding:
   - mock one busy hub (A4);
   - fix the fixture's *one gate open* (X4);
   - add `--violet-text` to DESIGN (X7);
   - re-take the light PNGs.
2. **C**, if the owner finds A's lines too busy in play. C1 and C2 must be fixed first. C2 matters
   most: an arrow pointing the wrong way is worse than no arrow. Its chat card should drop the
   line (C5).
3. **B** last. It's a well-built map that doesn't answer the question it was built for. Its one
   idea worth keeping is the Layers copy, *tap one for where it leads*, as the help line under
   A's toggle.
