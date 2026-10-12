// Dave's welcome and the first-time story beats (docs/plans/MINECRAFT_BEDROCK_PLAN.md
// §DW1; canon: Bleepbloop_City notes/lore.md, "Dave, the crew and the Tricorder"). The
// words are LINES in rules.js and the timings welcome_rules.js; this plays them, private
// to the player, and records each beat in the world so it is heard once. Scripted, never
// an AI turn: instant, the same for everyone, safe for kids. Stable @minecraft/server only.
import { system, world } from "@minecraft/server";
import { LINES, POWER_PACK } from "./rules.js";
import { switchIsOn } from "./transport_rules.js";
import { BEAT, firstDeathLines, helloTimeline, parseBeats, parseWreck, powerPackLines,
  welcomeTimeline } from "./welcome_rules.js";

const beatsKey = (id) => `jb:beats:${id}`;
// The wrapper's crash-site flag (jb:config's `wreck`); never sent reads as no wreck.
const WRECK = "jb:wreck";
const KEEP_SWITCH = "jb:power_pack_keep";
const TRANSPORT_SWITCH = "jb:power_pack_transport";
const HOTBAR = 9;
const ENDER_PEARL = "minecraft:ender_pearl";

const SPARKLE = "minecraft:endrod";
const CHIME = "note.chime";
const STATIC_SOUND = "random.fizz";
const BLACK = { red: 0, green: 0, blue: 0 };

// 2.0.0 made isValid a property; a method on older engines. Either way, gone is false.
const alive = (player) => {
  try {
    return typeof player.isValid === "function" ? player.isValid() : player.isValid !== false;
  } catch {
    return false;
  }
};

const wreckWorld = () => world.getDynamicProperty(WRECK) === true;

export const initWelcome = ({ beam, ownSlot, tricorderOn, say, dave }) => {
  // Who is mid-welcome, and who has Dave mid-speech (the welcome or a hello), by id. The
  // entity is kept so a player who left (a dead entity) never blocks their next join.
  const welcoming = new Map();
  const talking = new Map();
  const busy = (map, player) => {
    const p = map.get(player.id);
    return p !== undefined && alive(p);
  };
  const release = (map, player) => {
    if (map.get(player.id) === player) map.delete(player.id);
  };

  const beatsOf = (player) => parseBeats(world.getDynamicProperty(beatsKey(player.id)));
  const has = (player, beat) => beatsOf(player).includes(beat);
  const record = (player, beat) => {
    const beats = beatsOf(player);
    if (!beats.includes(beat)) world.setDynamicProperty(beatsKey(player.id), JSON.stringify([...beats, beat]));
  };

  // Each step re-checks the player: one who left mid-sequence ends it quietly.
  const play = (player, steps, run, done) => {
    const last = Math.max(...steps.map((s) => s.at));
    for (const s of steps) {
      const fire = () => {
        if (!alive(player)) return done();
        try {
          run(s);
        } catch (e) {
          say(`welcome step ${s.step} failed for ${player.name}: ${e}`);
        }
        if (s.at === last) done();
      };
      if (s.at === 0) fire();
      else system.runTimeout(fire, s.at);
    }
  };

  const sparkles = (player) => {
    // A column of light falling onto them over 2 s, with the chime climbing as it lands.
    for (let k = 0; k < 10; k++) {
      system.runTimeout(() => {
        if (!alive(player)) return;
        try {
          const { x, y, z } = player.location;
          const at = { x, y: y + 12 - k * 1.2, z };
          if (typeof player.spawnParticle === "function") player.spawnParticle(SPARKLE, at);
          else player.dimension.spawnParticle(SPARKLE, at);
          if (k % 2 === 0) player.playSound(CHIME, { pitch: 0.7 + k * 0.1 });
        } catch {
          // Sparkle and chime are flavour; the welcome goes on without them.
        }
      }, k * 4);
    }
  };

  const handTricorder = (player) => {
    // A player who already has their own (one from before DW1) keeps it: no second copy.
    if (tricorderOn(player) !== "own") beam(player, { hand: true });
    const i = ownSlot(player);
    if (i >= 0 && i < HOTBAR) player.selectedSlotIndex = i;
    player.playSound(CHIME, { pitch: 1.5 });
  };

  const welcomeStep = (player) => (s) => {
    const screen = player.onScreenDisplay;
    if (s.step === "dark") {
      screen.setActionBar(`§7§o${LINES.whereAmI}`);
      player.camera.fade({ fadeColor: BLACK, fadeTime: { fadeInTime: 0, holdTime: 2, fadeOutTime: 0.5 } });
    } else if (s.step === "sparkles") {
      sparkles(player);
    } else if (s.step === "whatsThis") {
      screen.setActionBar(`§7§o${LINES.whatsThis}`);
    } else if (s.step === "tricorder") {
      handTricorder(player);
    } else if (s.step === "signal") {
      player.playSound(STATIC_SOUND, { pitch: 0.5 });
      screen.setTitle(`§b${LINES.signal}`, {
        subtitle: `§7§o${LINES.signalSub}`,
        fadeInDuration: 10,
        stayDuration: 40,
        fadeOutDuration: 10,
      });
    } else if (s.step === "dave") {
      dave(player, s.text);
    }
  };

  // A first join in this world, including players from before DW1 (owner, 2026-10-12).
  // The beat is recorded as it starts, so a rejoin mid-welcome doesn't loop it.
  const welcome = (player) => {
    record(player, BEAT.welcome);
    welcoming.set(player.id, player);
    talking.set(player.id, player);
    play(player, welcomeTimeline(player.name, wreckWorld()), welcomeStep(player), () => {
      release(welcoming, player);
      release(talking, player);
    });
  };

  const needsWelcome = (player) => !has(player, BEAT.welcome);

  // A respawn during the welcome: the welcome brings the Tricorder, not the safety net.
  const inWelcome = (player) => busy(welcoming, player);

  const firstDeath = (player, savedByPack) => {
    if (has(player, BEAT.firstDeath)) return;
    record(player, BEAT.firstDeath);
    const steps = firstDeathLines(savedByPack).map((text, i) => ({ at: 20 + i * 60, step: "dave", text }));
    play(player, steps, (s) => dave(player, s.text), () => {});
  };

  // The first gate a player plants (§M12's beat, owner 2026-10-12), just after the
  // planted line, and only through their own Tricorder.
  const firstWormhole = (player) => {
    if (has(player, BEAT.firstWormhole) || tricorderOn(player) !== "own") return;
    record(player, BEAT.firstWormhole);
    play(player, [{ at: 30, step: "dave", text: LINES.firstWormhole }], (s) => dave(player, s.text), () => {});
  };

  // The first time a player holds 8 Power Packs (§M15's beat, owner 2026-10-12), on any
  // look of replicator.js's while they do: without their own Tricorder it stays unheard
  // for a later look, not spent with the unlock (#1684).
  const firstReplicator = (player) => {
    if (has(player, BEAT.firstReplicator) || tricorderOn(player) !== "own") return;
    record(player, BEAT.firstReplicator);
    dave(player, LINES.firstReplicator);
  };

  // After the item guide's look at what they carry (main.js's learn): `known` are all
  // their unlocks, `held` the item ids on them now.
  const onUnlocks = (player, { known, held }) => {
    // Any look with a pearl on them, not just the unlocking one: a pearl first held
    // without their own Tricorder keeps the line for later (#1684).
    const pearl = known.includes("power_pack") && held.includes(ENDER_PEARL);
    if (pearl && !has(player, BEAT.firstEnderPearl) && tricorderOn(player) === "own") {
      record(player, BEAT.firstEnderPearl);
      dave(player, LINES.firstEnderPearl);
    }
    if (!known.includes("power_pack_uses") || !held.includes(POWER_PACK)) return;
    if (has(player, BEAT.firstPowerPack)) return;
    const lines = powerPackLines({
      keep: world.getDynamicProperty(KEEP_SWITCH) === true,
      transport: switchIsOn(world.getDynamicProperty(TRANSPORT_SWITCH)),
    });
    // Nothing this world has built to tell of: unheard, so a later pack still gets it.
    if (!lines.length || tricorderOn(player) !== "own") return;
    record(player, BEAT.firstPowerPack);
    for (const line of lines) dave(player, line);
  };

  // `/dave hello`: the welcome's Dave lines again, no darkness and no Tricorder. Asked
  // again while Dave is still speaking, it waits for the first telling to end.
  const hello = (player) => {
    if (busy(talking, player)) return;
    talking.set(player.id, player);
    play(player, helloTimeline(player.name, wreckWorld()), (s) => dave(player, s.text), () =>
      release(talking, player),
    );
  };

  system.afterEvents.scriptEventReceive.subscribe((ev) => {
    if (ev.id !== "jb:config") return;
    try {
      const cfg = parseWreck(ev.message);
      if ("wreck" in cfg) world.setDynamicProperty(WRECK, cfg.wreck);
      say(`config wreck=${wreckWorld()}`);
    } catch {
      // main.js already said the config was refused.
    }
  });

  return {
    needsWelcome, welcome, inWelcome, firstDeath, firstWormhole, firstReplicator, onUnlocks, hello,
  };
};
