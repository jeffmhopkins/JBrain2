// The Tricorder's live scan (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M25): "Dave, set my
// Tricorder to scan for diamonds". Dave's mc_tricorder_scan sends `scriptevent jb:scan
// {json}`; the scan is kept per gamertag as a world property, like a waypoint, so it
// lasts across sessions and can be set while the player is offline. Every 10 s the
// Tricorder finds the nearest such block within 16 blocks and the held arrow points at
// it. The waypoint under it is left alone, so stopping the scan points back there. The
// pure rules are in rules.js. Stable @minecraft/server only.
import { world, BlockTypes, BlockVolume } from "@minecraft/server";
import { SCAN_EVERY, inScanBox, nearestBlock, parseScan, pickScan, scanBox, scanLabel,
  scanLine, scanTwins } from "./rules.js";

const scanKey = (name) => `jb:scan:${name.toLowerCase()}`;
// main.js's arrow loop runs every 5 ticks and calls step() once a frame.
const FRAME_TICKS = 5;
const EVERY_FRAMES = SCAN_EVERY / FRAME_TICKS;
// A mined target is looked past this soon, not at the next 10 s scan; no sooner, so a
// player digging through a vein of what they scan for doesn't scan every frame.
const MINED_RESCAN_FRAMES = 4;
// The console hears how long a scan took the first time, and whenever one is slow: the
// box check for getBlocks' cost on the real server.
const SLOW_MS = 10;

const blockKnown = (id) => {
  try {
    return BlockTypes.get(id) !== undefined;
  } catch {
    return false;
  }
};

const scanOf = (name) => {
  const raw = world.getDynamicProperty(scanKey(name));
  if (typeof raw !== "string") return undefined;
  try {
    const s = JSON.parse(raw);
    return Array.isArray(s.types) && s.types.length ? s : undefined;
  } catch {
    return undefined;
  }
};

export const initScan = ({ say }) => {
  // In memory only: when each scanning player is next due (in frames), and the block the
  // last scan found (null for none). A rejoin or a restart scans afresh at once.
  const due = new Map();
  const found = new Map();
  let frame = 0;
  let timed = false;

  const forget = (key) => {
    due.delete(key);
    found.delete(key);
  };

  // Ends a player's scan; true when one was on. Their waypoint was never touched, so the
  // arrow points back at it (or spawn).
  const end = (name) => {
    const was = scanOf(name) !== undefined;
    world.setDynamicProperty(scanKey(name), undefined);
    forget(name.toLowerCase());
    return was;
  };

  const start = (q) => {
    // "diamond" isn't a block but "diamond_ore" is: the ore is what a player means.
    const block = [q.block, `${q.block}_ore`].find(blockKnown);
    if (!block) {
      say(`scan refused: ${q.block} isn't a block`);
      return;
    }
    const types = scanTwins(block).filter((t) => t === block || blockKnown(t));
    const label = scanLabel(block);
    world.setDynamicProperty(scanKey(q.player), JSON.stringify({ block, types, label }));
    const key = q.player.toLowerCase();
    found.delete(key);
    due.set(key, frame);
    say(`scan set for ${q.player}: ${label} (${types.join(", ")})`);
  };

  const scanFor = (player, scan) => {
    const dim = player.dimension;
    const box = scanBox(player.location, dim.heightRange);
    const t0 = Date.now();
    const hits = dim.getBlocks(new BlockVolume(box.from, box.to), { includeTypes: scan.types }, true);
    const target = nearestBlock(player.location, hits.getBlockLocationIterator());
    const ms = Date.now() - t0;
    if (!timed || ms >= SLOW_MS) {
      timed = true;
      say(`scan took ${ms} ms for ${player.name} (${scan.label})`);
    }
    return target ?? null;
  };

  // One scan a frame at most, for whichever scanning player is most overdue.
  const step = () => {
    frame += 1;
    const online = new Map(world.getAllPlayers().map((p) => [p.name.toLowerCase(), p]));
    for (const key of [...due.keys()]) if (!online.has(key)) forget(key);
    for (const [key, p] of online) if (!due.has(key) && scanOf(p.name)) due.set(key, frame);
    const key = pickScan(due, frame);
    if (key === undefined) return;
    due.set(key, frame + EVERY_FRAMES);
    const player = online.get(key);
    const scan = scanOf(player.name);
    if (!scan) {
      forget(key);
      return;
    }
    try {
      found.set(key, scanFor(player, scan));
    } catch (e) {
      // Mid-portal or an unloaded edge: no target until the next scan.
      found.set(key, null);
      say(`scan failed for ${player.name}: ${e}`);
    }
  };

  // The held Tricorder's line while a scan is on, else undefined. The last target is
  // checked cheaply each frame: mined, it's gone now and looked past within a second;
  // walked away from, it's past the reach and hidden until the next scan.
  const line = (player) => {
    const scan = scanOf(player.name);
    if (!scan) return undefined;
    const key = player.name.toLowerCase();
    let target = found.get(key) ?? null;
    if (target && !inScanBox(player.location, target)) target = null;
    if (target) {
      let still = false;
      try {
        still = scan.types.includes(player.dimension.getBlock(target)?.typeId);
      } catch {
        // Unloaded: as good as gone.
      }
      if (!still) {
        found.set(key, null);
        due.set(key, Math.min(due.get(key) ?? frame, frame + MINED_RESCAN_FRAMES));
        target = null;
      }
    }
    const here = { dim: player.dimension.id, x: player.location.x, z: player.location.z };
    return scanLine(scan, target, here, player.getViewDirection());
  };

  const onEvent = (message) => {
    try {
      const q = parseScan(message);
      if (q.stop) {
        const was = end(q.player);
        say(`scan stopped for ${q.player}${was ? "" : " (none was on)"}`);
      } else {
        start(q);
      }
    } catch (e) {
      say(`scan refused: ${e}`);
    }
  };

  // Whether a scan is on for this player: its target is a find, never an arrival.
  const active = (name) => scanOf(name) !== undefined;

  return { step, line, end, onEvent, active };
};
