// The spawn wreck's pack (docs/plans/MINECRAFT_BEDROCK_PLAN.md §SW1–SW2). The wrapper
// installs it only into a world the box generated itself. On that world's first load it
// builds the crash site once: the escape pod in a crater at spawn, fitted to whatever
// ground is there, and the damaged mothership hanging above. Stable @minecraft/server
// only. The planning is in plan.js (node-tested); this file reads the world and places.
//
// Progress lives in a world dynamic property: the stage, each step done, and the ground
// as first read. A build cut short (a crash, a restart) resumes at the step it stopped
// on, from those first heights, so nothing is dug twice or placed twice; a finished one
// never runs again. That record, not the wrapper's bookkeeping, is the guard. (A hard
// kill can save the record and the chunks a moment apart; the worst that does is redo
// one step, and each step lands on the same blocks again.)
import { BlockPermutation, ItemStack, system, world } from "@minecraft/server";
import { WRECK } from "./data.js";
import * as plan from "./plan.js";

const KEY = "jbrain:wreck";
// A world that has already run for this long without a wreck isn't new: building into
// it now would land on whatever its players made at spawn.
const FRESH_TICKS = 20 * 60 * 10;
const POD_AREA = "jbrain_wreck_pod";
const SHIP_AREA = "jbrain_wreck_ship";
// Half-width of the square loaded and read round spawn: 128 blocks is at most 9 × 9
// chunks, inside a ticking area's 100. The site is chosen within 24 of spawn, and its
// crater, skid mark and canopy reach about 40 further.
const SURVEY = 64;
// Waits are measured on the clock, not in ticks, and never spin: a job that yields in a
// loop is handed the rest of every tick, and on the box that starved the world to a tick
// every few seconds (337 in 15 minutes), too slow for its chunks ever to load. Left
// alone it ran 10 ticks a second.
const LOAD_WAIT_MS = 90_000;
// A box has "held" once every chunk read fine on this many ticks, at least this far apart.
const STEADY_TICKS = 3;
const STEADY_MS = 1_000;
const PROGRESS_MS = 15_000;
const MAX_FAILURES = 3;
const RETRY_TICKS = 20 * 30;
const POD_STEPS = ["terrain", "place", "crater", "canopy", "details", "kit", "spawn"];

const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);
// A warning goes out as a plain log line: on the box a build whose block edits didn't
// land showed no warnings at all, and log lines are what the console reliably shows.
const warn = (text) => console.log(`[jbrain-wreck] ${text}`);
// Each pod step's block writes, and the first reason one didn't land, for its summary.
const writes = { tried: 0, failed: 0, first: "" };

class Unloaded extends Error {}

// Yielded by a build that has nothing to do until the next tick: run() stops the job and
// picks it up then, so the wait costs the world nothing.
const NEXT_TICK = Symbol("next tick");

function run(gen) {
  system.runJob(
    (function* () {
      for (;;) {
        const { done, value } = gen.next();
        if (done) return;
        if (value === NEXT_TICK) {
          system.runTimeout(() => run(gen), 1);
          return;
        }
        yield;
      }
    })(),
  );
}

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

function command(dim, text, quiet = false) {
  try {
    dim.runCommand(text);
    return true;
  } catch (err) {
    // `tickingarea add` for an area that survived a crash, `remove` for one already
    // gone: both mean the world is already as wanted.
    if (!quiet) warn(`${text}: ${err}`);
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
  writes.tried += 1;
  const fail = (why) => {
    writes.failed += 1;
    if (!writes.first) writes.first = `${id} at ${x} ${y} ${z}: ${why}`;
  };
  const b = blockAt(dim, x, y, z);
  if (!b) return fail("no block there (chunk not loaded?)");
  try {
    if (states) b.setPermutation(BlockPermutation.resolve(`minecraft:${id}`, states));
    else b.setType(`minecraft:${id}`);
  } catch (err) {
    return fail(String(err));
  }
  // Read back, so a write the game took without throwing but didn't keep is told apart
  // from one lost later (the box's first sweep lost every one, with no error).
  const now = blockAt(dim, x, y, z)?.typeId;
  if (now !== `minecraft:${id}`) fail(`read back as ${now}`);
}

// The ticking areas this run has up, by name, with their boxes. On the box a removed
// area's chunks unload a moment later, so re-adding one it already had let the load
// check pass on chunks about to go, and every block edit after it fell on nothing. An
// area already up for the same box is kept. A replaced one is removed and re-added in
// the same tick, never waited out, so the box being loaded is never without one.
// loadBox's steady reads are what catch the late unload (SW4).
const live = new Map();

function area(dim, name, x0, z0, x1, z1) {
  const box = `${x0} ${z0} ${x1} ${z1}`;
  if (live.get(name) === box) return;
  command(dim, `tickingarea remove ${name}`, true);
  command(dim, `tickingarea add ${x0} 0 ${z0} ${x1} 0 ${z1} ${name} true`);
  live.set(name, box);
}

function drop(dim, name, quiet = false) {
  command(dim, `tickingarea remove ${name}`, quiet);
  live.delete(name);
}

function dropAreas(dim) {
  for (const name of [POD_AREA, SHIP_AREA]) drop(dim, name, true);
}

/**
 * Wait, a tick at a time, until every chunk of the box can be read, and stays so. A
 * ticking area loads its chunks over several ticks, so the corners being ready says
 * nothing about the middle: one column per chunk is read. A chunk read once can still be
 * about to unload (a just-removed area's), so every chunk is read again each tick until
 * the box has held for STEADY_TICKS ticks over STEADY_MS. A slow load logs how many
 * chunks it's still missing.
 */
function* loadBox(dim, x0, z0, x1, z1) {
  const columns = [];
  for (let cx = Math.floor(x0 / 16); cx <= Math.floor(x1 / 16); cx += 1) {
    for (let cz = Math.floor(z0 / 16); cz <= Math.floor(z1 / 16); cz += 1) {
      columns.push([Math.min(Math.max(cx * 16, x0), x1), Math.min(Math.max(cz * 16, z0), z1)]);
    }
  }
  const start = Date.now();
  let told = start;
  let held = 0;
  let heldSince = 0;
  let last = -1;
  let missing = columns.length;
  while (Date.now() - start < LOAD_WAIT_MS) {
    const tick = system.currentTick;
    if (tick !== last) {
      last = tick;
      missing = columns.filter(([x, z]) => !blockAt(dim, x, 0, z)).length;
      if (missing) {
        held = 0;
      } else {
        if (held === 0) heldSince = Date.now();
        held += 1;
        if (held >= STEADY_TICKS && Date.now() - heldSince >= STEADY_MS) return;
      }
    }
    if (Date.now() - told >= PROGRESS_MS) {
      told = Date.now();
      warn(`loading ${x0},${z0}..${x1},${z1}: ${missing} of ${columns.length} chunks missing, tick ${tick}`);
    }
    yield NEXT_TICK;
  }
  throw new Unloaded(`${missing} chunks of ${x0},${z0}..${x1},${z1} never loaded`);
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
  return { y: b.y, top: plan.bare(b.typeId), water: plan.isWater(b.typeId), lava: plan.isLava(b.typeId) };
}

function* survey(dim, cx, cz, half) {
  const ground = new Map();
  let n = 0;
  for (let x = cx - half; x < cx + half; x += 1) {
    for (let z = cz - half; z < cz + half; z += 1) {
      ground.set(`${x},${z}`, readGround(dim, x, z));
      if (++n % 48 === 0) yield;
    }
  }
  return (x, z) => ground.get(`${x},${z}`) ?? null;
}

/** Choose the site and keep it, with the ground under it, before anything is changed. */
function* plan_(dim, state) {
  const spawn = world.getDefaultSpawnLocation();
  const cx = Math.floor(spawn.x);
  const cz = Math.floor(spawn.z);
  area(dim, POD_AREA, cx - SURVEY, cz - SURVEY, cx + SURVEY - 1, cz + SURVEY - 1);
  yield* loadBox(dim, cx - SURVEY, cz - SURVEY, cx + SURVEY - 1, cz + SURVEY - 1);
  const ground = yield* survey(dim, cx, cz, SURVEY);
  const site = plan.chooseSite(ground, { x: cx, z: cz }, WRECK.pod);
  const frame = plan.podFrame(site, WRECK.pod);
  const zone = plan.podZone(frame, WRECK.pod);
  const [sx, , sz] = WRECK.pod.size;
  state.spawn = { x: cx, z: cz };
  state.site = site;
  state.frame = frame;
  state.top = (ground(frame.origin.x + (sx >> 1), frame.origin.z + (sz >> 1)) || {}).top || "grass_block";
  state.ground = plan.encodeGround(ground, zone.x0, zone.z0, zone.w, zone.h, site.y);
  state.stage = "pod";
  state.steps = [];
  save(state);
}

/** Clear plants and trees standing on a column, so nothing hangs over the site. */
function clearColumn(dim, x, z, g) {
  for (let y = g.y + 1; y <= g.y + 40; y += 1) { // a mega jungle tree is ~31 tall
    const b = blockAt(dim, x, y, z);
    if (b && plan.standsOnGround(b.typeId) && plan.bare(b.typeId) !== "air") set(dim, x, y, z, "air");
  }
}

function* podStep(dim, state, step, ground) {
  const pod = WRECK.pod;
  const frame = state.frame;
  const [sx, , sz] = pod.size;
  const o = frame.origin;
  const mats = plan.materialsFor(state.top);
  const inFootprint = (x, z) => x >= o.x && x < o.x + sx && z >= o.z && z < o.z + sz;
  const crater = plan.craterColumns(frame);
  let n = 0;

  if (step === "terrain") {
    // Clear the site of trees and plants, then shape the footprint to the deck.
    const { x0, z0, w, h } = state.ground;
    for (let x = x0; x < x0 + w; x += 1) {
      for (let z = z0; z < z0 + h; z += 1) {
        const g = ground(x, z);
        if (g && !g.water) clearColumn(dim, x, z, g);
        if (++n % 32 === 0) yield;
      }
    }
    for (let x = o.x; x < o.x + sx; x += 1) {
      for (let z = o.z; z < o.z + sz; z += 1) {
        const g = ground(x, z);
        if (!g || frame.splash) continue;
        for (let y = g.y + 1; y <= state.site.y; y += 1) set(dim, x, y, z, mats.fill);
        for (let y = state.site.y + 1; y <= Math.max(g.y, state.site.y); y += 1) set(dim, x, y, z, "air");
      }
      yield;
    }
  } else if (step === "place") {
    world.structureManager.place(pod.id, dim, o);
  } else if (step === "crater") {
    // The crater, the soil it shoved up, and the skid mark, in the local ground's
    // colours. Heights are the first read's, so a re-run lands on the same blocks.
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
  } else if (step === "canopy") {
    // The parachute, draped at each column's own height; on water it floats as wool,
    // and it never goes near lava.
    const raised = new Map(crater.map((c) => [`${c.x},${c.z}`, c.raise - c.dig]));
    for (const c of plan.canopyCells(frame)) {
      const g = ground(c.x, c.z);
      if (!g || g.lava || inFootprint(c.x, c.z)) continue;
      if (g.water) set(dim, c.x, g.y, c.z, c.block.replace("_carpet", "_wool"));
      else set(dim, c.x, g.y + 1 + (raised.get(`${c.x},${c.z}`) ?? 0), c.z, c.block);
      if (++n % 32 === 0) yield;
    }
  } else if (step === "details") {
    for (const c of plan.hatchCover(frame)) {
      const g = ground(c.x, c.z);
      if (!g || g.water || inFootprint(c.x, c.z)) continue;
      if (c.block === "iron_trapdoor") {
        set(dim, c.x, g.y + 1, c.z, c.block, { direction: 0, open_bit: false, upside_down_bit: false });
      } else {
        set(dim, c.x, g.y + 1, c.z, c.block);
      }
    }
    // Level ground in front of the hatch, so it opens onto somewhere to stand. Never
    // over the hull: its frame round the hatch and its belly curve share these columns.
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
        for (let y = a.y + 1; y <= a.y + 3; y += 1) grade(a.x, y, a.z, "air");
      }
    }
    // Two campfires smoking on the crater floor (only where it was dug, never a rim).
    const [ax, az] = frame.axis;
    const dug = new Map(crater.map((c) => [`${c.x},${c.z}`, c.dig]));
    for (const d of [3, 5]) {
      const x = Math.floor(frame.shield[0] - ax * d);
      const z = Math.floor(frame.shield[2] - az * d + (d === 3 ? 2 : -2));
      const g = ground(x, z);
      const depth = dug.get(`${x},${z}`) ?? 0;
      if (!g || g.water || depth < 1 || inFootprint(x, z)) continue;
      set(dim, x, g.y - depth + 1, z, "campfire", { extinguished: false, "minecraft:cardinal_direction": "south" });
    }
  } else if (step === "kit") {
    // The starter kit, into barrels still empty, so a re-run doesn't double it.
    frame.barrels.forEach(([x, y, z], i) => {
      const kit = plan.STARTER_KIT[i];
      const container = blockAt(dim, x, y, z)?.getComponent("minecraft:inventory")?.container;
      if (!kit || !container || container.emptySlotsCount < container.size) return;
      for (const [id, count] of kit) {
        try {
          container.addItem(new ItemStack(`minecraft:${id}`, count));
        } catch (err) {
          warn(`kit ${id}: ${err}`);
        }
      }
    });
  } else if (step === "spawn") {
    world.setDefaultSpawnLocation(plan.spawnPoint(frame));
    command(dim, "gamerule spawnradius 0");
  }
}

function* buildPod(dim, state) {
  const { x: cx, z: cz } = state.spawn;
  area(dim, POD_AREA, cx - SURVEY, cz - SURVEY, cx + SURVEY - 1, cz + SURVEY - 1);
  yield* loadBox(dim, cx - SURVEY, cz - SURVEY, cx + SURVEY - 1, cz + SURVEY - 1);
  const ground = plan.decodeGround(state.ground);
  for (const step of POD_STEPS) {
    if (state.steps.includes(step)) continue;
    Object.assign(writes, { tried: 0, failed: 0, first: "" });
    yield* podStep(dim, state, step, ground);
    // Every step says what it wrote, so one that quietly did nothing (no ground read
    // for its columns, say) shows up on the box, not only one that failed.
    const lost = writes.failed ? `; ${writes.failed} not written, first: ${writes.first}` : "";
    warn(`step ${step}: ${writes.tried - writes.failed} of ${writes.tried} blocks written${lost}`);
    state.steps.push(step);
    save(state);
    yield;
  }
  drop(dim, POD_AREA);
}

function* peakUnder(dim, x0, z0, lx, lz) {
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
  return peak;
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
  if (!state.ship) {
    const base = plan.shipBase(state.frame, pod, ship);
    for (const shift of plan.SHIP_SHIFTS) {
      const [x0, z0] = [base.x, base.z + shift];
      area(dim, SHIP_AREA, x0, z0, x0 + lx - 1, z0 + lz - 1);
      yield* loadBox(dim, x0, z0, x0 + lx - 1, z0 + lz - 1);
      const y = plan.shipHeight(base.y, yield* peakUnder(dim, x0, z0, lx, lz), ly, top);
      if (y !== null) {
        state.ship = { x: x0, y, z: z0 };
        break;
      }
    }
    // Mountains everywhere it could go: hang it as high as it fits, over the base.
    if (!state.ship) state.ship = { x: base.x, y: top - ly + 1, z: base.z };
    state.tiles = 0;
    save(state);
  }
  const { x, y, z } = state.ship;
  area(dim, SHIP_AREA, x, z, x + lx - 1, z + lz - 1);
  yield* loadBox(dim, x, z, x + lx - 1, z + lz - 1);
  for (let i = state.tiles ?? 0; i < ship.tiles.length; i += 1) {
    const t = ship.tiles[i];
    world.structureManager.place(t.id, dim, { x: x + t.x, y, z: z + t.z });
    state.tiles = i + 1;
    save(state);
    yield;
  }
  drop(dim, SHIP_AREA);
}

function* build(state) {
  const dim = world.getDimension("overworld");
  try {
    if (!state.stage) yield* plan_(dim, state);
    if (state.stage === "pod") {
      yield* buildPod(dim, state);
      state.stage = "ship";
      save(state);
      const o = state.frame.origin;
      emit({ ev: "wreck", phase: "pod", x: o.x, y: o.y, z: o.z });
    }
    if (state.stage === "ship") {
      yield* buildShip(dim, state);
      state.stage = "done";
      save(state);
      emit({ ev: "wreck", phase: "ship", x: state.ship.x, y: state.ship.y, z: state.ship.z });
    }
    emit({ ev: "wreck", phase: "done" });
  } catch (err) {
    // Retried shortly, in this same session, from the step it stopped on (before anyone
    // has had time to build at spawn); a build that keeps failing is given up rather
    // than left half-trying forever.
    state.failures = (state.failures ?? 0) + 1;
    const cause = String(err?.message ?? err).slice(0, 60);
    if (state.failures >= MAX_FAILURES) {
      state.stage = "done";
      state.skipped = true;
      save(state);
      emit({ ev: "wreck", phase: "done", cause });
    } else {
      save(state);
      emit({ ev: "wreck", phase: "failed", cause });
      system.runTimeout(() => run(build(load())), RETRY_TICKS);
    }
  } finally {
    // A build that will be retried keeps its areas up, so its chunks are still there when
    // it picks up again.
    if (state.stage === "done") dropAreas(dim);
  }
}

world.afterEvents.worldLoad.subscribe(() => {
  emit({ ev: "wreck", phase: "loaded" });
  const state = load();
  if (state.stage === "done") {
    dropAreas(world.getDimension("overworld")); // any a crash left behind
    // In case the wrapper missed it last time; a world passed over says so again, or
    // the wrapper would record it built.
    emit({ ev: "wreck", phase: "done", ...(state.skipped ? { cause: "passed over" } : {}) });
    return;
  }
  // A world that already tried (and failed) to build is new by that record, however long
  // it has run since.
  if (!state.stage && !state.failures && world.getAbsoluteTime() > FRESH_TICKS) {
    save({ stage: "done", skipped: true });
    emit({ ev: "wreck", phase: "done", cause: "not a new world" });
    return;
  }
  run(build(state));
});
