// node --test: the wreck pack's pure planning (plan.js). The supervisor's
// test_minecraft_wreck.py runs this file, so CI does too.
import assert from "node:assert/strict";
import { test } from "node:test";
import * as plan from "../wreck-pack/scripts/plan.js";

const POD = {
  size: [14, 11, 10],
  hatch_out: [6, 4, 8],
  hatch_dir: [0, 1],
  axis_xz: [1, 0],
  shield: [1.05, 5.3, 5],
  nose: [12.93, 6.97, 5],
  barrels: [[3, 4, 3]],
  origin: [-6, 60, -5],
};

const flat = (y = 64, extra = {}) => (x, z) => ({ y, top: "grass_block", water: false, ...extra });

test("the ground under plants, trees and snow is found through them", () => {
  for (const id of ["minecraft:oak_leaves", "minecraft:jungle_log", "minecraft:snow_layer",
    "minecraft:short_grass", "minecraft:mangrove_roots", "minecraft:poppy", "minecraft:air"]) {
    assert.ok(plan.standsOnGround(id), id);
  }
  for (const id of ["minecraft:grass_block", "minecraft:sand", "minecraft:stone", "minecraft:mud"]) {
    assert.ok(!plan.standsOnGround(id), id);
  }
  assert.ok(plan.isWater("minecraft:water") && plan.isWater("minecraft:ice"));
});

test("each kind of ground gets a scorched crater of its own, and a fill that cannot fall", () => {
  assert.ok(plan.materialsFor("minecraft:sand").dig.includes("sandstone"));
  assert.equal(plan.materialsFor("minecraft:red_sand").fill, "red_sandstone");
  assert.equal(plan.materialsFor("minecraft:orange_terracotta").fill, "terracotta");
  assert.equal(plan.materialsFor("minecraft:mud").fill, "packed_mud");
  assert.equal(plan.materialsFor("minecraft:stone").rim, "cobblestone");
  assert.equal(plan.materialsFor("minecraft:grass_block").fill, "dirt");
  for (const top of ["sand", "red_sand", "gravel", "snow", "mud", "podzol", "unknown_block"]) {
    assert.ok(!["sand", "red_sand", "gravel"].includes(plan.materialsFor(top).fill), top);
  }
});

test("on flat dry ground the pod comes down near spawn, deck one above the surface", () => {
  const site = plan.chooseSite(flat(), { x: 100, z: -40 }, POD);
  assert.equal(site.splash, false);
  assert.ok(Math.hypot(site.x + 7 - 100, site.z + 5 + 40) <= 6, JSON.stringify(site));
  const f = plan.podFrame(site, POD);
  assert.equal(f.deckY, 65);
  assert.deepEqual(f.hatch, [site.x + 6, 65, site.z + 8]);
});

test("water under the footprint is never chosen; with no dry land it splashes down", () => {
  const lake = (x, z) => (x < 90 ? { y: 62, top: "water", water: true } : { y: 70, top: "grass_block", water: false });
  const site = plan.chooseSite(lake, { x: 85, z: 0 }, POD);
  assert.ok(site.x >= 90 && !site.splash);
  const sea = plan.chooseSite(() => ({ y: 62, top: "water", water: true }), { x: 0, z: 0 }, POD);
  assert.equal(sea.splash, true);
  const f = plan.podFrame(sea, POD);
  assert.equal(f.deckY, 63); // the deck and hatch just above the sea
  const sp = plan.spawnPoint(f);
  assert.equal(sp.y, f.deckY);
  assert.ok(sp.z < f.hatch[2]); // on the deck, inside, not in the water
});

test("a slope is taken when it is all there is, and the flattest part of it wins", () => {
  const hill = (x, z) => ({ y: Math.floor(64 + x * 0.8 + (x > 10 ? -(x - 10) * 0.8 : 0)), top: "stone", water: false });
  const site = plan.chooseSite(hill, { x: 0, z: 0 }, POD);
  assert.equal(site.splash, false);
  assert.ok(site.x >= 4, `took the flat top beyond x=10: ${site.x}`);
});

test("the crater digs round the shield and the skid mark runs back the way it came", () => {
  const f = plan.podFrame(plan.chooseSite(flat(), { x: 0, z: 0 }, POD), POD);
  const cols = plan.craterColumns(f);
  const at = (x, z) => cols.find((c) => c.x === x && c.z === z);
  const sx = Math.floor(f.shield[0]);
  const sz = Math.floor(f.shield[2]);
  assert.equal(at(sx, sz).dig, 2);
  const trail = cols.filter((c) => c.kind === "trail");
  assert.ok(trail.length > 20);
  assert.ok(trail.every((c) => c.x < f.shield[0])); // behind the shield (axis is +x)
  assert.ok(cols.some((c) => c.kind === "rim" && c.raise > 0));
});

test("the parachute lies beyond the nose, in alternating gores, with lines back", () => {
  const f = plan.podFrame(plan.chooseSite(flat(), { x: 0, z: 0 }, POD), POD);
  const cells = plan.canopyCells(f);
  assert.ok(cells.length > 100);
  assert.ok(cells.every((c) => c.x > f.nose[0] - 2));
  const kinds = new Set(cells.map((c) => c.block));
  for (const k of ["white_carpet", "orange_carpet"]) assert.ok(kinds.has(k), k);
  assert.ok(cells.some((c) => c.fold));
});

test("the apron in front of the hatch is level with the deck and starts at the hatch", () => {
  const f = plan.podFrame(plan.chooseSite(flat(70), { x: 0, z: 0 }, POD), POD);
  const apron = plan.hatchApron(f);
  assert.equal(apron.length, 8);
  assert.ok(apron.every((a) => a.y === f.deckY - 1));
  assert.ok(apron.some((a) => a.x === f.hatch[0] && a.z === f.hatch[2]));
  const sp = plan.spawnPoint(f);
  assert.ok(apron.some((a) => a.x === sp.x && a.z === sp.z)); // spawn stands on it
});

test("the ship keeps its signed-off place over the pod, lifted clear of peaks", () => {
  const SHIP = { size: [210, 65, 80], origin: [-6, -28, -33], offset: [-100, 192, -62] };
  const f = plan.podFrame({ x: -6, y: 63, z: -5, splash: false }, POD);
  const base = plan.shipBase(f, POD, SHIP);
  assert.deepEqual(base, { x: -106, y: 164, z: -95 }); // the renders' scene exactly
  assert.equal(plan.shipHeight(base.y, 70, 65), 164);
  assert.equal(plan.shipHeight(base.y, 200, 65), 230);
  assert.equal(plan.shipHeight(base.y, 240, 65), null); // would poke past the build limit
  assert.deepEqual(plan.SHIP_SHIFTS.slice(0, 3), [0, 40, -40]);
});

test("the starter kit is a few useful stacks of real items", () => {
  const ids = plan.STARTER_KIT.flat().map(([id]) => id);
  assert.ok(ids.includes("bread") && ids.includes("stone_pickaxe") && ids.includes("torch"));
  assert.ok(plan.STARTER_KIT.flat().every(([, n]) => n >= 1 && n <= 64));
});

test("the stable hash is deterministic and spread over 0..1", () => {
  assert.equal(plan.hash01(3, 4, 1), plan.hash01(3, 4, 1));
  const xs = Array.from({ length: 1000 }, (_, i) => plan.hash01(i, -i, 7));
  assert.ok(Math.min(...xs) >= 0 && Math.max(...xs) < 1);
  assert.ok(Math.abs(xs.reduce((a, b) => a + b) / xs.length - 0.5) < 0.05);
});
