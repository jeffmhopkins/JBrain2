// The spawn wreck's pack (docs/plans/MINECRAFT_BEDROCK_PLAN.md §SW1–SW2). The wrapper
// installs it only into a world the box generated itself. On that world's first load it
// builds the crash site once: the escape pod in a crater at spawn, fitted to whatever
// ground is there, and the damaged mothership hanging above. Stable @minecraft/server
// only. The planning is in plan.js (node-tested); this file reads the world and places.
//
// Progress lives in a world dynamic property, so a build cut short by a restart resumes
// at its stage, and a finished one never runs again: that record, not the wrapper's
// bookkeeping, is the guard.
import { BlockPermutation, ItemStack, system, world } from "@minecraft/server";
import { WRECK } from "./data.js";
import * as plan from "./plan.js";

const KEY = "jbrain:wreck";
// A world that has already run for this long without a wreck isn't new: building into
// it now would land on whatever its players made at spawn.
const FRESH_TICKS = 20 * 60 * 10;
const POD_AREA = "jbrain_wreck_pod";
const SHIP_AREA = "jbrain_wreck_ship";
const SURVEY = 64; // half-width of the ground read round spawn
const LOAD_WAIT_TICKS = 20 * 90;

const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

function load() {
  try {
    return JSON.parse(world.getDynamicProperty(KEY) ?? "{}");
  } catch {
    return {};
  }
}

function save(state) {
  world.setDynamicProperty(KEY, JSON.stringify(state));
}

function command(dim, text) {
  try {
    dim.runCommand(text);
    return true;
  } catch (err) {
    // `tickingarea add` for an area that survived a crash, `remove` for one already
    // gone: both mean the world is already as wanted.
    console.warn(`[jbrain-wreck] ${text}: ${err}`);
    return false;
  }
}

function blockAt(dim, x, y, z) {
  try {
    return dim.getBlock({ x, y, z });
  } catch {
    return undefined; // an unloaded chunk throws on some versions, undefined on others
  }
}

function set(dim, x, y, z, id, states) {
  const b = blockAt(dim, x, y, z);
  if (!b) return;
  try {
    if (states) b.setPermutation(BlockPermutation.resolve(`minecraft:${id}`, states));
    else b.setType(`minecraft:${id}`);
  } catch (err) {
    console.warn(`[jbrain-wreck] ${id} at ${x} ${y} ${z}: ${err}`);
  }
}

/** Wait (as a job: yielding) until every listed column can be read. */
function* waitLoaded(dim, columns) {
  for (let t = 0; t < LOAD_WAIT_TICKS; t += 1) {
    if (columns.every(([x, z]) => blockAt(dim, x, 0, z))) return true;
    yield;
  }
  return false;
}

/** The ground of one column: the topmost block that isn't a plant, tree or snow cover. */
function readGround(dim, x, z) {
  let b;
  try {
    b = dim.getTopmostBlock({ x, z });
  } catch {
    return null;
  }
  for (let i = 0; b && i < 48 && plan.standsOnGround(b.typeId); i += 1) b = b.below();
  if (!b) return null;
  return { y: b.y, top: plan.bare(b.typeId), water: plan.isWater(b.typeId) };
}

function* survey(dim, cx, cz, half) {
  const ground = new Map();
  let n = 0;
  for (let x = cx - half; x <= cx + half; x += 1) {
    for (let z = cz - half; z <= cz + half; z += 1) {
      ground.set(`${x},${z}`, readGround(dim, x, z));
      if (++n % 48 === 0) yield;
    }
  }
  return (x, z) => ground.get(`${x},${z}`) ?? null;
}

/** Clear plants and trees standing on a column, so nothing hangs over the site. */
function clearColumn(dim, x, z, g) {
  for (let y = g.y + 1; y <= g.y + 24; y += 1) {
    const b = blockAt(dim, x, y, z);
    if (b && plan.standsOnGround(b.typeId) && plan.bare(b.typeId) !== "air") set(dim, x, y, z, "air");
  }
}

function* buildPod(dim, state, ground) {
  const pod = WRECK.pod;
  const frame = state.frame;
  const [sx, , sz] = pod.size;
  const o = frame.origin;
  const mats = plan.materialsFor((ground(o.x + (sx >> 1), o.z + (sz >> 1)) || {}).top);
  const inFootprint = (x, z) => x >= o.x && x < o.x + sx && z >= o.z && z < o.z + sz;
  const crater = plan.craterColumns(frame);
  const canopy = plan.canopyCells(frame);
  const cover = plan.hatchCover(frame);

  // Clear the site of trees and plants, then shape the footprint to the deck.
  const zone = new Set();
  for (const c of [...crater, ...canopy, ...cover]) zone.add(`${c.x},${c.z}`);
  for (let x = o.x - 2; x < o.x + sx + 2; x += 1) for (let z = o.z - 2; z < o.z + sz + 2; z += 1) zone.add(`${x},${z}`);
  let n = 0;
  for (const key of zone) {
    const [x, z] = key.split(",").map(Number);
    const g = ground(x, z);
    if (g && !g.water) clearColumn(dim, x, z, g);
    if (++n % 32 === 0) yield;
  }
  const surface = state.site.y;
  for (let x = o.x; x < o.x + sx; x += 1) {
    for (let z = o.z; z < o.z + sz; z += 1) {
      const g = ground(x, z);
      if (!g || state.site.splash) continue;
      for (let y = g.y + 1; y <= surface; y += 1) set(dim, x, y, z, mats.fill);
      for (let y = surface + 1; y <= Math.max(g.y, surface); y += 1) set(dim, x, y, z, "air");
    }
    yield;
  }

  world.structureManager.place(pod.id, dim, o);
  yield;

  // The crater, the soil it shoved up, and the skid mark, in the local ground's colours.
  n = 0;
  for (const c of crater) {
    const g = ground(c.x, c.z);
    if (!g || g.water || inFootprint(c.x, c.z)) continue;
    if (c.dig > 0) {
      for (let y = g.y - c.dig + 1; y <= g.y; y += 1) set(dim, c.x, y, c.z, "air");
      set(dim, c.x, g.y - c.dig, c.z, plan.pick(mats.dig, c.x, c.z, 1));
    } else if (c.raise > 0) {
      for (let y = g.y + 1; y <= g.y + c.raise; y += 1) set(dim, c.x, y, c.z, mats.rim);
    } else {
      set(dim, c.x, g.y, c.z, plan.pick(mats.dig, c.x, c.z, 2));
    }
    if (++n % 32 === 0) yield;
  }

  // The parachute, draped at each column's own height (floating, on water).
  n = 0;
  for (const c of canopy) {
    const g = ground(c.x, c.z);
    if (!g || inFootprint(c.x, c.z)) continue;
    const bigger = c.block.replace("_carpet", "_wool");
    if (g.water) set(dim, c.x, g.y, c.z, bigger);
    else set(dim, c.x, g.y + 1, c.z, c.block);
    if (++n % 32 === 0) yield;
  }
  for (const c of cover) {
    const g = ground(c.x, c.z);
    if (!g || g.water || inFootprint(c.x, c.z)) continue;
    if (c.block === "iron_trapdoor") {
      set(dim, c.x, g.y + 1, c.z, c.block, { direction: 0, open_bit: false, upside_down_bit: false });
    } else {
      set(dim, c.x, g.y + 1, c.z, c.block);
    }
  }
  // Level ground in front of the hatch, so it opens onto somewhere to stand. Never over
  // the hull: its frame round the hatch and its belly curve share these columns.
  if (!frame.splash) {
    const hull = new Set(pod.hull);
    const grade = (x, y, z, id) => {
      const b = blockAt(dim, x, y, z);
      if (b && !hull.has(b.typeId)) set(dim, x, y, z, id);
    };
    for (const a of plan.hatchApron(frame)) {
      const g = ground(a.x, a.z);
      const from = g ? Math.min(g.y + 1, a.y) : a.y;
      for (let y = from; y <= a.y; y += 1) grade(a.x, y, a.z, mats.fill);
      for (let y = a.y + 1; y <= a.y + 2; y += 1) grade(a.x, y, a.z, "air");
    }
  }
  yield;

  // Two campfires in the crater, smoking, on its dug floor.
  const dug = new Map(crater.map((c) => [`${c.x},${c.z}`, c.dig]));
  const [ax, az] = frame.axis;
  for (const d of [3, 5]) {
    const x = Math.floor(frame.shield[0] - ax * d);
    const z = Math.floor(frame.shield[2] - az * d + (d === 3 ? 2 : -2));
    const g = ground(x, z);
    if (!g || g.water || inFootprint(x, z)) continue;
    const floor = g.y - (dug.get(`${x},${z}`) ?? 0);
    set(dim, x, floor + 1, z, "campfire", { extinguished: false, "minecraft:cardinal_direction": "south" });
  }
  yield;

  // The starter kit, and spawn at the hatch.
  frame.barrels.forEach(([x, y, z], i) => {
    const kit = plan.STARTER_KIT[i];
    const inv = blockAt(dim, x, y, z)?.getComponent("minecraft:inventory");
    if (!kit || !inv?.container) return;
    for (const [id, count] of kit) {
      try {
        inv.container.addItem(new ItemStack(`minecraft:${id}`, count));
      } catch (err) {
        console.warn(`[jbrain-wreck] kit ${id}: ${err}`);
      }
    }
  });
  world.setDefaultSpawnLocation(plan.spawnPoint(frame));
  command(dim, "gamerule spawnradius 0");
}

function* buildShip(dim, state) {
  const { pod, ship } = WRECK;
  let top = 319;
  try {
    top = dim.heightRange.max - 1;
  } catch {
    // keep the Overworld default
  }
  const [lx, ly, lz] = ship.size;
  const base = plan.shipBase(state.frame, pod, ship);
  for (const shift of plan.SHIP_SHIFTS) {
    const x0 = base.x;
    const z0 = base.z + shift;
    command(dim, `tickingarea remove ${SHIP_AREA}`);
    command(dim, `tickingarea add ${x0} 0 ${z0} ${x0 + lx - 1} 0 ${z0 + lz - 1} ${SHIP_AREA} true`);
    yield* waitLoaded(dim, [[x0, z0], [x0 + lx - 1, z0], [x0, z0 + lz - 1], [x0 + lx - 1, z0 + lz - 1]]);
    let peak = -64;
    let n = 0;
    for (let x = x0; x < x0 + lx; x += 6) {
      for (let z = z0; z < z0 + lz; z += 6) {
        try {
          const b = dim.getTopmostBlock({ x, z });
          if (b) peak = Math.max(peak, b.y);
        } catch {
          // an unread column: the others decide
        }
        if (++n % 32 === 0) yield;
      }
    }
    const y = plan.shipHeight(base.y, peak, ly, top);
    if (y !== null) {
      state.ship = { x: x0, y, z: z0 };
      break;
    }
  }
  // Mountains everywhere it could go: hang it as high as it fits, over the base.
  if (!state.ship) state.ship = { x: base.x, y: top - ly + 1, z: base.z };
  save(state);
  for (const t of ship.tiles) {
    world.structureManager.place(t.id, dim, { x: state.ship.x + t.x, y: state.ship.y, z: state.ship.z + t.z });
    yield;
  }
  command(dim, `tickingarea remove ${SHIP_AREA}`);
}

function* build(state) {
  const dim = world.getDimension("overworld");
  if (!state.stage) {
    const spawn = world.getDefaultSpawnLocation();
    const cx = Math.floor(spawn.x);
    const cz = Math.floor(spawn.z);
    command(dim, `tickingarea add circle ${cx} 0 ${cz} 4 ${POD_AREA} true`);
    const ring = [[cx - SURVEY, cz - SURVEY], [cx + SURVEY, cz + SURVEY], [cx - SURVEY, cz + SURVEY], [cx + SURVEY, cz - SURVEY]];
    if (!(yield* waitLoaded(dim, ring))) {
      emit({ ev: "wreck", phase: "waiting" }); // retried at the next load
      return;
    }
    state.spawn = { x: cx, z: cz };
    state.stage = "pod";
    save(state);
  }
  if (state.stage === "pod") {
    const { x: cx, z: cz } = state.spawn;
    command(dim, `tickingarea add circle ${cx} 0 ${cz} 4 ${POD_AREA} true`);
    yield* waitLoaded(dim, [[cx - SURVEY, cz - SURVEY], [cx + SURVEY, cz + SURVEY]]);
    const ground = yield* survey(dim, cx, cz, SURVEY);
    const site = state.site ?? plan.chooseSite(ground, state.spawn, WRECK.pod);
    state.site = site;
    state.frame = plan.podFrame(site, WRECK.pod);
    yield* buildPod(dim, state, ground);
    command(dim, `tickingarea remove ${POD_AREA}`);
    state.stage = "ship";
    save(state);
    emit({ ev: "wreck", phase: "pod", x: state.frame.origin.x, y: state.frame.origin.y, z: state.frame.origin.z });
  }
  if (state.stage === "ship") {
    yield* buildShip(dim, state);
    state.stage = "done";
    save(state);
    emit({ ev: "wreck", phase: "ship", x: state.ship.x, y: state.ship.y, z: state.ship.z });
  }
  emit({ ev: "wreck", phase: "done" });
}

world.afterEvents.worldLoad.subscribe(() => {
  emit({ ev: "wreck", phase: "loaded" });
  const state = load();
  if (state.stage === "done") {
    emit({ ev: "wreck", phase: "done" }); // in case the wrapper missed it last time
    return;
  }
  if (!state.stage && world.getAbsoluteTime() > FRESH_TICKS) {
    save({ stage: "done", skipped: true });
    emit({ ev: "wreck", phase: "done", cause: "not a new world" });
    return;
  }
  system.runJob(build(state));
});
