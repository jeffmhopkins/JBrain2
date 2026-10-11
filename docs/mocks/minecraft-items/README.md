# GUI gate — icons for the three Old Maker items (M9, M10, DW1, the Replicator)

> **Status:** Living · **Last verified:** 2026-10-11
>
> **Open: the owner picks.** These are three variants, A, B and C. Each is a whole set: the
> Tricorder, the Power Pack, and the Replicator, blank and programmed. No variant is chosen yet.
> Story source: Bleepbloop_City `notes/lore.md` ("Dave, the crew and the Tricorder" and "The
> Replicator"). Behaviour source: `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M9, §M10 and §DW1.

The items get final icons in place of the probe kit's placeholders
(`deploy/minecraft/probe-rp/textures/items/{tricorder,power_pack}.png`). The placeholder
Tricorder is a grey slab with green bars, and the placeholder Power Pack is close to an Eye of
Ender. Each variant gives all three items one look, so the Old Makers' tech reads as made by one
people:

- the **Tricorder** (`jbrain:tricorder`, stacks to 1, held in hand). It's how Dave sees and speaks
  to one crew member, and shows a waypoint arrow while held;
- the **Power Pack** (`jbrain:power_pack`, stacks to 16, with glint). It's crafted from nine Eyes
  of Ender. It must look related to an Eye of Ender but never be mistaken for one, or for an
  Ender Pearl, in the hotbar;
- the **Replicator**, a block: a furnace surrounded by eight Power Packs. Programmed, it becomes
  an "Iron Replicator" or similar. The variants give its inventory icon and its faces, blank and
  programmed.

## What's here

| Path | What |
|---|---|
| `a-salvaged-copper.html`, `b-deep-dark.html`, `c-ship-quartz.html` | One page per variant |
| `{a,b,c}/tricorder.png`, `power_pack.png` | The item icons, 16×16 RGBA |
| `{a,b,c}/replicator_front.png`, `replicator_front_iron.png`, `replicator_side.png`, `replicator_top.png` | The Replicator's block faces, 16×16 |
| `{a,b,c}/replicator.png`, `replicator_iron.png` | A 16×16 flat preview of the block as a slot shows it (see "Facts") |
| `vanilla/*.png` | Our own rough stand-ins for an Eye of Ender, Ender Pearl, compass, spyglass and furnace, there for contrast only. They are not Mojang textures. |
| `make_icons.py`, `make_pages.py` | Draw every PNG from pixel grids, and build the pages from them. To revise, edit a grid and run both. |

Each page:

- is self-contained and works offline, with the PNGs embedded as data URIs;
- is phone-framed: 390 px wide, 560 px on desktop, and full-bleed under 430 px. It holds at
  320 × 568;
- follows the system theme for its chrome, and the mock controls can override it;
- shows every icon at 1× (16 px), 2× and 8×, with nearest-neighbour scaling;
- shows a **hotbar** at 1× and at a phone's GUI scale, next to the vanilla stand-ins, on a day
  or a night scene. Tap a slot to select it, and its name shows above, as in game;
- shows an **inventory** on the light-grey Bedrock panel and the dark one, with stack counts
  (Power Pack 16 and 3). Tap an item for its tooltip;
- shows the **tooltips**: *BlockyFox's Tricorder*, *Power Pack*, *Replicator* and *Iron
  Replicator*;
- glints the Power Pack with an animated band. It holds still under `prefers-reduced-motion`,
  and the mock controls can pause it;
- draws the Replicator in a slot from its three faces, as the game draws a block. It also shows
  each face at 8×;
- shows the Tricorder held, with M9's actionbar arrow.

## The three variants

All three keep the Eye of Ender's green and dark pupil at the Power Pack's core, because that's
its recipe. Each variant tells it apart from a loose eye by its **silhouette and housing**. An eye
and a pearl are both round green-teal balls, so a round pack would get lost.

| Variant | File | Materials | Power Pack shape |
|---|---|---|---|
| **A — Salvaged copper** | `a-salvaged-copper.html` | Copper and verdigris, amethyst, tuff | A tall capped cell, like a battery |
| **B — Deep dark** | `b-deep-dark.html` | Deepslate, sculk-teal glow, echo shard | A diamond |
| **C — Ship's quartz** | `c-ship-quartz.html` | Quartz, gold contacts, redstone | An octagonal disc |

### A — Salvaged copper

Hardware pulled from the wreck. The canon calls copper the Old Makers' wiring, amethyst their
signal, and tuff the trial chambers' stone. The **Tricorder** is a copper handheld with an
amethyst antenna crystal and a lit amethyst screen showing the arrow. The **Power Pack** is a
copper-capped glass cell with the eye's green sealed inside. The **Replicator** is a tuff
furnace in a copper frame. Its program window is dark amethyst until it's taught, and two side
lights go from dim to lit green.

- **For:** it's the most distinct Power Pack at 1×. An orange-capped upright cell can't be read
  as a round green ball, and it says "fuel" at a glance. The materials are the most strongly
  canon (copper, amethyst, tuff). It's warm, and sits well in a survival hotbar.
- **Against:** the cell reads more "battery" than "Old Maker". At 1× the Tricorder's arrow is a
  couple of pixels and reads as "a screen". The tuff-grey Replicator is the least showy of the
  three on a grey inventory panel.

### B — Deep dark

Tech from the ancient cities: deepslate housings, sculk-teal glow and echo-shard trim. The
**Tricorder** is a slim slab under a glowing sculk lens, which is Dave's eye looking out, with a
cyan arrow on its screen. The **Power Pack** sets the eye in a deepslate-and-echo diamond with
sculk glow at its four points. The **Replicator** is deepslate tiles in an echo-shard frame,
with a teal-lit mouth and side lights that brighten when it's programmed.

- **For:** the strongest story read. A lens through which Dave sees is exactly what the canon
  says the Tricorder is. It glows the most on a night scene and the dark panel, and the four
  cyan points make the Power Pack sparkle even before its glint.
- **Against:** its Power Pack is the closest of the three to the eye's colours. The diamond and
  the cyan points carry it, so check it at 1× on the hotbar. The dark items sink on the dark
  inventory panel. The ancient cities are a "sculk took them" proposal on the lore page, not
  canon, so the look leans on an unpicked ending.

### C — Ship's quartz

The ship as it was built: pale quartz casings, gold contacts and redstone status lights, from
the lore page's ore table (gold the contacts, quartz the sensing parts, redstone the power). The
**Tricorder** is a flip-open case, its gold grille lid up, with a red arrow on its screen. The
**Power Pack** is an octagonal quartz cell, a gold-ringed eye lens with four rivets and a red
status light. The **Replicator** is polished quartz in a gold frame, and its redstone side
lights go from dark to bright red once programmed.

- **For:** it's the brightest set, and clearest on the dark panel and a night hotbar. The
  flip-open Tricorder is the most recognisably "a tricorder" of the three. The red arrow and
  lights tie straight to redstone, which players already read as "powered".
- **Against:** at 1× the Power Pack reads as a round pale disc, almost an eyeball. It doesn't
  read as an eye of ender, but nor does it read as fuel. Pale quartz loses contrast on the
  light-grey Bedrock panel. Gold-and-white is the least "ancient" of the three and looks closer
  to new.

## Render check (2026-10-11)

Every page was rendered in headless Chromium at 390 × 844 and 320 × 568, in dark and in light:

- **Overflow:** none, either from the page or from any card, hotbar or inventory.
- **Script errors:** none in the console.
- **Contrast:** every piece of page text is 4.5:1 or better. The in-game pieces are part of the
  picture and were not counted: the counts, tooltips and actionbar use Minecraft's own
  white-with-shadow text.

Telling the Power Pack apart at 1× in the hotbar, next to the eye and the pearl: A is clearest,
as an orange upright cell. C is clear, as a pale disc. B is clear by its shape and cyan points,
but it's the closest in colour. At the 2× GUI scale, all three are unmistakable.

The first render's glint was so strong it turned the pack lilac, so it was toned down. The 2×
hotbar overflowed its card, so it was sized to fit.

## Recommendation

**A, with B's Tricorder lens as an option to weigh.** A's Power Pack is the one a player can't
mix up with an Eye of Ender at hotbar size, which is the hard requirement. Its copper, amethyst
and tuff are the most canon materials. If the owner wants the Tricorder to say "Dave's eye" more
plainly, B's sculk lens can be drawn in copper and amethyst. The owner picks.

## Facts (for the build)

- **Size:** every texture is 16×16 RGBA, drawn on a 16-pixel grid with no anti-aliasing.
- **Items, in the `jbrain` resource pack:**
  - The icons go in `textures/items/tricorder.png` and `textures/items/power_pack.png`.
  - `textures/item_texture.json` maps them under the keys `jbrain_tricorder` and
    `jbrain_power_pack`, as the probe kit's `deploy/minecraft/probe-rp/textures/item_texture.json`
    already does.
  - The behavior pack's item JSON points `minecraft:icon` at those keys.
  - The Power Pack's glint is the item's `minecraft:glint`, not part of the texture.
- **The Replicator** is a block:
  - Its faces go in `textures/blocks/`, named for example `replicator_front.png`,
    `replicator_front_iron.png`, `replicator_side.png` and `replicator_top.png`.
  - `textures/terrain_texture.json` maps them, and `blocks.json` or the block's
    `minecraft:material_instances` puts them on the faces: `north` for the front, `up` for the
    top, and `*` for the sides.
  - A programmed Replicator needs its own front: a block state or permutation that swaps the
    front's material instance. Showing *what* it's programmed for, as an iron swatch, means one
    front texture per target.
  - The inventory draws a block from its faces, so the 16×16 `replicator.png` here is only a
    preview. The one way to get a flat icon instead is to give the block its own item with
    `minecraft:block_placer`, which takes an `item_texture.json` icon like the others.
- **Names:**
  - Each Tricorder is named "<Player>'s Tricorder" when Dave gives it (M9), so its tooltip
    reads, for example, *BlockyFox's Tricorder*.
  - The Power Pack is "Power Pack", and stacks to 16.
  - A blank Replicator is "Replicator", and a programmed one is, for example, "Iron Replicator".
- **Lore lines:**
  - The Tricorder's *Points where Dave says* is from M9.
  - The Power Pack's *Keeps your things, or takes you somewhere* is from M10.
  - The two Replicator lines are placeholders: *Ask Dave to program it* and *Burns any block
    into iron*.
