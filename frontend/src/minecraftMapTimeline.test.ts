// The timeline's pure parts (plan §M8a): days and ranges in the phone's own time zone, the
// honest status line, where a player was between samples, the scrubber's glyphs and the
// words for a death. Times are built from local dates, so no test depends on the zone or
// the time of day it runs at.

import { describe, expect, it } from "vitest";
import type { MinecraftSession, MinecraftTimelineEvent, MinecraftTrail } from "./api/client";
import {
  DAY_PAD,
  MAX_SPAN,
  SESSION_PAD,
  dayDiff,
  dayRange,
  dayStrip,
  deathCause,
  dimNote,
  entityName,
  eventWords,
  fmtClock,
  fmtDay,
  fmtDur,
  fmtKm,
  fmtSpan,
  isTimeTravel,
  nextSpeed,
  pathUpTo,
  posAt,
  presetRange,
  rosterOf,
  scrubMarks,
  sessionAt,
  sessionGroups,
  sessionRange,
  timeStatus,
} from "./minecraftMap";

/** The words use non-breaking spaces so a time never wraps; compare them as plain text. */
const plain = <T>(v: T): T => JSON.parse(JSON.stringify(v).replace(/\u00a0/g, " "));

const at = (d: number, h: number, m: number) => new Date(2026, 9, d, h, m).getTime() / 1000;
const NOW = at(10, 14, 40); // Sat Oct 10, 2:40 pm — the reviewed fixture's now

function ses(id: string, xuid: string, start: number, end: number | null): MinecraftSession {
  return {
    id,
    xuid,
    gamertag: xuid === "b" ? "BlockyFox" : xuid === "m" ? "Mira_P" : "Steve42",
    start,
    end,
    last_seen: end ?? NOW,
    distance: { overworld: 2_400, nether: 200 },
    dims: ["overworld", "nether"],
    deaths: 1,
    first: { dim: "overworld", x: -140, z: 90 },
  };
}

const SESSIONS = [
  ses("1", "b", at(4, 16, 2), at(4, 17, 31)),
  ses("2", "b", at(9, 16, 10), at(9, 19, 52)),
  ses("3", "m", at(9, 17, 0), at(9, 18, 15)),
  ses("4", "b", at(10, 13, 58), null),
  ses("5", "m", at(10, 14, 33), null),
];

describe("words for time", () => {
  it("reads clocks, days, lengths and spans the way the mock does", () => {
    expect(plain(fmtClock(at(9, 16, 10)))).toBe("4:10 pm");
    expect(plain(fmtClock(at(9, 0, 5)))).toBe("12:05 am");
    expect(plain(fmtClock(at(9, 12, 0)))).toBe("12:00 pm");
    expect(plain(fmtDay(at(10, 9, 0), NOW))).toBe("Today");
    expect(plain(fmtDay(at(9, 23, 59), NOW))).toBe("Yesterday");
    expect(plain(fmtDay(at(8, 1, 0), NOW))).toBe("Thu Oct 8");
    expect(dayDiff(at(4, 23, 0), NOW)).toBe(6);
    expect(plain(fmtDur(42 * 60))).toBe("42 min");
    expect(plain(fmtDur(3 * 3600 + 42 * 60))).toBe("3h 42m");
    expect(plain(fmtSpan(at(9, 16, 10), at(9, 19, 52)))).toBe("4:10–7:52 pm");
    expect(plain(fmtSpan(at(9, 11, 40), at(9, 12, 15)))).toBe("11:40 am–12:15 pm");
    expect(plain(fmtSpan(at(10, 13, 58), null))).toBe("1:58 pm–now");
    expect(plain(fmtKm(2_640))).toBe("2.6 km");
  });

  it("never says Live while replaying or paused anywhere but now", () => {
    const playing = plain(timeStatus(at(9, 16, 48), NOW, true, 600));
    expect(playing).toEqual({
      tone: "warn",
      head: "Not live · yesterday 4:48 pm",
      sub: "Replaying at 600× · positions from the travel log · terrain as of now",
    });
    const paused = plain(timeStatus(at(7, 10, 20), NOW, false, 60));
    expect(paused.head).toBe("Not live · Wed Oct 7 10:20 am");
    expect(paused.sub.startsWith("Paused")).toBe(true);
    for (const s of [playing, paused]) expect(`${s.head} ${s.sub}`).not.toMatch(/\bLive\b/);
    expect(isTimeTravel(true, NOW - 5, NOW)).toBe(true);
    expect(isTimeTravel(true, NOW, NOW)).toBe(false);
    expect(isTimeTravel(false, NOW - 500, NOW)).toBe(false);
  });

  it("cycles the speeds one tap at a time, round to 1×", () => {
    expect([1, 10, 60, 600].map(nextSpeed)).toEqual([10, 60, 600, 1]);
    expect(nextSpeed(7)).toBe(1);
  });
});

describe("days and ranges", () => {
  it("strips the week since the first session, a dot per player who played", () => {
    const days = dayStrip(SESSIONS, NOW, ["b", "m", "s"]);
    expect(days.map((d) => `${d.label} ${d.date}`)).toEqual([
      "Sun 4",
      "Mon 5",
      "Tue 6",
      "Wed 7",
      "Thu 8",
      "Fri 9",
      "Today 10",
    ]);
    const fri = days[5];
    expect(fri?.who).toEqual(["b", "m"]);
    expect(fri?.sessions.map((s) => s.id)).toEqual(["2", "3"]);
    expect(days[1]?.sessions).toEqual([]);
    expect(days[6]?.today).toBe(true);
    // A world first played two days ago starts its strip there.
    expect(dayStrip([ses("x", "b", at(8, 9, 0), at(8, 10, 0))], NOW, ["b"]).length).toBe(3);
    expect(dayStrip([], NOW, [])).toEqual([]);
  });

  it("loads a session with two minutes either side, never past now", () => {
    const fri = SESSIONS[1] as MinecraftSession;
    expect(sessionRange(fri, NOW)).toEqual([fri.start - SESSION_PAD, (fri.end ?? 0) + SESSION_PAD]);
    const on = SESSIONS[3] as MinecraftSession;
    expect(sessionRange(on, NOW)).toEqual([on.start - SESSION_PAD, NOW]);
  });

  it("a tapped day covers its activity, first start to last end", () => {
    const fri = SESSIONS.filter((s) => s.id === "2" || s.id === "3");
    expect(dayRange(fri, NOW)).toEqual([at(9, 16, 10) - DAY_PAD, at(9, 19, 52) + DAY_PAD]);
    expect(dayRange([], NOW)).toBeNull();
  });

  it("presets: this session is whoever's on, else the newest; today, the week, everything", () => {
    expect(presetRange("session", SESSIONS, NOW)).toEqual([at(10, 13, 58) - SESSION_PAD, NOW]);
    const past = SESSIONS.slice(0, 3);
    // Nobody on: the newest session, Mira's.
    expect(presetRange("session", past, NOW)).toEqual(
      sessionRange(past[2] as MinecraftSession, NOW),
    );
    expect(presetRange("session", [], NOW)).toEqual([NOW - 3600, NOW]);
    expect(presetRange("today", SESSIONS, NOW)).toEqual([at(10, 0, 0), NOW]);
    expect(presetRange("week", SESSIONS, NOW)).toEqual([NOW - 7 * 86_400, NOW]);
    expect(presetRange("all", SESSIONS, NOW)).toEqual([at(4, 16, 2) - SESSION_PAD, NOW]);
    const ancient = [ses("old", "b", NOW - 400 * 86_400, NOW - 399 * 86_400)];
    expect(presetRange("all", ancient, NOW)[0]).toBe(NOW - MAX_SPAN + 60);
  });

  it("groups the session list by day, newest first, one player's or everyone's", () => {
    const all = sessionGroups(SESSIONS, null, NOW);
    expect(plain(all.map((g) => g.label))).toEqual([
      "Today · Sat Oct 10",
      "Yesterday · Fri Oct 9",
      "Sun Oct 4",
    ]);
    expect(all[0]?.list.map((s) => s.id)).toEqual(["5", "4"]);
    expect(sessionGroups(SESSIONS, "m", NOW).flatMap((g) => g.list.map((s) => s.id))).toEqual([
      "5",
      "3",
    ]);
    expect(sessionAt(SESSIONS, "b", at(9, 17, 0), NOW)?.id).toBe("2");
    expect(sessionAt(SESSIONS, "b", at(9, 20, 0), NOW)).toBeNull();
    expect(sessionAt(SESSIONS, "b", NOW, NOW)?.id).toBe("4");
  });

  it("gives each player one colour everywhere, by gamertag", () => {
    const r = rosterOf([
      { xuid: "s", gamertag: "Steve42" },
      { xuid: "b", gamertag: "BlockyFox" },
      { xuid: "s", gamertag: "Steve42" },
      { xuid: "p", gamertag: "Pebble_J" },
      { xuid: "m", gamertag: "Mira_P" },
    ]);
    expect(r.map((x) => `${x.gamertag}:${x.token}`)).toEqual([
      "BlockyFox:--steel",
      "Mira_P:--green",
      "Pebble_J:--amber",
      "Steve42:--violet",
    ]);
  });
});

describe("where a player was", () => {
  const T = at(9, 16, 10);
  const trail: MinecraftTrail = {
    xuid: "b",
    gamertag: "BlockyFox",
    samples: 7,
    downsampled: false,
    segments: [
      {
        session: "2",
        dim: "overworld",
        points: [
          [T, 0, 70, 0],
          [T + 10, 40, 70, 0],
          [T + 100, 50, 70, 0], // stood still, then one sample's worth of walking
        ],
      },
      {
        session: "2",
        dim: "nether",
        points: [
          [T + 120, 5, 64, 5],
          [T + 130, 15, 64, 5],
        ],
      },
      {
        session: "9",
        dim: "overworld",
        points: [
          [T + 5_000, 1, 70, 1],
          [T + 5_010, 2, 70, 2],
        ],
      },
    ],
  };

  it("interpolates inside a segment between samples", () => {
    expect(posAt(trail, T + 5)).toMatchObject({ x: 20, z: 0, dim: "overworld", session: "2" });
    expect(posAt(trail, T + 125)).toMatchObject({ x: 10, dim: "nether" });
  });

  it("reads a long gap as standing still, then walking — unless the trail was cut down", () => {
    expect(posAt(trail, T + 50)?.x).toBe(40);
    expect(posAt(trail, T + 95)?.x).toBeCloseTo(45);
    const thin = { ...trail, downsampled: true };
    expect(posAt(thin, T + 55)?.x).toBeCloseTo(45);
  });

  it("holds at a segment's end until the same session goes on, and is nowhere between", () => {
    expect(posAt(trail, T + 110)).toMatchObject({ x: 50, dim: "overworld" }); // at the portal
    expect(posAt(trail, T + 1_000)).toBeNull(); // logged off
    expect(posAt(trail, T - 10)).toBeNull(); // not on yet
    expect(posAt(trail, T + 9_000)).toBeNull();
    expect(posAt(trail, T + 9_000, (id) => id === "9")).toMatchObject({ x: 2, z: 2 }); // on now
  });

  it("draws a path up to the moment, ending where they are", () => {
    const seg = trail.segments[0];
    if (!seg) throw new Error("no segment");
    expect(pathUpTo(seg, T + 5)).toEqual([
      [0, 0],
      [20, 0],
    ]);
    expect(pathUpTo(seg, T + 200)).toEqual([
      [0, 0],
      [40, 0],
      [50, 0],
    ]);
    expect(pathUpTo(seg, T + 200, T + 8)).toEqual([
      [40, 0],
      [50, 0],
    ]);
    expect(pathUpTo(seg, T - 1)).toEqual([]);
  });
});

describe("events", () => {
  const ev = (
    kind: MinecraftTimelineEvent["kind"],
    t: number,
    extra = {},
  ): MinecraftTimelineEvent => ({
    kind,
    xuid: "b",
    gamertag: "BlockyFox",
    at: t,
    dim: "overworld",
    x: 1,
    y: 64,
    z: 2,
    ...extra,
  });

  it("merges glyphs closer than the tap floor, a death leading, then a portal trip", () => {
    const from = 0;
    const to = 1_000;
    const marks = scrubMarks(
      [
        ev("join", 0),
        ev("dimension", 30, { dim: "nether" }),
        ev("death", 60, { cause: "lava" }),
        ev("respawn", 70),
        ev("leave", 900),
        ev("death", 2_000),
      ],
      from,
      to,
      300,
    );
    expect(marks.map((m) => [m.lead.kind, m.events.length])).toEqual([
      ["death", 3],
      ["leave", 1],
    ]);
    expect(marks[1]?.left).toBe(270);
    const wide = scrubMarks([ev("join", 0), ev("dimension", 30)], from, to, 3_000);
    expect(wide.map((m) => m.lead.kind)).toEqual(["join", "dimension"]);
  });

  it("says a death in the game's own terms", () => {
    expect(deathCause("lava")).toBe("burned in lava");
    expect(deathCause("fall")).toBe("fell from a high place");
    expect(deathCause("drowning")).toBe("drowned");
    expect(deathCause("entityAttack", "minecraft:zombie")).toBe("killed by a Zombie");
    expect(deathCause("entityExplosion", "minecraft:creeper")).toBe("blown up by a Creeper");
    expect(deathCause("projectile", "minecraft:skeleton")).toBe("shot by a Skeleton");
    expect(deathCause("entityAttack", "minecraft:enderman")).toBe("killed by an Enderman");
    expect(deathCause("magic")).toBe("killed by magic");
    expect(deathCause("void")).toBe("fell out of the world");
    expect(deathCause("somethingNew", null)).toBe("died");
    expect(deathCause(null, "minecraft:wither_skeleton")).toBe("killed by a Wither Skeleton");
    expect(entityName("minecraft:zombie_villager")).toBe("Zombie Villager");
  });

  it("names every event and the dimension notes", () => {
    expect(eventWords(ev("death", 0, { cause: "fall" }))).toBe("BlockyFox fell from a high place");
    expect(eventWords(ev("join", 0))).toBe("BlockyFox logged in");
    expect(eventWords(ev("leave", 0))).toBe("BlockyFox logged off");
    expect(eventWords(ev("respawn", 0))).toBe("BlockyFox respawned");
    expect(eventWords(ev("dimension", 0, { dim: "the_end" }))).toBe("BlockyFox went to the End");
    expect(dimNote("BlockyFox", "nether", false)).toBe("BlockyFox went to the Nether");
    expect(dimNote("BlockyFox", "overworld", true)).toBe(
      "BlockyFox died and respawned in the Overworld",
    );
  });
});
