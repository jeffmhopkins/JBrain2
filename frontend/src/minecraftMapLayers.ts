// Saved places, villagers and pets on the Minecraft map (GUI gate
// docs/mocks/minecraft-map-layers/, places A and villagers-and-pets A, owner 2026-10-13; plan
// §M8 and §M4e). The pure half: whose colour a pin takes, what each card and list row
// says, how villagers fold into a village tile and what a filter chip keeps. The canvas half
// is in screens/minecraftMapDraw.ts.

import type {
  MinecraftDim,
  MinecraftGate,
  MinecraftGoalProgress,
  MinecraftMapEntities,
  MinecraftMapPlaces,
  MinecraftMapPlayer,
  MinecraftMob,
  MinecraftPlace,
  MinecraftStructure,
  MinecraftTrade,
  MinecraftVillager,
  MinecraftVillagerCard,
} from "./api/client";
import { itemWords } from "./minecraftGoals";
import { type GlyphKey, type RosterEntry, agoWords } from "./minecraftMap";
import { bearingTo, gateSpot, num, shortDate } from "./minecraftMapMarkers";

const NB = "\u00a0";
/** How far round a place goals count its chests (§M24's NEAR_BLOCKS), as a radius. */
export const REACH_BLOCKS = 48;
/** From this many px a block a village splits into its villagers' job discs. */
export const SPLIT_SCALE = 3;
/** Split, villager discs merge closer than this, not the map's 40 px: a village's houses
 *  are a few blocks apart. */
export const VILLAGER_CLUSTER_PX = 22;

const IN_DIM: Record<MinecraftDim, string> = {
  overworld: "in the Overworld",
  nether: "in the Nether",
  the_end: "in the End",
};

const xz = (o: { x: number; z: number }) => `X${NB}${num(o.x)} · Z${NB}${num(o.z)}`;
const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/** A player's colour token from the roster, by key and then by gamertag; null for someone
 *  the map has never seen in this world (their places and pets take the neutral ring). */
export function tokenFor(roster: RosterEntry[], xuid: string | null, gamertag: string) {
  const r =
    roster.find((q) => xuid !== null && q.xuid === xuid) ??
    roster.find((q) => gamertag !== "" && same(q.gamertag, gamertag));
  return r?.token ?? null;
}

// ---- saved places ----

/** The Saved places row's line: how many, whose, and that each is named. */
export function placesLine(m: MinecraftMapPlaces | null): string {
  const list = m?.places ?? [];
  if (!list.length) return "none saved in this world yet · players save them through Dave";
  const counts = new Map<string, number>();
  for (const p of list) counts.set(p.gamertag, (counts.get(p.gamertag) ?? 0) + 1);
  const who = [...counts].map(([g, n]) => `${g}${NB}${n}`).join(" · ");
  return `${list.length} in this world · ${who} · each labelled`;
}

/** The card's sub line: whose, when it was put here, which dimension. */
export function placeSub(p: MinecraftPlace): string {
  return `${p.gamertag}'s saved place · ${p.moved ? "moved" : "saved"} ${shortDate(p.saved_at)} · ${IN_DIM[p.dim]}`;
}

export interface CardLine {
  tone: "" | "ok" | "gate" | "warn";
  icon: "shield" | "user" | "portal" | "target";
  text: string;
  /** The leading words in bold, as the mock sets them. */
  lead?: string;
}

/** The place card's lines: protected or not, where its player is, the gates aimed at it
 *  (by its block), and a gate still aimed where it used to be (the review's X1). */
export function placeLines(
  p: MinecraftPlace,
  gates: MinecraftGate[],
  player: MinecraftMapPlayer | null,
): CardLine[] {
  const out: CardLine[] = [];
  const b = p.covered[0];
  if (b) {
    const by = !b.placed_by ? "a" : same(b.placed_by, p.gamertag) ? "their" : `${b.placed_by}'s`;
    out.push({
      tone: "ok",
      icon: "shield",
      lead: "Protected",
      text: ` · by ${by} No-Spawn Beacon at ${xz(b)}`,
    });
  } else {
    out.push({ tone: "", icon: "shield", text: "Not protected · no No-Spawn Beacon covers it" });
  }
  if (player) {
    const state = player.online ? "online" : "offline, where they logged off";
    if (player.dim !== p.dim) {
      out.push({
        tone: "",
        icon: "user",
        text: `${p.gamertag} is ${IN_DIM[player.dim]} · ${state}`,
      });
    } else {
      const { way, blocks } = bearingTo(p.x + 0.5, p.z + 0.5, player.x, player.z);
      out.push({
        tone: "",
        icon: "user",
        text:
          blocks < 2
            ? `${p.gamertag} is here · ${state}`
            : `${p.gamertag} is ${blocks}${NB}blocks ${way} of it · ${state}`,
      });
    }
  }
  for (const id of p.gates) {
    const g = gates.find((q) => q.id === id);
    if (!g) continue;
    const from = g.dim === p.dim ? `from ${xz(gateSpot(g))}` : `from ${IN_DIM[g.dim]}`;
    out.push({ tone: "gate", icon: "portal", text: `Wormhole gate${NB}${id} leads here, ${from}` });
  }
  for (const id of p.gates_moved) {
    const g = gates.find((q) => q.id === id);
    const t = g?.target;
    if (!t) continue;
    out.push({
      tone: "warn",
      icon: "portal",
      text: `Wormhole gate${NB}${id} still leads to where ${p.name} was, ${xz(t)}${t.dim !== p.dim ? ` ${IN_DIM[t.dim]}` : ""}`,
    });
  }
  return out;
}

/** The goal line under a place: this place's share of each open counted goal (§M24 — a
 *  chest counts toward its nearest place, or toward the player where they stand). */
export function goalWords(
  p: MinecraftPlace,
  world: string,
  got: MinecraftGoalProgress | "loading" | "failed" | null,
): { head: string; lines: string[]; foot: string | null } {
  const head = `Goals count chests within ${REACH_BLOCKS}${NB}blocks`;
  if (got === "loading" || got === null) return { head, lines: ["counting…"], foot: null };
  if (got === "failed") return { head, lines: ["can't count right now"], foot: null };
  const here = got.goals.filter((g) => !g.world || g.world === world);
  if (!here.length) return { head, lines: [`${p.gamertag} has no counted goals here`], foot: null };
  const lines: string[] = [];
  for (const g of here) {
    const c = g.chests.find((q) => same(q.place, p.name));
    if (!c || c.count <= 0) continue;
    const of = Math.min(c.count, g.need);
    lines.push(
      `${of} of the ${g.need} ${itemWords(g.item, g.need)} ${of === 1 ? "is" : "are"} in chests here`,
    );
  }
  return {
    head,
    lines: lines.length ? lines : ["none here yet"],
    foot: `Each chest counts once, toward its nearest saved place, or toward ${p.gamertag} where they stand.`,
  };
}

export interface PlaceGroup {
  gamertag: string;
  token: string | null;
  rows: { key: string; title: string; sub: string; p: MinecraftPlace }[];
}

/** The Layers tab's "In this world" for places: by player, every dimension, so a place
 *  underground, in the sky or in the Nether can still be found. */
export function placeGroups(m: MinecraftMapPlaces | null, roster: RosterEntry[]): PlaceGroup[] {
  const out: PlaceGroup[] = [];
  for (const p of m?.places ?? []) {
    let g = out.find((q) => q.gamertag === p.gamertag);
    if (!g) {
      g = { gamertag: p.gamertag, token: tokenFor(roster, p.xuid, p.gamertag), rows: [] };
      out.push(g);
    }
    const height =
      p.y !== null && p.y < 0
        ? ` · down at Y${NB}${num(p.y)}`
        : p.y !== null && p.y > 120
          ? ` · up at Y${NB}${p.y}`
          : "";
    const where = p.dim === "overworld" ? xz(p) : `${IN_DIM[p.dim]} · ${xz(p)}`;
    const gate = p.gates.length ? ` · gate${NB}${p.gates.join(", ")} leads here` : "";
    g.rows.push({
      key: `pl:${p.id}`,
      title: p.name,
      sub: `${where}${height}${p.covered.length ? " · protected" : ""}${gate}`,
      p,
    });
  }
  return out;
}

/** Where a place is, as a key a gate's aim is compared with (by block, X1). */
export const spotKey = (dim: MinecraftDim, x: number, z: number) => `${dim}@${x},${z}`;

// ---- villagers ----

export const JOBS: Record<string, { glyph: GlyphKey; one: string; many: string }> = {
  librarian: { glyph: "book", one: "Librarian", many: "librarians" },
  cleric: { glyph: "flask", one: "Cleric", many: "clerics" },
  farmer: { glyph: "wheat", one: "Farmer", many: "farmers" },
  fisherman: { glyph: "fish", one: "Fisherman", many: "fishermen" },
  armorer: { glyph: "hammer", one: "Armorer", many: "armorers" },
  toolsmith: { glyph: "hammer", one: "Toolsmith", many: "toolsmiths" },
  weaponsmith: { glyph: "hammer", one: "Weaponsmith", many: "weaponsmiths" },
  cartographer: { glyph: "mapFold", one: "Cartographer", many: "cartographers" },
  shepherd: { glyph: "user", one: "Shepherd", many: "shepherds" },
  fletcher: { glyph: "user", one: "Fletcher", many: "fletchers" },
  butcher: { glyph: "user", one: "Butcher", many: "butchers" },
  leatherworker: { glyph: "user", one: "Leatherworker", many: "leatherworkers" },
  mason: { glyph: "user", one: "Mason", many: "masons" },
  unemployed: { glyph: "user", one: "Unemployed", many: "unemployed" },
  nitwit: { glyph: "user", one: "Nitwit", many: "nitwits" },
};

export function jobOf(job: string) {
  const key = job
    .toLowerCase()
    .replace(/^minecraft:/, "")
    .replace(/\s+/g, "_");
  return (
    JOBS[key] ?? {
      glyph: "user" as GlyphKey,
      one: key ? key[0]?.toUpperCase() + key.slice(1).replace(/_/g, " ") : "Villager",
      many: "villagers",
    }
  );
}

export const LEVELS = ["Novice", "Apprentice", "Journeyman", "Expert", "Master"];

/** "Librarian · Expert". */
export const villagerTitle = (v: { job: string; level: number }) =>
  `${jobOf(v.job).one} · ${LEVELS[v.level] ?? "Novice"}`;

export type VillagerFilter =
  | { kind: "all" }
  | { kind: "job"; job: string }
  | { kind: "enchant"; enchant: string };

export const ALL_JOBS: VillagerFilter = { kind: "all" };

export const filterKey = (f: VillagerFilter) =>
  f.kind === "all" ? "all" : f.kind === "job" ? `job:${f.job}` : `ench:${f.enchant}`;

export function matches(v: MinecraftVillager, f: VillagerFilter): boolean {
  if (f.kind === "job") return v.job === f.job;
  if (f.kind === "enchant") return v.enchants.includes(f.enchant);
  return true;
}

const titleCase = (w: string) => w.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/** The chips under the Villagers row: every job, the most common jobs, and Mending when
 *  anyone sells it (the book players ask Dave for most). */
export function villagerChips(
  e: MinecraftMapEntities | null,
): { f: VillagerFilter; label: string }[] {
  const out: { f: VillagerFilter; label: string }[] = [{ f: ALL_JOBS, label: "All jobs" }];
  const vs = e?.villagers ?? [];
  if (vs.some((v) => v.enchants.includes("mending"))) {
    out.push({ f: { kind: "enchant", enchant: "mending" }, label: "Sells Mending" });
  }
  const counts = new Map<string, number>();
  for (const v of vs) {
    if (v.job === "unemployed" || v.job === "nitwit") continue;
    counts.set(v.job, (counts.get(v.job) ?? 0) + 1);
  }
  const jobs = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 5);
  for (const [job] of jobs) {
    out.push({ f: { kind: "job", job }, label: titleCase(jobOf(job).many) });
  }
  return out;
}

/** A village tile's plate: *Village · 23 villagers* on a located village, *8 villagers*
 *  on a group with none; a filter retitles it (*Village · 4 librarians*). */
export function villageLabel(located: boolean, n: number, f: VillagerFilter): string {
  const word =
    f.kind === "job"
      ? n === 1
        ? jobOf(f.job).one.toLowerCase()
        : jobOf(f.job).many
      : f.kind === "enchant"
        ? `sell${n === 1 ? "s" : ""} ${titleCase(f.enchant)}`
        : n === 1
          ? "villager"
          : "villagers";
  return located ? `Village · ${n} ${word}` : `${n} ${word}`;
}

export interface VillageMark {
  id: string;
  x: number;
  z: number;
  /** The villagers it stands for under the filter. */
  count: number;
  /** All of its villagers, for the card's "in the village of 23". */
  size: number;
  /** The server's located village it takes the place of, when one is within reach. */
  struct: MinecraftStructure | null;
  label: string;
}

/** The zoomed-out village tiles: one per group with any villager the filter keeps. A
 *  group with a located `village` structure within reach keeps that amber tile, at the
 *  structure's spot, rather than a second tile beside it. */
export function villageMarks(
  e: MinecraftMapEntities | null,
  structs: MinecraftStructure[],
  f: VillagerFilter,
): VillageMark[] {
  if (!e) return [];
  const kept = new Map<string, number>();
  for (const v of e.villagers)
    if (matches(v, f)) kept.set(v.village, (kept.get(v.village) ?? 0) + 1);
  const used = new Set<MinecraftStructure>();
  const out: VillageMark[] = [];
  for (const g of e.villages) {
    // A truncated listing can't count by filter; the group's own count stands in.
    const n = e.villagers_truncated && f.kind === "all" ? g.count : (kept.get(g.id) ?? 0);
    if (!n) continue;
    const s =
      structs.find(
        (q) =>
          q.structure === "village" &&
          !used.has(q) &&
          Math.hypot(q.x - g.x, q.z - g.z) <= REACH_BLOCKS,
      ) ?? null;
    if (s) used.add(s);
    out.push({
      id: g.id,
      x: s ? s.x : g.x,
      z: s ? s.z : g.z,
      count: n,
      size: g.count,
      struct: s,
      label: villageLabel(!!s, n, f),
    });
  }
  return out;
}

export interface RankedRow {
  key: string;
  title: string;
  sub: string;
  v: MinecraftVillager;
}

/** C's ranked list under a chip: the matches nearest whoever is online in this dimension
 *  (else the middle of the map), as Dave's finder ranks them. */
export function rankedVillagers(
  e: MinecraftMapEntities | null,
  f: VillagerFilter,
  from: { x: number; z: number; who: string | null },
  limit = 8,
): RankedRow[] {
  if (!e || f.kind === "all") return [];
  return e.villagers
    .filter((v) => matches(v, f))
    .map((v) => ({ v, d: Math.hypot(v.x - from.x, v.z - from.z) }))
    .sort((a, b) => a.d - b.d)
    .slice(0, limit)
    .map(({ v }) => {
      const { way, blocks } = bearingTo(from.x, from.z, v.x + 0.5, v.z + 0.5);
      const of = from.who ? ` of ${from.who}` : " of the map's middle";
      const sells = f.kind === "enchant" ? ` · sells ${titleCase(f.enchant)}` : "";
      return {
        key: `vv:${v.id}`,
        title: villagerTitle(v),
        sub: `${blocks}${NB}blocks ${way}${of}${sells}${v.locked_in ? " · job locked in" : ""}`,
        v,
      };
    });
}

const ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"];

function stackWords([item, n]: [string, number]): string {
  // Paper is a quantity in a trade, as glass is: "24 paper".
  return `${n} ${item.endsWith("paper") ? "paper" : itemWords(item, n)}`;
}

/** What a trade sells, in the mock's words: *Emerald*, *Glass ×4*, *Enchanted book ·
 *  Mending I*. */
export function tradeSells(t: MinecraftTrade): { what: string; enchant: string | null } {
  const [item, n] = t.sell;
  const word = item.replace(/^[a-z0-9_]+:/, "").replace(/_/g, " ");
  const what = `${word[0]?.toUpperCase() ?? ""}${word.slice(1)}${n > 1 ? ` ×${n}` : ""}`;
  const enchant = t.enchantments.length
    ? t.enchantments.map(([e, lvl]) => `${titleCase(e)}${NB}${ROMAN[lvl] ?? lvl}`).join(", ")
    : null;
  return { what, enchant };
}

export interface TradeTier {
  level: string;
  trades: {
    key: string;
    what: string;
    enchant: string | null;
    price: string;
    uses: string;
    gone: boolean;
    hit: boolean;
  }[];
}

/** A villager card's trades now, grouped by level (owner decision: every trade it offers
 *  now). A trade the filter asked for is tinted. */
export function tradeTiers(card: MinecraftVillagerCard["found"], f: VillagerFilter): TradeTier[] {
  if (!card) return [];
  const out: TradeTier[] = [];
  card.trades.forEach((t, i) => {
    const level = LEVELS[t.tier] ?? "Novice";
    let tier = out.find((q) => q.level === level);
    if (!tier) {
      tier = { level, trades: [] };
      out.push(tier);
    }
    const { what, enchant } = tradeSells(t);
    tier.trades.push({
      key: `${i}`,
      what,
      enchant,
      price: `for ${stackWords(t.buy)}${t.buy2 ? ` and ${stackWords(t.buy2)}` : ""}`,
      uses: t.used_up ? "used up · restocks" : `${t.uses} of ${t.max_uses} used`,
      gone: t.used_up,
      hit: f.kind === "enchant" && t.enchantments.some(([e]) => e === f.enchant),
    });
  });
  return out;
}

/** The footnote under a villager's trades: what isn't told, and how old it is. */
export function tradesNote(card: MinecraftVillagerCard["found"], age: string): string {
  const top = LEVELS[card?.level ?? 0] ?? "Novice";
  const upTo = card && card.level > 0 ? `levels Novice to ${top}` : "Novice";
  const more =
    card && card.level < 4 ? ` ${LEVELS[card.level + 1]} trades are told when it gets there.` : "";
  return `Its trades now, ${upTo}.${more} ${age[0]?.toUpperCase() ?? ""}${age.slice(1)}, from the saved world.`;
}

// ---- pets and named mobs ----

const PET_WORDS: Record<string, string> = { wolf: "dog" };

export const kindWord = (kind: string) => PET_WORDS[kind] ?? kind.replace(/_/g, " ");

/** A pet's or named mob's card title and sub line. An owner the world doesn't know yet
 *  (they've never asked Dave) is said, never guessed. */
export function mobWords(m: MinecraftMob): { title: string; sub: string; whose: string | null } {
  const kind = kindWord(m.kind);
  const whose = m.owner_name ? m.owner_name : null;
  const traits = [...m.coat.slice(0, 1), ...(m.baby ? ["young"] : [])];
  if (!m.tamed) {
    return {
      title: m.name || `A ${kind}`,
      sub: [`a named ${kind}`, "nobody's", ...traits].join(" · "),
      whose: null,
    };
  }
  const owner = whose ? `${whose}'s ${kind}` : `a ${kind} · owner not known yet`;
  return {
    title: m.name || (whose ? `${whose}'s ${kind}` : `A ${kind}`),
    sub: [owner, ...traits, m.sitting ? "sitting" : "free to roam"].join(" · "),
    whose,
  };
}

/** The line under a pet's coordinates: how far from its owner, when they're on. */
export function mobFrom(m: MinecraftMob, owner: MinecraftMapPlayer | null, dim: MinecraftDim) {
  if (!m.tamed) return null;
  if (!m.owner_name) return "Its owner isn't known yet: they haven't asked Dave anything.";
  if (!owner) return null;
  const state = owner.online ? "online" : "offline";
  if (owner.dim !== dim) return `${m.owner_name} is ${IN_DIM[owner.dim]} · ${state}`;
  const { way, blocks } = bearingTo(owner.x, owner.z, m.x + 0.5, m.z + 0.5);
  return `${blocks}${NB}blocks ${way} of ${m.owner_name} · ${state}`;
}

/** The pets a chip keeps: everyone's, or one player's (and then no named mobs). */
export function petsShown(e: MinecraftMapEntities | null, of: string | null) {
  const pets = (e?.pets ?? []).filter((p) => of === null || same(p.owner_name ?? "", of));
  return { pets, named: of === null ? (e?.named ?? []) : [] };
}

/** C's ranked list under a player's pets chip: their pets, nearest them first (or the
 *  middle of the map while they're off). */
export function rankedPets(
  e: MinecraftMapEntities | null,
  of: string | null,
  from: { x: number; z: number; who: string | null },
  limit = 8,
): { key: string; title: string; sub: string; m: MinecraftMob }[] {
  if (!e || of === null) return [];
  return petsShown(e, of)
    .pets.map((m) => ({ m, d: Math.hypot(m.x - from.x, m.z - from.z) }))
    .sort((a, b) => a.d - b.d)
    .slice(0, limit)
    .map(({ m }) => {
      const { way, blocks } = bearingTo(from.x, from.z, m.x + 0.5, m.z + 0.5);
      const of$ = from.who ? ` of ${from.who}` : " of the map's middle";
      return {
        key: `pet:${m.id}`,
        title: mobWords(m).title,
        sub: `${kindWord(m.kind)} · ${blocks}${NB}blocks ${way}${of$} · ${m.sitting ? "sitting" : "free to roam"}`,
        m,
      };
    });
}

/** The chips under the Pets row: everyone's, then each owner the world knows. */
export function petChips(e: MinecraftMapEntities | null): string[] {
  const names = new Set<string>();
  for (const p of e?.pets ?? []) if (p.owner_name) names.add(p.owner_name);
  return [...names].sort((a, b) => a.localeCompare(b));
}

// ---- how old it is (the review's S3) ----

/** How old the saved world's mobs are. The age is the newest write to the world's files:
 *  on a live world that's seconds, though a mob's own record is only as new as its chunk's
 *  last save, so no number is said there — only that it's re-read. */
export function entitiesAge(e: MinecraftMapEntities | null, now: number): string {
  if (!e) return "not read yet";
  if (e.live) return "as of the world's last save, re-read every minute";
  if (e.as_of === null) return "the world hasn't been saved yet";
  return `as of the world's last save, ${agoWords(e.as_of, now)}`;
}

export function villagersLine(e: MinecraftMapEntities | null, now: number): string {
  if (!e) return "from the saved world · a village is one tile";
  const total = e.villages.reduce((a, v) => a + v.count, 0);
  if (!total) return `none in the saved world here · ${entitiesAge(e, now)}`;
  const n = e.villages.length;
  return `${total} in ${n} ${n === 1 ? "village" : "villages"} · ${entitiesAge(e, now)} · a village is one tile`;
}

export function petsLine(e: MinecraftMapEntities | null, now: number): string {
  if (!e) return "tamed animals and named mobs, from the saved world";
  const p = e.pets.length;
  const n = e.named.length;
  return `${p} ${p === 1 ? "pet" : "pets"} · ${n} named · ${entitiesAge(e, now)}`;
}

/** The pet card's footnote. */
export function petNote(m: MinecraftMob, age: string): string {
  const who = m.name || `This ${kindWord(m.kind)}`;
  if (!m.tamed) return `Where ${who} was ${age}. It may have wandered since.`;
  return `Where ${who} was ${age}. A sitting pet stays put; one free to roam may have wandered since.`;
}
