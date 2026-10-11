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
