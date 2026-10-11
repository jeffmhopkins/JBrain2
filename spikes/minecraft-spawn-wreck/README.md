# Spawn wreck: a crashed escape pod under a damaged mothership

**Design spike, not production.** It answers one question before any world-generation
work: *what do the two builds look like?* The owner signed off on the look from the
preview renders in `renders/`. Nothing here is wired into the `minecraft` sidecar, the
`jbrain` behavior pack, or world creation yet; that is a separate, planned change.

## The two builds

- **Escape pod (`gen_pod.py`)**: a small capsule, about 14 × 11 × 10 blocks and ~480 blocks
  in all, lying on its side at the world origin.
  - **Hull:** a charred heat shield dug into a small crater, a white and grey hull with an
    orange band, and a docking collar with a beacon lamp.
  - **Inside:** a flat walkable deck behind a blown 2 × 2 hatch, with a bed, crafting table,
    furnace, barrels, a console and a ceiling light. It works as a starter shelter.
  - **Around it:** the hatch cover in the grass, a short skid mark, and the parachute dragged
    out flat in orange and white carpet panels, with lines back to the collar.
- **Mothership (`gen_battleship.py`)**: a Battlestar-style carrier hanging over spawn, about
  210 × 65 × 80 blocks and ~56k blocks (hollow shell plus interior), at y ≈ 164–228.
  - **Exterior:** dark gunmetal, slab-sided and axis-aligned. It has an armoured prow, a
    stepped command tower with a glass bridge, and twin turrets trained out to both
    broadsides. Each flank carries a flight pod with a lit hangar slot. Seven blue engines
    are all lit, and the navigation lights are red to port and green to starboard.
  - **Damage, but still flying:** five breaches open onto lit decks, one turret has its guns
    shot away, and there are scorch marks and drifting debris. Campfires sit in the wounds;
    8 of them stand on hidden hay bales, which makes them signal fires with taller smoke.
  - **Interior:** about 18k blocks, connected by ladders.
    - **Tower:** bridge and combat centre.
    - **Engine block:** an engine room around a reactor column, with catwalks.
    - **Main deck:** the corridor, crew cabins, mess, medbay and armoury.
    - **Upper deck:** hydroponics garden, captain's quarters, briefing room and workshop.
    - **Prow:** fire control, with a TNT magazine below it.
    - **Flight pods:** hangar control booths, a ready room and a maintenance bay.
    - **Lower deck:** the cargo hold.

Rooms are only ever built in cells strictly inside the hull volume. `gen_battleship.py`
prints an audit (`shell cells missing after interiors: 0`) proving the plating has no new
holes. The deliberate exterior exceptions are the bridge's glass band, the nav lights and the
campfires.

## Running it

```
./build.sh                       # generate both builds + a few previews
./build.sh hero_bow,in_bridge    # pick views (names: the "shots" keys in the scene JSON)
RESX=1920 SAMPLES=96 ./build.sh hero_bow
```

- **Needs:** `python3` with numpy and Pillow, plus Blender (`apt install blender`) for the
  renders. All of it is opt-in: nothing here runs in dev-setup or CI.
- **Textures:** `fetch_textures.sh` pulls Mojang's vanilla block textures from
  `github.com/Mojang/bedrock-samples` into `~/.cache/jbrain-wreck`. They are used locally for
  previews and are **never committed**.

## What the renders are not

They are previews of block placement, not of the game.
- **Rough shapes:** beds, anvils, lanterns, ladders and campfires are drawn as boxes or flat
  panels, not the game's real models.
- **Smoke:** the grey columns stand in for campfire particles.
- **Lighting:** it's Cycles path tracing, not Minecraft's lighting.
- **Ground:** the distant grass is a flat plane.

## Output format

`gen_*.py` write JSON voxel lists: `[[x, y, z, "block_id"], ...]` in Minecraft axes (x east,
y up, z south). Block ids are vanilla Bedrock names, with a few render-only stand-ins for
oriented blocks (`ladder_n/s/e/w`, `bed_head/bed_feet`, `iron_trapdoor_flat`). Turning these
into `.mcstructure` files with the right block states is the first step of the integration
plan.
