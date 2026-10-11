// node --test: the No-Spawn Beacon (plan §M16): its pure rules (beacon_rules.js), its
// unlock in Dave's guide (rules.js), and beacon.js against a small stand-in of its own
// for the game API. The supervisor's test_minecraft_jbrain_pack.py runs this file.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import * as b from "../jbrain-pack/scripts/beacon_rules.js";
import { initBeacon } from "../jbrain-pack/scripts/beacon.js";
import { unlocksFrom } from "../jbrain-pack/scripts/rules.js";

const mc = (t) => `minecraft:${t}`;
const wild = (typeId, extra = {}) => ({
  typeId, tamed: false, named: false, leashed: false, kept: false, ...extra,
});

// --- the decision table -----------------------------------------------------------------

test("hostile, neutral and wild passive mobs go, the Warden and teleporting endermen too", () => {
  for (const t of ["zombie", "skeleton", "creeper", "spider", "phantom", "slime", "enderman",
    "piglin", "zombie_pigman", "witch", "pillager", "ravager", "warden", "elder_guardian",
    "wolf", "bee", "llama", "cow", "sheep", "pig", "chicken", "horse", "squid", "bat",
    "zombie_villager_v2"]) {
    assert.equal(b.keepReason(wild(mc(t))), null, t);
  }
});

test("the crew, villagers, golems and the great bosses always stay", () => {
  for (const t of ["player", "villager", "villager_v2", "wandering_trader", "iron_golem",
    "snow_golem", "copper_golem", "ender_dragon", "wither"]) {
    assert.equal(b.keepReason(wild(mc(t))), "always kept", t);
  }
  assert.ok(!b.ALWAYS_KEPT.has(mc("warden")));
});

test("tamed, name-tagged, leashed, interacted-with and bred mobs stay", () => {
  assert.equal(b.keepReason(wild(mc("wolf"), { tamed: true })), "tamed");
  assert.equal(b.keepReason(wild(mc("zombie"), { named: true })), "name tag");
  assert.equal(b.keepReason(wild(mc("cow"), { leashed: true })), "on a lead");
  assert.equal(b.keepReason(wild(mc("sheep"), { kept: true })), "a player looked after it");
});

test("anything that isn't a listed mob is never touched", () => {
  for (const t of ["item", "xp_orb", "arrow", "boat", "minecart", "armor_stand",
    "ender_crystal", "painting"]) {
    assert.equal(b.keepReason(wild(mc(t))), "not a mob the beacon removes", t);
  }
  assert.equal(b.keepReason(wild("jbrain:something")), "not a mob the beacon removes");
  const overlap = [...b.HOSTILE, ...b.NEUTRAL, ...b.PASSIVE].filter((t) => b.ALWAYS_KEPT.has(t));
  assert.deepEqual(overlap, []);
});

// --- chunk-area math --------------------------------------------------------------------

test("a beacon covers its own chunk and the eight round it: 48 × 48 blocks", () => {
  assert.deepEqual(b.coveredChunks({ x: 5, z: 5 }), [
    "-1,-1", "0,-1", "1,-1", "-1,0", "0,0", "1,0", "-1,1", "0,1", "1,1",
  ]);
  assert.deepEqual(b.areaOf({ x: 5, z: 5 }), { x: -16, z: -16, size: 48 });
  assert.deepEqual(b.areaOf({ x: 31, z: 16 }), { x: 0, z: 0, size: 48 });
});

test("negative coordinates floor into the right chunk", () => {
  assert.equal(b.chunkOf(-1), -1);
  assert.equal(b.chunkOf(-16), -1);
  assert.equal(b.chunkOf(-17), -2);
  assert.equal(b.chunkOf(-0.5), -1);
  assert.deepEqual(b.areaOf({ x: -1, z: -17 }), { x: -32, z: -48, size: 48 });
  const keys = b.coveredChunks({ x: -1, z: -17 });
  assert.ok(keys.includes("-1,-2") && keys.includes("-2,-3") && keys.includes("0,-1"));
  assert.equal(new Set(keys).size, 9);
});

test("the protected set covers exactly the area, per dimension", () => {
  const idx = b.buildIndex([b.makeRecord(mc("overworld"), { x: -1, y: 70, z: -17 })]);
  const ow = mc("overworld");
  assert.ok(b.isProtected(idx, ow, { x: -32, z: -48 }));
  assert.ok(b.isProtected(idx, ow, { x: 15.9, z: -1.1 }));
  assert.ok(!b.isProtected(idx, ow, { x: 16, z: -20 }));
  assert.ok(!b.isProtected(idx, ow, { x: -33, z: -20 }));
  assert.ok(!b.isProtected(idx, ow, { x: -10, z: 0 }));
  assert.ok(!b.isProtected(idx, mc("nether"), { x: -1, z: -17 }));
});

test("records round-trip and anything unreadable is ignored", () => {
  const rec = b.makeRecord(mc("the_end"), { x: 10.7, y: 200.2, z: -3.5 });
  assert.deepEqual([rec.x, rec.y, rec.z], [10, 200, -4]);
  assert.equal(b.recordKey(mc("the_end"), rec), "jb:beacon:minecraft:the_end@10,200,-4");
  assert.deepEqual(b.parseRecord(JSON.stringify(rec)), rec);
  for (const raw of [undefined, 3, "nope", "{}", '{"dim":"x","x":1,"y":2}']) {
    assert.equal(b.parseRecord(raw), undefined, String(raw));
  }
});

// --- the guide's unlock (§M19) ----------------------------------------------------------

test("an echo shard unlocks the beacon only once a Power Pack has been made", () => {
  assert.deepEqual(unlocksFrom([], [mc("echo_shard")]), ["tricorder"]);
  assert.deepEqual(unlocksFrom(["power_pack_uses", "tricorder"], [mc("echo_shard")]),
    ["no_spawn_beacon", "power_pack_uses", "tricorder"]);
  assert.deepEqual(unlocksFrom([], [mc("echo_shard"), "jbrain:power_pack"]),
    ["no_spawn_beacon", "power_pack_uses", "tricorder"]);
});

// --- beacon.js against a stand-in -------------------------------------------------------

class Entity {
  constructor(dim, typeId, location, extra = {}) {
    Object.assign(this, { dimension: dim, typeId, location, nameTag: "", isValid: true });
    this.props = {};
    this.components = extra.components ?? {};
  }
  getComponent(id) {
    return this.components[id];
  }
  getDynamicProperty(k) {
    return this.props[k];
  }
  setDynamicProperty(k, v) {
    this.props[k] = v;
  }
  remove() {
    this.isValid = false;
    this.dimension.entities = this.dimension.entities.filter((e) => e !== this);
  }
}

const inBox = (p, q) =>
  ["x", "y", "z"].every((a) => p[a] >= q.location[a] && p[a] < q.location[a] + q.volume[a]);

class Dim {
  constructor(id) {
    Object.assign(this, { id, heightRange: { min: -64, max: 320 }, entities: [], blocks: {},
      loaded: true, particles: [], queries: [] });
  }
  add(typeId, location, extra) {
    const e = new Entity(this, typeId, location, extra);
    this.entities.push(e);
    return e;
  }
  getBlock({ x, y, z }) {
    if (!this.loaded) return undefined;
    return { typeId: this.blocks[`${x},${y},${z}`] ?? "minecraft:air" };
  }
  getEntities(q) {
    this.queries.push(q);
    return this.entities.filter((e) => !q.excludeTypes.includes(e.typeId) && inBox(e.location, q));
  }
  spawnParticle(id, at) {
    this.particles.push({ id, at });
  }
}

let world;
let ticks;
let ev;
let props;
let ow;
const logs = [];
console.log = (...a) => logs.push(a.join(" "));

beforeEach(() => {
  props = new Map();
  ow = new Dim(mc("overworld"));
  ev = {};
  ticks = [];
  logs.length = 0;
  const on = (name) => ({ subscribe: (fn) => (ev[name] = fn) });
  world = {
    afterEvents: Object.fromEntries(["entitySpawn", "playerInteractWithEntity",
      "playerPlaceBlock", "playerBreakBlock"].map((n) => [n, on(n)])),
    getDynamicProperty: (k) => props.get(k),
    setDynamicProperty: (k, v) => (v === undefined ? props.delete(k) : props.set(k, v)),
    getDynamicPropertyIds: () => [...props.keys()],
    getDimension: (id) => (id === ow.id ? ow : new Dim(id)),
  };
  initBeacon(world, { runInterval: (fn, every) => ticks.push({ fn, every }) });
});

const place = (x, y, z) => {
  ow.blocks[`${x},${y},${z}`] = b.BEACON;
  ev.playerPlaceBlock({ block: { typeId: b.BEACON, location: { x, y, z } }, dimension: ow });
};
const sweep = () => ticks.forEach((t) => t.fn());

test("placing records the beacon in a world property; breaking removes it", () => {
  place(-5, 70, -20);
  const key = "jb:beacon:minecraft:overworld@-5,70,-20";
  assert.deepEqual(JSON.parse(props.get(key)), { dim: mc("overworld"), x: -5, y: 70, z: -20 });
  assert.ok(logs.some((l) => l.includes("no-spawn beacon on at -5 70 -20")));
  // Another block placed or broken changes nothing.
  ev.playerPlaceBlock({ block: { typeId: mc("stone"), location: { x: 0, y: 0, z: 0 } }, dimension: ow });
  ev.playerBreakBlock({ block: { location: { x: -5, y: 70, z: -20 } },
    brokenBlockPermutation: { type: { id: mc("stone") } }, dimension: ow });
  assert.ok(props.has(key));
  ev.playerBreakBlock({ block: { location: { x: -5, y: 70, z: -20 } },
    brokenBlockPermutation: { type: { id: b.BEACON } }, dimension: ow });
  assert.ok(!props.has(key));
  // Off means off: a zombie spawning there now stays.
  const z = ow.add(mc("zombie"), { x: -5, y: 70, z: -20 });
  ev.entitySpawn({ entity: z, cause: "Spawned" });
  assert.ok(z.isValid);
});

test("a hostile spawn in the area vanishes in a puff; outside it, or a kept mob, stays", () => {
  place(8, 64, 8);
  const inside = ow.add(mc("creeper"), { x: 31.9, y: -50, z: -16 });
  const outside = ow.add(mc("creeper"), { x: 32, y: 64, z: 8 });
  const villager = ow.add(mc("villager_v2"), { x: 1, y: 64, z: 1 });
  for (const entity of [inside, outside, villager]) ev.entitySpawn({ entity, cause: "Spawned" });
  assert.ok(!inside.isValid);
  assert.ok(outside.isValid && villager.isValid);
  assert.deepEqual(ow.particles.map((p) => p.id), ["minecraft:endrod", "minecraft:basic_smoke_particle"]);
});

test("the sweep removes an enderman that teleported in, and leaves the looked-after", () => {
  place(0, 100, 0);
  sweep();
  const enderman = ow.add(mc("enderman"), { x: 20, y: 300, z: -15 }); // no spawn event
  const pet = ow.add(mc("wolf"), { x: 1, y: 100, z: 1 }, { components: { "minecraft:is_tamed": {} } });
  const leashed = ow.add(mc("cow"), { x: 2, y: 100, z: 2 }, {
    components: { "minecraft:leashable": { isLeashed: true } },
  });
  const named = ow.add(mc("zombie"), { x: 3, y: 100, z: 3 });
  named.nameTag = "Bob";
  const fed = ow.add(mc("sheep"), { x: 4, y: 100, z: 4 });
  ev.playerInteractWithEntity({ target: fed });
  const dragon = ow.add(mc("ender_dragon"), { x: 5, y: 150, z: 5 });
  const warden = ow.add(mc("warden"), { x: -16, y: -60, z: -16 });
  const wildCow = ow.add(mc("cow"), { x: 6, y: 100, z: 6 });
  sweep();
  assert.ok(!enderman.isValid && !warden.isValid && !wildCow.isValid);
  for (const e of [pet, leashed, named, fed, dragon]) assert.ok(e.isValid, e.typeId);
  const [q] = ow.queries;
  assert.deepEqual(q.location, { x: -16, y: -64, z: -16 });
  assert.deepEqual(q.volume, { x: 48, y: 384, z: 48 });
  assert.deepEqual(q.excludeTypes, [mc("player")]);
});

test("a bred animal is kept from birth, in the area or not", () => {
  place(0, 64, 0);
  const calf = ow.add(mc("cow"), { x: 1, y: 64, z: 1 });
  ev.entitySpawn({ entity: calf, cause: "Born" });
  sweep();
  assert.ok(calf.isValid);
  assert.equal(calf.getDynamicProperty(b.KEPT), true);
});

test("an unloaded area is skipped; a beacon gone without a break (TNT) is forgotten", () => {
  place(0, 64, 0);
  ow.loaded = false;
  const zombie = ow.add(mc("zombie"), { x: 1, y: 64, z: 1 });
  sweep();
  assert.ok(zombie.isValid);
  assert.equal(ow.queries.length, 0);
  ow.loaded = true;
  delete ow.blocks["0,64,0"];
  sweep();
  assert.ok(zombie.isValid);
  assert.equal([...props.keys()].filter((k) => k.startsWith(b.RECORD_PREFIX)).length, 0);
  assert.ok(logs.some((l) => l.includes("no-spawn beacon off at 0 64 0") && l.includes("gone")));
});

test("records are read back after a restart; the sweep runs once a second", () => {
  props.set("jb:beacon:minecraft:overworld@0,64,0", JSON.stringify({ dim: mc("overworld"), x: 0, y: 64, z: 0 }));
  ow.blocks["0,64,0"] = b.BEACON;
  const slime = ow.add(mc("slime"), { x: 10, y: 20, z: 10 });
  ev.entitySpawn({ entity: slime, cause: "Spawned" });
  assert.ok(!slime.isValid);
  assert.equal(ticks.length, 1);
  assert.equal(ticks[0].every, 20);
});
