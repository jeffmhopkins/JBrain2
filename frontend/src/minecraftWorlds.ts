// The Worlds sub-screen's derived state and copy (DESIGN.md "Minecraft server screen" →
// "Worlds and backups"; binding mock docs/mocks/minecraft-worlds/b-worlds-subscreen.html).
// Pure, so the guards the owner relies on — the pinned-delete refusal, the import size
// limits, Same seed only for a known seed, live vs pending — are tested once and said the
// same way on every page.

import type {
  MinecraftBackup,
  MinecraftJob,
  MinecraftRuleValue,
  MinecraftRules,
  MinecraftSlot,
} from "./api/client";
import { clockOf, dayOf, listOf, shortDate } from "./minecraft";

// ---- slots ----

export const isEmptySlot = (s: MinecraftSlot) => !s.exists && !s.name;
/** Created but never loaded: it is generated on its first Load. */
export const notGenerated = (s: MinecraftSlot) => !s.exists && !!s.name;
export const slotNumber = (id: string) => id.replace(/^slot/, "");
export const slotName = (s: MinecraftSlot) => s.name ?? `Slot ${slotNumber(s.id)}`;

export const cap = (s: string) => (s ? `${s[0]?.toUpperCase()}${s.slice(1)}` : s);

export const NAME_MAX = 40;
export const LABEL_MAX = 40;
export const GAMEMODES = ["survival", "creative", "adventure"] as const;
export const DIFFICULTIES = ["peaceful", "easy", "normal", "hard"] as const;

export function fmtBytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  if (mb >= 100) return `${Math.round(mb)} MB`;
  return `${mb.toFixed(1)} MB`;
}

/** `today, 2:33 pm` or `Oct 4, 8:20 pm`. */
export function whenOf(epochS: number, nowMs: number): string {
  const day = dayOf(epochS, nowMs);
  return `${day}, ${clockOf(epochS)}`;
}

/** Live means loaded AND running; anything else waits for the next start or load. */
export function pendWhen(s: MinecraftSlot, running: boolean): string {
  if (!s.active) return `when ${slotName(s)} next loads`;
  return running ? "at the next restart" : "when the server starts";
}

export function settingsLine(s: MinecraftSlot, running: boolean): string {
  if (!s.active)
    return `Not loaded — a change is saved and applied when ${slotName(s)} next loads.`;
  if (!running) return "Loaded, server stopped — a change is applied when the server starts.";
  return "Loaded and running — difficulty changes live; game mode and cheats apply at the next restart.";
}

export function originText(s: MinecraftSlot): string {
  const made = s.created_at ? ` ${shortDate(s.created_at)}` : "";
  switch (s.origin) {
    case "imported":
      return `imported${made} from a .mcworld — settings came from the file`;
    case "restored":
      return `restored${made} from a backup`;
    case "reset":
      return `reset${made}`;
    case "new":
      return `new world${made ? `,${made}` : ""}`;
    default:
      return s.id === "slot1" ? "made on first boot" : "—";
  }
}

export function playedText(s: MinecraftSlot, running: boolean, online: number, nowMs: number) {
  if (notGenerated(s)) return "not generated yet — made the first time it loads";
  if (s.active && running) return online ? "playing now" : "loaded · nobody on";
  return s.last_played ? `last played ${whenOf(s.last_played, nowMs)}` : "never played";
}

// ---- seeds ----

export const SEED_MAX = 64;
export type SeedMode = "random" | "enter";

/** The seed a form sends, or null while Enter is chosen with nothing (or too much) typed.
 *  Random sends the rolled number it showed, so the box records exactly what was seen. */
export function seedFor(mode: SeedMode, rolled: string | null, typed: string): string | null {
  if (mode === "random") return rolled;
  const seed = typed.trim();
  if (!seed || seed.length > SEED_MAX || /[\r\n]/.test(seed)) return null;
  return seed;
}

// ---- import ----

/** The sidecar's hard limit, matched byte for byte so the refusal happens here first. */
export const MAX_IMPORT_BYTES = 1024 * 1024 * 1024;
/** Cloudflare's tunnel turns down request bodies over 100 MB; at home on the LAN there's
 *  no limit, so this only warns. */
export const TUNNEL_BYTES = 100 * 1000 * 1000;

export type ImportCheck = "ok" | "tunnel" | "too_big";

export function importCheck(size: number): ImportCheck {
  if (size > MAX_IMPORT_BYTES) return "too_big";
  if (size > TUNNEL_BYTES) return "tunnel";
  return "ok";
}

/** A world's name from its file: `Hilltop Village.mcworld` → `Hilltop Village`. */
export function nameFromFile(fileName: string): string {
  return fileName
    .replace(/\.(mcworld|zip)$/i, "")
    .trim()
    .slice(0, NAME_MAX);
}

// ---- reset ----

export interface ResetOption {
  mode: "same_seed" | "new_seed" | "empty";
  title: string;
  sub: string;
  disabled: boolean;
}

export function resetOptions(s: MinecraftSlot): ResetOption[] {
  return [
    {
      mode: "same_seed",
      title: "Same seed",
      sub: s.seed
        ? `The original terrain, fresh — everything built is gone. Seed ${s.seed}.`
        : "Not offered — this world's seed is unknown (its level.dat couldn't be read).",
      disabled: !s.seed,
    },
    {
      mode: "new_seed",
      title: "New seed",
      sub: "A brand-new world in this slot, same name and settings.",
      disabled: false,
    },
    {
      mode: "empty",
      title: "Empty",
      sub: s.active
        ? "Not offered — it's the loaded world. Load another first."
        : "Frees the slot. Its backups stay.",
      disabled: s.active,
    },
  ];
}

/** The option a Reset sheet opens on: Same seed when it can, else New seed. */
export const defaultResetMode = (s: MinecraftSlot): ResetOption["mode"] =>
  s.seed ? "same_seed" : "new_seed";

// ---- backups ----

export const PINNED_DELETE_WHY = "Unpin it first — a pinned backup can't be deleted.";
export const canDeleteBackup = (b: MinecraftBackup) => !b.pinned;

/** The automatic backups' reasons, from the sidecar's `pre-…` labels. */
export function autoReason(label: string): string {
  const update = label.match(/^pre-(?:auto-)?update-(.+)$/);
  if (update) return `before the update from ${update[1]}`;
  switch (label) {
    case "pre-load":
      return "before loading another world";
    case "pre-import":
      return "before an import";
    case "pre-reset":
      return "before a reset";
    case "pre-restore":
      return "before a restore";
    default:
      return label ? label.replace(/^pre-/, "before ").replace(/-/g, " ") : "automatic";
  }
}

export function backupTitle(b: MinecraftBackup): string {
  if (b.auto) return cap(autoReason(b.label));
  return b.label || "Your backup";
}

/** How a sentence names it: `“first night”` or `the Oct 4 automatic backup`. */
export function backupShort(b: MinecraftBackup): string {
  if (!b.auto && b.label) return `“${b.label}”`;
  return `the ${shortDate(b.created)} ${b.auto ? "automatic " : ""}backup`;
}

/** The backup the next one removes when a world already keeps `keep`: the oldest automatic
 *  one, else the oldest of the owner's (the sidecar's prune order). Pinned ones never go. */
export function nextToGo(backups: MinecraftBackup[], keep: number): MinecraftBackup | null {
  const unpinned = backups.filter((b) => !b.pinned);
  if (unpinned.length < keep) return null;
  const oldest = (xs: MinecraftBackup[]) =>
    xs.reduce<MinecraftBackup | null>((a, b) => (a === null || b.created < a.created ? b : a), null);
  return oldest(unpinned.filter((b) => b.auto)) ?? oldest(unpinned);
}

// ---- jobs ----

export const PHASE_TEXT: Record<string, string> = {
  "warning players": "players got a 10-second warning in chat",
  "backing up": "taking a safety backup first",
  stopping: "saving the world and stopping",
  writing: "writing the world into its slot",
  starting: "starting the server again",
};

export function elapsedOf(startedAt: number | null, nowMs: number): string {
  const t = startedAt === null ? 0 : Math.max(0, Math.round(nowMs / 1000 - startedAt));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

/** The Worlds & backups row's job line: `Importing a world — writing…`. */
export function jobLine(job: MinecraftJob): string {
  return `${cap(job.what)}${job.phase ? ` — ${job.phase}…` : "…"}`;
}

/** Why every world action is off right now, or "" when they're on. One lifecycle lock
 *  covers updates and world operations alike, so a refusal is said, not hidden. */
export function worldBlocked(job: MinecraftJob | null, updating: boolean, state: string): string {
  if (updating) {
    return "Busy updating Minecraft — loading, importing, resetting, restoring and backing up come back when it's done.";
  }
  if (job) return `Wait — the server is ${job.what}.`;
  if (state === "starting" || state === "stopping" || state === "restarting") {
    return `Wait for the server to finish ${state}.`;
  }
  return "";
}

// ---- confirms (one sentence each, in the shared Dialog) ----

export interface WorldConfirm {
  title: string;
  body: string;
  confirmLabel: string;
  tone: "danger" | "warn" | "primary";
}

export interface ServerCtx {
  running: boolean;
  online: string[];
}

const warned = (online: string[]) =>
  `${listOf(online)} get a 10-second warning in chat and are disconnected`;

/** The tail for an act that stops the loaded world: the chat warning, or the restart. */
function bounce(target: MinecraftSlot, ctx: ServerCtx): string {
  if (!target.active || !ctx.running) return "";
  return ctx.online.length
    ? ` — ${warned(ctx.online)} for about a minute`
    : " — the server restarts around it";
}

const dangerIfPlayers = (target: MinecraftSlot, ctx: ServerCtx, otherwise: "warn" | "primary") =>
  target.active && ctx.running && ctx.online.length ? "danger" : otherwise;

export function loadConfirm(
  target: MinecraftSlot,
  from: MinecraftSlot | null,
  ctx: ServerCtx,
): WorldConfirm {
  const t = slotName(target);
  const f = from ? slotName(from) : "the loaded world";
  const gen = notGenerated(target) ? " and is generated" : "";
  let body: string;
  if (!ctx.running) {
    // A stopped server has nothing unsaved, so no backup is taken (verified fact 14).
    body = `${t} becomes the loaded world — the server stays stopped until you start it.`;
  } else if (ctx.online.length) {
    body = `${warned(ctx.online)}; ${f} is backed up first, then ${t} starts${gen}.`;
  } else {
    body = `The server stops, ${f} is backed up, then ${t} starts${gen} — nobody is on.`;
  }
  return {
    title: `Load ${t}?`,
    body,
    confirmLabel: "Load",
    tone: ctx.running && ctx.online.length ? "danger" : "primary",
  };
}

export function importOverConfirm(
  target: MinecraftSlot,
  worldName: string,
  ctx: ServerCtx,
): WorldConfirm {
  const t = slotName(target);
  return {
    title: `Import over ${t}?`,
    body: `${t} is backed up first, then replaced by ${worldName} with the file's own settings${bounce(target, ctx)}.`,
    confirmLabel: "Import",
    tone: dangerIfPlayers(target, ctx, "warn"),
  };
}

export function resetConfirm(
  target: MinecraftSlot,
  mode: ResetOption["mode"],
  ctx: ServerCtx,
): WorldConfirm {
  const t = slotName(target);
  const what =
    mode === "empty"
      ? "emptied"
      : mode === "same_seed"
        ? "rebuilt from the same seed"
        : "replaced by a new world";
  const first = target.exists ? `${t} is backed up first, then ${what}` : `${t} is ${what}`;
  const players =
    target.active && ctx.running && ctx.online.length ? `, and ${warned(ctx.online)}` : "";
  return {
    title: `Reset ${t}?`,
    body: `${first} — everything built there leaves the slot${players}.`,
    confirmLabel: mode === "empty" ? "Empty slot" : "Reset",
    tone: "danger",
  };
}

export function restoreConfirm(
  b: MinecraftBackup,
  target: MinecraftSlot,
  ctx: ServerCtx,
  nowMs: number,
): WorldConfirm {
  const same = b.folder === target.folder;
  if (isEmptySlot(target)) {
    return {
      title: `Restore into slot ${slotNumber(target.id)}?`,
      body: `${cap(backupShort(b))} becomes a new world in the empty slot ${slotNumber(target.id)}; nothing else changes.`,
      confirmLabel: "Restore",
      tone: "warn",
    };
  }
  const t = slotName(target);
  return {
    title: `Restore into ${t}?`,
    body: `${t} is backed up first, then replaced with ${backupShort(b)} from ${whenOf(b.created, nowMs)}${same ? "" : ", its seed and settings included"}${bounce(target, ctx)}.`,
    confirmLabel: "Restore",
    tone: dangerIfPlayers(target, ctx, "warn"),
  };
}

/** What the Restore sheet says about each target. */
export function restoreTargetText(
  b: MinecraftBackup,
  target: MinecraftSlot,
  source: MinecraftSlot | null,
  ctx: ServerCtx,
): string {
  const restarts = target.active && ctx.running ? ", and the server restarts around it" : "";
  if (isEmptySlot(target)) {
    const from = source ? slotName(source) : b.folder;
    return `empty — becomes “${from} (${shortDate(b.created)})” with the backup's seed and settings and ${from}'s rules`;
  }
  const t = slotName(target);
  if (b.folder === target.folder) return `its own world — rolled back; ${t} is backed up first${restarts}`;
  return `replaces ${t} — the backup's seed and settings come too; ${t} is backed up first${restarts}`;
}

export function deleteConfirm(b: MinecraftBackup): WorldConfirm {
  return {
    title: `Delete ${!b.auto && b.label ? `“${b.label}”` : "this backup"}?`,
    body: `It's removed from the box for good${b.downloaded_at ? " — your downloaded copy is unaffected" : ""}.`,
    confirmLabel: "Delete",
    tone: "danger",
  };
}

// ---- game rules ----

export type RuleKind = "bool" | "num" | "choice";

export interface RuleDef {
  id: string;
  label: string;
  sub?: string;
  group: string;
  def: MinecraftRuleValue;
  kind: RuleKind;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
}

type Row = [
  string,
  string,
  MinecraftRuleValue,
  string?,
  { min: number; max: number; step: number; unit?: string }?,
];

const BIG = { min: 0, max: 2147483647, step: 1000 };

// Every rule the owner's server reports (BDS 1.26.52.3), in plain words; bounds are sensible
// UI limits, not Bedrock's own.
const GROUPS: [string, string, Row[]][] = [
  [
    "world",
    "World",
    [
      ["doFireTick", "Fire spreads", true],
      ["tntExplodes", "TNT explodes", true],
      ["mobGriefing", "Mobs can change blocks", true, "creepers blow holes, endermen move blocks"],
      ["projectilesCanBreakBlocks", "Arrows and tridents can break blocks", true],
      [
        "respawnBlocksExplode",
        "Beds and respawn anchors can explode",
        true,
        "used in the wrong dimension",
      ],
      ["tntExplosionDropDecay", "Some blocks blown up by TNT don't drop", false],
      [
        "randomTickSpeed",
        "Random tick speed",
        1,
        "how fast crops grow and leaves decay",
        { min: 0, max: 4096, step: 1 },
      ],
    ],
  ],
  [
    "time",
    "Time & weather",
    [
      ["doDayLightCycle", "Day and night cycle", true],
      ["doWeatherCycle", "Weather changes", true],
      ["doInsomnia", "Phantoms come when nobody sleeps", true],
      [
        "playersSleepingPercentage",
        "Sleepers needed to skip the night",
        100,
        "percent of players",
        { min: 0, max: 100, step: 5, unit: "%" },
      ],
    ],
  ],
  [
    "players",
    "Players",
    [
      ["keepInventory", "Keep inventory on death", false],
      ["naturalRegeneration", "Health comes back on its own", true],
      ["pvp", "Players can hurt each other", true],
      ["fallDamage", "Fall damage", true],
      ["fireDamage", "Fire damage", true],
      ["drowningDamage", "Drowning damage", true],
      ["freezeDamage", "Freezing damage", true, "powder snow"],
      ["doImmediateRespawn", "Respawn without the death screen", false],
      [
        "spawnRadius",
        "Spawn radius",
        10,
        "blocks around world spawn",
        { min: 0, max: 128, step: 1 },
      ],
      ["showCoordinates", "Show coordinates", false],
      ["showDaysPlayed", "Show days played", false],
      ["showDeathMessages", "Death messages in chat", true],
      ["showTags", "Show item tags", true, "“can place on” and “can destroy” lines"],
      ["locatorbar", "Locator bar", true],
      ["playerWaypoints", "Players shown on the locator bar", "everyone"],
    ],
  ],
  [
    "mobs",
    "Mobs & drops",
    [
      ["doMobSpawning", "Mobs spawn naturally", true],
      ["doMobLoot", "Mobs drop loot", true],
      ["doEntityDrops", "Boats, minecarts and frames drop as items", true],
      ["doTileDrops", "Broken blocks drop items", true],
    ],
  ],
  [
    "crafting",
    "Crafting",
    [
      ["recipesUnlock", "Recipes unlock as you play", true],
      ["doLimitedCrafting", "Only unlocked recipes can be crafted", false],
      ["showRecipeMessages", "“Recipe unlocked” messages", true],
    ],
  ],
  [
    "commands",
    "Commands",
    [
      ["commandBlocksEnabled", "Command blocks work", true],
      ["commandBlockOutput", "Command blocks report in chat", true],
      ["sendCommandFeedback", "Commands reply in chat", true],
      ["maxCommandChainLength", "Longest command chain", 65535, undefined, BIG],
      ["functionCommandLimit", "Most commands per function", 10000, undefined, BIG],
    ],
  ],
  ["display", "Display", [["showBorderEffect", "World border effect", true]]],
];

const kindOf = (v: MinecraftRuleValue): RuleKind =>
  typeof v === "boolean" ? "bool" : typeof v === "number" ? "num" : "choice";

export const RULES: Record<string, RuleDef> = {};
export const RULE_GROUPS: { id: string; title: string; rules: RuleDef[] }[] = GROUPS.map(
  ([id, title, rows]) => ({
    id,
    title,
    rules: rows.map(([rid, label, def, sub, bounds]) => {
      const r: RuleDef = { id: rid, label, group: id, def, kind: kindOf(def), ...bounds };
      if (sub) r.sub = sub;
      RULES[rid] = r;
      return r;
    }),
  }),
);

/** The catalogue's defaults: what a new world starts with. */
export const DEFAULT_RULES: Record<string, MinecraftRuleValue> = Object.fromEntries(
  Object.values(RULES).map((r) => [r.id, r.def]),
);

/** The groups for a set of reported rules. A rule a later server adds lands in "Other"
 *  under its own id rather than being hidden. */
export function groupsFor(
  values: Record<string, MinecraftRuleValue>,
): { id: string; title: string; rules: RuleDef[] }[] {
  const groups = RULE_GROUPS.map((g) => ({ ...g, rules: g.rules.filter((r) => r.id in values) }));
  const other = Object.keys(values)
    .filter((id) => !RULES[id])
    .sort()
    .map((id): RuleDef => {
      const v = values[id] as MinecraftRuleValue;
      return { id, label: id, group: "other", def: v, kind: kindOf(v), ...BIG, step: 1 };
    });
  if (other.length) groups.push({ id: "other", title: "Other", rules: other });
  return groups.filter((g) => g.rules.length);
}

const fmtNum = (v: number) => v.toLocaleString("en-US");

export function ruleWord(r: RuleDef, v: MinecraftRuleValue): string {
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "number") return `${fmtNum(v)}${r.unit ?? ""}`;
  return v;
}

/** `playerWaypoints` offers only what the server reports, plus "everyone" — its full value
 *  set isn't known, so nothing is guessed. */
export const choiceOptions = (v: MinecraftRuleValue) => [...new Set([String(v), "everyone"])];

export function clampRule(r: RuleDef, n: number): number {
  return Math.max(r.min ?? 0, Math.min(r.max ?? Number.MAX_SAFE_INTEGER, Math.round(n)));
}

export interface RulesNote {
  tone: "live" | "wait";
  head: string;
  rest: string;
}

/** The sticky note atop the rules: live only when the API says the slot is loaded and
 *  running; otherwise a change waits for the server's start or the world's next load. */
export function rulesNote(view: MinecraftRules, s: MinecraftSlot): RulesNote {
  if (view.live) {
    return {
      tone: "live",
      head: "Loaded and running — changes apply live, at once.",
      rest: "Saved rules are also re-applied every time the server starts.",
    };
  }
  if (s.active) {
    return {
      tone: "wait",
      head: "Server stopped — changes are applied when it starts.",
      rest: "Until then they're marked pending. Saved rules are re-applied on every start.",
    };
  }
  return {
    tone: "wait",
    head: `Not loaded — changes are saved and applied when ${slotName(s)} next loads.`,
    rest: "Until then they're marked pending.",
  };
}

/** The ids whose value differs from the default. */
export function changedRules(view: Pick<MinecraftRules, "rules" | "defaults">): string[] {
  return Object.keys(view.rules).filter(
    (id) => id in view.defaults && view.rules[id] !== view.defaults[id],
  );
}

export function ruleSavedToast(r: RuleDef, v: MinecraftRuleValue, live: boolean, when: string) {
  const what = r.label.toLowerCase();
  return live ? `Applied live — ${what}: ${ruleWord(r, v)}` : `Saved — ${what} changes ${when}`;
}
