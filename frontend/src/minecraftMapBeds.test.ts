import { describe, expect, it } from "vitest";
import type {
  MinecraftSpawnChange,
  MinecraftSpawnPoint,
  MinecraftTimelineEvent,
} from "./api/client";
import {
  AT_POINT,
  AT_WORLD_SPAWN,
  bedRows,
  bedTitle,
  curveEnd,
  playerBedLine,
  respawnAfter,
  showLabel,
  spawnOf,
  spawnThen,
  wakeLine,
  wakeOf,
  wakesHere,
} from "./minecraftMapBeds";
import { mcMapMarkers } from "./minecraftMapFixtures";

const FOX = "111";

function must<T>(v: T | null | undefined): T {
  if (v == null) throw new Error("expected a value");
  return v;
}
const SPAWN = { x: 32, z: -8 };

const ev = (
  kind: MinecraftTimelineEvent["kind"],
  at: number,
  x: number,
  z: number,
  dim: MinecraftTimelineEvent["dim"] = "overworld",
  xuid = FOX,
): MinecraftTimelineEvent => ({ kind, xuid, gamertag: "BlockyFox", at, dim, x, y: 70, z });

const change = (
  at: number,
  kind: MinecraftSpawnChange["kind"],
  where: { dim: "overworld" | "nether"; x: number; z: number } | null = null,
): MinecraftSpawnChange => ({
  xuid: FOX,
  at,
  kind,
  dim: where?.dim ?? null,
  x: where?.x ?? null,
  y: where ? 70 : null,
  z: where?.z ?? null,
});

const point = (kind: MinecraftSpawnPoint["kind"], over: Partial<MinecraftSpawnPoint> = {}) =>
  ({
    ...change(100, kind, kind === "none" ? null : { dim: "overworld", x: 70, z: 40 }),
    gamertag: "BlockyFox",
    set_at: 100,
    ...over,
  }) as MinecraftSpawnPoint;

const BED = { dim: "overworld" as const, x: 70, z: 40 };

describe("where a death's player woke", () => {
  it("takes the next respawn, never one after a later death", () => {
    const death = ev("death", 1000, -60, 140);
    const events = [
      ev("respawn", 900, 0, 0),
      death,
      ev("respawn", 1010, 71, 41),
      ev("death", 2000, 0, 0),
      ev("respawn", 2010, 32, -8),
      ev("respawn", 1005, 5, 5, "overworld", "other player"),
    ];
    expect(respawnAfter(death, events)?.at).toBe(1010);
    expect(respawnAfter(death, [death, ev("death", 1500, 0, 0), ev("respawn", 1510, 1, 1)])).toBe(
      null,
    );
    expect(respawnAfter(death, [death])).toBe(null);
  });

  it("matches the point they had then, beside it, not the one they have now", () => {
    const log = [
      change(10, "bed", { ...BED }),
      change(5000, "anchor", { dim: "nether", x: 1, z: 1 }),
    ];
    expect(spawnThen(log, FOX, 1010)?.kind).toBe("bed");
    expect(spawnThen(log, FOX, 9000)?.kind).toBe("anchor");
    expect(spawnThen(log, FOX, 5)).toBe(null);
    const death = ev("death", 1000, -60, 140);
    const beside = ev("respawn", 1010, 70 + AT_POINT, 40 - 2);
    const w = wakeOf(death, [death, beside], log, SPAWN);
    expect(w?.woke).toBe("bed");
    expect(curveEnd(must(w), "overworld", SPAWN)).toEqual({ x: 70, z: 40 });
    const line = wakeLine(must(w), death);
    expect(line.head).toBe("Respawned at their bed");
    expect(line.rest).toBe("164 blocks north-east · X 70 · Z 40");
  });

  it("at the world spawn says only that, never why", () => {
    const log = [change(10, "bed", { ...BED })];
    const death = ev("death", 1000, -20, -200);
    const woke = ev("respawn", 1010, SPAWN.x + AT_WORLD_SPAWN, SPAWN.z - 3);
    const w = wakeOf(death, [death, woke], log, SPAWN);
    expect(w?.woke).toBe("world_spawn");
    const line = wakeLine(must(w), death);
    expect(line).toEqual({ glyph: "flag", head: "Respawned at the world spawn", rest: "" });
    expect(JSON.stringify(line)).not.toMatch(/missing|blocked|broken|cleared/);
    expect(curveEnd(must(w), "overworld", SPAWN)).toEqual(SPAWN);
    // Not known where the world spawn is: only where they woke.
    const blind = wakeOf(death, [death, woke], log, null);
    expect(blind?.woke).toBe("elsewhere");
    expect(wakeLine(must(blind), death).head).toBe("Respawned");
  });

  it("an anchor in the Nether is said, with no curve across dimensions", () => {
    const nether = { dim: "nether" as const, x: -20, z: 40 };
    const log = [change(10, "anchor", nether)];
    const death = ev("death", 1000, -60, 140);
    const woke = ev("respawn", 1010, -21, 41, "nether");
    const w = wakeOf(death, [death, woke], log, SPAWN);
    expect(w?.woke).toBe("anchor");
    expect(curveEnd(must(w), "overworld", SPAWN)).toBe(null);
    expect(curveEnd(must(w), "nether", SPAWN)).toEqual({ x: -20, z: 40 });
    expect(wakeLine(must(w), death)).toEqual({
      glyph: "anchor",
      head: "Respawned at their respawn anchor",
      rest: "in the Nether · X −20 · Z 40",
    });
  });

  it("anywhere else is the spot itself, and no respawn yet is nothing", () => {
    const death = ev("death", 1000, 0, 0);
    const woke = ev("respawn", 1010, 500, 500);
    const w = wakeOf(death, [death, woke], [], SPAWN);
    expect(w?.woke).toBe("elsewhere");
    expect(curveEnd(must(w), "overworld", SPAWN)).toEqual({ x: 500, z: 500 });
    expect(wakeLine(must(w), death).rest).toBe("708 blocks south-east · X 500 · Z 500");
    expect(wakeOf(death, [death], [], SPAWN)).toBe(null);
  });
});

describe("a player's respawn point in words", () => {
  it("the player card's line: a bed, an anchor, not seen there, none, never sent", () => {
    const here = { dim: "overworld" as const, x: 44, z: 22 };
    expect(playerBedLine(point("bed"), here)).toEqual({
      glyph: "bed",
      head: "Bed",
      rest: "32 blocks south-east · X 70 · Z 40",
    });
    const anchor = point("anchor", { dim: "nether", x: -20, z: 40 });
    expect(playerBedLine(anchor, here)).toEqual({
      glyph: "anchor",
      head: "Respawn anchor",
      rest: "in the Nether · X −20 · Z 40",
    });
    expect(playerBedLine(point("other"), here)?.rest).toBe(
      "no bed seen there · 32 blocks south-east · X 70 · Z 40",
    );
    expect(playerBedLine(point("none"), here)).toEqual({
      glyph: "bed",
      head: "No bed set",
      rest: "respawns at the world spawn",
    });
    expect(playerBedLine(null, here)).toBe(null);
    expect(showLabel(point("bed"))).toBe("Show the bed");
    expect(showLabel(anchor)).toBe("Show the anchor");
    expect(showLabel(point("other"))).toBe("Show the spawn point");
    expect(bedTitle(anchor)).toBe("BlockyFox's respawn anchor");
  });

  it("finds a player's point by key, else by the name they go by", () => {
    const marks = mcMapMarkers("slot2");
    expect(spawnOf(marks, "2535411")?.kind).toBe("bed");
    expect(spawnOf(marks, "nobody", "mira_p")?.kind).toBe("anchor");
    expect(spawnOf(marks, "nobody")).toBe(null);
    expect(spawnOf(null, "2535411")).toBe(null);
  });

  it("the Layers list: one row per player the pack has told of", () => {
    const rows = bedRows(mcMapMarkers("slot2"), SPAWN, () => "Oct 11");
    expect(rows.map((r) => r.title)).toEqual([
      "BlockyFox's bed",
      "Mira_P's respawn anchor",
      "Steve42 · no bed set",
      "Pebble_J's spawn point",
    ]);
    expect(rows[0]?.sub).toBe("X −136 · Z 94 · set Oct 11");
    expect(rows[1]?.sub).toBe("in the Nether · X −12 · Z 36 · set Oct 11");
    expect(rows[2]?.sub).toBe("respawns at the world spawn · X 32 · Z −8");
    expect(rows[3]?.sub).toMatch(/no bed seen there since Oct 11$/);
    expect(bedRows(mcMapMarkers("slot1"), SPAWN, String)).toEqual([]);
  });

  it("a bed counts the deaths its player woke at it after, newest first", () => {
    const bed = point("bed", { set_at: 500 });
    const log = [change(10, "bed", { ...BED })];
    const events = [
      ev("death", 400, 0, 0),
      ev("respawn", 410, 70, 41), // before it was set
      ev("death", 1000, 0, 0),
      ev("respawn", 1010, 70, 41),
      ev("death", 2000, 0, 0),
      ev("respawn", 2010, SPAWN.x, SPAWN.z), // the world spawn
      ev("death", 3000, 0, 0),
      ev("respawn", 3010, 69, 39),
    ];
    expect(wakesHere(bed, events, log, SPAWN).map((w) => w.death.at)).toEqual([3000, 1000]);
    expect(wakesHere(point("none"), events, log, SPAWN)).toEqual([]);
  });
});
