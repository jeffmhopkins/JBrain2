// The jbrain behavior pack's first slice (docs/plans/MINECRAFT_BEDROCK_PLAN.md §T1):
// the deaths and respawns BDS never prints, as one JSON line each on the console,
// which the wrapper turns into timeline events. Stable @minecraft/server only.
import { world } from "@minecraft/server";

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
