// The Tricorder's live scan (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M25): "Dave, set my
// Tricorder to scan for diamonds". Dave's mc_tricorder_scan sends `scriptevent jb:scan
// {json}`; the scan is kept per gamertag as a world property, like a waypoint, so it
// lasts across sessions and can be set while the player is offline. Every 10 s the
// Tricorder finds the nearest such block within 16 blocks and the held arrow points at
// it. The waypoint under it is left alone, so stopping the scan points back there. Each
// change (and a heartbeat) goes out as a `scan` line, and every kept scan as a `scans`
// line after each load, for the map and Dave. The pure rules are in rules.js. Stable
// @minecraft/server only.
import { world, BlockTypes, BlockVolume } from "@minecraft/server";
import { SCAN_EVERY, SCAN_HEARTBEAT, inScanBox, nearestBlock, parseScan, pickScan, scanBox,
  scanEvent, scanLabel, scanLine, scansSnapshot, scanTwins } from "./rules.js";

const SCAN_PREFIX = "jb:scan:";
const scanKey = (name) => `${SCAN_PREFIX}${name.toLowerCase()}`;
// main.js's arrow loop runs every 5 ticks and calls step() once a frame.
const FRAME_TICKS = 5;
const EVERY_FRAMES = SCAN_EVERY / FRAME_TICKS;
// A mined target is looked past this soon, not at the next 10 s scan; no sooner, so a
// player digging through a vein of what they scan for doesn't scan every frame.
const MINED_RESCAN_FRAMES = 4;
// How often every scanner's last target is checked for being mined, held or not: once a
// second, one block read each.
const CHECK_FRAMES = 4;
const HEARTBEAT_FRAMES = SCAN_HEARTBEAT * EVERY_FRAMES;
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

const parseKept = (raw) => {
  if (typeof raw !== "string") return undefined;
  try {
    const s = JSON.parse(raw);
    return Array.isArray(s.types) && s.types.length ? s : undefined;
  } catch {
    return undefined;
  }
};
const scanOf = (name) => parseKept(world.getDynamicProperty(scanKey(name)));

// Every kept scan, for the load snapshot. The snapshot replaces every scan the map holds,
// so a failed read throws rather than passing for "nobody scanning". A scan kept before
// the player's name was stored with it is named by its key, lowercased.
const allScans = () => {
  const out = [];
  for (const id of world.getDynamicPropertyIds()) {
    if (!id.startsWith(SCAN_PREFIX)) continue;
    const s = parseKept(world.getDynamicProperty(id));
    if (s) out.push({ ...s, player: s.player ?? id.slice(SCAN_PREFIX.length) });
  }
  return out;
};

export const initScan = ({ say, emit }) => {
  // In memory only: when each scanning player is next due (in frames), the block the
  // last scan found (null for none), and what was last sent for them and when. A rejoin
  // or a restart scans afresh at once, and sends what it finds.
  const due = new Map();
  const found = new Map();
  const sent = new Map();
  let frame = 0;
  let timed = false;
  let reported = false;

  const forget = (key) => {
    due.delete(key);
    found.delete(key);
    sent.delete(key);
  };

  // Ends a player's scan; true when one was on. Their waypoint was never touched, so the
  // arrow points back at it (or spawn).
  const end = (name) => {
    const was = scanOf(name) !== undefined;
    world.setDynamicProperty(scanKey(name), undefined);
    forget(name.toLowerCase());
    if (was) emit(scanEvent("stop", name));
    return was;
  };

  // A scan's result goes out when it differs from the last one sent, or the last is a
  // heartbeat old: a steady target costs one line a minute.
  const report = (player, key, scan, target) => {
    const dim = player.dimension.id;
    const sig = target ? `${dim} ${target.x} ${target.y} ${target.z}` : "none";
    const last = sent.get(key);
    if (last && last.sig === sig && frame - last.at < HEARTBEAT_FRAMES) return;
    sent.set(key, { sig, at: frame });
    emit(scanEvent(target ? "found" : "none", player.name, scan, target, dim));
  };

  // Whether the block a scan found is still there: mined, it's gone now and looked past
  // within a second, held or not.
  const stillThere = (player, key, scan, target) => {
    let still = false;
    try {
      still = scan.types.includes(player.dimension.getBlock(target)?.typeId);
    } catch {
      // Unloaded: as good as gone.
    }
    if (!still) {
      found.set(key, null);
      due.set(key, Math.min(due.get(key) ?? frame, frame + MINED_RESCAN_FRAMES));
    }
    return still;
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
    const kept = { block, types, label, player: q.player };
    world.setDynamicProperty(scanKey(q.player), JSON.stringify(kept));
    const key = q.player.toLowerCase();
    found.delete(key);
    sent.delete(key);
    due.set(key, frame);
    emit(scanEvent("set", q.player, kept));
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

  // Once per load, retried until it goes out: the properties can't be read any sooner.
  const snapshot = () => {
    if (reported) return;
    try {
      emit(scansSnapshot(allScans()));
      reported = true;
    } catch (e) {
      say(`scan report failed, trying again: ${e}`);
    }
  };

  // One scan a frame at most, for whichever scanning player is most overdue.
  const step = () => {
    frame += 1;
    snapshot();
    const online = new Map(world.getAllPlayers().map((p) => [p.name.toLowerCase(), p]));
    for (const key of [...due.keys()]) if (!online.has(key)) forget(key);
    for (const [key, p] of online) if (!due.has(key) && scanOf(p.name)) due.set(key, frame);
    if (frame % CHECK_FRAMES === 0) {
      for (const [key, target] of found) {
        const p = online.get(key);
        const scan = target && p ? scanOf(p.name) : undefined;
        if (scan) stillThere(p, key, scan, target);
      }
    }
    const key = pickScan(due, frame);
    if (key === undefined) return;
    due.set(key, frame + EVERY_FRAMES);
    const player = online.get(key);
    const scan = scanOf(player.name);
    if (!scan) {
      forget(key);
      return;
    }
    let target;
    try {
      target = scanFor(player, scan);
    } catch (e) {
      // Mid-portal or an unloaded edge: no target until the next scan, and nothing sent,
      // so the map keeps the last one rather than flickering to "none".
      found.set(key, null);
      say(`scan failed for ${player.name}: ${e}`);
      return;
    }
    found.set(key, target);
    report(player, key, scan, target);
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
    if (target && !stillThere(player, key, scan, target)) target = null;
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
