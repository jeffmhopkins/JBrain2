// node --test: the jbrain pack's pure rules (rules.js). The supervisor's
// test_minecraft_jbrain_pack.py runs this file, so CI does too.
import assert from "node:assert/strict";
import { test } from "node:test";
import * as r from "../jbrain-pack/scripts/rules.js";

const N = { x: 0, z: -1 }; // facing north (-z)

test("the arrow points at all eight octants relative to where the player faces", () => {
  const here = { x: 0, z: 0 };
  const at = [[0, -10], [10, -10], [10, 0], [10, 10], [0, 10], [-10, 10], [-10, 0], [-10, -10]];
  at.forEach(([x, z], i) => assert.equal(r.arrowFor(here, N, x, z).arrow, r.ARROWS[i], `${x},${z}`));
  // Facing east, a target to the north is to the left.
  assert.equal(r.arrowFor(here, { x: 1, z: 0 }, 0, -10).arrow, "←");
  assert.equal(r.arrowFor(here, N, 30, 40).blocks, 50);
});

test("the bearing wraps at ±180°: just either side of due south is still ↓", () => {
  const here = { x: 0, z: 0 };
  assert.equal(r.arrowFor(here, N, 0.01, 100).arrow, "↓");
  assert.equal(r.arrowFor(here, N, -0.01, 100).arrow, "↓");
  // Facing a hair west of south, a target a hair east of south is dead ahead.
  assert.equal(r.arrowFor(here, { x: -0.01, z: 1 }, 0.01, 100).arrow, "↑");
});

test("the tracker line: spawn with no waypoint, arrival, depth and other dimensions", () => {
  const here = { dim: "minecraft:overworld", x: 0, z: 0 };
  assert.equal(r.trackerLine(undefined, here, N, { x: 0, z: -40 }), "↑ World spawn · 40 blocks");
  const wp = { x: 1, y: -58, z: -1, dim: "overworld", label: "Diamonds" };
  assert.equal(r.trackerLine(wp, here, N, {}), "↗ Diamonds · You're here · y -58");
  assert.equal(r.trackerLine({ ...wp, z: -100, y: null }, here, N, {}), "↑ Diamonds · 100 blocks");
  assert.equal(r.trackerLine({ ...wp, dim: "nether" }, here, N, {}), "Diamonds is in the Nether");
});

test("a Tricorder is its owner's only, and only the newest copy", () => {
  assert.equal(r.tricorderStatus({ owner: "-1", serial: 2 }, "-1", 2), "own");
  assert.equal(r.tricorderStatus({ owner: "-1", serial: 1 }, "-1", 2), "retired");
  assert.equal(r.tricorderStatus({ owner: "-1", serial: 2 }, "-9", 2), "foreign");
  assert.equal(r.tricorderStatus({ owner: undefined }, "-1", 2), "retired"); // a kit copy
});

test("the death table: both keep, anything less drops, the switch off is vanilla", () => {
  const plan = (switchOn, keepRule, hasPack, hasOwnTricorder) =>
    r.deathPlan({ switchOn, keepRule, hasPack, hasOwnTricorder });
  assert.equal(plan(true, true, true, true), "keep");
  assert.equal(plan(true, true, true, false), "drop");
  assert.equal(plan(true, true, false, true), "drop");
  assert.equal(plan(true, true, false, false), "drop");
  for (const [p, t] of [[true, true], [true, false], [false, true], [false, false]]) {
    assert.equal(plan(false, true, p, t), "vanilla");
    assert.equal(plan(true, false, p, t), "vanilla"); // the game already drops
  }
});

test("on a drop: keepOnDeath stays, Curse of Vanishing is destroyed, the rest falls", () => {
  assert.equal(r.slotFate({ keepOnDeath: true, vanishing: true }), "stay");
  assert.equal(r.slotFate({ keepOnDeath: false, vanishing: true }), "destroy");
  assert.equal(r.slotFate({ keepOnDeath: false, vanishing: false }), "drop");
});

test("the pack that burns is the smallest stack; XP and the void spot", () => {
  assert.deepEqual(r.packToBurn([{ i: 0, amount: 5 }, { i: 1, amount: 2 }, { i: 2, amount: 2 }]), { i: 1, amount: 2 });
  assert.equal(r.packToBurn([]), undefined);
  assert.equal(r.xpToDrop(0), 0);
  assert.equal(r.xpToDrop(3), 21);
  assert.equal(r.xpToDrop(30), 100);
  const at = { x: 1, y: -80, z: 2, dim: "minecraft:the_end" };
  const safe = { x: 5, y: 60, z: 5, dim: "minecraft:the_end" };
  assert.deepEqual(r.dropSpot(at, -64, safe), safe);
  assert.deepEqual(r.dropSpot({ ...at, y: 10 }, -64, safe), { ...at, y: 10 }); // not the void
  assert.deepEqual(r.dropSpot(at, -64, { ...safe, dim: "minecraft:overworld" }), at);
});

test("the wrapper's config and Dave's waypoints are parsed strictly", () => {
  assert.deepEqual(r.parseConfig('{"power_pack_keep": true}'), { power_pack_keep: true });
  assert.deepEqual(r.parseConfig('{"power_pack_keep": "yes", "other": 1}'), {});
  assert.throws(() => r.parseConfig("null"));
  assert.deepEqual(r.parseWaypoint('{"player": "Steve 42", "clear": true}'), { player: "Steve 42", clear: true });
  const wp = r.parseWaypoint('{"player": "A", "x": 1, "z": 2, "dim": "nether", "label": "' + "x".repeat(60) + '"}');
  assert.deepEqual(wp.record, { x: 1, y: null, z: 2, dim: "nether", label: "x".repeat(40) });
  assert.throws(() => r.parseWaypoint('{"player": "A", "x": 1, "z": 2, "dim": "moon"}'));
  assert.throws(() => r.parseWaypoint('{"x": 1, "z": 2, "dim": "nether"}'));
});

test("Dave's lines are the canon's, verbatim", () => {
  assert.equal(r.LINES.noSignal, "…kssht… no signal…");
  assert.equal(r.LINES.notTuned, "…static… that one isn't tuned to you.");
  assert.equal(
    r.LINES.rebeamed,
    "…there you are. Without your Tricorder I nearly lost you. Here's a new one. Keep it close.",
  );
  assert.equal(r.LINES.burnt(2), "Your Power Pack burnt out — you kept your things. 2 packs left.");
  assert.equal(r.LINES.burnt(1), "Your Power Pack burnt out — you kept your things. 1 pack left.");
  assert.equal(r.LINES.burnt(0), "Your Power Pack burnt out — you kept your things. No packs left.");
});

test("unlocks come from the key ingredient held, never shrink, and stay sorted", () => {
  assert.deepEqual(r.unlocksFrom([], []), ["tricorder"]);
  assert.deepEqual(r.unlocksFrom([], ["minecraft:dirt", "minecraft:ender_eye"]), ["tricorder"]);
  assert.deepEqual(r.unlocksFrom(["tricorder"], ["minecraft:ender_pearl"]), ["power_pack", "tricorder"]);
  assert.deepEqual(r.unlocksFrom([], ["jbrain:power_pack"]), ["power_pack_uses", "tricorder"]);
  assert.deepEqual(r.unlocksFrom(["power_pack", "tricorder"], []), ["power_pack", "tricorder"]);
  assert.deepEqual(r.parseKnown('["power_pack", 3, "tricorder"]'), ["power_pack", "tricorder"]);
  assert.deepEqual(r.parseKnown("{oops"), []);
  assert.deepEqual(r.parseKnown('{"a": 1}'), []);
  assert.deepEqual(r.parseKnown(undefined), []);
});

test("a mob search names a namespaced type, and its reach is clamped to what's loaded", () => {
  assert.deepEqual(r.parseFind('{"player":"Steve 42","type":"cow"}'), {
    player: "Steve 42",
    type: "minecraft:cow",
    max: r.FIND_MAX,
  });
  assert.equal(r.parseFind('{"player":"a","type":"minecraft:pig","max":2}').max, 8);
  assert.equal(r.parseFind('{"player":"a","type":"pig","max":9999}').max, r.FIND_MAX);
  for (const bad of ['{"type":"cow"}', '{"player":"a","type":"cow; op @a"}', "[]", "null"]) {
    assert.throws(() => r.parseFind(bad), bad);
  }
});

test("a mob search answers in the hit shape the backend reads, or says none", () => {
  const q = { type: "minecraft:cow", max: 256 };
  const here = { dim: "minecraft:nether", x: 0.5, z: 0.5 };
  assert.equal(
    r.findLine(q, "Steve", here, { x: 30.7, y: 64.2, z: -40.1 }),
    "find found minecraft:cow at x=30 y=64 z=-41, 51 blocks from Steve (nether)",
  );
  assert.equal(r.findLine(q, "Steve", here, undefined), "find none: no minecraft:cow within 256 blocks of Steve");
});

test("a Power Pack's lore is stale unless it is exactly the one current line", () => {
  assert.equal(r.packLoreStale(r.POWER_PACK, []), true);
  assert.equal(r.packLoreStale(r.POWER_PACK, ["an older wording"]), true);
  assert.equal(r.packLoreStale(r.POWER_PACK, [r.LINES.powerPackLore, "x"]), true);
  assert.equal(r.packLoreStale(r.POWER_PACK, [r.LINES.powerPackLore]), false);
  assert.equal(r.packLoreStale(r.TRICORDER, []), false);
  assert.equal(r.packLoreStale("minecraft:ender_pearl", []), false);
});

test("an item search names 1-8 namespaced types and stays within the short-range scan", () => {
  assert.deepEqual(r.parseItems('{"player":"Steve 42","types":["diamond","jbrain:power_pack"]}'), {
    player: "Steve 42",
    types: ["minecraft:diamond", "jbrain:power_pack"],
    max: r.ITEMS_MAX,
  });
  assert.equal(r.parseItems('{"player":"a","types":["apple"],"max":2}').max, 8);
  assert.equal(r.parseItems('{"player":"a","types":["apple"],"max":500}').max, r.ITEMS_MAX);
  const nine = JSON.stringify({ player: "a", types: Array(9).fill("apple") });
  for (const bad of ['{"types":["apple"]}', '{"player":"a","types":[]}', '{"player":"a","types":"apple"}',
    '{"player":"a","types":["Apple!"]}', nine, "null"]) {
    assert.throws(() => r.parseItems(bad), bad);
  }
});

test("an item search answers one line per kind per block, nearest first, capped", () => {
  const q = { types: ["minecraft:apple"], max: 48 };
  const here = { dim: "minecraft:the_end", x: 0, z: 0 };
  const stacks = Array.from({ length: 12 }, (_, i) => ({
    typeId: "minecraft:apple",
    amount: 1,
    location: { x: 12 - i + 0.3, y: 50.9, z: 0.2 },
  }));
  const lines = r.itemsLines(q, "Steve", here, stacks);
  assert.equal(lines.length, r.ITEMS_LINES);
  assert.equal(lines[0], "items ground: 1 minecraft:apple at x=1 y=50 z=0, 2 blocks from Steve (the_end)");
  assert.deepEqual(r.itemsLines(q, "Steve", here, []), [
    "items none: no minecraft:apple on the ground within 48 blocks of Steve",
  ]);
});

test("a carry count names 1-8 namespaced types for one player", () => {
  assert.deepEqual(r.parseCarry('{"player":"Steve 42","types":["iron_block","jbrain:power_pack"]}'), {
    player: "Steve 42",
    types: ["minecraft:iron_block", "jbrain:power_pack"],
  });
  const nine = JSON.stringify({ player: "a", types: Array(9).fill("apple") });
  for (const bad of ['{"types":["apple"]}', '{"player":"a","types":[]}', '{"player":"a","types":"apple"}',
    '{"player":"a","types":["Apple!"]}', '{"player":"","types":["apple"]}', nine, "null"]) {
    assert.throws(() => r.parseCarry(bad), bad);
  }
});

test("a carry count totals each asked type, says 0 for none, and counts shulker boxes", () => {
  const q = { types: ["minecraft:iron_block", "minecraft:diamond"] };
  const here = { dim: "minecraft:nether", x: 10.7, y: 64.2, z: -3.4 };
  const stacks = [
    { typeId: "minecraft:iron_block", amount: 3 },
    { typeId: "minecraft:dirt", amount: 64 },
    { typeId: "minecraft:iron_block", amount: 2 },
    { typeId: "minecraft:red_shulker_box", amount: 1 },
    { typeId: "minecraft:shulker_box", amount: 1 },
  ];
  assert.equal(
    r.carryLine(q, "Steve 42", here, stacks),
    "carry counted for Steve 42 in nether at x=10 y=64 z=-4: minecraft:iron_block=5 minecraft:diamond=0; shulker boxes=2",
  );
  assert.equal(r.isShulkerBox("minecraft:shulker_box"), true);
  assert.equal(r.isShulkerBox("minecraft:shulker_shell"), false);
});
