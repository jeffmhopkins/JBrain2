// A stand-in for @minecraft/server, enough of it to run the wreck pack's main.js against
// a synthetic world (plan §SW2). It is the simulator's world, not a model of the game:
// every chunk is loaded, generation is a height function, and blocks are what was set.
// sim.mjs installs it as node_modules/@minecraft/server for the run.

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
    return { container: { addItem: (stack) => items.push(stack) } };
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
  }
  idAt(x, y, z) {
    const c = this.cells.get(key(x, y, z));
    return c ? c.id : this.terrain(x, y, z);
  }
  statesAt(x, y, z) {
    return this.cells.get(key(x, y, z))?.states ?? {};
  }
  put(x, y, z, id, states) {
    if (y < this.heightRange.min || y >= this.heightRange.max) throw new Error(`y ${y} out of the world`);
    this.cells.set(key(x, y, z), { id: id.startsWith("minecraft:") ? id : `minecraft:${id}`, states });
  }
  inventory(x, y, z) {
    const k = key(x, y, z);
    if (!this.items.has(k)) this.items.set(k, []);
    return this.items.get(k);
  }
  getBlock({ x, y, z }) {
    return new Block(this, Math.floor(x), Math.floor(y), Math.floor(z));
  }
  getTopmostBlock({ x, z }) {
    for (let y = this.heightRange.max - 1; y >= this.heightRange.min; y -= 1) {
      if (this.idAt(x, y, z) !== "minecraft:air") return new Block(this, x, y, z);
    }
    return undefined;
  }
  runCommand(text) {
    this.commands.push(text);
    return { successCount: 1 };
  }
}

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
  runJob(gen) {
    // The game spreads a job over ticks; here it simply runs to the end.
    let steps = 0;
    while (!gen.next().done) {
      if (++steps > 5e6) throw new Error("job never finished");
    }
    return 1;
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
    return dim;
  },
  load() {
    for (const fn of listeners) fn({});
  },
  props,
  spawn: () => spawn,
  setTime: (t) => {
    ticks = t;
  },
};
