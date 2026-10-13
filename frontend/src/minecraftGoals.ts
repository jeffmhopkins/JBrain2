// Players' goals on the Minecraft screen (plan §M24; GUI gate docs/mocks/minecraft-goals/,
// variant B "expandable if clicked", look-only): the pure reading of the goals and a count.

import type {
  MinecraftGoal,
  MinecraftGoalCount,
  MinecraftJournalEntry,
  MinecraftPlayerGoals,
} from "./api/client";
import { agoOf, plural } from "./minecraft";

/** Kept apart in the glance and every line: a value and its noun never wrap apart. */
export const NB = " ";

export function isCounted(g: MinecraftGoal): boolean {
  return g.item !== null && g.item_count !== null;
}

export function openGoals(pg: MinecraftPlayerGoals): MinecraftGoal[] {
  return pg.goals.filter((g) => g.status === "open");
}

export function finishedGoals(pg: MinecraftPlayerGoals): MinecraftGoal[] {
  return pg.goals.filter((g) => g.status !== "open");
}

/** Whether counting this player's goals asks the server anything. */
export function needsCount(pg: MinecraftPlayerGoals): boolean {
  return openGoals(pg).some(isCounted);
}

/** The goal a Players-table glance shows — the first open counted one, else the first
 *  open one — and how many other open goals there are. Null with no open goals. */
export function glanceGoal(pg: MinecraftPlayerGoals): { goal: MinecraftGoal; more: number } | null {
  const open = openGoals(pg);
  const goal = open.find(isCounted) ?? open[0];
  return goal ? { goal, more: open.length - 1 } : null;
}

// Item ids that read as a quantity, not a count of things: "200 glass", never "200 glasses".
const MASS =
  /(wool|concrete|terracotta|glass|stone|sand|dirt|gravel|clay|obsidian|netherrack|wheat|sugar|redstone|gunpowder|meal|snow|ice|iron|gold|copper|netherite|deepslate|andesite|diorite|granite|tuff|calcite|glowstone|bread|kelp|moss|mud|string|leather|lava|water|honey|quartz|coal|lapis_lazuli|amethyst|blackstone|basalt|prismarine|purpur|end_stone|soul_soil|cobweb|scaffolding|bamboo|cactus)$/;

/** A goal's item as words, plural for a count above one: "iron blocks", "torches",
 *  "glass". Ids already plural ("oak_leaves", "wheat_seeds") stay as they are. */
export function itemWords(item: string, count: number): string {
  const id = item.replace(/^[a-z0-9_]+:/, "");
  const words = id.replace(/_/g, " ");
  if (count === 1 || MASS.test(id) || id.endsWith("s")) return words;
  if (/(x|z|ch|sh)$/.test(id)) return `${words}es`;
  if (/[^aeiou]y$/.test(id)) return `${words.slice(0, -1)}ies`;
  return `${words}s`;
}

/** "+1 more goal": said as goals, never beside the count where it reads as items short. */
export function moreText(more: number): string {
  return more > 0 ? `+${more}${NB}more ${more === 1 ? "goal" : "goals"}` : "";
}

/** "1 done · 1 abandoned" for a player's finished goals; "" with none. */
export function finishedText(pg: MinecraftPlayerGoals): string {
  const fin = finishedGoals(pg);
  const done = fin.filter((g) => g.status === "done").length;
  const gave = fin.length - done;
  return [done ? `${done}${NB}done` : "", gave ? `${gave}${NB}abandoned` : ""]
    .filter(Boolean)
    .join(" · ");
}

export function isReached(c: MinecraftGoalCount | null | undefined): boolean {
  return !!c && c.have !== null && c.have >= c.need;
}

/** "as of 3 min ago": the files' age when counted, carried forward to now. */
export function asOfText(c: MinecraftGoalCount, nowMs: number): string {
  if (c.as_of === null) return "";
  return `as${NB}of${NB}${agoOf(c.as_of, nowMs).replace(/ /g, NB)}`;
}

/** Where a place is from the player, for its source row. */
export function placeWhere(distance: number | null, far: boolean, tag: string): string {
  if (distance !== null) return `${plural(distance, "block").replace(" ", NB)} from ${tag}`;
  return far ? `in another dimension from ${tag}` : "";
}

/** The line under a counted goal: where its number comes from, and how old it is. */
export function whyParts(c: MinecraftGoalCount, tag: string, nowMs: number): string[] {
  const bits: string[] = [];
  if (c.carried !== null) bits.push(`${c.carried}${NB}carried`);
  for (const ch of c.chests) {
    if (ch.count) bits.push(`${ch.count}${NB}in chests near ${ch.place}`);
  }
  if (c.here) bits.push(`${c.here}${NB}in chests near ${tag}`);
  if (c.carried_why === "offline") bits.push(`what ${tag} carries counts while they're on`);
  if (c.carried_why === "failed") bits.push(`what ${tag} carries wasn't counted`);
  if (c.chests_why === "no_places") bits.push("no saved places, so no chests");
  if (c.chests_why === "no_world") bits.push("its world isn't on the server any more");
  if (c.chests_why === "failed") bits.push("chests weren't counted");
  if (bits.length === 0) bits.push("none in chests near their places");
  const age = asOfText(c, nowMs);
  if (age && c.chests_why === null) bits.push(age);
  return bits;
}

/** A goal's journal, newest first. */
export function journalOf(pg: MinecraftPlayerGoals, goalId: string): MinecraftJournalEntry[] {
  return pg.journal.filter((e) => e.goal_id === goalId);
}

/** The player's latest journal entries, any goal, newest first. */
export function latestJournal(pg: MinecraftPlayerGoals, n = 5): MinecraftJournalEntry[] {
  return pg.journal
    .slice()
    .sort((a, b) => b.at - a.at)
    .slice(0, n);
}

export function sourceText(e: MinecraftJournalEntry, tag: string): string {
  switch (e.source) {
    case "player":
      return `${tag}, in game`;
    case "dave":
      return "Dave";
    case "owner":
      return "you";
    default:
      return "auto";
  }
}
