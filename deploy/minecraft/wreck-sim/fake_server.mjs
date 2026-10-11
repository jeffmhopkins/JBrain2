// A stand-in for @minecraft/server, enough of it to run the wreck pack's main.js against
// a synthetic world (plan §SW2). Generation is a height function and blocks are what was
// set, but it is as strict as the game where that has bitten: with nobody online, only
// chunks inside a ticking area can be read or changed (anything else throws), and a
// run can be stopped dead mid-job, the way a server stops. sim.mjs installs it as
// node_modules/@minecraft/server for the run.

export class BlockPermutation {
  constructor(id, states) {
    this.id = id;
    this.states = states || {};
  }
  static resolve(id, states) {
    return new BlockPermutation(id, states);
  }
}

export class ItemStack {
  constructor(typeId, amount = 1) {
    this.typeId = typeId;
    this.amount = amount;
  }
}

const key = (x, y, z) => `${x},${y},${z}`;

class Block {
  constructor(dim, x, y, z) {
    this.dimension = dim;
    this.x = x;
    this.y = y;
    this.z = z;
  }
  get location() {
    return { x: this.x, y: this.y, z: this.z };
  }
  get typeId() {
    return this.dimension.idAt(this.x, this.y, this.z);
  }
  below() {
    return this.y - 1 < this.dimension.heightRange.min ? undefined : new Block(this.dimension, this.x, this.y - 1, this.z);
  }
  setType(id) {
    this.dimension.put(this.x, this.y, this.z, id, {});
  }
  setPermutation(p) {
    this.dimension.put(this.x, this.y, this.z, p.id, p.states);
  }
  getComponent(name) {
    if (name !== "minecraft:inventory" || !/barrel|chest/.test(this.typeId)) return undefined;
    const items = this.dimension.inventory(this.x, this.y, this.z);
    return {
      container: {
        size: 27,
        get emptySlotsCount() {
          return 27 - items.length;
        },
        addItem: (stack) => items.push(stack),
      },
    };
  }
}

export class Dimension {
  constructor(terrain) {
    this.id = "minecraft:overworld";
    this.terrain = terrain; // (x, y, z) => id for ungenerated cells
    this.cells = new Map();
    this.items = new Map();
    this.commands = [];
    this.heightRange = { min: -64, max: 320 };
    this.areas = new Map(); // name -> {box: [cx0, cz0, cx1, cz1] in chunks, at: tick}
    // Removed areas whose chunks haven't unloaded yet: on the box they go a moment
    // after the remove, so a load check right after one can be fooled.
    this.dying = []; // {box, until: tick}
  }
  loaded(x, z) {
    const cx = Math.floor(x / 16);
    const cz = Math.floor(z / 16);
    this.dying = this.dying.filter((d) => tick() < d.until);
    for (const { box } of this.dying) {
      const [x0, z0, x1, z1] = box;
      if (cx >= x0 && cx <= x1 && cz >= z0 && cz <= z1) return true;
    }
    // Like the game, an area's chunks come in over several ticks: here a few chunks a
    // tick, in rows, so a chunk in the middle loads after the corners have.
    for (const { box, at } of this.areas.values()) {
      const [x0, z0, x1, z1] = box;
      if (cx < x0 || cx > x1 || cz < z0 || cz > z1) continue;
      const order = (cx - x0) * (z1 - z0 + 1) + (cz - z0);
      const row = Math.floor((z1 - z0 + 1) / 2);
      const ready = order === 0 || cx === x1 || ((cz === z0 || cz === z1) && order < row) ? 0 : order;
      if (tick() - at >= Math.max(1, ready / CHUNKS_PER_TICK)) return true;
    }
    return false;
  }
  check(x, z) {
    if (!this.loaded(x, z)) throw new Error(`LocationInUnloadedChunkError at ${x} ${z}`);
  }
  idAt(x, y, z) {
    const c = this.cells.get(key(x, y, z));
    return c ? c.id : this.terrain(x, y, z);
  }
  statesAt(x, y, z) {
    return this.cells.get(key(x, y, z))?.states ?? {};
  }
  put(x, y, z, id, states, viaStructure = false) {
    if (y < this.heightRange.min || y >= this.heightRange.max) throw new Error(`y ${y} out of the world`);
    if (!viaStructure) this.check(x, z);
    this.cells.set(key(x, y, z), { id: id.startsWith("minecraft:") ? id : `minecraft:${id}`, states });
  }
  inventory(x, y, z) {
    const k = key(x, y, z);
    if (!this.items.has(k)) this.items.set(k, []);
    return this.items.get(k);
  }
  getBlock({ x, y, z }) {
    this.check(Math.floor(x), Math.floor(z));
    return new Block(this, Math.floor(x), Math.floor(y), Math.floor(z));
  }
  getTopmostBlock({ x, z }) {
    this.check(x, z);
    for (let y = this.heightRange.max - 1; y >= this.heightRange.min; y -= 1) {
      if (this.idAt(x, y, z) !== "minecraft:air") return new Block(this, x, y, z);
    }
    return undefined;
  }
  runCommand(text) {
    this.commands.push(text);
    const words = text.split(/\s+/);
    if (words[0] === "tickingarea" && words[1] === "remove") {
      const gone = this.areas.get(words[2]);
      // The box answers a remove of no such area without throwing.
      if (!gone) return { successCount: 0 };
      this.areas.delete(words[2]);
      this.dying.push({ box: gone.box, until: tick() + UNLOAD_DELAY_TICKS });
    } else if (words[0] === "tickingarea" && words[1] === "add" && words[2] !== "circle") {
      const [x0, , z0, x1, , z1] = words.slice(2, 8).map(Number);
      const box = [Math.floor(Math.min(x0, x1) / 16), Math.floor(Math.min(z0, z1) / 16),
        Math.floor(Math.max(x0, x1) / 16), Math.floor(Math.max(z0, z1) / 16)];
      if ((box[2] - box[0] + 1) * (box[3] - box[1] + 1) > 100) throw new Error("ticking area over 100 chunks");
      if (this.areas.size >= 10) throw new Error("too many ticking areas");
      this.areas.set(words[8], { box, at: tick() });
    } else if (words[0] === "tickingarea" && words[2] === "circle") {
      const [x, , z, r] = words.slice(3, 7).map(Number);
      const cx = Math.floor(x / 16);
      const cz = Math.floor(z / 16);
      // Only the inscribed square counts here, so code that leans on the corners of a
      // circle fails as it would in the game.
      const k = Math.floor(r / Math.SQRT2);
      this.areas.set(words[7], { box: [cx - k, cz - k, cx + k, cz + k], at: tick() });
    }
    return { successCount: 1 };
  }
}

let stopAfterSteps = Infinity;
let clock = 0; // job steps run so far
let stopped = 0;
let idle = 0;
// The game runs many job steps a tick, so a wait counted in yields is far shorter than
// it looks; the fake does the same, and loads chunks by the tick, as the game does.
const STEPS_PER_TICK = 50;
const CHUNKS_PER_TICK = 1;
const tick = () => Math.floor(clock / STEPS_PER_TICK);
// The box's own timing: a removed area's chunks are gone by the next tick, and a new
// area has none ready on the tick it was added, the corners included.
const UNLOAD_DELAY_TICKS = 1;
const timeouts = [];

const listeners = [];
const props = new Map();
let spawn = { x: 0, y: 32767, z: 0 };
let dim = null;
let placeHook = null;
let ticks = 0;

export const world = {
  afterEvents: { worldLoad: { subscribe: (fn) => listeners.push(fn) } },
  getDimension: () => dim,
  getDynamicProperty: (k) => props.get(k),
  setDynamicProperty: (k, v) => props.set(k, v),
  getDefaultSpawnLocation: () => ({ ...spawn }),
  setDefaultSpawnLocation: (loc) => {
    spawn = { ...loc };
  },
  getAbsoluteTime: () => ticks,
  structureManager: {
    place: (id, d, loc) => placeHook(id, d, loc),
  },
};

export const system = {
  get currentTick() {
    return tick();
  },
  runJob(gen) {
    // The game spreads a job over ticks; here it simply runs to the end — or, when the
    // test stops the "server" mid-job, is dropped there with no cleanup at all.
    let steps = 0;
    while (!gen.next().done) {
      const was = tick();
      clock += 1;
      // A tick a build spends with no ticking area up: on the box, with nobody on, the
      // world barely ticks then, so a wait through one takes minutes.
      if (tick() !== was && dim && dim.areas.size === 0) idle += 1;
      if (++steps >= stopAfterSteps) {
        stopped += 1;
        timeouts.length = 0; // the "server" is gone: nothing it scheduled runs either
        return 1; // abandoned: no finally, no catch
      }
      if (steps > 5e6) throw new Error("job never finished");
    }
    return 1;
  },
  runTimeout(fn, _ticks) {
    // Run once the current load's work has: sim.load() drains these.
    timeouts.push(fn);
    return timeouts.length;
  },
};

/** Test-side controls, not part of the game API. */
export const sim = {
  reset(terrain, spawnAt, onPlace) {
    listeners.length = 0;
    props.clear();
    spawn = { x: spawnAt.x, y: 32767, z: spawnAt.z };
    dim = new Dimension(terrain);
    placeHook = onPlace;
    ticks = 0;
    stopAfterSteps = Infinity;
    stopped = 0;
    idle = 0;
    clock = 0;
    timeouts.length = 0;
    return dim;
  },
  load() {
    for (const fn of listeners) fn({});
    for (let i = 0; timeouts.length && i < 20; i += 1) timeouts.shift()();
  },
  stopped: () => stopped,
  idleTicks: () => idle,
  clock: () => clock,
  /** Stop the "server" dead after this many job steps (Infinity: never). */
  stopAfter(n) {
    stopAfterSteps = n;
  },
  props,
  spawn: () => spawn,
  setTime: (t) => {
    ticks = t;
  },
};
