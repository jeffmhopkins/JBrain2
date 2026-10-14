// Beds and respawn points on the Minecraft map (binding mock
// docs/mocks/minecraft-map-layers/beds-b-with-the-player.html, variant B as decided
// 2026-10-13; plan §M8). The pure half: each player's current point, where a death's player
// woke (matched to the point they had then), and the words for the player, death and bed
// cards and the Layers list. The canvas half is in screens/minecraftMapDraw.ts.
//
// The death card only ever says where they woke, never why: the owner ruled out inferring
// "their bed was missing or blocked" from a waking at the world spawn.

import type {
  MinecraftDim,
  MinecraftMapMarkers,
  MinecraftSpawnChange,
  MinecraftSpawnPoint,
  MinecraftTimelineEvent,
} from "./api/client";
import { bearingTo, num } from "./minecraftMapMarkers";

/** A waking this close to the point (blocks, each way) is at it: the game wakes a player
 *  beside their bed or anchor, not on it. A home-test box check. */
export const AT_POINT = 4;
/** And this close to the world spawn: its spawn radius (the box's default is 10) plus the
 *  game's search for a safe block. A home-test box check. */
export const AT_WORLD_SPAWN = 16;

export type WokeAt = "bed" | "anchor" | "point" | "world_spawn" | "elsewhere";

export interface Wake {
  woke: WokeAt;
  /** Where they woke: the respawn the travel log recorded. */
  respawn: MinecraftTimelineEvent;
  /** The point they had when they woke, when the pack had sent one. */
  spawn: MinecraftSpawnChange | null;
}

const IN_DIM: Record<MinecraftDim, string> = {
  overworld: "in the Overworld",
  nether: "in the Nether",
  the_end: "in the End",
};

const sameName = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/** A player's current respawn point, by key, else by the name they last went by. */
export function spawnOf(
  marks: MinecraftMapMarkers | null | undefined,
  xuid: string,
  gamertag?: string,
): MinecraftSpawnPoint | null {
  const all = marks?.spawns ?? [];
  return (
    all.find((s) => s.xuid === xuid) ??
    (gamertag ? all.find((s) => sameName(s.gamertag, gamertag)) : undefined) ??
    null
  );
}

/** Whether a point has a place to draw (anything but `none`). */
export const placed = (
  s: { kind: string; dim: MinecraftDim | null; x: number | null; z: number | null } | null,
): s is { kind: "bed" | "anchor" | "other"; dim: MinecraftDim; x: number; y: number; z: number } =>
  !!s && s.kind !== "none" && s.dim !== null && s.x !== null && s.z !== null;

/** The respawn that followed a death: the player's next one, before any later death. */
export function respawnAfter(
  death: MinecraftTimelineEvent,
  events: MinecraftTimelineEvent[],
): MinecraftTimelineEvent | null {
  let next: MinecraftTimelineEvent | null = null;
  for (const e of events) {
    if (e.xuid !== death.xuid || e.at <= death.at) continue;
    if (e.kind !== "respawn" && e.kind !== "death") continue;
    if (!next || e.at < next.at) next = e;
  }
  return next?.kind === "respawn" ? next : null;
}

/** The point a player had at a moment: their last change at or before it. */
export function spawnThen(
  log: MinecraftSpawnChange[],
  xuid: string,
  at: number,
): MinecraftSpawnChange | null {
  let then: MinecraftSpawnChange | null = null;
  for (const c of log) if (c.xuid === xuid && c.at <= at && (!then || c.at >= then.at)) then = c;
  return then;
}

const near = (
  a: { x: number | null; y?: number | null; z: number | null },
  b: { x: number; z: number; y?: number | null },
  reach: number,
) =>
  a.x !== null &&
  a.z !== null &&
  Math.abs(a.x - b.x) <= reach &&
  Math.abs(a.z - b.z) <= reach &&
  (a.y == null || b.y == null || Math.abs(a.y - b.y) <= reach);

/** Where a death's player woke, matched to the point they had then and the world spawn.
 *  Null when the travel log has no respawn after it (yet). */
export function wakeOf(
  death: MinecraftTimelineEvent,
  events: MinecraftTimelineEvent[],
  log: MinecraftSpawnChange[],
  worldSpawn: { x: number; z: number } | null,
): Wake | null {
  const respawn = respawnAfter(death, events);
  if (!respawn || respawn.dim === null || respawn.x === null || respawn.z === null) return null;
  const spawn = spawnThen(log, death.xuid, respawn.at);
  let woke: WokeAt = "elsewhere";
  if (placed(spawn) && spawn.dim === respawn.dim && near(respawn, spawn, AT_POINT)) {
    woke = spawn.kind === "bed" ? "bed" : spawn.kind === "anchor" ? "anchor" : "point";
  } else if (
    worldSpawn &&
    respawn.dim === "overworld" &&
    near({ x: respawn.x, z: respawn.z }, worldSpawn, AT_WORLD_SPAWN)
  ) {
    woke = "world_spawn";
  }
  return { woke, respawn, spawn };
}

/** Where the death's curve ends, in the dimension shown: the point they woke at, the star,
 *  or the spot itself; null when they woke in another dimension (no line, words only). */
export function curveEnd(
  w: Wake,
  dim: MinecraftDim,
  worldSpawn: { x: number; z: number } | null,
): { x: number; z: number } | null {
  if (w.respawn.dim !== dim || w.respawn.x === null || w.respawn.z === null) return null;
  if ((w.woke === "bed" || w.woke === "point" || w.woke === "anchor") && placed(w.spawn)) {
    return { x: w.spawn.x, z: w.spawn.z };
  }
  if (w.woke === "world_spawn" && worldSpawn) return worldSpawn;
  return { x: Math.floor(w.respawn.x), z: Math.floor(w.respawn.z) };
}

// No-break spaces inside a value (DESIGN, M26): X −136 never breaks from its number.
const xz = (p: { x: number; z: number }) => `X ${num(p.x)} · Z ${num(p.z)}`;

/** The way and distance from one spot to a point, or the point's dimension when apart. */
function across(
  from: { dim: MinecraftDim | null; x: number | null; z: number | null } | null,
  to: { dim: MinecraftDim; x: number; z: number },
): string {
  if (!from || from.dim !== to.dim || from.x === null || from.z === null) {
    return `${IN_DIM[to.dim]} · ${xz(to)}`;
  }
  const { way, blocks } = bearingTo(from.x, from.z, to.x + 0.5, to.z + 0.5);
  return `${blocks} blocks ${way} · ${xz(to)}`;
}

const NOUN = { bed: "Bed", anchor: "Respawn anchor", other: "Spawn point" } as const;
const NAME = { bed: "bed", anchor: "respawn anchor", other: "spawn point" } as const;

export interface Line {
  glyph: "bed" | "anchor" | "flag" | "spot";
  /** The bold lead, then the rest, as the mock's `ln()` lines. */
  head: string;
  rest: string;
}

/** The player card's line about their respawn point (null when the pack never sent one,
 *  so "no bed set" is said only when it is known). */
export function playerBedLine(
  s: MinecraftSpawnPoint | null,
  from: { dim: MinecraftDim; x: number; z: number },
): Line | null {
  if (!s) return null;
  if (!placed(s)) {
    return { glyph: "bed", head: "No bed set", rest: "respawns at the world spawn" };
  }
  const rest =
    s.kind === "other"
      ? `no bed seen there · ${across(from, s)}`
      : s.kind === "anchor" && from.dim !== s.dim
        ? `${IN_DIM[s.dim]} · ${xz(s)}`
        : across(from, s);
  return { glyph: s.kind === "anchor" ? "anchor" : "bed", head: NOUN[s.kind], rest };
}

/** The button that shows it: "Show the bed", "Show the anchor" or "Show the spawn point". */
export function showLabel(s: MinecraftSpawnPoint): string {
  return s.kind === "anchor"
    ? "Show the anchor"
    : `Show the ${NAME[s.kind === "bed" ? "bed" : "other"]}`;
}

/** The death card's line: only where they woke. */
export function wakeLine(w: Wake, death: MinecraftTimelineEvent): Line {
  const r = w.respawn;
  const there = { dim: r.dim ?? "overworld", x: Math.floor(r.x ?? 0), z: Math.floor(r.z ?? 0) };
  if ((w.woke === "bed" || w.woke === "anchor" || w.woke === "point") && placed(w.spawn)) {
    const noun = NAME[w.spawn.kind];
    return {
      glyph: w.woke === "anchor" ? "anchor" : "bed",
      head: `Respawned at their ${noun}`,
      rest: across(death, w.spawn),
    };
  }
  if (w.woke === "world_spawn")
    return { glyph: "flag", head: "Respawned at the world spawn", rest: "" };
  return { glyph: "spot", head: "Respawned", rest: across(death, there) };
}

/** A bed card's title: "BlockyFox's bed". */
export const bedTitle = (s: MinecraftSpawnPoint) =>
  `${s.gamertag}'s ${placed(s) ? NAME[s.kind] : "bed"}`;

/** A Layers row in "In this world": one per player the pack has told about. */
export interface BedRow {
  key: string;
  s: MinecraftSpawnPoint;
  title: string;
  sub: string;
}

export function bedRows(
  marks: MinecraftMapMarkers | null | undefined,
  worldSpawn: { x: number; z: number } | null,
  date: (sec: number) => string,
): BedRow[] {
  return (marks?.spawns ?? []).map((s) => {
    if (!placed(s)) {
      return {
        key: `bed:${s.xuid}`,
        s,
        title: `${s.gamertag} · no bed set`,
        sub: `respawns at the world spawn${worldSpawn ? ` · ${xz(worldSpawn)}` : ""}`,
      };
    }
    const where = s.dim === "overworld" ? xz(s) : `${IN_DIM[s.dim]} · ${xz(s)}`;
    const seen = s.kind === "other" ? ` · no bed seen there since ${date(s.at)}` : "";
    return {
      key: `bed:${s.xuid}`,
      s,
      title: bedTitle(s),
      sub: `${where} · set ${date(s.set_at)}${seen}`,
    };
  });
}

/** The deaths a bed card counts: each of its player's in the log whose waking was at this
 *  very point since it was set. */
export function wakesHere(
  s: MinecraftSpawnPoint,
  events: MinecraftTimelineEvent[],
  log: MinecraftSpawnChange[],
  worldSpawn: { x: number; z: number } | null,
): { death: MinecraftTimelineEvent; wake: Wake }[] {
  if (!placed(s)) return [];
  const out: { death: MinecraftTimelineEvent; wake: Wake }[] = [];
  for (const e of events) {
    if (e.kind !== "death" || e.xuid !== s.xuid || e.at < s.set_at) continue;
    const w = wakeOf(e, events, log, worldSpawn);
    if (!w || !placed(w.spawn) || w.woke === "elsewhere" || w.woke === "world_spawn") continue;
    if (w.spawn.dim === s.dim && w.spawn.x === s.x && w.spawn.y === s.y && w.spawn.z === s.z) {
      out.push({ death: e, wake: w });
    }
  }
  return out.sort((a, b) => b.death.at - a.death.at);
}
