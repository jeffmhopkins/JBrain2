// The box's own behavior pack (docs/plans/MINECRAFT_BEDROCK_PLAN.md §T1, §M5): the
// deaths and respawns BDS never prints, and the in-game door to Dave (`/jb:dave …`), as
// one JSON line each on the console, which the wrapper turns into events. Stable
// @minecraft/server only.
import { system, world, CommandPermissionLevel, CustomCommandParamType,
  CustomCommandStatus } from "@minecraft/server";

const DIMENSIONS = {
  "minecraft:overworld": "overworld",
  "minecraft:nether": "nether",
  "minecraft:the_end": "the_end",
};

const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

const where = (entity) => {
  const { x, y, z } = entity.location;
  return {
    dim: DIMENSIONS[entity.dimension.id] ?? entity.dimension.id,
    x: Math.round(x * 10) / 10,
    y: Math.round(y * 10) / 10,
    z: Math.round(z * 10) / 10,
  };
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
  },
  { entityTypes: ["minecraft:player"] },
);

world.afterEvents.playerSpawn.subscribe((ev) => {
  if (ev.initialSpawn) return; // a log-in, which the join event already records
  emit({ ev: "respawn", name: ev.player.name, ...where(ev.player) });
});

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
  let spawn = null;
  try {
    spawn = bedOf(player);
  } catch {
    // An unreadable spawn point loses only the "home" fallback, not the question.
  }
  // The name is the entity's own, never typed. The wrapper attaches the xuid BDS logged
  // when this player connected, so the backend files the question under that account.
  emit({ ev: "dave", name: player.name, text: text.slice(0, QUESTION_MAX), ...where(player), spawn });
  player.sendMessage("§7Dave is thinking…");
};

system.beforeEvents.startup.subscribe((ev) => {
  ev.customCommandRegistry.registerCommand(
    {
      name: "jb:dave",
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
          message: 'Ask Dave something, like: /jb:dave point me home (use "quotes" for commas or numbers)',
        };
      }
      // A command callback runs read-only; reading the spawn point and messaging the
      // player wait for the next tick.
      system.run(() => askDave(player, text));
      return { status: CustomCommandStatus.Success };
    },
  );
});
