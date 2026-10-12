// node --test: the Tricorder's live scan (plan §M25, scan.js) against the fake server.
// The supervisor's test_minecraft_jbrain_pack.py lays the pack out beside it and runs this
// file, so CI does too.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { sim } from "@minecraft/server";

await import("./pack/scripts/main.js");
sim.startup();

const WELCOME_TICKS = 500;
// main.js's loop runs every 5 ticks; sim.tick() is one of its frames, so 10 s is 40.
const TEN_SECONDS = 40;

// A player past the welcome, holding their own Tricorder, standing at y 64.
const holder = (name, id) => {
  const p = sim.player(name, id);
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  p.selectedSlotIndex = p.inventory.slots.findIndex((s) => s?.typeId === "jbrain:tricorder");
  p.location = { x: 0.5, y: 64, z: 0.5 };
  return p;
};
const scanFor = (name, block) => sim.scriptevent("jb:scan", JSON.stringify({ player: name, block }));
const stop = (name) => sim.scriptevent("jb:scan", JSON.stringify({ player: name, stop: true }));
const waypoint = (msg) => sim.scriptevent("jb:waypoint", JSON.stringify(msg));
const ticks = (n) => {
  for (let i = 0; i < n; i++) sim.tick();
};
const shown = (p) => p.actionbar.at(-1);
const put = (x, y, z, t) => sim.overworld.setBlockAt({ x, y, z }, t);

beforeEach(() => sim.reset());

test("the arrow points at the nearest matching block, by 3D distance", () => {
  const p = holder("Steve 42", "-1");
  put(0, 64, -10, "minecraft:diamond_ore"); // 10 north
  put(2, 58, 0, "minecraft:diamond_ore"); // 6 down, 2 east: nearer
  put(3, 64, 0, "minecraft:stone");
  scanFor("Steve 42", "diamond_ore");
  assert.ok(sim.logs.includes(
    "[jbrain] scan set for Steve 42: diamond (minecraft:diamond_ore, minecraft:deepslate_diamond_ore)",
  ));
  ticks(1);
  assert.equal(shown(p), "→ Scanning: diamond · You're here · y 58");
  // The first scan after a start tells the console what it cost (the box check).
  assert.ok(sim.logs.some((l) => /^\[jbrain\] scan took \d+ ms for Steve 42 \(diamond\)$/.test(l)));
  const [call] = sim.overworld.getBlocksCalls;
  assert.deepEqual(call.volume.from, { x: -16, y: 48, z: -16 });
  assert.deepEqual(call.volume.to, { x: 16, y: 80, z: 16 });
  assert.equal(call.allowUnloadedChunks, true);
});

test("an ore brings its deepslate twin, and the twin brings the ore", () => {
  const p = holder("Twin", "-2");
  put(0, 64, -5, "minecraft:deepslate_diamond_ore");
  scanFor("Twin", "diamond_ore");
  ticks(1);
  assert.equal(shown(p), "↑ Scanning: diamond · 5 blocks · y 64");
  stop("Twin");
  sim.overworld.blocks.clear();
  put(5, 64, 0, "minecraft:iron_ore");
  scanFor("Twin", "minecraft:deepslate_iron_ore");
  ticks(1);
  assert.equal(shown(p), "→ Scanning: iron · 5 blocks · y 64");
});

test("nothing within 16 blocks: no arrow, a none-nearby line", () => {
  const p = holder("Empty", "-3");
  put(17, 64, 0, "minecraft:diamond_ore"); // one past the reach
  put(0, 64 + 17, 0, "minecraft:diamond_ore");
  scanFor("Empty", "diamond_ore");
  ticks(1);
  assert.equal(shown(p), "Scanning: diamond · none nearby");
  // The reach is 16 each way, the box's corner included.
  put(16, 48, -16, "minecraft:diamond_ore");
  ticks(TEN_SECONDS);
  assert.equal(shown(p), "↗ Scanning: diamond · 23 blocks · y 48");
});

test("a scan every 10 seconds: a new block shows at the next scan, not before", () => {
  const p = holder("Cadence", "-4");
  scanFor("Cadence", "oak_log");
  ticks(1);
  const calls = () => sim.overworld.getBlocksCalls.length;
  assert.equal(calls(), 1);
  put(0, 64, -3, "minecraft:oak_log");
  ticks(TEN_SECONDS - 1);
  assert.equal(calls(), 1);
  assert.equal(shown(p), "Scanning: oak log · none nearby");
  ticks(1);
  assert.equal(calls(), 2);
  assert.equal(shown(p), "↑ Scanning: oak log · 3 blocks · y 64");
  ticks(TEN_SECONDS * 3);
  assert.equal(calls(), 5);
});

test("a mined target drops the arrow at once and is looked past within a second", () => {
  const p = holder("Miner", "-5");
  put(0, 64, -3, "minecraft:diamond_ore");
  put(0, 64, -9, "minecraft:diamond_ore");
  scanFor("Miner", "diamond_ore");
  ticks(1);
  assert.equal(shown(p), "↑ Scanning: diamond · 3 blocks · y 64");
  put(0, 64, -3, undefined);
  ticks(1);
  assert.equal(shown(p), "Scanning: diamond · none nearby");
  ticks(4);
  assert.equal(shown(p), "↑ Scanning: diamond · 9 blocks · y 64");
});

test("stopping points back at the waypoint they had, or spawn", () => {
  const p = holder("Stopper", "-6");
  waypoint({ player: "Stopper", x: 100, z: 0, dim: "overworld", label: "Base" });
  scanFor("Stopper", "diamond_ore");
  ticks(1);
  assert.equal(shown(p), "Scanning: diamond · none nearby");
  stop("Stopper");
  assert.equal(sim.logs.at(-1), "[jbrain] scan stopped for Stopper");
  ticks(1);
  assert.equal(shown(p), "→ Base · 100 blocks");

  const q = holder("NoWay", "-7");
  q.location = { x: 0, y: 64, z: 50 };
  scanFor("NoWay", "stone");
  stop("NoWay");
  stop("NoWay");
  assert.equal(sim.logs.at(-1), "[jbrain] scan stopped for NoWay (none was on)");
  ticks(1);
  assert.equal(shown(q), "↑ World spawn · 50 blocks");
});

test("a new waypoint replaces the scan; reaching the old one doesn't end it", () => {
  const p = holder("Wayfarer", "-8");
  scanFor("Wayfarer", "diamond_ore");
  // The track drain's clear on arrival carries no end_scan.
  waypoint({ player: "Wayfarer", clear: true });
  assert.equal(sim.logs.at(-1), "[jbrain] waypoint cleared for Wayfarer");
  ticks(1);
  assert.equal(shown(p), "Scanning: diamond · none nearby");
  waypoint({ player: "Wayfarer", x: 0, z: -40, dim: "overworld", label: "Home" });
  assert.equal(sim.logs.at(-1), "[jbrain] waypoint set for Wayfarer: Home at 0 -40 (overworld); scan ended");
  ticks(1);
  assert.equal(shown(p), "↑ Home · 41 blocks");
  // Dave's clear ("clear my arrow") is a new heading too: spawn.
  scanFor("Wayfarer", "diamond_ore");
  waypoint({ player: "Wayfarer", clear: true, end_scan: true });
  assert.equal(sim.logs.at(-1), "[jbrain] waypoint cleared for Wayfarer; scan ended");
  ticks(1);
  assert.match(shown(p), /World spawn/);
});

test("a scan lasts across a rejoin, set while offline too", () => {
  const p = holder("Rejoin", "-9");
  scanFor("Rejoin", "diamond_ore");
  ticks(1);
  sim.leave(p);
  ticks(TEN_SECONDS * 2);
  put(0, 64, -4, "minecraft:diamond_ore");
  sim.join(p);
  ticks(1);
  assert.equal(shown(p), "↑ Scanning: diamond · 4 blocks · y 64");

  scanFor("Later", "oak_log"); // not online: kept for when they are
  assert.equal(sim.logs.at(-1), "[jbrain] scan set for Later: oak log (minecraft:oak_log)");
  const q = holder("Later", "-10");
  ticks(1);
  assert.equal(shown(q), "Scanning: oak log · none nearby");
});

test("an unknown block is refused; a bare ore name means the ore", () => {
  const p = holder("Picky", "-11");
  scanFor("Picky", "unobtainium");
  assert.equal(sim.logs.at(-1), "[jbrain] scan refused: minecraft:unobtainium isn't a block");
  sim.scriptevent("jb:scan", JSON.stringify({ player: "Picky", block: 'x"; op @a' }));
  assert.match(sim.logs.at(-1), /^\[jbrain\] scan refused: Error: not a block id/);
  ticks(1);
  assert.match(shown(p), /World spawn/);
  scanFor("Picky", "diamond");
  assert.match(sim.logs.at(-1), /^\[jbrain\] scan set for Picky: diamond \(minecraft:diamond_ore,/);
});

test("players scanning together are scanned on frames of their own", () => {
  const a = holder("A", "-12");
  const b = holder("B", "-13");
  const c = holder("C", "-14");
  const order = [];
  const real = sim.overworld.getBlocks.bind(sim.overworld);
  sim.overworld.getBlocks = (v, f, u) => {
    order.push({ frame, x: v.from.x });
    return real(v, f, u);
  };
  a.location = { x: 0.5, y: 64, z: 0.5 };
  b.location = { x: 1000.5, y: 64, z: 0.5 };
  c.location = { x: 2000.5, y: 64, z: 0.5 };
  let frame = 0;
  for (const n of ["A", "B", "C"]) scanFor(n, "stone");
  for (; frame < TEN_SECONDS * 3; frame++) sim.tick();
  const frames = order.map((o) => o.frame);
  assert.equal(new Set(frames).size, frames.length, "never two on one frame");
  assert.deepEqual(order.slice(0, 3).map((o) => o.frame), [0, 1, 2]);
  // Each player still every 10 seconds.
  for (const x of [-16, 984, 1984]) {
    const mine = order.filter((o) => o.x === x).map((o) => o.frame);
    assert.deepEqual(mine.slice(1).map((f, i) => f - mine[i]), [TEN_SECONDS, TEN_SECONDS]);
  }
});
