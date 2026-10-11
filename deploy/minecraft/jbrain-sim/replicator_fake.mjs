// fake_server.mjs plus the blocks the Replicator needs (plan §M15): placed blocks with
// states, block containers (chests, hoppers), stack sizes, and which chunks are loaded.
// The supervisor's test_minecraft_replicator.py installs it as node_modules/@minecraft/server,
// with fake_server.mjs beside it as base.mjs. As in the game, an item read from a
// container is a copy, and an unknown item id can't be made into a stack.
import { Dimension, ItemStack, sim } from "./base.mjs";

export * from "./base.mjs";

// Stack sizes for the ids the tests use; any other id is no item at all.
const MAX = {
  "minecraft:iron_ingot": 64, "minecraft:diamond": 64, "minecraft:cobblestone": 64,
  "minecraft:dirt": 64, "minecraft:stone": 64, "minecraft:coal": 64, "minecraft:charcoal": 64,
  "minecraft:coal_block": 64, "minecraft:oak_planks": 64, "minecraft:stick": 64,
  "minecraft:bucket": 16, "minecraft:lava_bucket": 1, "minecraft:ender_pearl": 16,
  "minecraft:snowball": 16, "minecraft:diamond_pickaxe": 1, "minecraft:furnace": 64,
  "jbrain:power_pack": 16, "jbrain:tricorder": 1, "jbrain:replicator": 1,
  "jbrain:replicator_programmed": 1,
};
Object.defineProperty(ItemStack.prototype, "maxAmount", {
  get() {
    if (!(this.typeId in MAX)) throw new Error(`Invalid item identifier '${this.typeId}'`);
    return MAX[this.typeId];
  },
});

const BLOCK_IDS = new Set([
  "minecraft:cobblestone", "minecraft:dirt", "minecraft:stone", "minecraft:coal_block",
  "minecraft:oak_planks", "minecraft:furnace", "minecraft:chest", "minecraft:hopper",
  "jbrain:replicator",
]);
export const BlockTypes = { get: (id) => (BLOCK_IDS.has(id) ? { id } : undefined) };

class BlockContainer {
  constructor(size) {
    this.size = size;
    this.slots = new Array(size).fill(undefined);
  }
  getItem(i) {
    return this.slots[i]?.clone();
  }
  setItem(i, item) {
    this.slots[i] = item && item.amount > 0 ? item.clone() : undefined;
  }
  // As the game does: onto matching plain stacks first, then into empty slots.
  addItem(item) {
    let left = item.amount;
    for (const s of this.slots) {
      if (left && s && s.typeId === item.typeId && !s.nameTag && s.amount < MAX[s.typeId]) {
        const put = Math.min(left, MAX[s.typeId] - s.amount);
        s.amount += put;
        left -= put;
      }
    }
    for (let i = 0; left && i < this.size; i++) {
      if (!this.slots[i]) {
        const put = Math.min(left, MAX[item.typeId] ?? 64);
        this.slots[i] = item.clone();
        this.slots[i].amount = put;
        left -= put;
      }
    }
    if (!left) return undefined;
    const spill = item.clone();
    spill.amount = left;
    return spill;
  }
}

const perm = (typeId, states) => ({
  type: { id: typeId },
  getState: (k) => states[k],
  withState: (k, v) => perm(typeId, { ...states, [k]: v }),
  states,
});

const keyOf = ({ x, y, z }) => `${x},${y},${z}`;

class Block {
  constructor(dimension, location) {
    this.dimension = dimension;
    this.location = { ...location };
  }
  get cell() {
    return this.dimension.cells.get(keyOf(this.location));
  }
  get typeId() {
    return this.cell?.permutation.type.id ?? "minecraft:air";
  }
  get permutation() {
    return this.cell?.permutation ?? perm("minecraft:air", {});
  }
  setPermutation(p) {
    this.dimension.cells.set(keyOf(this.location), { permutation: p, container: undefined });
  }
  getComponent(name) {
    const c = this.cell?.container;
    return name === "minecraft:inventory" && c ? { container: c } : undefined;
  }
  offset(dx, dy, dz) {
    const { x, y, z } = this.location;
    return new Block(this.dimension, { x: x + dx, y: y + dy, z: z + dz });
  }
  above() { return this.offset(0, 1, 0); }
  below() { return this.offset(0, -1, 0); }
  north() { return this.offset(0, 0, -1); }
  south() { return this.offset(0, 0, 1); }
  east() { return this.offset(1, 0, 0); }
  west() { return this.offset(-1, 0, 0); }
  center() {
    const { x, y, z } = this.location;
    return { x: x + 0.5, y: y + 0.5, z: z + 0.5 };
  }
}

Object.defineProperty(Dimension.prototype, "cells", {
  get() {
    if (!this._cells) this._cells = new Map();
    return this._cells;
  },
});
Dimension.prototype.getBlock = function getBlock(location) {
  return new Block(this, location);
};

const chunkOf = ({ x, z }) => `${Math.floor(x / 16)},${Math.floor(z / 16)}`;
const unloaded = new Set();
const REP = "jbrain:replicator";
const component = () => sim.blockComponent(REP);

/** Test-side controls for blocks, not part of the game API. */
export const blocks = {
  reset() {
    unloaded.clear();
  },
  // A container block (a chest, a hopper) at `loc`; returns its container.
  container(loc, typeId = "minecraft:chest", size = 27) {
    const b = sim.overworld.getBlock(loc);
    b.setPermutation(perm(typeId, {}));
    const c = new BlockContainer(size);
    b.cell.container = c;
    return c;
  },
  unload(loc) {
    unloaded.add(chunkOf(loc));
  },
  load(loc) {
    unloaded.delete(chunkOf(loc));
  },
  block: (loc) => sim.overworld.getBlock(loc),
  // A player placing the item in their selected slot at `loc`, as block_placer does:
  // the before-event (which may change the permutation), the block set, the item used up,
  // then onPlace.
  place(player, loc) {
    const block = sim.overworld.getBlock(loc);
    const ev = {
      block, dimension: sim.overworld, player,
      permutationToPlace: perm(REP, { "jbrain:programmed": false, "minecraft:cardinal_direction": "south" }),
    };
    component().beforeOnPlayerPlace(ev);
    block.setPermutation(ev.permutationToPlace);
    player.inventory.setItem(player.selectedSlotIndex, undefined);
    component().onPlace({ block, dimension: sim.overworld });
    return block;
  },
  breakBy(player, loc) {
    const block = sim.overworld.getBlock(loc);
    const brokenBlockPermutation = block.permutation;
    sim.overworld.cells.delete(keyOf(loc));
    component().onPlayerBreak({ block, dimension: sim.overworld, player, brokenBlockPermutation });
  },
  interact(player, loc) {
    component().onPlayerInteract({ block: sim.overworld.getBlock(loc), dimension: sim.overworld, player });
  },
  // `n` of the block's own ticks (each one is 200 game ticks, ~10 s): only replicators in
  // loaded chunks get them, as the game only ticks loaded land.
  ticks(n = 1) {
    for (let k = 0; k < n; k++) {
      for (const [key, cell] of [...sim.overworld.cells]) {
        if (cell.permutation.type.id !== REP) continue;
        const [x, y, z] = key.split(",").map(Number);
        if (unloaded.has(chunkOf({ x, z }))) continue;
        component().onTick({ block: sim.overworld.getBlock({ x, y, z }), dimension: sim.overworld });
      }
    }
  },
};
