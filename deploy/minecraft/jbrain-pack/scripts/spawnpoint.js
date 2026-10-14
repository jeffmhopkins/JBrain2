// Each player's respawn point for the map (plan §M8, beds): one `spawnpoint` line when it
// changes. Stable 2.0.0 has no event for that, so it is read a tick after a bed or
// respawn anchor is used, on joining and after every respawn, and by a ten-second check
// of everyone online; only a change is sent, and a join always is (the resync). The
// rules are in spawnpoint_rules.js. The game API is passed in, so node tests drive this
// module with their own stand-in.
import { CHECK_TICKS, DIMENSIONS, blockKind, kindAt, samePoint, sameLine,
  spawnLine } from "./spawnpoint_rules.js";

const say = (msg) => console.log(`[jbrain] ${msg}`);
const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

export const initSpawnPoints = (world, system) => {
  // What each online player's last line said, by name: a rejoin or a script reload starts
  // empty, and the join's resend refills it.
  const sent = new Map();

  const read = (player) => {
    const sp = player.getSpawnPoint();
    if (!sp) return spawnLine(player.name, null);
    const point = {
      dim: DIMENSIONS[sp.dimension.id] ?? sp.dimension.id,
      x: Math.floor(sp.x),
      y: Math.floor(sp.y),
      z: Math.floor(sp.z),
    };
    const kind = kindAt((dx, dy, dz) =>
      sp.dimension.getBlock({ x: point.x + dx, y: point.y + dy, z: point.z + dz })?.typeId);
    const before = sent.get(player.name);
    // Unreadable (an unloaded chunk): what this session knew of the same point stands;
    // with no such memory (a join, a reload, a new point) it is "unknown", and the box,
    // which keeps the last kind per point, decides.
    const known = kind ?? (samePoint(before, point) ? before.kind : "unknown");
    return spawnLine(player.name, point, known);
  };

  const check = (player, always = false) => {
    if (player.isValid === false) return;
    let line;
    try {
      line = read(player);
    } catch (e) {
      say(`spawn point unreadable for ${player.name}: ${e}`);
      return;
    }
    const before = sent.get(player.name);
    if (!always && (sameLine(before, line) || (before?.kind === "none" && line.kind === "none"))) {
      return;
    }
    sent.set(player.name, line);
    emit(line);
  };

  world.afterEvents.playerInteractWithBlock.subscribe((ev) => {
    if (!blockKind(ev.block?.typeId) || ev.isFirstEvent === false) return;
    const player = ev.player;
    // A tick on: sleeping or charging sets the point after the interaction.
    system.run(() => check(player));
  });
  world.afterEvents.playerSpawn.subscribe(({ player, initialSpawn }) => {
    // A respawn may have cleared a bed that was broken or blocked: the game only checks
    // it then. Joining sends whatever it is, so the map catches up after a restart.
    system.run(() => check(player, initialSpawn));
  });
  world.afterEvents.playerLeave?.subscribe(({ playerName }) => sent.delete(playerName));
  system.runInterval(() => {
    for (const player of world.getAllPlayers()) check(player);
  }, CHECK_TICKS);
};
