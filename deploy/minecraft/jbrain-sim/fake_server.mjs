// A stand-in for @minecraft/server, enough of it to run the jbrain pack's main.js
// (plan §M9, §M10, §DW1). Strict where the game is: an item read from a container or an
// equipment slot is a copy, so a change only sticks once it is written back. The
// supervisor's test_minecraft_jbrain_pack.py installs it as node_modules/@minecraft/server.

export const ItemLockMode = { none: "none", inventory: "inventory", slot: "slot" };
export const EquipmentSlot = {
  Head: "Head", Chest: "Chest", Legs: "Legs", Feet: "Feet", Offhand: "Offhand", Mainhand: "Mainhand",
};
export const CommandPermissionLevel = { Any: 0 };
export const CustomCommandParamType = { String: "String" };
export const CustomCommandStatus = { Success: 0, Failure: 1 };
// No block types here; replicator_fake.mjs has the blocks the Replicator's tests need.
export const BlockTypes = { get: () => undefined };

export class ItemStack {
  constructor(typeId, amount = 1) {
    this.typeId = typeId;
    this.amount = amount;
    this.nameTag = undefined;
    this.lockMode = ItemLockMode.none;
    this.keepOnDeath = false;
    this.lore = [];
    this.props = {};
    this.enchants = [];
  }
  setLore(lines) {
    this.lore = [...lines];
  }
  getLore() {
    return [...this.lore];
  }
  getDynamicProperty(k) {
    return this.props[k];
  }
  setDynamicProperty(k, v) {
    this.props[k] = v;
  }
  getComponent(name) {
    if (name !== "minecraft:enchantable") return undefined;
    return { hasEnchantment: (id) => this.enchants.includes(id) };
  }
  clone() {
    const c = new ItemStack(this.typeId, this.amount);
    Object.assign(c, { nameTag: this.nameTag, lockMode: this.lockMode, keepOnDeath: this.keepOnDeath });
    c.lore = [...this.lore];
    c.props = { ...this.props };
    c.enchants = [...this.enchants];
    return c;
  }
}

class Container {
  constructor(size) {
    this.size = size;
    this.slots = new Array(size).fill(undefined);
  }
  get emptySlotsCount() {
    return this.slots.filter((s) => !s).length;
  }
  getItem(i) {
    return this.slots[i]?.clone();
  }
  setItem(i, item) {
    this.slots[i] = item && item.amount > 0 ? item.clone() : undefined;
  }
  addItem(item) {
    const i = this.slots.findIndex((s) => !s);
    if (i < 0) return item;
    this.slots[i] = item.clone();
    return undefined;
  }
}

export class Dimension {
  constructor(id = "minecraft:overworld") {
    this.id = id;
    this.heightRange = { min: -64, max: 320 };
    this.spawned = []; // {item, loc}
    this.entities = []; // {id, loc}
  }
  spawnItem(item, loc) {
    this.spawned.push({ item: item.clone(), loc: { ...loc } });
  }
  spawnEntity(id, loc) {
    this.entities.push({ id, loc: { ...loc } });
  }
}

export class Player {
  constructor(name, id, dim) {
    this.name = name;
    this.id = id;
    this.typeId = "minecraft:player";
    this.dimension = dim;
    this.location = { x: 0, y: 64, z: 0 };
    this.view = { x: 0, y: 0, z: -1 }; // facing north
    this.isOnGround = true;
    this.selectedSlotIndex = 0;
    this.level = 0;
    this.inventory = new Container(36);
    this.equipment = {};
    this.props = {};
    this.messages = [];
    this.sounds = [];
    this.actionbar = [];
    this.onScreenDisplay = { setActionBar: (t) => this.actionbar.push(t) };
  }
  getComponent(name) {
    if (name === "minecraft:inventory") return { container: this.inventory };
    if (name === "minecraft:equippable") {
      return {
        getEquipment: (slot) => this.equipment[slot]?.clone(),
        setEquipment: (slot, item) => {
          this.equipment[slot] = item ? item.clone() : undefined;
        },
      };
    }
    return undefined;
  }
  getViewDirection() {
    return { ...this.view };
  }
  getSpawnPoint() {
    return undefined;
  }
  getDynamicProperty(k) {
    return this.props[k];
  }
  setDynamicProperty(k, v) {
    if (v === undefined) delete this.props[k];
    else this.props[k] = v;
  }
  sendMessage(t) {
    this.messages.push(t);
  }
  playSound(id) {
    this.sounds.push(id);
  }
  resetLevel() {
    this.level = 0;
  }
}

const subs = { die: [], spawn: [], script: [], startup: [] };
const intervals = [];
let queued = [];
const props = new Map();
let players = [];
let commands = new Map();
const blockComponents = new Map();
const rules = { keepInventory: false };

export const world = {
  afterEvents: {
    entityDie: { subscribe: (fn) => subs.die.push(fn) },
    playerSpawn: { subscribe: (fn) => subs.spawn.push(fn) },
    // The No-Spawn Beacon's (beacon.test.mjs drives it with its own stand-in).
    entitySpawn: { subscribe: () => {} },
    playerInteractWithEntity: { subscribe: () => {} },
    playerPlaceBlock: { subscribe: () => {} },
    playerBreakBlock: { subscribe: () => {} },
  },
  getDynamicProperty: (k) => props.get(k),
  getDynamicPropertyIds: () => [...props.keys()],
  setDynamicProperty: (k, v) => (v === undefined ? props.delete(k) : props.set(k, v)),
  getAllPlayers: () => [...players],
  getDefaultSpawnLocation: () => ({ x: 0, y: 64, z: 0 }),
  gameRules: rules,
};

export const system = {
  afterEvents: { scriptEventReceive: { subscribe: (fn) => subs.script.push(fn) } },
  beforeEvents: { startup: { subscribe: (fn) => subs.startup.push(fn) } },
  run: (fn) => queued.push(fn),
  runInterval: (fn) => intervals.push(fn),
};

/** Test-side controls, not part of the game API. */
export const sim = {
  logs: [],
  overworld: new Dimension(),
  reset() {
    props.clear();
    players = [];
    queued = [];
    rules.keepInventory = false;
    sim.logs.length = 0;
    sim.overworld = new Dimension();
  },
  startup() {
    commands = new Map();
    for (const fn of subs.startup) {
      fn({
        customCommandRegistry: { registerCommand: (def, cb) => commands.set(def.name, { def, cb }) },
        blockComponentRegistry: { registerCustomComponent: (name, c) => blockComponents.set(name, c) },
      });
    }
  },
  command: (name) => commands.get(name),
  blockComponent: (name) => blockComponents.get(name),
  player(name, id) {
    const p = new Player(name, id, sim.overworld);
    players.push(p);
    return p;
  },
  flush() {
    while (queued.length) queued.shift()();
  },
  spawn(player, initialSpawn) {
    for (const fn of subs.spawn) fn({ player, initialSpawn });
    sim.flush();
  },
  die(player) {
    for (const fn of subs.die) fn({ deadEntity: player, damageSource: { cause: "fall" } });
    sim.flush();
  },
  scriptevent(id, message) {
    for (const fn of subs.script) fn({ id, message });
  },
  tick() {
    for (const fn of intervals) fn();
  },
  props,
  rules,
};

const realLog = console.log;
console.log = (...a) => {
  sim.logs.push(a.join(" "));
  if (process.env.JB_SIM_ECHO) realLog(...a);
};
