// node --test: transport on request (plan §M10, "Teleport to a known place, one Power Pack
// per trip") — the pack's main.js and transport.js against the fake server and the fake
// server-ui. Laid out and run by the supervisor's test_minecraft_jbrain_pack.py.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { Dimension, ItemStack, Player, sim, system, world } from "@minecraft/server";
import { FormCancelationReason, forms } from "@minecraft/server-ui";
import {
  OFFER_TTL_MS, expired, landing, offerText, parseTransport, parseTransportSwitch, refusal,
  switchIsOn, visitedAfter,
} from "./pack/scripts/transport_rules.js";

// What the fake server lacks for a teleport, added here so this file stays its own.
const nether = new Dimension("minecraft:nether");
const dims = () => ({ "minecraft:overworld": sim.overworld, "minecraft:nether": nether });
world.getDimension = (id) => dims()[id];
system.runTimeout = (fn) => system.run(fn);
const land = { blocks: new Map(), unloaded: new Set(), solid: new Set() };
const key = (dim, { x, y, z }) => `${dim.id} ${Math.floor(x)} ${Math.floor(y)} ${Math.floor(z)}`;
Dimension.prototype.getBlock = function (loc) {
  const k = key(this, loc);
  if (land.unloaded.has(k)) return undefined;
  return { typeId: land.blocks.get(k) ?? "minecraft:air" };
};
Dimension.prototype.playSound = function (id, loc) {
  (this.sounds ??= []).push({ id, loc });
};
Dimension.prototype.spawnParticle = function (id, loc) {
  (this.particles ??= []).push({ id, loc });
};
Dimension.prototype.runCommand = function (command) {
  (this.commands ??= []).push(command);
};
Player.prototype.tryTeleport = function (loc, opts) {
  assert.equal(opts.checkForBlocks, true);
  if (land.solid.has(key(opts.dimension, loc))) return false;
  this.location = { ...loc };
  this.dimension = opts.dimension;
  return true;
};

await import("./pack/scripts/main.js");
sim.startup();

const YES = () => ({ canceled: false, selection: 0 });
const NO = () => ({ canceled: false, selection: 1 });
const MARKET = { label: "Market", x: 1200, y: 70, z: 310, dim: "overworld" };
let n = 0;

const settle = async () => {
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setImmediate(r));
    sim.flush();
  }
};
const lines = (id) =>
  sim.logs.filter((l) => l.startsWith(`[jbrain] transport ${id} `)).map((l) => {
    const [, , , outcome, ...rest] = l.split(" ");
    return { outcome, detail: JSON.parse(rest.join(" ")) };
  });
const send = async (player, place = MARKET, extra = {}) => {
  const id = (++n).toString(16).padStart(8, "0");
  sim.scriptevent("jb:transport", JSON.stringify({ id, player, ...place, ...extra }));
  await settle();
  return lines(id);
};
const packs = (p) =>
  p.inventory.slots.filter((s) => s?.typeId === "jbrain:power_pack").reduce((a, s) => a + s.amount, 0);

// A player with their own Tricorder (beamed on their first waking) and `count` packs.
const crew = (name, id, count) => {
  const p = sim.player(name, id);
  sim.spawn(p, true);
  if (count) p.inventory.setItem(5, new ItemStack("jbrain:power_pack", count));
  return p;
};

beforeEach(() => {
  sim.reset();
  forms.reset();
  nether.commands = [];
  land.blocks.clear();
  land.unloaded.clear();
  land.solid.clear();
});

test("Yes on the form: one pack burns, they land at the place, with sound at both ends", async () => {
  const p = crew("Steve 42", "-1", 3);
  p.location = { x: 0, y: 64, z: 0 };
  forms.answer = YES;
  const got = await send("Steve 42");
  assert.deepEqual(got[0], {
    outcome: "offered",
    detail: { text: "Market — 1,239 blocks, Overworld. Use 1 Power Pack? (you have 3)", packs: 3 },
  });
  assert.equal(forms.shown[0].body, got[0].detail.text);
  assert.deepEqual(forms.shown[0].buttons, ["Yes", "No"]);
  assert.equal(got[1].outcome, "moved");
  assert.deepEqual(got[1].detail, {
    player: "Steve 42",
    label: "Market",
    from: { x: 0, y: 64, z: 0, dim: "overworld" },
    left: 2,
  });
  assert.deepEqual(p.location, { x: 1200.5, y: 70, z: 310.5 });
  assert.equal(packs(p), 2);
  const sounds = sim.overworld.sounds.map((s) => s.loc);
  assert.deepEqual(sounds, [{ x: 0, y: 64, z: 0 }, { x: 1200.5, y: 70, z: 310.5 }]);
  assert.ok(sim.overworld.particles.length >= 2);
  // The far end was loaded for the check, and let go after.
  const id = n.toString(16).padStart(8, "0");
  assert.deepEqual(sim.overworld.commands, [
    `tickingarea add circle 1200 70 310 1 jbt${id} true`,
    `tickingarea remove jbt${id}`,
  ]);
});

test("the smallest stack pays, and the last pack can be spent", async () => {
  const p = crew("Alex", "-2", 0);
  p.inventory.setItem(3, new ItemStack("jbrain:power_pack", 5));
  p.inventory.setItem(4, new ItemStack("jbrain:power_pack", 1));
  forms.answer = YES;
  const got = await send("Alex");
  assert.equal(got.at(-1).outcome, "moved");
  assert.equal(got.at(-1).detail.left, 5);
  assert.equal(p.inventory.slots[4], undefined);
  assert.equal(p.inventory.slots[3].amount, 5);
});

test("No on the form, or closing it, costs nothing and moves nobody", async () => {
  const p = crew("Alex", "-2", 2);
  forms.answer = NO;
  assert.deepEqual((await send("Alex")).map((l) => l.outcome), ["offered", "declined"]);
  forms.answer = () => ({ canceled: true, cancelationReason: FormCancelationReason.UserClosed });
  assert.deepEqual((await send("Alex")).map((l) => l.outcome), ["offered", "declined"]);
  assert.equal(packs(p), 2);
  assert.deepEqual(p.location, { x: 0, y: 64, z: 0 });
});

test("a busy player (chat still open) gets the form again; the answer still counts", async () => {
  const p = crew("Alex", "-2", 1);
  let tries = 0;
  forms.answer = () =>
    ++tries < 3 ? { canceled: true, cancelationReason: FormCancelationReason.UserBusy } : YES();
  const got = await send("Alex");
  assert.equal(forms.shown.length, 3);
  assert.equal(got.at(-1).outcome, "moved");
  assert.equal(packs(p), 0);
});

test("a Yes after the offer ran out moves nobody", async (t) => {
  const p = crew("Alex", "-2", 1);
  const real = Date.now;
  let clock = 1_000_000;
  Date.now = () => clock;
  t.after(() => {
    Date.now = real;
  });
  forms.answer = () => {
    clock += OFFER_TTL_MS + 1;
    return YES();
  };
  assert.equal((await send("Alex")).at(-1).outcome, "expired");
  assert.equal(packs(p), 1);
});

test("a blocked landing is refused and the pack kept", async () => {
  const p = crew("Alex", "-2", 2);
  forms.answer = YES;
  land.solid.add("minecraft:overworld 1200 70 310");
  assert.equal((await send("Alex")).at(-1).outcome, "blocked");
  land.solid.clear();
  land.blocks.set("minecraft:overworld 1200 71 310", "minecraft:lava");
  assert.equal((await send("Alex")).at(-1).outcome, "blocked");
  assert.equal(packs(p), 2);
  assert.deepEqual(p.location, { x: 0, y: 64, z: 0 });
});

test("ground that never loads is refused, pack kept, after a few tries", async () => {
  const p = crew("Alex", "-2", 2);
  forms.answer = YES;
  land.unloaded.add("minecraft:overworld 1200 70 310");
  assert.equal((await send("Alex")).at(-1).outcome, "unloaded");
  assert.equal(packs(p), 2);
});

test("no pack, no Tricorder of their own, or the switch off: refused before any form", async () => {
  const p = crew("Alex", "-2", 0);
  assert.deepEqual(await send("Alex"), [{ outcome: "refused", detail: { why: "no-pack" } }]);
  p.inventory.setItem(5, new ItemStack("jbrain:power_pack", 1));
  p.inventory.slots = p.inventory.slots.map((s) => (s?.typeId === "jbrain:tricorder" ? undefined : s));
  assert.deepEqual(await send("Alex"), [{ outcome: "refused", detail: { why: "no-tricorder" } }]);
  // Someone else's Tricorder isn't theirs either.
  const sam = crew("Sam", "-3", 0);
  p.inventory.setItem(7, sam.inventory.slots.find((s) => s?.typeId === "jbrain:tricorder"));
  assert.deepEqual(await send("Alex"), [{ outcome: "refused", detail: { why: "no-tricorder" } }]);
  sim.spawn(p, false); // re-beamed their own
  sim.scriptevent("jb:config", JSON.stringify({ power_pack_keep: false, power_pack_transport: false }));
  assert.ok(sim.logs.includes("[jbrain] config power_pack_transport=false"));
  assert.deepEqual(await send("Alex"), [{ outcome: "refused", detail: { why: "off" } }]);
  sim.scriptevent("jb:config", JSON.stringify({ power_pack_transport: true }));
  forms.answer = YES;
  assert.equal((await send("Alex")).at(-1).outcome, "moved");
  assert.equal(forms.shown.length, 1);
});

test("the pack is checked again on Yes: dropped while the form was open, no trip", async () => {
  const p = crew("Alex", "-2", 1);
  forms.answer = (player) => {
    player.inventory.setItem(5, undefined);
    return YES();
  };
  const got = await send("Alex");
  assert.deepEqual(got.at(-1).detail, { player: "Alex", label: "Market", why: "no-pack" });
  assert.deepEqual(p.location, { x: 0, y: 64, z: 0 });
});

test("another dimension only once they've been there", async () => {
  const p = crew("Alex", "-2", 2);
  forms.answer = YES;
  const fort = { label: "Fort", x: 40, y: 80, z: -12, dim: "nether" };
  assert.deepEqual(await send("Alex", fort), [{ outcome: "refused", detail: { why: "unvisited" } }]);
  // A second in the Nether is remembered by the pack itself.
  p.dimension = nether;
  sim.tick();
  p.dimension = sim.overworld;
  const got = await send("Alex", fort);
  assert.equal(got[0].detail.text, "Fort — Nether. Use 1 Power Pack? (you have 2)");
  assert.equal(got.at(-1).outcome, "moved");
  assert.equal(p.dimension, nether);
  // Or by the box's travel log, which the tool passes along.
  const q = crew("Mira", "-4", 1);
  const got2 = await send("Mira", fort, { visited: ["nether"] });
  assert.equal(got2.at(-1).outcome, "moved");
  assert.equal(q.dimension, nether);
});

test("only an online player, and only one open form at a time", async () => {
  crew("Alex", "-2", 2);
  assert.deepEqual(await send("Nobody"), [{ outcome: "refused", detail: { why: "offline" } }]);
  forms.answer = () => new Promise(() => {}); // the form stays open
  assert.equal((await send("alex"))[0].outcome, "offered");
  assert.deepEqual(await send("Alex"), [{ outcome: "refused", detail: { why: "busy" } }]);
});

test("a malformed offer is refused whole", async () => {
  crew("Alex", "-2", 2);
  for (const bad of ["nope", "{}", JSON.stringify({ id: "x", player: "Alex", ...MARKET })]) {
    sim.scriptevent("jb:transport", bad);
  }
  assert.equal(sim.logs.filter((l) => l.startsWith("[jbrain] transport refused:")).length, 3);
  assert.equal(forms.shown.length, 0);
});

// --- the pure rules ---------------------------------------------------------------------

test("parsing: coordinates are whole and in the world, y may be unknown, dims are named", () => {
  const ok = parseTransport(JSON.stringify({ id: "0a0b0c0d", player: "A", ...MARKET, visited: ["nether", "moon"] }));
  assert.deepEqual(ok.visited, ["nether"]);
  assert.equal(parseTransport(JSON.stringify({ id: "0a0b0c0d", player: "A", ...MARKET, y: null })).y, null);
  for (const bad of [
    { ...MARKET, x: 1.5 },
    { ...MARKET, x: 40_000_000 },
    { ...MARKET, dim: "moon" },
    { ...MARKET, label: "  " },
  ]) {
    assert.throws(() => parseTransport(JSON.stringify({ id: "0a0b0c0d", player: "A", ...bad })));
  }
  assert.deepEqual(parseTransportSwitch('{"power_pack_transport": false}'), { on: false });
  assert.deepEqual(parseTransportSwitch('{"power_pack_transport": "no"}'), {});
  assert.equal(switchIsOn(undefined), true); // on by default
  assert.equal(switchIsOn(false), false);
});

test("the refusal order, the unexplored place, the offer text and the landing", () => {
  const base = {
    switchOn: true, tricorder: "own", packs: 1, hereDim: "overworld",
    target: { ...MARKET }, visited: [],
  };
  assert.equal(refusal(base), null);
  assert.equal(refusal({ ...base, switchOn: false, packs: 0 }), "off");
  assert.equal(refusal({ ...base, tricorder: "foreign" }), "no-tricorder");
  assert.equal(refusal({ ...base, target: { ...MARKET, y: null } }), "unexplored");
  assert.equal(refusal({ ...base, target: { ...MARKET, dim: "the_end" } }), "unvisited");
  assert.equal(
    offerText({ label: "Home", here: { x: 0, z: 0, dim: "overworld" }, target: { x: 3, z: 4, dim: "overworld" }, packs: 1 }),
    "Home — 5 blocks, Overworld. Use 1 Power Pack? (you have 1)",
  );
  assert.equal(landing({ typeId: "minecraft:tall_grass" }, { typeId: "minecraft:air" }), null);
  assert.equal(landing(undefined, { typeId: "minecraft:air" }), "unloaded");
  assert.equal(landing({ typeId: "minecraft:flowing_lava" }, { typeId: "minecraft:air" }), "blocked");
  assert.deepEqual(visitedAfter(["overworld"], "nether"), ["nether", "overworld"]);
  const same = ["overworld"];
  assert.equal(visitedAfter(same, "overworld"), same);
  assert.equal(expired(0, OFFER_TTL_MS), false);
  assert.equal(expired(0, OFFER_TTL_MS + 1), true);
});
