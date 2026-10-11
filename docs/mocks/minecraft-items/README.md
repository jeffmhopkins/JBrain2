# GUI gate — icons for the three Old Maker items (M9, M10, DW1, the Replicator)

> **Status:** Living · **Last verified:** 2026-10-11
>
> **Decision (binding, owner 2026-10-11): set A, with the Tricorder's A face (the ping: a
> ring round a dot on the amethyst screen).** The owner on revision 2: *"Icons look amazing!"*,
> then *"a"*. A2 (the lens) stays here as the record.
>
> - **Revision 2 took A forward.** It answers the independent review in `REVIEW.md`, which
>   recommended A with conditions.
> - B and C stay as the record of what was weighed: the review found that B's Tricorder reads
>   as a soul lantern, C's as a first-aid kit, and C's Power Pack as an eyeball.
> - Still to check on a device: the set beside a real Eye of Ender and Ender Pearl at GUI
>   scale 2 and 3, in the build that installs it.
> - The Replicator's placer-item design (below) is the coordinator's decision, and the owner can
>   overrule it.
>
> Story source: Bleepbloop_City `notes/lore.md` ("Dave, the crew and the Tricorder" and "The
> Replicator"). Behaviour source: `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M9, §M10 and §DW1.
> The Replicator's plan is §M15 (added after revision 1); the wave that builds it takes this
> page's facts in — its placer item and one generic programmed look are recorded here.

The items get final icons in place of the probe kit's placeholders
(`deploy/minecraft/probe-rp/textures/items/{tricorder,power_pack}.png`). The placeholder
Tricorder is a grey slab with green bars, and the placeholder Power Pack is close to an Eye of
Ender. Each variant gives all three items one look, so the Old Makers' tech reads as made by one
people:

- the **Tricorder** (`jbrain:tricorder`, stacks to 1, held in hand). It's how Dave sees and
  speaks to one crew member, and the actionbar shows the waypoint arrow while it's held;
- the **Power Pack** (`jbrain:power_pack`, stacks to 16, with glint). It's crafted from nine
  Eyes of Ender. It must look related to an Eye of Ender but never be mistaken for one, or for
  an Ender Pearl, in the hotbar;
- the **Replicator**: a furnace surrounded by eight Power Packs. It's a block in the world, and
  in the inventory a placer item (see "Facts"). Programmed, it's named after its target, for
  example "Iron Replicator".

## What's here

| Path | What |
|---|---|
| `a-copper-amethyst.html` | **A, revision 2**: the current page |
| `b-deep-dark.html`, `c-ship-quartz.html` | B and C, revision 1, kept as the record. Only their links to A were updated. |
| `a/tricorder.png`, `a/tricorder_lens.png` | A's Tricorder (ping) and A2's (lens), 16×16 RGBA |
| `a/power_pack.png` | A's Power Pack |
| `a/replicator.png`, `a/replicator_programmed.png` | The Replicator **item**'s flat sprite, blank and programmed |
| `a/replicator_front.png`, `a/replicator_front_programmed.png`, `a/replicator_top.png`, `a/replicator_top_programmed.png`, `a/replicator_side.png` | The placed block's faces. The side also serves as the bottom. |
| `{b,c}/*.png` | Revision 1's B and C sets, unchanged. Their `replicator.png` and `replicator_iron.png` are flat previews of the block cube. |
| `vanilla/*.png` | Our own rough stand-ins for an Eye of Ender, Ender Pearl, compass, spyglass and furnace, there for contrast only. They are not Mojang textures. |
| `make_icons.py`, `make_pages.py` | Draw every PNG from pixel grids, and build A's page from them. To revise, edit a grid and run both. B and C still draw byte-identical; their pages aren't rebuilt. |
| `REVIEW.md` | The independent review of revision 1 |

A's page:

- is self-contained and works offline, with the PNGs embedded as data URIs;
- is phone-framed: 390 px wide, 560 px on desktop, and full-bleed under 430 px. It holds at
  320 × 568;
- follows the system theme for its chrome, and the mock controls can override it. They also
  switch the Tricorder between A and A2 everywhere on the page;
- shows every icon at 1× (16 px), 2× and 8×, with nearest-neighbour scaling;
- shows a **hotbar** next to the vanilla stand-ins, on a day or a night scene. The top bar is
  true size: 16 px icons in 20 px slots. The lower bar puts 2× icons in 35 px slots, or 1.5×
  icons in 29 px slots under 380 px. The page states which, since the game's slots would be
  20 × the scale. Tap a slot to select it, and its name shows above, as in game;
- shows an **inventory** on the light-grey Bedrock panel and the dark one, with Power Pack
  counts of 16 and 3. The icons are 2×, or 1× under 360 px. Tap an item for its tooltip;
- shows **Bedrock-style tooltips**: the name and the lore line, with no item id and no count;
- glints the Power Pack with a strong purple sweep, like Bedrock's. It holds still under
  `prefers-reduced-motion`, and the mock controls can pause it;
- shows the Replicator item's flat sprite in every slot, which is what the game would draw. The
  placed block appears separately, drawn from its faces, with each face at 8×;
- shows the Tricorder held, with M9's actionbar arrow.

## A — Copper and amethyst (revision 2)

Copper is the Old Makers' wiring, amethyst their signal, and tuff the trial chambers' stone:
the most strongly canon materials of the three.

- **Tricorder.** Bright copper, because Dave beams it down new, with an amethyst antenna
  crystal. It sits in x 2–13, as vanilla items do.
  - **A, ping:** a lit amethyst screen showing a ring round a dot. It points nowhere, because
    a texture can't follow a bearing; the real arrow is in the actionbar.
  - **A2, lens:** Dave's eye, an amethyst lens set into the copper face. This is B's idea,
    redrawn on the face rather than as a glowing cap, which is what made B read as a lantern.
- **Power Pack.** Unchanged in shape: a copper-capped glass cell, with the eye's green and dark
  pupil sealed inside. Its upright, capped silhouette is what passed the review's hotbar test
  best. The teal edges of the glass are glass, not verdigris.
- **Replicator.** A tuff machine in a copper frame.
  - **Item:** a face-on sprite with three pack lights along its top, the program window and
    the barred furnace mouth.
  - **Front:** the program window is dark amethyst when blank and lit amethyst when
    programmed. The two side lights go from dim to lit green.
  - **Top:** eight of A's own capped cells stand in one tuff panel round the furnace. That's
    the recipe seen from above. Programmed, every core and the centre light up.

**Tricorder, A or A2:**

- **For A:** a screen is the plainest "device you look at", and the ping says "signal" without
  promising a direction.
- **Against A:** at 1× the ring and dot read as a lit screen, nothing more.
- **For A2:** it says what the canon says the Tricorder is: the eye Dave sees through.
- **Against A2:** at 1× it's a copper square with a purple dot, close to a compass in weight.

**The set:**

- **For:** the Power Pack is the hardest of all the candidates to mistake for an Eye of Ender
  or an Ender Pearl. The materials are canon. The programmed Replicator now differs on its
  front, its top and its item sprite.
- **Against:** the bright copper Tricorder and the copper-capped Power Pack share a palette, so
  side by side in a hotbar they read as a family. That's intended, but it's less contrast than
  between unrelated items. The tuff side is flat on the light panel; the copper frame carries
  it.

## B and C (revision 1, the record)

- **B — Deep dark:** deepslate, sculk-teal glow and echo shard. Its Tricorder is a slim slab
  under a sculk lens, and its Power Pack is the eye in a diamond. The review found:
  - the Tricorder reads as a soul lantern;
  - the Power Pack is told apart from an eye only by four cyan pixels;
  - the Replicator is near-black, and blank and programmed are about 4 px apart;
  - Dave's eye in sculk contradicts the "sculk took them" proposal.
- **C — Ship's quartz:** quartz, gold and redstone. Its Tricorder is a flip-open case, and its
  Power Pack an octagonal cell. The review found:
  - the Tricorder reads as a first-aid kit;
  - the Power Pack reads as a bloodshot eyeball.
  - Its redstone status lights, which made blank and programmed the easiest to tell apart, were
    carried over to A as the lit-green lights.

## Revision 2 — what changed since the review

| Review finding | Revision 2 |
|---|---|
| **F1** A block's item draws from its default permutation, and breaking it drops the plain block, so a permutation can't show a programmed Replicator in a slot. | Decided (coordinator; the owner can overrule): a **custom placer item**, using `minecraft:block_placer` with `replace_block_item`. The item's name and lore carry the target, and a script sets the block's state when it's placed. The item has its own **flat 16×16 sprite**, blank and programmed, and the page's slots show exactly that. |
| **F2** Canon allows hundreds of targets, so per-target fronts don't scale. | **One generic programmed look** on the item, the front and the top. The target appears only in the name and tooltip ("Iron Replicator"). The iron swatch is gone. |
| **F3** There's no Replicator plan section. | Said in the header; the plan has since gained §M15, the Replicator. |
| **F4** Facing, the GUI cube, and the bottom face. | In "Facts": `minecraft:placement_direction` with transformation permutations, a box check of which face the cube shows, and the bottom face. |
| **F5** §M10 says "an Eye of Ender look with a glow". | Reconciled: §M10 now names set A's icon. Since 2026-10-11 the pack is crafted from 8 Ender Pearls around a Blaze Powder, not Eyes of Ender. |
| Cross 2: the top drew eight eyes, a checkerboard at 1×. | A's top now has eight of A's own capped cells, with no outlines between them, on one tuff panel. |
| Cross 3: blank and programmed looked the same at 1×. | Programmed now shows on the **top**, the face a cube shows largest: every core and the centre light up. It also shows on the item sprite (the lights and the window) and on the front (the window and the side lights). |
| Up-arrow fights M9 and reads as a plus. | Replaced by a non-directional ping (A), with A2's lens offered beside it. |
| Java-style tooltips. | Bedrock style: name and lore only, with no id line and no "(16)". |
| "2× GUI scale" was really 1.75×. | The page states the real sizes, for each width. |
| Glint toned down too far. | A strong purple sweep, as in Bedrock. It also sets the pack apart from a loose eye. |
| Flat `replicator.png` was mush. | Replaced by a drawn item sprite. |
| A's Tricorder was too big: x 1–14. | Trimmed to x 2–13. |
| A3: "verdigris bloom" was 2 px. | Claim dropped. The Tricorder is bright new copper, and the pack's teal is glass. |
| A4: keep the Tricorder's copper bright. | Done. The copper is brighter across the set. |
| Vanilla stand-ins are rough. | Kept as stand-ins. The pick still needs checking in game against the real Eye of Ender and Ender Pearl, at GUI scale 2 and 3. |

## Render check (2026-10-11, revision 2)

A's page was rendered in headless Chromium at 390 × 844 and 320 × 568, in dark and in light:

- **Overflow:** none, either from the page or from any card, hotbar or inventory.
- **Script errors:** none in the console.
- **Contrast:** every piece of page text is 4.5:1 or better. The in-game pieces are part of the
  picture and were not counted.

That check caught one problem, and it's fixed: the A/A2 pair overflowed its card at 320 px.

Telling the Power Pack apart at 1× in the hotbar, next to the eye and the pearl: it reads as an
orange-capped upright cell. With the glint on, it's purple-washed as well. Blank and programmed
Replicator items differ at 1× by the lit window and the green lights.

## Recommendation

**A.** For the Tricorder, **A2** if the owner wants it to say "Dave's eye", and **A** if a
screen reads better to the kids. Before the build PR, check the pick in game against the real
Eye of Ender and Ender Pearl. The owner picks.

## Facts (for the build)

- **Size:** every texture is 16×16 RGBA, with hard alpha and no anti-aliasing.
- **Items, in the `jbrain` resource pack:**
  - The icons go in `textures/items/tricorder.png` and `textures/items/power_pack.png`.
    `textures/item_texture.json` maps them under the keys `jbrain_tricorder` and
    `jbrain_power_pack`, as the probe kit's `deploy/minecraft/probe-rp/textures/item_texture.json`
    already does.
  - The behavior pack's item JSON points `minecraft:icon` at those keys.
  - The Power Pack's glint is the item's `minecraft:glint`, not part of the texture.
  - **§M10** describes the Power Pack as "an Eye of Ender look with a glow". The chosen icon is
    a related cell, not an eye look, so the build PR updates §M10 to match.
- **The Replicator item** is a custom item, for example `jbrain:replicator`:
  - It uses `minecraft:block_placer` with `replace_block_item: true`, so it is the block's item.
  - It has a flat icon under the keys `jbrain_replicator` and `jbrain_replicator_programmed`,
    from `textures/items/replicator.png` and `textures/items/replicator_programmed.png`.
  - Its name and lore carry the target ("Iron Replicator"). Being picked up keeps the
    programming, as canon requires: the script reads the block's state when it's broken and
    gives back the named item.
  - When it's placed, a script reads the item's target and sets the block's state.
  - The programmed sprite is the same for every target.
- **The Replicator block:**
  - Its faces go in `textures/blocks/`: `replicator_front`, `replicator_front_programmed`,
    `replicator_top`, `replicator_top_programmed` and `replicator_side`.
  - `textures/terrain_texture.json` maps them, and `minecraft:material_instances` puts them on
    the faces.
  - A `programmed` state swaps the front and the top. The **bottom** uses the side texture, so
    the barred vent shows underneath; give it its own plain tuff texture if that matters.
  - **Facing:** like a furnace, it needs `minecraft:placement_direction` (`cardinal_direction`)
    and a permutation per direction, each carrying a `minecraft:transformation` rotation, so
    the front faces the player who placed it.
  - **Box check:** which face the GUI cube shows if the block item ever appears; with the
    placer item, it shouldn't. Also check that the front shows the right way round in every
    direction.
- **Names:**
  - Each Tricorder is named "<Player>'s Tricorder" when Dave gives it (M9), so its tooltip
    reads, for example, *BlockyFox's Tricorder*.
  - The Power Pack is "Power Pack", and stacks to 16.
  - A blank Replicator is "Replicator", and a programmed one is "<Target> Replicator".
- **Lore lines:**
  - The Tricorder's *Points where Dave says* is from M9.
  - The Power Pack's *Keeps your things, or takes you somewhere* is from M10.
  - The two Replicator lines are placeholders: *Ask Dave to program it* and *Burns any block
    into iron*.
