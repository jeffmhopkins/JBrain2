# Independent review — Minecraft item icons (d16e7d8c)

Passed everywhere: true 16×16 RGBA, hard alpha (0 semi-transparent px), top-left light, no console errors, no overflow, text ≥ 4.5:1, theme follows system, reduced-motion freezes glint, item_texture keys/paths match the probe kit.

## Facts that supersede the README
- F1 (blocking, Replicator): a block's item draws from its DEFAULT permutation and breaking drops the plain block item — a permutation can never put the iron front in a slot, and canon says it keeps programming when picked up. Choose: (a) custom placer item (block_placer + replace_block_item) with name/lore carrying the target and a flat item sprite; or (b) one block id per target.
- F2: canon allows hundreds of targets; per-target fronts don't scale. Use one generic "programmed" front (target only in tooltip), or a capped curated list.
- F3: the plan has no Replicator section; say the icon is ahead of its spec.
- F4: furnace-like block needs minecraft:placement_direction (cardinal) + transformation permutations; check in game which face the GUI cube shows; bottom face falls back to side (grille shows underneath).
- F5: plan §M10 says "an Eye of Ender look with a glow" — reconcile in the build PR.

## Cross-variant
Blocking:
1. F1/F2.
2. Replicator top draws eight EYES (green squares), not the variant's own Power Pack; in the iso slot view it's the dominant face and reads as a black/green checkerboard at 1×. Draw each variant's pack in the cells; drop cell borders to face colour.
3. Blank vs programmed Replicator indistinguishable at 1× (front is the half-width left face at .82 brightness). Put the programmed signal on the TOP (centre cell lights, frame dim→lit).
Non-blocking:
- Fixed up-arrow on Tricorder screens fights M9 (texture can't follow bearing) and reads as a plus at 1×; use a non-directional signal glyph or Dave's lens.
- Tooltips are Java style (purple border, namespaced id line, "(16)"); Bedrock shows neither.
- "2× GUI scale" is actually 1.75× (1.45× under 380 px).
- Glint was toned down vs Bedrock's strong purple sweep; show it honestly (it's also a cheap differentiator from a loose eye).
- Vanilla stand-ins are rough; check the pick in game vs real Eye/Pearl at GUI 2 and 3.
- Flat replicator.png / replicator_iron.png are mush; draw a proper sprite if (a).
- Footprint: A's Tricorder fills x1–14,y0–15; vanilla sits ~2–13. Trim a pixel.

## A — Salvaged copper
- A1 blocking: F1/F2, cross 2–3 (blank/iron identical at 1× night).
- A2: Power Pack passes the hotbar test best (orange caps break the round silhouette).
- A3: Tricorder reads as old TV/radio — a device; glyph is a plus; "verdigris bloom" is just 2 px — show it or drop the claim.
- A4: strongest canon fit (copper/amethyst/tuff). Tricorder is beamed down NEW, so keep its copper bright; "salvaged" fits the Replicator.
- A5: Replicator clearly not vanilla; tuff side flat on light panel, copper frame saves it.
- A6: cohesive.

## B — Deep dark
- B1 blocking: worst Replicator — near-black, blank vs iron ~4 px.
- B2 blocking: Tricorder reads as a SOUL LANTERN (glowing cyan cap over dark body).
- B3: Pack only distinguishable by 4 cyan tip px; weakest on light sky.
- B4: sculk "took" the Old Makers in that proposal — Dave's eye of sculk contradicts it; warden look for a kids' guide object.
- B5: sinks on dark panel/night.

## C — Ship's quartz
- C1 blocking: F1/F2; red side lights make blank vs programmed most distinguishable — idea worth carrying over.
- C2 blocking: Power Pack reads as a bloodshot EYEBALL; white stack count loses its edge.
- C3 blocking: Tricorder reads as a FIRST-AID KIT (white case, red plus); lid floats (transparent notch row 4).
- C4: good materials; "new" fits a beamed-down Tricorder.
- C5: Replicator best on dark, worst on light.

## Recommendation
A, with conditions: resolve F1/F2 and redraw the inventory Iron Replicator to match; programmed visible from the top (C's lights idea); redraw A's top with A's own capped cells and calm the grid; replace the Tricorder up-arrow with a non-directional glyph (optionally B's lens in amethyst on the copper face, not a lantern cap); add F3/F4/F5; re-shoot with realistic glint and Bedrock tooltips; check against real Eye/Pearl in game. The owner picks.
