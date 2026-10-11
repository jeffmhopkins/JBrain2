// Run the wreck pack's real main.js against synthetic worlds, one per kind of ground a
// new world can spawn on, and check the crash site it builds (plan §SW2: "the capsule
// can come down in every biome"). The supervisor's test_minecraft_wreck.py runs this;
// by hand:  node sim.mjs PACK_DIR WRECK_DATA_DIR [scenario ...]
//
// PACK_DIR is a built jbrain-wreck pack (structures.py build) beside a node_modules
// whose @minecraft/server is fake_server.mjs. Prints one JSON line per scenario.
import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";
import { sim } from "@minecraft/server";

const [packDir, dataDir, ...only] = process.argv.slice(2);
const read = (name) => JSON.parse(gunzipSync(readFileSync(join(dataDir, `${name}.json.gz`))).toString());
const POD = read("pod");
const SHIP = read("mothership");
const { WRECK } = await import(pathToFileURL(resolve(packDir, "scripts/data.js")).href);

const SEA = 62;
const cellsOf = (d) => d.blocks.map(([x, y, z, i]) => [x, y, z, d.palette[i]]);
const podCells = cellsOf(POD);
const shipCells = cellsOf(SHIP);

/** A world from a height map, a surface block, trees and water up to sea level. */
function terrain({ h, top = "grass_block", sub = "dirt", cover = null, trees = () => 0, leaves = "oak_leaves", log = "oak_log" }) {
  return (x, y, z) => {
    const g = Math.floor(h(x, z));
    if (y > g) {
      const t = trees(x, z);
      if (t && y <= g + t) return `minecraft:${log}`;
      // A canopy round each trunk: leaves near the top of any tree within 2 columns.
      for (let dx = -2; dx <= 2; dx += 1) {
        for (let dz = -2; dz <= 2; dz += 1) {
          const tt = trees(x + dx, z + dz);
          if (!tt) continue;
          const gg = Math.floor(h(x + dx, z + dz));
          if (y >= gg + tt - 2 && y <= gg + tt + 1 && Math.abs(dx) + Math.abs(dz) <= 3) return `minecraft:${leaves}`;
        }
      }
      if (cover && y === g + 1 && g >= SEA) return `minecraft:${cover}`;
      return y <= SEA ? "minecraft:water" : "minecraft:air";
    }
    if (y === g) return `minecraft:${g < SEA ? "sand" : top}`;
    if (y > g - 4) return `minecraft:${sub}`;
    return y < -60 ? "minecraft:bedrock" : "minecraft:stone";
  };
}

const wave = (x, z, a) => a * (Math.sin(x * 0.11) * Math.cos(z * 0.09) + 0.6 * Math.sin((x - z) * 0.07));
const every = (n, height) => (x, z) => ((((x * 7 + z * 13) % n) + n) % n === 0 ? height : 0);

const SCENARIOS = {
  plains: { h: (x, z) => 68 + wave(x, z, 1.5) },
  forest: { h: (x, z) => 70 + wave(x, z, 2), trees: every(13, 5) },
  jungle: { h: (x, z) => 72 + wave(x, z, 3), trees: every(11, 18), log: "jungle_log", leaves: "jungle_leaves" },
  desert: { h: (x, z) => 70 + wave(x, z, 4), top: "sand", sub: "sand" },
  snowy: { h: (x, z) => 70 + wave(x, z, 2), cover: "snow_layer", trees: every(17, 6), log: "spruce_log", leaves: "spruce_leaves" },
  mountainside: { h: (x, z) => 90 + x * 0.7 + wave(x, z, 2), top: "stone", sub: "stone" },
  badlands: { h: (x, z) => 75 + wave(x, z, 3), top: "red_sand", sub: "orange_terracotta" },
  swamp: { h: (x, z) => 62.6 + wave(x, z, 1.2), trees: every(19, 6) },
  mangrove: { h: (x, z) => 63 + wave(x, z, 1), top: "mud", sub: "mud", trees: every(9, 7), log: "mangrove_log", leaves: "mangrove_leaves" },
  island: { h: (x, z) => (Math.hypot(x - 30, z) < 14 ? 66 - Math.hypot(x - 30, z) * 0.1 : 48) },
  ocean: { h: () => 45, splash: true },
  // A spawn so high the ship's signed-off height would break the build limit.
  high_plateau: { h: (x, z) => 200 + wave(x, z, 2), top: "stone", sub: "stone" },
  // The game refuses a placement once (say, a chunk not ready): reported, then retried.
  place_fails_once: { h: (x, z) => 68 + wave(x, z, 1.5), failPlaceOnce: true },
  // Spawn on a plain, with a range of peaks where the ship would hang.
  peaks_under_ship: {
    h: (x, z) => (z < -10 && z > -100 && x > -110 && x < 110 ? 230 + wave(x, z, 20) : 68 + wave(x, z, 1)),
  },
  // Built, then started again: the pack says done again, for a wrapper that missed it.
  reloaded: { h: (x, z) => 68 + wave(x, z, 1.5), loads: 2 },
  // The box's short load, every time: the ship's area never loads its last column and
  // row of chunks, and the build has to patch them in.
  short_ship_area: { h: (x, z) => 68 + wave(x, z, 1.5), shortArea: "jbrain_wreck_ship" },
  // A slow server, a tick every 3 s, and one spawn chunk that never loads.
  slow_and_stuck: { h: (x, z) => 68 + wave(x, z, 1.5), tickMs: 3000, deadChunk: "1,1" },
  // A world already played when the pack first sees it: passed over, and every later
  // start says so too, or the wrapper would record it built.
  already_played: { h: (x, z) => 68 + wave(x, z, 1.5), played: true, loads: 2 },
};

async function run(name, spec, n, stopAt = Infinity) {
  const placed = [];
  let refused = false;
  const dim = sim.reset(terrain(spec), { x: 0, z: 0 }, (id, d, loc) => {
    if (spec.failPlaceOnce && !refused) {
      refused = true;
      throw new Error("structure could not be placed");
    }
    placed.push({ id, loc });
    let cells = podCells;
    if (id !== WRECK.pod.id) {
      const t = WRECK.ship.tiles.find((tile) => tile.id === id);
      const [sx, , sz] = t.size;
      cells = shipCells
        .filter(([x, , z]) => x >= t.x && x < t.x + sx && z >= t.z && z < t.z + sz)
        .map(([x, y, z, p]) => [x - t.x, y, z - t.z, p]);
    }
    for (const [x, y, z, p] of cells) d.put(loc.x + x, loc.y + y, loc.z + z, p.name, p.states, true);
  }, { tickMs: spec.tickMs ?? 50 });
  if (spec.deadChunk) dim.dead.add(spec.deadChunk);
  if (spec.shortArea) dim.alwaysShort = spec.shortArea;
  const began = sim.now();
  // A fresh copy of main.js per world: it subscribes to worldLoad when imported.
  await import(`${pathToFileURL(resolve(packDir, "scripts/main.js")).href}?world=${n}`);
  const logs = [];
  const original = console.log;
  console.log = (line) => logs.push(String(line));
  try {
    if (stopAt !== Infinity) {
      // The server stops dead mid-build, then starts again: the second load resumes.
      sim.stopAfter(stopAt);
      sim.load();
      sim.stopAfter(Infinity);
    }
    if (spec.played) sim.setTime(1e6);
    for (let i = 0; i < (spec.loads ?? 1); i += 1) sim.load();
  } finally {
    console.log = original;
  }
  const events = logs.filter((l) => l.includes("[jbrain] ")).map((l) => JSON.parse(l.split("[jbrain] ")[1]));
  const state = JSON.parse(sim.props.get("jbrain:wreck") || "{}");
  const checks = {};
  const id = (x, y, z) => dim.idAt(x, y, z).replace("minecraft:", "");
  const open = (x, y, z) => ["air"].includes(id(x, y, z));
  const solid = (x, y, z) => !["air", "water"].includes(id(x, y, z)) && !/carpet|snow_layer/.test(id(x, y, z));

  const phases = events.map((e) => e.phase).join(",");
  // Uninterrupted: the whole story in one load. Resumed: it ends done, never failed.
  const done = events.filter((e) => e.phase === "done");
  if (spec.deadChunk) {
    // It can't build, so it must give up within its own waits, never hang.
    checks.gave_up = phases.startsWith("loaded,failed,failed,done") && done.some((e) => e.cause);
    // Three loads of at most 90 s each, and the two 30-tick-second retries between.
    checks.gave_up_in_time = sim.now() - began <= 3 * 120_000 + 2 * 600 * (spec.tickMs ?? 50);
  }
  if (spec.shortArea) checks.patched = logs.some((l) => l.includes("chunks stuck, patching"));
  // Every block edit a build tries must land; the box once lost them all silently.
  checks.writes_landed = !logs.some((l) => l.includes("not written"));
  // A build never waits with no ticking area up, or its chunks go.
  checks.never_idle = sim.idleTicks() === 0;
  const wrote = (step) => {
    const line = logs.find((l) => l.includes(`[jbrain-wreck] step ${step}: `));
    return line ? Number(line.split(": ")[1].split(" ")[0]) : 0;
  };
  // A step that does nothing at all is as wrong as one whose writes fail.
  if (!spec.played && !spec.splash && !spec.deadChunk) checks.steps_wrote = ["crater", "canopy", "details"].every((s) => wrote(s) > 0);
  checks.finished = spec.deadChunk
    ? checks.gave_up
    : spec.played
    ? phases === "loaded,done,loaded,done" && done.every((e) => e.cause) && !placed.length
    : spec.loads === 2
      ? phases === "loaded,pod,ship,done,loaded,done" && done.every((e) => !e.cause)
      : spec.failPlaceOnce
        ? phases === "loaded,failed,pod,ship,done" // retried in the same session
        : stopAt === Infinity
          ? phases === "loaded,pod,ship,done"
          : phases.endsWith(",done") && !phases.includes("failed");
  const o = state.frame?.origin;
  if (o) {
    // The hull is there, whole.
    checks.pod_whole = podCells.every(([x, y, z, p]) => dim.idAt(o.x + x, o.y + y, o.z + z) === p.name);
    // Not floating: every column the hull reaches ground level in (its belly and heat
    // shield; the nose rides up 8°, overhanging by design) has ground under it, or the
    // sea for a splashdown.
    const grade = WRECK.pod.hatch_out[1] - 1;
    const lowest = new Map();
    for (const [x, y, z, p] of podCells) {
      if (p.name === "minecraft:air") continue;
      const k = `${x},${z}`;
      if (!lowest.has(k) || y < lowest.get(k)) lowest.set(k, y);
    }
    const unsupported = [...lowest].filter(([k, y]) => y <= grade).filter(([k, y]) => {
      const [x, z] = k.split(",").map(Number);
      const below = id(o.x + x, o.y + y - 1, o.z + z);
      return below === "air";
    });
    checks.pod_supported = unsupported.length === 0;
    // A player-sized way out of the hatch onto walkable ground.
    const [hx, hy, hz] = state.frame.hatch;
    const [dx, dz] = state.frame.side;
    const ox = hx + dx * 2;
    const oz = hz + dz * 2;
    checks.hatch_open = open(hx, hy, hz) && open(hx, hy + 1, hz);
    const step = [0, -1, -2, 1].some((d) => solid(ox, hy - 1 + d, oz) && open(ox, hy + d, oz) && open(ox, hy + 1 + d, oz));
    checks.hatch_walkable = spec.splash ? true : step;
    // Spawn stands a player on ground, in the open.
    const sp = sim.spawn();
    checks.spawn_set = sp.y !== 32767;
    checks.spawn_on_ground = spec.splash || (solid(sp.x, sp.y - 1, sp.z) && open(sp.x, sp.y, sp.z) && open(sp.x, sp.y + 1, sp.z));
    // The parachute lies on something.
    const carpets = [];
    for (let x = o.x - 10; x < o.x + 60; x += 1) {
      for (let z = o.z - 40; z < o.z + 40; z += 1) {
        for (let y = o.y - 12; y < o.y + 20; y += 1) {
          if (/(white|orange)_carpet/.test(id(x, y, z))) carpets.push([x, y, z]);
        }
      }
    }
    checks.canopy_laid = spec.splash || carpets.length > 40;
    checks.canopy_on_ground = carpets.every(([x, y, z]) => id(x, y - 1, z) !== "air");
    // The starter kit is in the barrels.
    checks.kit_stocked = [...dim.items.values()].filter((list) => list.length).length >= 3;
  }
  const s = state.ship;
  if (s) {
    const [lx, ly, lz] = WRECK.ship.size;
    let peak = -64;
    for (let x = s.x; x < s.x + lx; x += 3) {
      for (let z = s.z; z < s.z + lz; z += 3) {
        for (let y = s.y - 1; y > -64; y -= 1) {
          if (!["air", "water"].includes(id(x, y, z))) {
            peak = Math.max(peak, y);
            break;
          }
        }
      }
    }
    checks.ship_clears_ground = s.y - peak >= 20;
    checks.ship_under_build_limit = s.y + ly - 1 <= 319;
    checks.ship_tiles_placed = placed.filter((p) => p.id !== WRECK.pod.id).length === WRECK.ship.tiles.length;
  }
  checks.ticking_areas_removed = dim.areas.size === 0;
  // A resume case proves nothing unless the build really was stopped part-way.
  if (stopAt !== Infinity) checks.was_stopped = sim.stopped() === 1;
  const digest = createHash("sha256");
  for (const [k, c] of [...dim.cells].sort(([a], [b]) => (a < b ? -1 : 1))) {
    digest.update(`${k}=${c.id}${JSON.stringify(c.states)};`);
  }
  for (const [k, items] of [...dim.items].sort(([a], [b]) => (a < b ? -1 : 1))) {
    digest.update(`${k}:${items.map((i) => `${i.typeId}x${i.amount}`).join(",")};`);
  }
  if (process.env.WRECK_SIM_DUMP && o) {
    // The site's blocks, for rendering a scenario (spikes/minecraft-spawn-wreck/render.py).
    const out = [];
    for (let x = o.x - 16; x < o.x + 46; x += 1) {
      for (let z = o.z - 34; z < o.z + 36; z += 1) {
        for (let y = o.y - 10; y < o.y + 26; y += 1) {
          const b = id(x, y, z);
          if (b !== "air") out.push([x, y, z, b]);
        }
      }
    }
    writeFileSync(join(process.env.WRECK_SIM_DUMP, `${name}.json`), JSON.stringify({ origin: o, cells: out }));
  }
  return {
    scenario: name,
    steps: sim.clock(),
    wrote: Object.fromEntries(["terrain", "crater", "canopy", "details"].map((st) => [st, wrote(st)])),
    world: digest.digest("hex"),
    ok: Object.values(checks).every(Boolean),
    checks,
    site: state.site && { x: state.site.x, y: state.site.y, z: state.site.z, splash: state.site.splash },
    ship: state.ship,
    material: state.frame && id(state.frame.shield[0] | 0, (state.frame.shield[1] | 0) - 3, state.frame.shield[2] | 0),
  };
}

let n = 0;
for (const [name, spec] of Object.entries(SCENARIOS)) {
  if (only.length && !only.includes(name)) continue;
  console.log(JSON.stringify(await run(name, spec, n++)));
}
// Stopped dead at several points of a plains build, then resumed: the world it ends up
// with must be exactly the one an uninterrupted build makes.
if (!only.length || only.includes("resume")) {
  const clean = await run("plains", SCENARIOS.plains, n++);
  for (const pct of [1, 30, 60, 90]) {
    const stopAt = Math.max(2, Math.floor((clean.steps * pct) / 100));
    const r = await run(`resume_after_${pct}pct`, SCENARIOS.plains, n++, stopAt);
    r.checks.same_world_as_uninterrupted = r.world === clean.world;
    r.ok = Object.values(r.checks).every(Boolean);
    console.log(JSON.stringify(r));
  }
}
