# wreck-pack — the spawn wreck

A behavior pack that builds the crash site at a new world's spawn, **once**: the crew's
escape pod in a crater, fitted to whatever ground is there, with the damaged mothership
hanging above it (plan §SW1–SW4). It gives a new world its opening scene. Dave's welcome
(in [`../jbrain-pack/`](../jbrain-pack/README.md)) refers to the ship overhead and the
pod's barrels when a world has a wreck, and tells a different story when it doesn't.

Stable `@minecraft/server` 2.0.0 only, so it runs on vanilla worlds.

## Which worlds get it

**Only a world the box generated itself.** The wrapper arms a slot when it creates or
resets one, unless the owner says no. It installs the pack before BDS first generates
that world, so the build is in place when the world first loads. An **imported or restored
world is never armed**: the wreck must never land on a world the family has already
built on. The pack is taken back out of any world that isn't armed.
`test_minecraft_wreck.py` pins all of this.

The pack guards itself too. A world that has already run for a while with no wreck
record isn't new, so the pack records it as passed over and builds nothing.

## How the build runs

1. **Survey.** A ticking area loads the ground round spawn (128 blocks each way, at most
   9 × 9 chunks), and the pack reads its heights and surface.
2. **Plan** (`scripts/plan.js`, pure). Picks a site within 24 blocks of spawn: the
   flattest dry footprint, then the driest, then the nearest. With no dry footprint
   anywhere, the pod splashes down at spawn. Then lays out the pod's frame, the crater and skid mark in
   scorched versions of the local ground, the parachute canopy, the hatch cover, the
   spawn point and the starter kit. The mothership goes over the pod, shifted sideways
   and raised as high as it needs to clear the peaks.
3. **Place** (`scripts/main.js`). Digs the crater, then places the pod's structure, then
   the ship's 64 × 64 column tiles, one step at a time.

Progress lives in a world dynamic property: the stage, each finished step, and the ground
as first read. A build cut short by a crash or a restart resumes at the step it stopped
on, from the original heights, so nothing is dug or placed twice. A finished build never
runs again. A build that keeps failing is given up rather than left half-trying.

Waits are measured on the clock and never spin. A job that yields in a loop gets the rest
of every tick, and on the box that once starved the world to a tick every few seconds.

The pack reports its progress as `[jbrain] {"ev": "wreck", "phase": …}` lines (pod, ship,
done, or done with a cause when it was passed over). The wrapper records them on the slot,
and the welcome finds out from `jb:config` whether there's a crash site.

## Where the builds come from

| Path | What |
| --- | --- |
| `spikes/minecraft-spawn-wreck/` | The design spike. `gen_pod.py` and `gen_battleship.py` generate the builds; the owner signed off on the look from its renders. |
| `../wreck/pod.json.gz`, `../wreck/mothership.json.gz` | The builds as compact block lists, written by the spike's `export.py`. **These are what's checked in.** |
| `../structures.py` | At image build, `python3 structures.py build wreck wreck-pack` turns them into `.mcstructure` files under `structures/` and the metadata module `scripts/data.js`. |
| `structures/`, `scripts/data.js` | Generated, so git-ignored (`.gitignore`). They exist only in the built image or a test's temp directory. |

To change the look, change the spike's generators, re-export into `../wreck/`, and rebuild.
The structure files are never edited by hand. The pod's interior is explicit air, which
clears terrain inside the hull. The ship's surroundings are "leave what's there", so the
ship never overwrites a hill.

## Testing

`supervisor/tests/test_minecraft_wreck.py` covers this pack. It runs as part of the
`supervisor` gate, or on its own:

```sh
(cd supervisor && uv run pytest tests/test_minecraft_wreck.py)
```

- **Structure files:** the NBT byte layout and index order (a mis-ordered index scrambles
  the whole build), that every bed has its other half, and that the mothership can't burn
  or blow up its own magazine.
- **Arming:** new and reset worlds are armed, imports and restores never are, and a test
  world stays marked as one only until the owner takes it over.
- **`../wreck-sim/plan.test.mjs`:** `plan.js`'s pure rules under `node --test`.
- **`../wreck-sim/sim.mjs`:** the real `main.js` against synthetic worlds
  (`../wreck-sim/fake_server.mjs`), one for each kind of ground a spawn can land on:
  plains, forest, jungle, desert, snow, mountainside, badlands, swamps, mangrove, island,
  ocean, a high plateau, and peaks under the ship. Plus the hard cases: a placement that
  fails once, a reload mid-build, a run stopped dead at several points (it must resume
  cleanly), a slow, stuck server, and a world that has already been played. The fake is as
  strict as the game where that has caused bugs: with nobody online, only chunks inside a
  ticking area can be read or changed.

By hand, against a built pack: `node sim.mjs PACK_DIR WRECK_DATA_DIR [scenario ...]`
(see the header of `../wreck-sim/sim.mjs`).

What only a real server shows (how it looks, how long it takes on the box) is checked on
the box. See plan §SW2–SW4.
