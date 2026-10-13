// node --test: the jbrain pack's main.js against the fake server (fake_server.mjs, as
// node_modules/@minecraft/server). The supervisor's test_minecraft_jbrain_pack.py lays the
// pack out beside it and runs this file, so CI does too.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { CustomCommandStatus, Dimension, ItemLockMode, ItemStack, sim } from "@minecraft/server";
import { FormCancelationReason, forms } from "@minecraft/server-ui";
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
const WELCOME_TICKS = 800;
// The once-a-session hint on first holding the Tricorder, past by the time a test looks.
const TRI_HINT = "Your Tricorder points where Dave says · ask with /dave";
const HINT_TICKS = 60;
const wake = (p) => {
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  sim.tick();
  sim.advance(HINT_TICKS);
  p.messages.length = 0;
  p.sounds.length = 0;
};

beforeEach(() => {
  sim.reset();
  forms.reset();
});

test("the welcome's Tricorder is bound to its owner", () => {
  const p = sim.player("Steve 42", "-111");
  wake(p);
  const [t] = tricorders(p);
  assert.ok(t, "beamed");
  assert.equal(t.lockMode, ItemLockMode.inventory);
  assert.equal(t.keepOnDeath, true);
  assert.equal(t.nameTag, "Steve 42's Tricorder");
  // No description: Bedrock's held-item pop-up shows lore over the heading (2026-10-13).
  assert.deepEqual(t.lore, []);
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

test("an owned Tricorder's old description is cleared on the next look; a retired one keeps its", () => {
  const p = sim.player("Lored Tri", "-5");
  wake(p);
  const slot = p.inventory.slots.findIndex((s) => s?.typeId === "jbrain:tricorder");
  const own = p.inventory.slots[slot];
  own.setLore(["Points where Dave says"]); // a copy beamed before 2026-10-13
  p.inventory.setItem(slot, own);
  const retired = own.clone();
  retired.setDynamicProperty("jb:serial", -1);
  retired.setLore([LINES.retired]);
  p.inventory.setItem(20, retired);
  const stale = own.clone(); // an outdated copy with the old line, not yet held
  stale.setDynamicProperty("jb:serial", -2);
  p.inventory.setItem(21, stale);
  for (let i = 0; i < 4; i++) sim.tick();
  const now = p.inventory.slots[slot];
  assert.deepEqual(now.lore, []);
  assert.equal(now.nameTag, "Lored Tri's Tricorder");
  assert.equal(now.lockMode, ItemLockMode.inventory);
  assert.equal(now.keepOnDeath, true);
  assert.deepEqual(p.inventory.slots[20].lore, [LINES.retired]);
  p.selectedSlotIndex = 21; // held, it reads as retired, as before
  sim.tick();
  assert.deepEqual(p.inventory.slots[21].lore, [LINES.retired]);
});

test("the Tricorder's hint shows on the first hold of a session only, then the heading", () => {
  const p = sim.player("Hinted", "-6");
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS); // the welcome hands it over, in their hand
  p.location = { x: 0, y: 64, z: 100 };
  sim.tick();
  assert.equal(p.actionbar.at(-1), TRI_HINT);
  sim.advance(HINT_TICKS - 1);
  sim.tick();
  assert.equal(p.actionbar.at(-1), TRI_HINT); // about 3 s on screen
  sim.advance(1);
  sim.tick();
  assert.equal(p.actionbar.at(-1), "↑ World spawn · 100 blocks");

  const slot = p.selectedSlotIndex;
  p.selectedSlotIndex = (slot + 1) % 9; // put away, and back: no hint again
  sim.tick();
  p.selectedSlotIndex = slot;
  sim.tick();
  assert.equal(p.actionbar.at(-1), "↑ World spawn · 100 blocks");
  sim.spawn(p, false); // a respawn is the same session
  sim.tick();
  assert.equal(p.actionbar.at(-1), "↑ World spawn · 100 blocks");

  sim.leave(p); // a rejoin is a new session: the hint once more
  sim.join(p);
  sim.spawn(p, true);
  sim.tick();
  assert.equal(p.actionbar.at(-1), TRI_HINT);
  sim.advance(HINT_TICKS);
  sim.tick();
  assert.equal(p.actionbar.at(-1), "↑ World spawn · 100 blocks");
  assert.ok(!p.messages.includes(TRI_HINT)); // the actionbar only, never chat
});

test("the heading is re-sent every frame, so a re-select shows it as the pop-up clears", () => {
  const p = sim.player("Reequip", "-7");
  wake(p);
  p.location = { x: 0, y: 64, z: 100 };
  const slot = p.selectedSlotIndex;
  p.selectedSlotIndex = (slot + 1) % 9;
  sim.tick();
  const away = p.actionbar.length;
  p.selectedSlotIndex = slot;
  sim.tick(); // the first frame after the re-select
  assert.equal(p.actionbar.length, away + 1);
  assert.equal(p.actionbar.at(-1), "↑ World spawn · 100 blocks");
  // Unchanged, it is still sent each frame: Bedrock's actionbar fades after 2-3 s.
  for (let i = 0; i < 12; i++) sim.tick();
  assert.equal(p.actionbar.length, away + 13);
  assert.ok(p.actionbar.slice(away).every((l) => l === "↑ World spawn · 100 blocks"));
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

test("each /dave question carries the asker's player id, the owner id pets are filed under", () => {
  const p = sim.player("Hank", "-4294967295");
  wake(p);
  ask(p, "where's", "my", "dog");
  assert.equal(JSON.parse(daveEvents().at(-1).split("[jbrain] ")[1]).pid, "-4294967295");
});

test("in the Nether a waypoint home points through the nearest portal Dave named", () => {
  const p = sim.player("Owner", "-1");
  wake(p);
  p.selectedSlotIndex = p.inventory.slots.indexOf(tricorders(p)[0]);
  const via = [
    { dim: "nether", x: 40, z: 0 },
    { dim: "nether", x: 0, z: -20 },
  ];
  sim.scriptevent(
    "jb:waypoint",
    JSON.stringify({ player: "Owner", x: 800, z: 0, dim: "overworld", label: "Base", via }),
  );
  assert.ok(sim.logs.includes("[jbrain] waypoint set for Owner: Base at 800 0 (overworld) via 2 portal(s)"));
  p.dimension = new Dimension("minecraft:nether");
  p.location = { x: 0, y: 64, z: 0 };
  sim.tick();
  assert.equal(p.actionbar.at(-1), "↑ Nether portal · 20 blocks · Base is in the Overworld");
  p.dimension = sim.overworld;
  sim.tick();
  assert.equal(p.actionbar.at(-1), "→ Base · 800 blocks");
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

test("/d and /ai are the same door as /dave", () => {
  const p = sim.player("Shorty", "-30");
  wake(p);
  for (const name of ["jb:d", "jb:ai"]) {
    const res = sim.command(name).cb({ sourceEntity: p }, "point", "me", name);
    sim.flush();
    assert.equal(res.status, CustomCommandStatus.Success);
    assert.match(daveEvents().at(-1), new RegExp(`"text":"point me ${name}"`));
  }
});

// --- the quick menu (§M7) ---------------------------------------------------------------

// Every pending form answer, and what it sets off.
const settle = () => new Promise((resolve) => setImmediate(resolve));
const HINT = 'Ask Dave something, like: /dave point me home (use "quotes" for commas or numbers)';
const MENU = ["Where am I?", "Point me home", "My last death", "Save this spot", "Ask something else…"];
const pick = (i, typed) => (_p, form) =>
  form.buttons ? { canceled: false, selection: i } : { canceled: false, formValues: [typed] };
const menu = async (p, command = "jb:dave") => {
  const res = sim.command(command).cb({ sourceEntity: p });
  assert.equal(res.status, CustomCommandStatus.Success);
  assert.equal(res.message, undefined);
  sim.flush();
  await settle();
};
const asked = () => daveEvents().map((l) => JSON.parse(l.slice(l.indexOf("{"))).text);

test("an empty /dave, /d or /ai opens Dave's quick menu", async () => {
  const p = sim.player("Menu", "-40");
  wake(p);
  for (const name of ["jb:dave", "jb:d", "jb:ai"]) await menu(p, name);
  assert.equal(forms.shown.length, 3);
  for (const f of forms.shown) {
    assert.equal(f.title, "Dave");
    assert.deepEqual(f.buttons, MENU);
  }
  assert.deepEqual(asked(), []); // each closed: nothing asked, nothing said
  assert.deepEqual(p.messages, []);
});

test("each plain button asks its own words, as typed after /dave", async () => {
  const p = sim.player("Menu", "-41");
  wake(p);
  for (const i of [0, 1, 2]) {
    forms.answer = pick(i);
    await menu(p);
  }
  assert.deepEqual(asked(), ["Where am I?", "Point me home", "My last death"]);
  assert.equal(p.messages.at(-1), "§7Dave is thinking…");
});

test("Save this spot asks for a name, then saves it under that name", async () => {
  const p = sim.player("Menu", "-42");
  wake(p);
  forms.answer = pick(3, "  Base camp ");
  await menu(p);
  const box = forms.shown.at(-1);
  assert.equal(box.title, "Dave");
  assert.deepEqual(box.fields, [{ label: "Name this spot", placeholder: "base" }]);
  assert.deepEqual(asked(), ["save this spot as Base camp"]);
});

test("Ask something else… sends whatever they type; a blank box asks nothing", async () => {
  const p = sim.player("Menu", "-43");
  wake(p);
  forms.answer = pick(4, "where is the nearest pig");
  await menu(p);
  assert.deepEqual(forms.shown.at(-1).fields, [
    { label: "Ask Dave", placeholder: "where is the nearest village" },
  ]);
  forms.answer = pick(4, "   ");
  await menu(p);
  forms.answer = pick(3, "");
  await menu(p);
  assert.deepEqual(asked(), ["where is the nearest pig"]);
});

test("closing the menu or its text box does nothing", async () => {
  const p = sim.player("Menu", "-44");
  wake(p);
  await menu(p); // the menu closed
  forms.answer = (_p, form) =>
    form.buttons
      ? { canceled: false, selection: 3 }
      : { canceled: true, cancelationReason: FormCancelationReason.UserClosed };
  await menu(p); // the name box closed
  assert.equal(forms.shown.length, 3);
  assert.deepEqual(asked(), []);
  assert.deepEqual(p.messages, []);
});

test("a menu that can't show while chat is open waits, then falls back to the hint", async () => {
  const p = sim.player("Menu", "-45");
  wake(p);
  const busy = { canceled: true, cancelationReason: FormCancelationReason.UserBusy };
  let left = 2;
  forms.answer = (pl, form) => (left-- > 0 ? busy : pick(1)(pl, form));
  await menu(p);
  for (let i = 0; i < 3; i++) {
    sim.advance(20);
    await settle();
  }
  assert.equal(forms.shown.length, 3); // shown again each second until chat closed
  assert.deepEqual(asked(), ["Point me home"]);

  forms.answer = () => busy;
  await menu(p);
  for (let i = 0; i < 12; i++) {
    sim.advance(20);
    await settle();
  }
  assert.deepEqual(p.messages.slice(-1), [HINT]);
  assert.equal(forms.shown.length, 3 + 10);
});

test("no Tricorder, no menu: an empty /dave gets the static", async () => {
  const p = sim.player("Nobody", "-46");
  await menu(p);
  assert.deepEqual(forms.shown, []);
  assert.deepEqual(p.messages, ["§7§o" + LINES.noSignal]);
});

// --- the arrival chime (§M9) ------------------------------------------------------------

const ORB = "random.orb";
const chimes = (p) => p.sounds.filter((s) => s === ORB).length;
const setWaypoint = (player, x, z, label = "Base") =>
  sim.scriptevent("jb:waypoint", JSON.stringify({ player, x, z, dim: "overworld", label }));
const holding = (name, id) => {
  const p = sim.player(name, id);
  wake(p);
  p.selectedSlotIndex = p.inventory.slots.findIndex((s) => s?.typeId === "jbrain:tricorder");
  return p;
};

test("holding the Tricorder, reaching the waypoint chimes once, not every frame", () => {
  const p = holding("Walker", "-50");
  setWaypoint("Walker", 100, 100);
  sim.tick();
  assert.equal(chimes(p), 0);
  p.location = { x: 100, y: 64, z: 101 };
  for (let i = 0; i < 8; i++) sim.tick();
  assert.equal(p.actionbar.at(-1), "↑ Base · You're here");
  assert.equal(chimes(p), 1);
  p.location = { x: 100, y: 64, z: 120 }; // away, and back
  sim.tick();
  p.location = { x: 100, y: 64, z: 100 };
  sim.tick();
  assert.equal(chimes(p), 2);
  // The track drain's clear after it: the arrival has already rung.
  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Walker", clear: true }));
  assert.equal(chimes(p), 2);
});

test("carried, not held: the track drain's arrival clear chimes; Dave's clear doesn't", () => {
  const p = holding("Pocket", "-51");
  p.selectedSlotIndex = (p.selectedSlotIndex + 1) % 9; // the Tricorder in a pocket
  setWaypoint("Pocket", 10, 10);
  p.location = { x: 10, y: 64, z: 10 };
  sim.tick();
  assert.equal(chimes(p), 0);
  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Pocket", clear: true }));
  assert.equal(chimes(p), 1);
  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Pocket", clear: true })); // none left
  setWaypoint("Pocket", 10, 10);
  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Pocket", clear: true, end_scan: true }));
  assert.equal(chimes(p), 1);
});

test("no chime without your own Tricorder, or for a live scan's find", () => {
  const p = holding("Scanner", "-52");
  setWaypoint("Scanner", 0, 0); // where they stand, under the scan that follows
  sim.scriptevent("jb:scan", JSON.stringify({ player: "Scanner", block: "diamond_ore" }));
  sim.overworld.setBlockAt({ x: 0, y: 63, z: 0 }, "minecraft:diamond_ore");
  for (let i = 0; i < 4; i++) sim.tick();
  assert.match(p.actionbar.at(-1), /Scanning: diamond · You're here/);
  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Scanner", clear: true }));
  assert.equal(chimes(p), 0);

  const bare = sim.player("Bare", "-53");
  setWaypoint("Bare", 0, 0);
  sim.scriptevent("jb:waypoint", JSON.stringify({ player: "Bare", clear: true }));
  assert.deepEqual(bare.sounds, []);
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

test("every Power Pack carried gets the lore within a second, keeping all else", () => {
  const p = sim.player("Lored", "-90");
  wake(p);
  const crafted = new ItemStack("jbrain:power_pack", 3);
  crafted.nameTag = "Spare";
  p.inventory.setItem(4, crafted);
  const old = new ItemStack("jbrain:power_pack", 1);
  old.setLore(["an older wording"]);
  p.getComponent("minecraft:equippable").setEquipment("Offhand", old);
  p.inventory.setItem(5, new ItemStack("minecraft:ender_pearl", 2));
  for (let i = 0; i < 4; i++) sim.tick();
  const pack = p.inventory.slots[4];
  assert.deepEqual(pack.lore, [LINES.powerPackLore]);
  assert.equal(pack.amount, 3);
  assert.equal(pack.nameTag, "Spare");
  // A stale line is rewritten, so every pack carries the same lore and they stack alike.
  assert.deepEqual(p.equipment.Offhand.lore, [LINES.powerPackLore]);
  assert.deepEqual(p.inventory.slots[5].lore, []); // only packs
  assert.deepEqual(p.inventory.slots.find((s) => s?.typeId === "jbrain:tricorder").lore, []);
});

test("a lored pack is left alone: the look writes nothing back", () => {
  const p = sim.player("Steady", "-91");
  wake(p);
  const lored = new ItemStack("jbrain:power_pack", 2);
  lored.setLore([LINES.powerPackLore]);
  p.inventory.setItem(4, lored);
  let writes = 0;
  const setItem = p.inventory.setItem.bind(p.inventory);
  p.inventory.setItem = (i, v) => {
    writes += 1;
    setItem(i, v);
  };
  for (let i = 0; i < 4; i++) sim.tick();
  assert.equal(writes, 0);
  assert.equal(p.inventory.slots[4].amount, 2);
});

const lored = (n) => {
  const it = new ItemStack("jbrain:power_pack", n);
  it.setLore([LINES.powerPackLore]);
  return it;
};
const packs = (p) => p.inventory.slots.filter((s) => s?.typeId === "jbrain:power_pack");

test("a freshly crafted pack joins the lored stack once it gets the lore (#1694)", () => {
  const p = sim.player("Crafter", "-92");
  wake(p);
  p.inventory.setItem(2, lored(1));
  p.inventory.setItem(7, new ItemStack("jbrain:power_pack", 1)); // off the crafting table
  for (let i = 0; i < 4; i++) sim.tick();
  assert.deepEqual(packs(p).map((s) => s.amount), [2]);
  assert.equal(p.inventory.slots[2].amount, 2);
  assert.equal(p.inventory.slots[7], undefined);
});

test("folding a crafted pack in stops at 16 and loses none, even in a full inventory", () => {
  const p = sim.player("Hoarder", "-93");
  wake(p);
  p.inventory.setItem(2, lored(15));
  p.inventory.setItem(7, new ItemStack("jbrain:power_pack", 3));
  for (let i = 0; i < 4; i++) sim.tick();
  assert.equal(p.inventory.slots[2].amount, 16);
  assert.equal(p.inventory.slots[7].amount, 2); // the rest stays where it landed
  assert.deepEqual(p.inventory.slots[7].lore, [LINES.powerPackLore]);

  const full = sim.player("Packed", "-94");
  wake(full);
  for (let i = 0; i < full.inventory.size; i++) {
    if (!full.inventory.slots[i]) full.inventory.setItem(i, new ItemStack("minecraft:dirt", 64));
  }
  full.inventory.setItem(3, lored(16));
  full.inventory.setItem(9, lored(10));
  full.inventory.setItem(20, new ItemStack("jbrain:power_pack", 9));
  for (let i = 0; i < 4; i++) sim.tick();
  assert.deepEqual(packs(full).map((s) => s.amount), [16, 16, 3]);
  assert.ok(packs(full).every((s) => s.lore[0] === LINES.powerPackLore));
});

const packCount = (p) => packs(p).reduce((n, s) => n + s.amount, 0);

test("a write that fails mid-fold never makes a Power Pack, and loses none (#1698)", () => {
  // Slot 2 is the lored stack topped up; slot 7 the crafted pack folded in from.
  for (const failing of [2, 7]) {
    const p = sim.player(`Glitch${failing}`, `-9${failing}`);
    wake(p);
    p.inventory.setItem(2, lored(1));
    p.inventory.setItem(7, new ItemStack("jbrain:power_pack", 1));
    p.inventory.failNextWrite(failing);
    sim.tick(); // one look, the one that fails
    for (let i = 0; i < 3; i++) sim.tick();
    assert.equal(packCount(p), 2, `a failed write to slot ${failing}`);
    for (let i = 0; i < 4; i++) sim.tick(); // the next look folds it after all
    assert.deepEqual(packs(p).map((s) => s.amount), [2]);
  }
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

test("Dave's look around finds item stacks on the ground near an online player", () => {
  const p = sim.player("Steve 42", "-61");
  p.location = { x: 0.5, y: 64, z: 0.5 };
  sim.overworld.dropItem(new ItemStack("minecraft:iron_ingot", 3), { x: 10.2, y: 64.1, z: 0.7 });
  sim.overworld.dropItem(new ItemStack("minecraft:iron_ingot", 2), { x: 10.9, y: 64.4, z: 0.1 });
  sim.overworld.dropItem(new ItemStack("minecraft:diamond", 1), { x: -3, y: 63, z: 4 });
  sim.overworld.dropItem(new ItemStack("minecraft:diamond", 9), { x: 100, y: 64, z: 0 }); // too far
  sim.overworld.dropItem(new ItemStack("minecraft:dirt", 64), { x: 1, y: 64, z: 1 });
  sim.overworld.spawnEntity("minecraft:cow", { x: 2, y: 64, z: 2 });
  sim.logs.length = 0;
  sim.scriptevent("jb:items", JSON.stringify({ player: "steve 42", types: ["iron_ingot", "minecraft:diamond"] }));
  assert.deepEqual(sim.logs, [
    "[jbrain] items ground: 1 minecraft:diamond at x=-3 y=63 z=4, 5 blocks from Steve 42 (overworld)",
    "[jbrain] items ground: 5 minecraft:iron_ingot at x=10 y=64 z=0, 10 blocks from Steve 42 (overworld)",
  ]);
  sim.logs.length = 0;
  sim.scriptevent("jb:items", JSON.stringify({ player: "Steve 42", types: ["emerald"] }));
  assert.deepEqual(sim.logs, [
    "[jbrain] items none: no minecraft:emerald on the ground within 48 blocks of Steve 42",
  ]);
  sim.scriptevent("jb:items", JSON.stringify({ player: "Nobody", types: ["emerald"] }));
  assert.equal(sim.logs.at(-1), "[jbrain] items refused: Nobody isn't online");
  sim.scriptevent("jb:items", JSON.stringify({ player: "Steve 42", types: ["x; op @a"] }));
  assert.match(sim.logs.at(-1), /^\[jbrain\] items refused: Error: not 1-8 item types/);
});

test("Dave's carry count reads the asker's own inventory, armor and off-hand", () => {
  const p = sim.player("Steve 42", "-62");
  p.location = { x: 5.5, y: 70, z: -9.2 };
  p.inventory.setItem(0, new ItemStack("minecraft:iron_block", 3));
  p.inventory.setItem(8, new ItemStack("minecraft:iron_block", 2));
  p.inventory.setItem(9, new ItemStack("minecraft:blue_shulker_box", 1));
  p.getComponent("minecraft:equippable").setEquipment("Offhand", new ItemStack("minecraft:iron_block", 1));
  p.getComponent("minecraft:equippable").setEquipment("Head", new ItemStack("minecraft:iron_helmet", 1));
  // Someone else's pockets never count.
  const other = sim.player("Mira", "-63");
  other.inventory.setItem(0, new ItemStack("minecraft:iron_block", 64));
  sim.logs.length = 0;
  sim.scriptevent("jb:carry", JSON.stringify({ player: "steve 42", types: ["iron_block", "minecraft:iron_helmet"] }));
  assert.deepEqual(sim.logs, [
    "[jbrain] carry counted for Steve 42 in overworld at x=5 y=70 z=-10:"
      + " minecraft:iron_block=6 minecraft:iron_helmet=1; shulker boxes=1",
  ]);
  sim.scriptevent("jb:carry", JSON.stringify({ player: "Nobody", types: ["iron_block"] }));
  assert.equal(sim.logs.at(-1), "[jbrain] carry refused: Nobody isn't online");
  sim.scriptevent("jb:carry", JSON.stringify({ player: "Steve 42", types: ["x; op @a"] }));
  assert.match(sim.logs.at(-1), /^\[jbrain\] carry refused: Error: not 1-8 item types/);
});
