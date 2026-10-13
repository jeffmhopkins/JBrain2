import { describe, expect, it } from "vitest";
import type {
  MinecraftGoalProgress,
  MinecraftMapEntities,
  MinecraftMapPlayer,
  MinecraftMob,
  MinecraftPlace,
  MinecraftStructure,
} from "./api/client";
import { clusterMarkers, rosterOf } from "./minecraftMap";
import { mcMapEntities, mcMapMarkers, mcMapPlaces, mcMapVillager } from "./minecraftMapFixtures";
import {
  ALL_JOBS,
  entitiesAge,
  goalWords,
  jobOf,
  mobFrom,
  mobWords,
  petChips,
  petNote,
  petsShown,
  placeGroups,
  placeLines,
  placeSub,
  placesLine,
  rankedPets,
  rankedVillagers,
  tokenFor,
  tradeTiers,
  tradesNote,
  villageLabel,
  villageMarks,
  villagerChips,
  villagerTitle,
  villagersLine,
} from "./minecraftMapLayers";
import { cornerBox } from "./screens/minecraftMapDraw";

const places = mcMapPlaces("slot2");
const place = (name: string) => places.places.find((p) => p.name === name) as MinecraftPlace;
const gates = mcMapMarkers("slot2").gates;
const roster = rosterOf([
  { xuid: "2535411", gamertag: "BlockyFox" },
  { xuid: "2535412", gamertag: "Mira_P" },
  { xuid: "2535413", gamertag: "Steve42" },
]);
const fox: MinecraftMapPlayer = {
  xuid: "2535411",
  gamertag: "BlockyFox",
  dim: "overworld",
  x: 42,
  y: 70,
  z: 12,
  at: 0,
  online: true,
};
const NB = " ";

describe("saved places (places A)", () => {
  it("each pin takes its player's colour; someone never seen takes the neutral ring", () => {
    expect(tokenFor(roster, "2535411", "BlockyFox")).toBe("--steel");
    // A renamed or name-keyed player is found by gamertag.
    expect(tokenFor(roster, "name:Mira_P", "mira_p")).toBe("--green");
    expect(tokenFor(roster, "9", "Stranger")).toBeNull();
  });

  it("the row says how many, whose, and that each is named", () => {
    expect(placesLine(places)).toBe(
      `7 in this world · BlockyFox${NB}3 · Mira_P${NB}2 · Steve42${NB}2 · each labelled`,
    );
    expect(placesLine(null)).toMatch(/^none saved in this world yet/);
  });

  it("a card says saved or moved, and protected by whose beacon", () => {
    expect(placeSub(place("Home"))).toMatch(
      /^BlockyFox's saved place · saved \w+\s\d+ · in the Overworld$/,
    );
    expect(placeSub(place("Nether base"))).toMatch(/· moved .* · in the Nether$/);
    const home = placeLines(place("Home"), gates, fox);
    expect(home[0]).toMatchObject({ tone: "ok", lead: "Protected" });
    expect(home[0]?.text).toBe(` · by their No-Spawn Beacon at X${NB}90 · Z${NB}−10`);
    expect(home[1]?.text).toBe(`BlockyFox is 29${NB}blocks west of it · online`);
    const bakery = placeLines({ ...place("Bakery"), gamertag: "Steve42" }, gates, null);
    expect(bakery[0]?.text).toContain("by Mira_P's No-Spawn Beacon");
    const unknown = { ...place("Bakery"), covered: [{ x: 1, y: 1, z: 1, placed_by: "" }] };
    expect(placeLines(unknown, gates, null)[0]?.text).toContain("by a No-Spawn Beacon");
    expect(placeLines(place("Melon farm"), gates, null)).toEqual([
      { tone: "", icon: "shield", text: "Not protected · no No-Spawn Beacon covers it" },
    ]);
  });

  it("a gate leads here by its block; one aimed where the place was says so (X1)", () => {
    const farm = placeLines(place("Tree farm"), gates, { ...fox, online: false, dim: "nether" });
    expect(farm.map((l) => l.text)).toEqual([
      "Not protected · no No-Spawn Beacon covers it",
      "BlockyFox is in the Nether · offline, where they logged off",
      `Wormhole gate${NB}1 leads here, from X${NB}30 · Z${NB}14`,
    ]);
    const moved = { ...place("Tree farm"), x: -100, gates: [], gates_moved: [1] };
    expect(placeLines(moved, gates, null)[1]).toEqual({
      tone: "warn",
      icon: "portal",
      text: `Wormhole gate${NB}1 still leads to where Tree farm was, X${NB}−160 · Z${NB}−140`,
    });
    // A gate in the Overworld leading to a Nether place says where it comes from.
    expect(placeLines(place("Nether base"), gates, null)[1]?.text).toBe(
      `Wormhole gate${NB}3 leads here, from in the Overworld`,
    );
  });

  it("the goal line is this place's share, never the goal's total (S2)", () => {
    const got: MinecraftGoalProgress = {
      xuid: "2535411",
      online: true,
      goals: [
        {
          id: "g",
          world: "slot2",
          world_name: "Castle Hill",
          loaded: true,
          item: "minecraft:iron_block",
          need: 23,
          have: 14,
          carried: 5,
          carried_why: null,
          boxes: 0,
          chests: [{ place: "Home", count: 9, distance: 14, far: false }],
          here: 0,
          chests_why: null,
          detail: null,
          age_s: 1,
          as_of: 1,
          flush: null,
        },
        {
          id: "elsewhere",
          world: "slot3",
          world_name: "Other",
          loaded: false,
          item: "minecraft:glass",
          need: 5,
          have: 5,
          carried: null,
          carried_why: "not_loaded",
          boxes: 0,
          chests: [{ place: "Home", count: 5, distance: null, far: false }],
          here: null,
          chests_why: null,
          detail: null,
          age_s: 1,
          as_of: 1,
          flush: null,
        },
      ],
    };
    const w = goalWords(place("Home"), "slot2", got);
    expect(w.head).toBe(`Goals count chests within 48${NB}blocks`);
    expect(w.lines).toEqual(["9 of the 23 iron blocks are in chests here"]);
    expect(w.foot).toMatch(/nearest saved place, or toward BlockyFox where they stand/);
    expect(goalWords(place("Tree farm"), "slot2", got).lines).toEqual(["none here yet"]);
    expect(goalWords(place("Home"), "slot9", got).lines).toEqual([
      "BlockyFox has no counted goals here",
    ]);
    expect(goalWords(place("Home"), "slot2", "loading").lines).toEqual(["counting…"]);
    expect(goalWords(place("Home"), "slot2", "failed").lines).toEqual(["can't count right now"]);
  });

  it("In this world lists them by player, in any dimension, underground or up high", () => {
    const groups = placeGroups(places, roster);
    expect(groups.map((g) => [g.gamertag, g.token, g.rows.length])).toEqual([
      ["BlockyFox", "--steel", 3],
      ["Mira_P", "--green", 2],
      ["Steve42", "--amber", 2],
    ]);
    const subs = groups.flatMap((g) => g.rows.map((r) => `${r.title}: ${r.sub}`));
    expect(subs).toContain(`Home: X${NB}70 · Z${NB}12 · protected`);
    expect(subs).toContain(`Tree farm: X${NB}−160 · Z${NB}−140 · gate${NB}1 leads here`);
    expect(subs).toContain(
      `Nether base: in the Nether · X${NB}−20 · Z${NB}40 · gate${NB}3 leads here`,
    );
    expect(subs).toContain(`Mine outpost: X${NB}−110 · Z${NB}40 · down at Y${NB}−12`);
    expect(subs).toContain(`Lookout: X${NB}−150 · Z${NB}−236 · up at Y${NB}140`);
  });
});

describe("villagers (villagers and pets A)", () => {
  const e = mcMapEntities("slot2", "overworld");
  const village: MinecraftStructure = { structure: "village", x: 232, z: -304 };

  it("a village is one tile; the located one keeps its amber tile, at its spot", () => {
    const marks = villageMarks(e, [village], ALL_JOBS);
    expect(marks.map((m) => [m.label, m.x, m.z, !!m.struct])).toEqual([
      ["Village · 23 villagers", 232, -304, true],
      ["8 villagers", 100, 140, false],
    ]);
    // A village structure too far away doesn't lend its tile.
    expect(
      villageMarks(e, [{ structure: "village", x: 900, z: 900 }], ALL_JOBS)[0]?.struct,
    ).toBeNull();
  });

  it("a filter retitles the tile and keeps only villages with a match", () => {
    const libs = villageMarks(e, [village], { kind: "job", job: "librarian" });
    expect(libs.map((m) => m.label)).toEqual(["Village · 4 librarians", "1 librarian"]);
    const mending = villageMarks(e, [village], { kind: "enchant", enchant: "mending" });
    expect(mending.map((m) => [m.label, m.size])).toEqual([["Village · 2 sell Mending", 23]]);
    expect(villageLabel(false, 1, { kind: "enchant", enchant: "mending" })).toBe("1 sells Mending");
    // A truncated listing can only count a village whole.
    const cut: MinecraftMapEntities = { ...e, villagers: [], villagers_truncated: true };
    expect(villageMarks(cut, [], ALL_JOBS).map((m) => m.count)).toEqual([23, 8]);
  });

  it("chips: every job, Mending when anyone sells it, the commonest jobs", () => {
    expect(villagerChips(e).map((c) => c.label)).toEqual([
      "All jobs",
      "Sells Mending",
      "Farmers",
      "Librarians",
      "Clerics",
      "Fishermen",
      "Armorers",
    ]);
    expect(villagerChips(null).map((c) => c.label)).toEqual(["All jobs"]);
  });

  it("thirteen jobs share seven glyphs; an unknown job is a person", () => {
    expect(jobOf("librarian").glyph).toBe("book");
    expect(jobOf("weaponsmith").glyph).toBe("hammer");
    expect(jobOf("minecraft:cartographer").glyph).toBe("mapFold");
    expect(jobOf("beekeeper")).toMatchObject({ glyph: "user", one: "Beekeeper" });
    expect(villagerTitle({ job: "librarian", level: 3 })).toBe("Librarian · Expert");
  });

  it("C's ranked list under a chip: nearest first, with the way and the price", () => {
    const rows = rankedVillagers(
      e,
      { kind: "enchant", enchant: "mending" },
      {
        x: 42,
        z: 12,
        who: "BlockyFox",
      },
    );
    expect(rows).toHaveLength(2);
    expect(rows[0]?.title).toBe("Librarian · Expert");
    expect(rows[0]?.sub).toMatch(
      new RegExp(`^\\d+${NB}blocks north-east of BlockyFox · sells Mending · job locked in$`),
    );
    expect(rankedVillagers(e, ALL_JOBS, { x: 0, z: 0, who: null })).toEqual([]);
  });

  it("a villager's card: every trade it offers now, by level, used up said in words", () => {
    const card = mcMapVillager("slot2", e.villagers[0]?.id ?? "").found;
    const tiers = tradeTiers(card, { kind: "enchant", enchant: "mending" });
    expect(tiers.map((t) => t.level)).toEqual(["Novice", "Apprentice", "Journeyman", "Expert"]);
    expect(tiers[0]?.trades[0]).toMatchObject({
      what: "Emerald",
      price: "for 24 paper",
      uses: "3 of 16 used",
    });
    const book = tiers[1]?.trades[0];
    expect(book).toMatchObject({
      what: "Enchanted book",
      enchant: `Mending${NB}I`,
      price: "for 12 emeralds and 1 book",
      hit: true,
    });
    expect(tiers[1]?.trades[1]).toMatchObject({ uses: "used up · restocks", gone: true });
    expect(tiers[2]?.trades[0]?.what).toBe("Glass ×4");
    expect(tradesNote(card, "as of the world's last save, 6 min ago")).toBe(
      "Its trades now, levels Novice to Expert. Master trades are told when it gets there. As of the world's last save, 6 min ago, from the saved world.",
    );
    expect(tradeTiers(null, ALL_JOBS)).toEqual([]);
  });
});

describe("pets and named mobs", () => {
  const e = mcMapEntities("slot2", "overworld");
  const rex = e.pets.find((p) => p.name === "Rex") as MinecraftMob;
  const stray = e.pets.find((p) => !p.owner_name) as MinecraftMob;
  const clover = e.named[0] as MinecraftMob;

  it("a pet is whose, its coat, and sitting or free; an unknown owner is said (S6)", () => {
    expect(mobWords(rex)).toEqual({
      title: "Rex",
      sub: "BlockyFox's dog · pale · sitting",
      whose: "BlockyFox",
    });
    expect(mobWords(stray)).toEqual({
      title: "A cat",
      sub: "a cat · owner not known yet · tabby · sitting",
      whose: null,
    });
    expect(mobFrom(stray, null, "overworld")).toBe(
      "Its owner isn't known yet: they haven't asked Dave anything.",
    );
    expect(mobFrom(rex, fox, "overworld")).toBe(`42${NB}blocks south of BlockyFox · online`);
    expect(mobWords(clover)).toEqual({
      title: "Clover",
      sub: "a named cow · nobody's",
      whose: null,
    });
    expect(mobFrom(clover, null, "overworld")).toBeNull();
  });

  it("a chip keeps one player's pets and no named mobs; their list is nearest them", () => {
    expect(petChips(e)).toEqual(["BlockyFox", "Mira_P", "Steve42"]);
    expect(petsShown(e, "mira_p").pets.map((p) => p.name)).toEqual(["Whiskers", "Kiwi"]);
    expect(petsShown(e, "Mira_P").named).toEqual([]);
    expect(petsShown(e, null).named.map((m) => m.name)).toEqual(["Clover", "Hopper"]);
    const rows = rankedPets(e, "Steve42", { x: -96, z: -40, who: "Steve42" });
    expect(rows.map((r) => r.title)).toEqual(["Biscuit", "Luna"]);
    expect(rows[1]?.sub).toMatch(/^dog · \d+ blocks south-west of Steve42 · sitting$/);
    expect(rankedPets(e, null, { x: 0, z: 0, who: null })).toEqual([]);
  });

  it("the age: no number on a live world, the last save's on a stopped one (S3)", () => {
    expect(entitiesAge(e, 0)).toBe("as of the world's last save, re-read every minute");
    const saved = { ...e, live: false, as_of: 1000 };
    expect(entitiesAge(saved, 1360)).toBe("as of the world's last save, 6 min ago");
    expect(entitiesAge({ ...saved, as_of: null }, 1)).toBe("the world hasn't been saved yet");
    expect(entitiesAge(null, 1)).toBe("not read yet");
    expect(villagersLine(saved, 1360)).toBe(
      "31 in 2 villages · as of the world's last save, 6 min ago · a village is one tile",
    );
    expect(petNote(rex, "as of the world's last save, 6 min ago")).toBe(
      "Where Rex was as of the world's last save, 6 min ago. A sitting pet stays put; one free to roam may have wandered since.",
    );
    expect(petNote(clover, "x")).toBe("Where Clover was x. It may have wandered since.");
  });
});

describe("one cluster pass for every marker but the players (X2)", () => {
  const at = (sx: number, sy: number, pr: number, cr?: number) => ({
    x: 0,
    z: 0,
    sx,
    sy,
    pr,
    cr,
  });

  it("a pet beside a gate tile merges with it, as any two markers within 40 px do", () => {
    const got = clusterMarkers([at(0, 0, 2), at(20, 0, 1.6)]);
    expect(got).toHaveLength(1);
    expect(got[0]?.members).toHaveLength(2);
  });

  it("split villagers merge only within 22 px; one beside a tile still merges at 22", () => {
    expect(clusterMarkers([at(0, 0, 1.8, 22), at(30, 0, 1.8, 22)])).toHaveLength(2);
    expect(clusterMarkers([at(0, 0, 1.8, 22), at(18, 0, 1.8, 22)])).toHaveLength(1);
    expect(clusterMarkers([at(0, 0, 2), at(30, 0, 1.8, 22)])).toHaveLength(2);
  });

  it("a village's count goes on a corner no gate line crosses (A1)", () => {
    // A line through the top right corner: the count moves to the top left.
    const seg = { ax: 0, ay: -30, bx: 40, by: 0 };
    expect(cornerBox(0, 0, 20, [seg])).toEqual({ x: -26, y: -20, w: 20, h: 16 });
    expect(cornerBox(0, 0, 20, [])).toEqual({ x: 6, y: -20, w: 20, h: 16 });
  });
});
