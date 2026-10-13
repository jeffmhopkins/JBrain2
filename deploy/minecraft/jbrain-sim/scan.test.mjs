// node --test: the Tricorder's live scan (plan §M25, scan.js) against the fake server.
// The supervisor's test_minecraft_jbrain_pack.py lays the pack out beside it and runs this
// file, so CI does too.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { sim, world } from "@minecraft/server";

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

const events = (ev) =>
  sim.logs.filter((l) => l.startsWith("[jbrain] {")).map((l) => JSON.parse(l.slice(9))).filter((e) => e.ev === ev);
const DIAMOND = ["minecraft:diamond_ore", "minecraft:deepslate_diamond_ore"];
const found = (player, x, y, z) => ({
  ev: "scan", op: "found", player, label: "diamond", ids: DIAMOND, dim: "overworld", x, y, z,
});
const none = (player) => ({ ev: "scan", op: "none", player, label: "diamond", ids: DIAMOND });

beforeEach(() => sim.reset());

// First in the file: the report is once per load, and main.js loads once for all of them.
test("after a load every kept scan is reported once, retried until it goes out", () => {
  world.setDynamicProperty(
    "jb:scan:blockyfox",
    JSON.stringify({ block: DIAMOND[0], types: DIAMOND, label: "diamond", player: "BlockyFox" }),
  );
  // Kept before the name was stored with it: named by its key.
  world.setDynamicProperty("jb:scan:mira_p", JSON.stringify({ types: ["minecraft:oak_log"], label: "oak log" }));
  world.setDynamicProperty("jb:waypoint:mira_p", JSON.stringify({ x: 1, z: 2 }));
  const read = world.getDynamicProperty;
  let failed = false;
  world.getDynamicProperty = function (k) {
    if (k.startsWith("jb:scan:") && !failed) {
      failed = true;
      throw new Error("not ready");
    }
    return read.call(this, k);
  };
  sim.tick();
  world.getDynamicProperty = read;
  assert.deepEqual(events("scans"), []);
  assert.equal(sim.logs.filter((l) => l.startsWith("[jbrain] scan report failed, trying again")).length, 1);
  ticks(3);
  assert.deepEqual(events("scans"), [
    {
      ev: "scans",
      all: [
        { player: "BlockyFox", label: "diamond", ids: DIAMOND },
        { player: "mira_p", label: "oak log", ids: ["minecraft:oak_log"] },
      ],
    },
  ]);
});

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

test("the map hears each change: set, found, a nearer block, none, and stop", () => {
  const p = holder("BlockyFox", "-20");
  put(0, 64, -9, "minecraft:diamond_ore");
  scanFor("BlockyFox", "diamond_ore");
  assert.deepEqual(events("scan"), [{ ev: "scan", op: "set", player: "BlockyFox", label: "diamond", ids: DIAMOND }]);
  ticks(1);
  assert.deepEqual(events("scan").at(-1), found("BlockyFox", 0, 64, -9));
  // A steady target costs nothing at each scan.
  ticks(TEN_SECONDS * 2);
  assert.equal(events("scan").length, 2);
  put(3, 60, 0, "minecraft:deepslate_diamond_ore");
  ticks(TEN_SECONDS);
  assert.deepEqual(events("scan").at(-1), found("BlockyFox", 3, 60, 0));
  assert.equal(events("scan").length, 3);
  sim.overworld.blocks.clear();
  ticks(TEN_SECONDS);
  assert.deepEqual(events("scan").at(-1), none("BlockyFox"));
  stop("BlockyFox");
  assert.deepEqual(events("scan").at(-1), { ev: "scan", op: "stop", player: "BlockyFox" });
  // Stopping a scan that isn't on says nothing to the map.
  stop("BlockyFox");
  assert.equal(events("scan").length, 5);
  ticks(1);
  assert.match(shown(p), /World spawn/);
});

test("a mined block is noticed within two seconds with the Tricorder put away", () => {
  const p = holder("Pocket", "-21");
  p.selectedSlotIndex = (p.selectedSlotIndex + 1) % 9;
  put(0, 64, -3, "minecraft:diamond_ore");
  put(0, 64, -9, "minecraft:diamond_ore");
  scanFor("Pocket", "diamond_ore");
  ticks(1);
  assert.deepEqual(events("scan").at(-1), found("Pocket", 0, 64, -3));
  put(0, 64, -3, undefined);
  ticks(8);
  assert.deepEqual(events("scan").at(-1), found("Pocket", 0, 64, -9));
  put(0, 64, -9, undefined);
  ticks(8);
  assert.deepEqual(events("scan").at(-1), none("Pocket"));
});

test("walking out of reach of the found block is noticed within two seconds, held or not", () => {
  const p = holder("Walker", "-23");
  p.selectedSlotIndex = (p.selectedSlotIndex + 1) % 9;
  put(0, 64, -9, "minecraft:diamond_ore");
  scanFor("Walker", "diamond_ore");
  ticks(1);
  assert.deepEqual(events("scan").at(-1), found("Walker", 0, 64, -9));
  p.location = { x: 0.5, y: 64, z: 40.5 };
  ticks(8);
  assert.deepEqual(events("scan").at(-1), none("Walker"));
});

test("a steady target is sent again once a minute, so its time stays true", () => {
  holder("Steady", "-22");
  put(5, 64, 0, "minecraft:diamond_ore");
  scanFor("Steady", "diamond_ore");
  ticks(1);
  ticks(TEN_SECONDS * 5);
  assert.equal(events("scan").filter((e) => e.op === "found").length, 1);
  ticks(TEN_SECONDS);
  assert.equal(events("scan").filter((e) => e.op === "found").length, 2);
  ticks(TEN_SECONDS * 6);
  assert.equal(events("scan").filter((e) => e.op === "found").length, 3);
});

test("a new waypoint ends the scan on the map too; a rejoin sends what it finds afresh", () => {
  const p = holder("Hopper", "-23");
  put(0, 64, -5, "minecraft:diamond_ore");
  scanFor("Hopper", "diamond_ore");
  ticks(1);
  sim.leave(p);
  ticks(TEN_SECONDS);
  sim.join(p);
  ticks(1);
  assert.equal(events("scan").filter((e) => e.op === "found").length, 2);
  waypoint({ player: "Hopper", x: 0, z: -40, dim: "overworld", label: "Home" });
  assert.deepEqual(events("scan").at(-1), { ev: "scan", op: "stop", player: "Hopper" });
  // The track drain's arrival clear sends no end_scan, and the map hears nothing.
  scanFor("Hopper", "diamond_ore");
  const before = events("scan").length;
  waypoint({ player: "Hopper", clear: true });
  assert.equal(events("scan").length, before);
});
