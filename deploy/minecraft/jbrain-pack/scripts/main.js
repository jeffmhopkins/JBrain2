// The box's own behavior pack (docs/plans/MINECRAFT_BEDROCK_PLAN.md §T1, §M5, §M9, §M10,
// §DW1): the deaths and respawns BDS never prints and the in-game door to Dave
// (`/jb:dave …`), as one JSON line each on the console, which the wrapper turns into
// events; and the crew's items, the Tricorder and the Power Pack, with the rules the
// canon gives them. Stable @minecraft/server only. The rules themselves are in rules.js;
// Dave's welcome and first-time beats are welcome.js.
import { system, world, CommandPermissionLevel, CustomCommandParamType,
  CustomCommandStatus, EquipmentSlot, ItemLockMode, ItemStack } from "@minecraft/server";
import { LINES, LORE, POWER_PACK, TRICORDER, carryLine, deathPlan, dropSpot, findLine, itemsLines,
  packLoreStale, packToBurn, parseCarry, parseConfig, parseFind, parseItems, parseKnown, parseWaypoint,
  slotFate, trackerLine, tricorderStatus, unlocksFrom, xpToDrop } from "./rules.js";
import { initWormholes } from "./wormhole.js";
import { initTransport } from "./transport.js";
import { initReplicator } from "./replicator.js";
import { initBeacon } from "./beacon.js";
import { initWelcome } from "./welcome.js";
import { isHello } from "./welcome_rules.js";

initBeacon(world, system);

const DIMENSIONS = {
  "minecraft:overworld": "overworld",
  "minecraft:nether": "nether",
  "minecraft:the_end": "the_end",
};

const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);
// A plain line, never a JSON event: the wrapper's event parser only takes `[jbrain] {…}`,
// and Dave's mc_set_waypoint reads these replies off the console.
const say = (msg) => console.log(`[jbrain] ${msg}`);

const DAVE = "§b[Dave]§r ";
const STATIC = "§7§o";

const where = (entity) => {
  const { x, y, z } = entity.location;
  return {
    dim: DIMENSIONS[entity.dimension.id] ?? entity.dimension.id,
    x: Math.round(x * 10) / 10,
    y: Math.round(y * 10) / 10,
    z: Math.round(z * 10) / 10,
  };
};

// --- the crew's items -------------------------------------------------------------------
// World properties: each owner's current Tricorder serial, the waypoint per gamertag (set
// by Dave while the player may be offline), and the wrapper's Power Pack switch.
const serialKey = (id) => `jb:tricorder:${id}`;
const waypointKey = (name) => `jb:waypoint:${name.toLowerCase()}`;
const KEEP_SWITCH = "jb:power_pack_keep";
// Each player's Old Maker unlocks in this world (§M19), keyed by their id like the
// Tricorder's serial, so the pack needs no backend for them.
const knownKey = (id) => `jb:known:${id}`;
// A player property: packs left after a burn, told on the respawn that follows.
const BURNT = "jb:burnt";
const EQUIPMENT = [EquipmentSlot.Head, EquipmentSlot.Chest, EquipmentSlot.Legs,
  EquipmentSlot.Feet, EquipmentSlot.Offhand];

const waypointOf = (name) => {
  const raw = world.getDynamicProperty(waypointKey(name));
  if (typeof raw !== "string") return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
};

const currentSerial = (id) => world.getDynamicProperty(serialKey(id));

const statusOf = (item, player) =>
  tricorderStatus(
    { owner: item.getDynamicProperty("jb:owner"), serial: item.getDynamicProperty("jb:serial") },
    player.id,
    currentSerial(player.id),
  );

// Every stack the player carries — inventory, armor, off-hand — with a way to write it
// back. A shulker box's contents are not "on" them, as the rule says.
const carried = (player) => {
  const out = [];
  const inv = player.getComponent("minecraft:inventory")?.container;
  if (inv) {
    for (let i = 0; i < inv.size; i++) {
      const item = inv.getItem(i);
      if (item) out.push({ item, put: (v) => inv.setItem(i, v), inv, slot: i });
    }
  }
  const eq = player.getComponent("minecraft:equippable");
  if (eq) {
    for (const slot of EQUIPMENT) {
      const item = eq.getEquipment(slot);
      if (item) out.push({ item, put: (v) => eq.setEquipment(slot, v) });
    }
  }
  return out;
};

// "own" when the player carries their own current Tricorder, "foreign" when they carry
// only someone else's (or a retired one), "none" when they carry none at all.
const tricorderOn = (player) => {
  let found = "none";
  for (const { item } of carried(player)) {
    if (item.typeId !== TRICORDER) continue;
    if (statusOf(item, player) === "own") return "own";
    found = "foreign";
  }
  return found;
};

// A retired copy reads as one and lets go: unlocked and no longer kept on death, so its
// holder can be rid of it.
const retire = (item, put) => {
  item.setLore([LINES.retired]);
  item.lockMode = ItemLockMode.none;
  item.keepOnDeath = false;
  put(item);
};

// Bound to its owner (DW1): locked in their inventory, kept through a death, named for
// them, and carrying their id and a serial that a newer copy outdates.
const makeTricorder = (player, serial) => {
  const item = new ItemStack(TRICORDER, 1);
  item.nameTag = `${player.name}'s Tricorder`;
  item.setLore([LORE]);
  item.lockMode = ItemLockMode.inventory;
  item.keepOnDeath = true;
  item.setDynamicProperty("jb:owner", player.id);
  item.setDynamicProperty("jb:serial", serial);
  return item;
};

// The selected slot when `hand` asks for it and it's free (the welcome's "in their hand"),
// else the first free one.
const beam = (player, { hand = false } = {}) => {
  const serial = (Number(currentSerial(player.id)) || 0) + 1;
  world.setDynamicProperty(serialKey(player.id), serial);
  const item = makeTricorder(player, serial);
  const inv = player.getComponent("minecraft:inventory")?.container;
  if (!inv) return;
  for (const c of carried(player)) {
    if (c.item.typeId === TRICORDER && c.item.getDynamicProperty("jb:owner") === player.id) {
      retire(c.item, c.put);
    }
  }
  const slot = player.selectedSlotIndex;
  if (hand && !inv.getItem(slot)) {
    inv.setItem(slot, item);
    return;
  }
  if (inv.addItem(item) === undefined) return;
  // A full inventory: the selected slot's stack makes room, at the player's feet.
  const displaced = inv.getItem(slot);
  if (displaced) player.dimension.spawnItem(displaced, player.location);
  inv.setItem(slot, item);
};

// Where the player's own current Tricorder sits in their inventory, or -1.
const ownSlot = (player) => {
  const inv = player.getComponent("minecraft:inventory")?.container;
  if (!inv) return -1;
  for (let i = 0; i < inv.size; i++) {
    const item = inv.getItem(i);
    if (item?.typeId === TRICORDER && statusOf(item, player) === "own") return i;
  }
  return -1;
};

const dave = (player, text) => player.sendMessage(DAVE + text);

const story = initWelcome({ beam, ownSlot, tricorderOn, say, dave });

const onWaking = (player, initialSpawn) => {
  const left = player.getDynamicProperty(BURNT);
  const savedByPack = typeof left === "number";
  if (savedByPack) {
    player.setDynamicProperty(BURNT, undefined);
    player.sendMessage(`§7${LINES.burnt(left)}`);
    player.playSound("random.fizz");
  }
  // The welcome hands over the Tricorder itself, at its 5 s mark.
  if (initialSpawn && story.needsWelcome(player)) {
    story.welcome(player);
    return;
  }
  if (!story.inWelcome(player)) {
    if (currentSerial(player.id) === undefined) {
      // Never beamed one (they left before the welcome's beam): nothing was lost.
      beam(player);
    } else if (tricorderOn(player) !== "own") {
      beam(player);
      dave(player, LINES.rebeamed);
    }
  }
  if (!initialSpawn) story.firstDeath(player, savedByPack);
};

// --- the item guide's unlocks (§M19) ----------------------------------------------------

// Bedrock never merges two stacks already in separate slots, so a crafted pack lored
// where it landed would sit apart from the lored stack for good (#1694): it tops up any
// lored stack with room, and only what doesn't fit stays in its own slot. Folded by hand,
// not re-added, so a pack with nowhere to merge never jumps to another slot.
const fold = (inv, slot, item) => {
  let left = item.amount;
  for (let j = 0; j < inv.size && left > 0; j++) {
    const other = j === slot ? undefined : inv.getItem(j);
    if (!other?.isStackableWith(item)) continue;
    const n = Math.min(other.maxAmount - other.amount, left);
    if (n <= 0) continue;
    other.amount += n;
    // The top-up is written first: a failure between the two writes doubles a pack
    // rather than losing one.
    inv.setItem(j, other);
    left -= n;
  }
  if (left === 0) return inv.setItem(slot, undefined);
  item.amount = left;
  inv.setItem(slot, item);
};

// What the player has unlocked, after a look at what they carry now; written back only
// when something new was unlocked. The same look lores their Power Packs (§M10): a
// crafted one comes without, and only a stack written back keeps it.
const learn = (player) => {
  const before = parseKnown(world.getDynamicProperty(knownKey(player.id)));
  const stacks = carried(player);
  for (const { item, put, inv, slot } of stacks) {
    if (!packLoreStale(item.typeId, item.getLore())) continue;
    item.setLore([LINES.powerPackLore]);
    if (inv) fold(inv, slot, item);
    else put(item);
  }
  const held = stacks.map((c) => c.item.typeId);
  const after = unlocksFrom(before, held);
  if (after.join() !== [...before].sort().join()) {
    world.setDynamicProperty(knownKey(player.id), JSON.stringify(after));
  }
  try {
    story.onUnlocks(player, { known: after, held });
  } catch (e) {
    say(`story beat failed for ${player.name}: ${e}`);
  }
  return after;
};

// --- deaths: the travel log, and the Power Pack charm -----------------------------------

// Where each player last stood on solid ground, for a void death's drops.
const lastSafe = new Map();

const isVanishing = (item) => {
  try {
    return Boolean(item.getComponent("minecraft:enchantable")?.hasEnchantment("vanishing"));
  } catch {
    return false;
  }
};

const switchOn = () => world.getDynamicProperty(KEEP_SWITCH) === true;

const keepRule = () => {
  try {
    return world.gameRules.keepInventory !== false;
  } catch {
    return true; // unreadable: assume the wrapper's rule held, the fail-safe direction
  }
};

const burnOne = (packs) => {
  const pick = packToBurn(packs.map((p, i) => ({ i, amount: p.item.amount })));
  const { item, put } = packs[pick.i];
  if (item.amount > 1) {
    item.amount -= 1;
    put(item);
  } else {
    put(undefined);
  }
};

const dropAll = (player) => {
  const dim = player.dimension;
  const spot = dropSpot(
    { ...player.location, dim: dim.id },
    dim.heightRange.min,
    lastSafe.get(player.id),
  );
  const loc = { x: spot.x, y: spot.y, z: spot.z };
  for (const { item, put } of carried(player)) {
    const fate = slotFate({ keepOnDeath: item.keepOnDeath, vanishing: isVanishing(item) });
    if (fate === "stay") continue;
    if (fate === "drop") dim.spawnItem(item, loc);
    put(undefined);
  }
  const xp = xpToDrop(player.level);
  player.resetLevel();
  for (let i = 0; i < xp; i++) dim.spawnEntity("minecraft:xp_orb", loc);
  const at = [spot.x, spot.y, spot.z].map(Math.round).join(" ");
  say(`power pack: ${player.name} dropped their things at ${at} (${DIMENSIONS[spot.dim] ?? spot.dim})`);
};

const charm = (player) => {
  if (!switchOn() || !keepRule()) return; // vanilla: the game does the dropping
  const stacks = carried(player);
  const packs = stacks.filter((s) => s.item.typeId === POWER_PACK);
  const plan = deathPlan({
    switchOn: true,
    keepRule: true,
    hasPack: packs.length > 0,
    hasOwnTricorder: stacks.some(
      (s) => s.item.typeId === TRICORDER && statusOf(s.item, player) === "own",
    ),
  });
  if (plan === "keep") {
    const before = packs.reduce((n, p) => n + p.item.amount, 0);
    burnOne(packs);
    player.setDynamicProperty(BURNT, before - 1);
    say(`power pack: ${player.name} kept their things; ${before - 1} left`);
  } else if (plan === "drop") {
    dropAll(player);
  }
};

world.afterEvents.entityDie.subscribe(
  (ev) => {
    const player = ev.deadEntity;
    let place = {};
    try {
      place = where(player);
    } catch {
      // A player gone from the world at the instant of death still has a death worth
      // recording; the timeline places it at their last sample instead.
    }
    emit({
      ev: "death",
      name: player.name,
      ...place,
      cause: ev.damageSource.cause,
      killer: ev.damageSource.damagingEntity?.typeId ?? null,
    });
    try {
      charm(player);
    } catch (e) {
      // keepInventory is on, so a fault here leaves them keeping everything (§M10).
      say(`power pack failed for ${player.name}: ${e}`);
    }
  },
  { entityTypes: ["minecraft:player"] },
);

world.afterEvents.playerSpawn.subscribe((ev) => {
  const player = ev.player;
  if (!ev.initialSpawn) emit({ ev: "respawn", name: player.name, ...where(player) });
  // A tick on, so the joining player's inventory is there to read and write.
  system.run(() => {
    try {
      onWaking(player, ev.initialSpawn);
    } catch (e) {
      say(`waking failed for ${player.name}: ${e}`);
    }
  });
});

// --- scriptevents from the wrapper and from Dave's tools --------------------------------

system.afterEvents.scriptEventReceive.subscribe((ev) => {
  if (ev.id === "jb:waypoint") {
    try {
      const wp = parseWaypoint(ev.message);
      if (wp.clear) {
        world.setDynamicProperty(waypointKey(wp.player), undefined);
        say(`waypoint cleared for ${wp.player}`);
      } else {
        world.setDynamicProperty(waypointKey(wp.player), JSON.stringify(wp.record));
        const r = wp.record;
        say(`waypoint set for ${wp.player}: ${r.label} at ${r.x} ${r.z} (${r.dim})`);
      }
    } catch (e) {
      say(`waypoint refused: ${e}`);
    }
  } else if (ev.id === "jb:find") {
    // Dave's nearest-mob search: only the game knows where mobs are, and only near players.
    try {
      const q = parseFind(ev.message);
      const who = q.player.toLowerCase();
      const p = world.getAllPlayers().find((pl) => pl.name.toLowerCase() === who);
      if (!p) {
        say(`find refused: ${q.player} isn't online`);
        return;
      }
      const [mob] = p.dimension.getEntities({
        type: q.type,
        location: p.location,
        closest: 1,
        maxDistance: q.max,
      });
      const here = { dim: p.dimension.id, x: p.location.x, z: p.location.z };
      say(findLine(q, p.name, here, mob?.location));
    } catch (e) {
      say(`find refused: ${e}`);
    }
  } else if (ev.id === "jb:items") {
    // Dave's look around for things on the ground (§M23): a death's drops, a dropped
    // stack. Item entities, like mobs, exist only in the chunks around players.
    try {
      const q = parseItems(ev.message);
      const who = q.player.toLowerCase();
      const p = world.getAllPlayers().find((pl) => pl.name.toLowerCase() === who);
      if (!p) {
        say(`items refused: ${q.player} isn't online`);
        return;
      }
      const stacks = [];
      for (const e of p.dimension.getEntities({
        type: "minecraft:item",
        location: p.location,
        maxDistance: q.max,
      })) {
        const stack = e.getComponent("minecraft:item")?.itemStack;
        if (stack && q.types.includes(stack.typeId)) {
          stacks.push({ typeId: stack.typeId, amount: stack.amount, location: e.location });
        }
      }
      const here = { dim: p.dimension.id, x: p.location.x, z: p.location.z };
      for (const line of itemsLines(q, p.name, here, stacks)) say(line);
    } catch (e) {
      say(`items refused: ${e}`);
    }
  } else if (ev.id === "jb:carry") {
    // What the asker carries toward a goal (§M24): their own inventory, armor and
    // off-hand, nobody else's — Dave only ever sends the asker's own name.
    try {
      const q = parseCarry(ev.message);
      const who = q.player.toLowerCase();
      const p = world.getAllPlayers().find((pl) => pl.name.toLowerCase() === who);
      if (!p) {
        say(`carry refused: ${q.player} isn't online`);
        return;
      }
      const stacks = carried(p).map(({ item }) => ({ typeId: item.typeId, amount: item.amount }));
      const here = { dim: p.dimension.id, x: p.location.x, y: p.location.y, z: p.location.z };
      say(carryLine(q, p.name, here, stacks));
    } catch (e) {
      say(`carry refused: ${e}`);
    }
  } else if (ev.id === "jb:config") {
    try {
      const cfg = parseConfig(ev.message);
      if ("power_pack_keep" in cfg) world.setDynamicProperty(KEEP_SWITCH, cfg.power_pack_keep);
      say(`config power_pack_keep=${switchOn()}`);
    } catch (e) {
      say(`config refused: ${e}`);
    }
  }
});

initTransport({ carried, tricorderOn, say });
initWormholes({ onPlanted: (player) => story.firstWormhole(player) });

// --- the Tricorder's arrow, about four times a second -----------------------------------

// Unlocks are looked for once a second, not every frame: the whole inventory is read.
const LEARN_EVERY = 4;
let frame = 0;

system.runInterval(() => {
  frame = (frame + 1) % LEARN_EVERY;
  for (const player of world.getAllPlayers()) {
    try {
      if (player.isOnGround && player.location.y >= player.dimension.heightRange.min) {
        lastSafe.set(player.id, { ...player.location, dim: player.dimension.id });
      }
      if (frame === 0) learn(player);
      const inv = player.getComponent("minecraft:inventory")?.container;
      const slot = player.selectedSlotIndex;
      const held = inv?.getItem(slot);
      if (held?.typeId !== TRICORDER) continue;
      const status = statusOf(held, player);
      if (status === "own") {
        player.onScreenDisplay.setActionBar(
          trackerLine(
            waypointOf(player.name),
            { dim: player.dimension.id, x: player.location.x, z: player.location.z },
            player.getViewDirection(),
            world.getDefaultSpawnLocation(),
          ),
        );
      } else if (status === "foreign") {
        player.onScreenDisplay.setActionBar(STATIC + LINES.notTuned);
      } else {
        if (held.lockMode !== ItemLockMode.none) retire(held, (v) => inv.setItem(slot, v));
        player.onScreenDisplay.setActionBar(LINES.retired);
      }
    } catch {
      // One player's unreadable state (mid-join, mid-dimension change) skips a frame.
    }
  }
}, 5);

// --- /jb:dave, the in-game door ---------------------------------------------------------

// The backend caps a question at this; cutting it here keeps one console line short.
const QUESTION_MAX = 256;
// Each word is its own optional parameter, so "point me home" needs no quotes. Checked on
// 1.26.52: a String parameter takes one plain word or one "quoted phrase", a comma or a
// bare number in it is a syntax error the engine shows the player, and eight is the cap
// on a custom command's parameters — so a longer question, or one with a comma or
// coordinates, goes in quotes: /jb:dave "Dave, mark 100 64 -20 as base".
const WORDS = 8;

const bedOf = (player) => {
  // undefined when the player has no bed or respawn anchor set, so "home" can't fall
  // back to it.
  const sp = player.getSpawnPoint();
  if (!sp) return null;
  return {
    x: Math.floor(sp.x),
    y: Math.floor(sp.y),
    z: Math.floor(sp.z),
    dim: DIMENSIONS[sp.dimension.id] ?? sp.dimension.id,
  };
};

const askDave = (player, text) => {
  // No Tricorder, no Dave (DW1): the question never reaches the backend, so no AI turn.
  const tricorder = tricorderOn(player);
  if (tricorder !== "own") {
    player.sendMessage(STATIC + (tricorder === "none" ? LINES.noSignal : LINES.notTuned));
    return;
  }
  // The welcome's lines again, answered here: no question for the backend.
  if (isHello(text)) {
    story.hello(player);
    return;
  }
  let spawn = null;
  try {
    spawn = bedOf(player);
  } catch {
    // An unreadable spawn point loses only the "home" fallback, not the question.
  }
  // The name is the entity's own, never typed. The wrapper attaches the xuid BDS logged
  // when this player connected, so the backend files the question under that account.
  let known = ["tricorder"];
  try {
    known = learn(player);
  } catch {
    // Unreadable unlocks cost only the guide's later entries, never the question.
  }
  emit({
    ev: "dave",
    name: player.name,
    text: text.slice(0, QUESTION_MAX),
    ...where(player),
    spawn,
    known,
  });
  player.sendMessage("§7Dave is thinking…");
};

// Bedrock also answers each name without its namespace, so players type /dave, /d or /ai;
// the short ones are the owner's ask (2026-10-12), and all share one handler.
const DAVE_COMMANDS = ["jb:dave", "jb:d", "jb:ai"];

system.beforeEvents.startup.subscribe((ev) => {
  for (const name of DAVE_COMMANDS) registerDave(ev.customCommandRegistry, name);
});

const registerDave = (registry, name) => {
  registry.registerCommand(
    {
      name,
      description: "Ask Dave",
      permissionLevel: CommandPermissionLevel.Any,
      optionalParameters: Array.from({ length: WORDS }, (_, i) => ({
        name: i === 0 ? "question" : `word${i + 1}`,
        type: CustomCommandParamType.String,
      })),
    },
    (origin, ...words) => {
      const player = origin.sourceEntity;
      if (player?.typeId !== "minecraft:player") {
        return { status: CustomCommandStatus.Failure, message: "Only a player can ask Dave." };
      }
      const text = words.filter((w) => typeof w === "string").join(" ").trim();
      if (!text) {
        return {
          status: CustomCommandStatus.Success,
          message: 'Ask Dave something, like: /dave point me home (use "quotes" for commas or numbers)',
        };
      }
      // A command callback runs read-only; reading the inventory and the spawn point and
      // messaging the player wait for the next tick.
      system.run(() => askDave(player, text));
      return { status: CustomCommandStatus.Success };
    },
  );
};

initReplicator({ onUnlocked: (player) => story.firstReplicator(player) });
