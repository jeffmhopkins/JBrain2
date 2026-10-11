// The Replicator (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M15): a block Dave programs to
// make one thing, which burns any block into it like a furnace. Stable @minecraft/server
// and @minecraft/server-ui only, so it is a custom block with script-run "furnace" behaviour: a custom block can't
// hold a container, and scripts can't open a furnace's UI on one. It reads blocks from a
// container on top, fuel from one beside it and puts what it makes into one below — the
// sides a furnace's hoppers use — once per block tick, which the game only runs where the
// land is loaded. The rules are in replicator_rules.js.
import { system, world, BlockTypes, ItemStack } from "@minecraft/server";
import { ActionFormData, FormCancelationReason } from "@minecraft/server-ui";
import { EFFECTS, FUEL_LEFTOVER, PATTERN, PROGRAMMED, REPLICATOR, STATE, TARGET, WORKING,
  fuelTicks, parseProgram, parseRecord, programCheck, programmedLore, programmedName,
  panelBody, panelTitle, PANEL, recordKey, roomFor, takePlan, turn, unlocksReplicator } from "./replicator_rules.js";

const say = (msg) => console.log(`[jbrain] ${msg}`);

const keyOf = (block) => recordKey(block.dimension.id, block.location);
const readRecord = (block) => parseRecord(world.getDynamicProperty(keyOf(block)));
const writeRecord = (block, rec) =>
  world.setDynamicProperty(keyOf(block), rec ? JSON.stringify(rec) : undefined);

const targetOf = (item) => {
  if (item?.typeId !== PROGRAMMED) return undefined;
  const t = item.getDynamicProperty(TARGET);
  return typeof t === "string" ? t : undefined;
};

export const makeReplicator = (target) => {
  if (!target) return new ItemStack(REPLICATOR, 1);
  const item = new ItemStack(PROGRAMMED, 1);
  item.nameTag = programmedName(target);
  item.setLore([programmedLore(target)]);
  item.setDynamicProperty(TARGET, target);
  return item;
};

const maxAmountOf = (typeId) => {
  try {
    return new ItemStack(typeId, 1).maxAmount;
  } catch {
    return undefined; // no such item
  }
};

// --- programming, at Dave's request (mc_replicator_program) -----------------------------

const program = (message) => {
  const { player: name, target } = parseProgram(message);
  const player = world.getAllPlayers().find((p) => p.name.toLowerCase() === name.toLowerCase());
  if (!player) return `replicator refused: offline ${name}`;
  const inv = player.getComponent("minecraft:inventory")?.container;
  if (!inv) return `replicator refused: offline ${name}`;
  const slot = player.selectedSlotIndex;
  const held = inv.getItem(slot);
  const slots = [];
  for (let i = 0; i < inv.size; i++) {
    const it = inv.getItem(i);
    if (it) slots.push({ i, typeId: it.typeId, amount: it.amount });
  }
  const have = slots.reduce((n, s) => n + (s.typeId === target ? s.amount : 0), 0);
  const refused = programCheck({
    held: held && { typeId: held.typeId, target: targetOf(held) },
    target,
    maxAmount: maxAmountOf(target),
    have,
  });
  if (refused) {
    return `replicator refused: ${refused.code}${refused.detail === undefined ? "" : ` ${refused.detail}`}`;
  }
  for (const { i, left } of takePlan(slots, target, PATTERN)) {
    if (left > 0) {
      const it = inv.getItem(i);
      it.amount = left;
      inv.setItem(i, it);
    } else {
      inv.setItem(i, undefined);
    }
  }
  inv.setItem(slot, makeReplicator(target));
  player.playSound("random.anvil_use");
  return `replicator programmed: ${player.name} ${target} as ${programmedName(target)}`;
};

// --- the placed block ---------------------------------------------------------------------

const containerAt = (block) => {
  try {
    return block?.getComponent("minecraft:inventory")?.container;
  } catch {
    return undefined;
  }
};

const isBlock = (typeId) => typeId !== REPLICATOR && typeId !== PROGRAMMED
  && BlockTypes.get(typeId) !== undefined;

const firstSlot = (container, ok) => {
  if (!container) return -1;
  for (let i = 0; i < container.size; i++) {
    const it = container.getItem(i);
    if (it && ok(it)) return i;
  }
  return -1;
};

const takeOne = (container, i) => {
  const it = container.getItem(i);
  const leftover = FUEL_LEFTOVER[it.typeId];
  if (it.amount > 1) {
    it.amount -= 1;
    container.setItem(i, it);
  } else {
    container.setItem(i, leftover ? new ItemStack(leftover, 1) : undefined);
  }
};

const slotsOf = (container) => {
  const out = [];
  for (let i = 0; i < container.size; i++) {
    const it = container.getItem(i);
    out.push(it && { typeId: it.typeId, amount: it.amount, named: Boolean(it.nameTag) });
  }
  return out;
};

// Its working look (owner, 2026-10-11): lit and pulsing while it makes things, idle the
// tick it stops. Unloaded, it keeps whichever look it last had.
const setWorking = (block, on) => {
  const perm = block.permutation;
  if (perm.getState(WORKING) === on) return;
  block.setPermutation(perm.withState(WORKING, on));
};

// Which way the front faces: the placement state names it (the trait's 180° offset turns
// the front toward the player who placed it).
const FACING = { north: [0, -1], south: [0, 1], east: [1, 0], west: [-1, 0] };

// A burst at the front and a soft crackle for each item made. Cosmetic: a particle or
// sound the game doesn't know never stops the making.
const effects = (block) => {
  const [dx, dz] = FACING[block.permutation.getState("minecraft:cardinal_direction")] ?? [0, 0];
  const c = block.center();
  const at = { x: c.x + dx * 0.6, y: c.y, z: c.z + dz * 0.6 };
  for (const fx of [
    () => block.dimension.spawnParticle(EFFECTS.flame, at),
    () => block.dimension.spawnParticle(EFFECTS.sparkle, at),
    () => block.dimension.playSound(EFFECTS.sound, c, { volume: 0.6 }),
  ]) {
    try {
      fx();
    } catch {
      // A bad id costs only that effect.
    }
  }
};

// One block tick: every COOK_TICKS (the block's `minecraft:tick`), so only while its
// chunk ticks, and with no catching up after.
const onTick = ({ block }) => {
  const rec = readRecord(block);
  if (!rec) return;
  const input = containerAt(block.above());
  const output = containerAt(block.below());
  const fuelBoxes = [block.north(), block.south(), block.east(), block.west()]
    .map(containerAt)
    .filter(Boolean);
  const inSlot = firstSlot(input, (it) => isBlock(it.typeId));
  let fuelBox;
  let fuelSlot = -1;
  for (const c of fuelBoxes) {
    fuelSlot = firstSlot(c, (it) => fuelTicks(it.typeId) > 0);
    if (fuelSlot >= 0) {
      fuelBox = c;
      break;
    }
  }
  const room = Boolean(output) && roomFor(slotsOf(output), rec.target, maxAmountOf(rec.target) ?? 64);
  const plan = turn(rec, {
    input: inSlot >= 0,
    burn: fuelBox ? fuelTicks(fuelBox.getItem(fuelSlot).typeId) : 0,
    room,
  });
  if (plan.takeFuel) takeOne(fuelBox, fuelSlot);
  if (plan.takeInput) takeOne(input, inSlot);
  if (plan.make) {
    const spill = output.addItem(new ItemStack(rec.target, 1));
    if (spill) block.dimension.spawnItem(spill, block.below().center());
    effects(block);
  }
  setWorking(block, plan.make);
  if (plan.fuel !== rec.fuel || plan.state !== rec.state) {
    writeRecord(block, { target: rec.target, fuel: plan.fuel, state: plan.state });
  }
};

// Placed by a player: the held item says what it makes. The before-event is read-only,
// so the target waits here for onPlace, which runs for this placement next.
const pending = new Map();

const beforeOnPlayerPlace = (ev) => {
  let held;
  try {
    const inv = ev.player?.getComponent("minecraft:inventory")?.container;
    held = inv?.getItem(ev.player.selectedSlotIndex);
  } catch {
    return; // unreadable: it goes down blank
  }
  const target = targetOf(held);
  if (!target) return;
  ev.permutationToPlace = ev.permutationToPlace.withState(STATE, true);
  pending.set(keyOf(ev.block), target);
};

const onPlace = ({ block }) => {
  const key = keyOf(block);
  const target = pending.get(key);
  pending.delete(key);
  // A stale record (the block went some way that never told us) is overwritten either way.
  writeRecord(block, target ? { target, fuel: 0, state: "no_input" } : undefined);
};

// Its loot table is empty: the script gives back the item, programmed as it was. Fuel
// already burning in it is lost, as a furnace's is.
const onPlayerBreak = ({ block, dimension }) => {
  const rec = readRecord(block);
  writeRecord(block, undefined);
  dimension.spawnItem(makeReplicator(rec?.target), block.center());
};

// Players whose panel is open, so a second use doesn't stack another on top.
const panelOpen = new Set();

const onPlayerInteract = ({ block, player }) => {
  // Sneaking is placing a hopper or chest against it, as on a furnace: no panel.
  if (!player || player.isSneaking || panelOpen.has(player.id)) return;
  const rec = readRecord(block);
  const title = panelTitle(rec);
  const body = panelBody(rec);
  const inChat = () => player.sendMessage(`§6${title}§r\n${body}`);
  panelOpen.add(player.id);
  // A form can't open from inside a block event's tick; the next one is soon enough.
  system.run(() => {
    try {
      new ActionFormData().title(title).body(body).button(PANEL.ok)
        .show(player)
        .then((resp) => {
          panelOpen.delete(player.id);
          if (resp.canceled && resp.cancelationReason === FormCancelationReason.UserBusy) inChat();
        })
        .catch(() => {
          panelOpen.delete(player.id);
          inChat();
        });
    } catch {
      panelOpen.delete(player.id);
      inChat();
    }
  });
};

// --- §M19: Dave explains it once a player has 8 Power Packs on them ----------------------

const knownKey = (id) => `jb:known:${id}`;

const learnReplicator = () => {
  for (const player of world.getAllPlayers()) {
    try {
      const raw = world.getDynamicProperty(knownKey(player.id));
      const known = typeof raw === "string" ? JSON.parse(raw) : [];
      if (!Array.isArray(known) || known.includes("replicator")) continue;
      const inv = player.getComponent("minecraft:inventory")?.container;
      const stacks = [];
      for (let i = 0; i < (inv?.size ?? 0); i++) {
        const it = inv.getItem(i);
        if (it) stacks.push({ typeId: it.typeId, amount: it.amount });
      }
      if (!unlocksReplicator(stacks)) continue;
      // main.js keeps every name it finds here and adds the Tricorder if it's missing.
      world.setDynamicProperty(knownKey(player.id), JSON.stringify([...known, "replicator"].sort()));
    } catch {
      // A player mid-join is looked at again next second.
    }
  }
};

export const initReplicator = () => {
  system.beforeEvents.startup.subscribe((ev) => {
    ev.blockComponentRegistry.registerCustomComponent(REPLICATOR, {
      beforeOnPlayerPlace,
      onPlace,
      onPlayerBreak,
      onPlayerInteract,
      onTick,
    });
  });
  system.afterEvents.scriptEventReceive.subscribe((ev) => {
    if (ev.id !== "jb:replicator") return;
    try {
      say(program(ev.message));
    } catch (e) {
      say(`replicator refused: bad_request ${e}`);
    }
  });
  system.runInterval(learnReplicator, 20);
};
