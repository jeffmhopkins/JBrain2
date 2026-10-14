// node --test: each player's respawn point for the map (plan §M8, beds): the pure rules
// (spawnpoint_rules.js) and spawnpoint.js against a small stand-in of its own for the game
// API. The supervisor's test_minecraft_jbrain_pack.py runs this file.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import * as r from "../jbrain-pack/scripts/spawnpoint_rules.js";
import { initSpawnPoints } from "../jbrain-pack/scripts/spawnpoint.js";

const mc = (t) => `minecraft:${t}`;

// --- the rules -------------------------------------------------------------------------

test("a bed of any colour and a respawn anchor are spawn blocks; nothing else is", () => {
  assert.equal(r.blockKind(mc("bed")), "bed");
  assert.equal(r.blockKind(mc("red_bed")), "bed");
  assert.equal(r.blockKind(mc("respawn_anchor")), "anchor");
  for (const t of [mc("air"), mc("bedrock"), mc("flower_bed"), "jbrain:bed", undefined, 3]) {
    assert.equal(r.blockKind(t), null, String(t));
  }
});

test("the kind is the block at the point or touching it; unread blocks say nothing", () => {
  const at = (cells) => (dx, dy, dz) => cells[`${dx},${dy},${dz}`] ?? mc("air");
  assert.equal(r.kindAt(at({ "0,0,0": mc("bed") })), "bed");
  assert.equal(r.kindAt(at({ "1,0,0": mc("bed") })), "bed"); // woken beside it
  assert.equal(r.kindAt(at({ "0,-1,0": mc("respawn_anchor") })), "anchor"); // standing on it
  assert.equal(r.kindAt(at({ "2,0,0": mc("bed") })), "other"); // too far to be it
  assert.equal(r.kindAt(at({})), "other"); // a /spawnpoint, or the bed is gone
  assert.equal(r.kindAt(() => undefined), null); // an unloaded chunk
  assert.equal(r.kindAt(() => { throw new Error("LocationInUnloadedChunkError"); }), null);
  assert.equal(r.NEAR.length, 27);
  assert.deepEqual(r.NEAR[0], [0, 0, 0]);
});

test("a line is whole blocks in a known dimension, and none carries no coordinates", () => {
  assert.deepEqual(r.spawnLine("Mira_P", { dim: "nether", x: -20.7, y: 71.2, z: 40.5 }, "anchor"),
    { ev: "spawnpoint", player: "Mira_P", kind: "anchor", dim: "nether", x: -21, y: 71, z: 40 });
  assert.deepEqual(r.spawnLine("Steve42", null), { ev: "spawnpoint", player: "Steve42", kind: "none" });
  assert.deepEqual(r.KINDS, ["bed", "anchor", "other", "none", "unknown"]);
  assert.equal(r.CHECK_TICKS, 200); // ten seconds
});

// --- spawnpoint.js against a stand-in -------------------------------------------------------

class Dim {
  constructor(id) {
    Object.assign(this, { id, blocks: {}, loaded: true });
  }
  getBlock({ x, y, z }) {
    if (!this.loaded) throw new Error("LocationInUnloadedChunkError");
    return { typeId: this.blocks[`${x},${y},${z}`] ?? mc("air") };
  }
}

class Player {
  constructor(name, dim) {
    Object.assign(this, { name, dimension: dim, isValid: true, spawn: undefined });
  }
  getSpawnPoint() {
    return this.spawn;
  }
}

let ow;
let nether;
let ev;
let queued;
let checks;
let players;
const logs = [];
console.log = (...a) => logs.push(a.join(" "));

beforeEach(() => {
  ow = new Dim(mc("overworld"));
  nether = new Dim(mc("nether"));
  ev = {};
  queued = [];
  checks = [];
  players = [];
  logs.length = 0;
  const on = (name) => ({ subscribe: (fn) => (ev[name] = fn) });
  const world = {
    afterEvents: Object.fromEntries(["playerInteractWithBlock", "playerSpawn", "playerLeave"]
      .map((n) => [n, on(n)])),
    getAllPlayers: () => [...players],
  };
  initSpawnPoints(world, {
    run: (fn) => queued.push(fn),
    runInterval: (fn, every) => checks.push({ fn, every }),
  });
});

const lines = () => logs.filter((l) => l.startsWith("[jbrain] {")).map((l) => JSON.parse(l.slice(9)));
const tick = () => {
  while (queued.length) queued.shift()();
};
const check = () => checks.forEach((c) => c.fn());
const join = (p) => {
  players.push(p);
  ev.playerSpawn({ player: p, initialSpawn: true });
  tick();
};
const sleep = (p, at) => {
  ow.blocks[`${at.x},${at.y},${at.z}`] = mc("bed");
  ev.playerInteractWithBlock({ player: p, block: { typeId: mc("bed") }, isFirstEvent: true });
  p.spawn = { dimension: ow, ...at };
  tick();
};

test("joining always says where they'll wake, even unchanged and even with none", () => {
  const p = new Player("Steve42", ow);
  join(p);
  assert.deepEqual(lines(), [{ ev: "spawnpoint", player: "Steve42", kind: "none" }]);
  check();
  assert.equal(lines().length, 1); // nothing changed: nothing sent
  ev.playerLeave({ playerName: "Steve42" });
  players = [];
  join(p);
  assert.equal(lines().length, 2); // the rejoin resends
});

test("sleeping in a bed sends it a tick later; the check sends nothing more", () => {
  const p = new Player("BlockyFox", ow);
  join(p);
  sleep(p, { x: 70, y: 64, z: 40 });
  assert.deepEqual(lines().at(-1),
    { ev: "spawnpoint", player: "BlockyFox", kind: "bed", dim: "overworld", x: 70, y: 64, z: 40 });
  check();
  check();
  assert.equal(lines().length, 2);
  assert.equal(checks.length, 1);
  assert.equal(checks[0].every, 200);
});

test("a used block that isn't a bed or an anchor is not a reason to look", () => {
  const p = new Player("BlockyFox", ow);
  join(p);
  ev.playerInteractWithBlock({ player: p, block: { typeId: mc("chest") }, isFirstEvent: true });
  ev.playerInteractWithBlock({ player: p, block: { typeId: mc("bed") }, isFirstEvent: false });
  assert.equal(queued.length, 0);
});

test("a bed broken while the spawn point stays is sent as other: not seen there", () => {
  const p = new Player("BlockyFox", ow);
  join(p);
  sleep(p, { x: 70, y: 64, z: 40 });
  delete ow.blocks["70,64,40"];
  check();
  assert.deepEqual(lines().at(-1),
    { ev: "spawnpoint", player: "BlockyFox", kind: "other", dim: "overworld", x: 70, y: 64, z: 40 });
  // The respawn that follows clears it in the game; the check after it says so.
  p.spawn = undefined;
  ev.playerSpawn({ player: p, initialSpawn: false });
  tick();
  assert.deepEqual(lines().at(-1), { ev: "spawnpoint", player: "BlockyFox", kind: "none" });
});

test("an unloaded chunk keeps what was known of the same point, and a new one is unknown", () => {
  const p = new Player("BlockyFox", ow);
  join(p);
  sleep(p, { x: 70, y: 64, z: 40 });
  ow.loaded = false;
  check();
  assert.equal(lines().length, 2); // still the bed: nothing new to say
  p.spawn = { dimension: ow, x: 500, y: 70, z: 500 }; // a /spawnpoint far off
  check();
  assert.equal(lines().at(-1).kind, "unknown");
  ow.loaded = true;
  ow.blocks["500,70,501"] = mc("bed"); // the chunk loads: a bed beside the point after all
  check();
  assert.equal(lines().at(-1).kind, "bed");
});

test("a respawn anchor in the Nether is sent with its dimension", () => {
  const p = new Player("Mira_P", nether);
  join(p);
  nether.blocks["-20,71,40"] = mc("respawn_anchor");
  ev.playerInteractWithBlock({ player: p, block: { typeId: mc("respawn_anchor") } });
  p.spawn = { dimension: nether, x: -20, y: 71, z: 40 };
  tick();
  assert.deepEqual(lines().at(-1),
    { ev: "spawnpoint", player: "Mira_P", kind: "anchor", dim: "nether", x: -20, y: 71, z: 40 });
});

test("a player gone, or a spawn point that throws, sends nothing and stops nothing", () => {
  const gone = new Player("Gone", ow);
  gone.isValid = false;
  const odd = new Player("Odd", ow);
  odd.getSpawnPoint = () => {
    throw new Error("no");
  };
  const fine = new Player("Fine", ow);
  players.push(gone, odd, fine);
  check();
  assert.deepEqual(lines(), [{ ev: "spawnpoint", player: "Fine", kind: "none" }]);
  assert.ok(logs.some((l) => l.includes("spawn point unreadable for Odd")));
});

test("a join with the bed's chunk unloaded and nothing remembered sends unknown, not other", () => {
  // A previous session confirmed the bed; this one starts empty (a rejoin, a reload).
  const p = new Player("BlockyFox", ow);
  join(p);
  sleep(p, { x: 70, y: 64, z: 40 });
  ev.playerLeave({ playerName: "BlockyFox" });
  players = [];
  ow.loaded = false;
  join(p);
  assert.deepEqual(lines().at(-1),
    { ev: "spawnpoint", player: "BlockyFox", kind: "unknown", dim: "overworld", x: 70, y: 64, z: 40 });
  // Still unreadable: nothing new to say. Readable again: the real kind, as usual.
  check();
  assert.equal(lines().at(-1).kind, "unknown");
  const n = lines().length;
  check();
  assert.equal(lines().length, n);
  ow.loaded = true;
  check();
  assert.equal(lines().at(-1).kind, "bed");
});

test("a bed broken while its player was away is corrected by the first readable check", () => {
  const p = new Player("BlockyFox", ow);
  join(p);
  sleep(p, { x: 70, y: 64, z: 40 });
  ev.playerLeave({ playerName: "BlockyFox" });
  players = [];
  ow.loaded = false;
  join(p);
  assert.equal(lines().at(-1).kind, "unknown");
  delete ow.blocks["70,64,40"];
  ow.loaded = true;
  check();
  assert.equal(lines().at(-1).kind, "other");
});
