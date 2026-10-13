// The No-Spawn Beacon (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M16): placed, it keeps the
// 3×3 of chunks round it clear of mobs, full height, with no Dave to switch it on; broken,
// it stops. A spawn in a covered chunk is checked at once, and once a second a sweep of
// each loaded area catches what walked, fell, teleported or came through a portal in.
// The rules are in beacon_rules.js. The game API is passed in, so node tests drive this
// module with their own stand-in.
import { BEACON, KEPT, RECORD_PREFIX, REMOVABLE, areaOf, beaconEvent, beaconsSnapshot,
  buildIndex, isProtected, keepReason, makeRecord, parseRecord, recordKey } from "./beacon_rules.js";

const SWEEP_TICKS = 20;
// "Beamed away by Dave": white sparks up and a puff, never a death (so nothing drops).
const PARTICLES = ["minecraft:endrod", "minecraft:basic_smoke_particle"];

const say = (msg) => console.log(`[jbrain] ${msg}`);
// The prose lines stay for people reading the log; this one is the map's and Dave's.
const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

export const initBeacon = (world, system) => {
  // Read lazily: the world's properties can't be read while the script first loads.
  let records;
  let index = new Map();

  const load = () => {
    if (records) return;
    records = new Map();
    for (const id of world.getDynamicPropertyIds()) {
      if (!id.startsWith(RECORD_PREFIX)) continue;
      const rec = parseRecord(world.getDynamicProperty(id));
      if (rec) records.set(id, rec);
    }
    index = buildIndex(records.values());
  };

  const remember = (dim, at, by) => {
    load();
    const rec = makeRecord(dim, at, by);
    const key = recordKey(dim, rec);
    world.setDynamicProperty(key, JSON.stringify({ dim, x: rec.x, y: rec.y, z: rec.z, by }));
    records.set(key, rec);
    index = buildIndex(records.values());
    emit(beaconEvent("on", rec, by));
    say(`no-spawn beacon on at ${rec.x} ${rec.y} ${rec.z} (${dim})`);
  };

  // `by` is who broke it; empty when the sweep found it gone (TNT, a piston, a command).
  const forget = (dim, at, why, by = "") => {
    load();
    const key = recordKey(dim, at);
    const rec = records.get(key);
    if (!rec) return;
    world.setDynamicProperty(key, undefined);
    records.delete(key);
    index = buildIndex(records.values());
    emit(beaconEvent("off", rec, by));
    say(`no-spawn beacon off at ${Math.floor(at.x)} ${Math.floor(at.y)} ${Math.floor(at.z)} (${dim}): ${why}`);
  };

  // The type check first: most entities in a sweep are items, XP and players, whose
  // components are never read.
  const goes = (entity) => {
    if (!REMOVABLE.has(entity.typeId)) return false;
    return keepReason({
      typeId: entity.typeId,
      tamed: Boolean(entity.getComponent("minecraft:is_tamed")),
      named: Boolean(entity.nameTag),
      leashed: Boolean(entity.getComponent("minecraft:leashable")?.isLeashed),
      kept: entity.getDynamicProperty(KEPT) === true,
    }) === null;
  };

  const vanish = (entity) => {
    const { dimension, location } = entity;
    const at = { x: location.x, y: location.y + 0.8, z: location.z };
    for (const p of PARTICLES) {
      try {
        dimension.spawnParticle(p, at);
      } catch {
        // A missing particle costs the puff, never the removal.
      }
    }
    entity.remove();
  };

  const trySweep = (entity) => {
    try {
      if (entity.isValid && goes(entity)) vanish(entity);
    } catch {
      // An entity gone between the query and the read is already what we wanted.
    }
  };

  world.afterEvents.entitySpawn.subscribe(({ entity, cause }) => {
    try {
      // Bred by a player: looked after from birth, wherever it was born.
      if (cause === "Born") {
        if (REMOVABLE.has(entity.typeId)) entity.setDynamicProperty(KEPT, true);
        return;
      }
      load();
      if (!index.has(entity.dimension.id)) return;
      if (!isProtected(index, entity.dimension.id, entity.location)) return;
      trySweep(entity);
    } catch (e) {
      say(`no-spawn beacon spawn check failed: ${e}`);
    }
  });

  world.afterEvents.playerInteractWithEntity.subscribe(({ target }) => {
    try {
      if (REMOVABLE.has(target.typeId)) target.setDynamicProperty(KEPT, true);
    } catch {
      // A target gone mid-interaction needs no mark.
    }
  });

  world.afterEvents.playerPlaceBlock.subscribe(({ block, dimension, player }) => {
    if (block.typeId === BEACON) remember(dimension.id, block.location, player?.name ?? "");
  });

  world.afterEvents.playerBreakBlock.subscribe(({ block, brokenBlockPermutation, dimension, player }) => {
    if (brokenBlockPermutation.type.id === BEACON) {
      forget(dimension.id, block.location, "broken", player?.name ?? "");
    }
  });

  let reported = false;
  system.runInterval(() => {
    try {
      load();
    } catch (e) {
      say(`no-spawn beacon records unreadable: ${e}`);
      return;
    }
    // Once per load, after the first tick: the properties can't be read any sooner.
    if (!reported) {
      reported = true;
      emit(beaconsSnapshot(records.values()));
    }
    for (const rec of [...records.values()]) {
      try {
        const dim = world.getDimension(rec.dim);
        let block;
        try {
          block = dim.getBlock({ x: rec.x, y: rec.y, z: rec.z });
        } catch {
          block = undefined;
        }
        // An unloaded beacon chunk has no mobs worth a sweep; it is looked at again when
        // a player is near.
        if (!block) continue;
        if (block.typeId !== BEACON) {
          forget(rec.dim, rec, "gone");
          continue;
        }
        const area = areaOf(rec);
        const { min, max } = dim.heightRange;
        const found = dim.getEntities({
          location: { x: area.x, y: min, z: area.z },
          volume: { x: area.size, y: max - min, z: area.size },
          excludeTypes: ["minecraft:player"],
        });
        for (const entity of found) trySweep(entity);
      } catch (e) {
        say(`no-spawn beacon sweep failed at ${rec.x} ${rec.y} ${rec.z}: ${e}`);
      }
    }
  }, SWEEP_TICKS);
};
