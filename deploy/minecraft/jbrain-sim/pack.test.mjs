// node --test: the jbrain pack's main.js against the fake server (fake_server.mjs, as
// node_modules/@minecraft/server). The supervisor's test_minecraft_jbrain_pack.py lays the
// pack out beside it and runs this file, so CI does too.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { CustomCommandStatus, ItemLockMode, ItemStack, sim } from "@minecraft/server";
import { LINES } from "./pack/scripts/rules.js";

await import("./pack/scripts/main.js");
sim.startup();

const DAVE = "§b[Dave]§r ";
const tricorders = (p) => p.inventory.slots.filter((s) => s?.typeId === "jbrain:tricorder");
const daveEvents = () => sim.logs.filter((l) => l.includes('"ev":"dave"'));
const ask = (p, ...words) => {
  sim.command("jb:dave").cb({ sourceEntity: p }, ...words);
  sim.flush();
};

// A first join plays Dave's welcome (welcome.test.mjs); these tests start after it.
const WELCOME_TICKS = 500;
const wake = (p) => {
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  p.messages.length = 0;
  p.sounds.length = 0;
};

beforeEach(() => sim.reset());

test("the welcome's Tricorder is bound to its owner", () => {
  const p = sim.player("Steve 42", "-111");
  wake(p);
  const [t] = tricorders(p);
  assert.ok(t, "beamed");
  assert.equal(t.lockMode, ItemLockMode.inventory);
  assert.equal(t.keepOnDeath, true);
  assert.equal(t.nameTag, "Steve 42's Tricorder");
  assert.deepEqual(t.lore, ["Points where Dave says"]);
  assert.equal(t.getDynamicProperty("jb:owner"), "-111");
});

test("waking without your own Tricorder re-beams one with Dave's line; with it, nothing", () => {
  const p = sim.player("Alex", "-2");
  wake(p);
  sim.spawn(p, false);
  assert.equal(tricorders(p).length, 1);
  assert.deepEqual(p.messages, []);

  const old = tricorders(p)[0].clone();
  p.inventory.slots.fill(undefined); // a /clear got past the lock
  sim.spawn(p, false);
  assert.equal(tricorders(p).length, 1);
  assert.deepEqual(p.messages, [DAVE + LINES.rebeamed]);
  // The new one outdates every older copy, wherever it is.
  const sibling = sim.player("Sib", "-3");
  sibling.inventory.setItem(0, old);
  sibling.selectedSlotIndex = 0;
  p.inventory.setItem(0, old); // the owner holding the stale copy
  p.selectedSlotIndex = 0;
  sim.tick();
  assert.equal(p.actionbar.at(-1), LINES.retired);
  const relabelled = p.inventory.slots[0];
  assert.deepEqual(relabelled.lore, [LINES.retired]);
  assert.equal(relabelled.lockMode, ItemLockMode.none); // it can be thrown away now
  assert.equal(relabelled.keepOnDeath, false);
});

test("a full inventory still gets the Tricorder; the selected stack makes room", () => {
  const p = sim.player("Full", "-4");
  for (let i = 0; i < 36; i++) p.inventory.setItem(i, new ItemStack("minecraft:dirt", 64));
  p.selectedSlotIndex = 3;
  wake(p);
  assert.equal(p.inventory.slots[3].typeId, "jbrain:tricorder");
  assert.equal(sim.overworld.spawned[0].item.typeId, "minecraft:dirt");
});

test("only the owner sees the arrow; anyone else gets static", () => {
  const owner = sim.player("Owner", "-10");
  wake(owner);
  const tricorder = tricorders(owner)[0];
  const slot = owner.inventory.slots.indexOf(tricorder);
  owner.selectedSlotIndex = slot;
  owner.location = { x: 0, y: 64, z: 100 }; // spawn is due north
  sim.tick();
  assert.equal(owner.actionbar.at(-1), "↑ World spawn · 100 blocks");

  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Owner", x: 100, z: 100, dim: "overworld", label: "Base" }));
  assert.ok(sim.logs.includes("[jbrain] waypoint set for Owner: Base at 100 100 (overworld)"));
  sim.tick();
  assert.equal(owner.actionbar.at(-1), "→ Base · 100 blocks");

  const sibling = sim.player("Sib", "-11");
  sibling.inventory.setItem(0, tricorder);
  sim.tick();
  assert.equal(sibling.actionbar.at(-1), "§7§o" + LINES.notTuned);
});

test("no Tricorder, no Dave: static lines and no question reaches the backend", () => {
  const p = sim.player("Asker", "-20");
  ask(p, "where", "am", "I");
  assert.deepEqual(p.messages, ["§7§o" + LINES.noSignal]);

  const other = sim.player("Other", "-21");
  wake(other);
  p.inventory.setItem(5, tricorders(other)[0]);
  ask(p, "hello");
  assert.equal(p.messages.at(-1), "§7§o" + LINES.notTuned);
  assert.equal(daveEvents().length, 0);

  sim.spawn(p, false); // first waking for this player: their own arrives
  ask(p, "point", "me", "home");
  assert.equal(daveEvents().length, 1);
  assert.match(daveEvents()[0], /"text":"point me home"/);
});

test("/d and /ai are the same door as /dave, and an empty ask names /dave", () => {
  const p = sim.player("Shorty", "-30");
  wake(p);
  for (const name of ["jb:d", "jb:ai"]) {
    const res = sim.command(name).cb({ sourceEntity: p }, "point", "me", name);
    sim.flush();
    assert.equal(res.status, CustomCommandStatus.Success);
    assert.match(daveEvents().at(-1), new RegExp(`"text":"point me ${name}"`));
  }
  assert.match(sim.command("jb:dave").cb({ sourceEntity: p }).message, /\/dave point me home/);
});

const kit = (p) => {
  wake(p);
  p.inventory.setItem(1, new ItemStack("minecraft:diamond_sword", 1));
  const cursed = new ItemStack("minecraft:carved_pumpkin", 1);
  cursed.enchants = ["vanishing"];
  p.inventory.setItem(2, cursed);
  p.getComponent("minecraft:equippable").setEquipment("Chest", new ItemStack("minecraft:iron_chestplate", 1));
  p.level = 20;
};
const switchOn = (on) => {
  sim.scriptevent("jb:config", JSON.stringify({ power_pack_keep: on }));
  sim.rules.keepInventory = on;
};

test("switch off: a death is vanilla, the pack touches nothing", () => {
  const p = sim.player("Van", "-30");
  kit(p);
  p.inventory.setItem(3, new ItemStack("jbrain:power_pack", 2));
  sim.die(p);
  assert.equal(sim.overworld.spawned.length, 0);
  assert.equal(p.inventory.slots[3].amount, 2);
  assert.equal(p.level, 20);
});

test("a Power Pack and your own Tricorder: one pack burns (smallest stack), all kept", () => {
  switchOn(true);
  assert.ok(sim.logs.includes("[jbrain] config power_pack_keep=true"));
  const p = sim.player("Keep", "-31");
  kit(p);
  p.inventory.setItem(3, new ItemStack("jbrain:power_pack", 5));
  p.getComponent("minecraft:equippable").setEquipment("Offhand", new ItemStack("jbrain:power_pack", 2));
  sim.die(p);
  assert.equal(sim.overworld.spawned.length, 0);
  assert.equal(p.inventory.slots[3].amount, 5);
  assert.equal(p.equipment.Offhand.amount, 1); // the smaller stack, in the off-hand
  assert.equal(p.level, 20);
  sim.spawn(p, false);
  assert.deepEqual(p.messages, ["§7" + LINES.burnt(6)]); // 5 + 2, less the one burnt
  assert.deepEqual(p.sounds, ["random.fizz"]);
  sim.spawn(p, false);
  assert.equal(p.messages.length, 1); // told once
});

for (const [name, pack, own] of [["a pack only", true, false], ["a Tricorder only", false, true], ["neither", false, false]]) {
  test(`${name}: everything drops like a normal death, the Tricorder stays`, () => {
    switchOn(true);
    const p = sim.player("Drop", "-40");
    kit(p);
    if (!own) p.inventory.slots = p.inventory.slots.map((s) => (s?.typeId === "jbrain:tricorder" ? undefined : s));
    if (pack) p.inventory.setItem(3, new ItemStack("jbrain:power_pack", 1));
    p.location = { x: 5, y: 70, z: 6 };
    sim.die(p);
    const dropped = sim.overworld.spawned.map((s) => s.item.typeId).sort();
    const want = ["minecraft:diamond_sword", "minecraft:iron_chestplate"];
    if (pack) want.push("jbrain:power_pack");
    assert.deepEqual(dropped, want.sort());
    assert.ok(sim.overworld.spawned.every((s) => s.loc.x === 5 && s.loc.z === 6));
    assert.equal(p.inventory.slots[2], undefined); // Curse of Vanishing: gone, not dropped
    assert.equal(p.equipment.Chest, undefined);
    assert.equal(tricorders(p).length, own ? 1 : 0); // keepOnDeath: never dropped
    assert.equal(p.level, 0);
    assert.equal(sim.overworld.entities.filter((e) => e.id === "minecraft:xp_orb").length, 100);
  });
}

test("two deaths with one pack: the first keeps, the second drops", () => {
  switchOn(true);
  const p = sim.player("Twice", "-50");
  kit(p);
  p.inventory.setItem(3, new ItemStack("jbrain:power_pack", 1));
  sim.die(p);
  assert.equal(sim.overworld.spawned.length, 0);
  assert.equal(p.inventory.slots[3], undefined);
  sim.spawn(p, false);
  assert.deepEqual(p.messages, ["§7" + LINES.burnt(0)]);
  sim.die(p);
  assert.ok(sim.overworld.spawned.length > 0);
});

test("a void death drops at the last place the player stood", () => {
  switchOn(true);
  const p = sim.player("Void", "-60");
  kit(p);
  p.location = { x: 7, y: 60, z: 8 };
  sim.tick(); // on the ground here
  p.isOnGround = false;
  p.location = { x: 9, y: -90, z: 8 };
  sim.die(p);
  assert.ok(sim.overworld.spawned.length > 0);
  assert.ok(sim.overworld.spawned.every((s) => s.loc.y === 60 && s.loc.x === 7));
});

test("the rule off while the switch says on: the game drops, the pack doesn't double it", () => {
  sim.scriptevent("jb:config", JSON.stringify({ power_pack_keep: true }));
  sim.rules.keepInventory = false;
  const p = sim.player("Rule", "-70");
  kit(p);
  sim.die(p);
  assert.equal(sim.overworld.spawned.length, 0);
});

const knownOf = (line) => JSON.parse(line.slice(line.indexOf("{"))).known;

test("each /dave question carries the asker's unlocks, first held and then kept", () => {
  const p = sim.player("Learner", "-80");
  wake(p);
  ask(p, "what", "now");
  assert.deepEqual(knownOf(daveEvents().at(-1)), ["tricorder"]);

  // The loop notices a first Ender Pearl within a second (every fourth frame).
  p.inventory.setItem(4, new ItemStack("minecraft:ender_pearl", 3));
  for (let i = 0; i < 4; i++) sim.tick();
  assert.equal(sim.props.get("jb:known:-80"), JSON.stringify(["power_pack", "tricorder"]));

  // Asking checks too, so a pack made a moment ago counts at once.
  p.inventory.setItem(4, new ItemStack("jbrain:power_pack", 1));
  ask(p, "what", "now");
  assert.deepEqual(knownOf(daveEvents().at(-1)), ["power_pack", "power_pack_uses", "tricorder"]);

  // Once unlocked, always: spending the pack forgets nothing.
  p.inventory.setItem(4, undefined);
  ask(p, "what", "now");
  assert.deepEqual(knownOf(daveEvents().at(-1)), ["power_pack", "power_pack_uses", "tricorder"]);

  // Per player: someone else's pearl unlocks nothing for this one.
  const other = sim.player("Other", "-81");
  wake(other);
  ask(other, "what", "now");
  assert.deepEqual(knownOf(daveEvents().at(-1)), ["tricorder"]);
});

test("a pearl in a shulker box isn't held; an unreadable record starts fresh", () => {
  const p = sim.player("Boxed", "-82");
  wake(p);
  sim.props.set("jb:known:-82", "not json");
  p.inventory.setItem(6, new ItemStack("minecraft:shulker_box", 1));
  ask(p, "what", "now");
  assert.deepEqual(knownOf(daveEvents().at(-1)), ["tricorder"]);
});

test("Dave's mob search finds the nearest of a type around an online player", () => {
  const p = sim.player("Steve 42", "-60");
  p.location = { x: 10, y: 64, z: 10 };
  sim.overworld.spawnEntity("minecraft:cow", { x: 200, y: 64, z: 10 });
  sim.overworld.spawnEntity("minecraft:cow", { x: 40, y: 70, z: 50 });
  sim.overworld.spawnEntity("minecraft:pig", { x: 11, y: 64, z: 11 });
  sim.scriptevent("jb:find", JSON.stringify({ player: "steve 42", type: "cow" }));
  assert.equal(
    sim.logs.at(-1),
    "[jbrain] find found minecraft:cow at x=40 y=70 z=50, 50 blocks from Steve 42 (overworld)",
  );
  sim.scriptevent("jb:find", JSON.stringify({ player: "Steve 42", type: "sheep" }));
  assert.equal(sim.logs.at(-1), "[jbrain] find none: no minecraft:sheep within 256 blocks of Steve 42");
  sim.scriptevent("jb:find", JSON.stringify({ player: "Nobody", type: "cow" }));
  assert.equal(sim.logs.at(-1), "[jbrain] find refused: Nobody isn't online");
  sim.scriptevent("jb:find", JSON.stringify({ player: "Steve 42", type: "cow\"; op @a" }));
  assert.match(sim.logs.at(-1), /^\[jbrain\] find refused: Error: not a mob type/);
});
